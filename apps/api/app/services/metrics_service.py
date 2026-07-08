"""
Measurement Layer — metrics_service.py  (production-hardened)

Design principles
-----------------
1. NEVER divide by zero — every denominator is guarded with NULLIF or a Python check.
2. NEVER return misleading values — every rate/average carries a DataQuality block.
3. Minimum sample sizes enforce meaningful metrics:
      CONVERSION_MIN_ORDERS  = 5   (order-level conversion metrics)
      KITCHEN_MIN_ORDERS     = 3   (avg, breach rate)
      P90_MIN_ORDERS         = 10  (PERCENTILE_CONT unreliable below this)
4. pct_change is bounded to [-300, +300]% and snapped to "flat" when |change| < 1%.
5. All errors during a group computation are caught, logged, and returned in
   meta.errors.  A partial result is ALWAYS returned — never a bare 500.
6. Wall-clock timing is measured for the full computation (meta.computation_ms).
7. Revenue saved is only credited for resolution_quality IN ('good','partial').
   Failed / unattributed completions contribute 0.

Comparison model
----------------
Primary comparison: target_date - 1 day  (always applied)
Design provision:   same weekday last week could be added by passing
                    comparison_date=target_date - 7 days — no schema change needed.
"""
from __future__ import annotations

import logging
import time
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from sqlalchemy.orm import Session
from sqlalchemy import text

from app.schemas.metrics import (
    DataQuality,
    TrendValue,
    ConversionMetrics,
    DecisionMetrics,
    KitchenMetrics,
    RevenueProtectionMetrics,
    ActualOutcome,
    MetricsObservability,
    DailyMetricsResponse,
)

logger = logging.getLogger(__name__)

# ── Minimum sample thresholds ─────────────────────────────────────────────────
# These are the same values referenced in metric_definitions.py.
# Change here only — metric_definitions.py reads from constants, not hardcoded.
CONVERSION_MIN_ORDERS = 5
KITCHEN_MIN_ORDERS    = 3
P90_MIN_ORDERS        = 10
SLA_THRESHOLD_MINUTES = 10     # mirrors decision_engine.py 'sla_risk' critical

# ── Trend bounds ──────────────────────────────────────────────────────────────
PCT_CAP        = 300.0   # cap extreme pct_change to suppress low-traffic noise
FLAT_THRESHOLD = 1.0     # |pct_change| < 1% → "flat" (not a real signal)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _quality(
    sample: int,
    minimum: int,
    valid_value: float,
    *,
    sanity_checks: list[tuple[bool, str]] | None = None,
) -> DataQuality:
    """
    Build a DataQuality block.

    sanity_checks: list of (condition_that_must_be_true, error_message).
    If any check fails, status = 'unreliable'.
    """
    # Sanity checks first (override everything)
    if sanity_checks:
        for condition, msg in sanity_checks:
            if not condition:
                return DataQuality(
                    status="unreliable",
                    sample_size=sample,
                    min_required=minimum,
                    message=msg,
                )

    if sample == 0:
        return DataQuality(
            status="no_data",
            sample_size=0,
            min_required=minimum,
            message="No data for this date.",
        )
    if sample < minimum:
        return DataQuality(
            status="low_sample",
            sample_size=sample,
            min_required=minimum,
            message=f"Only {sample} sample(s); {minimum} required for a reliable metric.",
        )
    return DataQuality(status="valid", sample_size=sample, min_required=minimum)


def _trend(
    value: float,
    sample: int,
    minimum: int,
    prev: Optional[float],
    prev_sample: int,
    *,
    sanity_checks: list[tuple[bool, str]] | None = None,
) -> TrendValue:
    """
    Build a TrendValue with bounded pct_change and DataQuality.

    prev is ignored if prev_sample == 0 (no comparison data available).
    """
    qual = _quality(sample, minimum, value, sanity_checks=sanity_checks)

    if prev is None or prev_sample == 0:
        return TrendValue(value=round(value, 4), quality=qual)

    if prev == 0:
        # Cannot compute meaningful pct_change; value exists but comparison is zero-base
        direction = "up" if value > 0 else "flat"
        return TrendValue(
            value=round(value, 4),
            prev_value=round(prev, 4),
            trend=direction,
            pct_change=None,
            quality=qual,
        )

    raw_pct = (value - prev) / abs(prev) * 100
    bounded = max(-PCT_CAP, min(PCT_CAP, raw_pct))
    bounded = round(bounded, 1)
    trend = "flat" if abs(bounded) < FLAT_THRESHOLD else ("up" if bounded > 0 else "down")

    return TrendValue(
        value=round(value, 4),
        prev_value=round(prev, 4),
        trend=trend,
        pct_change=bounded,
        quality=qual,
    )


