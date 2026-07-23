"""
Store setup & menu provisioning schemas.

No request model here has a ``store_id`` field, and every one of them forbids
unknown keys outright (``extra="forbid"``). The branch is derived from the
authenticated session (see routers/owner_setup.py), so there is nothing for a
client to set — and a body that smuggles in ``"store_id": 2`` is REJECTED with a
422 rather than silently ignored. Ignoring it would leave a client believing it
had published a product onto another branch's menu, and cheerfully told so.

Responses DO carry ``store_id``, so a screen can always see which branch a menu
decision belongs to.

Prices are Decimal end-to-end (never float) and serialize as JSON strings, the
same contract the customer menu already uses.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


# ── Setup readiness ──────────────────────────────────────────────────────────

class SetupCheck(BaseModel):
    """
    One line of the readiness checklist.

    ``key`` is the stable English contract a client branches on; ``label`` and
    ``detail`` are the Turkish sentences a manager reads. Both travel together so
    no screen has to invent a translation, and so a client that somehow receives
    an unrecognised key still has something safe to display.

    ``count`` is the number BEHIND the boolean — "0 masa" and "3 masa" are very
    different answers to "is this shop set up?", and a bare ``done: false`` hides
    which one it is.
    """
    key: str
    done: bool
    count: int
    label: str
    detail: str


class SetupStatusResponse(BaseModel):
    """
    Why the customer menu looks the way it does, in one read.

    Since migration ``a9e4c7b25d13`` a branch that has published nothing serves an
    EMPTY customer menu (docs/CUSTOMER_MENU_SCOPING.md). That is the intended
    failure mode, but on its own it is indistinguishable from a broken system. This
    response is the answer to "why is my menu empty?" — it names the missing step
    rather than leaving the owner to guess.
    """
    store_id: int
    store_name: Optional[str] = None

    # Chain-wide catalog size. Context for "I have products but no menu".
    catalog_active_products: int

    # This branch only.
    tables_total: int
    tables_with_active_qr: int
    published_products: int
    available_products: int
    # Published AND available AND the product is still active chain-wide — i.e.
    # exactly the predicate menu_service.list_menu_products uses. This is the
    # number a guest would actually see.
    menu_products: int

    ready_for_customer_orders: bool
    checks: list[SetupCheck]


# ── Menu products ────────────────────────────────────────────────────────────

class MenuProductItem(BaseModel):
    """
    One catalog product, plus what THIS branch has decided about it.

    The catalog is chain-wide by design, so the product fields are the chain's.
    Everything below ``published`` is this branch's own decision and nobody
    else's: the response never carries another branch's publication state, its
    availability, or its menu order.

    ``on_customer_menu`` is computed server-side with the same three-way predicate
    the customer menu uses (published ∧ available ∧ active). A client must not
    re-derive it: the day a fourth condition is added, a client-side AND would
    quietly disagree with what the guest can actually see.
    """
    product_id: int
    name: Optional[str] = None
    category: Optional[str] = None
    base_price: Optional[Decimal] = None
    is_active: bool

    published: bool
    # Null when not published — there is no availability decision to report.
    is_available: Optional[bool] = None
    sort_order: Optional[int] = None
    published_at: Optional[datetime] = None

    on_customer_menu: bool

    model_config = ConfigDict(from_attributes=True)


class MenuProductListResponse(BaseModel):
    total: int
    store_id: int
    published_total: int
    on_menu_total: int
    items: list[MenuProductItem]


class ProductCreateRequest(BaseModel):
    """
    Add a product to the chain catalog.

    ``publish_to_current_store`` defaults to **False**. Creating a product is a
    catalog act; putting it in front of guests is a separate decision, and one
    that must be taken for one named branch at a time. A create that silently
    published everywhere is how a half-priced trial item ends up on every menu in
    the chain — and it is the shape that put eight ``TestWaffle`` rows one render
    away from a customer's phone before migration ``a9e4c7b25d13``.

    The flag exists at all because the honest first-run flow is "add my menu",
    not "add a product, then go and publish it": the owner-web form asks the
    question explicitly with a checkbox, so the publication is still a decision
    the manager took, not one the server took for them.
    """
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    category: Optional[str] = Field(default=None, max_length=100)
    # Bounds, not validation theatre: the service re-checks and answers with a
    # Turkish sentence. `gt=0` here only stops an obviously empty form early.
    base_price: Decimal = Field(gt=0)
    is_active: bool = True

    publish_to_current_store: bool = False


class ProductUpdateRequest(BaseModel):
    """
    Edit the safe fields of a catalog product.

    Every field is optional and an omitted field is LEFT ALONE — this is a genuine
    patch, not a full replacement, because a manager renaming an item has not
    thereby made a decision about its price.

    Note what is not here: no publication state, no availability, no menu order.
    Those are per-branch decisions and live on their own endpoints; putting them
    in the same body would let one request rewrite the chain's catalog and one
    branch's menu together, and the two need different confirmations on screen.
    """
    model_config = ConfigDict(extra="forbid")

    name: Optional[str] = Field(default=None, min_length=1, max_length=200)
    category: Optional[str] = Field(default=None, max_length=100)
    base_price: Optional[Decimal] = Field(default=None, gt=0)
    # Retires the item CHAIN-WIDE. It disappears from every branch's customer menu
    # and becomes unorderable everywhere, even where an offering row survives.
    is_active: Optional[bool] = None


class AvailabilityUpdateRequest(BaseModel):
    """
    Switch a published item off for the day, or back on.

    Distinct from unpublishing: the publication decision survives, so "we sold out
    of pistachio waffles" does not quietly become "we stopped selling pistachio
    waffles" and lose the branch's menu order along with it.
    """
    model_config = ConfigDict(extra="forbid")

    is_available: bool


class SortOrderUpdateRequest(BaseModel):
    """Menu position within this branch. Ties are broken by name server-side, so
    two items sharing a number still render in a stable order."""
    model_config = ConfigDict(extra="forbid")

    sort_order: int = Field(ge=0)


class MenuPublicationReceipt(BaseModel):
    """
    The state of one product on one branch's menu after a publication decision.

    ``changed`` is False when the request asked for the state the row was already
    in — publishing twice, or withdrawing something that was never published. That
    is a SUCCESS, not an error: these endpoints are naturally idempotent, and a
    retried click must not become a second failure on screen.
    """
    store_id: int
    product_id: int
    name: Optional[str] = None
    is_active: bool
    published: bool
    is_available: Optional[bool] = None
    sort_order: Optional[int] = None
    on_customer_menu: bool
    changed: bool

    model_config = ConfigDict(from_attributes=True)


# ── Tables & QR ──────────────────────────────────────────────────────────────

class TableItem(BaseModel):
    """
    One table in the caller's branch, with the state of its QR sticker.

    There is no ``qr_url`` field, and that absence is a security property rather
    than an omission. Raw QR tokens are stored only as a SHA-256 hash and are
    unrecoverable by design (services/qr_token_service.py), so a link can be shown
    exactly once — at issue or rotation. ``token_prefix`` is the non-secret
    fragment that lets a manager match a record to the sticker physically on the
    table; it is not a token and cannot be scanned.
    """
    table_id: int
    store_id: int
    table_number: Optional[str] = None
    display_name: str

    has_active_qr: bool
    token_prefix: Optional[str] = None
    qr_created_at: Optional[datetime] = None
    qr_last_used_at: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)


class TableListResponse(BaseModel):
    total: int
    store_id: int
    with_active_qr: int
    items: list[TableItem]


class TableCreateRequest(BaseModel):
    """
    Add a table to the caller's own branch.

    ``issue_qr`` defaults to True because a table with no sticker cannot be
    scanned and is therefore not yet a table anybody can order from. The resulting
    link is returned ONCE on the response and never again.
    """
    model_config = ConfigDict(extra="forbid")

    table_number: str = Field(min_length=1, max_length=50)
    issue_qr: bool = True


class TableUpdateRequest(BaseModel):
    """
    Rename a table.

    Only the number/label can be edited. There is deliberately no ``is_active``
    field: ``tables`` has no such column, and inventing one in this branch would
    be a schema decision smuggled in under a rename. Closing a table safely is
    named as remaining work in docs/STORE_SETUP_AND_MENU_PROVISIONING.md.
    """
    model_config = ConfigDict(extra="forbid")

    table_number: str = Field(min_length=1, max_length=50)


class TableQrReceipt(BaseModel):
    """
    A freshly minted QR link — the ONE and ONLY time it can be read.

    ``qr_url`` is the customer app URL with the raw token in the URL *fragment*
    (``#qr=…``), matching what scripts/manage_qr_tokens.py prints and what
    apps/customer-web/src/lib/qr-session.ts parses. A fragment is not sent to the
    server on the initial page request, so this long-lived bearer token cannot
    leak into web-server, proxy, CDN or platform access logs the way a ``?qr=``
    query parameter would.

    ``previous_token_revoked`` is True after a rotation: the sticker currently
    stuck to that table stopped working the moment this response was produced,
    and the screen has to say so before anybody walks away from the printer.
    """
    table_id: int
    store_id: int
    table_number: Optional[str] = None
    display_name: str
    token_id: int
    token_prefix: str
    qr_url: str
    previous_token_revoked: bool
    # The one-time-only warning, pre-written in Turkish so no client invents a
    # softer version of it.
    notice: str


class TableCreateResponse(BaseModel):
    """The new table, plus its first QR link if one was issued (once only)."""
    table: TableItem
    qr: Optional[TableQrReceipt] = None
