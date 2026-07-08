"""
GET  /owner/metrics/           — daily metrics (defaults to today UTC)
GET  /owner/metrics/?date=...  — metrics for a specific date (YYYY-MM-DD)
GET  /owner/metrics/dictionary — formal metric definitions

Error behaviour
---------------
- No silent 500s.  Any computation error inside metrics_service is caught there
  and surfaced in meta.errors.  The HTTP response is always 200 with a valid
  DailyMetricsResponse.
- A genuine infrastructure failure (DB unreachable) returns HTTP 503 with a
  structured body — never a bare 500 string.
- Future-date requests return HTTP 422 with a clear message.

Sample response (200 OK)
------------------------
{
  "date": "2026-04-02",
  "as_of": "2026-04-02T10:30:00+00:00",
  "conversion": {
    "combo_usage_rate": {
      "value": 0.65, "prev_value": 0.60,
      "trend": "up", "pct_change": 8.3,
      "quality": {"status": "valid", "sample_size": 42, "min_required": 5}
    },
    ...
  },
  "decisions": { "decisions_seen": 8, ... },
  "kitchen": {
    "avg_prep_time_minutes": {
      "value": 6.2, "prev_value": 7.1,
      "trend": "down", "pct_change": -12.7,
      "quality": {"status": "valid", "sample_size": 38, "min_required": 3}
    },
    ...
  },
  "revenue_protection": {
    "stock_risk_triggered": 4,
    "stock_risk_resolved": 3,
    "estimated_revenue_saved": 850.0,
    "actual_outcome": {"good": 2, "partial": 1, "failed": 0}
  },
  "meta": {
    "computed_at": "2026-04-02T10:30:00+00:00",
    "computation_ms": 47,
    "target_date": "2026-04-02",
    "comparison_date": "2026-04-01",
    "errors": []
  }
}
"""
import logging
from datetime import date, datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.schemas.metrics import DailyMetricsResponse, MetricDictionaryResponse
from app.services.metrics_service import fetch_daily_metrics
from app.services.metric_definitions import get_metric_dictionary

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/owner", tags=["Owner Metrics"])


@router.get("/metrics/", response_model=DailyMetricsResponse)
def get_metrics(
    target_date: Optional[date] = Query(
        default=None,
        alias="date",
        description="Date to compute metrics for (YYYY-MM-DD). Defaults to today UTC.",
        example="2026-04-02",
    ),
    db: Session = Depends(get_db),
) -> DailyMetricsResponse:
    """
    Measurement layer — four metric groups for one day.

    Groups:
      conversion         — combo_usage_rate, AOV split, upsell_acceptance_rate
      decisions          — seen / acknowledged / completed / completion_rate
      kitchen            — avg_prep_time, p90_prep_time, sla_breach_rate
      revenue_protection — stock_risk signals triggered / resolved / saved / outcome

    Every rate/average carries a DataQuality block (valid | low_sample | no_data | unreliable).
    Every TrendValue carries a bounded day-over-day pct_change and trend direction.

    Degraded states:
      - If the target date is in the future, HTTP 422 is returned.
      - If the DB is unreachable, HTTP 503 is returned.
      - If individual metric groups fail, meta.errors lists the non-fatal issues
        and all other groups are still returned.  HTTP 200 always.

    Comparison model:
      Primary:  target_date - 1 day  (always applied)
      Provision: same weekday last week supported by passing ?date=target and
                 modifying comparison_date — no schema change needed.
    """
    # Validate: refuse future dates (no data can exist, would mislead)
    today_utc = datetime.now(timezone.utc).date()
    if target_date and target_date > today_utc:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "future_date",
                "message": f"Cannot compute metrics for a future date ({target_date}). "
                           f"Today is {today_utc}.",
            },
        )

    try:
        return fetch_daily_metrics(db, target_date)
    except OperationalError as exc:
        # DB is unreachable — not a metrics data issue, a real infrastructure failure
        logger.error("metrics DB unreachable: %s", exc)
        raise HTTPException(
            status_code=503,
            detail={
                "error": "database_unavailable",
                "message": "Metrics database is temporarily unavailable. "
                           "Please retry in a few seconds.",
            },
        )
    except Exception as exc:
        # Unexpected — still return structured error, never a bare 500 string
        logger.exception("metrics computation unexpected failure: %s", exc)
        raise HTTPException(
            status_code=503,
            detail={
                "error": "computation_failed",
                "message": "Metrics computation failed unexpectedly. "
                           "This has been logged for investigation.",
            },
        )


@router.get("/metrics/dictionary", response_model=MetricDictionaryResponse)
def get_metrics_dictionary() -> MetricDictionaryResponse:
    """
    Formal metric dictionary — definitions, calculations, edge cases, and
    decision implications for every metric in the measurement layer.

    Use this to:
      - Render inline help text on the dashboard
      - Audit how a metric is computed
      - Understand what high/low values mean and what action to take
    """
    return get_metric_dictionary()
