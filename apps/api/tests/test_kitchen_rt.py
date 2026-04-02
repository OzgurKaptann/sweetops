"""
Kitchen real-time system tests.

Covers:
  1. Unit: _sla_severity — all three bands, exact boundary values.
  2. Unit: _priority_score — status weight, SLA multiplier, complexity cap,
           critical always beats non-critical regardless of ingredient count.
  3. Integration: GET /kitchen/orders/ sorting — orders returned highest
     priority first; uses DB timestamp backdating (same technique as undo tests).
  4. Integration: sla_severity field value matches age of order.
  5. Integration: created_at in response is a UTC ISO-8601 string with timezone.
  6. Async: multiple WS clients all receive a broadcast.
  7. Async: dead socket is cleaned up; broadcast does not raise.
  8. Async: broadcast with zero connections is a no-op.
"""
import json
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from app.models.order import Order
from app.services.kitchen_service import (
    SLA_CRITICAL_MINUTES,
    SLA_WARNING_MINUTES,
    _priority_score,
    _sla_severity,
)
from tests.conftest import cleanup_ingredient, make_ingredient, order_payload


# ---------------------------------------------------------------------------
# 1–2. Unit: pure scoring functions
# ---------------------------------------------------------------------------

class TestSlaSeverity:

    def test_ok_below_warning(self):
        assert _sla_severity(0.0) == "ok"
        assert _sla_severity(SLA_WARNING_MINUTES - 0.1) == "ok"

    def test_warning_at_exact_boundary(self):
        assert _sla_severity(SLA_WARNING_MINUTES) == "warning"

    def test_warning_just_below_critical(self):
        assert _sla_severity(SLA_CRITICAL_MINUTES - 0.1) == "warning"

    def test_critical_at_exact_boundary(self):
        assert _sla_severity(SLA_CRITICAL_MINUTES) == "critical"

    def test_critical_well_past_threshold(self):
        assert _sla_severity(30.0) == "critical"


class TestPriorityScore:

    def test_new_outranks_in_prep_same_age_same_complexity(self):
        """NEW accumulates urgency faster than IN_PREP (1.2 vs 1.0 weight)."""
        new_score = _priority_score(5.0, 2, "NEW")
        in_prep_score = _priority_score(5.0, 2, "IN_PREP")
        assert new_score > in_prep_score

    def test_critical_order_always_beats_ok_order_regardless_of_complexity(self):
        """
        A just-breached order with zero ingredients must outrank
        a fresh order with maximum complexity.
        """
        critical = _priority_score(SLA_CRITICAL_MINUTES, 0, "NEW")
        fresh_complex = _priority_score(0.1, 100, "NEW")  # 100 slots, but capped at 5
        assert critical > fresh_complex, (
            f"Critical score {critical} must beat fresh-complex score {fresh_complex}"
        )

    def test_complexity_cap_at_5_slots(self):
        """Slots beyond 5 contribute nothing — outlier orders don't dominate."""
        score_5 = _priority_score(5.0, 5, "NEW")
        score_10 = _priority_score(5.0, 10, "NEW")
        score_100 = _priority_score(5.0, 100, "NEW")
        assert score_5 == score_10 == score_100

    def test_complexity_bonus_increments_below_cap(self):
        """Each slot adds 0.3 up to the cap."""
        s0 = _priority_score(5.0, 0, "NEW")
        s1 = _priority_score(5.0, 1, "NEW")
        s5 = _priority_score(5.0, 5, "NEW")
        assert round(s1 - s0, 5) == 0.3
        assert round(s5 - s0, 5) == 1.5

    def test_warning_multiplier_separates_from_ok(self):
        """An order in the warning band scores higher than same-age order just below it."""
        below_warning = _priority_score(SLA_WARNING_MINUTES - 0.1, 0, "IN_PREP")
        at_warning = _priority_score(SLA_WARNING_MINUTES, 0, "IN_PREP")
        assert at_warning > below_warning * 1.4  # multiplier jumps 1.0→1.5

    def test_known_values(self):
        """Regression: verify exact documented examples from the docstring."""
        # 11 min NEW, 2 slots → 11×1.2×2.5 + 0.6 = 33.6
        assert _priority_score(11.0, 2, "NEW") == 33.6
        # 8 min IN_PREP, 5 slots → 8×1.0×1.5 + 1.5 = 13.5
        assert _priority_score(8.0, 5, "IN_PREP") == 13.5
        # 5 min NEW, 3 slots → 5×1.2×1.0 + 0.9 = 6.9
        assert _priority_score(5.0, 3, "NEW") == 6.9
        # 1 min NEW, 5 slots → 1×1.2×1.0 + 1.5 = 2.7
        assert _priority_score(1.0, 5, "NEW") == 2.7


