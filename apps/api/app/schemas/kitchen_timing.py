"""
Response schemas for kitchen preparation timing.

Durations are integer SECONDS (or ``None`` when the underlying lifecycle event is
missing — see kitchen_timing_service for the "never fabricate" rule). Timestamps
are UTC ISO-8601 strings. ``status``/``delay_state``/``delay_reason`` stay as the
English wire enums; the frontend owns the Turkish rendering.
"""
from typing import List, Optional

from .common import BaseSchema


class OrderTimingResponse(BaseSchema):
    order_id: int
    store_id: int
    table_id: Optional[int] = None
    status: str                                  # NEW | IN_PREP | READY (active board)

    # Lifecycle timestamps (UTC ISO-8601, or null when the step hasn't happened)
    created_at: Optional[str] = None
    prep_started_at: Optional[str] = None
    ready_at: Optional[str] = None
    delivered_at: Optional[str] = None
    cancelled_at: Optional[str] = None

    # Completed durations (seconds) — null until the phase closes
    queued_seconds: Optional[int] = None
    prep_seconds: Optional[int] = None
    time_to_ready_seconds: Optional[int] = None

    # Active (live) durations (seconds) — present only while the phase is open
    queued_seconds_active: Optional[int] = None
    prep_seconds_active: Optional[int] = None
    active_seconds: Optional[int] = None

    # Delay state (static-threshold classification of the live durations)
    is_delayed: bool
    delay_state: str                             # ok | warning | critical
    delay_reason: Optional[str] = None           # queue_/prep_ warning|critical


class ActiveTimingSummary(BaseSchema):
    active_orders: int
    waiting_orders: int
    in_prep_orders: int
    ready_orders: int
    delayed_orders: int


class ActiveTimingResponse(BaseSchema):
    orders: List[OrderTimingResponse]
    summary: ActiveTimingSummary


class TimingSummaryResponse(BaseSchema):
    # Live counts
    active_orders: int
    waiting_orders: int
    in_prep_orders: int
    ready_orders: int
    delayed_orders: int
    # Completed-today aggregates (null when no completed orders today)
    completed_orders_today: int
    average_prep_seconds_today: Optional[int] = None
    average_time_to_ready_seconds_today: Optional[int] = None
    p95_prep_seconds_today: Optional[int] = None
    as_of: str
