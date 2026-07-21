"""
Kitchen preparation timing — derived from the existing order lifecycle.

Covers:
  Unit (pure timing math, no DB):
    - queued / prep / time-to-ready durations
    - active IN_PREP duration uses `now` safely
    - READY-before-IN_PREP yields None, never a negative
    - missing events yield None, never a fabricated 0
    - a CANCELLED order never shows a completed prep
    - static-threshold delay classification (queue + prep, warning + critical)
    - _nonneg_delta and _percentile edge cases
  Integration (real DB + HTTP):
    - timing reads the status-event log as the source of truth
    - store scoping rejects cross-store reads
    - unauthenticated access is rejected
    - KITCHEN role is allowed; CASHIER (no kitchen:read) is rejected
    - Cache-Control: no-store on both endpoints
    - summary counts active/waiting/in-prep/ready/delayed correctly
    - empty-day summary is safe (nulls, not fabricated numbers)
    - reading timing mutates no payment / inventory state
"""
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.models.ingredient_stock import IngredientStockMovement
from app.models.order import Order
from app.models.order_status_event import OrderStatusEvent
from app.services import kitchen_timing_service as kt
from app.services.kitchen_timing_service import (
    PREP_CRITICAL_SECONDS,
    PREP_WARNING_SECONDS,
    QUEUED_CRITICAL_SECONDS,
    QUEUED_WARNING_SECONDS,
    _nonneg_delta,
    _percentile,
    compute_order_timing,
    get_active_order_timing,
    get_timing_summary,
)
from tests.conftest import (
    cleanup_ingredient,
    make_authed_client,
    make_ingredient,
    order_payload,
)

UTC = timezone.utc


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

def _mk_order(
    db,
    store_id,
    *,
    created_at,
    status,
    prep_at=None,
    ready_at=None,
    delivered_at=None,
    cancelled_at=None,
):
    """
    Create an Order plus its status-event log directly, with fully controlled
    timestamps. This is what lets timing be asserted deterministically: the
    service derives everything from these events.
    """
    order = Order(store_id=store_id, status=status, total_amount=Decimal("0"))
    db.add(order)
    db.flush()
    order.created_at = created_at
    db.add(OrderStatusEvent(order_id=order.id, status_from=None, status_to="NEW",
                            created_at=created_at))
    if prep_at is not None:
        db.add(OrderStatusEvent(order_id=order.id, status_from="NEW", status_to="IN_PREP",
                                created_at=prep_at))
    if ready_at is not None:
        db.add(OrderStatusEvent(order_id=order.id, status_from="IN_PREP", status_to="READY",
                                created_at=ready_at))
    if delivered_at is not None:
        db.add(OrderStatusEvent(order_id=order.id, status_from="READY", status_to="DELIVERED",
                                created_at=delivered_at))
    if cancelled_at is not None:
        db.add(OrderStatusEvent(order_id=order.id, status_from="NEW", status_to="CANCELLED",
                                created_at=cancelled_at))
    db.commit()
    db.refresh(order)
    return order


def _base(now=None):
    now = now or datetime(2026, 7, 21, 12, 0, 0, tzinfo=UTC)
    created = now - timedelta(minutes=20)
    return now, created


# ---------------------------------------------------------------------------
# 1–3. Completed durations
# ---------------------------------------------------------------------------

class TestCompletedDurations:

    def test_queued_and_prep_and_ttr(self):
        now, created = _base()
        prep = created + timedelta(minutes=3)      # queued 180s
        ready = prep + timedelta(minutes=5)         # prep 300s
        rec = compute_order_timing(
            order_id=1, store_id=1, table_id=None, status="READY",
            created_at=created, prep_started_at=prep, ready_at=ready,
            delivered_at=None, cancelled_at=None, now=now,
        )
        assert rec["queued_seconds"] == 180
        assert rec["prep_seconds"] == 300
        assert rec["time_to_ready_seconds"] == 480

    def test_completed_order_has_no_active_durations(self):
        now, created = _base()
        prep = created + timedelta(minutes=2)
        ready = prep + timedelta(minutes=4)
        delivered = ready + timedelta(minutes=1)
        rec = compute_order_timing(
            order_id=1, store_id=1, table_id=None, status="DELIVERED",
            created_at=created, prep_started_at=prep, ready_at=ready,
            delivered_at=delivered, cancelled_at=None, now=now,
        )
        assert rec["active_seconds"] is None
        assert rec["queued_seconds_active"] is None
        assert rec["prep_seconds_active"] is None
        assert rec["is_delayed"] is False


# ---------------------------------------------------------------------------
# 4. Active durations use `now`
# ---------------------------------------------------------------------------