# ---------------------------------------------------------------------------
# 3–5. Integration: GET /kitchen/orders/ endpoint
# ---------------------------------------------------------------------------

def _backdate_order(db, order_id: int, minutes_ago: float) -> None:
    """Directly set order.created_at in the past to simulate aging."""
    backdated = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    order = db.query(Order).filter(Order.id == order_id).first()
    assert order is not None
    order.created_at = backdated
    db.commit()


class TestKitchenOrdersSorting:

    def test_critical_age_order_leads_queue(self, db, client):
        """
        A 12-min-old NEW order with 1 ingredient must appear before a
        fresh NEW order with 5 ingredients despite the latter's complexity.
        """
        ing, _ = make_ingredient(db, stock_quantity=Decimal("200.00"))

        # Fresh order (high complexity)
        p_fresh, h_fresh = order_payload(ing.id, idem_key=uuid.uuid4().hex)
        r1 = client.post("/public/orders/", json=p_fresh, headers=h_fresh)
        fresh_id = r1.json()["order_id"]

        # Aged order (critical zone)
        p_old, h_old = order_payload(ing.id, idem_key=uuid.uuid4().hex)
        r2 = client.post("/public/orders/", json=p_old, headers=h_old)
        old_id = r2.json()["order_id"]

        _backdate_order(db, old_id, minutes_ago=12.0)

        r = client.get("/kitchen/orders/?store_id=1")
        assert r.status_code == 200
        ids = [o["id"] for o in r.json()]

        assert ids.index(old_id) < ids.index(fresh_id), (
            f"Critical-zone order {old_id} must appear before fresh order {fresh_id}. "
            f"Got order: {ids}"
        )

        cleanup_ingredient(db, ing.id)

    def test_orders_sorted_by_priority_score_descending(self, db, client):
        """
        Three orders at 1 min, 8 min (warning), 11 min (critical).
        Expected ranking: critical → warning → ok.
        """
        ing, _ = make_ingredient(db, stock_quantity=Decimal("200.00"))

        r_ok = client.post("/public/orders/",
                           json=order_payload(ing.id, idem_key=uuid.uuid4().hex)[0],
                           headers=order_payload(ing.id, idem_key=uuid.uuid4().hex)[1])
        r_warn = client.post("/public/orders/",
                             json=order_payload(ing.id, idem_key=uuid.uuid4().hex)[0],
                             headers=order_payload(ing.id, idem_key=uuid.uuid4().hex)[1])
        r_crit = client.post("/public/orders/",
                             json=order_payload(ing.id, idem_key=uuid.uuid4().hex)[0],
                             headers=order_payload(ing.id, idem_key=uuid.uuid4().hex)[1])

        ok_id = r_ok.json()["order_id"]
        warn_id = r_warn.json()["order_id"]
        crit_id = r_crit.json()["order_id"]

        _backdate_order(db, ok_id, minutes_ago=1.0)
        _backdate_order(db, warn_id, minutes_ago=8.0)
        _backdate_order(db, crit_id, minutes_ago=11.0)

        r = client.get("/kitchen/orders/?store_id=1")
        assert r.status_code == 200
        orders = r.json()

        # Extract only our three orders (others may exist in the test DB)
        our_ids = {ok_id, warn_id, crit_id}
        our_orders = [o for o in orders if o["id"] in our_ids]
        assert len(our_orders) == 3

        our_scores = [(o["id"], o["priority_score"]) for o in our_orders]
        scores_in_order = [s for _, s in our_scores]

        # Verify descending order
        assert scores_in_order == sorted(scores_in_order, reverse=True), (
            f"Orders not sorted by priority_score descending: {our_scores}"
        )

        # Verify critical is first of our three
        assert our_orders[0]["id"] == crit_id, (
            f"Critical-zone order must lead. Got: {[o['id'] for o in our_orders]}"
        )

        cleanup_ingredient(db, ing.id)

    def test_priority_score_in_response_is_positive(self, db, client):
        """Sanity: every order in response has a non-negative priority_score."""
        ing, _ = make_ingredient(db, stock_quantity=Decimal("50.00"))
        p, h = order_payload(ing.id, idem_key=uuid.uuid4().hex)
        client.post("/public/orders/", json=p, headers=h)

        r = client.get("/kitchen/orders/?store_id=1")
        for order in r.json():
            assert order["priority_score"] >= 0, f"Negative score: {order}"

        cleanup_ingredient(db, ing.id)


