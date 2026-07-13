"""
Inventory API — protected, store-scoped stock reads and manual stock mutations.

Every route requires an authenticated staff session. Reads need
``inventory:read``; every mutation needs ``inventory:adjust`` plus a trusted
Origin, a valid CSRF token (both enforced by ``require_permission`` on
state-changing methods) and an ``Idempotency-Key`` header.

Store scope
-----------
The store is ALWAYS ``staff.store_id``, taken from the authenticated session.
It is never read from the request body, the query string, or a header — so a
Store A manager cannot list, and cannot write off, Store B's stock by naming
Store B in a payload. There is deliberately no "all stores" view and no
store_id parameter to tamper with: the absence of the parameter is the security
property.

Because stock is genuinely store-scoped now, these routes no longer fail closed
when a second branch opens. A member of staff with no store assignment is
refused, however — there is no meaningful chain-wide inventory to show them.

Nothing here is public. Customers never see inventory internals: the customer
menu only exposes a coarse in_stock/low_stock/out_of_stock status for their own
branch, and order rejection returns a Turkish message with no stock figures.
See docs/STORE_SCOPED_INVENTORY.md.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from sqlalchemy.orm import Session

from app.core import messages
from app.core.db import get_db
from app.core.deps import require_permission
from app.core.permissions import PERM_INVENTORY_ADJUST, PERM_INVENTORY_READ
from app.models.ingredient import Ingredient
from app.models.ingredient_stock import (
    MOVEMENT_TYPES,
    IngredientStock,
    IngredientStockMovement,
)
from app.models.inventory_transfer import InventoryTransfer
from app.models.store import Store
from app.schemas.inventory import (
    ManualAdjustmentRequest,
    MovementListResponse,
    MovementReceipt,
    PurchaseReceiptRequest,
    StockListResponse,
    TransferDestinationListResponse,
    TransferItem,
    TransferListResponse,
    TransferReceipt,
    TransferRequest,
    WasteRequest,
)
from app.services import inventory_service
from app.services.auth_service import CurrentStaff

router = APIRouter(prefix="/inventory", tags=["Inventory"])


def _store_id(staff: CurrentStaff) -> int:
    """
    The ONE source of store scope for every route in this module.

    Inventory is physical, and physical stock sits in a named branch. A session
    with no store cannot be answered — not with an empty list, and certainly not
    with somebody else's stock — so it is refused outright.
    """
    if staff.store_id is None:
        raise HTTPException(
            status_code=403,
            detail={
                "error": "no_store_assigned",
                "message": messages.INVENTORY_NO_STORE_ASSIGNED,
            },
        )
    return staff.store_id


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


def _client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


def _idem_key(request: Request) -> str | None:
    return request.headers.get("Idempotency-Key")


def _receipt(
    db: Session, movement: IngredientStockMovement, *, replay: bool
) -> MovementReceipt:
    stock = (
        db.query(IngredientStock)
        .filter(
            IngredientStock.store_id == movement.store_id,
            IngredientStock.ingredient_id == movement.ingredient_id,
        )
        .first()
    )
    return MovementReceipt(
        movement_id=movement.id,
        store_id=movement.store_id,
        ingredient_id=movement.ingredient_id,
        movement_type=movement.movement_type,
        quantity=movement.quantity,
        quantity_delta_on_hand=movement.quantity_delta_on_hand,
        unit=movement.unit,
        reason=movement.reason,
        on_hand_quantity=stock.on_hand_quantity if stock else 0,
        reserved_quantity=stock.reserved_quantity if stock else 0,
        available_quantity=stock.available_quantity if stock else 0,
        created_at=movement.created_at,
        idempotent_replay=replay,
    )


# ── Reads ────────────────────────────────────────────────────────────────────

@router.get("/stock", response_model=StockListResponse)
def list_stock(
    response: Response,
    db: Session = Depends(get_db),
    staff: CurrentStaff = Depends(require_permission(PERM_INVENTORY_READ)),
):
    """
    On-hand, reserved and available quantities for every active ingredient IN
    THE CALLER'S STORE.

    An ingredient this branch does not stock simply does not appear — the join
    is on (store_id, ingredient_id), so there is no row and no fallback to any
    other branch's figures.
    """
    _no_store(response)
    store_id = _store_id(staff)

    rows = (
        db.query(Ingredient, IngredientStock)
        .join(
            IngredientStock,
            (IngredientStock.ingredient_id == Ingredient.id)
            & (IngredientStock.store_id == store_id),
        )
        .filter(Ingredient.is_active == True)  # noqa: E712
        .order_by(Ingredient.name)
        .all()
    )
    items = [
        {
            "ingredient_id": ing.id,
            "ingredient_name": ing.name,
            "category": ing.category,
            "unit": stock.unit,
            "on_hand_quantity": stock.on_hand_quantity,
            "reserved_quantity": stock.reserved_quantity,
            "available_quantity": stock.available_quantity,
            "reorder_level": stock.reorder_level,
        }
        for ing, stock in rows
    ]
    return {"total": len(items), "items": items}


@router.get("/movements", response_model=MovementListResponse)
def list_movements(
    response: Response,
    ingredient_id: int | None = None,
    movement_type: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    db: Session = Depends(get_db),
    staff: CurrentStaff = Depends(require_permission(PERM_INVENTORY_READ)),
):
    """The append-only movement ledger for the caller's store, newest first."""
    _no_store(response)
    store_id = _store_id(staff)

    q = db.query(IngredientStockMovement, Ingredient).join(
        Ingredient, Ingredient.id == IngredientStockMovement.ingredient_id
    ).filter(IngredientStockMovement.store_id == store_id)
    if ingredient_id is not None:
        q = q.filter(IngredientStockMovement.ingredient_id == ingredient_id)
    if movement_type is not None:
        # Constrain to the known domain so an arbitrary string cannot be probed.
        if movement_type not in MOVEMENT_TYPES:
            return {"total": 0, "items": []}
        q = q.filter(IngredientStockMovement.movement_type == movement_type)

    rows = q.order_by(IngredientStockMovement.id.desc()).limit(limit).all()
    items = [
        {
            "id": m.id,
            "ingredient_id": m.ingredient_id,
            "ingredient_name": ing.name,
            "movement_type": m.movement_type,
            "quantity": m.quantity,
            "quantity_delta_on_hand": m.quantity_delta_on_hand,
            "quantity_delta_reserved": m.quantity_delta_reserved,
            "unit": m.unit,
            "order_id": m.order_id,
            "reason": m.reason,
            "actor_user_id": m.actor_user_id,
            "created_at": m.created_at,
        }
        for m, ing in rows
    ]
    return {"total": len(items), "items": items}


