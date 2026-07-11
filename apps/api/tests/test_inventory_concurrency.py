"""
Inventory concurrency, against real PostgreSQL with real row locks.

Every test here fires genuine simultaneous HTTP requests via threads. There is
no mocking and no sleeping: the guarantees come from SELECT … FOR UPDATE and
database CHECK constraints, not from the frontend disabling a button.

Proven here:
  1. two simultaneous orders cannot reserve the same remaining stock
  2. a concurrent idempotent retry reserves exactly once
  3. two simultaneous start-prep mutations consume exactly once
  4. a cancel/start-prep race cannot both release and consume the same reservation
  5. concurrent manual adjustments cannot corrupt the summary
  6. multi-ingredient orders lock in a deterministic order (no deadlock)
"""
import threading
import uuid
from decimal import Decimal

import pytest

from app.models.ingredient_stock import (
    IngredientStock,
    IngredientStockMovement,
    OrderInventoryLine,
)
from tests.conftest import (
    cleanup_ingredient,
    cleanup_orders_for_ingredient,
    make_authed_client,
    make_ingredient,
    order_payload,
)


def _stock(db, ing_id: int) -> IngredientStock:
    db.expire_all()
    return db.query(IngredientStock).filter_by(ingredient_id=ing_id).first()


def _run(fns) -> None:
    threads = [threading.Thread(target=f) for f in fns]
    for t in threads:
        t.start()
    for t in threads:
        t.join()


# ---------------------------------------------------------------------------
# 1 & 2 — reservation races
# ---------------------------------------------------------------------------

class TestConcurrentReservation:

    def test_concurrent_orders_cannot_over_reserve(self, db, client):
        """Stock fits exactly 2 orders; 6 threads race. Exactly 2 may win."""
        ing, _ = make_ingredient(
            db, on_hand=Decimal("20.000"), standard_quantity=Decimal("10.00")
        )
        try:
            results: list[int] = []
            lock = threading.Lock()

            def fire():
                payload, headers = order_payload(ing.id, idem_key=uuid.uuid4().hex)
                r = client.post("/public/orders/", json=payload, headers=headers)
                with lock:
                    results.append(r.status_code)

            _run([fire] * 6)

            assert results.count(200) == 2, f"Expected exactly 2 winners, got {results}"
            assert results.count(422) == 4

            s = _stock(db, ing.id)
            assert s.reserved_quantity == Decimal("20.000")
            assert s.available_quantity == Decimal("0"), "never oversold"
            assert s.available_quantity >= Decimal("0")
            assert s.on_hand_quantity == Decimal("20.000"), "nothing cooked yet"
        finally:
            cleanup_ingredient(db, ing.id)

    def test_concurrent_idempotent_retry_reserves_once(self, db, client):
        """
        The SAME idempotency key fired 5 ways at once. One order, one
        reservation — the losers must resolve to the winner, not 500.
        """
        ing, _ = make_ingredient(
            db, on_hand=Decimal("100.000"), standard_quantity=Decimal("10.00")
        )
        try:
            idem = uuid.uuid4().hex
            results: list[tuple[int, int | None]] = []
            lock = threading.Lock()

            def fire():
                payload, headers = order_payload(ing.id, idem_key=idem)
                r = client.post("/public/orders/", json=payload, headers=headers)
                oid = r.json().get("order_id") if r.status_code == 200 else None
                with lock:
                    results.append((r.status_code, oid))

            _run([fire] * 5)

            codes = [c for c, _ in results]
            assert all(c == 200 for c in codes), f"All retries must succeed: {results}"

            order_ids = {oid for _, oid in results}
            assert len(order_ids) == 1, f"Retries produced multiple orders: {order_ids}"

            s = _stock(db, ing.id)
            assert s.reserved_quantity == Decimal("10.000"), "reserved exactly once"
            assert db.query(IngredientStockMovement).filter_by(
                ingredient_id=ing.id, movement_type="RESERVATION_CREATED"
            ).count() == 1
            assert db.query(OrderInventoryLine).filter_by(
                ingredient_id=ing.id
            ).count() == 1
        finally:
            cleanup_ingredient(db, ing.id)


# ---------------------------------------------------------------------------
# 3 & 4 — consumption / cancellation races
# ---------------------------------------------------------------------------

