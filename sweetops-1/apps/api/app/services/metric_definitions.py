"""
Formal Metric Dictionary — metric_definitions.py

This file is the single source of truth for what every metric means,
how it is calculated, when it is valid, and what an owner should do
when it is high or low.

Served via GET /owner/metrics/dictionary so the dashboard can render
inline help text from the same source that governs computation.
"""
from app.schemas.metrics import MetricDefinition, MetricDictionaryResponse

METRIC_VERSION = "1.0"

METRIC_DEFINITIONS: list[MetricDefinition] = [

    # ── Conversion ─────────────────────────────────────────────────────────

    MetricDefinition(
        name="combo_usage_rate",
        group="conversion",
        definition=(
            "The fraction of non-cancelled orders placed today in which "
            "at least one order-item contains two or more distinct ingredients. "
            "Measures how often customers take advantage of ingredient combinations."
        ),
        calculation=(
            "numerator:   COUNT(DISTINCT order_id) WHERE any order_item has "
            "             COUNT(DISTINCT ingredient_id) >= 2\n"
            "denominator: COUNT(DISTINCT order_id) WHERE status != 'CANCELLED'\n"
            "result:      numerator / denominator\n"
            "minimum sample: 5 orders"
        ),
        edge_cases=[
            "Orders with zero ingredients (e.g. base product only) count as non-combo.",
            "A cancelled order is excluded from both numerator and denominator.",
            "An order with multiple items where each item has 1 ingredient is NOT a combo.",
            "Status 'no_data' when fewer than 5 non-cancelled orders exist.",
        ],
        interpretation_high=(
            "Most customers are building ingredient combinations. "
            "The combo engine and menu ranking are working. "
            "AOV should be above the non-combo baseline."
        ),
        interpretation_low=(
            "Customers are ordering single ingredients. "
            "Upsell surfaces may not be visible, or popular-combo badges are not compelling. "
            "Consider promoting the top 3 combos on the menu page."
        ),
        decision_implication=(
            "If combo_usage_rate < 30% for 3+ consecutive days, "
            "review the ingredient recommendation engine and combo badge display. "
            "If > 70%, the system is working; focus energy elsewhere."
        ),
        min_sample=5,
        unit="rate",
        lower_is_better=False,
    ),

    MetricDefinition(
        name="avg_order_value_with_combo",
        group="conversion",
        definition=(
            "Mean total_amount of non-cancelled orders that qualify as combo orders "
            "(at least one item with ≥ 2 ingredients). "
            "Measures the revenue impact of ingredient combinations."
        ),
        calculation=(
            "AVG(total_amount) WHERE order is_combo = TRUE AND status != 'CANCELLED'\n"
            "minimum sample: 5 combo orders"
        ),
        edge_cases=[
            "Returns 0 and quality='no_data' if no combo orders exist today.",
            "Compare against avg_order_value_without_combo to validate the combo premium.",
            "A negative AOV would trigger quality='unreliable' — this should never happen.",
        ],
        interpretation_high=(
            "Combo orders command a meaningful premium over single-ingredient orders. "
            "This directly validates the combo engine's revenue contribution."
        ),
        interpretation_low=(
            "Combo orders are not generating significantly higher revenue than single orders. "
            "Check ingredient pricing — combos may be underpiced relative to perceived value."
        ),
        decision_implication=(
            "If avg_order_value_with_combo is less than 20% higher than "
            "avg_order_value_without_combo, the combo engine is not driving incremental revenue. "
            "Review ingredient mix and whether high-margin items appear in combo recommendations."
        ),
        min_sample=5,
        unit="currency",
        lower_is_better=False,
    ),

    MetricDefinition(
        name="avg_order_value_without_combo",
        group="conversion",
        definition=(
            "Mean total_amount of non-cancelled orders that are NOT combo orders. "
            "Used as the baseline to measure the combo premium."
        ),
        calculation=(
            "AVG(total_amount) WHERE order is_combo = FALSE AND status != 'CANCELLED'\n"
            "minimum sample: 5 non-combo orders"
        ),
        edge_cases=[
            "If ALL orders today are combos, returns 0 with quality='no_data'.",
            "This metric alone is not actionable — always read in relation to avg_order_value_with_combo.",
        ],
        interpretation_high=(
            "Even single-ingredient orders are high-value. "
            "Base product price or ingredient pricing may be strong."
        ),
        interpretation_low=(
            "Single-ingredient orders generate low revenue — expected. "
            "The goal is to pull these customers toward combos."
        ),
        decision_implication=(
            "Monitor the gap: (with_combo - without_combo) / without_combo. "
            "A gap below 15% means combos are not adding meaningful value per order."
        ),
        min_sample=5,
        unit="currency",
        lower_is_better=False,
    ),

    MetricDefinition(
        name="upsell_acceptance_rate",
        group="conversion",
        definition=(
            "The fraction of order-items (not orders) that contain two or more "
            "distinct ingredients. Measures item-level acceptance of ingredient "
            "addition prompts — finer-grained than combo_usage_rate."
        ),
        calculation=(
            "numerator:   COUNT(order_item_id) WHERE COUNT(DISTINCT ingredient_id) >= 2\n"
            "denominator: COUNT(order_item_id) across all non-cancelled orders\n"
            "result:      numerator / denominator\n"
            "minimum sample: 5 order-items"
        ),
        edge_cases=[
            "An order can have multiple items; each is evaluated independently.",
            "An item with 0 ingredients is counted in the denominator as non-combo.",
            "Will be 0 if all items have exactly 1 ingredient.",
        ],
        interpretation_high=(
            "Customers frequently add extra ingredients at item level. "
            "Upsell prompts in the ordering UI are effective."
        ),
        interpretation_low=(
            "Most items are ordered as base configurations. "
            "The upsell prompt may be buried, poorly timed, or unconvincing."
        ),
        decision_implication=(
            "upsell_acceptance_rate < 20% for 3+ days: review the ordering flow. "
            "Is the upsell suggestion visible before the customer finalises their item?"
        ),
        min_sample=5,
        unit="rate",
        lower_is_better=False,
    ),

    # ── Decisions ──────────────────────────────────────────────────────────

    MetricDefinition(
        name="decisions_seen",
        group="decisions",
        definition=(
            "Count of owner decisions on which any lifecycle action "
            "(acknowledge, complete, or dismiss) was taken today. "
            "A decision created before today but acted on today is included."
        ),
        calculation=(
            "COUNT(*) WHERE acknowledged_at::date = target_date\n"
            "           OR completed_at::date = target_date\n"
            "           OR (status = 'dismissed' AND updated_at::date = target_date)"
        ),
        edge_cases=[
            "0 is a valid value — no decisions were acted on. Does not imply no decisions exist.",
            "A decision can be counted twice if acknowledged and completed on the same day.",
            "Decisions from prior days that were ignored until today are still counted.",
        ],
        interpretation_high="Owner is actively engaging with the decision engine.",
        interpretation_low=(
            "Decisions are being generated but not acted on. "
            "Owner may be overwhelmed, or signals may be irrelevant."
        ),
        decision_implication=(
            "If decisions_seen = 0 but decisions exist, the signal volume or priority scoring "
            "may be miscalibrated. Review whether 'high' severity decisions are being surfaced first."
        ),
        min_sample=0,
        unit="count",
        lower_is_better=False,
    ),

    MetricDefinition(
        name="decisions_acknowledged",
        group="decisions",
        definition=(
            "Count of decisions moved to 'acknowledged' status today. "
            "Acknowledgement means the owner has seen the signal and intends to act."
        ),
        calculation=(
            "COUNT(*) WHERE acknowledged_at::date = target_date"
        ),
        edge_cases=[
            "A decision acknowledged today may be completed on a future day.",
            "Does not overlap with decisions_completed (different timestamps).",
        ],
        interpretation_high="Owner is reviewing signals in a timely manner.",
        interpretation_low=(
            "Signals are being completed directly (skipping acknowledge) "
            "or not being reviewed at all."
        ),
        decision_implication=(
            "Persistent low acknowledgement with high completion suggests owners are "
            "acting fast — this is positive. Low on both is a workflow problem."
        ),
        min_sample=0,
        unit="count",
        lower_is_better=False,
    ),

    MetricDefinition(
        name="decisions_completed",
        group="decisions",
        definition=(
            "Count of decisions marked 'completed' today. "
            "Completion means an action was taken in the real world."
        ),
        calculation=(
            "COUNT(*) WHERE completed_at::date = target_date"
        ),
        edge_cases=[
            "A decision may be completed on a different day from when it was triggered.",
            "Dismissed decisions are NOT counted here (see decisions_seen).",
        ],
        interpretation_high="Owner is closing the loop on signals — the engine has real impact.",
        interpretation_low=(
            "Signals are not converting to action. "
            "This is the most important metric to move."
        ),
        decision_implication=(
            "If decisions_completed / decisions_seen < 50% for 3 consecutive days, "
            "run a calibration review: are signals actionable, or are they noise?"
        ),
        min_sample=0,
        unit="count",
        lower_is_better=False,
    ),

    MetricDefinition(
        name="completion_rate",
        group="decisions",
        definition=(
            "decisions_completed / decisions_seen. "
            "The fraction of acted-on decisions that were actually completed "
            "(vs acknowledged or dismissed without action)."
        ),
        calculation=(
            "decisions_completed / decisions_seen\n"
            "quality = 'no_data' when decisions_seen = 0"
        ),
        edge_cases=[
            "decisions_seen = 0 → quality='no_data'; value = 0 by convention.",
            "Can exceed 1.0 in theory if completions from prior days are counted; "
            "this would surface as quality='unreliable'.",
        ],
        interpretation_high="Decisions are being completed, not just acknowledged.",
        interpretation_low=(
            "Owner is seeing signals but not resolving them. "
            "Check whether decisions have clear recommended actions."
        ),
        decision_implication=(
            "Completion rate < 40%: review whether recommended_action text is actionable. "
            "Completion rate > 80%: the engine is well-calibrated."
        ),
        min_sample=1,
        unit="rate",
        lower_is_better=False,
    ),

    # ── Kitchen ────────────────────────────────────────────────────────────

    MetricDefinition(
        name="avg_prep_time_minutes",
        group="kitchen",
        definition=(
            "Mean time in minutes from when an order is created "
            "to when it receives its first READY status event. "
            "Measures kitchen throughput efficiency."
        ),
        calculation=(
            "AVG(\n"
            "  EXTRACT(EPOCH FROM (first_ready_event.created_at - orders.created_at)) / 60\n"
            ")\n"
            "scoped to orders where created_at::date = target_date AND status NOT IN ('NEW','IN_PREP','CANCELLED')\n"
            "minimum sample: 3 orders"
        ),
        edge_cases=[
            "Orders still in NEW or IN_PREP status are excluded (not yet completed).",
            "Cancelled orders are excluded.",
            "If the READY event is missing for a delivered order, that order is excluded.",
            "quality='low_sample' when fewer than 3 READY orders exist.",
        ],
        interpretation_high="Kitchen is slow. Risk of SLA breaches and customer dissatisfaction.",
        interpretation_low="Kitchen is fast and throughput is high.",
        decision_implication=(
            "avg_prep_time > 8 minutes: check for batching opportunities or staffing. "
            "avg_prep_time trending up 3+ days in a row: escalate to kitchen investigation."
        ),
        min_sample=3,
        unit="minutes",
        lower_is_better=True,
    ),

    MetricDefinition(
        name="p90_prep_time_minutes",
        group="kitchen",
        definition=(
            "The 90th-percentile prep time: 90% of orders today were ready within "
            "this many minutes. Measures worst-case kitchen performance, not the average."
        ),
        calculation=(
            "PERCENTILE_CONT(0.9) WITHIN GROUP (ORDER BY prep_minutes)\n"
            "minimum sample: 10 orders (PERCENTILE_CONT with fewer is statistically unreliable)"
        ),
        edge_cases=[
            "quality='low_sample' when fewer than 10 READY orders exist.",
            "Will be inflated by a single very slow order — this is by design.",
            "Compare against avg_prep_time: a large gap signals inconsistency.",
        ],
        interpretation_high=(
            "The slowest 10% of orders take a very long time — significant outliers exist. "
            "Investigate whether specific order types or peak periods cause spikes."
        ),
        interpretation_low="Kitchen is consistent — few outlier orders.",
        decision_implication=(
            "p90 > 15 minutes: individual orders are experiencing extreme delays. "
            "Check batching logic and whether complex orders are being prioritised correctly."
        ),
        min_sample=10,
        unit="minutes",
        lower_is_better=True,
    ),

    MetricDefinition(
        name="sla_breach_rate",
        group="kitchen",
        definition=(
            "Fraction of completed orders today with prep time > 10 minutes. "
            "10 minutes is the SLA critical threshold defined in the decision engine."
        ),
        calculation=(
            "COUNT(*) FILTER (WHERE prep_minutes > 10) / COUNT(*)\n"
            "scoped to orders that reached READY status on target_date\n"
            "minimum sample: 3 orders"
        ),
        edge_cases=[
            "Threshold is hardcoded at 10 minutes to match decision_engine.py SLA_CRITICAL.",
            "An order that took exactly 10.0 minutes does NOT breach (strictly greater than).",
            "quality='low_sample' when fewer than 3 READY orders exist.",
        ],
        interpretation_high=(
            "Many orders are breaching SLA. Customer experience is degraded. "
            "Kitchen is likely overloaded or understaffed."
        ),
        interpretation_low="Kitchen is meeting SLA on nearly all orders.",
        decision_implication=(
            "sla_breach_rate > 20%: immediate kitchen intervention required. "
            "Review active sla_risk decisions in the decision panel."
        ),
        min_sample=3,
        unit="rate",
        lower_is_better=True,
    ),

    # ── Revenue Protection ─────────────────────────────────────────────────

    MetricDefinition(
        name="stock_risk_triggered",
        group="revenue_protection",
        definition=(
            "Count of stock_risk decision signals created today. "
            "Each signal represents an ingredient predicted to run out "
            "within the next 12 hours based on current velocity."
        ),
        calculation=(
            "COUNT(*) WHERE type = 'stock_risk' AND created_at::date = target_date"
        ),
        edge_cases=[
            "The same ingredient can trigger multiple signals in a day if the decision "
            "was resolved and the risk re-emerged.",
            "0 is a valid and desirable value.",
        ],
        interpretation_high=(
            "Many stock-risk signals today — inventory management needs attention. "
            "Could indicate high-demand day or a supply chain problem."
        ),
        interpretation_low="Inventory is healthy — no predicted stockouts.",
        decision_implication=(
            "Compare against stock_risk_resolved. "
            "A large gap (triggered >> resolved) means risks are not being addressed."
        ),
        min_sample=0,
        unit="count",
        lower_is_better=True,
    ),

    MetricDefinition(
        name="stock_risk_resolved",
        group="revenue_protection",
        definition=(
            "Count of stock_risk decisions marked 'completed' today. "
            "Resolution means the owner took a real-world action "
            "(reordered, adjusted menu, sourced alternative supply)."
        ),
        calculation=(
            "COUNT(*) WHERE type = 'stock_risk' AND status = 'completed' "
            "AND completed_at::date = target_date"
        ),
        edge_cases=[
            "A decision triggered yesterday can be resolved today — this is expected.",
            "Resolution count can exceed triggered count for the same day.",
            "Dismissed decisions are NOT counted as resolved.",
        ],
        interpretation_high="Owner is actively closing stock-risk loops.",
        interpretation_low=(
            "Risks are being identified but not acted on. "
            "Revenue may be lost if stockouts occur."
        ),
        decision_implication=(
            "If stock_risk_resolved < stock_risk_triggered for 3+ days, "
            "the stock management workflow needs a process review."
        ),
        min_sample=0,
        unit="count",
        lower_is_better=False,
    ),

    MetricDefinition(
        name="estimated_revenue_saved",
        group="revenue_protection",
        definition=(
            "Total estimated revenue protected by resolving stock-risk signals today. "
            "Only counts decisions where resolution_quality = 'good' or 'partial'. "
            "Failed or unattributed completions contribute zero."
        ),
        calculation=(
            "SUM(estimated_revenue_saved)\n"
            "WHERE type = 'stock_risk'\n"
            "  AND status = 'completed'\n"
            "  AND completed_at::date = target_date\n"
            "  AND resolution_quality IN ('good', 'partial')\n"
            "Failed / NULL resolution_quality: excluded (not credited)"
        ),
        edge_cases=[
            "estimated_revenue_saved on each decision is set by the owner at completion time.",
            "If resolution_quality is NULL at completion, the decision is treated as failed "
            "and contributes 0 to this sum.",
            "This is an estimate, not an audited figure — treat as directional.",
        ],
        interpretation_high=(
            "The stock management system is generating measurable revenue protection. "
            "The decision engine is providing real business value."
        ),
        interpretation_low=(
            "Either no risks were resolved today, or resolved decisions did not "
            "attribute savings. Encourage owners to set estimated_revenue_saved at completion."
        ),
        decision_implication=(
            "Track as a monthly cumulative. If consistently low, "
            "check whether owners are filling in resolution details when completing decisions."
        ),
        min_sample=0,
        unit="currency",
        lower_is_better=False,
    ),

    MetricDefinition(
        name="actual_outcome",
        group="revenue_protection",
        definition=(
            "Deterministic breakdown of resolution quality for stock_risk decisions "
            "completed today. Uses only the resolution_quality field — no inference.\n\n"
            "good    = resolution_quality = 'good'\n"
            "partial = resolution_quality = 'partial'\n"
            "failed  = resolution_quality = 'failed' "
            "OR (status='completed' AND resolution_quality IS NULL)"
        ),
        calculation=(
            "COUNT(*) FILTER (WHERE resolution_quality = 'good')    AS good\n"
            "COUNT(*) FILTER (WHERE resolution_quality = 'partial') AS partial\n"
            "COUNT(*) FILTER (\n"
            "  WHERE resolution_quality = 'failed'\n"
            "     OR (status='completed' AND resolution_quality IS NULL)\n"
            ") AS failed\n\n"
            "All three counts are mutually exclusive."
        ),
        edge_cases=[
            "A completion with no resolution_quality is classified as 'failed' "
            "conservatively — we cannot claim a save without confirmation.",
            "The sum (good + partial + failed) equals stock_risk_resolved.",
            "good + partial >= stock_risk_resolved - failed (no overlaps by design).",
        ],
        interpretation_high="N/A — this is a breakdown, not a single directional metric.",
        interpretation_low="N/A — this is a breakdown, not a single directional metric.",
        decision_implication=(
            "High 'failed' count: investigate whether the recommended actions are "
            "achievable (e.g. supplier lead times are too long). "
            "Encourage owners to always set resolution_quality when completing decisions."
        ),
        min_sample=0,
        unit="count",
        lower_is_better=False,
    ),
]


def get_metric_dictionary() -> MetricDictionaryResponse:
    return MetricDictionaryResponse(
        version=METRIC_VERSION,
        metrics=METRIC_DEFINITIONS,
    )
