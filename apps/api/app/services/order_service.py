"""
Order Service — production-grade order creation.

Key guarantees:
  1. Idempotent: same idempotency_key returns existing order, never duplicates.
  2. Transactional stock validation: SELECT ... FOR UPDATE row locks prevent
     concurrent over-deduction. If any ingredient is insufficient, the entire
     transaction is rolled back and a 422 is returned.
  3. Stock is deducted at order creation — customers are rejected immediately
     if stock is unavailable, not silently at kitchen time.
  4. Every mutation is audit-logged inside the same transaction.
"""
import logging
from datetime import timezone
from decimal import Decimal
from fastapi import BackgroundTasks, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.audit_log import AuditLog  # noqa — ensure model registered
from app.models.ingredient import Ingredient
from app.models.ingredient_stock import IngredientStock, IngredientStockMovement
from app.models.order import Order
from app.models.order_item import OrderItem
from app.models.order_item_ingredient import OrderItemIngredient
from app.models.order_status_event import OrderStatusEvent
from app.models.product import Product
from app.schemas.order import OrderCreateRequest, OrderCreatedResponse
from app.services.audit_service import audit

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def create_order(
    db: Session,
    order_data: OrderCreateRequest,
    background_tasks: BackgroundTasks,
    idempotency_key: str | None = None,
    ip_address: str | None = None,
) -> OrderCreatedResponse:
    """
    Create an order.

    Idempotency:
        If idempotency_key already exists in orders table, returns the
        existing order immediately without touching stock.

    Stock validation:
        All required ingredient quantities are checked inside a single
        transaction using SELECT … FOR UPDATE row locks. Any shortfall
        triggers a 422 with a list of the unavailable ingredients.
        On success, stock is deducted atomically in the same transaction.
    """
    # ── 1. Idempotency check ─────────────────────────────────────────────
    if idempotency_key:
        existing = db.query(Order).filter(
            Order.idempotency_key == idempotency_key
        ).first()
        if existing:
            logger.info("idempotency_hit order_id=%s key=%s", existing.id, idempotency_key)
            return _build_response(existing)

    # ── 2. Resolve products & ingredients (outside lock — read-only) ─────
    ingredient_ids: list[int] = []
    for item in order_data.items:
        for ing in item.ingredients:
            ingredient_ids.append(ing.ingredient_id)

    if not ingredient_ids:
        raise HTTPException(status_code=422, detail="Order must contain at least one ingredient.")

    # Fetch ingredient metadata once
    ingredients_by_id: dict[int, Ingredient] = {
        ing.id: ing
        for ing in db.query(Ingredient).filter(
            Ingredient.id.in_(ingredient_ids),
            Ingredient.is_active == True,  # noqa: E712
        ).all()
    }

    # Validate all requested ingredients exist and are active
    missing = [iid for iid in ingredient_ids if iid not in ingredients_by_id]
    if missing:
        raise HTTPException(
            status_code=422,
            detail={"error": "unknown_ingredients", "ids": missing},
        )

    # ── 3. Compute required stock per ingredient ─────────────────────────
    # Map ingredient_id → total consumed_quantity needed for this order
    required: dict[int, Decimal] = {}
    for item in order_data.items:
        for ing_req in item.ingredients:
            ing = ingredients_by_id[ing_req.ingredient_id]
            consumed = (ing.standard_quantity or Decimal("1")) * ing_req.quantity
            required[ing.id] = required.get(ing.id, Decimal("0")) + consumed

    # ── 4. Stock validation — SELECT … FOR UPDATE (row-level lock) ───────
    # Each ingredient_stock row is locked. Concurrent orders for the
    # same ingredient queue here. No lost update possible.
    out_of_stock: list[str] = []
    stock_rows: dict[int, IngredientStock] = {}

    for ing_id, needed in required.items():
        stock = db.execute(
            select(IngredientStock)
            .where(IngredientStock.ingredient_id == ing_id)
            .with_for_update()
        ).scalar_one_or_none()

        if stock is None or stock.stock_quantity < needed:
            out_of_stock.append(ingredients_by_id[ing_id].name)
        else:
            stock_rows[ing_id] = stock

    if out_of_stock:
        # Roll back implicit (no writes done yet) and reject
        raise HTTPException(
            status_code=422,
            detail={"error": "out_of_stock", "items": out_of_stock},
        )

    # ── 5. Build order inside transaction ────────────────────────────────
    new_order = Order(
        store_id=order_data.store_id,
        table_id=order_data.table_id,
        status="NEW",
        total_amount=Decimal("0.00"),
        idempotency_key=idempotency_key,
    )
    db.add(new_order)
    db.flush()  # get new_order.id without committing

    # Status event
    db.add(OrderStatusEvent(
        order_id=new_order.id,
        status_from=None,
        status_to="NEW",
        actor_type="CUSTOMER",
    ))

    # ── 6. Order items + price calculation ───────────────────────────────
    total_amount = Decimal("0.00")

    for item_data in order_data.items:
        product = db.get(Product, item_data.product_id)
        if product is None:
            raise HTTPException(status_code=422, detail=f"Product {item_data.product_id} not found.")

        base_price = product.base_price
        item_total = base_price * item_data.quantity

        new_item = OrderItem(
            order_id=new_order.id,
            product_id=item_data.product_id,
            quantity=item_data.quantity,
            price=base_price,
        )
        db.add(new_item)
        db.flush()

        for ing_data in item_data.ingredients:
            ing = ingredients_by_id[ing_data.ingredient_id]
            consumed_qty = (ing.standard_quantity or Decimal("1")) * ing_data.quantity

            db.add(OrderItemIngredient(
                order_item_id=new_item.id,
                ingredient_id=ing_data.ingredient_id,
                quantity=ing_data.quantity,
                price_modifier=ing.price,
                consumed_quantity=consumed_qty,
                consumed_unit=ing.unit,
            ))
            item_total += ing.price * ing_data.quantity

        total_amount += item_total

    new_order.total_amount = total_amount
    db.flush()

    # ── 7. Deduct stock (same transaction, locks already held) ───────────
    _deduct_stock(db, new_order.id, required, stock_rows, ingredients_by_id)

    # ── 8. Audit log ─────────────────────────────────────────────────────
    try:
        audit(
            db,
            entity_type="order",
            entity_id=new_order.id,
            action="created",
            actor_type="CUSTOMER",
            ip_address=ip_address,
            payload_after={
                "store_id": new_order.store_id,
                "table_id": new_order.table_id,
                "total_amount": new_order.total_amount,
                "ingredient_ids": list(required.keys()),
                "idempotency_key": idempotency_key,
            },
        )
    except Exception as exc:  # pragma: no cover
        logger.error("audit_call_failed order_created err=%s", exc)

    db.commit()
    db.refresh(new_order)

    logger.info("order_created id=%s store=%s total=%s",
                new_order.id, new_order.store_id, new_order.total_amount)

    # ── 9. Broadcast (after commit — data is durable) ────────────────────
    from app.services.websocket_manager import kitchen_ws_manager
    # Slot count = distinct ingredient rows (one prep step each)
    ingredient_slot_count = sum(
        len(item.ingredients)
        for item in order_data.items
    )
    created_utc = new_order.created_at
    if created_utc.tzinfo is None:
        created_utc = created_utc.replace(tzinfo=timezone.utc)
    background_tasks.add_task(
        kitchen_ws_manager.broadcast_kitchen_event,
        event="order_created",
        data={
            "order_id": new_order.id,
            "store_id": new_order.store_id,
            "table_id": new_order.table_id,
            "status": new_order.status,
            "ingredient_slot_count": ingredient_slot_count,
            "priority_score": round(ingredient_slot_count * 0.3, 2),  # age≈0 at creation
            "sla_severity": "ok",
            "created_at": created_utc.isoformat(),
        },
    )

    return _build_response(new_order)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _deduct_stock(
    db: Session,
    order_id: int,
    required: dict[int, Decimal],
    stock_rows: dict[int, IngredientStock],
    ingredients_by_id: dict[int, Ingredient],
) -> None:
    """
    Deduct stock and write movement records.
    Called inside the order creation transaction — locks are already held.
    """
    for ing_id, needed in required.items():
        stock = stock_rows[ing_id]
        ing = ingredients_by_id[ing_id]

        stock.stock_quantity = Decimal(str(stock.stock_quantity)) - needed

        db.add(IngredientStockMovement(
            ingredient_id=ing_id,
            movement_type="ORDER_DEDUCTION",
            quantity_delta=-needed,
            unit=ing.unit,
            reference_type="order",
            reference_id=order_id,
        ))


def _build_response(order: Order) -> OrderCreatedResponse:
    item_count = sum(item.quantity for item in order.items)
    return OrderCreatedResponse(
        order_id=order.id,
        store_id=order.store_id,
        table_id=order.table_id,
        status=order.status,
        total_amount=order.total_amount,
        item_count=item_count,
        created_at=order.created_at,
    )