class TestConcurrentKitchenTransitions:

    def test_concurrent_start_prep_consumes_once(self, db, client, make_staff):
        """
        Two cooks hit "start" at the same instant. The order may only be cooked
        once, and only one of them may win the transition.
        """
        initial = Decimal("100.000")
        ing, _ = make_ingredient(
            db, on_hand=initial, standard_quantity=Decimal("10.00")
        )
        try:
            payload, headers = order_payload(ing.id, idem_key=uuid.uuid4().hex)
            oid = client.post("/public/orders/", json=payload, headers=headers).json()["order_id"]

            # Two independent authenticated kitchen clients (two real cooks).
            k1 = make_authed_client(db, make_staff("KITCHEN", store_id=1))
            k2 = make_authed_client(db, make_staff("KITCHEN", store_id=1))

            results: list[int] = []
            lock = threading.Lock()

            def fire(cl):
                def _go():
                    r = cl.patch(
                        f"/kitchen/orders/{oid}/status", json={"status": "IN_PREP"}
                    )
                    with lock:
                        results.append(r.status_code)
                return _go

            _run([fire(k1), fire(k2)])

            assert results.count(200) == 1, (
                f"Exactly one start-prep may win the transition, got {results}"
            )

            s = _stock(db, ing.id)
            assert s.on_hand_quantity == initial - Decimal("10.000"), (
                "Consumed exactly once — not twice"
            )
            assert s.reserved_quantity == Decimal("0")
            assert db.query(IngredientStockMovement).filter_by(
                ingredient_id=ing.id, movement_type="CONSUMPTION"
            ).count() == 1
        finally:
            cleanup_ingredient(db, ing.id)

    def test_cancel_and_start_prep_race_settles_reservation_once(
        self, db, client, make_staff
    ):
        """
        A cancel and a start-prep hit the same order simultaneously. Whoever
        wins, the reservation must be settled exactly ONCE — either consumed or
        released, never both. The database's
        `consumed + released <= reserved` CHECK is the last line of defence.
        """
        initial = Decimal("100.000")
        std = Decimal("10.000")
        ing, _ = make_ingredient(
            db, on_hand=initial, standard_quantity=Decimal("10.00")
        )
        try:
            payload, headers = order_payload(ing.id, idem_key=uuid.uuid4().hex)
            oid = client.post("/public/orders/", json=payload, headers=headers).json()["order_id"]

            k1 = make_authed_client(db, make_staff("KITCHEN", store_id=1))
            k2 = make_authed_client(db, make_staff("KITCHEN", store_id=1))

            results: dict[str, int] = {}
            lock = threading.Lock()

            def fire(cl, status):
                def _go():
                    r = cl.patch(
                        f"/kitchen/orders/{oid}/status", json={"status": status}
                    )
                    with lock:
                        results[status] = r.status_code
                return _go

            _run([fire(k1, "IN_PREP"), fire(k2, "CANCELLED")])

            db.expire_all()
            line = db.query(OrderInventoryLine).filter_by(order_id=oid).one()
            s = _stock(db, ing.id)

            settled = line.consumed_quantity + line.released_quantity
            assert settled == std, (
                f"Reservation must be settled exactly once. "
                f"consumed={line.consumed_quantity} released={line.released_quantity}"
            )
            assert s.reserved_quantity == Decimal("0"), "no reservation may leak"

            # Whichever branch won, the physical books agree with the line.
            if line.consumed_quantity == std:
                assert s.on_hand_quantity == initial - std
            else:
                assert s.on_hand_quantity == initial

            # available is always the identity, whatever happened.
            assert s.available_quantity == s.on_hand_quantity - s.reserved_quantity
        finally:
            cleanup_ingredient(db, ing.id)


# ---------------------------------------------------------------------------
# 5 — manual adjustment races
# ---------------------------------------------------------------------------

