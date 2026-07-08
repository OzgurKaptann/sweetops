"""
Operational Context Service — operational_context_service.py

Computes the current operational mode from today's metrics.
This is the bridge between the measurement layer and behavior adaptation.

Mode hierarchy (only the HIGHEST-priority mode is active):
  sla_critical      — sla_breach_rate > 35%
  high_kitchen_load — sla_breach_rate > 20% OR avg_prep_time > 9min
  boost_combos      — combo_usage_rate < 30% OR upsell_acceptance_rate < 15%
  normal            — no thresholds breached

Downstream consumers:
  1. decision_engine._metric_driven_signals() — generates decisions FROM this context
  2. menu_service.get_menu()                  — adjusts combo ranking weight
  3. public_menu /upsell                      — adjusts suggestion count + focus
  4. GET /owner/operational-context           — exposes mode to dashboard

Rules:
  - Only apply an adaptation when DataQuality.status == "valid" (≥ min_sample).
  - low_sample / no_data / unreliable metrics never trigger mode changes.
  - All thresholds are deterministic constants — no randomness, no A/B flags.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Optional

from sqlalchemy.orm import Session

from app.services.metrics_service import fetch_daily_metrics
from app.schemas.metrics import DailyMetricsResponse

# ── Thresholds ────────────────────────────────────────────────────────────────
# Changes here propagate to all consumers.

COMBO_RATE_BOOST_THRESHOLD       = 0.30   # combo_usage_rate < 30% → boost_combos
UPSELL_RATE_BOOST_THRESHOLD      = 0.15   # upsell_acceptance_rate < 15% → boost_combos
SLA_BREACH_HIGH_LOAD_THRESHOLD   = 0.20   # sla_breach_rate > 20% → high_kitchen_load
SLA_BREACH_CRITICAL_THRESHOLD    = 0.35   # sla_breach_rate > 35% → sla_critical
AVG_PREP_HIGH_LOAD_THRESHOLD     = 9.0    # avg_prep_time > 9 min → high_kitchen_load

# Adaptation parameters (consumed by conversion_engine)
COMBO_BOOST_MULTIPLIER           = 1.6    # ranking_score multiplier for popular-combo items
NORMAL_MAX_UPSELL_SUGGESTIONS    = 3      # default from conversion_engine.MAX_UPSELL_SUGGESTIONS
HIGH_LOAD_MAX_UPSELL_SUGGESTIONS = 1      # reduce complexity during kitchen stress


@dataclass
class OperationalContext:
    """
    The current operational mode and all adaptation parameters.

    mode:
      "normal"            — system operating within expected parameters
      "boost_combos"      — combo/upsell metrics below threshold; increase combo visibility
      "high_kitchen_load" — kitchen throughput degraded; reduce order complexity
      "sla_critical"      — severe kitchen degradation; maximum simplification

    combo_boost:
      Multiplier applied to the ranking_score popularity component for ingredients
      that are part of known combos. 1.0 = no boost. Applied in conversion_engine.

    max_upsell_suggestions:
      Maximum number of upsell suggestions to return at /public/menu/upsell.
      Reduced during kitchen load to minimize order complexity.

    reasons:
      Ordered list of human-readable strings explaining why this mode is active.
      Used in the API response and decision why_now text.
    """
    mode: str = "normal"
    combo_boost: float = 1.0
    max_upsell_suggestions: int = NORMAL_MAX_UPSELL_SUGGESTIONS
    reasons: list[str] = field(default_factory=list)
    computed_at: str = ""
    metrics_date: str = ""

    # Raw metric values at time of computation (for decision data payload)
    combo_usage_rate: Optional[float] = None
    upsell_acceptance_rate: Optional[float] = None
    sla_breach_rate: Optional[float] = None
    avg_prep_time: Optional[float] = None
    completion_rate: Optional[float] = None
    decisions_seen: int = 0
    decisions_completed: int = 0


def _is_valid(quality_status: str) -> bool:
    """Only trust metrics with sufficient sample size."""
    return quality_status == "valid"


def compute_operational_context(
    db: Session,
    target_date: Optional[date] = None,
) -> OperationalContext:
    """
    Compute today's operational context from the measurement layer.

    Always returns a complete OperationalContext.
    If metrics fail, returns mode="normal" (safe default — no adaptation applied).
    """
    ctx = OperationalContext(
        computed_at=datetime.now(timezone.utc).isoformat(),
        metrics_date=str(target_date or datetime.now(timezone.utc).date()),
    )

    try:
        metrics: DailyMetricsResponse = fetch_daily_metrics(db, target_date)
    except Exception:
        # Metrics failure → safe default
        ctx.reasons.append("Metrics unavailable; operating in normal mode.")
        return ctx

    conv    = metrics.conversion
    kitchen = metrics.kitchen
    dec     = metrics.decisions

    # Extract values (only from valid-quality metrics)
    combo_rate   = conv.combo_usage_rate.value   if _is_valid(conv.combo_usage_rate.quality.status)   else None
    upsell_rate  = conv.upsell_acceptance_rate.value if _is_valid(conv.upsell_acceptance_rate.quality.status) else None
    sla_breach   = kitchen.sla_breach_rate.value if _is_valid(kitchen.sla_breach_rate.quality.status) else None
    avg_prep     = kitchen.avg_prep_time_minutes.value if _is_valid(kitchen.avg_prep_time_minutes.quality.status) else None
    comp_rate    = dec.completion_rate.value     if _is_valid(dec.completion_rate.quality.status)     else None

    ctx.combo_usage_rate       = combo_rate
    ctx.upsell_acceptance_rate = upsell_rate
    ctx.sla_breach_rate        = sla_breach
    ctx.avg_prep_time          = avg_prep
    ctx.completion_rate        = comp_rate
    ctx.decisions_seen         = dec.decisions_seen
    ctx.decisions_completed    = dec.decisions_completed

    # ── Evaluate mode (highest priority wins) ─────────────────────────────

    # sla_critical
    if sla_breach is not None and sla_breach > SLA_BREACH_CRITICAL_THRESHOLD:
        ctx.mode = "sla_critical"
        ctx.reasons.append(
            f"SLA breach rate is {sla_breach * 100:.0f}% (threshold: "
            f"{SLA_BREACH_CRITICAL_THRESHOLD * 100:.0f}%). "
            "Kitchen is severely overloaded."
        )
        ctx.combo_boost = 1.0               # no combo push during crisis
        ctx.max_upsell_suggestions = HIGH_LOAD_MAX_UPSELL_SUGGESTIONS
        return ctx

    # high_kitchen_load
    if (
        (sla_breach is not None and sla_breach > SLA_BREACH_HIGH_LOAD_THRESHOLD)
        or (avg_prep is not None and avg_prep > AVG_PREP_HIGH_LOAD_THRESHOLD)
    ):
        ctx.mode = "high_kitchen_load"
        if sla_breach is not None and sla_breach > SLA_BREACH_HIGH_LOAD_THRESHOLD:
            ctx.reasons.append(
                f"SLA breach rate is {sla_breach * 100:.0f}% "
                f"(threshold: {SLA_BREACH_HIGH_LOAD_THRESHOLD * 100:.0f}%)."
            )
        if avg_prep is not None and avg_prep > AVG_PREP_HIGH_LOAD_THRESHOLD:
            ctx.reasons.append(
                f"Average prep time is {avg_prep:.1f} min "
                f"(threshold: {AVG_PREP_HIGH_LOAD_THRESHOLD:.0f} min)."
            )
        ctx.combo_boost = 1.0               # no combo push during high load
        ctx.max_upsell_suggestions = HIGH_LOAD_MAX_UPSELL_SUGGESTIONS
        return ctx

    # boost_combos
    triggered_by_combo = combo_rate is not None and combo_rate < COMBO_RATE_BOOST_THRESHOLD
    triggered_by_upsell = upsell_rate is not None and upsell_rate < UPSELL_RATE_BOOST_THRESHOLD

    if triggered_by_combo or triggered_by_upsell:
        ctx.mode = "boost_combos"
        if triggered_by_combo:
            ctx.reasons.append(
                f"Combo usage rate is {combo_rate * 100:.0f}% "  # type: ignore[operator]
                f"(threshold: {COMBO_RATE_BOOST_THRESHOLD * 100:.0f}%). "
                "Increasing combo ingredient visibility in menu ranking."
            )
        if triggered_by_upsell:
            ctx.reasons.append(
                f"Upsell acceptance rate is {upsell_rate * 100:.0f}% "  # type: ignore[operator]
                f"(threshold: {UPSELL_RATE_BOOST_THRESHOLD * 100:.0f}%). "
                "Combo suggestions will be prioritized."
            )
        ctx.combo_boost = COMBO_BOOST_MULTIPLIER
        ctx.max_upsell_suggestions = NORMAL_MAX_UPSELL_SUGGESTIONS
        return ctx

    # normal
    ctx.reasons.append("All metrics within expected thresholds.")
    return ctx


def context_to_dict(ctx: OperationalContext) -> dict:
    """Serialise OperationalContext for the API response."""
    return {
        "mode": ctx.mode,
        "reasons": ctx.reasons,
        "combo_boost": ctx.combo_boost,
        "max_upsell_suggestions": ctx.max_upsell_suggestions,
        "computed_at": ctx.computed_at,
        "metrics_date": ctx.metrics_date,
        "metric_values": {
            "combo_usage_rate":       ctx.combo_usage_rate,
            "upsell_acceptance_rate": ctx.upsell_acceptance_rate,
            "sla_breach_rate":        ctx.sla_breach_rate,
            "avg_prep_time_minutes":  ctx.avg_prep_time,
            "completion_rate":        ctx.completion_rate,
            "decisions_seen":         ctx.decisions_seen,
            "decisions_completed":    ctx.decisions_completed,
        },
        "thresholds": {
            "combo_rate_boost":       COMBO_RATE_BOOST_THRESHOLD,
            "upsell_rate_boost":      UPSELL_RATE_BOOST_THRESHOLD,
            "sla_breach_high_load":   SLA_BREACH_HIGH_LOAD_THRESHOLD,
            "sla_breach_critical":    SLA_BREACH_CRITICAL_THRESHOLD,
            "avg_prep_high_load_min": AVG_PREP_HIGH_LOAD_THRESHOLD,
        },
    }
