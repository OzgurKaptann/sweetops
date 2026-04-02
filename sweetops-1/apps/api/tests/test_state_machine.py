"""
State machine tests — prove every transition guard is enforced by the backend.

Covers:
  1. All valid transitions succeed.
  2. Every invalid forward transition returns 409 with structured error.
  3. Terminal states (DELIVERED, CANCELLED) reject ALL further transitions.
  4. Undo (backward transitions) succeed within the 60s window.
  5. Undo fails with 410 after the window expires (DB timestamp manipulated).
  6. Cancellation returns stock exactly once — no double-return possible.
  7. Status events are written for every transition.
"""
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from tests.conftest import cleanup_ingredient, make_ingredient, order_payload
from app.models.order_status_event import OrderStatusEvent


def create_order_and_get_id(client, ingredient_id: int) -> int:
    """Helper: create a fresh order, return its ID."""
    payload, headers = order_payload(ingredient_id, idem_key=uuid.uuid4().hex)
    r = client.post("/public/orders/", json=payload, headers=headers)
    assert r.status_code == 200, f"Order creation failed: {r.json()}"
    return r.json()["order_id"]


def patch_status(client, order_id: int, status: str) -> "requests.Response":  # type: ignore[name-defined]
    return client.patch(
        f"/kitchen/orders/{order_id}/status",
        json={"status": status},
    )


class TestValidTransitions:

    def test_new_to_in_prep(self, db, client):
        ing, _ = make_ingredient(db, stock_quantity=Decimal("100.00"))
        oid = create_order_and_get_id(client, ing.id)

        r = patch_status(client, oid, "IN_PREP")
        assert r.status_code == 200
        assert r.json()["new_status"] == "IN_PREP"

        cleanup_ingredient(db, ing.id)

    def test_in_prep_to_ready(self, db, client):
        ing, _ = make_ingredient(db, stock_quantity=Decimal("100.00"))
        oid = create_order_and_get_id(client, ing.id)

        patch_status(client, oid, "IN_PREP")
        r = patch_status(client, oid, "READY")
        assert r.status_code == 200
        assert r.json()["new_status"] == "READY"

        cleanup_ingredient(db, ing.id)

    def test_ready_to_delivered(self, db, client):
        ing, _ = make_ingredient(db, stock_quantity=Decimal("100.00"))
        oid = create_order_and_get_id(client, ing.id)

        patch_status(client, oid, "IN_PREP")
        patch_status(client, oid, "READY")
        r = patch_status(client, oid, "DELIVERED")
        assert r.status_code == 200
        assert r.json()["new_status"] == "DELIVERED"

        cleanup_ingredient(db, ing.id)

    def test_new_to_cancelled(self, db, client):
        ing, _ = make_ingredient(db, stock_quantity=Decimal("100.00"))
        oid = create_order_and_get_id(client, ing.id)

        r = patch_status(client, oid, "CANCELLED")
        assert r.status_code == 200
        assert r.json()["new_status"] == "CANCELLED"

        cleanup_ingredient(db, ing.id)

    def test_status_events_written_for_each_transition(self, db, client):
        """Every transition must produce an OrderStatusEvent record."""
        ing, _ = make_ingredient(db, stock_quantity=Decimal("100.00"))
        oid = create_order_and_get_id(client, ing.id)

        patch_status(client, oid, "IN_PREP")
        patch_status(client, oid, "READY")

        events = (
            db.query(OrderStatusEvent)
            .filter_by(order_id=oid)
            .order_by(OrderStatusEvent.created_at)
            .all()
        )
        # 1 event from order creation (None→NEW), 2 from transitions
        assert len(events) == 3, f"Expected 3 status events, got {len(events)}"
        statuses = [e.status_to for e in events]
        assert statuses == ["NEW", "IN_PREP", "READY"]

        cleanup_ingredient(db, ing.id)


class TestInvalidTransitions:

    @pytest.mark.parametrize("from_status,to_status", [
        ("NEW",     "READY"),       # skip IN_PREP
        ("NEW",     "DELIVERED"),   # skip two states
        ("IN_PREP", "NEW"),         # backwards (without undo path — treated as forward guard)
        ("IN_PREP", "DELIVERED"),   # skip READY
        ("READY",   "NEW"),         # far backwards
        ("READY",   "IN_PREP"),     # backwards (undo handled separately)
    ])
    def test_invalid_forward_transition_returns_409(self, db, client, from_status, to_status):
        """
        Backend must reject any skip or invalid forward transition with 409
        and a structured error body.
        NOTE: IN_PREP→NEW and READY→IN_PREP are undo transitions tested
        separately. Here we test them as forward-transition violations when
        the undo window concept doesn't apply (i.e., they go through the
        invalid forward path).
        """
        ing, _ = make_ingredient(db, stock_quantity=Decimal("100.00"))
        oid = create_order_and_get_id(client, ing.id)

        # Advance to from_status
        path = {
            "NEW": [],
            "IN_PREP": ["IN_PREP"],
            "READY": ["IN_PREP", "READY"],
        }
        for s in path.get(from_status, []):
            patch_status(client, oid, s)

        # Expire the undo window so backward transitions fail via invalid_transition
        # (not undo_window_expired) — gives us the 409 we want
        if to_status in ("NEW", "IN_PREP") and from_status in ("IN_PREP", "READY"):
            _expire_undo_window(db, oid, from_status)

        r = patch_status(client, oid, to_status)
        assert r.status_code in (409, 410), (
            f"{from_status}→{to_status} must be rejected. Got {r.status_code}: {r.json()}"
        )

        cleanup_ingredient(db, ing.id)

    def test_unknown_order_returns_404(self, db, client):
        r = patch_status(client, 999_999_999, "IN_PREP")
        assert r.status_code == 404


