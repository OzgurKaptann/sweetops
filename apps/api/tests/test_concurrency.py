"""
Concurrency tests — prove stock never goes negative under simultaneous load.

Strategy:
  - Uses threading.Thread so both requests truly reach PostgreSQL concurrently.
  - The SELECT … FOR UPDATE row lock in order_service.py serialises the stock
    check + deduction.  One thread wins; the other gets 422.
  - No sleep, no mocking — real DB, real locks.

Why TestClient + threading works here:
  Starlette's TestClient runs the ASGI app in a background thread.  Multiple
  test threads calling client.post() simultaneously DO issue concurrent
  requests to that ASGI server, which are each dispatched to their own
  async task and hit PostgreSQL concurrently.  Row locks are real.
"""
import threading
import uuid
from decimal import Decimal

import pytest

from tests.conftest import cleanup_ingredient, make_ingredient, order_payload


class TestStockNeverGoesNegative:
    """
    Core invariant: AVAILABLE stock must never drop below 0 — the shop can never
    promise more than it physically holds. Under the reservation lifecycle,
    order creation moves `reserved`, not `on_hand`, so these tests assert on
    availability (on_hand - reserved), which is what order acceptance gates on.
    """

    def test_two_concurrent_requests_exactly_one_stock_unit(self, db, client):
        """
        Scenario:
          stock = 10g, standard_quantity = 10g/order → exactly 1 order fits.
          Two threads fire simultaneously.
          Expected: exactly 1 succeeds (200), 1 fails (422).
          Stock after: 0 — never negative.
        """
        ing, stock = make_ingredient(
            db,
            on_hand=Decimal("10.00"),
            standard_quantity=Decimal("10.00"),  # 10g per order = fits exactly 1
        )

        results: list[int] = []
        lock = threading.Lock()

        def fire(idem_key: str):
            payload, headers = order_payload(ing.id, idem_key=idem_key)
            r = client.post("/public/orders/", json=payload, headers=headers)
            with lock:
                results.append(r.status_code)

        threads = [
            threading.Thread(target=fire, args=(f"conc-1stock-{uuid.uuid4().hex}",)),
            threading.Thread(target=fire, args=(f"conc-1stock-{uuid.uuid4().hex}",)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        successes = results.count(200)
        failures = results.count(422)

        assert successes == 1, f"Expected exactly 1 success, got {successes}. Results: {results}"
        assert failures == 1, f"Expected exactly 1 failure (422), got {failures}. Results: {results}"

        # Availability is exactly 0 — never negative. Physical stock is untouched:
        # nothing has been cooked yet.
        db.expire_all()
        final_stock = db.query(
            __import__("app.models.ingredient_stock", fromlist=["IngredientStock"]).IngredientStock
        ).filter_by(ingredient_id=ing.id).first()
        assert final_stock.available_quantity == Decimal("0.00"), (
            f"Available should be 0, got {final_stock.available_quantity}"
        )
        assert final_stock.reserved_quantity == Decimal("10.00")
        assert final_stock.on_hand_quantity == Decimal("10.00"), (
            "Reserving must never consume physical stock"
        )

        cleanup_ingredient(db, ing.id)

    def test_five_concurrent_requests_sufficient_stock(self, db, client):
        """
        Scenario:
          stock = 100g, standard_quantity = 10g → fits 10 orders.
          5 threads fire simultaneously — all should succeed.
          Stock after: 100 - (5 × 10) = 50.  Never negative.
        """
        from app.models.ingredient_stock import IngredientStock

        ing, stock = make_ingredient(
            db,
            on_hand=Decimal("100.00"),
            standard_quantity=Decimal("10.00"),
        )

        results: list[int] = []
        lock = threading.Lock()

        def fire(idem_key: str):
            payload, headers = order_payload(ing.id, idem_key=idem_key)
            r = client.post("/public/orders/", json=payload, headers=headers)
            with lock:
                results.append(r.status_code)

        threads = [
            threading.Thread(target=fire, args=(f"conc-5ok-{uuid.uuid4().hex}",))
            for _ in range(5)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert all(s == 200 for s in results), (
            f"All 5 requests should succeed. Got: {results}"
        )

        db.expire_all()
        final_stock = db.query(IngredientStock).filter_by(ingredient_id=ing.id).first()

        expected = Decimal("100.00") - (5 * Decimal("10.00"))  # = 50 available
        assert final_stock.available_quantity == expected, (
            f"Expected available {expected}, got {final_stock.available_quantity}"
        )
        assert final_stock.reserved_quantity == 5 * Decimal("10.00")
        assert final_stock.on_hand_quantity == Decimal("100.00")
        assert final_stock.available_quantity >= Decimal("0"), "Available must not be negative"

        cleanup_ingredient(db, ing.id)

    def test_ten_concurrent_requests_only_N_fit(self, db, client):
        """
        Scenario:
          stock = 30g, standard_quantity = 10g → fits exactly 3 orders.
          10 threads fire simultaneously.
          Expected: exactly 3 succeed, 7 fail with 422.
          Stock after: 0.
        """
        from app.models.ingredient_stock import IngredientStock

        ing, stock = make_ingredient(
            db,
            on_hand=Decimal("30.00"),
            standard_quantity=Decimal("10.00"),
        )

        results: list[int] = []
        lock = threading.Lock()

        def fire(idem_key: str):
            payload, headers = order_payload(ing.id, idem_key=idem_key)
            r = client.post("/public/orders/", json=payload, headers=headers)
            with lock:
                results.append(r.status_code)

        threads = [
            threading.Thread(target=fire, args=(f"conc-3fit-{uuid.uuid4().hex}",))
            for _ in range(10)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        successes = results.count(200)
        failures = results.count(422)

        assert successes == 3, (
            f"Expected exactly 3 successes (stock fits 3 orders). Got {successes}. Results: {results}"
        )
        assert failures == 7, f"Expected 7 failures, got {failures}"

        db.expire_all()
        final_stock = db.query(IngredientStock).filter_by(ingredient_id=ing.id).first()
        assert final_stock.available_quantity == Decimal("0.00"), (
            f"Available should be exactly 0, got {final_stock.available_quantity}"
        )
        assert final_stock.available_quantity >= Decimal("0"), "Available must never go negative"
        assert final_stock.reserved_quantity == Decimal("30.00")

        cleanup_ingredient(db, ing.id)

    def test_movement_log_count_matches_successful_orders(self, db, client):
        """
        Every successful order produces exactly one RESERVATION_CREATED movement.
        Movement count = success count. No phantom reservations.
        """
        from app.models.ingredient_stock import IngredientStockMovement

        ing, stock = make_ingredient(
            db,
            on_hand=Decimal("20.00"),
            standard_quantity=Decimal("10.00"),
        )

        results: list[int] = []
        lock = threading.Lock()

        def fire(idem_key: str):
            payload, headers = order_payload(ing.id, idem_key=idem_key)
            r = client.post("/public/orders/", json=payload, headers=headers)
            with lock:
                results.append(r.status_code)

        threads = [
            threading.Thread(target=fire, args=(f"conc-mvmt-{uuid.uuid4().hex}",))
            for _ in range(4)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        successes = results.count(200)

        db.expire_all()
        movement_count = (
            db.query(IngredientStockMovement)
            .filter_by(
                ingredient_id=ing.id,
                movement_type="RESERVATION_CREATED",
            )
            .count()
        )

        assert movement_count == successes, (
            f"Movement records ({movement_count}) must equal successful orders ({successes})"
        )
        # None of them cooked anything.
        assert db.query(IngredientStockMovement).filter_by(
            ingredient_id=ing.id, movement_type="CONSUMPTION"
        ).count() == 0

        cleanup_ingredient(db, ing.id)