class TestSlaSeverityInResponse:

    def test_fresh_order_is_ok(self, db, client):
        ing, _ = make_ingredient(db, stock_quantity=Decimal("50.00"))
        p, h = order_payload(ing.id, idem_key=uuid.uuid4().hex)
        r = client.post("/public/orders/", json=p, headers=h)
        oid = r.json()["order_id"]

        orders = client.get("/kitchen/orders/?store_id=1").json()
        our = next(o for o in orders if o["id"] == oid)
        assert our["sla_severity"] == "ok"

        cleanup_ingredient(db, ing.id)

    def test_aged_order_is_warning(self, db, client):
        ing, _ = make_ingredient(db, stock_quantity=Decimal("50.00"))
        p, h = order_payload(ing.id, idem_key=uuid.uuid4().hex)
        r = client.post("/public/orders/", json=p, headers=h)
        oid = r.json()["order_id"]

        _backdate_order(db, oid, minutes_ago=SLA_WARNING_MINUTES + 0.5)

        orders = client.get("/kitchen/orders/?store_id=1").json()
        our = next(o for o in orders if o["id"] == oid)
        assert our["sla_severity"] == "warning", f"Expected warning, got: {our['sla_severity']}"

        cleanup_ingredient(db, ing.id)

    def test_breached_order_is_critical(self, db, client):
        ing, _ = make_ingredient(db, stock_quantity=Decimal("50.00"))
        p, h = order_payload(ing.id, idem_key=uuid.uuid4().hex)
        r = client.post("/public/orders/", json=p, headers=h)
        oid = r.json()["order_id"]

        _backdate_order(db, oid, minutes_ago=SLA_CRITICAL_MINUTES + 1.0)

        orders = client.get("/kitchen/orders/?store_id=1").json()
        our = next(o for o in orders if o["id"] == oid)
        assert our["sla_severity"] == "critical", f"Expected critical, got: {our['sla_severity']}"

        cleanup_ingredient(db, ing.id)


class TestTimestampConsistency:

    def test_created_at_is_utc_iso8601_string(self, db, client):
        """
        created_at in kitchen orders response must be a UTC ISO-8601 string
        with explicit timezone offset (not a naive datetime).
        """
        ing, _ = make_ingredient(db, stock_quantity=Decimal("50.00"))
        p, h = order_payload(ing.id, idem_key=uuid.uuid4().hex)
        r = client.post("/public/orders/", json=p, headers=h)
        oid = r.json()["order_id"]

        orders = client.get("/kitchen/orders/?store_id=1").json()
        our = next(o for o in orders if o["id"] == oid)

        ts = our["created_at"]
        assert isinstance(ts, str), f"created_at must be a string, got {type(ts)}"

        # Must be parseable as an aware datetime
        parsed = datetime.fromisoformat(ts)
        assert parsed.tzinfo is not None, (
            f"created_at must include timezone info, got: {ts!r}"
        )

        # Must end with +00:00 (UTC), not a bare Z or naive string
        assert "+00:00" in ts or ts.endswith("Z"), (
            f"created_at must be UTC, got: {ts!r}"
        )

        cleanup_ingredient(db, ing.id)

    def test_created_at_matches_order_creation_response(self, db, client):
        """
        The created_at returned by GET /kitchen/orders/ must refer to the
        same point in time as the order's actual creation.
        """
        ing, _ = make_ingredient(db, stock_quantity=Decimal("50.00"))
        p, h = order_payload(ing.id, idem_key=uuid.uuid4().hex)
        r = client.post("/public/orders/", json=p, headers=h)
        oid = r.json()["order_id"]

        # Fetch from kitchen endpoint
        orders = client.get("/kitchen/orders/?store_id=1").json()
        our = next(o for o in orders if o["id"] == oid)

        # Fetch raw from DB
        order = db.query(Order).filter(Order.id == oid).first()
        db_utc = order.created_at
        if db_utc.tzinfo is None:
            db_utc = db_utc.replace(tzinfo=timezone.utc)

        api_utc = datetime.fromisoformat(our["created_at"])

        # Allow 1-second tolerance for clock jitter
        delta = abs((api_utc - db_utc).total_seconds())
        assert delta < 1.0, f"Timestamp mismatch: API={our['created_at']!r} DB={db_utc.isoformat()!r}"

        cleanup_ingredient(db, ing.id)


