"""
The inventory lifecycle end-to-end, through the real API.

The rule under test throughout: **an order is a promise, cooking is a fact.**

  order created        → RESERVED   (available falls, on-hand untouched)
  kitchen starts       → CONSUMED   (reserved falls, on-hand falls) — exactly once
  ready / delivered    → nothing more happens
  cancel before start  → RELEASED   (reserved falls, on-hand untouched)
  cancel after start   → nothing restored (the batter really was poured)

Payment state never moves stock, and stock never moves payment.
"""
import uuid
from decimal import Decimal

import pytest

from app.models.ingredient_stock import (
    IngredientStock,
    IngredientStockMovement,
    OrderInventoryLine,
)
from app.models.order import Order
from tests.conftest import (
    cleanup_ingredient,
    make_ingredient,
    order_payload,
    purge_payments_for_orders,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _stock(db, ing_id: int) -> IngredientStock:
    db.expire_all()
    return db.query(IngredientStock).filter_by(ingredient_id=ing_id).first()


def _movements(db, ing_id: int, movement_type: str) -> list[IngredientStockMovement]:
    return (
        db.query(IngredientStockMovement)
        .filter_by(ingredient_id=ing_id, movement_type=movement_type)
        .all()
    )


def _order(db, client, ing_id: int) -> int:
    payload, headers = order_payload(ing_id, idem_key=uuid.uuid4().hex)
    r = client.post("/public/orders/", json=payload, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()["order_id"]


def _patch(kitchen_client, order_id: int, status: str):
    return kitchen_client.patch(
        f"/kitchen/orders/{order_id}/status", json={"status": status}
    )


# ---------------------------------------------------------------------------
# Order creation → reservation
# ---------------------------------------------------------------------------

class TestOrderCreationReserves:

    def test_order_reserves_and_does_not_consume(self, db, client):
        initial = Decimal("100.000")
        ing, _ = make_ingredient(
            db, on_hand=initial, standard_quantity=Decimal("10.00")
        )
        try:
            oid = _order(db, client, ing.id)

            s = _stock(db, ing.id)
            assert s.on_hand_quantity == initial, "creation must NOT consume"
            assert s.reserved_quantity == Decimal("10.000")
            assert s.available_quantity == initial - Decimal("10.000")

            # One reservation movement, no consumption movement.
            assert len(_movements(db, ing.id, "RESERVATION_CREATED")) == 1
            assert len(_movements(db, ing.id, "CONSUMPTION")) == 0

            # And a per-order inventory line exists with the reservation on it.
            lines = db.query(OrderInventoryLine).filter_by(order_id=oid).all()
            assert len(lines) == 1
            assert lines[0].reserved_quantity == Decimal("10.000")
            assert lines[0].consumed_quantity == Decimal("0")
            assert lines[0].released_quantity == Decimal("0")
        finally:
            cleanup_ingredient(db, ing.id)

    def test_insufficient_available_stock_rejects_order(self, db, client):
        """Availability is the gate, and rejection leaves nothing behind."""
        ing, _ = make_ingredient(
            db, on_hand=Decimal("5.000"), standard_quantity=Decimal("10.00")
        )
        try:
            payload, headers = order_payload(ing.id, idem_key=uuid.uuid4().hex)
            r = client.post("/public/orders/", json=payload, headers=headers)
            assert r.status_code == 422
            assert r.json()["detail"]["error"] == "out_of_stock"

            s = _stock(db, ing.id)
            assert s.on_hand_quantity == Decimal("5.000")
            assert s.reserved_quantity == Decimal("0")
            assert len(_movements(db, ing.id, "RESERVATION_CREATED")) == 0
        finally:
            cleanup_ingredient(db, ing.id)

    def test_stock_reserved_by_another_order_is_not_available(self, db, client):
        """
        The heart of the bug this branch fixes: physical stock that is already
        promised to an open order must not be sellable again. On-hand is 10 and
        stays 10 — but the second order is still correctly refused.
        """
        ing, _ = make_ingredient(
            db, on_hand=Decimal("10.000"), standard_quantity=Decimal("10.00")
        )
        try:
            _order(db, client, ing.id)   # reserves all 10

            s = _stock(db, ing.id)
            assert s.on_hand_quantity == Decimal("10.000"), "nothing cooked yet"
            assert s.available_quantity == Decimal("0")

            payload, headers = order_payload(ing.id, idem_key=uuid.uuid4().hex)
            r = client.post("/public/orders/", json=payload, headers=headers)
            assert r.status_code == 422, (
                "Stock already promised to an open order must not be sold twice, "
                "even though it is still physically on the shelf"
            )
        finally:
            cleanup_ingredient(db, ing.id)

    def test_idempotent_replay_does_not_double_reserve(self, db, client):
        ing, _ = make_ingredient(
            db, on_hand=Decimal("100.000"), standard_quantity=Decimal("10.00")
        )
        try:
            idem = uuid.uuid4().hex
            payload, headers = order_payload(ing.id, idem_key=idem)
            r1 = client.post("/public/orders/", json=payload, headers=headers)
            r2 = client.post("/public/orders/", json=payload, headers=headers)

            assert r1.status_code == r2.status_code == 200
            assert r1.json()["order_id"] == r2.json()["order_id"]

            s = _stock(db, ing.id)
            assert s.reserved_quantity == Decimal("10.000")
            assert len(_movements(db, ing.id, "RESERVATION_CREATED")) == 1
            assert db.query(OrderInventoryLine).filter_by(
                order_id=r1.json()["order_id"]
            ).count() == 1
        finally:
            cleanup_ingredient(db, ing.id)


# ---------------------------------------------------------------------------
# Kitchen → consumption
# ---------------------------------------------------------------------------

class TestKitchenConsumption:

    def test_start_prep_consumes_exactly_once(self, db, client, kitchen_client):
        initial = Decimal("100.000")
        ing, _ = make_ingredient(
            db, on_hand=initial, standard_quantity=Decimal("10.00")
        )
        try:
            oid = _order(db, client, ing.id)
            assert _patch(kitchen_client, oid, "IN_PREP").status_code == 200

            s = _stock(db, ing.id)
            assert s.on_hand_quantity == initial - Decimal("10.000"), "must consume"
            assert s.reserved_quantity == Decimal("0"), "reservation is now spent"
            assert s.available_quantity == initial - Decimal("10.000")

            consumption = _movements(db, ing.id, "CONSUMPTION")
            assert len(consumption) == 1
            assert consumption[0].quantity == Decimal("10.000")
            assert consumption[0].quantity_delta_on_hand == Decimal("-10.000")
            assert consumption[0].quantity_delta_reserved == Decimal("-10.000")
            assert consumption[0].actor_user_id is not None, "staff must be named"

            line = db.query(OrderInventoryLine).filter_by(order_id=oid).first()
            assert line.consumed_quantity == Decimal("10.000")
        finally:
            cleanup_ingredient(db, ing.id)

    def test_ready_and_delivered_do_not_consume_again(self, db, client, kitchen_client):
        initial = Decimal("100.000")
        ing, _ = make_ingredient(
            db, on_hand=initial, standard_quantity=Decimal("10.00")
        )
        try:
            oid = _order(db, client, ing.id)
            _patch(kitchen_client, oid, "IN_PREP")
            after_prep = _stock(db, ing.id).on_hand_quantity

            assert _patch(kitchen_client, oid, "READY").status_code == 200
            assert _stock(db, ing.id).on_hand_quantity == after_prep

            assert _patch(kitchen_client, oid, "DELIVERED").status_code == 200
            assert _stock(db, ing.id).on_hand_quantity == after_prep

            assert len(_movements(db, ing.id, "CONSUMPTION")) == 1, (
                "Serving a waffle does not cook it a second time"
            )
        finally:
            cleanup_ingredient(db, ing.id)

    def test_undo_and_restart_prep_does_not_double_consume(self, db, client, kitchen_client):
        """
        NEW → IN_PREP → (undo) NEW → IN_PREP. The ingredients were spent on the
        first start; restarting must not spend them again.
        """
        initial = Decimal("100.000")
        ing, _ = make_ingredient(
            db, on_hand=initial, standard_quantity=Decimal("10.00")
        )
        try:
            oid = _order(db, client, ing.id)
            _patch(kitchen_client, oid, "IN_PREP")
            assert _stock(db, ing.id).on_hand_quantity == initial - Decimal("10.000")

            # Undo back to NEW (inside the undo window).
            assert _patch(kitchen_client, oid, "NEW").status_code == 200
            assert _stock(db, ing.id).on_hand_quantity == initial - Decimal("10.000"), (
                "Undo must not un-cook already consumed ingredients"
            )

            # Start again.
            assert _patch(kitchen_client, oid, "IN_PREP").status_code == 200
            s = _stock(db, ing.id)
            assert s.on_hand_quantity == initial - Decimal("10.000"), (
                "Restarting preparation must not consume a second time"
            )
            assert len(_movements(db, ing.id, "CONSUMPTION")) == 1
        finally:
            cleanup_ingredient(db, ing.id)

    def test_invalid_transition_mutates_no_stock(self, db, client, kitchen_client):
        """A rejected transition is a no-op — not a partial one."""
        initial = Decimal("100.000")
        ing, _ = make_ingredient(
            db, on_hand=initial, standard_quantity=Decimal("10.00")
        )
        try:
            oid = _order(db, client, ing.id)

            # NEW → READY is not a legal transition.
            r = _patch(kitchen_client, oid, "READY")
            assert r.status_code == 409

            s = _stock(db, ing.id)
            assert s.on_hand_quantity == initial
            assert s.reserved_quantity == Decimal("10.000"), "reservation untouched"
            assert len(_movements(db, ing.id, "CONSUMPTION")) == 0
        finally:
            cleanup_ingredient(db, ing.id)

    def test_consumption_is_atomic_with_status_change(self, db, client, kitchen_client):
        """
        If the status write and the stock write could split, the shop would
        either cook without recording it or record without cooking. They commit
        together.
        """
        initial = Decimal("100.000")
        ing, _ = make_ingredient(
            db, on_hand=initial, standard_quantity=Decimal("10.00")
        )
        try:
            oid = _order(db, client, ing.id)
            _patch(kitchen_client, oid, "IN_PREP")

            db.expire_all()
            order = db.get(Order, oid)
            s = _stock(db, ing.id)

            assert order.status == "IN_PREP"
            assert s.on_hand_quantity == initial - Decimal("10.000")
            # Both sides landed, in one transaction.
            assert len(_movements(db, ing.id, "CONSUMPTION")) == 1
        finally:
            cleanup_ingredient(db, ing.id)


# ---------------------------------------------------------------------------
# Cancellation
# ---------------------------------------------------------------------------

class TestCancellation:

    def test_cancel_before_consumption_releases_reservation(self, db, client, kitchen_client):
        initial = Decimal("100.000")
        ing, _ = make_ingredient(
            db, on_hand=initial, standard_quantity=Decimal("10.00")
        )
        try:
            oid = _order(db, client, ing.id)
            assert _patch(kitchen_client, oid, "CANCELLED").status_code == 200

            s = _stock(db, ing.id)
            assert s.reserved_quantity == Decimal("0")
            assert s.on_hand_quantity == initial, "on-hand never moved"
            assert s.available_quantity == initial

            releases = _movements(db, ing.id, "RESERVATION_RELEASED")
            assert len(releases) == 1
            assert releases[0].quantity_delta_on_hand == Decimal("0")

            line = db.query(OrderInventoryLine).filter_by(order_id=oid).first()
            assert line.released_quantity == Decimal("10.000")
            assert line.consumed_quantity == Decimal("0")
        finally:
            cleanup_ingredient(db, ing.id)

    def test_cancel_after_consumption_does_not_restore_stock(self, db, client, kitchen_client):
        initial = Decimal("100.000")
        ing, _ = make_ingredient(
            db, on_hand=initial, standard_quantity=Decimal("10.00")
        )
        try:
            oid = _order(db, client, ing.id)
            _patch(kitchen_client, oid, "IN_PREP")
            assert _patch(kitchen_client, oid, "CANCELLED").status_code == 200

            s = _stock(db, ing.id)
            assert s.on_hand_quantity == initial - Decimal("10.000"), (
                "Consumed ingredients stay consumed — cancelling cannot un-pour batter"
            )
            assert s.reserved_quantity == Decimal("0")

            # No RETURNED movement is fabricated.
            assert len(_movements(db, ing.id, "RETURNED")) == 0
            # Nothing left outstanding to release, so no release row either.
            assert len(_movements(db, ing.id, "RESERVATION_RELEASED")) == 0

            line = db.query(OrderInventoryLine).filter_by(order_id=oid).first()
            assert line.consumed_quantity == Decimal("10.000")
            assert line.released_quantity == Decimal("0")
        finally:
            cleanup_ingredient(db, ing.id)


# ---------------------------------------------------------------------------
# Payment × inventory interaction
# ---------------------------------------------------------------------------

class TestPaymentInteraction:

    def test_paid_order_cancellation_blocked_before_any_stock_mutation(
        self, db, client, kitchen_client, make_staff, make_table
    ):
        """
        The payment guard must fire BEFORE inventory is touched. Otherwise a
        blocked cancellation would still have released the reservation — money
        safe, stock corrupted.
        """
        from tests.conftest import make_authed_client

        initial = Decimal("100.000")
        ing, _ = make_ingredient(
            db, on_hand=initial, standard_quantity=Decimal("10.00")
        )
        cashier = make_staff("CASHIER", store_id=1)
        cashier_client = make_authed_client(db, cashier)
        try:
            oid = _order(db, client, ing.id)

            # Collect money on the order.
            pay = cashier_client.post(
                f"/cashier/orders/{oid}/payments",
                json={"payment_method": "CASH"},
                headers={"Idempotency-Key": uuid.uuid4().hex},
            )
            assert pay.status_code == 200, pay.text

            before = _stock(db, ing.id)
            assert before.reserved_quantity == Decimal("10.000")

            # Cancelling a paid order is refused.
            r = _patch(kitchen_client, oid, "CANCELLED")
            assert r.status_code == 409
            assert r.json()["detail"]["error"] == "payment_outstanding"

            # And crucially: the reservation was NOT released on the way out.
            after = _stock(db, ing.id)
            assert after.reserved_quantity == Decimal("10.000"), (
                "Blocked cancellation must not have mutated inventory"
            )
            assert after.on_hand_quantity == initial
            assert len(_movements(db, ing.id, "RESERVATION_RELEASED")) == 0
        finally:
            purge_payments_for_orders(db, [oid])
            cleanup_ingredient(db, ing.id)

    def test_fully_refunded_unconsumed_order_cancels_and_releases(
        self, db, client, kitchen_client, make_staff
    ):
        from tests.conftest import make_authed_client

        initial = Decimal("100.000")
        ing, _ = make_ingredient(
            db, on_hand=initial, standard_quantity=Decimal("10.00")
        )
        manager = make_staff("MANAGER", store_id=1)
        mgr_client = make_authed_client(db, manager)
        try:
            oid = _order(db, client, ing.id)

            pay = mgr_client.post(
                f"/cashier/orders/{oid}/payments",
                json={"payment_method": "CASH"},
                headers={"Idempotency-Key": uuid.uuid4().hex},
            )
            assert pay.status_code == 200, pay.text
            allocation_id = pay.json()["allocations"][0]["id"]
            amount = pay.json()["allocations"][0]["amount"]

            refund = mgr_client.post(
                f"/cashier/allocations/{allocation_id}/refunds",
                json={"amount": str(amount), "reason": "musteri vazgecti"},
                headers={"Idempotency-Key": uuid.uuid4().hex},
            )
            assert refund.status_code == 200, refund.text

            # Net paid is now zero → cancellation is permitted, and releases.
            r = _patch(kitchen_client, oid, "CANCELLED")
            assert r.status_code == 200, r.text

            s = _stock(db, ing.id)
            assert s.reserved_quantity == Decimal("0")
            assert s.on_hand_quantity == initial
            assert len(_movements(db, ing.id, "RESERVATION_RELEASED")) == 1
        finally:
            purge_payments_for_orders(db, [oid])
            cleanup_ingredient(db, ing.id)

    def test_fully_refunded_consumed_order_cancels_without_restoring(
        self, db, client, kitchen_client, make_staff
    ):
        from tests.conftest import make_authed_client

        initial = Decimal("100.000")
        ing, _ = make_ingredient(
            db, on_hand=initial, standard_quantity=Decimal("10.00")
        )
        manager = make_staff("MANAGER", store_id=1)
        mgr_client = make_authed_client(db, manager)
        try:
            oid = _order(db, client, ing.id)
            _patch(kitchen_client, oid, "IN_PREP")   # consumed

            pay = mgr_client.post(
                f"/cashier/orders/{oid}/payments",
                json={"payment_method": "CASH"},
                headers={"Idempotency-Key": uuid.uuid4().hex},
            )
            assert pay.status_code == 200, pay.text
            allocation_id = pay.json()["allocations"][0]["id"]
            amount = pay.json()["allocations"][0]["amount"]

            refund = mgr_client.post(
                f"/cashier/allocations/{allocation_id}/refunds",
                json={"amount": str(amount), "reason": "urun begenilmedi"},
                headers={"Idempotency-Key": uuid.uuid4().hex},
            )
            assert refund.status_code == 200, refund.text

            r = _patch(kitchen_client, oid, "CANCELLED")
            assert r.status_code == 200, r.text

            # The customer got their money back; the waffle is still in the bin.
            s = _stock(db, ing.id)
            assert s.on_hand_quantity == initial - Decimal("10.000"), (
                "Refunding money does not un-cook the waffle"
            )
            assert len(_movements(db, ing.id, "RETURNED")) == 0
        finally:
            purge_payments_for_orders(db, [oid])
            cleanup_ingredient(db, ing.id)

    def test_payment_does_not_change_stock(self, db, client, make_staff):
        """Collecting money must not move a single gram."""
        from tests.conftest import make_authed_client

        initial = Decimal("100.000")
        ing, _ = make_ingredient(
            db, on_hand=initial, standard_quantity=Decimal("10.00")
        )
        cashier = make_staff("CASHIER", store_id=1)
        cashier_client = make_authed_client(db, cashier)
        try:
            oid = _order(db, client, ing.id)
            before = _stock(db, ing.id)

            pay = cashier_client.post(
                f"/cashier/orders/{oid}/payments",
                json={"payment_method": "CASH"},
                headers={"Idempotency-Key": uuid.uuid4().hex},
            )
            assert pay.status_code == 200, pay.text

            after = _stock(db, ing.id)
            assert after.on_hand_quantity == before.on_hand_quantity
            assert after.reserved_quantity == before.reserved_quantity
        finally:
            purge_payments_for_orders(db, [oid])
            cleanup_ingredient(db, ing.id)