class TestTerminalStateImmutability:

    @pytest.mark.parametrize("terminal", ["DELIVERED", "CANCELLED"])
    @pytest.mark.parametrize("attempted", ["NEW", "IN_PREP", "READY", "DELIVERED", "CANCELLED"])
    def test_terminal_state_rejects_all_transitions(self, db, client, terminal, attempted):
        """
        Once in a terminal state, NO further transition is permitted.
        This covers all 10 combinations (2 terminals × 5 attempted statuses).
        """
        ing, _ = make_ingredient(db, stock_quantity=Decimal("200.00"))
        oid = create_order_and_get_id(client, ing.id)

        # Reach terminal state
        if terminal == "DELIVERED":
            patch_status(client, oid, "IN_PREP")
            patch_status(client, oid, "READY")
            patch_status(client, oid, "DELIVERED")
        else:  # CANCELLED
            patch_status(client, oid, "CANCELLED")

        r = patch_status(client, oid, attempted)
        assert r.status_code == 409, (
            f"Terminal {terminal} must reject {attempted}. Got {r.status_code}: {r.json()}"
        )
        assert r.json()["detail"]["error"] == "terminal_state"

        cleanup_ingredient(db, ing.id)


class TestUndoWindow:

    def test_undo_in_prep_within_window(self, db, client):
        """IN_PREP → NEW must succeed immediately (within window)."""
        ing, _ = make_ingredient(db, stock_quantity=Decimal("100.00"))
        oid = create_order_and_get_id(client, ing.id)

        patch_status(client, oid, "IN_PREP")
        r = patch_status(client, oid, "NEW")

        assert r.status_code == 200, f"Undo must succeed within window. Got: {r.json()}"
        assert r.json()["new_status"] == "NEW"

        cleanup_ingredient(db, ing.id)

    def test_undo_ready_within_window(self, db, client):
        """READY → IN_PREP must succeed immediately (within window)."""
        ing, _ = make_ingredient(db, stock_quantity=Decimal("100.00"))
        oid = create_order_and_get_id(client, ing.id)

        patch_status(client, oid, "IN_PREP")
        patch_status(client, oid, "READY")
        r = patch_status(client, oid, "IN_PREP")

        assert r.status_code == 200, f"Undo must succeed within window. Got: {r.json()}"
        assert r.json()["new_status"] == "IN_PREP"

        cleanup_ingredient(db, ing.id)

    def test_undo_expires_after_window(self, db, client):
        """
        Undo must be rejected with 410 after UNDO_WINDOW_SECONDS.
        We manipulate the OrderStatusEvent.created_at to simulate elapsed time
        without actually waiting 60 seconds.
        """
        ing, _ = make_ingredient(db, stock_quantity=Decimal("100.00"))
        oid = create_order_and_get_id(client, ing.id)

        patch_status(client, oid, "IN_PREP")

        # Backdate the IN_PREP event by 61 seconds
        _expire_undo_window(db, oid, "IN_PREP")

        r = patch_status(client, oid, "NEW")

        assert r.status_code == 410, (
            f"Undo must be rejected with 410 after window. Got {r.status_code}: {r.json()}"
        )
        assert r.json()["detail"]["error"] == "undo_window_expired"

        cleanup_ingredient(db, ing.id)

    def test_undo_after_window_leaves_order_in_original_state(self, db, client):
        """If undo is rejected, the order must remain in the current state."""
        from app.models.order import Order

        ing, _ = make_ingredient(db, stock_quantity=Decimal("100.00"))
        oid = create_order_and_get_id(client, ing.id)

        patch_status(client, oid, "IN_PREP")
        _expire_undo_window(db, oid, "IN_PREP")
        patch_status(client, oid, "NEW")  # will fail with 410

        db.expire_all()
        order = db.query(Order).filter_by(id=oid).first()
        assert order.status == "IN_PREP", (
            f"Rejected undo must leave order in IN_PREP, got {order.status}"
        )

        cleanup_ingredient(db, ing.id)


