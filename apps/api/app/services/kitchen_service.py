"""
Kitchen Service — order status transitions with strict state machine enforcement.

Key guarantees:
  1. Invalid transitions are rejected with 409.
  2. Terminal states (DELIVERED, CANCELLED) are immutable.
  3. Backward transitions (undo) are allowed only within UNDO_WINDOW_SECONDS.
  4. Stock is returned when an IN_PREP order is cancelled.
  5. Every transition is audit-logged.
  6. N+1 queries eliminated — kitchen orders fetched with eager loads.
"""
import logging
from datetime import datetime, timezone, timedelta
from decimal import Decimal

from fastapi import BackgroundTasks, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.models.ingredient import Ingredient
from app.models.ingredient_stock import IngredientStock, IngredientStockMovement
from app.models.order import Order
from app.models.order_item import OrderItem
from app.models.order_item_ingredient import OrderItemIngredient
from app.models.order_status_event import OrderStatusEvent
from app.models.product import Product
from app.services.audit_service import audit

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# State machine definition
# ---------------------------------------------------------------------------

VALID_TRANSITIONS: dict[str, list[str]] = {
    "NEW":     ["IN_PREP", "CANCELLED"],
    "IN_PREP": ["READY",   "CANCELLED"],
    "READY":   ["DELIVERED"],
}

# Backward (undo) transitions — only within UNDO_WINDOW_SECONDS of forward move
UNDO_TRANSITIONS: dict[str, str] = {
    "IN_PREP": "NEW",
    "READY":   "IN_PREP",
}

TERMINAL_STATES = {"DELIVERED", "CANCELLED"}
UNDO_WINDOW_SECONDS = 60

# SLA thresholds (minutes)
SLA_WARNING_MINUTES = 7    # amber — approaching limit
SLA_CRITICAL_MINUTES = 10  # red   — SLA breached

# NEW orders haven't been acknowledged yet → urgency accrues faster per minute
_STATUS_WEIGHT: dict[str, float] = {"NEW": 1.2, "IN_PREP": 1.0}

# Multiplier jumps at each SLA threshold so breached orders always lead the queue
_SLA_MULTIPLIER: dict[str, float] = {"ok": 1.0, "warning": 1.5, "critical": 2.5}


# ---------------------------------------------------------------------------
# SLA severity
# ---------------------------------------------------------------------------

def _sla_severity(age_minutes: float) -> str:
    """
    ok       — age < SLA_WARNING_MINUTES     (green, normal)
    warning  — SLA_WARNING_MINUTES ≤ age < SLA_CRITICAL_MINUTES  (amber, attention)
    critical — age ≥ SLA_CRITICAL_MINUTES    (red, SLA breached)
    """
    if age_minutes >= SLA_CRITICAL_MINUTES:
        return "critical"
    if age_minutes >= SLA_WARNING_MINUTES:
        return "warning"
    return "ok"


# ---------------------------------------------------------------------------
# Priority scoring
# ---------------------------------------------------------------------------

def _priority_score(age_minutes: float, ingredient_slot_count: int, status: str) -> float:
    """
    Higher score = higher urgency = appears first in kitchen queue.

    Formula:
        score = (age_minutes × status_weight × sla_multiplier) + complexity_bonus

    status_weight:
        NEW    = 1.2  — unacknowledged; each minute counts more
        IN_PREP = 1.0 — actively being worked on

    sla_multiplier (see _sla_severity):
        ok       = 1.0  — linear growth
        warning  = 1.5  — urgency accelerates near threshold
        critical = 2.5  — guaranteed above all non-critical orders

    complexity_bonus:
        min(ingredient_slot_count, 5) × 0.3
        Uses distinct ingredient slot count (number of OrderItemIngredient rows),
        NOT quantity sum. Three portions of strawberry is one prep step, not three.
        Capped at 5 slots so outliers cannot override age-based urgency.
        Maximum bonus = 1.5 (vs. minimum critical-zone score ≈ 25 for 10-min order).

    Why this beats age + count*0.5:
        1. Status-aware: NEW/IN_PREP have different urgency rates.
        2. Non-linear SLA: breached orders always float above fresh complex orders.
        3. Correct complexity metric: slots not quantities.
        4. Coefficients have explicit operational justification.

    Examples:
        11 min NEW,     2 slots → 11×1.2×2.5 + 0.6 = 33.6  (critical, top)
         8 min IN_PREP, 5 slots → 8×1.0×1.5 + 1.5 = 13.5  (warning)
         5 min NEW,     3 slots → 5×1.2×1.0 + 0.9 =  6.9  (ok)
         1 min NEW,     5 slots → 1×1.2×1.0 + 1.5 =  2.7  (ok, bottom)
    """
    weight = _STATUS_WEIGHT.get(status, 1.0)
    multiplier = _SLA_MULTIPLIER[_sla_severity(age_minutes)]
    complexity = min(ingredient_slot_count, 5) * 0.3
    return round(age_minutes * weight * multiplier + complexity, 2)


# ---------------------------------------------------------------------------
# Timestamp helper
# ---------------------------------------------------------------------------