# ── Conversion ────────────────────────────────────────────────────────────────

_CONVERSION_SQL = text("""
WITH item_ing_counts AS (
    -- count distinct ingredients per order_item for the target date
    SELECT
        oi.order_id,
        oii.order_item_id,
        COUNT(DISTINCT oii.ingredient_id) AS ing_count
    FROM order_item_ingredients oii
    JOIN order_items oi ON oi.id = oii.order_item_id
    JOIN orders o        ON o.id  = oi.order_id
    WHERE o.created_at::date = :target_date
      AND o.status <> 'CANCELLED'
    GROUP BY oi.order_id, oii.order_item_id
),
order_combo_flag AS (
    -- order is "combo" when any of its items has >= 2 distinct ingredients
    SELECT order_id, BOOL_OR(ing_count >= 2) AS is_combo
    FROM item_ing_counts
    GROUP BY order_id
),
order_totals AS (
    SELECT
        o.id                                              AS order_id,
        CAST(o.total_amount AS FLOAT)                    AS total_amount,
        COALESCE(cf.is_combo, FALSE)                     AS is_combo
    FROM orders o
    LEFT JOIN order_combo_flag cf ON cf.order_id = o.id
    WHERE o.created_at::date = :target_date
      AND o.status <> 'CANCELLED'
),
item_level AS (
    -- item-level for upsell_acceptance_rate
    SELECT
        COUNT(*)                               AS total_items,
        COUNT(*) FILTER (WHERE ing_count >= 2) AS combo_items
    FROM item_ing_counts
)
SELECT
    -- order-level metrics
    COUNT(*)                                           AS total_orders,
    COUNT(*) FILTER (WHERE is_combo)                   AS combo_orders,
    AVG(total_amount) FILTER (WHERE is_combo)          AS aov_combo,
    AVG(total_amount) FILTER (WHERE NOT is_combo)      AS aov_no_combo,
    -- item-level metrics
    (SELECT total_items FROM item_level)               AS total_items,
    (SELECT combo_items FROM item_level)               AS combo_items
FROM order_totals
""")


def _fetch_conversion_raw(db: Session, target_date: date) -> dict:
    try:
        row = db.execute(_CONVERSION_SQL, {"target_date": str(target_date)}).fetchone()
    except Exception:
        logger.exception("conversion query failed for %s", target_date)
        return _empty_conversion()

    if not row or row[0] is None:
        return _empty_conversion()

    return {
        "total_orders": int(row[0]),
        "combo_orders": int(row[1]) if row[1] else 0,
        "aov_combo":    float(row[2]) if row[2] is not None else 0.0,
        "aov_no_combo": float(row[3]) if row[3] is not None else 0.0,
        "total_items":  int(row[4]) if row[4] else 0,
        "combo_items":  int(row[5]) if row[5] else 0,
        "ok": True,
    }


def _empty_conversion() -> dict:
    return {
        "total_orders": 0, "combo_orders": 0,
        "aov_combo": 0.0,  "aov_no_combo": 0.0,
        "total_items": 0,  "combo_items": 0,
        "ok": False,
    }


def _compute_conversion(
    db: Session,
    target: date,
    prev: date,
    errors: list[str],
) -> ConversionMetrics:
    cur = _fetch_conversion_raw(db, target)
    prv = _fetch_conversion_raw(db, prev)

    if not cur["ok"]:
        errors.append(f"conversion: query failed for {target}")

    def rate(orders: int, combos: int) -> float:
        return combos / orders if orders > 0 else 0.0

    def upsell(items: int, combo_items: int) -> float:
        return combo_items / items if items > 0 else 0.0

    combo_rate_cur = rate(cur["total_orders"], cur["combo_orders"])
    combo_rate_prv = rate(prv["total_orders"], prv["combo_orders"])

    upsell_cur = upsell(cur["total_items"], cur["combo_items"])
    upsell_prv = upsell(prv["total_items"], prv["combo_items"])

    # Combo orders needed for AOV metrics (may differ from total_orders)
    cur_combo_n   = cur["combo_orders"]
    prv_combo_n   = prv["combo_orders"]
    cur_no_combo_n = cur["total_orders"] - cur["combo_orders"]
    prv_no_combo_n = prv["total_orders"] - prv["combo_orders"]

    return ConversionMetrics(
        combo_usage_rate=_trend(
            combo_rate_cur, cur["total_orders"], CONVERSION_MIN_ORDERS,
            combo_rate_prv, prv["total_orders"],
        ),
        avg_order_value_with_combo=_trend(
            cur["aov_combo"], cur_combo_n, CONVERSION_MIN_ORDERS,
            cur["aov_combo"] if cur_combo_n > 0 else None,
            prv_combo_n,
            sanity_checks=[(cur["aov_combo"] >= 0, "AOV cannot be negative")],
        ),
        avg_order_value_without_combo=_trend(
            cur["aov_no_combo"], cur_no_combo_n, CONVERSION_MIN_ORDERS,
            prv["aov_no_combo"] if prv_no_combo_n > 0 else None,
            prv_no_combo_n,
            sanity_checks=[(cur["aov_no_combo"] >= 0, "AOV cannot be negative")],
        ),
        upsell_acceptance_rate=_trend(
            upsell_cur, cur["total_items"], CONVERSION_MIN_ORDERS,
            upsell_prv, prv["total_items"],
        ),
    )


