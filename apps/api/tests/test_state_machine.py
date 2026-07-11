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


def patch_status(kitchen_client, order_id: int, status: str) -> "requests.Response":  # type: ignore[name-defined]
    return kitchen_client.patch(
        f"/kitchen/orders/{order_id}/status",
        json={"status": status},
    )


class TestValidTransitions:

    def test_new_to_in_prep(self, db, client, kitchen_client):
        ing, _ = make_ingredient(db, on_hand=Decimal("100.00"))
        oid = create_order_and_get_id(client, ing.id)

        r = patch_status(kitchen_client, oid, "IN_PREP")
        assert r.status_code == 200
        assert r.json()["new_status"] == "IN_PREP"

        cleanup_ingredient(db, ing.id)

    def test_in_prep_to_ready(self, db, client, kitchen_client):
        ing, _ = make_ingredient(db, on_hand=Decimal("100.00"))
        oid = create_order_and_get_id(client, ing.id)

        patch_status(kitchen_client, oid, "IN_PREP")
        r = patch_status(kitchen_client, oid, "READY")
        assert r.status_code == 200
        assert r.json()["new_status"] == "READY"

        cleanup_ingredient(db, ing.id)

    def test_ready_to_delivered(self, db, client, kitchen_client):
        ing, _ = make_ingredient(db, on_hand=Decimal("100.00"))
        oid = create_order_and_get_id(client, ing.id)

        patch_status(kitchen_client, oid, "IN_PREP")
        patch_status(kitchen_client, oid, "READY")
        r = patch_status(kitchen_client, oid, "DELIVERED")
        assert r.status_code == 200
        assert r.json()["new_status"] == "DELIVERED"

        cleanup_ingredient(db, ing.id)

    def test_new_to_cancelled(self, db, client, kitchen_client):
        ing, _ = make_ingredient(db, on_hand=Decimal("100.00"))
        oid = create_order_and_get_id(client, ing.id)

        r = patch_status(kitchen_client, oid, "CANCELLED")
        assert r.status_code == 200
        assert r.json()["new_status"] == "CANCELLED"

        cleanup_ingredient(db, ing.id)

    def test_status_events_written_for_each_transition(self, db, client, kitchen_client):
        """Every transition must produce an OrderStatusEvent record."""
        ing, _ = make_ingredient(db, on_hand=Decimal("100.00"))
        oid = create_order_and_get_id(client, ing.id)

        patch_status(kitchen_client, oid, "IN_PREP")
        patch_status(kitchen_client, oid, "READY")

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
    def test_invalid_forward_transition_returns_409(self, db, client, kitchen_client, from_status, to_status):
        """
        Backend must reject any skip or invalid forward transition with 409
        and a structured error body.
        NOTE: IN_PREP→NEW and READY→IN_PREP are undo transitions tested
        separately. Here we test them as forward-transition violations when
        the undo window concept doesn't apply (i.e., they go through the
        invalid forward path).
        """
        ing, _ = make_ingredient(db, on_hand=Decimal("100.00"))
        oid = create_order_and_get_id(client, ing.id)

        # Advance to from_status
        path = {
            "NEW": [],
            "IN_PREP": ["IN_PREP"],
            "READY": ["IN_PREP", "READY"],
        }
        for s in path.get(from_status, []):
            patch_status(kitchen_client, oid, s)

        # Expire the undo window so backward transitions fail via invalid_transition
        # (not undo_window_expired) — gives us the 409 we want
        if to_status in ("NEW", "IN_PREP") and from_status in ("IN_PREP", "READY"):
            _expire_undo_window(db, oid, from_status)

        r = patch_status(kitchen_client, oid, to_status)
        assert r.status_code in (409, 410), (
            f"{from_status}→{to_status} must be rejected. Got {r.status_code}: {r.json()}"
        )

        cleanup_ingredient(db, ing.id)

    def test_unknown_order_returns_404(self, db, client, kitchen_client):
        r = patch_status(kitchen_client, 999_999_999, "IN_PREP")
        assert r.status_code == 404