# ── Mutations (inventory:adjust + Origin + CSRF + Idempotency-Key) ───────────

@router.post("/purchase-receipts", response_model=MovementReceipt)
def create_purchase_receipt(
    body: PurchaseReceiptRequest,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    staff: CurrentStaff = Depends(require_permission(PERM_INVENTORY_ADJUST)),
):
    """
    Record goods received from a supplier — the caller's store's physical stock
    goes up. This is also how a newly opened branch gets its opening stock: no
    store ever inherits another store's inventory.
    """
    _no_store(response)

    result = inventory_service.record_purchase_receipt(
        db,
        store_id=_store_id(staff),
        ingredient_id=body.ingredient_id,
        quantity=body.quantity,
        reason=body.reason,
        actor_user_id=staff.user_id,
        idempotency_key=_idem_key(request),
        ip_address=_client_ip(request),
    )
    return _receipt(db, result.movement, replay=result.replayed)


@router.post("/manual-adjustments", response_model=MovementReceipt)
def create_manual_adjustment(
    body: ManualAdjustmentRequest,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    staff: CurrentStaff = Depends(require_permission(PERM_INVENTORY_ADJUST)),
):
    """Correct the caller's store's on-hand stock to a real physical count
    (signed delta + reason). A count is taken of one branch's shelves."""
    _no_store(response)

    result = inventory_service.record_manual_adjustment(
        db,
        store_id=_store_id(staff),
        ingredient_id=body.ingredient_id,
        delta=body.delta,
        reason=body.reason,
        actor_user_id=staff.user_id,
        idempotency_key=_idem_key(request),
        ip_address=_client_ip(request),
    )
    return _receipt(db, result.movement, replay=result.replayed)


