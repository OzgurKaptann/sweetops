"""
Owner Decision Engine — deterministic, explainable signals + action lifecycle.

Five signal categories:
  stock_risk      — velocity-based stockout prediction + revenue loss estimate
  demand_spike    — last-1h order rate vs 23h rolling baseline
  slow_moving     — ingredients with stock but zero deductions in 24h
  sla_risk        — kitchen orders breaching SLA thresholds
  revenue_anomaly — hourly revenue vs same-period baseline

Persistence layer:
  Every signal is upserted into owner_decisions on GET /owner/decisions/.
  Completed/dismissed decisions are suppressed for COOLDOWN_HOURS; after that
  window they reset to pending so the owner sees recurring issues again.

Prioritization:
  decision_score = base_score + urgency_bonus + blocking_bonus
  Ordering: decision_score DESC, then decision_id ASC (deterministic tiebreak).

Lifecycle transitions (via apply_decision_action):
  pending → acknowledged → completed
  pending | acknowledged → dismissed
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.ingredient import Ingredient
from app.models.ingredient_stock import IngredientStock, IngredientStockMovement
from app.models.order import Order
from app.models.owner_decision import OwnerDecision

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
STOCK_RISK_HIGH_HOURS   = 6    # stockout < 6h  → high severity
STOCK_RISK_MEDIUM_HOURS = 12   # stockout < 12h → medium severity
DEMAND_SPIKE_HIGH   = 3.0
DEMAND_SPIKE_MEDIUM = 2.0
DEMAND_SPIKE_LOW    = 1.5
SLA_WARNING_MINUTES  = 7
SLA_CRITICAL_MINUTES = 10
REVENUE_DROP_HIGH    = 0.35    # < 35% of baseline → high
REVENUE_DROP_MEDIUM  = 0.60    # < 60% of baseline → medium
REVENUE_SPIKE_THRESHOLD = 2.5

COOLDOWN_HOURS = 2             # completed/dismissed decisions suppressed for 2h

# Scoring constants
_BASE_SCORE  = {"high": 100, "medium": 50, "low": 20}
_BLOCK_BONUS = 25


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _severity_order(s: str) -> int:
    return {"high": 0, "medium": 1, "low": 2}.get(s, 3)


def _is_blocking(signal_type: str, severity: str, data: dict) -> bool:
    """
    A decision is blocking when the owner must act before the next order
    cycle or revenue is immediately at risk.
    """
    if signal_type == "stock_risk" and severity == "high":
        return True
    if signal_type == "demand_spike" and severity in ("high", "medium"):
        return True
    if signal_type == "sla_risk" and severity == "high":
        return True
    if signal_type == "revenue_anomaly" and severity == "high" and data.get("direction") == "drop":
        return True
    return False


def _urgency_bonus(signal_type: str, data: dict) -> float:
    """
    Type-specific urgency bonus added on top of the severity base score.
    All formulas are capped to prevent one signal from dominating unfairly.
    """
    if signal_type == "stock_risk":
        h = data.get("hours_to_stockout")
        if h is None:
            return 0.0
        if h == 0:
            return 30.0
        if h < STOCK_RISK_HIGH_HOURS:
            return (STOCK_RISK_HIGH_HOURS - h) * 5.0
        if h < STOCK_RISK_MEDIUM_HOURS:
            return (STOCK_RISK_MEDIUM_HOURS - h) * 2.0
        return 0.0

    if signal_type == "demand_spike":
        ratio = data.get("spike_ratio", 0.0)
        return min((ratio - DEMAND_SPIKE_LOW) * 10.0, 25.0)

    if signal_type == "sla_risk":
        critical = data.get("critical_count", 0)
        warning  = data.get("warning_count", 0)
        return min(critical * 5.0 + warning * 2.0, 30.0)

    if signal_type == "revenue_anomaly":
        direction = data.get("direction", "")
        ratio = data.get("ratio", 1.0)
        if direction == "drop":
            return 15.0 if ratio < REVENUE_DROP_HIGH else 5.0
        return 0.0

    return 0.0  # slow_moving


def _decision_score(severity: str, signal_type: str, data: dict, blocking: bool) -> float:
    base    = _BASE_SCORE.get(severity, 20)
    urgency = _urgency_bonus(signal_type, data)
    bonus   = _BLOCK_BONUS if blocking else 0
    return round(base + urgency + bonus, 2)


def _why_now(signal_type: str, severity: str, data: dict) -> str:
    """Concrete, time-anchored reason this decision is surfaced right now."""
    if signal_type == "stock_risk":
        h = data.get("hours_to_stockout")
        if h == 0:
            return f"{data['ingredient_name']} has zero stock. Every incoming order requiring it will fail immediately."
        if h is not None:
            return f"At current consumption rate of {data['velocity_per_hour']:.2f} {data.get('unit','units')}/h, stock runs out in {h:.1f}h."
        return f"{data['ingredient_name']} is at or below reorder level with no recent demand."

    if signal_type == "demand_spike":
        ratio = data.get("spike_ratio", 0.0)
        return f"Order rate is {ratio:.1f}× the 23h rolling average in the last 60 minutes."

    if signal_type == "slow_moving":
        return f"{data['ingredient_name']} has had no order deductions in the last 24h while holding {data['current_stock']} units."

    if signal_type == "sla_risk":
        worst = data.get("worst_age_minutes", 0)
        critical = data.get("critical_count", 0)
        if critical:
            return f"{critical} order(s) have been waiting over {SLA_CRITICAL_MINUTES} min. Worst case: {worst:.1f} min."
        return f"Orders are approaching the {SLA_CRITICAL_MINUTES}-min SLA limit. Longest wait: {worst:.1f} min."

    if signal_type == "revenue_anomaly":
        direction = data.get("direction", "")
        ratio = data.get("ratio", 1.0)
        baseline = data.get("avg_hourly_baseline", 0.0)
        last = data.get("last_1h_revenue", 0.0)
        if direction == "drop":
            pct = round((1 - ratio) * 100)
            return f"Last-hour revenue ₺{last:.0f} is {pct}% below the ₺{baseline:.0f} hourly baseline."
        pct = round((ratio - 1) * 100)
        return f"Last-hour revenue ₺{last:.0f} is {pct}% above the ₺{baseline:.0f} hourly baseline."

    return "Signal generated by automated decision engine."


def _expected_impact(signal_type: str, severity: str, data: dict, blocking: bool) -> str:
    """What happens if the owner takes the recommended action."""
    if signal_type == "stock_risk":
        risk = data.get("revenue_at_risk", 0.0)
        if risk > 0:
            return f"Reordering prevents ~₺{risk:.0f} in lost revenue from unfulfillable orders."
        return "Reordering prevents order failures for this ingredient."

    if signal_type == "demand_spike":
        ratio = data.get("spike_ratio", 0.0)
        if severity == "high":
            return f"Increasing prep capacity during a {ratio:.1f}× spike prevents SLA breaches and customer churn."
        return f"Pre-staging ingredients during this {ratio:.1f}× spike reduces prep time per order."

    if signal_type == "slow_moving":
        capital = data.get("tied_capital", 0.0)
        return f"Running a promotion or adjusting reorder quantity frees ~₺{capital:.0f} in tied-up capital."

    if signal_type == "sla_risk":
        breach_count = data.get("critical_count", 0) + data.get("warning_count", 0)
        return f"Acting now prevents {breach_count} order(s) from breaching SLA and avoids negative customer feedback."

    if signal_type == "revenue_anomaly":
        direction = data.get("direction", "")
        if direction == "drop":
            if severity == "high":
                return "Investigating and resolving the root cause can recover revenue that is currently being lost."
            return "Early investigation prevents a moderate dip from becoming a sustained outage."
        return "Ensuring kitchen capacity during the spike prevents SLA degradation and maximises revenue capture."

    return "Taking the recommended action reduces operational risk."


# ---------------------------------------------------------------------------
# Signal functions
# (Each returns a list of raw signal dicts — no DB interaction here.)
# ---------------------------------------------------------------------------

def _stock_risk_signals(db: Session) -> list[dict]:
    now = _now_utc()
    window_start = now - timedelta(hours=24)

    rows = (
        db.query(Ingredient, IngredientStock)
        .join(IngredientStock, IngredientStock.ingredient_id == Ingredient.id)
        .filter(Ingredient.is_active == True)
        .all()
    )

    movements = (
        db.query(
            IngredientStockMovement.ingredient_id,
            func.sum(IngredientStockMovement.quantity_delta).label("total_delta"),
        )
        .filter(
            IngredientStockMovement.movement_type == "ORDER_DEDUCTION",
            IngredientStockMovement.created_at >= window_start,
        )
        .group_by(IngredientStockMovement.ingredient_id)
        .all()
    )
    velocity_map: dict[int, float] = {
        m.ingredient_id: abs(float(m.total_delta)) / 24.0 for m in movements
    }

    signals: list[dict] = []
    for ing, stock in rows:
        current_qty = float(stock.stock_quantity)
        reorder     = float(stock.reorder_level) if stock.reorder_level else 0.0

        if current_qty > reorder:
            continue

        velocity = velocity_map.get(ing.id, 0.0)
        price    = float(ing.price) if ing.price else 0.0

        if current_qty <= 0:
            severity         = "high"
            hours_to_stockout: float | None = 0.0
            description      = f"{ing.name} has no stock. Cannot fulfill any orders requiring this ingredient."
        elif velocity > 0:
            hours_to_stockout = current_qty / velocity
            if hours_to_stockout < STOCK_RISK_HIGH_HOURS:
                severity = "high"
            elif hours_to_stockout < STOCK_RISK_MEDIUM_HOURS:
                severity = "medium"
            else:
                severity = "low"
            description = (
                f"{ing.name} will run out in {hours_to_stockout:.1f}h at current usage rate "
                f"({velocity:.1f} {ing.unit}/h)."
            )
        else:
            severity          = "low"
            hours_to_stockout = None
            description       = (
                f"{ing.name} is at reorder level ({current_qty} {ing.unit}) "
                f"with no recent demand in the last 24h."
            )

        if hours_to_stockout is not None and velocity > 0:
            hours_until_empty = hours_to_stockout if current_qty > 0 else 0.0
            revenue_at_risk   = round(velocity * hours_until_empty * price, 2)
            impact = f"~₺{revenue_at_risk:.0f} estimated revenue at risk based on last 24h demand."
        else:
            revenue_at_risk = 0.0
            impact = "No recent demand. Monitor for waste or reduce reorder quantity."

        signal_data = {
            "ingredient_id":     ing.id,
            "ingredient_name":   ing.name,
            "unit":              ing.unit,
            "current_stock":     current_qty,
            "reorder_level":     reorder,
            "velocity_per_hour": round(velocity, 3),
            "hours_to_stockout": round(hours_to_stockout, 1) if hours_to_stockout is not None else None,
            "revenue_at_risk":   revenue_at_risk,
        }
        blocking = _is_blocking("stock_risk", severity, signal_data)
        score    = _decision_score(severity, "stock_risk", signal_data, blocking)

        signals.append({
            "id":                    f"stock_risk_{ing.id}",
            "type":                  "stock_risk",
            "severity":              severity,
            "decision_score":        score,
            "blocking_vs_non_blocking": blocking,
            "title":                 f"Stock risk: {ing.name}",
            "description":           description,
            "impact":                impact,
            "recommended_action":    (
                f"Reorder {ing.name} immediately."
                if severity == "high"
                else f"Schedule reorder for {ing.name} soon."
            ),
            "why_now":       _why_now("stock_risk", severity, signal_data),
            "expected_impact": _expected_impact("stock_risk", severity, signal_data, blocking),
            "data": signal_data,
        })

    return signals


def _demand_spike_signals(db: Session) -> list[dict]:
    now           = _now_utc()
    one_hour_ago  = now - timedelta(hours=1)
    window_start  = now - timedelta(hours=24)

    last_1h: int = db.query(func.count(Order.id)).filter(Order.created_at >= one_hour_ago).scalar() or 0
    prev_23h: int = (
        db.query(func.count(Order.id))
        .filter(Order.created_at >= window_start, Order.created_at < one_hour_ago)
        .scalar()
        or 0
    )

    avg_baseline = prev_23h / 23.0 if prev_23h > 0 else 0.0
    if last_1h == 0 or avg_baseline == 0:
        return []

    ratio = last_1h / avg_baseline
    if ratio < DEMAND_SPIKE_LOW:
        return []

    if ratio >= DEMAND_SPIKE_HIGH:
        severity = "high"
    elif ratio >= DEMAND_SPIKE_MEDIUM:
        severity = "medium"
    else:
        severity = "low"

    signal_data = {
        "last_1h_orders":      last_1h,
        "avg_hourly_baseline": round(avg_baseline, 2),
        "spike_ratio":         round(ratio, 2),
    }
    blocking = _is_blocking("demand_spike", severity, signal_data)
    score    = _decision_score(severity, "demand_spike", signal_data, blocking)

    return [{
        "id":                    "demand_spike_current",
        "type":                  "demand_spike",
        "severity":              severity,
        "decision_score":        score,
        "blocking_vs_non_blocking": blocking,
        "title":                 "Demand spike detected",
        "description":           (
            f"{last_1h} orders in the last hour vs average of "
            f"{avg_baseline:.1f} orders/h over the previous 23h. "
            f"That is {ratio:.1f}× the baseline."
        ),
        "impact": (
            f"Kitchen is processing {ratio:.1f}× normal load. "
            f"SLA risk increases significantly during spikes."
        ),
        "recommended_action": (
            "Increase prep capacity immediately. Alert kitchen staff."
            if severity == "high"
            else "Monitor kitchen queue closely and pre-stage common ingredients."
        ),
        "why_now":        _why_now("demand_spike", severity, signal_data),
        "expected_impact": _expected_impact("demand_spike", severity, signal_data, blocking),
        "data": signal_data,
    }]


def _slow_moving_signals(db: Session) -> list[dict]:
    now          = _now_utc()
    window_start = now - timedelta(hours=24)

    active_ids: set[int] = {
        row.ingredient_id
        for row in db.query(IngredientStockMovement.ingredient_id)
        .filter(
            IngredientStockMovement.movement_type == "ORDER_DEDUCTION",
            IngredientStockMovement.created_at >= window_start,
        )
        .distinct()
        .all()
    }

    rows = (
        db.query(Ingredient, IngredientStock)
        .join(IngredientStock, IngredientStock.ingredient_id == Ingredient.id)
        .filter(Ingredient.is_active == True)
        .all()
    )

    signals: list[dict] = []
    for ing, stock in rows:
        current_qty = float(stock.stock_quantity)
        reorder     = float(stock.reorder_level) if stock.reorder_level else 0.0

        if current_qty <= 0:
            continue
        if ing.id in active_ids:
            continue
        if current_qty <= reorder:
            continue

        price        = float(ing.price) if ing.price else 0.0
        tied_capital = round(current_qty * price, 2)

        signal_data = {
            "ingredient_id":   ing.id,
            "ingredient_name": ing.name,
            "current_stock":   current_qty,
            "reorder_level":   reorder,
            "tied_capital":    tied_capital,
            "hours_since_last_use": 24,
        }
        blocking = _is_blocking("slow_moving", "medium", signal_data)
        score    = _decision_score("medium", "slow_moving", signal_data, blocking)

        signals.append({
            "id":                    f"slow_moving_{ing.id}",
            "type":                  "slow_moving",
            "severity":              "medium",
            "decision_score":        score,
            "blocking_vs_non_blocking": blocking,
            "title":                 f"Slow-moving stock: {ing.name}",
            "description":           (
                f"{ing.name} has {current_qty} {ing.unit} in stock "
                f"but has not been used in any order in the last 24h."
            ),
            "impact":             f"~₺{tied_capital:.0f} of capital tied up. Risk of spoilage if perishable.",
            "recommended_action": f"Run a promotion featuring {ing.name} or reduce future reorder quantity.",
            "why_now":        _why_now("slow_moving", "medium", signal_data),
            "expected_impact": _expected_impact("slow_moving", "medium", signal_data, blocking),
            "data": signal_data,
        })

    return signals


def _sla_risk_signals(db: Session) -> list[dict]:
    now = _now_utc()

    active_orders = db.query(Order).filter(Order.status.in_(["NEW", "IN_PREP"])).all()
    if not active_orders:
        return []

    critical_orders: list[tuple[int, float]] = []
    warning_orders:  list[tuple[int, float]] = []

    for order in active_orders:
        created = order.created_at
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        age = (now - created).total_seconds() / 60.0
        if age >= SLA_CRITICAL_MINUTES:
            critical_orders.append((order.id, round(age, 1)))
        elif age >= SLA_WARNING_MINUTES:
            warning_orders.append((order.id, round(age, 1)))

    if not critical_orders and not warning_orders:
        return []

    if critical_orders:
        severity      = "high"
        breach_count  = len(critical_orders)
        worst_age     = max(age for _, age in critical_orders)
        description   = (
            f"{breach_count} order(s) have exceeded the {SLA_CRITICAL_MINUTES}-min SLA. "
            f"Worst case: {worst_age:.1f} min in queue."
        )
        recommended_action = (
            "Call in additional staff or notify customers of delay immediately. "
            "Expedite orders: " + ", ".join(f"#{oid}" for oid, _ in critical_orders[:5]) + "."
        )
    else:
        severity      = "medium"
        breach_count  = len(warning_orders)
        worst_age     = max(age for _, age in warning_orders)
        description   = (
            f"{breach_count} order(s) approaching the {SLA_CRITICAL_MINUTES}-min SLA limit. "
            f"Longest wait: {worst_age:.1f} min."
        )
        recommended_action = "Prioritise pending orders now to avoid SLA breach."

    signal_data = {
        "critical_order_ids": [oid for oid, _ in critical_orders],
        "warning_order_ids":  [oid for oid, _ in warning_orders],
        "critical_count":     len(critical_orders),
        "warning_count":      len(warning_orders),
        "worst_age_minutes":  worst_age,
    }
    blocking = _is_blocking("sla_risk", severity, signal_data)
    score    = _decision_score(severity, "sla_risk", signal_data, blocking)

    return [{
        "id":                    "sla_risk_current",
        "type":                  "sla_risk",
        "severity":              severity,
        "decision_score":        score,
        "blocking_vs_non_blocking": blocking,
        "title":                 "Kitchen SLA risk",
        "description":           description,
        "impact": (
            f"{breach_count} order(s) risk customer dissatisfaction. "
            f"Repeated SLA breaches correlate with churn."
        ),
        "recommended_action":  recommended_action,
        "why_now":        _why_now("sla_risk", severity, signal_data),
        "expected_impact": _expected_impact("sla_risk", severity, signal_data, blocking),
        "data": signal_data,
    }]


def _revenue_anomaly_signals(db: Session) -> list[dict]:
    now           = _now_utc()
    one_hour_ago  = now - timedelta(hours=1)
    window_start  = now - timedelta(hours=24)

    last_1h_revenue: float = float(
        db.query(func.coalesce(func.sum(Order.total_amount), 0))
        .filter(Order.created_at >= one_hour_ago)
        .scalar() or 0
    )
    prev_23h_revenue: float = float(
        db.query(func.coalesce(func.sum(Order.total_amount), 0))
        .filter(Order.created_at >= window_start, Order.created_at < one_hour_ago)
        .scalar() or 0
    )

    avg_baseline = prev_23h_revenue / 23.0 if prev_23h_revenue > 0 else 0.0
    if avg_baseline < 1.0:
        return []

    ratio = last_1h_revenue / avg_baseline
    if REVENUE_DROP_MEDIUM <= ratio <= REVENUE_SPIKE_THRESHOLD:
        return []

    if ratio < REVENUE_DROP_HIGH:
        severity  = "high"
        direction = "drop"
        pct       = round((1 - ratio) * 100)
        description = (
            f"Revenue is ₺{last_1h_revenue:.0f} in the last hour, "
            f"{pct}% below the hourly baseline of ₺{avg_baseline:.0f}."
        )
        impact             = "Significant under-performance. Possible kitchen outage, menu issue, or demand collapse."
        recommended_action = (
            "Investigate immediately: check for menu availability issues, "
            "kitchen incidents, or external factors."
        )
    elif ratio < REVENUE_DROP_MEDIUM:
        severity  = "medium"
        direction = "drop"
        pct       = round((1 - ratio) * 100)
        description = (
            f"Revenue is ₺{last_1h_revenue:.0f} in the last hour, "
            f"{pct}% below the hourly baseline of ₺{avg_baseline:.0f}."
        )
        impact             = "Below-average performance. May reflect slow period or early warning of an issue."
        recommended_action = "Monitor order flow. Check if kitchen is operating normally."
    else:
        severity  = "low"
        direction = "spike"
        pct       = round((ratio - 1) * 100)
        description = (
            f"Revenue is ₺{last_1h_revenue:.0f} in the last hour, "
            f"{pct}% above the hourly baseline of ₺{avg_baseline:.0f}."
        )
        impact             = "Positive revenue spike. Ensure kitchen can sustain throughput."
        recommended_action = "Verify kitchen capacity. Pre-stage popular ingredients."

    signal_data = {
        "last_1h_revenue":     round(last_1h_revenue, 2),
        "avg_hourly_baseline": round(avg_baseline, 2),
        "ratio":               round(ratio, 3),
        "direction":           direction,
    }
    blocking = _is_blocking("revenue_anomaly", severity, signal_data)
    score    = _decision_score(severity, "revenue_anomaly", signal_data, blocking)

    return [{
        "id":                    "revenue_anomaly_current",
        "type":                  "revenue_anomaly",
        "severity":              severity,
        "decision_score":        score,
        "blocking_vs_non_blocking": blocking,
        "title":                 f"Revenue {direction} detected",
        "description":           description,
        "impact":                impact,
        "recommended_action":    recommended_action,
        "why_now":        _why_now("revenue_anomaly", severity, signal_data),
        "expected_impact": _expected_impact("revenue_anomaly", severity, signal_data, blocking),
        "data": signal_data,
    }]


# ---------------------------------------------------------------------------
# Upsert logic
# ---------------------------------------------------------------------------

def _upsert_decision(db: Session, signal: dict, now: datetime) -> OwnerDecision | None:
    """
    Upsert one signal into owner_decisions:
      - INSERT if new
      - UPDATE mutable fields if pending/acknowledged
      - Skip if completed/dismissed within the cooldown window
      - Reset to pending if completed/dismissed and cooldown has expired
    Returns the row to include in the response, or None if suppressed.
    """
    decision_id = signal["id"]
    row: OwnerDecision | None = db.get(OwnerDecision, decision_id)

    if row is None:
        # First time this signal fires → create as pending
        row = OwnerDecision(
            decision_id=decision_id,
            status="pending",
        )
        db.add(row)
        _apply_signal_fields(row, signal)
        db.flush()
        return row

    # Row exists — check if it's in a terminal state within cooldown
    if row.status in ("completed", "dismissed"):
        cooldown_cutoff = now - timedelta(hours=COOLDOWN_HOURS)
        if row.updated_at and row.updated_at.replace(tzinfo=timezone.utc) > cooldown_cutoff:
            # Still within cooldown — suppress this signal
            return None
        # Cooldown expired — reset to pending
        row.status                  = "pending"
        row.acknowledged_at         = None
        row.completed_at            = None
        row.actor_id                = None
        row.resolution_note         = None
        row.resolution_quality      = None
        row.estimated_revenue_saved = None

    # Update mutable signal fields regardless of current status
    _apply_signal_fields(row, signal)
    db.flush()
    return row


def _apply_signal_fields(row: OwnerDecision, signal: dict) -> None:
    """Copy all signal-computed fields onto the ORM row."""
    row.type                   = signal["type"]
    row.severity               = signal["severity"]
    row.decision_score         = signal["decision_score"]
    row.blocking_vs_non_blocking = signal["blocking_vs_non_blocking"]
    row.title                  = signal["title"]
    row.description            = signal["description"]
    row.impact                 = signal["impact"]
    row.recommended_action     = signal["recommended_action"]
    row.why_now                = signal["why_now"]
    row.expected_impact        = signal["expected_impact"]
    row.data                   = signal["data"]


def _row_to_dict(row: OwnerDecision) -> dict:
    return {
        "id":                    row.decision_id,
        "type":                  row.type,
        "severity":              row.severity,
        "decision_score":        row.decision_score,
        "blocking_vs_non_blocking": row.blocking_vs_non_blocking,
        "title":                 row.title,
        "description":           row.description,
        "impact":                row.impact,
        "recommended_action":    row.recommended_action,
        "why_now":               row.why_now,
        "expected_impact":       row.expected_impact,
        "data":                  row.data or {},
        "status":                row.status,
        "acknowledged_at":       row.acknowledged_at.isoformat() if row.acknowledged_at else None,
        "completed_at":          row.completed_at.isoformat() if row.completed_at else None,
        "actor_id":              row.actor_id,
        "resolution_note":       row.resolution_note,
        "resolution_quality":       row.resolution_quality,
        "estimated_revenue_saved":  row.estimated_revenue_saved,
        "created_at":            row.created_at.isoformat() if row.created_at else None,
        "updated_at":            row.updated_at.isoformat() if row.updated_at else None,
    }


# ---------------------------------------------------------------------------
# Public: GET
# ---------------------------------------------------------------------------

def get_owner_decisions(db: Session) -> dict:
    """
    1. Compute all fresh signals.
    2. Upsert into owner_decisions (respecting cooldown).
    3. Sort by decision_score DESC, then decision_id ASC.
    4. Return envelope.
    """
    now = _now_utc()
    all_signals: list[dict] = []

    for fn in (
        _stock_risk_signals,
        _demand_spike_signals,
        _slow_moving_signals,
        _sla_risk_signals,
        _revenue_anomaly_signals,
    ):
        try:
            all_signals.extend(fn(db))
        except Exception as exc:
            logger.error("decision_engine signal_fn=%s err=%s", fn.__name__, exc)

    visible: list[dict] = []
    for signal in all_signals:
        try:
            row = _upsert_decision(db, signal, now)
            if row is not None:
                visible.append(_row_to_dict(row))
        except Exception as exc:
            logger.error("decision_engine upsert decision_id=%s err=%s", signal["id"], exc)

    try:
        db.commit()
    except Exception as exc:
        logger.error("decision_engine commit err=%s", exc)
        db.rollback()

    # Sort: score DESC, then id ASC for deterministic tiebreak
    visible.sort(key=lambda d: (-d["decision_score"], d["id"]))

    summary = {
        "high":   sum(1 for d in visible if d["severity"] == "high"),
        "medium": sum(1 for d in visible if d["severity"] == "medium"),
        "low":    sum(1 for d in visible if d["severity"] == "low"),
    }

    return {
        "decisions":         visible,
        "generated_at":      now.isoformat(),
        "signals_evaluated": 5,
        "active_count":      len(visible),
        "summary":           summary,
    }


# ---------------------------------------------------------------------------
# Public: PATCH — lifecycle transition
# ---------------------------------------------------------------------------

_VALID_TRANSITIONS: dict[str, set[str]] = {
    "acknowledge": {"pending"},
    "complete":    {"pending", "acknowledged"},
    "dismiss":     {"pending", "acknowledged"},
}


def apply_decision_action(
    db: Session,
    decision_id: str,
    action: str,
    actor_id: str | None = None,
    resolution_note: str | None = None,
    resolution_quality: str | None = None,
    estimated_revenue_saved: float | None = None,
) -> dict:
    """
    Transition a decision to a new lifecycle status.
    Returns the updated decision dict.
    Raises ValueError on invalid transition, LookupError if not found.
    """
    row: OwnerDecision | None = db.get(OwnerDecision, decision_id)
    if row is None:
        raise LookupError(f"Decision '{decision_id}' not found.")

    allowed = _VALID_TRANSITIONS.get(action)
    if allowed is None:
        raise ValueError(f"Unknown action '{action}'. Valid: acknowledge, complete, dismiss.")

    if row.status not in allowed:
        raise ValueError(
            f"Cannot '{action}' a decision in status '{row.status}'. "
            f"Allowed from: {sorted(allowed)}."
        )

    now = _now_utc()

    if action == "acknowledge":
        row.status          = "acknowledged"
        row.acknowledged_at = now
        row.actor_id        = actor_id

    elif action == "complete":
        row.status       = "completed"
        row.completed_at = now
        row.actor_id     = actor_id
        if resolution_note is not None:
            row.resolution_note = resolution_note
        if resolution_quality is not None:
            row.resolution_quality = resolution_quality
        if estimated_revenue_saved is not None:
            row.estimated_revenue_saved = estimated_revenue_saved

    elif action == "dismiss":
        row.status       = "dismissed"
        row.completed_at = now
        row.actor_id     = actor_id
        if resolution_note is not None:
            row.resolution_note = resolution_note

    db.commit()
    db.refresh(row)
    return _row_to_dict(row)