class TestActiveDurations:

    def test_in_prep_active_uses_now(self):
        now, created = _base()
        prep = now - timedelta(minutes=4)   # cooking for 240s so far
        rec = compute_order_timing(
            order_id=1, store_id=1, table_id=None, status="IN_PREP",
            created_at=created, prep_started_at=prep, ready_at=None,
            delivered_at=None, cancelled_at=None, now=now,
        )
        assert rec["prep_seconds_active"] == 240
        assert rec["prep_seconds"] is None            # not ready yet → no completed prep
        assert rec["queued_seconds"] is not None      # queue phase already closed
        assert rec["active_seconds"] == int((now - created).total_seconds())

    def test_waiting_active_uses_now(self):
        now, created = _base()
        created = now - timedelta(minutes=7)
        rec = compute_order_timing(
            order_id=1, store_id=1, table_id=None, status="NEW",
            created_at=created, prep_started_at=None, ready_at=None,
            delivered_at=None, cancelled_at=None, now=now,
        )
        assert rec["queued_seconds_active"] == 420
        assert rec["queued_seconds"] is None          # prep never started
        assert rec["active_seconds"] == 420


# ---------------------------------------------------------------------------
# 5. READY before IN_PREP is safe
# ---------------------------------------------------------------------------

class TestReadyBeforePrep:

    def test_ready_strictly_before_prep_gives_none(self):
        now, created = _base()
        prep = now                       # prep event recorded "after" ready
        ready = now - timedelta(minutes=1)
        rec = compute_order_timing(
            order_id=1, store_id=1, table_id=None, status="READY",
            created_at=created, prep_started_at=prep, ready_at=ready,
            delivered_at=None, cancelled_at=None, now=now,
        )
        assert rec["prep_seconds"] is None      # never a negative number
        assert rec["prep_seconds"] != 0

    def test_ready_without_any_prep_event(self):
        now, created = _base()
        ready = created + timedelta(minutes=5)
        rec = compute_order_timing(
            order_id=1, store_id=1, table_id=None, status="READY",
            created_at=created, prep_started_at=None, ready_at=ready,
            delivered_at=None, cancelled_at=None, now=now,
        )
        assert rec["prep_seconds"] is None
        assert rec["queued_seconds"] is None
        assert rec["time_to_ready_seconds"] == 300   # end-to-end still known


# ---------------------------------------------------------------------------
# 6. Missing events → None
# ---------------------------------------------------------------------------

class TestMissingEvents:

    def test_new_order_all_completed_durations_none(self):
        now, created = _base()
        rec = compute_order_timing(
            order_id=1, store_id=1, table_id=None, status="NEW",
            created_at=created, prep_started_at=None, ready_at=None,
            delivered_at=None, cancelled_at=None, now=now,
        )
        assert rec["queued_seconds"] is None
        assert rec["prep_seconds"] is None
        assert rec["time_to_ready_seconds"] is None


# ---------------------------------------------------------------------------
# 7. Cancelled orders never fabricate a completed prep
# ---------------------------------------------------------------------------

class TestCancelledNoFakePrep:

    def test_cancelled_after_prep_but_never_ready(self):
        now, created = _base()
        prep = created + timedelta(minutes=2)
        cancelled = prep + timedelta(minutes=1)
        rec = compute_order_timing(
            order_id=1, store_id=1, table_id=None, status="CANCELLED",
            created_at=created, prep_started_at=prep, ready_at=None,
            delivered_at=None, cancelled_at=cancelled, now=now,
        )
        assert rec["prep_seconds"] is None            # no READY → no completed prep
        assert rec["time_to_ready_seconds"] is None
        assert rec["is_delayed"] is False
        assert rec["active_seconds"] is None          # terminal

    def test_cancelled_from_new(self):
        now, created = _base()
        cancelled = created + timedelta(minutes=1)
        rec = compute_order_timing(
            order_id=1, store_id=1, table_id=None, status="CANCELLED",
            created_at=created, prep_started_at=None, ready_at=None,
            delivered_at=None, cancelled_at=cancelled, now=now,
        )
        assert rec["prep_seconds"] is None
        assert rec["queued_seconds"] is None


# ---------------------------------------------------------------------------
# 8-part. Delay classification against static thresholds
# ---------------------------------------------------------------------------