@router.post("/waste", response_model=MovementReceipt)
def create_waste(
    body: WasteRequest,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    staff: CurrentStaff = Depends(require_permission(PERM_INVENTORY_ADJUST)),
):
    """Record stock physically thrown away at the caller's store. Stays visible
    as WASTE, never consumption, and is attributed to the branch that lost it."""
    _no_store(response)

    result = inventory_service.record_waste(
        db,
        store_id=_store_id(staff),
        ingredient_id=body.ingredient_id,
        quantity=body.quantity,
        reason=body.reason,
        actor_user_id=staff.user_id,
        idempotency_key=_idem_key(request),
        ip_address=_client_ip(request),
    )
    return _receipt(db, result.movement, replay=result.replayed)


# ── Store-to-store transfers ─────────────────────────────────────────────────
#
# A transfer is ONE business event with TWO linked ledger movements, never two
# unrelated manual adjustments. See docs/INVENTORY_TRANSFER_WORKFLOW.md.
#
# The SOURCE store is always the session's store. There is no source_store_id
# field to send (TransferRequest forbids unknown fields outright), so shipping
# another branch's stock is not a permission check that could be got wrong — it
# is a request that cannot be expressed.


def _transfer_item(
    transfer: InventoryTransfer, ingredient: Ingredient, viewer_store_id: int
) -> dict:
    """One transfer, labelled from the point of view of the store reading it."""
    return {
        "transfer_id": transfer.id,
        "source_store_id": transfer.source_store_id,
        "destination_store_id": transfer.destination_store_id,
        "ingredient_id": transfer.ingredient_id,
        "ingredient_name": ingredient.name if ingredient else None,
        "quantity": transfer.quantity,
        "unit": transfer.unit,
        "status": transfer.status,
        "reason": transfer.reason,
        "note": transfer.note,
        "initiated_by_user_id": transfer.initiated_by_user_id,
        "direction": (
            "OUTBOUND" if transfer.source_store_id == viewer_store_id else "INBOUND"
        ),
        "created_at": transfer.created_at,
    }


def _transfer_receipt(
    db: Session, result: inventory_service.TransferResult
) -> TransferReceipt:
    t = result.transfer
    source_stock = (
        db.query(IngredientStock)
        .filter(
            IngredientStock.store_id == t.source_store_id,
            IngredientStock.ingredient_id == t.ingredient_id,
        )
        .first()
    )
    ingredient = db.get(Ingredient, t.ingredient_id)
    return TransferReceipt(
        transfer_id=t.id,
        source_store_id=t.source_store_id,
        destination_store_id=t.destination_store_id,
        ingredient_id=t.ingredient_id,
        ingredient_name=ingredient.name if ingredient else None,
        quantity=t.quantity,
        unit=t.unit,
        status=t.status,
        reason=t.reason,
        note=t.note,
        initiated_by_user_id=t.initiated_by_user_id,
        source_movement_id=result.source_movement.id,
        destination_movement_id=result.destination_movement.id,
        source_on_hand_quantity=source_stock.on_hand_quantity if source_stock else 0,
        source_reserved_quantity=source_stock.reserved_quantity if source_stock else 0,
        source_available_quantity=source_stock.available_quantity if source_stock else 0,
        created_at=t.created_at,
        idempotent_replay=result.replayed,
    )


@router.get("/transfer-destinations", response_model=TransferDestinationListResponse)
def list_transfer_destinations(
    response: Response,
    db: Session = Depends(get_db),
    staff: CurrentStaff = Depends(require_permission(PERM_INVENTORY_READ)),
):
    """
    The branches this store may ship to — every store except the caller's own.

    A manager cannot type a destination store id into a form and cannot be
    expected to memorise them, so the transfer form needs SOME way to name the
    other branch. This is the smallest read that answers that question: id and
    name only, no stock, no staff, no takings. The caller's own store is filtered
    out here as a usability courtesy — ``transfer_stock`` still rejects a
    same-store transfer server-side (``same_store_transfer``), and that check,
    not this list, is the actual guarantee.

    Deliberately NOT a general store-management API: read-only, no create/update,
    no other branch's operational data, and still behind ``inventory:read`` and a
    store-assigned session.
    """
    _no_store(response)
    store_id = _store_id(staff)

    rows = (
        db.query(Store)
        .filter(Store.id != store_id)
        .order_by(Store.name)
        .all()
    )
    items = [
        {"store_id": s.id, "name": s.name, "location": s.location} for s in rows
    ]
    return {"total": len(items), "items": items}