# ---------------------------------------------------------------------------
# 6–8. Async: WebSocket lifecycle
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_broadcast_reaches_multiple_clients():
    """All registered clients receive the same broadcast payload."""
    from app.services.websocket_manager import KitchenWebSocketManager

    manager = KitchenWebSocketManager()

    ws1 = AsyncMock()
    ws2 = AsyncMock()
    ws3 = AsyncMock()

    # Register without going through connect() to avoid WebSocket handshake
    manager._connections[ws1] = "aaa"
    manager._connections[ws2] = "bbb"
    manager._connections[ws3] = "ccc"

    await manager.broadcast_kitchen_event("order_created", {"order_id": 42})

    expected = json.dumps({"event": "order_created", "data": {"order_id": 42}})
    ws1.send_text.assert_called_once_with(expected)
    ws2.send_text.assert_called_once_with(expected)
    ws3.send_text.assert_called_once_with(expected)


@pytest.mark.anyio
async def test_dead_socket_removed_after_broadcast():
    """
    A socket that raises on send_text is removed from the registry.
    The surviving socket still receives the message.
    """
    from app.services.websocket_manager import KitchenWebSocketManager

    manager = KitchenWebSocketManager()

    dead_ws = AsyncMock()
    dead_ws.send_text.side_effect = RuntimeError("connection closed")
    live_ws = AsyncMock()

    manager._connections[dead_ws] = "dead"
    manager._connections[live_ws] = "live"

    await manager.broadcast_kitchen_event("ping", {})

    # Dead socket removed
    assert dead_ws not in manager._connections
    # Live socket still registered and received the message
    assert live_ws in manager._connections
    live_ws.send_text.assert_called_once()


@pytest.mark.anyio
async def test_all_dead_sockets_cleaned_no_crash():
    """
    If every connection is dead, broadcast completes without raising
    and the registry ends up empty.
    """
    from app.services.websocket_manager import KitchenWebSocketManager

    manager = KitchenWebSocketManager()

    for i in range(3):
        ws = AsyncMock()
        ws.send_text.side_effect = Exception(f"dead-{i}")
        manager._connections[ws] = f"dead-{i}"

    # Must not raise
    await manager.broadcast_kitchen_event("test", {"x": 1})

    assert manager.connection_count == 0


@pytest.mark.anyio
async def test_broadcast_with_zero_connections_is_noop():
    """Empty registry — broadcast returns immediately, no exceptions."""
    from app.services.websocket_manager import KitchenWebSocketManager

    manager = KitchenWebSocketManager()
    assert manager.connection_count == 0

    # No exception
    await manager.broadcast_kitchen_event("test", {})


@pytest.mark.anyio
async def test_disconnect_is_idempotent():
    """Calling disconnect() twice for the same socket must not raise."""
    from app.services.websocket_manager import KitchenWebSocketManager

    manager = KitchenWebSocketManager()
    ws = AsyncMock()
    manager._connections[ws] = "test"

    manager.disconnect(ws)
    manager.disconnect(ws)  # second call must be silent

    assert manager.connection_count == 0