class TestDelayClassification:

    def _new(self, wait_seconds, now):
        created = now - timedelta(seconds=wait_seconds)
        return compute_order_timing(
            order_id=1, store_id=1, table_id=None, status="NEW",
            created_at=created, prep_started_at=None, ready_at=None,
            delivered_at=None, cancelled_at=None, now=now,
        )

    def _prep(self, prep_seconds, now):
        created = now - timedelta(seconds=prep_seconds + 60)
        prep = now - timedelta(seconds=prep_seconds)
        return compute_order_timing(
            order_id=1, store_id=1, table_id=None, status="IN_PREP",
            created_at=created, prep_started_at=prep, ready_at=None,
            delivered_at=None, cancelled_at=None, now=now,
        )

    def test_queue_ok(self):
        now = datetime.now(UTC)
        rec = self._new(QUEUED_WARNING_SECONDS - 10, now)
        assert rec["delay_state"] == "ok" and rec["is_delayed"] is False

    def test_queue_warning(self):
        now = datetime.now(UTC)
        rec = self._new(QUEUED_WARNING_SECONDS + 5, now)
        assert rec["delay_state"] == "warning"
        assert rec["delay_reason"] == "queue_warning"

    def test_queue_critical(self):
        now = datetime.now(UTC)
        rec = self._new(QUEUED_CRITICAL_SECONDS + 5, now)
        assert rec["delay_state"] == "critical"
        assert rec["delay_reason"] == "queue_critical"

    def test_prep_warning(self):
        now = datetime.now(UTC)
        rec = self._prep(PREP_WARNING_SECONDS + 5, now)
        assert rec["delay_state"] == "warning"
        assert rec["delay_reason"] == "prep_warning"

    def test_prep_critical(self):
        now = datetime.now(UTC)
        rec = self._prep(PREP_CRITICAL_SECONDS + 5, now)
        assert rec["delay_state"] == "critical"
        assert rec["delay_reason"] == "prep_critical"


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

class TestHelpers:

    def test_nonneg_delta_none_on_missing(self):
        now = datetime.now(UTC)
        assert _nonneg_delta(now, None) is None
        assert _nonneg_delta(None, now) is None

    def test_nonneg_delta_none_on_negative(self):
        now = datetime.now(UTC)
        assert _nonneg_delta(now - timedelta(seconds=5), now) is None

    def test_percentile_empty_is_none(self):
        assert _percentile([], 0.95) is None

    def test_percentile_single(self):
        assert _percentile([42], 0.95) == 42

    def test_percentile_nearest_rank(self):
        vals = [10, 20, 30, 40, 50, 60, 70, 80, 90, 100]
        assert _percentile(vals, 0.95) == 100
        assert _percentile(vals, 0.5) == 50


# ---------------------------------------------------------------------------
# Integration: source of truth + isolation + summary
# ---------------------------------------------------------------------------

class TestSourceOfTruth:

    def test_timing_derives_prep_start_from_status_event(self, db, client, kitchen_client):
        """Advancing the order through the real API writes an IN_PREP event; the
        timing endpoint must surface prep_started_at from THAT event."""
        ing, _ = make_ingredient(db, on_hand=Decimal("200.00"))
        p, h = order_payload(ing.id, idem_key=uuid.uuid4().hex)
        oid = client.post("/public/orders/", json=p, headers=h).json()["order_id"]

        # Before starting prep, no prep_started_at.
        before = kitchen_client.get("/kitchen/timing/orders").json()
        mine_before = next(o for o in before["orders"] if o["order_id"] == oid)
        assert mine_before["prep_started_at"] is None
        assert mine_before["status"] == "NEW"

        # Start prep through the real kitchen API (writes an IN_PREP status event).
        r = kitchen_client.patch(f"/kitchen/orders/{oid}/status", json={"status": "IN_PREP"})
        assert r.status_code == 200, r.text

        after = kitchen_client.get("/kitchen/timing/orders").json()
        mine_after = next(o for o in after["orders"] if o["order_id"] == oid)
        assert mine_after["prep_started_at"] is not None
        assert mine_after["status"] == "IN_PREP"
        # queued_seconds is now a fixed number derived from the event log.
        assert mine_after["queued_seconds"] is not None

        cleanup_ingredient(db, ing.id)