@router.post("/transfers", response_model=TransferReceipt)
def create_transfer(
    body: TransferRequest,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    staff: CurrentStaff = Depends(require_permission(PERM_INVENTORY_ADJUST)),
):
    """
    Ship stock from the caller's store to another store, atomically.

    Source on-hand falls and destination on-hand rises in one transaction, as one
    TRANSFER_OUT / TRANSFER_IN pair that shares a transfer id. Reserved stock is
    never transferable: the gate is AVAILABLE (on_hand - reserved), because batter
    already promised to an accepted order is not batter this branch may put on a
    van.

    Requires ``inventory:adjust`` — the same physical-stock authority as waste and
    manual adjustment, and for the same reason: this permanently changes what is
    on a branch's shelves. Plus a trusted Origin, a CSRF token, and an
    ``Idempotency-Key``, so a retried van manifest ships the chocolate once.
    """
    _no_store(response)

    result = inventory_service.transfer_stock(
        db,
        # The session's store. Never the body's.
        source_store_id=_store_id(staff),
        destination_store_id=body.destination_store_id,
        ingredient_id=body.ingredient_id,
        quantity=body.quantity,
        reason=body.reason,
        note=body.note,
        actor_user_id=staff.user_id,
        idempotency_key=_idem_key(request),
        ip_address=_client_ip(request),
    )
    return _transfer_receipt(db, result)


@router.get("/transfers", response_model=TransferListResponse)
def list_transfers(
    response: Response,
    direction: str | None = Query(default=None, pattern="^(OUTBOUND|INBOUND)$"),
    ingredient_id: int | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    db: Session = Depends(get_db),
    staff: CurrentStaff = Depends(require_permission(PERM_INVENTORY_READ)),
):
    """
    Transfers this store was involved in — both the ones it SENT and the ones it
    RECEIVED, newest first.

    Both sides are shown deliberately. A branch that only saw its outbound
    shipments could not answer "where did this crate of chocolate come from?",
    which is half of what traceability is for. A transfer between two OTHER stores
    is not visible here at all.
    """
    _no_store(response)
    store_id = _store_id(staff)

    q = (
        db.query(InventoryTransfer, Ingredient)
        .join(Ingredient, Ingredient.id == InventoryTransfer.ingredient_id)
        .filter(
            (InventoryTransfer.source_store_id == store_id)
            | (InventoryTransfer.destination_store_id == store_id)
        )
    )
    if direction == "OUTBOUND":
        q = q.filter(InventoryTransfer.source_store_id == store_id)
    elif direction == "INBOUND":
        q = q.filter(InventoryTransfer.destination_store_id == store_id)
    if ingredient_id is not None:
        q = q.filter(InventoryTransfer.ingredient_id == ingredient_id)

    rows = q.order_by(InventoryTransfer.id.desc()).limit(limit).all()
    items = [_transfer_item(t, ing, store_id) for t, ing in rows]
    return {"total": len(items), "items": items}


@router.get("/transfers/{transfer_id}", response_model=TransferItem)
def get_transfer(
    transfer_id: int,
    response: Response,
    db: Session = Depends(get_db),
    staff: CurrentStaff = Depends(require_permission(PERM_INVENTORY_READ)),
):
    """
    One transfer, if the caller's store is one of its two sides.

    A transfer between two other branches 404s rather than 403s: the caller has no
    business knowing it exists, and a 403 would confirm that it does.
    """
    _no_store(response)
    store_id = _store_id(staff)

    row = (
        db.query(InventoryTransfer, Ingredient)
        .join(Ingredient, Ingredient.id == InventoryTransfer.ingredient_id)
        .filter(
            InventoryTransfer.id == transfer_id,
            (InventoryTransfer.source_store_id == store_id)
            | (InventoryTransfer.destination_store_id == store_id),
        )
        .first()
    )
    if row is None:
        raise HTTPException(
            status_code=404,
            detail={
                "error": "transfer_not_found",
                "message": messages.INVENTORY_TRANSFER_NOT_FOUND,
            },
        )
    transfer, ingredient = row
    return _transfer_item(transfer, ingredient, store_id)