# ── Decisions ─────────────────────────────────────────────────────────────────

_DECISION_SQL = text("""
SELECT
    COUNT(*) FILTER (
        WHERE acknowledged_at::date = :target_date
    )                                             AS acknowledged,
    COUNT(*) FILTER (
        WHERE completed_at::date = :target_date
    )                                             AS completed,
    COUNT(*) FILTER (
        WHERE status = 'dismissed'
          AND updated_at::date = :target_date
    )                                             AS dismissed
FROM owner_decisions
WHERE acknowledged_at::date    = :target_date
   OR completed_at::date        = :target_date
   OR (status = 'dismissed' AND updated_at::date = :target_date)
""")


def _fetch_decision_raw(db: Session, target_date: date) -> dict:
    try:
        row = db.execute(_DECISION_SQL, {"target_date": str(target_date)}).fetchone()
    except Exception:
        logger.exception("decision query failed for %s", target_date)
        return {"acknowledged": 0, "completed": 0, "dismissed": 0, "ok": False}

    ack       = int(row[0]) if row and row[0] else 0
    completed = int(row[1]) if row and row[1] else 0
    dismissed = int(row[2]) if row and row[2] else 0
    return {"acknowledged": ack, "completed": completed, "dismissed": dismissed, "ok": True}


def _compute_decisions(
    db: Session,
    target: date,
    prev: date,
    errors: list[str],
) -> DecisionMetrics:
    cur = _fetch_decision_raw(db, target)
    prv = _fetch_decision_raw(db, prev)

    if not cur["ok"]:
        errors.append(f"decisions: query failed for {target}")

    cur_seen = cur["acknowledged"] + cur["completed"] + cur["dismissed"]
    prv_seen = prv["acknowledged"] + prv["completed"] + prv["dismissed"]

    def cr(seen: int, completed: int) -> float:
        return completed / seen if seen > 0 else 0.0

    # Sanity: completion_rate must be 0–1
    cur_cr = cr(cur_seen, cur["completed"])
    sanity = [(0.0 <= cur_cr <= 1.0, f"completion_rate {cur_cr} is outside [0,1]")]

    return DecisionMetrics(
        decisions_seen=cur_seen,
        decisions_acknowledged=cur["acknowledged"],
        decisions_completed=cur["completed"],
        completion_rate=_trend(
            cur_cr,
            cur_seen,          # sample = decisions seen
            1,                 # minimum 1 seen before rate is meaningful
            cr(prv_seen, prv["completed"]),
            prv_seen,
            sanity_checks=sanity,
        ),
    )


# ── Kitchen ───────────────────────────────────────────────────────────────────

_KITCHEN_SQL = text("""
WITH ready_events AS (
    -- first READY event per order
    SELECT order_id, MIN(created_at) AS ready_at
    FROM order_status_events
    WHERE status_to = 'READY'
    GROUP BY order_id
),
prep_times AS (
    SELECT
        GREATEST(
            EXTRACT(EPOCH FROM (re.ready_at - o.created_at)) / 60.0,
            0
        ) AS prep_minutes
    FROM orders o
    JOIN ready_events re ON re.order_id = o.id
    WHERE o.created_at::date = :target_date
      AND o.status NOT IN ('NEW', 'IN_PREP', 'CANCELLED')
)
SELECT
    COUNT(*)                                                          AS n,
    COALESCE(AVG(prep_minutes), 0)                                   AS avg_prep,
    COALESCE(
        PERCENTILE_CONT(0.9) WITHIN GROUP (ORDER BY prep_minutes),
        0
    )                                                                 AS p90_prep,
    COALESCE(
        COUNT(*) FILTER (WHERE prep_minutes > :sla_threshold)
            * 1.0 / NULLIF(COUNT(*), 0),
        0
    )                                                                 AS breach_rate
FROM prep_times
""")


