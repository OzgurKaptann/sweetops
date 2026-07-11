"""
Inventory API — protected stock reads and manual stock mutations.

Every route requires an authenticated staff session. Reads need
``inventory:read``; every mutation needs ``inventory:adjust`` plus a trusted
Origin, a valid CSRF token (both enforced by ``require_permission`` on
state-changing methods) and an ``Idempotency-Key`` header.

Nothing here is public. Customers never see inventory internals: the customer
menu only exposes a coarse in_stock/low_stock/out_of_stock status, and order
rejection returns a Turkish message with no stock figures.

Inventory is GLOBAL in this schema (no store_id), so every route fails closed
with a Turkish 409 when more than one operational store exists — a Store-B
manager must never be shown, let alone allowed to write off, Store-A's stock.
See docs/INVENTORY_LIFECYCLE.md.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request, Response
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.deps import require_permission
from app.core.permissions import PERM_INVENTORY_ADJUST, PERM_INVENTORY_READ
from app.models.ingredient import Ingredient
from app.models.ingredient_stock import (
    MOVEMENT_TYPES,
    IngredientStock,
    IngredientStockMovement,
)
from app.schemas.inventory import (
    ManualAdjustmentRequest,
    MovementListResponse,
    MovementReceipt,
    PurchaseReceiptRequest,
    StockListResponse,
    WasteRequest,
)
from app.services import inventory_service
from app.services.auth_service import CurrentStaff
from app.services.inventory_guard import assert_single_operational_store

router = APIRouter(prefix="/inventory", tags=["Inventory"])


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
        .filter(IngredientStock.ingredient_id == movement.ingredient_id)
        .first()
    )
    return MovementReceipt(
        movement_id=movement.id,
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
    """On-hand, reserved and available quantities for every active ingredient."""
    _no_store(response)
    assert_single_operational_store(db)

    rows = (
        db.query(Ingredient, IngredientStock)
        .join(IngredientStock, IngredientStock.ingredient_id == Ingredient.id)
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
    """The append-only movement ledger, newest first."""
    _no_store(response)
    assert_single_operational_store(db)

    q = db.query(IngredientStockMovement, Ingredient).join(
        Ingredient, Ingredient.id == IngredientStockMovement.ingredient_id
    )
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
    """Record goods received from a supplier — physical stock goes up."""
    _no_store(response)
    assert_single_operational_store(db)

    result = inventory_service.record_purchase_receipt(
        db,
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
    """Correct on-hand stock to a real physical count (signed delta + reason)."""
    _no_store(response)
    assert_single_operational_store(db)

    result = inventory_service.record_manual_adjustment(
        db,
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
    """Record stock physically thrown away. Stays visible as WASTE, never consumption."""
    _no_store(response)
    assert_single_operational_store(db)

    result = inventory_service.record_waste(
        db,
        ingredient_id=body.ingredient_id,
        quantity=body.quantity,
        reason=body.reason,
        actor_user_id=staff.user_id,
        idempotency_key=_idem_key(request),
        ip_address=_client_ip(request),
    )
    return _receipt(db, result.movement, replay=result.replayed)