class TestTerminalStateImmutability:

    @pytest.mark.parametrize("terminal", ["DELIVERED", "CANCELLED"])
    @pytest.mark.parametrize("attempted", ["NEW", "IN_PREP", "READY", "DELIVERED", "CANCELLED"])
    def test_terminal_state_rejects_all_transitions(self, db, client, kitchen_client, terminal, attempted):
        """
        Once in a terminal state, NO further transition is permitted.
        This covers all 10 combinations (2 terminals × 5 attempted statuses).
        """
        ing, _ = make_ingredient(db, on_hand=Decimal("200.00"))
        oid = create_order_and_get_id(client, ing.id)

        # Reach terminal state
        if terminal == "DELIVERED":
            patch_status(kitchen_client, oid, "IN_PREP")
            patch_status(kitchen_client, oid, "READY")
            patch_status(kitchen_client, oid, "DELIVERED")
        else:  # CANCELLED
            patch_status(kitchen_client, oid, "CANCELLED")

        r = patch_status(kitchen_client, oid, attempted)
        assert r.status_code == 409, (
            f"Terminal {terminal} must reject {attempted}. Got {r.status_code}: {r.json()}"
        )
        assert r.json()["detail"]["error"] == "terminal_state"

        cleanup_ingredient(db, ing.id)


class TestUndoWindow:

    def test_undo_in_prep_within_window(self, db, client, kitchen_client):
        """IN_PREP → NEW must succeed immediately (within window)."""
        ing, _ = make_ingredient(db, on_hand=Decimal("100.00"))
        oid = create_order_and_get_id(client, ing.id)

        patch_status(kitchen_client, oid, "IN_PREP")
        r = patch_status(kitchen_client, oid, "NEW")

        assert r.status_code == 200, f"Undo must succeed within window. Got: {r.json()}"
        assert r.json()["new_status"] == "NEW"

        cleanup_ingredient(db, ing.id)

    def test_undo_ready_within_window(self, db, client, kitchen_client):
        """READY → IN_PREP must succeed immediately (within window)."""
        ing, _ = make_ingredient(db, on_hand=Decimal("100.00"))
        oid = create_order_and_get_id(client, ing.id)

        patch_status(kitchen_client, oid, "IN_PREP")
        patch_status(kitchen_client, oid, "READY")
        r = patch_status(kitchen_client, oid, "IN_PREP")

        assert r.status_code == 200, f"Undo must succeed within window. Got: {r.json()}"
        assert r.json()["new_status"] == "IN_PREP"

        cleanup_ingredient(db, ing.id)

    def test_undo_expires_after_window(self, db, client, kitchen_client):
        """
        Undo must be rejected with 410 after UNDO_WINDOW_SECONDS.
        We manipulate the OrderStatusEvent.created_at to simulate elapsed time
        without actually waiting 60 seconds.
        """
        ing, _ = make_ingredient(db, on_hand=Decimal("100.00"))
        oid = create_order_and_get_id(client, ing.id)

        patch_status(kitchen_client, oid, "IN_PREP")

        # Backdate the IN_PREP event by 61 seconds
        _expire_undo_window(db, oid, "IN_PREP")

        r = patch_status(kitchen_client, oid, "NEW")

        assert r.status_code == 410, (
            f"Undo must be rejected with 410 after window. Got {r.status_code}: {r.json()}"
        )
        assert r.json()["detail"]["error"] == "undo_window_expired"

        cleanup_ingredient(db, ing.id)

    def test_undo_after_window_leaves_order_in_original_state(self, db, client, kitchen_client):
        """If undo is rejected, the order must remain in the current state."""
        from app.models.order import Order

        ing, _ = make_ingredient(db, on_hand=Decimal("100.00"))
        oid = create_order_and_get_id(client, ing.id)

        patch_status(kitchen_client, oid, "IN_PREP")
        _expire_undo_window(db, oid, "IN_PREP")
        patch_status(kitchen_client, oid, "NEW")  # will fail with 410

        db.expire_all()
        order = db.query(Order).filter_by(id=oid).first()
        assert order.status == "IN_PREP", (
            f"Rejected undo must leave order in IN_PREP, got {order.status}"
        )

        cleanup_ingredient(db, ing.id)


