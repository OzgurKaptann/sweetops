"""
Kitchen Service — order status transitions with strict state machine enforcement.

Key guarantees:
  1. Invalid transitions are rejected with 409, mutating no stock.
  2. Terminal states (DELIVERED, CANCELLED) are immutable.
  3. Backward transitions (undo) are allowed only within UNDO_WINDOW_SECONDS.
  4. Preparation status drives the INVENTORY lifecycle through explicit rules —
     it never carries inventory meaning implicitly:
       • entering IN_PREP (the first physical-preparation state) converts the
         order's outstanding reservation into physical consumption, exactly once;
       • READY / DELIVERED consume nothing further;
       • CANCELLED releases whatever is still merely reserved and NEVER restores
         stock that was already physically consumed.
  5. Inventory movement and the status change commit in ONE transaction — an
     order can never be marked IN_PREP without its consumption, or consumed
     without being marked IN_PREP.
  6. Payment safety is evaluated BEFORE any inventory mutation.
  7. Every transition is audit-logged.
  8. N+1 queries eliminated — kitchen orders fetched with eager loads.
"""
import logging
from datetime import datetime, timezone, timedelta
from decimal import Decimal

from fastapi import BackgroundTasks, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.core import messages
from app.models.ingredient import Ingredient
from app.models.order import Order
from app.models.order_item import OrderItem
from app.models.order_status_event import OrderStatusEvent
from app.models.product import Product
from app.services import inventory_service
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

# The status at which the kitchen physically starts cooking. Entering it is what
# turns a reservation into real consumption — the single, explicit bridge from
# preparation state to inventory state.
CONSUMING_STATUS = "IN_PREP"

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

# Decision signal thresholds
START_IMMEDIATELY_MINUTES = 3   # NEW orders waiting ≥ this should be started even before warning zone

# Batching
BATCH_TIME_SAVE_SECONDS = 30    # seconds saved per extra order per shared ingredient when batching

# Kitchen load thresholds (active order count)
LOAD_MEDIUM_THRESHOLD = 4       # ≥4 active orders → medium
LOAD_HIGH_THRESHOLD = 7         # ≥7 active orders → high


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
# Decision signals
# ---------------------------------------------------------------------------

def _decision_signals(
    age_minutes: float, status: str, severity: str
) -> tuple[bool, str]:
    """
    Determine whether an order needs immediate action and articulate why.

    The reason string is read by kitchen staff on the order card, so it is
    Turkish. The status/severity values it branches on stay English — they are
    the wire contract, not copy.

    NEW orders — should_be_started = True when:
        • SLA critical (≥10 min): breach already occurred
        • SLA warning (7–10 min): approaching breach
        • age ≥ START_IMMEDIATELY_MINUTES (3 min): waited long enough even in ok zone

    IN_PREP orders — should_be_started = True when:
        • SLA critical: execution is taking too long → expedite
        • SLA warning: running long → finish soon
    """
    if status == "NEW":
        if severity == "critical":
            return True, f"Süre aşıldı — {age_minutes:.1f} dk sırada bekliyor"
        if severity == "warning":
            return True, f"Süre doluyor — {age_minutes:.1f} dk sırada bekliyor"
        if age_minutes >= START_IMMEDIATELY_MINUTES:
            return True, f"{age_minutes:.1f} dk bekledi — şimdi başlayın"
        return False, f"Yeni geldi — {age_minutes:.1f} dk sırada"

    if status == "IN_PREP":
        if severity == "critical":
            return True, "Süre aşıldı — hemen yetiştirin"
        if severity == "warning":
            return True, f"Uzun sürüyor — {age_minutes:.1f} dk geçti"
        return False, f"Hazırlanıyor — {age_minutes:.1f} dk geçti"

    return False, "Bu sipariş için işlem gerekmiyor."


def _action_hint(
    order_id: int,
    status: str,
    severity: str,
    age_minutes: float,
    batch_partner_ids: list[int],
) -> str:
    """
    Single actionable instruction for kitchen staff, in Turkish — this is the
    line a cook reads mid-service, so it is an imperative, not a description.

    Precedence for NEW orders (highest → lowest):
        1. critical SLA → "Hemen başlayın — süre aşıldı"
        2. warning SLA  → "Yakında başlayın — süre doluyor"
        3. has batch partners → "#X ile birlikte hazırlayın"
        4. age ≥ threshold  → "Şimdi başlayın"
        5. fresh             → "Bekleyebilir"

    Precedence for IN_PREP orders:
        1. critical → "Yetiştirin — süre aşıldı"
        2. warning  → "Yakında bitirin — süre doluyor"
        3. normal   → "Hazırlanıyor"
    """
    if status == "IN_PREP":
        if severity == "critical":
            return "Yetiştirin — süre aşıldı"
        if severity == "warning":
            return "Yakında bitirin — süre doluyor"
        return "Hazırlanıyor"

    if status == "NEW":
        if severity == "critical":
            return "Hemen başlayın — süre aşıldı"
        if severity == "warning":
            return "Yakında başlayın — süre doluyor"
        if batch_partner_ids:
            return f"#{batch_partner_ids[0]} ile birlikte hazırlayın"
        if age_minutes >= START_IMMEDIATELY_MINUTES:
            return "Şimdi başlayın"
        return "Bekleyebilir"

    return "İşlem gerekmiyor"


