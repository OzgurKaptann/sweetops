"""
Store setup & menu provisioning API — how a real shop is opened without a developer.

This is the authenticated answer to RUNTIME_PRODUCT_GAP_REVIEW **F-13**: until now
a store, a table, a QR sticker or a menu item could only be created by editing
Python or running a script on the database host. It is also the missing half of
`docs/CUSTOMER_MENU_SCOPING.md`: migration ``a9e4c7b25d13`` made the customer menu
fail closed, correctly, and left no supported way to open it.

Store scope
-----------
The branch is ALWAYS ``staff.store_id``, taken from the authenticated session. It
is never read from the body, the query string or a header, and the request models
in ``app/schemas/store_setup.py`` set ``extra="forbid"`` so a smuggled
``store_id`` does not merely get ignored — it 422s. Publishing a product onto
another branch's menu is therefore not a permission check that could be got wrong;
it is a request that cannot be expressed. A member of staff with no branch is
refused outright: there is no chain-wide menu to provision.

Authorization
-------------
Reads need ``setup:read``; every mutation needs ``setup:manage``, plus a trusted
Origin and a valid CSRF token (both enforced by ``require_permission`` on
state-changing methods). Only OWNER and MANAGER hold either — a cook cannot
rewrite the menu and a cashier cannot rotate a QR sticker.

Idempotency-Key
---------------
Deliberately NOT required here, and the reason is not that it was forgotten. The
publication routes are naturally idempotent: publishing twice leaves one row,
withdrawing twice leaves none, and setting availability to the value it already
has changes nothing. They report ``changed: false`` and a 200, so a double-click
is a no-op rather than a second failure on screen. Creation is not idempotent and
is protected instead by a duplicate-name guard (409), because adding an
idempotency ledger for configuration would mean a migration this branch does not
need. See docs/STORE_SETUP_AND_MENU_PROVISIONING.md § "Idempotency, honestly".

Nothing here touches stock, orders, money, shifts or the inventory lifecycle.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy.orm import Session

from app.core import messages
from app.core.config import settings
from app.core.db import get_db
from app.core.deps import require_permission
from app.core.permissions import PERM_SETUP_MANAGE, PERM_SETUP_READ
from app.schemas.store_setup import (
    AvailabilityUpdateRequest,
    MenuProductListResponse,
    MenuPublicationReceipt,
    ProductCreateRequest,
    ProductUpdateRequest,
    SetupStatusResponse,
    SortOrderUpdateRequest,
    TableCreateRequest,
    TableCreateResponse,
    TableListResponse,
    TableQrReceipt,
    TableUpdateRequest,
)
from app.services import store_setup_service as setup
from app.services.auth_service import CurrentStaff
from app.services.qr_token_service import table_display_name

router = APIRouter(prefix="/owner", tags=["Owner Store Setup"])


# ── Shared guards / helpers ──────────────────────────────────────────────────

def _store_id(staff: CurrentStaff) -> int:
    """
    The ONE source of branch scope for every route in this module.

    A menu belongs to a shop. A session with no branch cannot be answered — not
    with an empty list, and certainly not with somebody else's menu — so it is
    refused outright, exactly as routers/inventory.py refuses a storeless stock
    request.
    """
    if staff.store_id is None:
        raise HTTPException(
            status_code=403,
            detail={
                "error": "no_store_assigned",
                "message": messages.SETUP_NO_STORE_ASSIGNED,
            },
        )
    return staff.store_id


def _no_store(response: Response) -> None:
    # Setup state changes the moment somebody publishes something. A cached
    # readiness checklist would tell an owner their menu is still empty after
    # they fixed it.
    response.headers["Cache-Control"] = "no-store"


def _client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


def _menu_item(row: setup.MenuRow, store_id: int) -> dict:
    p, o = row.product, row.offering
    return {
        "product_id": p.id,
        "name": p.name,
        "category": p.category,
        "base_price": p.base_price,
        "is_active": p.is_active,
        "published": o is not None,
        "is_available": o.is_available if o is not None else None,
        "sort_order": o.sort_order if o is not None else None,
        "published_at": o.created_at if o is not None else None,
        # Computed once, server-side, with the customer menu's own predicate.
        "on_customer_menu": row.visible,
    }


def _receipt(result: setup.PublicationResult, store_id: int) -> dict:
    row = result.row
    p, o = row.product, row.offering
    return {
        "store_id": store_id,
        "product_id": p.id,
        "name": p.name,
        "is_active": p.is_active,
        "published": o is not None,
        "is_available": o.is_available if o is not None else None,
        "sort_order": o.sort_order if o is not None else None,
        "on_customer_menu": row.visible,
        "changed": result.changed,
    }


def _table_item(row: setup.TableRow) -> dict:
    t, tok = row.table, row.token
    return {
        "table_id": t.id,
        "store_id": t.store_id,
        "table_number": t.table_number,
        "display_name": table_display_name(t),
        "has_active_qr": tok is not None,
        # The non-secret prefix only. There is no field here that could carry a
        # token, and no endpoint that could return one for an existing sticker.
        "token_prefix": tok.token_prefix if tok is not None else None,
        "qr_created_at": tok.created_at if tok is not None else None,
        "qr_last_used_at": tok.last_used_at if tok is not None else None,
    }


def _qr_receipt(table, issued: setup.IssuedQr) -> dict:
    return {
        "table_id": table.id,
        "store_id": table.store_id,
        "table_number": table.table_number,
        "display_name": table_display_name(table),
        "token_id": issued.token.id,
        "token_prefix": issued.token.token_prefix,
        # The ONE time this value exists outside a hash function.
        "qr_url": setup.customer_qr_url(
            issued.raw_token, settings.CUSTOMER_WEB_BASE_URL
        ),
        "previous_token_revoked": issued.previous_revoked,
        "notice": messages.QR_LINK_SHOWN_ONCE,
    }


# ── Setup readiness ──────────────────────────────────────────────────────────

@router.get("/setup/status", response_model=SetupStatusResponse)
def get_setup_status(
    response: Response,
    db: Session = Depends(get_db),
    staff: CurrentStaff = Depends(require_permission(PERM_SETUP_READ)),
):
    """
    Is this branch ready to take a customer order, and if not, which step is missing?

    The screen this feeds exists to answer one question an owner will otherwise
    ask support: **"why is my customer menu empty?"** Since migration
    ``a9e4c7b25d13`` the honest answer is usually "because nothing has been
    published to this branch yet", and a guest's phone cannot say that — it shows
    the same calm empty state whether the shop forgot to publish or the server is
    down. This endpoint names the missing step instead.

    Four booleans, each with the count behind it: a table exists, that table can
    be scanned, something is published here, and something on it is actually
    orderable right now. The last two are separate because a branch that switched
    all five of its items off for the day passes one and fails the other, and the
    fixes are nothing alike.
    """
    _no_store(response)
    return setup.setup_status(db, _store_id(staff))


# ── Menu products ────────────────────────────────────────────────────────────

@router.get("/menu/products", response_model=MenuProductListResponse)
def list_menu_products(
    response: Response,
    db: Session = Depends(get_db),
    staff: CurrentStaff = Depends(require_permission(PERM_SETUP_READ)),
):
    """
    The chain catalog, annotated with what THIS branch has decided about each item.

    Both halves are needed on one screen. "What is on my menu?" cannot be answered
    without the publication rows, and "what could I add?" cannot be answered
    without the catalog — a manager who can only see what they have already
    published cannot publish anything else, which is the position every branch is
    in the moment the fail-closed menu ships.

    What this response cannot reveal: another branch's menu. The publication
    columns are joined for the caller's store id only, so an item Moda sells and
    this branch does not is indistinguishable here from one nobody sells.
    """
    _no_store(response)
    store_id = _store_id(staff)

    rows = setup.list_menu_rows(db, store_id)
    items = [_menu_item(r, store_id) for r in rows]
    return {
        "total": len(items),
        "store_id": store_id,
        "published_total": sum(1 for r in rows if r.published),
        "on_menu_total": sum(1 for r in rows if r.visible),
        "items": items,
    }


@router.post("/menu/products", response_model=MenuPublicationReceipt)
def create_menu_product(
    body: ProductCreateRequest,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    staff: CurrentStaff = Depends(require_permission(PERM_SETUP_MANAGE)),
):
    """
    Add a product to the chain catalog — and, only if asked, to THIS branch's menu.

    ``publish_to_current_store`` defaults to False and can only ever mean the
    caller's own branch: there is no store field in the body and no "publish to
    all" anywhere in this module. A new product is invisible to every guest in
    every other branch until somebody with a session in that branch decides
    otherwise. That is not a courtesy — automatically publishing new catalog rows
    everywhere is precisely the shape that put eight ``TestWaffle`` rows one
    render away from a customer's phone.

    A duplicate name is refused with 409 ``product_name_taken``. That guard is
    also what makes a double-submitted form safe here: the second POST finds the
    first product rather than minting a twin.
    """
    _no_store(response)
    store_id = _store_id(staff)

    row = setup.create_product(
        db,
        # The session's branch. Never the body's — there is no field for one.
        store_id=store_id,
        name=body.name,
        category=body.category,
        base_price=body.base_price,
        is_active=body.is_active,
        publish_to_current_store=body.publish_to_current_store,
        actor_user_id=staff.user_id,
        ip_address=_client_ip(request),
    )
    return _receipt(setup.PublicationResult(row, changed=True), store_id)


@router.patch("/menu/products/{product_id}", response_model=MenuPublicationReceipt)
def update_menu_product(
    product_id: int,
    body: ProductUpdateRequest,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    staff: CurrentStaff = Depends(require_permission(PERM_SETUP_MANAGE)),
):
    """
    Edit a catalog product. Omitted fields are left alone.

    ``is_active: false`` is the dangerous one and it is CHAIN-WIDE: the item
    disappears from every branch's customer menu at once and becomes unorderable
    everywhere, even where a publication row still points at it. It is not a
    delete — the offering rows and every past order line survive, so history still
    says what was sold — but it is not a decision about this branch alone, and the
    owner-web control says so before it is sent.

    The product comes from the PATH. There is no product id in the body and no
    store id anywhere: both would be ways to edit something other than the thing
    on screen.
    """
    _no_store(response)
    store_id = _store_id(staff)

    row = setup.update_product(
        db,
        store_id=store_id,
        product_id=product_id,
        name=body.name,
        category=body.category,
        base_price=body.base_price,
        is_active=body.is_active,
        actor_user_id=staff.user_id,
        ip_address=_client_ip(request),
    )
    return _receipt(setup.PublicationResult(row, changed=True), store_id)


@router.post(
    "/menu/products/{product_id}/publish", response_model=MenuPublicationReceipt
)
def publish_menu_product(
    product_id: int,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    staff: CurrentStaff = Depends(require_permission(PERM_SETUP_MANAGE)),
):
    """
    Put a catalog product on THIS branch's customer menu.

    One row in ``store_products`` is the whole decision, and it is what the
    customer menu is built from. Idempotent — publishing something already
    published returns ``changed: false`` and a 200, and does NOT reset an item
    that is merely switched off for the day.
    """
    _no_store(response)
    store_id = _store_id(staff)

    result = setup.publish_product(
        db,
        store_id=store_id,
        product_id=product_id,
        actor_user_id=staff.user_id,
        ip_address=_client_ip(request),
    )
    return _receipt(result, store_id)


@router.post(
    "/menu/products/{product_id}/unpublish", response_model=MenuPublicationReceipt
)
def unpublish_menu_product(
    product_id: int,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    staff: CurrentStaff = Depends(require_permission(PERM_SETUP_MANAGE)),
):
    """
    Take a product off THIS branch's customer menu.

    The publication row is deleted, so the menu join has nothing left to join to:
    the item disappears from this branch's menu immediately and ``order_service``
    refuses it at submit time even for a guest whose phone still shows the old
    list. No other branch is affected, the product keeps existing, and every order
    ever placed for it is untouched.

    Use availability instead when the item is merely sold out today — that keeps
    the publication decision and this branch's menu order.
    """
    _no_store(response)
    store_id = _store_id(staff)

    result = setup.unpublish_product(
        db,
        store_id=store_id,
        product_id=product_id,
        actor_user_id=staff.user_id,
        ip_address=_client_ip(request),
    )
    return _receipt(result, store_id)


@router.patch(
    "/menu/products/{product_id}/availability", response_model=MenuPublicationReceipt
)
def update_menu_product_availability(
    product_id: int,
    body: AvailabilityUpdateRequest,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    staff: CurrentStaff = Depends(require_permission(PERM_SETUP_MANAGE)),
):
    """
    Switch a published item off for the day, or back on. This branch only.

    Distinct from unpublishing: "bugün kalmadı" keeps the publication decision and
    the branch's menu order, so tomorrow morning is one toggle rather than a
    re-publish that lands the item at the bottom of the board.

    A product this branch has not published is refused (409 ``not_published``)
    rather than quietly published as unavailable.
    """
    _no_store(response)
    store_id = _store_id(staff)

    result = setup.set_availability(
        db,
        store_id=store_id,
        product_id=product_id,
        is_available=body.is_available,
        actor_user_id=staff.user_id,
        ip_address=_client_ip(request),
    )
    return _receipt(result, store_id)


@router.patch(
    "/menu/products/{product_id}/sort-order", response_model=MenuPublicationReceipt
)
def update_menu_product_sort_order(
    product_id: int,
    body: SortOrderUpdateRequest,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    staff: CurrentStaff = Depends(require_permission(PERM_SETUP_MANAGE)),
):
    """
    Move an item within THIS branch's menu order.

    Only this branch's board changes; the same product may sit first here and last
    somewhere else. Ties are broken by name then id on the customer side, so two
    items sharing a number still render deterministically and a manager never has
    to renumber the whole menu to move one row.
    """
    _no_store(response)
    store_id = _store_id(staff)

    result = setup.set_sort_order(
        db,
        store_id=store_id,
        product_id=product_id,
        sort_order=body.sort_order,
        actor_user_id=staff.user_id,
        ip_address=_client_ip(request),
    )
    return _receipt(result, store_id)


# ── Tables & QR ──────────────────────────────────────────────────────────────
#
# There is deliberately no ``GET /owner/tables/{id}/qr-link``.
#
# A raw QR token is stored only as a SHA-256 hash and is returned exactly once, at
# the moment it is minted (services/qr_token_service.py). Recovering the link for
# an existing sticker is therefore not a missing endpoint — it is cryptographically
# impossible, and that is the property that makes a database leak useless. The list
# below carries the non-secret ``token_prefix`` so a manager can match a record to
# the sticker physically on the table; getting a *scannable* link again means
# rotating, which invalidates the old sticker and says so.


@router.get("/tables", response_model=TableListResponse)
def list_tables(
    response: Response,
    db: Session = Depends(get_db),
    staff: CurrentStaff = Depends(require_permission(PERM_SETUP_READ)),
):
    """
    The caller's own branch's tables and the state of each one's QR sticker.

    No ``qr_url`` field exists on this response — see the note above. What is here
    is what a manager can act on: whether the table can be scanned at all, the
    non-secret prefix that identifies the printed sticker, when it was created,
    and when a guest last used it (a table nobody has scanned in weeks is usually
    a sticker that fell off).
    """
    _no_store(response)
    store_id = _store_id(staff)

    rows = setup.list_tables(db, store_id)
    items = [_table_item(r) for r in rows]
    return {
        "total": len(items),
        "store_id": store_id,
        "with_active_qr": sum(1 for r in rows if r.token is not None),
        "items": items,
    }


@router.post("/tables", response_model=TableCreateResponse)
def create_table(
    body: TableCreateRequest,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    staff: CurrentStaff = Depends(require_permission(PERM_SETUP_MANAGE)),
):
    """
    Add a table to the caller's own branch, with its first QR sticker.

    The branch is the session's. There is no store field in the body, so adding a
    table to another shop is a request that cannot be expressed.

    ``issue_qr`` defaults to true because a table with no sticker cannot be
    scanned and is therefore not yet a table anybody can order from. The link
    comes back on this response and **never again** — it exists in cleartext for
    exactly the duration of this HTTP response.
    """
    _no_store(response)
    store_id = _store_id(staff)

    table, issued = setup.create_table(
        db,
        store_id=store_id,
        table_number=body.table_number,
        issue_qr=body.issue_qr,
        actor_user_id=staff.user_id,
        ip_address=_client_ip(request),
    )
    row = setup.TableRow(table=table, token=issued.token if issued else None)
    return {
        "table": _table_item(row),
        "qr": _qr_receipt(table, issued) if issued else None,
    }


@router.patch("/tables/{table_id}", response_model=TableListResponse)
def update_table(
    table_id: int,
    body: TableUpdateRequest,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    staff: CurrentStaff = Depends(require_permission(PERM_SETUP_MANAGE)),
):
    """
    Rename a table in the caller's own branch.

    Renaming does NOT touch the QR sticker: the code on that table keeps working,
    which is what a manager fixing a typo needs. Invalidating a live sticker is
    rotation, and rotation is a separate, warned action.

    Another branch's table 404s rather than 403s — a 403 would confirm it exists.
    The whole table list comes back so the screen re-renders from server state
    rather than from what it hoped the change did.
    """
    _no_store(response)
    store_id = _store_id(staff)

    setup.rename_table(
        db,
        store_id=store_id,
        table_id=table_id,
        table_number=body.table_number,
        actor_user_id=staff.user_id,
        ip_address=_client_ip(request),
    )
    rows = setup.list_tables(db, store_id)
    return {
        "total": len(rows),
        "store_id": store_id,
        "with_active_qr": sum(1 for r in rows if r.token is not None),
        "items": [_table_item(r) for r in rows],
    }


@router.post("/tables/{table_id}/qr-token", response_model=TableQrReceipt)
def issue_table_qr(
    table_id: int,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    staff: CurrentStaff = Depends(require_permission(PERM_SETUP_MANAGE)),
):
    """
    Mint the FIRST QR sticker for a table that has none. The link is shown once.

    A table that already has a live sticker is refused (409
    ``qr_token_already_active``) — one physical table, one trusted code — and the
    manager is pointed at rotation, which replaces a sticker on purpose.
    """
    _no_store(response)
    store_id = _store_id(staff)

    issued = setup.issue_table_qr(
        db,
        store_id=store_id,
        table_id=table_id,
        actor_user_id=staff.user_id,
        ip_address=_client_ip(request),
    )
    return _qr_receipt(issued.table, issued)


@router.post("/tables/{table_id}/rotate-qr", response_model=TableQrReceipt)
def rotate_table_qr(
    table_id: int,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    staff: CurrentStaff = Depends(require_permission(PERM_SETUP_MANAGE)),
):
    """
    Replace a table's QR sticker. **The printed code on that table stops working.**

    Supported safely by the existing model, which is why it is here at all: the
    service locks the table, revokes the current ACTIVE token, inserts the
    replacement and links the lineage in one transaction, with a partial unique
    index as the backstop. Nothing is deleted, so the record of which sticker was
    live when survives.

    What it costs in the shop, stated plainly because the owner-web control has to
    repeat it: every guest whose phone is pointed at the old sticker gets
    "geçersiz veya süresi dolmuş" until the printed code is physically replaced.
    That is exactly right for a photographed or leaked sticker, and exactly wrong
    by accident.

    The new link is on this response and nowhere else, ever.
    """
    _no_store(response)
    store_id = _store_id(staff)

    issued = setup.rotate_table_qr(
        db,
        store_id=store_id,
        table_id=table_id,
        actor_user_id=staff.user_id,
        ip_address=_client_ip(request),
    )
    return _qr_receipt(issued.table, issued)
