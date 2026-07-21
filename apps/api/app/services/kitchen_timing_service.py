"""
Kitchen Preparation Timing — operational timing metrics DERIVED from the existing
order lifecycle.

Source of truth
---------------
This module invents no new stored state. Every timing point comes from records
that already exist:

  * ``orders.created_at``        — when the order entered the system (== the first
                                   ``NEW`` status event written at creation).
  * ``order_status_events``      — the append-only transition log. Each kitchen
                                   step (IN_PREP, READY, DELIVERED, CANCELLED) is
                                   already recorded here by ``order_service`` /
                                   ``kitchen_service`` with a server ``created_at``.

From those we read, per order, the FIRST time it entered each state:

  * prep_started_at  = MIN(created_at) WHERE status_to = 'IN_PREP'
  * ready_at         = MIN(created_at) WHERE status_to = 'READY'
  * delivered_at     = MIN(created_at) WHERE status_to = 'DELIVERED'
  * cancelled_at     = MIN(created_at) WHERE status_to = 'CANCELLED'

"First entry" (MIN) is deliberate: the undo window lets an order bounce
NEW→IN_PREP→NEW→IN_PREP, and the honest "when did the kitchen first start this?"
is the earliest IN_PREP, not the latest.

Why this is measurement, not forecasting
-----------------------------------------
Everything here is arithmetic on timestamps that already happened, plus a
comparison against STATIC thresholds for display. Nothing predicts the future,
estimates a completion time, or models demand. ``is_delayed`` means "this order
has *already* been waiting/cooking longer than the configured line", never "this
order *will* be late".

Safety guarantees
-----------------
  * Missing events yield ``None`` durations — never a fabricated 0 or a guess.
  * A READY recorded before any IN_PREP (impossible ordering, or an order marked
    ready without a prep event) yields ``prep_seconds = None``, never a negative
    number. See ``_nonneg_delta``.
  * A CANCELLED order that never reached READY has no prep/ready duration — its
    prep timing stays ``None`` so a cancellation can never look like a completed
    preparation.
  * Active ("still happening") durations are measured against ``now`` and are
    reported in the ``*_active`` fields, kept separate from completed durations.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.order import Order
from app.models.order_status_event import OrderStatusEvent

# ---------------------------------------------------------------------------
# Static delay thresholds (display only — NOT prediction)
# ---------------------------------------------------------------------------
# These are the operational lines the kitchen wants to see crossed. They are
# intentionally distinct from kitchen_service's SLA bands (which drive the live
# priority queue): those measure *total age* from creation; these measure the
# *queue* and *prep* phases separately, which is what prep-timing is about.
#
# Seconds, so downstream arithmetic never round-trips through minutes.
QUEUED_WARNING_SECONDS = 10 * 60    # 600  — waiting this long before prep starts
QUEUED_CRITICAL_SECONDS = 15 * 60   # 900
PREP_WARNING_SECONDS = 12 * 60      # 720  — cooking this long without going READY
PREP_CRITICAL_SECONDS = 20 * 60     # 1200

# Non-terminal statuses that are "active" in the kitchen. READY is active for
# timing visibility (the food exists but hasn't been handed over), but it accrues
# no further prep delay — its prep is done.
ACTIVE_STATUSES = ("NEW", "IN_PREP", "READY")

# Delay-state labels on the wire (English enums; the frontend maps to Turkish).
DELAY_OK = "ok"
DELAY_WARNING = "warning"
DELAY_CRITICAL = "critical"

# Machine-readable reason for is_delayed, so the UI can explain *which* phase is
# slow without re-deriving it. None when not delayed.
REASON_QUEUE_WARNING = "queue_warning"
REASON_QUEUE_CRITICAL = "queue_critical"
REASON_PREP_WARNING = "prep_warning"
REASON_PREP_CRITICAL = "prep_critical"


# ---------------------------------------------------------------------------
# Timestamp helpers
# ---------------------------------------------------------------------------

def _to_utc(dt: Optional[datetime]) -> Optional[datetime]:
    """UTC-aware view of a possibly-naive DB datetime (PostgreSQL stores UTC)."""
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _nonneg_delta(later: Optional[datetime], earlier: Optional[datetime]) -> Optional[int]:
    """
    Whole seconds between two instants, or ``None`` if either is missing OR the
    ordering is impossible (later < earlier).

    Returning ``None`` on an impossible ordering — rather than 0 or a negative —
    is the single rule that keeps "READY before IN_PREP" from ever fabricating a
    prep duration. A dirty event log degrades to "unknown", not to a lie.
    """
    if later is None or earlier is None:
        return None
    delta = (later - earlier).total_seconds()
    if delta < 0:
        return None
    return int(round(delta))


# ---------------------------------------------------------------------------
# Per-order timing (pure — takes already-loaded timestamps)
# ---------------------------------------------------------------------------

def _delay_state(
    queued_active: Optional[int],
    prep_active: Optional[int],
) -> tuple[str, Optional[str]]:
    """
    Classify an ACTIVE order's delay from its live queue/prep durations against
    the static thresholds. Prep delay dominates queue delay (an order actively
    cooking too long is the more urgent signal than one that merely waited).

    Returns (delay_state, delay_reason). delay_reason is None when state is ok.
    """
    # Prep phase (IN_PREP) — evaluated first so it wins over a stale queue number.
    if prep_active is not None:
        if prep_active >= PREP_CRITICAL_SECONDS:
            return DELAY_CRITICAL, REASON_PREP_CRITICAL
        if prep_active >= PREP_WARNING_SECONDS:
            return DELAY_WARNING, REASON_PREP_WARNING

    # Queue phase (still NEW, waiting to be started).
    if queued_active is not None:
        if queued_active >= QUEUED_CRITICAL_SECONDS:
            return DELAY_CRITICAL, REASON_QUEUE_CRITICAL
        if queued_active >= QUEUED_WARNING_SECONDS:
            return DELAY_WARNING, REASON_QUEUE_WARNING

    return DELAY_OK, None


def compute_order_timing(
    *,
    order_id: int,
    store_id: int,
    table_id: Optional[int],
    status: str,
    created_at: datetime,
    prep_started_at: Optional[datetime],
    ready_at: Optional[datetime],
    delivered_at: Optional[datetime],
    cancelled_at: Optional[datetime],
    now: datetime,
) -> dict:
    """
    Build the full timing record for one order from its lifecycle timestamps.

    All ``*_seconds`` are completed (fixed) durations; all ``*_seconds_active``
    are live durations measured against ``now`` and only present while the
    relevant phase is still open. See the module docstring for the safety rules.
    """
    created = _to_utc(created_at)
    prep_started = _to_utc(prep_started_at)
    ready = _to_utc(ready_at)
    delivered = _to_utc(delivered_at)
    cancelled = _to_utc(cancelled_at)

    is_terminal = status in ("DELIVERED", "CANCELLED")
    is_waiting = status == "NEW"
    is_in_prep = status == "IN_PREP"

    # ── Completed durations ────────────────────────────────────────────────
    # queued_seconds: how long it sat before prep began. Known once prep started.
    queued_seconds = _nonneg_delta(prep_started, created)

    # prep_seconds: how long prep took. Known once READY, and only if prep was
    # actually recorded (guards READY-before-IN_PREP → None, never negative).
    prep_seconds = _nonneg_delta(ready, prep_started)

    # time_to_ready_seconds: end-to-end to first READY.
    time_to_ready_seconds = _nonneg_delta(ready, created)

    # ── Active (live) durations ────────────────────────────────────────────
    queued_seconds_active: Optional[int] = None
    prep_seconds_active: Optional[int] = None
    active_seconds: Optional[int] = None

    if not is_terminal:
        # Time in system so far, for any non-terminal order.
        active_seconds = _nonneg_delta(now, created)

        if is_waiting:
            # Still queued: live wait is now - created.
            queued_seconds_active = _nonneg_delta(now, created)
        elif is_in_prep and prep_started is not None:
            # Actively cooking: live prep is now - prep_started.
            prep_seconds_active = _nonneg_delta(now, prep_started)

    # ── Delay classification (active orders only) ──────────────────────────
    if is_terminal:
        delay_state, delay_reason = DELAY_OK, None
    else:
        delay_state, delay_reason = _delay_state(queued_seconds_active, prep_seconds_active)
    is_delayed = delay_state != DELAY_OK

    return {
        "order_id": order_id,
        "store_id": store_id,
        "table_id": table_id,
        "status": status,
        # Timestamps (UTC ISO-8601 strings, or None)
        "created_at": created.isoformat() if created else None,
        "prep_started_at": prep_started.isoformat() if prep_started else None,
        "ready_at": ready.isoformat() if ready else None,
        "delivered_at": delivered.isoformat() if delivered else None,
        "cancelled_at": cancelled.isoformat() if cancelled else None,
        # Completed durations
        "queued_seconds": queued_seconds,
        "prep_seconds": prep_seconds,
        "time_to_ready_seconds": time_to_ready_seconds,
        # Active durations
        "queued_seconds_active": queued_seconds_active,
        "prep_seconds_active": prep_seconds_active,
        "active_seconds": active_seconds,
        # Delay state
        "is_delayed": is_delayed,
        "delay_state": delay_state,
        "delay_reason": delay_reason,
    }


# ---------------------------------------------------------------------------
# Event-log reduction
# ---------------------------------------------------------------------------

def _first_transition_times(db: Session, order_ids: list[int]) -> dict[int, dict[str, datetime]]:
    """
    For each order, the FIRST time it entered each tracked status, read straight
    from ``order_status_events`` (the source of truth). One grouped query — no
    per-order round trips.

    Returns ``{order_id: {"IN_PREP": dt, "READY": dt, ...}}`` with only the
    statuses that actually occurred present.
    """
    if not order_ids:
        return {}

    rows = (
        db.query(
            OrderStatusEvent.order_id,
            OrderStatusEvent.status_to,
            func.min(OrderStatusEvent.created_at),
        )
        .filter(
            OrderStatusEvent.order_id.in_(order_ids),
            OrderStatusEvent.status_to.in_(["IN_PREP", "READY", "DELIVERED", "CANCELLED"]),
        )
        .group_by(OrderStatusEvent.order_id, OrderStatusEvent.status_to)
        .all()
    )

    out: dict[int, dict[str, datetime]] = {}
    for order_id, status_to, first_at in rows:
        out.setdefault(order_id, {})[status_to] = first_at
    return out


def _timing_for_orders(db: Session, orders: list[Order], now: datetime) -> list[dict]:
    """Compute timing records for a list of already-loaded Order rows."""
    order_ids = [o.id for o in orders]
    transitions = _first_transition_times(db, order_ids)

    records: list[dict] = []
    for order in orders:
        t = transitions.get(order.id, {})
        records.append(
            compute_order_timing(
                order_id=order.id,
                store_id=order.store_id,
                table_id=order.table_id,
                status=order.status,
                created_at=order.created_at,
                prep_started_at=t.get("IN_PREP"),
                ready_at=t.get("READY"),
                delivered_at=t.get("DELIVERED"),
                cancelled_at=t.get("CANCELLED"),
                now=now,
            )
        )
    return records


# ---------------------------------------------------------------------------
# Public: active-orders timing
# ---------------------------------------------------------------------------

def get_active_order_timing(db: Session, store_id: int) -> dict:
    """
    Timing for every ACTIVE order in the store (NEW, IN_PREP, READY), most-delayed
    first, plus a live summary. Store isolation is enforced by the caller (the
    store_id comes from the authenticated session, never the client).
    """
    now = datetime.now(timezone.utc)

    orders = (
        db.query(Order)
        .filter(
            Order.store_id == store_id,
            Order.status.in_(ACTIVE_STATUSES),
        )
        .all()
    )

    records = _timing_for_orders(db, orders, now)

    # Order the board by urgency: delayed first (critical above warning), then by
    # longest live elapsed time. Non-delayed READY food sinks to the bottom.
    _severity_rank = {DELAY_CRITICAL: 2, DELAY_WARNING: 1, DELAY_OK: 0}
    records.sort(
        key=lambda r: (
            _severity_rank.get(r["delay_state"], 0),
            r["active_seconds"] or 0,
        ),
        reverse=True,
    )

    summary = _summarise_active(records)

    return {"orders": records, "summary": summary}


def _summarise_active(records: list[dict]) -> dict:
    """Live counts across the active board (no DB access — pure over records)."""
    waiting = sum(1 for r in records if r["status"] == "NEW")
    in_prep = sum(1 for r in records if r["status"] == "IN_PREP")
    ready = sum(1 for r in records if r["status"] == "READY")
    delayed = sum(1 for r in records if r["is_delayed"])
    return {
        "active_orders": len(records),
        "waiting_orders": waiting,
        "in_prep_orders": in_prep,
        "ready_orders": ready,
        "delayed_orders": delayed,
    }


# ---------------------------------------------------------------------------
# Public: today's completed-timing summary
# ---------------------------------------------------------------------------

def _percentile(sorted_values: list[int], pct: float) -> Optional[int]:
    """
    Nearest-rank percentile over a NON-EMPTY sorted list. ``None`` for an empty
    list (no completed data → no fabricated number).
    """
    if not sorted_values:
        return None
    if len(sorted_values) == 1:
        return sorted_values[0]
    # Nearest-rank: rank = ceil(pct * n), 1-indexed.
    import math

    rank = max(1, math.ceil(pct * len(sorted_values)))
    return sorted_values[min(rank, len(sorted_values)) - 1]


def get_timing_summary(db: Session, store_id: int) -> dict:
    """
    Operational timing summary for the store: live active/waiting/in-prep/ready/
    delayed counts (right now) + completed averages for orders CREATED today
    (UTC) that reached READY.

    "Today" is keyed on ``orders.created_at::date`` = today (UTC), matching the
    day boundary the owner metrics layer already uses. Averages are computed only
    from real completed prep timing; with no completed orders every completed
    figure is ``None`` (never 0-as-if-measured, never a guess).
    """
    now = datetime.now(timezone.utc)
    today = now.date()

    # ── Live active board ──────────────────────────────────────────────────
    active_orders = (
        db.query(Order)
        .filter(Order.store_id == store_id, Order.status.in_(ACTIVE_STATUSES))
        .all()
    )
    active_records = _timing_for_orders(db, active_orders, now)
    live = _summarise_active(active_records)

    # ── Completed-today prep/ready durations ───────────────────────────────
    # Orders created today that actually reached READY (so a real prep completed).
    # CANCELLED-before-READY orders are naturally excluded: they have no READY
    # event, so no prep/ready duration, so they cannot inflate the averages.
    completed_today = (
        db.query(Order)
        .filter(
            Order.store_id == store_id,
            func.date(Order.created_at) == today,
            Order.status.in_(["READY", "DELIVERED"]),
        )
        .all()
    )
    completed_records = _timing_for_orders(db, completed_today, now)

    prep_values = sorted(
        r["prep_seconds"] for r in completed_records if r["prep_seconds"] is not None
    )
    ttr_values = sorted(
        r["time_to_ready_seconds"]
        for r in completed_records
        if r["time_to_ready_seconds"] is not None
    )

    avg_prep = round(sum(prep_values) / len(prep_values)) if prep_values else None
    avg_ttr = round(sum(ttr_values) / len(ttr_values)) if ttr_values else None
    p95_prep = _percentile(prep_values, 0.95)

    return {
        # Live counts
        "active_orders": live["active_orders"],
        "waiting_orders": live["waiting_orders"],
        "in_prep_orders": live["in_prep_orders"],
        "ready_orders": live["ready_orders"],
        "delayed_orders": live["delayed_orders"],
        # Completed-today aggregates (None when no completed data)
        "completed_orders_today": len(prep_values),
        "average_prep_seconds_today": avg_prep,
        "average_time_to_ready_seconds_today": avg_ttr,
        "p95_prep_seconds_today": p95_prep,
        "as_of": now.isoformat(),
    }