# ---------------------------------------------------------------------------
# Batching suggestions
# ---------------------------------------------------------------------------

def _batching_suggestions(orders: list[dict]) -> list[dict]:
    """
    Finds NEW orders that share at least one ingredient and groups them.

    Algorithm: union-find on ingredient co-occurrence.
        Two orders are connected if they share any ingredient.
        Transitive connections form one group (order A shares with B,
        B shares with C → A, B, C are one group even if A and C share nothing).

    Only NEW orders are eligible — IN_PREP orders have already started.

    Each suggestion:
        grouped_order_ids   — sorted list of order IDs in the batch
        shared_ingredients  — ingredient names shared by ≥2 orders in the group
        estimated_time_saved — seconds saved: Σ (n_orders_per_ingredient - 1) × 30s

    Time-save rationale: batching N orders that all need ingredient X means
    prepping X once instead of N times. Each avoided prep = 30 s saved.
    """
    # Step 1: collect ingredient sets for NEW orders
    order_ingredients: dict[int, set[str]] = {}
    ing_to_orders: dict[str, list[int]] = {}

    for order in orders:
        if order["status"] != "NEW":
            continue
        ings: set[str] = set()
        for item in order["items"]:
            for oii in item["ingredients"]:
                name = oii["ingredient_name"]
                ings.add(name)
                if name not in ing_to_orders:
                    ing_to_orders[name] = []
                if order["id"] not in ing_to_orders[name]:
                    ing_to_orders[name].append(order["id"])
        if ings:
            order_ingredients[order["id"]] = ings

    if len(order_ingredients) < 2:
        return []

    # Step 2: union-find
    parent: dict[int, int] = {oid: oid for oid in order_ingredients}

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]  # path compression
            x = parent[x]
        return x

    for oid_list in ing_to_orders.values():
        for i in range(1, len(oid_list)):
            px, py = find(oid_list[0]), find(oid_list[i])
            if px != py:
                parent[px] = py

    # Step 3: group orders by component root
    groups: dict[int, list[int]] = {}
    for oid in order_ingredients:
        root = find(oid)
        groups.setdefault(root, []).append(oid)

    # Step 4: build suggestions for groups with ≥2 orders
    suggestions = []
    for group_ids in sorted(groups.values(), key=lambda g: min(g)):
        if len(group_ids) < 2:
            continue

        # Ingredients shared by ≥2 orders in this group
        ing_counts: dict[str, int] = {}
        for oid in group_ids:
            for ing in order_ingredients[oid]:
                ing_counts[ing] = ing_counts.get(ing, 0) + 1
        shared_ings = sorted(ing for ing, cnt in ing_counts.items() if cnt >= 2)

        if not shared_ings:
            continue

        # Time saved: one fewer prep per extra order that uses the ingredient
        time_saved_s = sum(
            (ing_counts[ing] - 1) * BATCH_TIME_SAVE_SECONDS
            for ing in shared_ings
        )

        suggestions.append({
            "grouped_order_ids": sorted(group_ids),
            "shared_ingredients": shared_ings,
            "estimated_time_saved": f"{time_saved_s}s",
        })

    return suggestions


# ---------------------------------------------------------------------------
# Kitchen load
# ---------------------------------------------------------------------------

def _kitchen_load(orders: list[dict]) -> dict:
    """
    Classifies kitchen load from active (NEW + IN_PREP) order count.

    Thresholds:
        low:    count < LOAD_MEDIUM_THRESHOLD  (< 4)
        medium: LOAD_MEDIUM_THRESHOLD ≤ count < LOAD_HIGH_THRESHOLD  (4–6)
        high:   count ≥ LOAD_HIGH_THRESHOLD   (≥ 7)

    average_age_minutes: mean age of active orders — proxy for in-queue prep time.
    """
    active_count = len(orders)
    in_prep_count = sum(1 for o in orders if o["status"] == "IN_PREP")
    new_count = active_count - in_prep_count

    if active_count == 0:
        return {
            "load_level": "low",
            "active_orders_count": 0,
            "in_prep_count": 0,
            "average_age_minutes": 0.0,
            "explanation": "Bekleyen sipariş yok — mutfak sakin.",
        }

    avg_age = round(
        sum(o["computed_age_minutes"] for o in orders) / active_count, 1
    )

    counts = f"{active_count} açık sipariş ({new_count} bekliyor, {in_prep_count} hazırlanıyor)"

    if active_count >= LOAD_HIGH_THRESHOLD:
        level = "high"
        explanation = f"{counts} — yoğun, sipariş başına ortalama {avg_age} dk."
    elif active_count >= LOAD_MEDIUM_THRESHOLD:
        level = "medium"
        explanation = f"{counts} — normal tempo, sipariş başına ortalama {avg_age} dk."
    else:
        level = "low"
        explanation = f"{counts} — sakin, sipariş başına ortalama {avg_age} dk."

    return {
        "load_level": level,
        "active_orders_count": active_count,
        "in_prep_count": in_prep_count,
        "average_age_minutes": avg_age,
        "explanation": explanation,
    }


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------