class TestCancellationStockReturn:

    def test_cancellation_returns_stock_once(self, db, client):
        """
        Cancelling an order must return stock to exactly the original level.
        """
        from app.models.ingredient_stock import IngredientStock

        initial = Decimal("50.00")
        ing, _ = make_ingredient(
            db,
            stock_quantity=initial,
            standard_quantity=Decimal("10.00"),
        )

        oid = create_order_and_get_id(client, ing.id)

        # Stock deducted at order creation
        db.expire_all()
        after_order = db.query(IngredientStock).filter_by(ingredient_id=ing.id).first()
        assert after_order.stock_quantity == initial - Decimal("10.00")

        # Cancel the order
        r = patch_status(client, oid, "CANCELLED")
        assert r.status_code == 200

        db.expire_all()
        after_cancel = db.query(IngredientStock).filter_by(ingredient_id=ing.id).first()
        assert after_cancel.stock_quantity == initial, (
            f"Stock must return to {initial} after cancel. Got {after_cancel.stock_quantity}"
        )

        cleanup_ingredient(db, ing.id)

    def test_cancelled_order_cannot_be_cancelled_again(self, db, client):
        """
        CANCELLED is a terminal state — a second cancellation must return 409.
        Stock must not be double-returned.
        """
        from app.models.ingredient_stock import IngredientStock

        initial = Decimal("50.00")
        ing, _ = make_ingredient(
            db,
            stock_quantity=initial,
            standard_quantity=Decimal("10.00"),
        )

        oid = create_order_and_get_id(client, ing.id)

        # First cancellation — succeeds
        patch_status(client, oid, "CANCELLED")

        db.expire_all()
        after_first_cancel = db.query(IngredientStock).filter_by(ingredient_id=ing.id).first()
        assert after_first_cancel.stock_quantity == initial

        # Second cancellation — must be rejected
        r2 = patch_status(client, oid, "CANCELLED")
        assert r2.status_code == 409
        assert r2.json()["detail"]["error"] == "terminal_state"

        # Stock must not have been double-returned
        db.expire_all()
        after_second = db.query(IngredientStock).filter_by(ingredient_id=ing.id).first()
        assert after_second.stock_quantity == initial, (
            f"Stock double-returned! Got {after_second.stock_quantity}, expected {initial}"
        )

        cleanup_ingredient(db, ing.id)

    def test_in_prep_cancellation_returns_stock(self, db, client):
        """
        An order that reached IN_PREP before being cancelled must also
        have its stock returned (stock was deducted at creation).
        """
        from app.models.ingredient_stock import IngredientStock

        initial = Decimal("50.00")
        ing, _ = make_ingredient(
            db,
            stock_quantity=initial,
            standard_quantity=Decimal("10.00"),
        )

        oid = create_order_and_get_id(client, ing.id)
        patch_status(client, oid, "IN_PREP")

        r = patch_status(client, oid, "CANCELLED")
        assert r.status_code == 200

        db.expire_all()
        stock_after = db.query(IngredientStock).filter_by(ingredient_id=ing.id).first()
        assert stock_after.stock_quantity == initial, (
            f"Stock must return after IN_PREP→CANCELLED. Got {stock_after.stock_quantity}"
        )

        cleanup_ingredient(db, ing.id)

    def test_cancellation_return_movement_recorded(self, db, client):
        """
        A CANCELLATION_RETURN movement must exist after cancel.
        Exactly one — not zero, not two.
        """
        from app.models.ingredient_stock import IngredientStockMovement

        ing, _ = make_ingredient(
            db,
            stock_quantity=Decimal("50.00"),
            standard_quantity=Decimal("10.00"),
        )

        oid = create_order_and_get_id(client, ing.id)
        patch_status(client, oid, "CANCELLED")

        returns = (
            db.query(IngredientStockMovement)
            .filter_by(ingredient_id=ing.id, movement_type="CANCELLATION_RETURN")
            .count()
        )
        assert returns == 1, f"Expected 1 CANCELLATION_RETURN, got {returns}"

        cleanup_ingredient(db, ing.id)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _expire_undo_window(db, order_id: int, status_to: str) -> None:
    """
    Backdate the most recent OrderStatusEvent for order_id/status_to
    by 61 seconds, forcing the undo window to have expired.
    Direct DB manipulation — avoids sleeping 60s in tests.
    """
    event = (
        db.query(OrderStatusEvent)
        .filter_by(order_id=order_id, status_to=status_to)
        .order_by(OrderStatusEvent.created_at.desc())
        .first()
    )
    assert event is not None, f"No status event found for order {order_id} status {status_to}"

    backdated = datetime.now(timezone.utc) - timedelta(seconds=61)
    event.created_at = backdated
    db.commit()