def _fetch_kitchen_raw(db: Session, target_date: date) -> dict:
    try:
        row = db.execute(_KITCHEN_SQL, {
            "target_date": str(target_date),
            "sla_threshold": SLA_THRESHOLD_MINUTES,
        }).fetchone()
    except Exception:
        logger.exception("kitchen query failed for %s", target_date)
        return {"n": 0, "avg": 0.0, "p90": 0.0, "breach": 0.0, "ok": False}

    return {
        "n":      int(row[0])   if row and row[0] is not None else 0,
        "avg":    float(row[1]) if row and row[1] is not None else 0.0,
        "p90":    float(row[2]) if row and row[2] is not None else 0.0,
        "breach": float(row[3]) if row and row[3] is not None else 0.0,
        "ok": True,
    }


def _compute_kitchen(
    db: Session,
    target: date,
    prev: date,
    errors: list[str],
) -> KitchenMetrics:
    cur = _fetch_kitchen_raw(db, target)
    prv = _fetch_kitchen_raw(db, prev)

    if not cur["ok"]:
        errors.append(f"kitchen: query failed for {target}")

    prv_n = prv["n"]

    return KitchenMetrics(
        avg_prep_time_minutes=_trend(
            cur["avg"],    cur["n"], KITCHEN_MIN_ORDERS,
            prv["avg"],    prv_n,
            sanity_checks=[(cur["avg"] >= 0, "Prep time cannot be negative")],
        ),
        p90_prep_time_minutes=_trend(
            cur["p90"],    cur["n"], P90_MIN_ORDERS,
            prv["p90"],    prv_n,
            sanity_checks=[(cur["p90"] >= 0, "P90 prep time cannot be negative")],
        ),
        sla_breach_rate=_trend(
            cur["breach"], cur["n"], KITCHEN_MIN_ORDERS,
            prv["breach"], prv_n,
            sanity_checks=[(0.0 <= cur["breach"] <= 1.0,
                            f"breach_rate {cur['breach']} outside [0,1]")],
        ),
    )


# ── Revenue Protection ────────────────────────────────────────────────────────

_REVPROT_SQL = text("""
SELECT
    -- triggered today (new signals)
    COUNT(*) FILTER (
        WHERE created_at::date = :target_date
    )                                                      AS triggered,

    -- resolved today (completed, regardless of when triggered)
    COUNT(*) FILTER (
        WHERE status = 'completed'
          AND completed_at::date = :target_date
    )                                                      AS resolved,

    -- saved: only good + partial — failed = 0 contribution
    COALESCE(SUM(estimated_revenue_saved) FILTER (
        WHERE status = 'completed'
          AND completed_at::date = :target_date
          AND resolution_quality IN ('good', 'partial')
    ), 0)                                                  AS saved,

    -- outcome breakdown (deterministic, mutually exclusive)
    COUNT(*) FILTER (
        WHERE status = 'completed'
          AND completed_at::date = :target_date
          AND resolution_quality = 'good'
    )                                                      AS outcome_good,

    COUNT(*) FILTER (
        WHERE status = 'completed'
          AND completed_at::date = :target_date
          AND resolution_quality = 'partial'
    )                                                      AS outcome_partial,

    -- failed = explicit 'failed' OR completed without attribution (conservative)
    COUNT(*) FILTER (
        WHERE status = 'completed'
          AND completed_at::date = :target_date
          AND (resolution_quality = 'failed' OR resolution_quality IS NULL)
    )                                                      AS outcome_failed

FROM owner_decisions
WHERE type = 'stock_risk'
  AND (
      created_at::date = :target_date
      OR (status = 'completed' AND completed_at::date = :target_date)
  )
""")


def _compute_revenue_protection(
    db: Session,
    target: date,
    errors: list[str],
) -> RevenueProtectionMetrics:
    try:
        row = db.execute(_REVPROT_SQL, {"target_date": str(target)}).fetchone()
    except Exception:
        logger.exception("revenue_protection query failed for %s", target)
        errors.append(f"revenue_protection: query failed for {target}")
        return RevenueProtectionMetrics(
            stock_risk_triggered=0,
            stock_risk_resolved=0,
            estimated_revenue_saved=0.0,
            actual_outcome=ActualOutcome(),
        )

    if not row:
        return RevenueProtectionMetrics(
            stock_risk_triggered=0,
            stock_risk_resolved=0,
            estimated_revenue_saved=0.0,
            actual_outcome=ActualOutcome(),
        )

    saved = float(row[2]) if row[2] else 0.0

    # Sanity: saved cannot be negative
    if saved < 0:
        errors.append(f"revenue_protection: estimated_revenue_saved={saved} is negative; clamped to 0")
        saved = 0.0

    return RevenueProtectionMetrics(
        stock_risk_triggered=int(row[0]) if row[0] else 0,
        stock_risk_resolved=int(row[1]) if row[1] else 0,
        estimated_revenue_saved=round(saved, 2),
        actual_outcome=ActualOutcome(
            good=int(row[3]) if row[3] else 0,
            partial=int(row[4]) if row[4] else 0,
            failed=int(row[5]) if row[5] else 0,
        ),
    )