def get_kitchen_orders(db: Session, store_id: int = 1) -> dict:
    """
    Return the kitchen dashboard: active orders + decision intelligence.

    Returns a structured dict with three keys:
        orders              — priority-sorted list of active orders with
                              per-order decision signals and action hints
        kitchen_load        — load level, counts, average age, explanation
        batching_suggestions — ingredient-grouped batch opportunities

    All datetimes are UTC ISO-8601 strings.
    Eager-loads items → ingredients (no N+1).
    """
    now = datetime.now(timezone.utc)

    db_orders = (
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
    product_ids = {item.product_id for o in db_orders for item in o.items}
    ingredient_ids = {
        oii.ingredient_id
        for o in db_orders
        for item in o.items
        for oii in item.ingredients
    }

    products = {p.id: p for p in db.query(Product).filter(Product.id.in_(product_ids)).all()}
    ingredients_map = {
        i.id: i
        for i in db.query(Ingredient).filter(Ingredient.id.in_(ingredient_ids)).all()
    }

    # ── Build order list with scoring ─────────────────────────────────────
    result: list[dict] = []
    for order in db_orders:
        created_utc = _to_utc(order.created_at)
        age_minutes = round((now - created_utc).total_seconds() / 60, 1)

        items_list = []
        ingredient_slot_count = 0

        for item in order.items:
            product = products.get(item.product_id)
            ing_list = []
            for oii in item.ingredients:
                ing_list.append({
                    "id": oii.id,
                    "ingredient_id": oii.ingredient_id,
                    "ingredient_name": ingredients_map[oii.ingredient_id].name
                    if oii.ingredient_id in ingredients_map else "Bilinmiyor",
                    "quantity": oii.quantity,
                })
                ingredient_slot_count += 1

            items_list.append({
                "id": item.id,
                "product_id": item.product_id,
                "product_name": product.name if product else "Bilinmiyor",
                "quantity": item.quantity,
                "ingredients": ing_list,
            })

        severity = _sla_severity(age_minutes)
        score = _priority_score(age_minutes, ingredient_slot_count, order.status)
        should_start, urgency_reason = _decision_signals(age_minutes, order.status, severity)

        result.append({
            "id": order.id,
            "store_id": order.store_id,
            "table_id": order.table_id,
            "status": order.status,
            "created_at": created_utc.isoformat(),
            "computed_age_minutes": age_minutes,
            "priority_score": score,
            "sla_severity": severity,
            "should_be_started": should_start,
            "urgency_reason": urgency_reason,
            "action_hint": "",           # filled in after batching is computed
            "items": items_list,
        })

    # Highest urgency first
    result.sort(key=lambda o: o["priority_score"], reverse=True)

    # ── Batching suggestions (needs sorted result) ─────────────────────────
    suggestions = _batching_suggestions(result)

    # Build order_id → batch partner IDs map (for action_hint)
    order_to_partners: dict[int, list[int]] = {}
    for suggestion in suggestions:
        for oid in suggestion["grouped_order_ids"]:
            partners = [x for x in suggestion["grouped_order_ids"] if x != oid]
            order_to_partners[oid] = partners

    # Fill action_hint now that batch info is available
    for order in result:
        partners = order_to_partners.get(order["id"], [])
        order["action_hint"] = _action_hint(
            order["id"],
            order["status"],
            order["sla_severity"],
            order["computed_age_minutes"],
            partners,
        )

    # ── Kitchen load ───────────────────────────────────────────────────────
    load = _kitchen_load(result)

    return {
        "orders": result,
        "kitchen_load": load,
        "batching_suggestions": suggestions,
    }


# ---------------------------------------------------------------------------
# Write
# ---------------------------------------------------------------------------

def update_order_status(
    db: Session,
    order_id: int,
    new_status: str,
    background_tasks: BackgroundTasks,
    store_id: int | None = None,
    actor_type: str = "STAFF",
    actor_id: str | None = None,
) -> Order:
    """
    Transition an order to new_status.

    Store isolation:
      When store_id is provided, an order belonging to a different store is
      treated as non-existent (404) so cross-store existence is not disclosed.

    Guards:
      - Terminal state → 409
      - Invalid forward transition → 409 (no stock is touched)
      - Undo transition outside 60s window → 410
      - Cancellation of an order still holding collected money → 409, raised
        BEFORE any inventory mutation

    Inventory:
      - → IN_PREP   : outstanding reservation becomes physical consumption, once
      - → READY/DELIVERED : nothing
      - → CANCELLED : outstanding reservation is released; already-consumed
                      stock is NOT restored
    """
    # Lock the order row for the whole transition. This serialises a start-prep
    # against a concurrent cancel of the same order: whichever commits first
    # settles the reservation, and the other then finds nothing outstanding.
    order = db.execute(
        select(Order)
        .where(Order.id == order_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if not order:
        raise HTTPException(status_code=404, detail=messages.ORDER_NOT_FOUND)

    # Non-disclosing cross-store guard: a Store-A user must not be able to
    # distinguish "order belongs to Store B" from "order does not exist".
    if store_id is not None and order.store_id != store_id:
        raise HTTPException(status_code=404, detail=messages.ORDER_NOT_FOUND)

    old_status = order.status

    # ── Guard: terminal state ─────────────────────────────────────────────
    if old_status in TERMINAL_STATES:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "terminal_state",
                "current_status": old_status,
                "message": messages.ORDER_ALREADY_CLOSED,
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

    # ── Guard: cannot cancel an order that still holds collected money ─────
    # Payment state is independent of preparation state. An order with a
    # positive net paid amount (paid − refunded) must have its collection
    # refunded before it can be cancelled — cancellation never fabricates a
    # refund and never silently discards collected cash.
    if new_status == "CANCELLED":
        paid = Decimal(str(order.paid_amount or 0))
        refunded = Decimal(str(order.refunded_amount or 0))
        if paid - refunded > Decimal("0"):
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "payment_outstanding",
                    "current_status": old_status,
                    "message": messages.ORDER_CANCEL_BLOCKED_PAID,
                },
            )

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

    # ── Inventory side effects (same transaction as the status change) ────
    actor_user_id = _actor_user_id(actor_id)

    if new_status == CONSUMING_STATUS and not is_undo:
        # The kitchen is starting to cook: the reservation becomes physical
        # consumption. Idempotent — consume_order settles only the OUTSTANDING
        # reservation, so a re-entry after an undo (NEW → IN_PREP → NEW →
        # IN_PREP) finds nothing left to consume and deducts nothing twice.
        inventory_service.consume_order(
            db, order, actor_type=actor_type, actor_user_id=actor_user_id
        )

    elif is_undo and old_status == CONSUMING_STATUS:
        # Undo IN_PREP → NEW. Deliberately does NOT un-consume: the batter was
        # really poured. The order returns to the queue while the ingredients
        # stay spent; putting them back would be a lie about physical reality.
        # Restoring usable stock is an explicit, actor-attributed RETURNED /
        # MANUAL_ADJUSTMENT movement, never an implicit consequence of an undo.
        pass

    elif new_status == "CANCELLED":
        # Release only what is still merely promised. Anything already consumed
        # stays consumed — see release_order_reservation.
        inventory_service.release_order_reservation(
            db, order, actor_type=actor_type, actor_user_id=actor_user_id
        )

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
    sla_sev = _sla_severity(age_min)
    should_start, urgency_reason = _decision_signals(age_min, new_status, sla_sev)
    # batch_partner_ids unavailable in broadcast context — clients use REST for full dashboard
    hint = _action_hint(order.id, new_status, sla_sev, age_min, [])

    background_tasks.add_task(
        kitchen_ws_manager.broadcast_kitchen_event,
        store_id=order.store_id,
        event="order_status_updated",
        data={
            "order_id": order.id,
            "store_id": order.store_id,
            "from_status": old_status,
            "to_status": new_status,
            "computed_age_minutes": age_min,
            "sla_severity": sla_sev,
            "should_be_started": should_start,
            "urgency_reason": urgency_reason,
            "action_hint": hint,
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
                "message": messages.ORDER_UNDO_EXPIRED,
            },
        )


def _actor_user_id(actor_id: str | None) -> int | None:
    """
    The staff user id behind a kitchen transition, for ledger attribution.

    actor_id is a free-form string on the status-event/audit contract; only a
    numeric staff id can be attributed to an inventory movement's actor FK.
    Anything else (a system or customer actor) attributes to no user.
    """
    if actor_id is None:
        return None
    try:
        return int(actor_id)
    except (TypeError, ValueError):
        return None
