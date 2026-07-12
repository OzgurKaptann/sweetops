"""
Transfer atomicity and concurrency.

A transfer touches TWO stores' stock rows, which is one more than anything else in
the system does. That buys two new hazards, and both of them are silent:

  1. HALF A TRANSFER. The source's on-hand falls, the destination's never rises,
     and 2 kg of chocolate stops existing — with a perfectly self-consistent
     ledger and summary in each branch. Nothing raises. Nothing reconciles wrong
     per-store. The stock is just gone.

  2. OVERDRAW. Two managers (or a manager and a customer's order, or a manager and
     a colleague recording waste) each read the same available quantity, each
     decide there is enough, and each act. The shop has now promised or shipped
     stock it does not have.

Both are prevented by the same two mechanisms, and these tests exist to hold them
to it: ONE transaction per transfer, and a FOR UPDATE row lock on every stock row
the transfer touches, taken in ascending (store_id, ingredient_id) order.

The ordering matters as much as the locking. Two managers shipping chocolate to
each other at the same instant — Kadıköy → Beşiktaş and Beşiktaş → Kadıköy — would
each hold the row the other needs if the source were locked first, and PostgreSQL
would kill one of them with a deadlock. Ordering by store_id means both reach for
the lower-numbered store first, so one simply waits.

These tests use real threads against the real database, because a mocked lock
proves nothing about PostgreSQL's.
"""
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.core.config import settings
from app.core.db import SessionLocal
from app.main import app
from app.models.ingredient_stock import (
    MOVEMENT_TRANSFER_IN,
    MOVEMENT_TRANSFER_OUT,
    IngredientStock,
    IngredientStockMovement,
)
from app.models.inventory_transfer import InventoryTransfer
from app.services import auth_service, inventory_service
from tests.conftest import (
    DEFAULT_STORE_ID,
    cleanup_ingredient,
    make_authed_client,
    make_ingredient,
    order_payload,
    stock_for,
)


def _key() -> str:
    return uuid.uuid4().hex


def _stock(db, store_id: int, ing_id: int) -> IngredientStock | None:
    db.expire_all()
    return (
        db.query(IngredientStock)
        .filter(
            IngredientStock.store_id == store_id,
            IngredientStock.ingredient_id == ing_id,
        )
        .first()
    )


def _count_movements(db, ing_id: int, movement_type: str) -> int:
    db.expire_all()
    return (
        db.query(IngredientStockMovement)
        .filter(
            IngredientStockMovement.ingredient_id == ing_id,
            IngredientStockMovement.movement_type == movement_type,
        )
        .count()
    )


def _session_client(db, user) -> TestClient:
    """A TestClient with its own session — safe to drive from a worker thread."""
    return make_authed_client(db, user)


def _body(dest_id: int, ing_id: int, qty: str) -> dict:
    return {
        "destination_store_id": dest_id,
        "ingredient_id": ing_id,
        "quantity": qty,
        "reason": "şube takviyesi",
    }


@pytest.fixture()
def transfer_setup(db, make_store, make_staff):
    dest = make_store("Beşiktaş")
    owner = make_staff("OWNER", store_id=DEFAULT_STORE_ID)
    return owner, DEFAULT_STORE_ID, dest.id


# ═══════════════════════════════════════════════════════════════════════════
# Atomicity
# ═══════════════════════════════════════════════════════════════════════════

class TestAtomicity:

    def test_failure_after_the_source_lock_moves_no_stock_on_either_side(
        self, db, transfer_setup
    ):
        """
        The all-or-nothing guarantee, forced.

        The transfer is driven directly through the service with a poisoned commit,
        so it fails AFTER the source row has been locked and decremented in memory
        and both ledger legs have been staged. If the transaction were not the unit
        of work, this is precisely the moment 20 g of chocolate would disappear.
        """
        owner, src, dst = transfer_setup
        ing, _ = make_ingredient(db, on_hand=Decimal("100.000"))
        stock_for(db, ing, dst, on_hand=Decimal("30.000"))
        try:
            session = SessionLocal()
            boom = RuntimeError("commit failed after the source was locked")

            def explode():
                raise boom

            session.commit = explode  # type: ignore[method-assign]
            try:
                with pytest.raises(RuntimeError):
                    inventory_service.transfer_stock(
                        session,
                        source_store_id=src,
                        destination_store_id=dst,
                        ingredient_id=ing.id,
                        quantity=Decimal("20.000"),
                        reason="doomed",
                        actor_user_id=owner.id,
                        idempotency_key=_key(),
                    )
            finally:
                session.rollback()
                session.close()

            # Neither shelf moved, and no half-transfer survived.
            assert _stock(db, src, ing.id).on_hand_quantity == Decimal("100.000")
            assert _stock(db, dst, ing.id).on_hand_quantity == Decimal("30.000")
            assert _count_movements(db, ing.id, MOVEMENT_TRANSFER_OUT) == 0
            assert _count_movements(db, ing.id, MOVEMENT_TRANSFER_IN) == 0
            assert db.query(InventoryTransfer).filter(
                InventoryTransfer.ingredient_id == ing.id
            ).count() == 0
        finally:
            cleanup_ingredient(db, ing.id)