# ── Consistency validation ────────────────────────────────────────────────────

def _run_consistency_checks(
    response: DailyMetricsResponse,
    errors: list[str],
) -> None:
    """
    Cross-group sanity checks.  Appends to errors list but does not raise.
    These catch systematic data issues that individual group checks cannot see.
    """
    c = response.conversion

    # 1. combo_usage_rate must be in [0, 1]
    if not (0.0 <= c.combo_usage_rate.value <= 1.0):
        errors.append(
            f"validation: combo_usage_rate={c.combo_usage_rate.value} outside [0,1]"
        )

    # 2. upsell_acceptance_rate must be in [0, 1]
    if not (0.0 <= c.upsell_acceptance_rate.value <= 1.0):
        errors.append(
            f"validation: upsell_acceptance_rate={c.upsell_acceptance_rate.value} outside [0,1]"
        )

    # 3. If combo_usage_rate > 0, aov_with_combo should be >= aov_without
    #    (not enforced strictly — unusual days can break this)
    if (
        c.combo_usage_rate.value > 0
        and c.avg_order_value_with_combo.quality.status == "valid"
        and c.avg_order_value_without_combo.quality.status == "valid"
        and c.avg_order_value_with_combo.value < c.avg_order_value_without_combo.value * 0.5
    ):
        errors.append(
            "validation: avg_order_value_with_combo is less than half of without_combo — "
            "check ingredient pricing data"
        )

    # 4. Kitchen: p90 should be >= avg (always true for a right-skewed distribution)
    k = response.kitchen
    if (
        k.avg_prep_time_minutes.quality.status == "valid"
        and k.p90_prep_time_minutes.quality.status == "valid"
        and k.p90_prep_time_minutes.value < k.avg_prep_time_minutes.value
    ):
        errors.append(
            "validation: p90_prep_time < avg_prep_time — data inconsistency in kitchen metrics"
        )

    # 5. Revenue: outcome total should equal stock_risk_resolved
    rp = response.revenue_protection
    outcome_total = (
        rp.actual_outcome.good
        + rp.actual_outcome.partial
        + rp.actual_outcome.failed
    )
    if outcome_total != rp.stock_risk_resolved:
        errors.append(
            f"validation: outcome total ({outcome_total}) != stock_risk_resolved "
            f"({rp.stock_risk_resolved}) — query logic inconsistency"
        )


# ── Public entry point ────────────────────────────────────────────────────────

def fetch_daily_metrics(
    db: Session,
    target_date: Optional[date] = None,
) -> DailyMetricsResponse:
    """
    Return all four metric groups for target_date (defaults to today UTC).

    Always returns a complete DailyMetricsResponse.
    Non-fatal errors are captured in meta.errors — never raises for data issues.
    """
    if target_date is None:
        target_date = datetime.now(timezone.utc).date()

    prev_date = target_date - timedelta(days=1)
    errors: list[str] = []
    t_start = time.monotonic()

    conversion        = _compute_conversion(db, target_date, prev_date, errors)
    decisions         = _compute_decisions(db, target_date, prev_date, errors)
    kitchen           = _compute_kitchen(db, target_date, prev_date, errors)
    revenue_protection = _compute_revenue_protection(db, target_date, errors)

    computed_at = _now_utc()
    computation_ms = int((time.monotonic() - t_start) * 1000)

    response = DailyMetricsResponse(
        date=str(target_date),
        as_of=computed_at,
        conversion=conversion,
        decisions=decisions,
        kitchen=kitchen,
        revenue_protection=revenue_protection,
        meta=MetricsObservability(
            computed_at=computed_at,
            computation_ms=computation_ms,
            target_date=str(target_date),
            comparison_date=str(prev_date),
            errors=errors,
        ),
    )

    # Cross-group consistency checks (append to errors, do not mutate metric values)
    _run_consistency_checks(response, errors)

    # Update meta.errors after consistency checks
    response.meta.errors = errors
    if errors:
        logger.warning("metrics for %s: %d validation issue(s): %s", target_date, len(errors), errors)

    return response