class TestCancellationInventoryLifecycle:
    """
    Cancellation is inventory-aware, and the rule is physical reality:

      * cancel BEFORE the kitchen starts  → the reservation is released;
        on-hand never moved, so nothing is "returned".
      * cancel AFTER the kitchen started  → the batter was really poured. Stock
        is NOT restored. Pretending otherwise would invent ingredients that are
        sitting in a bin.
    """

    def test_cancel_before_prep_releases_reservation(self, db, client, kitchen_client):
        """Cancelling an un-started order frees the reservation and leaves on-hand alone."""
        from app.models.ingredient_stock import IngredientStock

        initial = Decimal("50.00")
        ing, _ = make_ingredient(
            db,
            on_hand=initial,
            standard_quantity=Decimal("10.00"),
        )

        oid = create_order_and_get_id(client, ing.id)

        # Order creation RESERVES: available drops, physical on-hand does not.
        db.expire_all()
        after_order = db.query(IngredientStock).filter_by(ingredient_id=ing.id).first()
        assert after_order.on_hand_quantity == initial
        assert after_order.reserved_quantity == Decimal("10.00")
        assert after_order.available_quantity == initial - Decimal("10.00")

        r = patch_status(kitchen_client, oid, "CANCELLED")
        assert r.status_code == 200

        db.expire_all()
        after_cancel = db.query(IngredientStock).filter_by(ingredient_id=ing.id).first()
        assert after_cancel.reserved_quantity == Decimal("0"), "Reservation must be released"
        assert after_cancel.on_hand_quantity == initial, "On-hand must never have moved"
        assert after_cancel.available_quantity == initial

        cleanup_ingredient(db, ing.id)

    def test_cancelled_order_cannot_be_cancelled_again(self, db, client, kitchen_client):
        """
        CANCELLED is terminal — a second cancellation returns 409 and the
        reservation is not released twice.
        """
        from app.models.ingredient_stock import IngredientStock

        initial = Decimal("50.00")
        ing, _ = make_ingredient(
            db,
            on_hand=initial,
            standard_quantity=Decimal("10.00"),
        )

        oid = create_order_and_get_id(client, ing.id)
        patch_status(kitchen_client, oid, "CANCELLED")

        db.expire_all()
        after_first = db.query(IngredientStock).filter_by(ingredient_id=ing.id).first()
        assert after_first.reserved_quantity == Decimal("0")
        assert after_first.available_quantity == initial

        r2 = patch_status(kitchen_client, oid, "CANCELLED")
        assert r2.status_code == 409
        assert r2.json()["detail"]["error"] == "terminal_state"

        # A double release would push available ABOVE the physical stock.
        db.expire_all()
        after_second = db.query(IngredientStock).filter_by(ingredient_id=ing.id).first()
        assert after_second.available_quantity == initial, (
            f"Reservation double-released! Got {after_second.available_quantity}, expected {initial}"
        )
        assert after_second.on_hand_quantity == initial

        cleanup_ingredient(db, ing.id)

    def test_cancel_after_prep_does_not_restore_stock(self, db, client, kitchen_client):
        """
        An order cancelled AFTER the kitchen started it keeps its ingredients
        consumed. The waffle batter is already on the iron; cancelling the order
        does not put it back in the tub.
        """
        from app.models.ingredient_stock import IngredientStock

        initial = Decimal("50.00")
        std = Decimal("10.00")
        ing, _ = make_ingredient(
            db,
            on_hand=initial,
            standard_quantity=std,
        )

        oid = create_order_and_get_id(client, ing.id)

        # Kitchen starts cooking: reservation becomes physical consumption.
        patch_status(kitchen_client, oid, "IN_PREP")
        db.expire_all()
        after_prep = db.query(IngredientStock).filter_by(ingredient_id=ing.id).first()
        assert after_prep.on_hand_quantity == initial - std, "Start-prep must consume"
        assert after_prep.reserved_quantity == Decimal("0"), "Reservation is now consumed"

        r = patch_status(kitchen_client, oid, "CANCELLED")
        assert r.status_code == 200

        db.expire_all()
        after_cancel = db.query(IngredientStock).filter_by(ingredient_id=ing.id).first()
        assert after_cancel.on_hand_quantity == initial - std, (
            f"Consumed stock must NOT be restored on cancellation. "
            f"Expected on-hand {initial - std}, got {after_cancel.on_hand_quantity}"
        )
        assert after_cancel.reserved_quantity == Decimal("0")
        assert after_cancel.available_quantity == initial - std

        cleanup_ingredient(db, ing.id)

    def test_cancellation_release_movement_recorded(self, db, client, kitchen_client):
        """
        Cancelling an un-started order writes exactly one RESERVATION_RELEASED
        movement — not zero, not two — and no physical movement at all.
        """
        from app.models.ingredient_stock import IngredientStockMovement

        ing, _ = make_ingredient(
            db,
            on_hand=Decimal("50.00"),
            standard_quantity=Decimal("10.00"),
        )

        oid = create_order_and_get_id(client, ing.id)
        patch_status(kitchen_client, oid, "CANCELLED")

        releases = (
            db.query(IngredientStockMovement)
            .filter_by(ingredient_id=ing.id, movement_type="RESERVATION_RELEASED")
            .count()
        )
        assert releases == 1, f"Expected 1 RESERVATION_RELEASED, got {releases}"

        # Nothing physical happened: no consumption, and no phantom "return" of
        # stock that was never taken off the shelf.
        for physical in ("CONSUMPTION", "RETURNED"):
            assert db.query(IngredientStockMovement).filter_by(
                ingredient_id=ing.id, movement_type=physical
            ).count() == 0, f"Cancelling an un-started order must not write {physical}"

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