class TestConcurrentManualAdjustments:

    def test_concurrent_adjustments_preserve_a_valid_summary(self, db, make_staff):
        """
        Six simultaneous +10 receipts. Lost updates would leave on-hand below
        160; the row lock means every one of them lands.
        """
        ing, _ = make_ingredient(db, on_hand=Decimal("100.000"))
        owners = [
            make_authed_client(db, make_staff("OWNER", store_id=1)) for _ in range(6)
        ]
        try:
            results: list[int] = []
            lock = threading.Lock()

            def fire(cl):
                def _go():
                    r = cl.post(
                        "/inventory/purchase-receipts",
                        json={"ingredient_id": ing.id, "quantity": "10.000"},
                        headers={"Idempotency-Key": uuid.uuid4().hex},
                    )
                    with lock:
                        results.append(r.status_code)
                return _go

            _run([fire(c) for c in owners])

            assert all(c == 200 for c in results), results

            s = _stock(db, ing.id)
            assert s.on_hand_quantity == Decimal("160.000"), (
                f"Lost update: expected 160, got {s.on_hand_quantity}"
            )

            # And the ledger agrees with the summary.
            ledger = sum(
                m.quantity_delta_on_hand
                for m in db.query(IngredientStockMovement).filter_by(ingredient_id=ing.id)
            )
            assert ledger == Decimal("60.000")
        finally:
            cleanup_ingredient(db, ing.id)

    def test_concurrent_same_key_adjustments_apply_once(self, db, make_staff):
        """The same Idempotency-Key fired 5 ways applies exactly one movement."""
        ing, _ = make_ingredient(db, on_hand=Decimal("100.000"))
        owners = [
            make_authed_client(db, make_staff("OWNER", store_id=1)) for _ in range(5)
        ]
        try:
            key = uuid.uuid4().hex
            results: list[int] = []
            lock = threading.Lock()

            def fire(cl):
                def _go():
                    r = cl.post(
                        "/inventory/purchase-receipts",
                        json={"ingredient_id": ing.id, "quantity": "10.000",
                              "reason": "teslimat"},
                        headers={"Idempotency-Key": key},
                    )
                    with lock:
                        results.append(r.status_code)
                return _go

            _run([fire(c) for c in owners])

            assert all(c == 200 for c in results), results
            assert _stock(db, ing.id).on_hand_quantity == Decimal("110.000"), (
                "A concurrent retry storm must deliver the goods exactly once"
            )
            assert db.query(IngredientStockMovement).filter_by(
                ingredient_id=ing.id, movement_type="PURCHASE_RECEIPT"
            ).count() == 1
        finally:
            cleanup_ingredient(db, ing.id)


# ---------------------------------------------------------------------------
# 6 — deadlock avoidance
# ---------------------------------------------------------------------------

class TestDeterministicLockOrdering:

    def test_multi_ingredient_orders_do_not_deadlock(self, db, client):
        """
        Two orders need the same two ingredients in OPPOSITE payload order. If
        stock rows were locked in payload order, the two transactions would take
        the locks head-to-head and deadlock. Locking is by ascending
        ingredient_id instead, so one simply waits for the other.

        A deadlock would surface as a 500 (PostgreSQL aborts a victim), so the
        assertion is simply: nobody gets a 500, and the books stay exact.
        """
        ing_a, _ = make_ingredient(
            db, on_hand=Decimal("1000.000"), standard_quantity=Decimal("10.00")
        )
        ing_b, _ = make_ingredient(
            db, on_hand=Decimal("1000.000"), standard_quantity=Decimal("10.00")
        )
        try:
            results: list[int] = []
            lock = threading.Lock()

            def fire(first, second):
                def _go():
                    payload = {
                        "store_id": 1,
                        "table_id": 1,
                        "items": [{
                            "product_id": 1,
                            "quantity": 1,
                            "ingredients": [
                                {"ingredient_id": first, "quantity": 1},
                                {"ingredient_id": second, "quantity": 1},
                            ],
                        }],
                    }
                    r = client.post(
                        "/public/orders/",
                        json=payload,
                        headers={"Idempotency-Key": uuid.uuid4().hex},
                    )
                    with lock:
                        results.append(r.status_code)
                return _go

            # 12 threads, half in each payload order — maximal deadlock pressure.
            fns = []
            for _ in range(6):
                fns.append(fire(ing_a.id, ing_b.id))
                fns.append(fire(ing_b.id, ing_a.id))
            _run(fns)

            assert 500 not in results, f"Deadlock or crash under lock contention: {results}"
            assert all(c == 200 for c in results), results

            # 12 orders × 10 g of each ingredient.
            for ing in (ing_a, ing_b):
                s = _stock(db, ing.id)
                assert s.reserved_quantity == Decimal("120.000"), (
                    f"ingredient {ing.id}: expected 120 reserved, got {s.reserved_quantity}"
                )
                assert s.on_hand_quantity == Decimal("1000.000")
        finally:
            # These orders span BOTH ingredients, so the order graph must go
            # first — see cleanup_orders_for_ingredient.
            cleanup_orders_for_ingredient(db, ing_a.id)
            cleanup_ingredient(db, ing_a.id)
            cleanup_ingredient(db, ing_b.id)
