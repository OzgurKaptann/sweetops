"""
Measurement Layer — Schemas

Key design decisions:
  - Every rate/average metric carries a DataQuality block so callers
    always know whether to trust the value.
  - TrendValue.pct_change is bounded at ±300% to suppress extreme noise
    from very-low-traffic days (e.g. 1 order → 6 orders = 500%; capped).
  - trend is "flat" when |pct_change| < 1% — avoids phantom signals.
  - MetricsObservability is always present so ops teams can verify freshness.
  - Raw counts (decisions_seen, stock_risk_triggered, etc.) do NOT use
    TrendValue; they are always valid — zero is a real answer.
"""
from __future__ import annotations

from typing import List, Optional
from pydantic import BaseModel


# ── Data quality ──────────────────────────────────────────────────────────────

class DataQuality(BaseModel):
    """
    Attached to every rate / average metric.

    status values:
      "valid"       — sample ≥ min_required, all sanity checks passed
      "low_sample"  — sample is present but below min_required;
                      value shown but flagged with a warning
      "no_data"     — zero samples; value is 0.0 by convention, do not display
      "unreliable"  — value failed a sanity check (e.g. negative AOV);
                      must never be used for decisions
    """
    status: str                     # "valid" | "low_sample" | "no_data" | "unreliable"
    sample_size: int = 0
    min_required: int = 0
    message: Optional[str] = None   # human-readable reason for non-valid status


# ── Trend value ───────────────────────────────────────────────────────────────

class TrendValue(BaseModel):
    """
    A single numeric metric with a day-over-day trend.

    value       — today's value
    prev_value  — yesterday's value (None if no data)
    trend       — "up" | "down" | "flat"
                  "flat" when |pct_change| < 1% or prev_value is absent
    pct_change  — signed %, bounded to [-300, +300]; None when prev_value == 0
    quality     — data quality classification; always present
    """
    value: float
    prev_value: Optional[float] = None
    trend: str = "flat"
    pct_change: Optional[float] = None
    quality: DataQuality


# ── Conversion ────────────────────────────────────────────────────────────────

class ConversionMetrics(BaseModel):
    """
    Measures whether combo surfaces and upsell prompts drive order value.

    combo_usage_rate
        Orders where ≥ 1 order-item has ≥ 2 distinct ingredients / all orders.
        Minimum sample: 5 non-cancelled orders.

    avg_order_value_with_combo / avg_order_value_without_combo
        Mean total_amount split by combo flag.
        A positive gap validates that combos drive higher spend.

    upsell_acceptance_rate
        Order-items with ≥ 2 ingredients / all order-items.
        Captures item-level acceptance (finer grain than order-level).
    """
    combo_usage_rate: TrendValue
    avg_order_value_with_combo: TrendValue
    avg_order_value_without_combo: TrendValue
    upsell_acceptance_rate: TrendValue


# ── Decisions ─────────────────────────────────────────────────────────────────

class DecisionMetrics(BaseModel):
    """
    Measures whether the decision engine is being acted on.

    decisions_seen
        Count of decisions where any lifecycle event (acknowledge / complete /
        dismiss) occurred on this date.  A decision created earlier but acted
        on today is counted.

    decisions_acknowledged
        Count where acknowledged_at::date = target_date.

    decisions_completed
        Count where completed_at::date = target_date.

    completion_rate
        decisions_completed / decisions_seen.
        quality.status = "no_data" when decisions_seen == 0.
    """
    decisions_seen: int
    decisions_acknowledged: int
    decisions_completed: int
    completion_rate: TrendValue


# ── Kitchen ───────────────────────────────────────────────────────────────────

class KitchenMetrics(BaseModel):
    """
    Measures kitchen throughput and SLA compliance.

    avg_prep_time_minutes
        Mean(seconds from order.created_at to first READY event) / 60.
        Minimum sample: 3 orders that reached READY status.

    p90_prep_time_minutes
        90th-percentile prep time.
        Minimum sample: 10 orders (PERCENTILE_CONT is misleading below this).

    sla_breach_rate
        Orders with prep_time > 10 minutes / all READY orders.
        10-minute threshold mirrors decision_engine.py 'sla_risk' critical level.
        Minimum sample: 3 READY orders.

    Note: for these metrics lower values are better.  The frontend
    must apply lowerIsBetter=true when colouring trend arrows.
    """
    avg_prep_time_minutes: TrendValue
    p90_prep_time_minutes: TrendValue
    sla_breach_rate: TrendValue


# ── Revenue Protection ────────────────────────────────────────────────────────

class ActualOutcome(BaseModel):
    """
    Breakdown of resolution_quality for stock_risk decisions completed today.

    Definitions (deterministic, not subjective):
      good    — owner explicitly set resolution_quality = 'good'
      partial — owner explicitly set resolution_quality = 'partial'
      failed  — resolution_quality = 'failed'
               OR status = 'completed' but resolution_quality IS NULL
               (unattributed completion is treated conservatively as failed)

    Revenue saved is only credited for good + partial outcomes.
    Failed outcomes contribute 0 to estimated_revenue_saved.
    """
    good: int = 0
    partial: int = 0
    failed: int = 0            # includes unattributed completions


class RevenueProtectionMetrics(BaseModel):
    """
    Measures whether stock-risk signals translate into real revenue protection.

    stock_risk_triggered
        stock_risk decisions where created_at::date = target_date.
        (New signals detected today.)

    stock_risk_resolved
        stock_risk decisions where completed_at::date = target_date.
        (Note: a signal triggered yesterday may be resolved today.)

    estimated_revenue_saved
        SUM(estimated_revenue_saved) for decisions where:
          completed_at::date = target_date
          AND resolution_quality IN ('good', 'partial')
        Failed / unattributed completions contribute 0.

    actual_outcome
        Deterministic breakdown — see ActualOutcome.
    """
    stock_risk_triggered: int
    stock_risk_resolved: int
    estimated_revenue_saved: float
    actual_outcome: ActualOutcome


# ── Observability ─────────────────────────────────────────────────────────────

class MetricsObservability(BaseModel):
    """
    Lightweight observability block — always present, never omitted.

    computed_at       — UTC ISO-8601 timestamp when this response was built
    computation_ms    — wall-clock milliseconds for the full metrics run
    target_date       — the date these metrics describe
    comparison_date   — the date used for prev_value (always target_date - 1 day)
    errors            — non-fatal errors encountered during computation;
                        partial results may still be returned
    """
    computed_at: str
    computation_ms: int
    target_date: str
    comparison_date: str
    errors: List[str] = []


# ── Top-level response ────────────────────────────────────────────────────────

class DailyMetricsResponse(BaseModel):
    date: str                           # YYYY-MM-DD
    as_of: str                          # ISO-8601 UTC (same as meta.computed_at)
    conversion: ConversionMetrics
    decisions: DecisionMetrics
    kitchen: KitchenMetrics
    revenue_protection: RevenueProtectionMetrics
    meta: MetricsObservability


# ── Dictionary endpoint types ─────────────────────────────────────────────────

class MetricDefinition(BaseModel):
    name: str
    group: str
    definition: str
    calculation: str
    edge_cases: List[str]
    interpretation_high: str
    interpretation_low: str
    decision_implication: str
    min_sample: int
    unit: str                           # "rate" | "currency" | "minutes" | "count"
    lower_is_better: bool = False


class MetricDictionaryResponse(BaseModel):
    version: str = "1.0"
    metrics: List[MetricDefinition]
