"""
Store setup & menu provisioning — the shop-side half of the customer menu.

Migration ``a9e4c7b25d13`` made the customer menu fail closed: a product reaches a
guest only through a ``store_products`` row that says THIS branch publishes it,
and nothing was backfilled (docs/CUSTOMER_MENU_SCOPING.md). That was the right
boundary and it left one thing missing — there was no way to write those rows
outside ``scripts/seed_demo_data.py`` and a psql prompt
(RUNTIME_PRODUCT_GAP_REVIEW F-13). A shop could be *protected* from test debris
but could not be *set up*.

This module is where an owner or manager takes those decisions. Everything here
obeys four rules:

  1. **The branch is the session's branch.** Every function takes ``store_id``
     from the router, which takes it from the authenticated session. No caller
     path exists that accepts a client-supplied store.
  2. **Catalog and publication stay separate.** ``products`` is what the chain can
     sell; ``store_products`` is what one branch does sell. Creating a product
     publishes nothing unless a caller asks for this branch by name.
  3. **The menu predicate is written once.** ``on_customer_menu`` below is the
     same three-way condition ``menu_service.list_menu_products`` joins on. A
     screen that re-derived it would drift the day a fourth condition appears.
  4. **Nothing here touches stock, orders, money or the inventory lifecycle.**
     Publishing a product reserves nothing and consumes nothing.

Idempotency, honestly stated: the publication endpoints are naturally idempotent
(publishing twice leaves one row; withdrawing twice leaves none), so they need no
``Idempotency-Key`` and none is required. Product and table CREATION are not
naturally idempotent, and this branch does not add an idempotency-key ledger for
them — a duplicate-name guard is what makes a double-submitted form safe instead.
The residual race is named in docs/STORE_SETUP_AND_MENU_PROVISIONING.md rather
than papered over.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from fastapi import HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core import messages
from app.models.product import Product
from app.models.store import Store
from app.models.store_product import StoreProduct
from app.models.table import Table
from app.models.table_qr_token import QR_TOKEN_STATUS_ACTIVE, TableQrToken
from app.services import qr_token_service
from app.services.audit_service import audit


# ── Errors ───────────────────────────────────────────────────────────────────

def _not_found(message: str, error: str) -> HTTPException:
    return HTTPException(status_code=404, detail={"error": error, "message": message})


def _conflict(message: str, error: str) -> HTTPException:
    return HTTPException(status_code=409, detail={"error": error, "message": message})


def _invalid(message: str, error: str) -> HTTPException:
    return HTTPException(status_code=422, detail={"error": error, "message": message})


# ── The one menu predicate ───────────────────────────────────────────────────

def on_customer_menu(product: Product, offering: Optional[StoreProduct]) -> bool:
    """
    Would a guest sitting in this branch see this product right now?

    Deliberately the same three conditions ``menu_service.list_menu_products``
    joins on, written once so the setup screen and the customer's phone can never
    disagree:

      * the branch published it (an offering row exists),
      * the branch has not switched it off for the day (``is_available``),
      * the chain has not retired it (``products.is_active``).
    """
    return bool(offering is not None and offering.is_available and product.is_active)


# ── Catalog + this branch's publication state ────────────────────────────────

@dataclass(frozen=True)
class MenuRow:
    """One catalog product as this branch sees it."""
    product: Product
    offering: Optional[StoreProduct]

    @property
    def published(self) -> bool:
        return self.offering is not None

    @property
    def visible(self) -> bool:
        return on_customer_menu(self.product, self.offering)


def list_menu_rows(db: Session, store_id: int) -> list[MenuRow]:
    """
    The whole chain catalog, LEFT-joined to this branch's publication rows.

    An outer join and not an inner one, because the screen's job is to answer
    "what could I put on my menu?" as well as "what is on it?". A manager who can
    only see what they have already published cannot publish anything else, which
    is the position every branch is in immediately after migration
    ``a9e4c7b25d13``.

    What this does NOT expose is any other branch's decision: the join is filtered
    to ``store_id`` before it is applied, so a product Moda sells and Kadıköy does
    not appears here as simply "not published", exactly as an unknown product
    would. Which branch sells what is not derivable from this response.

    Order: published items first in menu order, then the rest of the catalog by
    name. Ties broken by name then id so the list never reshuffles between two
    loads and a manager never clicks the row that has moved.
    """
    rows = (
        db.query(Product, StoreProduct)
        .outerjoin(
            StoreProduct,
            (StoreProduct.product_id == Product.id)
            & (StoreProduct.store_id == store_id),
        )
        .order_by(
            # Published first (False sorts before True in Postgres, so negate).
            (StoreProduct.id == None).asc(),  # noqa: E711
            StoreProduct.sort_order.asc(),
            Product.name.asc(),
            Product.id.asc(),
        )
        .all()
    )
    return [MenuRow(product=p, offering=o) for p, o in rows]


def _product_or_404(db: Session, product_id: int) -> Product:
    product = db.get(Product, product_id)
    if product is None:
        raise _not_found(messages.MENU_PRODUCT_NOT_FOUND, "product_not_found")
    return product


def _offering(db: Session, store_id: int, product_id: int) -> Optional[StoreProduct]:
    return (
        db.query(StoreProduct)
        .filter(
            StoreProduct.store_id == store_id,
            StoreProduct.product_id == product_id,
        )
        .first()
    )


def _name_taken(db: Session, name: str, *, exclude_product_id: int | None = None) -> bool:
    """
    Is this product name already in the CHAIN catalog (case-insensitively)?

    Chain-wide and not per-branch, because ``products`` is chain-wide: two rows
    called "Fıstıklı Waffle" are one catalog with an ambiguity in it, whichever
    branches publish them.

    This is also what makes a double-submitted create form safe in the absence of
    an idempotency key — the second POST is refused with a 409 instead of quietly
    minting a twin. It is an application check, not a database constraint, so two
    genuinely simultaneous creates could still both pass; that residual race is
    documented rather than hidden, and its worst outcome is a duplicate row a
    manager can retire.
    """
    q = db.query(Product.id).filter(func.lower(Product.name) == name.strip().lower())
    if exclude_product_id is not None:
        q = q.filter(Product.id != exclude_product_id)
    return db.query(q.exists()).scalar()


def _clean_name(raw: str | None) -> str:
    name = (raw or "").strip()
    if not name:
        raise _invalid(messages.MENU_PRODUCT_NAME_REQUIRED, "product_name_required")
    return name


def _check_price(price: Decimal | None) -> Decimal:
    if price is None or Decimal(str(price)) <= 0:
        raise _invalid(messages.MENU_PRICE_INVALID, "invalid_price")
    return Decimal(str(price))


# ── Product create / edit ────────────────────────────────────────────────────

def create_product(
    db: Session,
    *,
    store_id: int,
    name: str,
    category: Optional[str],
    base_price: Decimal,
    is_active: bool,
    publish_to_current_store: bool,
    actor_user_id: int,
    ip_address: str | None = None,
) -> MenuRow:
    """
    Add a product to the chain catalog, and optionally to THIS branch's menu.

    ``publish_to_current_store`` publishes to the caller's own branch and no
    other. There is no "publish everywhere" here and there is no store parameter
    to point somewhere else: a new product is invisible to every guest in every
    other branch until somebody sitting in that branch decides otherwise.

    A product created inactive is a catalog draft — it can be published, and it
    still will not reach a guest, because ``products.is_active`` gates the menu
    join regardless of the publication row.
    """
    clean = _clean_name(name)
    price = _check_price(base_price)
    if _name_taken(db, clean):
        raise _conflict(messages.MENU_PRODUCT_NAME_TAKEN, "product_name_taken")

    product = Product(
        name=clean,
        category=(category or "").strip() or None,
        base_price=price,
        is_active=is_active,
    )
    db.add(product)
    db.flush()

    offering: Optional[StoreProduct] = None
    if publish_to_current_store:
        offering = _insert_offering(db, store_id, product.id)

    audit(
        db,
        entity_type="product",
        entity_id=product.id,
        action="created",
        actor_type="STAFF",
        actor_id=str(actor_user_id),
        payload_after={
            "name": product.name,
            "category": product.category,
            "base_price": product.base_price,
            "is_active": product.is_active,
            # Which branch, if any, this product was put in front of guests in.
            "published_store_id": store_id if publish_to_current_store else None,
        },
        ip_address=ip_address,
    )
    db.commit()
    db.refresh(product)
    if offering is not None:
        db.refresh(offering)
    return MenuRow(product=product, offering=offering)


def update_product(
    db: Session,
    *,
    store_id: int,
    product_id: int,
    name: Optional[str],
    category: Optional[str],
    base_price: Optional[Decimal],
    is_active: Optional[bool],
    actor_user_id: int,
    ip_address: str | None = None,
) -> MenuRow:
    """
    Edit the safe fields of a catalog product. Omitted fields are left alone.

    ``is_active=False`` is the destructive one and it is chain-wide: the product
    vanishes from EVERY branch's customer menu and becomes unorderable everywhere,
    even where a ``store_products`` row still points at it. It is not a delete —
    the publication rows and every historical order line survive, so yesterday's
    receipts still say what was sold — but it is not reversible from the guest's
    point of view until somebody switches it back on.
    """
    product = _product_or_404(db, product_id)
    before = {
        "name": product.name,
        "category": product.category,
        "base_price": product.base_price,
        "is_active": product.is_active,
    }

    if name is not None:
        clean = _clean_name(name)
        if _name_taken(db, clean, exclude_product_id=product.id):
            raise _conflict(messages.MENU_PRODUCT_NAME_TAKEN, "product_name_taken")
        product.name = clean
    if category is not None:
        product.category = category.strip() or None
    if base_price is not None:
        product.base_price = _check_price(base_price)
    if is_active is not None:
        product.is_active = is_active

    audit(
        db,
        entity_type="product",
        entity_id=product.id,
        action="updated",
        actor_type="STAFF",
        actor_id=str(actor_user_id),
        payload_before=before,
        payload_after={
            "name": product.name,
            "category": product.category,
            "base_price": product.base_price,
            "is_active": product.is_active,
        },
        ip_address=ip_address,
    )
    db.commit()
    db.refresh(product)
    return MenuRow(product=product, offering=_offering(db, store_id, product.id))


# ── Publication (this branch's menu) ─────────────────────────────────────────

def _next_sort_order(db: Session, store_id: int) -> int:
    """
    Append position for a newly published item.

    New items land at the END of the branch's menu rather than at position 0.
    Inserting at the top would silently reorder a menu somebody has already
    arranged — and the printed board on the wall would stop matching the screen.
    """
    current = (
        db.query(func.max(StoreProduct.sort_order))
        .filter(StoreProduct.store_id == store_id)
        .scalar()
    )
    return int(current) + 1 if current is not None else 0


def _insert_offering(db: Session, store_id: int, product_id: int) -> StoreProduct:
    offering = StoreProduct(
        store_id=store_id,
        product_id=product_id,
        is_available=True,
        sort_order=_next_sort_order(db, store_id),
    )
    db.add(offering)
    db.flush()
    return offering


@dataclass(frozen=True)
class PublicationResult:
    row: MenuRow
    changed: bool


def publish_product(
    db: Session,
    *,
    store_id: int,
    product_id: int,
    actor_user_id: int,
    ip_address: str | None = None,
) -> PublicationResult:
    """
    Put a catalog product on THIS branch's customer menu.

    Idempotent: publishing something already published changes nothing and reports
    ``changed=False``. It deliberately does not reset ``is_available`` either — a
    manager pressing "menüye ekle" on an item that is merely sold out for the day
    has not said the pistachio arrived.

    Publishing an INACTIVE product is allowed and does exactly what it says: the
    branch has decided to sell it, and it still will not appear to a guest until
    the chain reactivates it. Refusing here would force a manager to reactivate an
    item chain-wide before they could arrange their own menu.
    """
    product = _product_or_404(db, product_id)
    existing = _offering(db, store_id, product_id)
    if existing is not None:
        return PublicationResult(MenuRow(product, existing), changed=False)

    offering = _insert_offering(db, store_id, product_id)
    audit(
        db,
        entity_type="store_product",
        entity_id=offering.id,
        action="published",
        actor_type="STAFF",
        actor_id=str(actor_user_id),
        payload_after={
            "store_id": store_id,
            "product_id": product_id,
            "sort_order": offering.sort_order,
        },
        ip_address=ip_address,
    )
    db.commit()
    db.refresh(offering)
    db.refresh(product)
    return PublicationResult(MenuRow(product, offering), changed=True)


def unpublish_product(
    db: Session,
    *,
    store_id: int,
    product_id: int,
    actor_user_id: int,
    ip_address: str | None = None,
) -> PublicationResult:
    """
    Take a product off THIS branch's customer menu.

    The offering ROW is deleted, which is what "we stopped selling this here"
    means in this model — as distinct from ``is_available=False``, which means
    "not today". The product itself, its price, and every order ever placed for it
    are untouched; only this branch's decision to sell it goes.

    The guest-facing effect is immediate and total: the menu join has nothing to
    join to, so the item disappears from the branch's menu and ``order_service``
    refuses it at submit time even for a guest whose phone still shows the old
    list.

    Idempotent: withdrawing something that was never published reports
    ``changed=False`` rather than a 404 the screen would have to explain.
    """
    product = _product_or_404(db, product_id)
    existing = _offering(db, store_id, product_id)
    if existing is None:
        return PublicationResult(MenuRow(product, None), changed=False)

    offering_id = existing.id
    payload_before = {
        "store_id": store_id,
        "product_id": product_id,
        "is_available": existing.is_available,
        "sort_order": existing.sort_order,
    }
    db.delete(existing)
    db.flush()
    audit(
        db,
        entity_type="store_product",
        entity_id=offering_id,
        action="unpublished",
        actor_type="STAFF",
        actor_id=str(actor_user_id),
        payload_before=payload_before,
        ip_address=ip_address,
    )
    db.commit()
    db.refresh(product)
    return PublicationResult(MenuRow(product, None), changed=True)


def set_availability(
    db: Session,
    *,
    store_id: int,
    product_id: int,
    is_available: bool,
    actor_user_id: int,
    ip_address: str | None = None,
) -> PublicationResult:
    """
    Switch a published item off for the day, or back on.

    Requires an existing publication row and says so (409 ``not_published``)
    rather than quietly creating one: "sold out" is a statement about something
    this branch sells, and inferring a publication decision from it would put an
    item on the menu by way of a button that says it is unavailable.
    """
    product = _product_or_404(db, product_id)
    offering = _offering(db, store_id, product_id)
    if offering is None:
        raise _conflict(messages.MENU_PRODUCT_NOT_PUBLISHED, "not_published")

    if offering.is_available == is_available:
        return PublicationResult(MenuRow(product, offering), changed=False)

    before = offering.is_available
    offering.is_available = is_available
    audit(
        db,
        entity_type="store_product",
        entity_id=offering.id,
        action="availability_changed",
        actor_type="STAFF",
        actor_id=str(actor_user_id),
        payload_before={"is_available": before},
        payload_after={
            "store_id": store_id,
            "product_id": product_id,
            "is_available": is_available,
        },
        ip_address=ip_address,
    )
    db.commit()
    db.refresh(offering)
    db.refresh(product)
    return PublicationResult(MenuRow(product, offering), changed=True)


def set_sort_order(
    db: Session,
    *,
    store_id: int,
    product_id: int,
    sort_order: int,
    actor_user_id: int,
    ip_address: str | None = None,
) -> PublicationResult:
    """
    Move a published item within THIS branch's menu order.

    Nothing is renumbered around it. Two items may share a number; the customer
    menu breaks the tie by name and then id, so the guest's list stays
    deterministic either way and a manager is never forced to rewrite the whole
    board to move one row.
    """
    if sort_order < 0:
        raise _invalid(messages.MENU_SORT_ORDER_INVALID, "invalid_sort_order")

    product = _product_or_404(db, product_id)
    offering = _offering(db, store_id, product_id)
    if offering is None:
        raise _conflict(messages.MENU_PRODUCT_NOT_PUBLISHED, "not_published")

    if offering.sort_order == sort_order:
        return PublicationResult(MenuRow(product, offering), changed=False)

    before = offering.sort_order
    offering.sort_order = sort_order
    audit(
        db,
        entity_type="store_product",
        entity_id=offering.id,
        action="sort_order_changed",
        actor_type="STAFF",
        actor_id=str(actor_user_id),
        payload_before={"sort_order": before},
        payload_after={
            "store_id": store_id,
            "product_id": product_id,
            "sort_order": sort_order,
        },
        ip_address=ip_address,
    )
    db.commit()
    db.refresh(offering)
    db.refresh(product)
    return PublicationResult(MenuRow(product, offering), changed=True)


# ── Tables & QR ──────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class TableRow:
    table: Table
    token: Optional[TableQrToken]


def _active_token(db: Session, table_id: int) -> Optional[TableQrToken]:
    return (
        db.query(TableQrToken)
        .filter(
            TableQrToken.table_id == table_id,
            TableQrToken.status == QR_TOKEN_STATUS_ACTIVE,
        )
        .first()
    )


def list_tables(db: Session, store_id: int) -> list[TableRow]:
    """
    The caller's own branch's tables, each with its live QR record.

    One query for the tables and one for their active tokens — not one token
    query per row. A branch with forty tables is not a reason for forty
    round-trips.
    """
    tables = (
        db.query(Table)
        .filter(Table.store_id == store_id)
        .order_by(Table.table_number, Table.id)
        .all()
    )
    if not tables:
        return []

    table_ids = [t.id for t in tables]
    tokens = (
        db.query(TableQrToken)
        .filter(
            TableQrToken.table_id.in_(table_ids),
            TableQrToken.status == QR_TOKEN_STATUS_ACTIVE,
        )
        .all()
    )
    by_table = {t.table_id: t for t in tokens}
    return [TableRow(table=t, token=by_table.get(t.id)) for t in tables]


def _table_or_404(db: Session, store_id: int, table_id: int) -> Table:
    """
    One table, if it belongs to the CALLER'S branch.

    Another branch's table 404s rather than 403s, matching every other
    cross-store lookup in this codebase: a 403 would confirm the table exists.
    """
    table = (
        db.query(Table)
        .filter(Table.id == table_id, Table.store_id == store_id)
        .first()
    )
    if table is None:
        raise _not_found(messages.TABLE_NOT_FOUND, "table_not_found")
    return table


def _table_number_taken(
    db: Session, store_id: int, number: str, *, exclude_table_id: int | None = None
) -> bool:
    """Same label twice in one branch makes two printed stickers ambiguous."""
    q = db.query(Table.id).filter(
        Table.store_id == store_id,
        func.lower(Table.table_number) == number.strip().lower(),
    )
    if exclude_table_id is not None:
        q = q.filter(Table.id != exclude_table_id)
    return db.query(q.exists()).scalar()


def _clean_table_number(raw: str | None) -> str:
    number = (raw or "").strip()
    if not number:
        raise _invalid(messages.TABLE_NUMBER_REQUIRED, "table_number_required")
    return number


@dataclass(frozen=True)
class IssuedQr:
    """
    A freshly minted token, the table it belongs to, and its raw value.

    ``raw_token`` is the only cleartext copy that will ever exist. It is not
    persisted anywhere — the row keeps a SHA-256 hash — so it lives exactly as
    long as the response that carries it.
    """
    table: Table
    token: TableQrToken
    raw_token: str
    previous_revoked: bool


def customer_qr_url(raw_token: str, base_url: str) -> str:
    """
    The URL a guest scans, built exactly as scripts/manage_qr_tokens.py builds it.

    The raw token goes in the URL *fragment* (``#qr=…``), never a query string. A
    fragment is not transmitted to the server on the initial page request, so this
    long-lived bearer token cannot leak into web-server, proxy, CDN or platform
    access logs the way ``?qr=`` would. apps/customer-web/src/lib/qr-session.ts
    parses this exact shape, captures the token, and scrubs it from the address
    bar.
    """
    return f"{base_url.rstrip('/')}/#qr={raw_token}"


def create_table(
    db: Session,
    *,
    store_id: int,
    table_number: str,
    issue_qr: bool,
    actor_user_id: int,
    ip_address: str | None = None,
) -> tuple[Table, Optional[IssuedQr]]:
    """
    Add a table to the CALLER'S branch, optionally with its first QR sticker.

    ``tables.qr_code`` is a legacy UNIQUE column that predates the token model and
    is no longer a credential: resolution goes through ``table_qr_tokens`` and
    their SHA-256 hashes, and nothing reads this column to authorize anything. It
    is filled with a non-secret, globally unique value so the constraint is
    satisfied without a rename or a re-created label ever colliding with a value
    some earlier table left behind.

    The QR link is returned exactly ONCE, here. It is stored as a hash and cannot
    be read again; recovering it later is not a missing endpoint, it is
    cryptographically impossible by design.
    """
    number = _clean_table_number(table_number)
    if _table_number_taken(db, store_id, number):
        raise _conflict(messages.TABLE_NUMBER_TAKEN, "table_number_taken")

    table = Table(
        store_id=store_id,
        table_number=number,
        # Non-secret, unique, and never used for resolution — see the docstring.
        qr_code=f"store-{store_id}-{uuid.uuid4().hex}",
    )
    db.add(table)
    db.flush()

    audit(
        db,
        entity_type="table",
        entity_id=table.id,
        action="created",
        actor_type="STAFF",
        actor_id=str(actor_user_id),
        payload_after={"store_id": store_id, "table_number": number},
        ip_address=ip_address,
    )

    issued: Optional[IssuedQr] = None
    if issue_qr:
        record, raw = qr_token_service.issue_token(
            db, table.id, created_reason="owner_setup_create_table", commit=False
        )
        issued = IssuedQr(
            table=table, token=record, raw_token=raw, previous_revoked=False
        )

    db.commit()
    db.refresh(table)
    return table, issued


def rename_table(
    db: Session,
    *,
    store_id: int,
    table_id: int,
    table_number: str,
    actor_user_id: int,
    ip_address: str | None = None,
) -> Table:
    """
    Change a table's label.

    Renaming does NOT touch the QR token: the sticker on that table keeps working,
    which is the behaviour a manager correcting a typo needs. Invalidating a live
    sticker is rotation, and rotation is its own explicit, warned action.
    """
    table = _table_or_404(db, store_id, table_id)
    number = _clean_table_number(table_number)
    if _table_number_taken(db, store_id, number, exclude_table_id=table_id):
        raise _conflict(messages.TABLE_NUMBER_TAKEN, "table_number_taken")

    before = table.table_number
    table.table_number = number
    audit(
        db,
        entity_type="table",
        entity_id=table.id,
        action="renamed",
        actor_type="STAFF",
        actor_id=str(actor_user_id),
        payload_before={"table_number": before},
        payload_after={"store_id": store_id, "table_number": number},
        ip_address=ip_address,
    )
    db.commit()
    db.refresh(table)
    return table


def issue_table_qr(
    db: Session,
    *,
    store_id: int,
    table_id: int,
    actor_user_id: int,
    ip_address: str | None = None,
) -> IssuedQr:
    """
    Mint the FIRST QR sticker for a table that has none.

    Refuses (409) when the table already has a live token. That is the
    one-active-token invariant talking — one physical table, one trusted sticker —
    and the manager is pointed at rotation, which replaces a sticker deliberately
    and says that the old one stops working.
    """
    table = _table_or_404(db, store_id, table_id)
    try:
        record, raw = qr_token_service.issue_token(
            db, table_id, created_reason="owner_setup_issue", commit=False
        )
    except qr_token_service.ActiveTokenExists:
        db.rollback()
        raise _conflict(messages.QR_TOKEN_ALREADY_ACTIVE, "qr_token_already_active")

    audit(
        db,
        entity_type="table_qr_token",
        entity_id=record.id,
        action="issued",
        actor_type="STAFF",
        actor_id=str(actor_user_id),
        # The prefix only. A raw token is never logged, never audited, and never
        # recoverable from any record this system keeps.
        payload_after={
            "store_id": store_id,
            "table_id": table_id,
            "token_prefix": record.token_prefix,
        },
        ip_address=ip_address,
    )
    db.commit()
    db.refresh(record)
    db.refresh(table)
    return IssuedQr(
        table=table, token=record, raw_token=raw, previous_revoked=False
    )


def rotate_table_qr(
    db: Session,
    *,
    store_id: int,
    table_id: int,
    actor_user_id: int,
    ip_address: str | None = None,
) -> IssuedQr:
    """
    Replace a table's QR sticker. **The old sticker stops working immediately.**

    This is the destructive one on this screen, and the model already supports it
    safely: ``qr_token_service.rotate_token`` locks the table, revokes the current
    ACTIVE token, inserts the replacement, and links the lineage, in one
    transaction guarded by a partial unique index. Nothing is deleted, so the
    history of which sticker was live when survives.

    What it costs in the shop: every guest holding a phone pointed at the old
    sticker gets "geçersiz veya süresi dolmuş" until somebody physically replaces
    the printed code. That is the correct behaviour for a leaked or photographed
    sticker, and the wrong thing to do by accident — which is why the owner-web
    control asks for confirmation and this docstring says it out loud.
    """
    table = _table_or_404(db, store_id, table_id)
    had_active = _active_token(db, table_id) is not None

    record, raw = qr_token_service.rotate_token(
        db, table_id, created_reason="owner_setup_rotate", commit=False
    )
    audit(
        db,
        entity_type="table_qr_token",
        entity_id=record.id,
        action="rotated",
        actor_type="STAFF",
        actor_id=str(actor_user_id),
        payload_after={
            "store_id": store_id,
            "table_id": table_id,
            "token_prefix": record.token_prefix,
            "previous_token_revoked": had_active,
        },
        ip_address=ip_address,
    )
    db.commit()
    db.refresh(record)
    db.refresh(table)
    return IssuedQr(
        table=table, token=record, raw_token=raw, previous_revoked=had_active
    )


# ── Setup readiness ──────────────────────────────────────────────────────────

@dataclass(frozen=True)
class SetupStatus:
    store_id: int
    store_name: Optional[str]
    catalog_active_products: int
    tables_total: int
    tables_with_active_qr: int
    published_products: int
    available_products: int
    menu_products: int
    ready_for_customer_orders: bool
    checks: list[dict]


def setup_status(db: Session, store_id: int) -> SetupStatus:
    """
    Can a guest walk in, scan, and order — and if not, which step is missing?

    This exists because of what migration ``a9e4c7b25d13`` deliberately does: a
    branch that has published nothing serves an EMPTY customer menu. That is the
    right failure mode, and on a phone it is indistinguishable from a broken
    system. An owner needs to be told "you have not published anything yet", not
    left to conclude the software is down.

    Four questions, in the order a shop actually opens:

      1. Is there a table at all?
      2. Can that table be SCANNED (does it have a live QR sticker)?
      3. Has anything been published to this branch's menu?
      4. Is anything on that menu actually orderable right now?

    (3) and (4) are separate on purpose. A branch that published five products and
    then switched all five off for the day passes (3) and fails (4), and the two
    have completely different fixes.

    Every count is scoped to this branch except ``catalog_active_products``, which
    is the chain catalog and is here to answer the specific confusion this screen
    exists to resolve: "there are fourteen products in the system, why is my menu
    empty?" It is a count, never a list of another branch's menu.
    """
    store = db.get(Store, store_id)

    catalog_active = (
        db.query(func.count(Product.id))
        .filter(Product.is_active == True)  # noqa: E712
        .scalar()
    ) or 0

    tables_total = (
        db.query(func.count(Table.id)).filter(Table.store_id == store_id).scalar()
    ) or 0

    tables_with_qr = (
        db.query(func.count(func.distinct(TableQrToken.table_id)))
        .join(Table, Table.id == TableQrToken.table_id)
        .filter(
            Table.store_id == store_id,
            TableQrToken.status == QR_TOKEN_STATUS_ACTIVE,
        )
        .scalar()
    ) or 0

    published = (
        db.query(func.count(StoreProduct.id))
        .filter(StoreProduct.store_id == store_id)
        .scalar()
    ) or 0

    available = (
        db.query(func.count(StoreProduct.id))
        .filter(
            StoreProduct.store_id == store_id,
            StoreProduct.is_available == True,  # noqa: E712
        )
        .scalar()
    ) or 0

    # The guest's-eye count: the same join the customer menu is built from.
    menu_products = (
        db.query(func.count(StoreProduct.id))
        .join(Product, Product.id == StoreProduct.product_id)
        .filter(
            StoreProduct.store_id == store_id,
            StoreProduct.is_available == True,  # noqa: E712
            Product.is_active == True,  # noqa: E712
        )
        .scalar()
    ) or 0

    checks = [
        {
            "key": "has_table",
            "done": tables_total > 0,
            "count": tables_total,
            "label": "Şubede en az bir masa var",
            "detail": (
                "Masa ekleyin. Misafirler siparişi masadaki QR kodu okutarak veriyor."
                if tables_total == 0
                else f"{tables_total} masa tanımlı."
            ),
        },
        {
            "key": "has_table_qr",
            "done": tables_with_qr > 0 and tables_with_qr == tables_total,
            "count": tables_with_qr,
            "label": "Masaların QR kodu hazır",
            "detail": (
                "Henüz QR kodu olan masa yok. Masa için QR kodu oluşturun."
                if tables_with_qr == 0
                else (
                    f"{tables_with_qr} masanın geçerli QR kodu var."
                    if tables_with_qr == tables_total
                    else f"{tables_with_qr}/{tables_total} masanın geçerli QR kodu var; "
                         "kalan masalar için QR kodu oluşturun."
                )
            ),
        },
        {
            "key": "has_published_product",
            "done": published > 0,
            "count": published,
            "label": "Şube menüsünde yayında ürün var",
            "detail": (
                "Menüde hiç ürün yok. Ürün kataloğundan şube menünüze ürün ekleyin; "
                "katalogda ürün olması tek başına yeterli değildir."
                if published == 0
                else f"{published} ürün şube menünüzde yayında."
            ),
        },
        {
            "key": "menu_ready",
            "done": menu_products > 0,
            "count": menu_products,
            "label": "Misafirler menüden sipariş verebiliyor",
            "detail": (
                "Yayındaki ürünlerin tümü ya günlük olarak kapalı ya da katalogda "
                "pasif. Menü misafire boş görünüyor."
                if menu_products == 0 and published > 0
                else (
                    "Menü boş görünüyor. Önce şube menünüze ürün ekleyin."
                    if menu_products == 0
                    else f"{menu_products} ürün misafire görünüyor."
                )
            ),
        },
    ]

    # Every check, not just the menu one: a menu nobody can scan sells nothing,
    # and a scannable table with an empty menu sells nothing either.
    ready = all(c["done"] for c in checks)

    return SetupStatus(
        store_id=store_id,
        store_name=store.name if store else None,
        catalog_active_products=int(catalog_active),
        tables_total=int(tables_total),
        tables_with_active_qr=int(tables_with_qr),
        published_products=int(published),
        available_products=int(available),
        menu_products=int(menu_products),
        ready_for_customer_orders=ready,
        checks=checks,
    )