class TestStoreScoping:

    def test_cross_store_read_excluded(self, db, make_store, make_staff):
        store_a = make_store()
        store_b = make_store()
        now = datetime.now(UTC)
        _mk_order(db, store_a.id, created_at=now - timedelta(minutes=3), status="NEW")

        # Service-level isolation: store B sees none of store A's orders.
        b_active = get_active_order_timing(db, store_b.id)
        assert b_active["summary"]["active_orders"] == 0

        # HTTP-level isolation for a store-B kitchen user.
        kitchen_b = make_staff("KITCHEN", store_id=store_b.id)
        client_b = make_authed_client(db, kitchen_b)
        body = client_b.get("/kitchen/timing/orders").json()
        assert body["summary"]["active_orders"] == 0
        assert body["orders"] == []

    def test_unauthenticated_rejected(self):
        anon = TestClient(app)
        assert anon.get("/kitchen/timing/orders").status_code == 401
        assert anon.get("/kitchen/timing/summary").status_code == 401

    def test_kitchen_allowed_cashier_rejected(self, db, make_staff, make_store):
        store = make_store()
        kitchen = make_staff("KITCHEN", store_id=store.id)
        cashier = make_staff("CASHIER", store_id=store.id)

        k_client = make_authed_client(db, kitchen)
        c_client = make_authed_client(db, cashier)

        assert k_client.get("/kitchen/timing/orders").status_code == 200
        assert k_client.get("/kitchen/timing/summary").status_code == 200
        # CASHIER has no kitchen:read permission.
        assert c_client.get("/kitchen/timing/orders").status_code == 403
        assert c_client.get("/kitchen/timing/summary").status_code == 403

    def test_no_store_cache_header(self, db, make_staff, make_store):
        store = make_store()
        kitchen = make_staff("KITCHEN", store_id=store.id)
        client = make_authed_client(db, kitchen)
        for path in ("/kitchen/timing/orders", "/kitchen/timing/summary"):
            r = client.get(path)
            assert r.headers.get("cache-control") == "no-store", path


class TestSummary:

    def test_counts_and_averages(self, db, make_store):
        store = make_store()
        now = datetime.now(UTC)
        today = now

        # 1 waiting (NEW)
        _mk_order(db, store.id, created_at=today - timedelta(minutes=2), status="NEW")
        # 1 in-prep
        _mk_order(
            db, store.id, created_at=today - timedelta(minutes=6), status="IN_PREP",
            prep_at=today - timedelta(minutes=4),
        )
        # 1 delayed waiting (crosses queue critical)
        _mk_order(
            db, store.id,
            created_at=today - timedelta(seconds=QUEUED_CRITICAL_SECONDS + 30),
            status="NEW",
        )
        # 2 completed today (READY) with known prep durations 300s and 500s
        _mk_order(
            db, store.id, created_at=today - timedelta(minutes=10), status="READY",
            prep_at=today - timedelta(minutes=9),
            ready_at=today - timedelta(minutes=9) + timedelta(seconds=300),
        )
        _mk_order(
            db, store.id, created_at=today - timedelta(minutes=12), status="READY",
            prep_at=today - timedelta(minutes=11),
            ready_at=today - timedelta(minutes=11) + timedelta(seconds=500),
        )

        summary = get_timing_summary(db, store.id)
        # active board = NEW(2) + IN_PREP(1) + READY(2) = 5 (READY is still active)
        assert summary["active_orders"] == 5
        assert summary["waiting_orders"] == 2
        assert summary["in_prep_orders"] == 1
        assert summary["ready_orders"] == 2
        assert summary["delayed_orders"] >= 1
        # Completed prep averages from real durations only: mean(300,500)=400
        assert summary["completed_orders_today"] == 2
        assert summary["average_prep_seconds_today"] == 400
        assert summary["p95_prep_seconds_today"] == 500

    def test_empty_day_summary_is_safe(self, db, make_store):
        store = make_store()
        summary = get_timing_summary(db, store.id)
        assert summary["active_orders"] == 0
        assert summary["completed_orders_today"] == 0
        assert summary["average_prep_seconds_today"] is None
        assert summary["average_time_to_ready_seconds_today"] is None
        assert summary["p95_prep_seconds_today"] is None


class TestNoSideEffects:

    def test_reading_timing_mutates_nothing(self, db, client, kitchen_client):
        """Reading timing must not touch payment fields or inventory movements."""
        ing, _ = make_ingredient(db, on_hand=Decimal("200.00"))
        p, h = order_payload(ing.id, idem_key=uuid.uuid4().hex)
        oid = client.post("/public/orders/", json=p, headers=h).json()["order_id"]

        order_before = db.query(Order).filter(Order.id == oid).first()
        paid_before = order_before.paid_amount
        pay_status_before = order_before.payment_status
        movements_before = db.query(IngredientStockMovement).count()

        kitchen_client.get("/kitchen/timing/orders")
        kitchen_client.get("/kitchen/timing/summary")

        db.expire_all()
        order_after = db.query(Order).filter(Order.id == oid).first()
        assert order_after.paid_amount == paid_before
        assert order_after.payment_status == pay_status_before
        assert db.query(IngredientStockMovement).count() == movements_before

        cleanup_ingredient(db, ing.id)