def _to_utc(dt: datetime) -> datetime:
    """Return dt as UTC-aware. Treats naive datetimes as UTC (PostgreSQL default)."""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------

def get_kitchen_orders(db: Session, store_id: int = 1) -> list[dict]:
    """
    Return active orders (NEW, IN_PREP) for the kitchen screen.

    Sorted by priority_score descending (highest urgency first).
    Each order includes computed_age_minutes, priority_score, sla_severity.
    Eager-loads items → ingredients to avoid N+1.
    All datetimes are UTC-aware ISO-8601 strings.
    """
    now = datetime.now(timezone.utc)

    orders = (
        db.query(Order)
        .options(
            selectinload(Order.items)
            .selectinload(OrderItem.ingredients)
        )
        .filter(
            Order.store_id == store_id,
            Order.status.in_(["NEW", "IN_PREP"]),
        )
        .all()
    )

    # Batch-load products and ingredients to avoid per-row queries
    product_ids = {item.product_id for o in orders for item in o.items}
    ingredient_ids = {
        oii.ingredient_id
        for o in orders
        for item in o.items
        for oii in item.ingredients
    }

    products = {p.id: p for p in db.query(Product).filter(Product.id.in_(product_ids)).all()}
    ingredients = {i.id: i for i in db.query(Ingredient).filter(Ingredient.id.in_(ingredient_ids)).all()}

    result = []
    for order in orders:
        created_utc = _to_utc(order.created_at)
        age_minutes = round((now - created_utc).total_seconds() / 60, 1)

        items_list = []
        ingredient_slot_count = 0  # distinct OII rows, not quantity sum

        for item in order.items:
            product = products.get(item.product_id)
            ing_list = []
            for oii in item.ingredients:
                ing_list.append({
                    "id": oii.id,
                    "ingredient_id": oii.ingredient_id,
                    "ingredient_name": ingredients[oii.ingredient_id].name
                    if oii.ingredient_id in ingredients else "Bilinmiyor",
                    "quantity": oii.quantity,
                })
                ingredient_slot_count += 1  # one prep step per distinct ingredient row

            items_list.append({
                "id": item.id,
                "product_id": item.product_id,
                "product_name": product.name if product else "Bilinmiyor",
                "quantity": item.quantity,
                "ingredients": ing_list,
            })

        severity = _sla_severity(age_minutes)
        score = _priority_score(age_minutes, ingredient_slot_count, order.status)

        result.append({
            "id": order.id,
            "store_id": order.store_id,
            "table_id": order.table_id,
            "status": order.status,
            "created_at": created_utc.isoformat(),  # always UTC ISO-8601
            "computed_age_minutes": age_minutes,
            "priority_score": score,
            "sla_severity": severity,
            "items": items_list,
        })

    # Highest urgency first
    result.sort(key=lambda o: o["priority_score"], reverse=True)
    return result


# ---------------------------------------------------------------------------
# Write
# ---------------------------------------------------------------------------

def update_order_status(
    db: Session,
    order_id: int,
    new_status: str,
    background_tasks: BackgroundTasks,
    actor_type: str = "STAFF",
    actor_id: str | None = None,
) -> Order:
    """
    Transition an order to new_status.

    Guards:
      - Terminal state → 409
      - Invalid forward transition → 409
      - Undo transition outside 60s window → 410
      - Stock deducted on NEW → IN_PREP (idempotent guard)
      - Stock returned on IN_PREP → CANCELLED
    """
    order = db.query(Order).filter(Order.id == order_id).first()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found.")

    old_status = order.status

    # ── Guard: terminal state ─────────────────────────────────────────────
    if old_status in TERMINAL_STATES:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "terminal_state",
                "current_status": old_status,
                "message": "Bu sipariş tamamlandı veya iptal edildi.",
            },
        )

    # ── Determine transition type ─────────────────────────────────────────
    is_undo = (
        new_status in UNDO_TRANSITIONS.values()
        and UNDO_TRANSITIONS.get(old_status) == new_status
    )

    if is_undo:
        _validate_undo_window(db, order_id, old_status)
    else:
        _validate_forward_transition(old_status, new_status)

    # ── Apply transition ───────────────────────────────────────────────────
    snapshot_before = {"status": old_status, "order_id": order.id}

    order.status = new_status

    db.add(OrderStatusEvent(
        order_id=order.id,
        status_from=old_status,
        status_to=new_status,
        actor_type=actor_type,
        actor_id=actor_id,
    ))

    # ── Side effects ──────────────────────────────────────────────────────
    if new_status == "IN_PREP" and not is_undo:
        # Stock was already deducted at order creation.
        # This is intentionally a no-op — kept for clarity and future hooks.
        pass

    if is_undo and old_status == "IN_PREP":
        # Undo of IN_PREP: we're going back to NEW.
        # Stock was deducted at order creation, NOT at IN_PREP transition,
        # so no stock return needed here.
        pass

    if new_status == "CANCELLED":
        # Stock was deducted at order creation — return it on cancellation.
        _return_stock_for_order(db, order)

    # ── Audit ─────────────────────────────────────────────────────────────
    try:
        audit(
            db,
            entity_type="order",
            entity_id=order.id,
            action="status_changed",
            actor_type=actor_type,
            actor_id=actor_id,
            payload_before=snapshot_before,
            payload_after={"status": new_status},
        )
    except Exception as exc:  # pragma: no cover
        logger.error("audit_call_failed status_changed order=%s err=%s", order.id, exc)

    db.commit()
    db.refresh(order)

    logger.info("order_status_changed id=%s %s→%s actor=%s",
                order.id, old_status, new_status, actor_type)

    # ── Broadcast ─────────────────────────────────────────────────────────
    from app.services.websocket_manager import kitchen_ws_manager

    now_utc = datetime.now(timezone.utc)
    age_min = round((now_utc - _to_utc(order.created_at)).total_seconds() / 60, 1)

    background_tasks.add_task(
        kitchen_ws_manager.broadcast_kitchen_event,
        event="order_status_updated",
        data={
            "order_id": order.id,
            "store_id": order.store_id,
            "from_status": old_status,
            "to_status": new_status,
            "computed_age_minutes": age_min,
            "sla_severity": _sla_severity(age_min),
            "updated_at": now_utc.isoformat(),
        },
    )

    return order


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _validate_forward_transition(current: str, requested: str) -> None:
    allowed = VALID_TRANSITIONS.get(current, [])
    if requested not in allowed:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "invalid_transition",
                "current_status": current,
                "attempted": requested,
                "allowed": allowed,
            },
        )