# ═══════════════════════════════════════════════════════════════════════════
# Concurrency
# ═══════════════════════════════════════════════════════════════════════════

class TestConcurrency:

    def test_concurrent_transfers_cannot_overdraw_the_source(self, db, transfer_setup):
        """
        50 g on the shelf. Five managers each try to ship 20 g at the same instant.
        At most two can win — and the shelf may never go negative or dip below what
        is reserved.
        """
        owner, src, dst = transfer_setup
        ing, _ = make_ingredient(db, on_hand=Decimal("50.000"))
        stock_for(db, ing, dst, on_hand=Decimal("0.000"))
        try:
            client = _session_client(db, owner)

            def ship(_i: int) -> int:
                return client.post(
                    "/inventory/transfers",
                    json=_body(dst, ing.id, "20.000"),
                    headers={"Idempotency-Key": _key()},
                ).status_code

            with ThreadPoolExecutor(max_workers=5) as pool:
                codes = list(pool.map(ship, range(5)))

            wins = sum(1 for c in codes if c == 200)
            assert wins == 2, f"expected exactly 2 winners, got {codes}"
            assert all(c in (200, 409) for c in codes), codes

            source = _stock(db, src, ing.id)
            destination = _stock(db, dst, ing.id)
            assert source.on_hand_quantity == Decimal("10.000")
            assert source.on_hand_quantity >= Decimal("0")
            # Every gram that left arrived. Nothing evaporated in the race.
            assert destination.on_hand_quantity == Decimal("40.000")
            assert (
                source.on_hand_quantity + destination.on_hand_quantity
                == Decimal("50.000")
            )

            # Exactly one pair of legs per winning transfer.
            assert _count_movements(db, ing.id, MOVEMENT_TRANSFER_OUT) == 2
            assert _count_movements(db, ing.id, MOVEMENT_TRANSFER_IN) == 2
        finally:
            cleanup_ingredient(db, ing.id)

    def test_concurrent_transfer_and_order_reservation_cannot_overdraw(
        self, db, transfer_setup
    ):
        """
        The van and the till, racing for the same 100 g.

        A transfer of 95 g and a customer order reserving 10 g cannot BOTH succeed:
        that would leave the shop having shipped stock it had already promised to a
        waiting customer. Whichever wins, the source's reserved must never exceed
        its on-hand — which the database also enforces (ck_stock_reserved_le_on_hand),
        so a broken lock here surfaces as an integrity error rather than silent
        corruption.
        """
        owner, src, dst = transfer_setup
        ing, _ = make_ingredient(
            db, on_hand=Decimal("100.000"), standard_quantity=Decimal("10.00")
        )
        stock_for(db, ing, dst, on_hand=Decimal("0.000"))
        try:
            staff_client = _session_client(db, owner)
            public_client = TestClient(app)
            results: dict[str, int] = {}
            barrier = threading.Barrier(2)

            def do_transfer():
                barrier.wait()
                results["transfer"] = staff_client.post(
                    "/inventory/transfers",
                    json=_body(dst, ing.id, "95.000"),
                    headers={"Idempotency-Key": _key()},
                ).status_code

            def do_order():
                barrier.wait()
                payload, headers = order_payload(ing.id, idem_key=_key())
                results["order"] = public_client.post(
                    "/public/orders/", json=payload, headers=headers
                ).status_code

            threads = [threading.Thread(target=do_transfer), threading.Thread(target=do_order)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            source = _stock(db, src, ing.id)

            # The invariant, whoever won: the shop never promises or ships what it
            # does not physically have.
            assert source.on_hand_quantity >= Decimal("0")
            assert source.reserved_quantity <= source.on_hand_quantity
            assert source.available_quantity >= Decimal("0")

            transferred = Decimal("95.000") if results.get("transfer") == 200 else Decimal("0")
            reserved = source.reserved_quantity

            # 95 shipped + 10 reserved does not fit in 100. They cannot both win.
            assert not (transferred > 0 and reserved > 0), (
                f"both succeeded and overdrew the shelf: {results}"
            )
            assert source.on_hand_quantity == Decimal("100.000") - transferred
            assert _stock(db, dst, ing.id).on_hand_quantity == transferred
        finally:
            cleanup_ingredient(db, ing.id)

    def test_concurrent_transfer_and_waste_cannot_overdraw(self, db, transfer_setup):
        """
        Two staff, one shelf: one ships 30 g to the other branch while the other
        writes 30 g off as burnt. There are only 50 g. Both cannot win.
        """
        owner, src, dst = transfer_setup
        ing, _ = make_ingredient(db, on_hand=Decimal("50.000"))
        stock_for(db, ing, dst, on_hand=Decimal("0.000"))
        try:
            client = _session_client(db, owner)
            results: dict[str, int] = {}
            barrier = threading.Barrier(2)

            def do_transfer():
                barrier.wait()
                results["transfer"] = client.post(
                    "/inventory/transfers",
                    json=_body(dst, ing.id, "30.000"),
                    headers={"Idempotency-Key": _key()},
                ).status_code

            def do_waste():
                barrier.wait()
                results["waste"] = client.post(
                    "/inventory/waste",
                    json={
                        "ingredient_id": ing.id,
                        "quantity": "30.000",
                        "reason": "yandı",
                    },
                    headers={"Idempotency-Key": _key()},
                ).status_code

            threads = [threading.Thread(target=do_transfer), threading.Thread(target=do_waste)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            source = _stock(db, src, ing.id)
            assert source.on_hand_quantity >= Decimal("0"), results

            transferred = Decimal("30.000") if results.get("transfer") == 200 else Decimal("0")
            wasted = Decimal("30.000") if results.get("waste") == 200 else Decimal("0")

            # 30 + 30 > 50: exactly one of them can have happened.
            assert transferred + wasted <= Decimal("50.000"), results
            assert source.on_hand_quantity == Decimal("50.000") - transferred - wasted
            # Whatever was shipped arrived; what was wasted did not go to the
            # other branch.
            assert _stock(db, dst, ing.id).on_hand_quantity == transferred
        finally:
            cleanup_ingredient(db, ing.id)

    def test_opposing_transfers_between_two_stores_do_not_deadlock(
        self, db, make_store, make_staff
    ):
        """
        THE deadlock case, and the reason locks are ordered by store_id rather than
        "source first".

        Kadıköy ships to Beşiktaş at the same instant Beşiktaş ships to Kadıköy, of
        the same ingredient. Lock the source first and each transaction holds the
        row the other is waiting for: PostgreSQL detects the cycle and kills one
        with a deadlock error. Lock in ascending store_id order and both reach for
        the lower-numbered store first, so one simply queues behind the other.

        Both transfers must therefore SUCCEED — a 500 here is the deadlock.
        """
        store_b = make_store("Beşiktaş")
        owner_a = make_staff("OWNER", store_id=DEFAULT_STORE_ID)
        owner_b = make_staff("OWNER", store_id=store_b.id)

        ing, _ = make_ingredient(db, on_hand=Decimal("100.000"), store_id=DEFAULT_STORE_ID)
        stock_for(db, ing, store_b.id, on_hand=Decimal("100.000"))
        try:
            client_a = _session_client(db, owner_a)
            client_b = _session_client(db, owner_b)
            results: dict[str, int] = {}
            barrier = threading.Barrier(2)

            def a_to_b():
                barrier.wait()
                results["a_to_b"] = client_a.post(
                    "/inventory/transfers",
                    json=_body(store_b.id, ing.id, "10.000"),
                    headers={"Idempotency-Key": _key()},
                ).status_code

            def b_to_a():
                barrier.wait()
                results["b_to_a"] = client_b.post(
                    "/inventory/transfers",
                    json=_body(DEFAULT_STORE_ID, ing.id, "15.000"),
                    headers={"Idempotency-Key": _key()},
                ).status_code

            threads = [threading.Thread(target=a_to_b), threading.Thread(target=b_to_a)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=30)
            assert not any(t.is_alive() for t in threads), "a transfer hung — likely a lock cycle"

            assert results["a_to_b"] == 200, results
            assert results["b_to_a"] == 200, results

            # A sent 10 and received 15; B sent 15 and received 10.
            assert _stock(db, DEFAULT_STORE_ID, ing.id).on_hand_quantity == Decimal("105.000")
            assert _stock(db, store_b.id, ing.id).on_hand_quantity == Decimal("95.000")
            # Nothing was created or destroyed.
            assert (
                _stock(db, DEFAULT_STORE_ID, ing.id).on_hand_quantity
                + _stock(db, store_b.id, ing.id).on_hand_quantity
                == Decimal("200.000")
            )
        finally:
            cleanup_ingredient(db, ing.id)

    def test_concurrent_retry_of_the_same_key_creates_exactly_one_transfer(
        self, db, transfer_setup
    ):
        """
        A flaky network makes a client fire the same request five times at once.
        The chocolate ships ONCE. The four losers replay the winner's result rather
        than erroring — and, critically, rather than shipping again.
        """
        owner, src, dst = transfer_setup
        ing, _ = make_ingredient(db, on_hand=Decimal("100.000"))
        stock_for(db, ing, dst, on_hand=Decimal("0.000"))
        try:
            client = _session_client(db, owner)
            key = _key()
            body = _body(dst, ing.id, "20.000")

            def ship(_i: int):
                r = client.post(
                    "/inventory/transfers", json=body, headers={"Idempotency-Key": key}
                )
                return r.status_code, r.json()

            with ThreadPoolExecutor(max_workers=5) as pool:
                results = list(pool.map(ship, range(5)))

            codes = [c for c, _ in results]
            assert all(c == 200 for c in codes), codes

            # All five agree on ONE transfer and ONE pair of legs.
            transfer_ids = {body_["transfer_id"] for _, body_ in results}
            assert len(transfer_ids) == 1, transfer_ids

            assert db.query(InventoryTransfer).filter(
                InventoryTransfer.ingredient_id == ing.id
            ).count() == 1
            assert _count_movements(db, ing.id, MOVEMENT_TRANSFER_OUT) == 1
            assert _count_movements(db, ing.id, MOVEMENT_TRANSFER_IN) == 1

            # Stock moved exactly once, not five times.
            assert _stock(db, src, ing.id).on_hand_quantity == Decimal("80.000")
            assert _stock(db, dst, ing.id).on_hand_quantity == Decimal("20.000")
        finally:
            cleanup_ingredient(db, ing.id)

    def test_concurrent_first_transfers_into_a_new_branch_do_not_duplicate_its_stock_row(
        self, db, transfer_setup
    ):
        """
        Two transfers of DIFFERENT ingredients arriving at a branch that has never
        held either. Both must materialise their destination row without racing each
        other into a duplicate — the grain (store, ingredient) is what makes
        availability meaningful, and two rows for one pair would let two orders each
        believe they had the last of it.
        """
        owner, _src, dst = transfer_setup
        ing_a, _ = make_ingredient(db, on_hand=Decimal("100.000"))
        ing_b, _ = make_ingredient(db, on_hand=Decimal("100.000"))
        try:
            client = _session_client(db, owner)

            def ship(ing_id: int) -> int:
                return client.post(
                    "/inventory/transfers",
                    json=_body(dst, ing_id, "10.000"),
                    headers={"Idempotency-Key": _key()},
                ).status_code

            with ThreadPoolExecutor(max_workers=2) as pool:
                codes = list(pool.map(ship, [ing_a.id, ing_b.id]))
            assert all(c == 200 for c in codes), codes

            for ing in (ing_a, ing_b):
                rows = (
                    db.query(IngredientStock)
                    .filter(
                        IngredientStock.store_id == dst,
                        IngredientStock.ingredient_id == ing.id,
                    )
                    .count()
                )
                assert rows == 1, f"ingredient {ing.id} got {rows} stock rows in the destination"
                assert _stock(db, dst, ing.id).on_hand_quantity == Decimal("10.000")
        finally:
            cleanup_ingredient(db, ing_a.id)
            cleanup_ingredient(db, ing_b.id)