def _validate_undo_window(db: Session, order_id: int, current_status: str) -> None:
    """
    Undo is only permitted within UNDO_WINDOW_SECONDS of the last forward
    transition into current_status.
    """
    last_event = (
        db.query(OrderStatusEvent)
        .filter(
            OrderStatusEvent.order_id == order_id,
            OrderStatusEvent.status_to == current_status,
        )
        .order_by(OrderStatusEvent.created_at.desc())
        .first()
    )

    if not last_event:
        raise HTTPException(status_code=409, detail={"error": "undo_no_event"})

    now = datetime.now(timezone.utc)
    # created_at may be naive (no tz) depending on DB driver — normalise
    event_time = last_event.created_at
    if event_time.tzinfo is None:
        event_time = event_time.replace(tzinfo=timezone.utc)

    elapsed = (now - event_time).total_seconds()
    if elapsed > UNDO_WINDOW_SECONDS:
        raise HTTPException(
            status_code=410,
            detail={
                "error": "undo_window_expired",
                "elapsed_seconds": int(elapsed),
                "window_seconds": UNDO_WINDOW_SECONDS,
                "message": "Geri alma süresi doldu.",
            },
        )


def _return_stock_for_order(db: Session, order: Order) -> None:
    """
    Return stock for a cancelled order.
    Idempotent: if a CANCELLATION_RETURN movement already exists, skip.
    Only returns stock if an ORDER_DEDUCTION movement exists
    (i.e., stock was actually deducted at order creation).
    """
    deduction_exists = (
        db.query(IngredientStockMovement)
        .filter(
            IngredientStockMovement.reference_type == "order",
            IngredientStockMovement.reference_id == order.id,
            IngredientStockMovement.movement_type == "ORDER_DEDUCTION",
        )
        .first()
    )
    if not deduction_exists:
        return

    already_returned = (
        db.query(IngredientStockMovement)
        .filter(
            IngredientStockMovement.reference_type == "order",
            IngredientStockMovement.reference_id == order.id,
            IngredientStockMovement.movement_type == "CANCELLATION_RETURN",
        )
        .first()
    )
    if already_returned:
        logger.info("cancellation_return already exists for order %s, skipping", order.id)
        return

    # Eagerly load if not already
    if not order.items:
        order = (
            db.query(Order)
            .options(selectinload(Order.items).selectinload(OrderItem.ingredients))
            .filter(Order.id == order.id)
            .first()
        )

    for item in order.items:
        for oii in item.ingredients:
            if not oii.consumed_quantity or not oii.consumed_unit:
                continue

            consumed = Decimal(str(oii.consumed_quantity))

            db.add(IngredientStockMovement(
                ingredient_id=oii.ingredient_id,
                movement_type="CANCELLATION_RETURN",
                quantity_delta=consumed,   # positive = return to stock
                unit=oii.consumed_unit,
                reference_type="order",
                reference_id=order.id,
            ))

            stock = (
                db.query(IngredientStock)
                .filter(IngredientStock.ingredient_id == oii.ingredient_id)
                .first()
            )
            if stock:
                stock.stock_quantity = Decimal(str(stock.stock_quantity)) + consumed

    try:
        audit(
            db,
            entity_type="order",
            entity_id=order.id,
            action="stock_returned",
            actor_type="SYSTEM",
            payload_after={"reason": "CANCELLATION", "order_id": order.id},
        )
    except Exception as exc:  # pragma: no cover
        logger.error("audit_call_failed stock_returned order=%s err=%s", order.id, exc)

    logger.info("stock_returned for cancelled order %s", order.id)
