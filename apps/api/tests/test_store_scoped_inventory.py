"""
Store-scoped inventory.

The property under test, in one sentence: a jar of chocolate in Kadıköy is not a
jar of chocolate in Beşiktaş, and nothing in SweetOps may pretend otherwise.

Everything here is built on the same shape — ONE catalog ingredient, TWO stores,
TWO independent physical quantities — because that is the shape the old global
model could not represent, and every bug it caused was a variation on collapsing
those two quantities into one.

The failure mode being guarded against is not a crash. It is a plausible-looking
number: Store A's order silently eating Store B's stock, Store A's dashboard
quietly showing Store B's shelves, Store A's shortage cancelling out against
Store B's surplus in a reconciliation total. None of those raise an exception.
They just make the database quietly wrong, which is why several of these tests
go around the service layer entirely and assert that the DATABASE refuses.
"""
import uuid
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.main import app
from app.models.ingredient_stock import (
    IngredientStock,
    IngredientStockMovement,
    OrderInventoryLine,
)
from app.services import inventory_service
from app.services.decision_engine import _slow_moving_signals, _stock_risk_signals
from tests.conftest import (
    DEFAULT_STORE_ID,
    cleanup_ingredient,
    make_authed_client,
    make_ingredient,
    make_store_table_token,
    cleanup_store_table,
    qr_order_payload,
    stock_for,
)

client = TestClient(app)

# Both are integrity failures raised by PostgreSQL; which one surfaces depends on
# whether SQLAlchemy could pre-empt it.
_DB_REJECTS = (IntegrityError, DBAPIError)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

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


def _on_hand(db, store_id: int, ing_id: int) -> Decimal:
    return Decimal(str(_stock(db, store_id, ing_id).on_hand_quantity))


def _reserved(db, store_id: int, ing_id: int) -> Decimal:
    return Decimal(str(_stock(db, store_id, ing_id).reserved_quantity))


def _order_in(db, store_id: int, ing_id: int, *, quantity: int = 1) -> int:
    """Place an order in a store via the legacy path. Returns the order id."""
    payload = {
        "store_id": store_id,
        "items": [{
            "product_id": 1,
            "quantity": quantity,
            "ingredients": [{"ingredient_id": ing_id, "quantity": 1}],
        }],
    }
    r = client.post(
        "/public/orders/", json=payload, headers={"Idempotency-Key": uuid.uuid4().hex}
    )
    assert r.status_code == 200, r.text
    return r.json()["order_id"]


def _idem(key: str | None = None) -> dict:
    return {"Idempotency-Key": key or uuid.uuid4().hex}


@pytest.fixture()
def two_stores(db, make_store):
    """
    One catalog ingredient; 100 g of it in store A, 100 g in store B.

    Two rows, two locks, two truths. Every test below perturbs exactly one of
    them and insists the other does not move.
    """
    store_b = make_store()
    ing, _ = make_ingredient(
        db, on_hand=Decimal("100.000"), standard_quantity=Decimal("10.00"),
        store_id=DEFAULT_STORE_ID,
    )
    stock_for(db, ing, store_b.id, on_hand=Decimal("100.000"))
    yield DEFAULT_STORE_ID, store_b.id, ing
    cleanup_ingredient(db, ing.id)


# ═══════════════════════════════════════════════════════════════════════════
# 1. Model constraints — the database itself refuses to cross stores
# ═══════════════════════════════════════════════════════════════════════════

class TestStoreScopedConstraints:

    def test_same_ingredient_may_exist_in_two_stores(self, db, two_stores):
        """The whole point: one catalog row, two independent physical rows."""
        store_a, store_b, ing = two_stores
        assert _on_hand(db, store_a, ing.id) == Decimal("100.000")
        assert _on_hand(db, store_b, ing.id) == Decimal("100.000")
        assert _stock(db, store_a, ing.id).id != _stock(db, store_b, ing.id).id

    def test_stock_is_unique_per_store_and_ingredient(self, db, two_stores):
        """A second row for the same ingredient in the same store is refused."""
        store_a, _, ing = two_stores
        with pytest.raises(_DB_REJECTS):
            db.add(IngredientStock(
                store_id=store_a,
                ingredient_id=ing.id,
                on_hand_quantity=Decimal("5.000"),
                unit=ing.unit,
            ))
            db.commit()
        db.rollback()

    def test_movement_cannot_cite_another_stores_stock(self, db, two_stores):
        """
        A movement's (store_id, ingredient_id) must resolve to a real stock row
        in THAT store. Here store B is given a movement for an ingredient it has
        no stock row for — refused by fk_movement_stock_store.
        """
        _, store_b, _ = two_stores
        # An ingredient stocked ONLY in store A.
        other, _ = make_ingredient(db, on_hand=Decimal("10.000"), store_id=DEFAULT_STORE_ID)
        try:
            with pytest.raises(_DB_REJECTS):
                db.execute(text("""
                    INSERT INTO ingredient_stock_movements (
                        store_id, ingredient_id, movement_type, quantity,
                        quantity_delta_on_hand, quantity_delta_reserved,
                        unit, legacy_backfill
                    ) VALUES (
                        :store, :ing, 'PURCHASE_RECEIPT', 1.0, 1.0, 0, 'g', true
                    )
                """), {"store": store_b, "ing": other.id})
                db.commit()
            db.rollback()
        finally:
            db.rollback()
            cleanup_ingredient(db, other.id)

    def test_movement_store_must_match_its_orders_store(self, db, two_stores):
        """A store-B movement cannot be attached to a store-A order."""
        store_a, store_b, ing = two_stores
        oid = _order_in(db, store_a, ing.id)

        with pytest.raises(_DB_REJECTS):
            db.execute(text("""
                INSERT INTO ingredient_stock_movements (
                    store_id, ingredient_id, movement_type, quantity,
                    quantity_delta_on_hand, quantity_delta_reserved,
                    unit, order_id, legacy_backfill
                ) VALUES (
                    :store, :ing, 'CONSUMPTION', 1.0, -1.0, -1.0, 'g', :oid, true
                )
            """), {"store": store_b, "ing": ing.id, "oid": oid})
            db.commit()
        db.rollback()

    def test_order_inventory_line_store_must_match_order_store(self, db, two_stores):
        """A line cannot claim store B while its order lives in store A."""
        store_a, store_b, ing = two_stores
        oid = _order_in(db, store_a, ing.id)
        item_id = db.execute(
            text("SELECT id FROM order_items WHERE order_id = :o LIMIT 1"), {"o": oid}
        ).scalar()

        with pytest.raises(_DB_REJECTS):
            db.add(OrderInventoryLine(
                store_id=store_b,          # ← the lie
                order_id=oid,              # ← an order in store A
                order_item_id=item_id,
                ingredient_id=ing.id,
                reserved_quantity=Decimal("1.000"),
                unit=ing.unit,
            ))
            db.commit()
        db.rollback()

    def test_manual_actor_must_belong_to_the_movement_store(
        self, db, make_staff, two_stores
    ):
        """
        A member of staff can only be recorded as moving stock in their OWN
        store — enforced by the composite FK to users(store_id, id), not by a
        code path that could be forgotten.
        """
        store_a, store_b, ing = two_stores
        staff_a = make_staff("MANAGER", store_id=store_a)

        with pytest.raises(_DB_REJECTS):
            db.execute(text("""
                INSERT INTO ingredient_stock_movements (
                    store_id, ingredient_id, movement_type, quantity,
                    quantity_delta_on_hand, quantity_delta_reserved,
                    unit, reason, actor_user_id
                ) VALUES (
                    :store, :ing, 'WASTE', 1.0, -1.0, 0, 'g', 'dusurdum', :actor
                )
            """), {"store": store_b, "ing": ing.id, "actor": staff_a.id})
            db.commit()
        db.rollback()

    def test_ledger_remains_append_only(self, db, two_stores):
        """Store scoping did not weaken the immutability guard."""
        store_a, _, ing = two_stores
        _order_in(db, store_a, ing.id)

        mid = db.execute(text(
            "SELECT id FROM ingredient_stock_movements "
            "WHERE store_id = :s AND ingredient_id = :i LIMIT 1"
        ), {"s": store_a, "i": ing.id}).scalar()
        assert mid is not None

        with pytest.raises(_DB_REJECTS):
            db.execute(
                text("UPDATE ingredient_stock_movements SET quantity = 999 WHERE id = :i"),
                {"i": mid},
            )
            db.commit()
        db.rollback()

        with pytest.raises(_DB_REJECTS):
            db.execute(
                text("DELETE FROM ingredient_stock_movements WHERE id = :i"), {"i": mid}
            )
            db.commit()
        db.rollback()


# ═══════════════════════════════════════════════════════════════════════════
# 2. Order creation — a QR order reserves only its own store's stock
# ═══════════════════════════════════════════════════════════════════════════

class TestOrderCreationIsStoreScoped:

    def test_order_reserves_only_its_own_store(self, db, two_stores):
        store_a, store_b, ing = two_stores
        _order_in(db, store_a, ing.id)

        # Store A gave up 10 g of availability; store B did not move at all.
        assert _reserved(db, store_a, ing.id) == Decimal("10.000")
        assert _reserved(db, store_b, ing.id) == Decimal("0.000")
        assert _on_hand(db, store_b, ing.id) == Decimal("100.000")

    def test_each_store_reserves_from_its_own_shelf(self, db, two_stores):
        store_a, store_b, ing = two_stores
        _order_in(db, store_a, ing.id)
        _order_in(db, store_b, ing.id, quantity=2)

        assert _reserved(db, store_a, ing.id) == Decimal("10.000")
        assert _reserved(db, store_b, ing.id) == Decimal("20.000")

    def test_availability_is_independent_across_stores(self, db, make_store):
        """
        The headline property. Store A is sold out of the very same ingredient
        Store B has plenty of — and Store A's order is rejected while Store B's
        succeeds.
        """
        store_b = make_store()
        ing, _ = make_ingredient(
            db, on_hand=Decimal("5.000"), standard_quantity=Decimal("10.00"),
            store_id=DEFAULT_STORE_ID,          # not enough for one waffle
        )
        stock_for(db, ing, store_b.id, on_hand=Decimal("500.000"))  # plenty
        try:
            # Store A cannot serve it...
            r_a = client.post("/public/orders/", json={
                "store_id": DEFAULT_STORE_ID,
                "items": [{"product_id": 1, "quantity": 1,
                           "ingredients": [{"ingredient_id": ing.id, "quantity": 1}]}],
            }, headers=_idem())
            assert r_a.status_code == 422
            assert r_a.json()["detail"]["error"] == "out_of_stock"

            # ...and crucially it did NOT quietly borrow store B's 500 g.
            assert _on_hand(db, store_b.id, ing.id) == Decimal("500.000")
            assert _reserved(db, store_b.id, ing.id) == Decimal("0.000")

            # Store B, with the same ingredient, serves it happily.
            _order_in(db, store_b.id, ing.id)
            assert _reserved(db, store_b.id, ing.id) == Decimal("10.000")
        finally:
            cleanup_ingredient(db, ing.id)

    def test_qr_order_reserves_the_tokens_store(self, db):
        """Store context comes from the scanned token, and stock follows it."""
        store, table, record, raw = make_store_table_token(db)
        ing, _ = make_ingredient(
            db, on_hand=Decimal("50.000"), standard_quantity=Decimal("10.00"),
            store_id=store.id,
        )
        stock_for(db, ing, DEFAULT_STORE_ID, on_hand=Decimal("50.000"))
        try:
            payload, headers = qr_order_payload(ing.id, raw, idem_key=uuid.uuid4().hex)
            r = client.post("/public/orders/", json=payload, headers=headers)
            assert r.status_code == 200
            assert r.json()["store_id"] == store.id

            # The QR table's store paid for it; the other store did not.
            assert _reserved(db, store.id, ing.id) == Decimal("10.000")
            assert _reserved(db, DEFAULT_STORE_ID, ing.id) == Decimal("0.000")
        finally:
            cleanup_ingredient(db, ing.id)
            cleanup_store_table(db, store.id, table.id)

    def test_idempotent_replay_does_not_double_reserve(self, db, two_stores):
        store_a, store_b, ing = two_stores
        key = uuid.uuid4().hex
        payload = {
            "store_id": store_a,
            "items": [{"product_id": 1, "quantity": 1,
                       "ingredients": [{"ingredient_id": ing.id, "quantity": 1}]}],
        }
        r1 = client.post("/public/orders/", json=payload, headers=_idem(key))
        r2 = client.post("/public/orders/", json=payload, headers=_idem(key))
        assert r1.status_code == r2.status_code == 200
        assert r1.json()["order_id"] == r2.json()["order_id"]

        assert _reserved(db, store_a, ing.id) == Decimal("10.000")   # once, not twice
        assert _reserved(db, store_b, ing.id) == Decimal("0.000")

    def test_lines_and_movements_carry_the_orders_store(self, db, two_stores):
        store_a, store_b, ing = two_stores
        oid = _order_in(db, store_a, ing.id)

        lines = db.query(OrderInventoryLine).filter(
            OrderInventoryLine.order_id == oid
        ).all()
        assert lines and all(ln.store_id == store_a for ln in lines)

        movements = db.query(IngredientStockMovement).filter(
            IngredientStockMovement.order_id == oid
        ).all()
        assert movements and all(m.store_id == store_a for m in movements)
        assert not any(m.store_id == store_b for m in movements)


# ═══════════════════════════════════════════════════════════════════════════
# 3. Kitchen and cancellation
# ═══════════════════════════════════════════════════════════════════════════

class TestKitchenIsStoreScoped:

    def test_start_prep_consumes_only_the_orders_store(
        self, db, make_staff, two_stores
    ):
        store_a, store_b, ing = two_stores
        oid = _order_in(db, store_a, ing.id)
        c = make_authed_client(db, make_staff("KITCHEN", store_id=store_a))

        r = c.patch(f"/kitchen/orders/{oid}/status", json={"status": "IN_PREP"})
        assert r.status_code == 200

        # Store A really burned the batter...
        assert _on_hand(db, store_a, ing.id) == Decimal("90.000")
        assert _reserved(db, store_a, ing.id) == Decimal("0.000")
        # ...and store B's shelf is untouched, on hand AND reserved.
        assert _on_hand(db, store_b, ing.id) == Decimal("100.000")
        assert _reserved(db, store_b, ing.id) == Decimal("0.000")

    def test_cross_store_kitchen_mutation_is_blocked(
        self, db, make_staff, two_stores
    ):
        """Store B's kitchen cannot start store A's order, so cannot consume it."""
        store_a, store_b, ing = two_stores
        oid = _order_in(db, store_a, ing.id)
        c_b = make_authed_client(db, make_staff("KITCHEN", store_id=store_b))

        r = c_b.patch(f"/kitchen/orders/{oid}/status", json={"status": "IN_PREP"})
        assert r.status_code in (403, 404)

        # Nothing was consumed anywhere.
        assert _on_hand(db, store_a, ing.id) == Decimal("100.000")
        assert _reserved(db, store_a, ing.id) == Decimal("10.000")   # still merely promised
        assert _on_hand(db, store_b, ing.id) == Decimal("100.000")

    def test_repeated_status_update_does_not_double_consume(
        self, db, make_staff, two_stores
    ):
        store_a, store_b, ing = two_stores
        oid = _order_in(db, store_a, ing.id)
        c = make_authed_client(db, make_staff("KITCHEN", store_id=store_a))

        c.patch(f"/kitchen/orders/{oid}/status", json={"status": "IN_PREP"})
        c.patch(f"/kitchen/orders/{oid}/status", json={"status": "READY"})
        c.patch(f"/kitchen/orders/{oid}/status", json={"status": "DELIVERED"})

        assert _on_hand(db, store_a, ing.id) == Decimal("90.000")     # deducted once
        assert _on_hand(db, store_b, ing.id) == Decimal("100.000")

    def test_cancellation_releases_only_the_orders_store(
        self, db, make_staff, two_stores
    ):
        store_a, store_b, ing = two_stores
        oid = _order_in(db, store_a, ing.id)
        c = make_authed_client(db, make_staff("KITCHEN", store_id=store_a))

        assert _reserved(db, store_a, ing.id) == Decimal("10.000")
        r = c.patch(f"/kitchen/orders/{oid}/status", json={"status": "CANCELLED"})
        assert r.status_code == 200

        # Store A's promise is released; nothing physical moved, in either store.
        assert _reserved(db, store_a, ing.id) == Decimal("0.000")
        assert _on_hand(db, store_a, ing.id) == Decimal("100.000")
        assert _reserved(db, store_b, ing.id) == Decimal("0.000")
        assert _on_hand(db, store_b, ing.id) == Decimal("100.000")

    def test_cancel_after_consumption_restores_no_stock_in_either_store(
        self, db, make_staff, two_stores
    ):
        store_a, store_b, ing = two_stores
        oid = _order_in(db, store_a, ing.id)
        c = make_authed_client(db, make_staff("KITCHEN", store_id=store_a))

        c.patch(f"/kitchen/orders/{oid}/status", json={"status": "IN_PREP"})
        c.patch(f"/kitchen/orders/{oid}/status", json={"status": "CANCELLED"})

        # The batter was really poured: cancelling cannot un-pour it, and it
        # certainly cannot un-pour it into the other branch.
        assert _on_hand(db, store_a, ing.id) == Decimal("90.000")
        assert _on_hand(db, store_b, ing.id) == Decimal("100.000")


# ═══════════════════════════════════════════════════════════════════════════
# 4. Manual inventory operations
# ═══════════════════════════════════════════════════════════════════════════

class TestManualOperationsAreStoreScoped:

    def test_purchase_receipt_affects_only_the_actors_store(
        self, db, make_staff, two_stores
    ):
        store_a, store_b, ing = two_stores
        c = make_authed_client(db, make_staff("MANAGER", store_id=store_a))

        r = c.post("/inventory/purchase-receipts", headers=_idem(), json={
            "ingredient_id": ing.id, "quantity": "25.000", "reason": "mal kabul",
        })
        assert r.status_code == 200, r.text
        assert r.json()["store_id"] == store_a

        assert _on_hand(db, store_a, ing.id) == Decimal("125.000")
        assert _on_hand(db, store_b, ing.id) == Decimal("100.000")   # untouched

    def test_waste_and_adjustment_affect_only_the_actors_store(
        self, db, make_staff, two_stores
    ):
        store_a, store_b, ing = two_stores
        c = make_authed_client(db, make_staff("MANAGER", store_id=store_a))

        c.post("/inventory/waste", headers=_idem(), json={
            "ingredient_id": ing.id, "quantity": "4.000", "reason": "yandi",
        })
        c.post("/inventory/manual-adjustments", headers=_idem(), json={
            "ingredient_id": ing.id, "delta": "-6.000", "reason": "sayim farki",
        })

        assert _on_hand(db, store_a, ing.id) == Decimal("90.000")
        assert _on_hand(db, store_b, ing.id) == Decimal("100.000")

    def test_client_supplied_store_id_in_body_is_ignored(
        self, db, make_staff, two_stores
    ):
        """
        The store is the SESSION's, full stop. A body that names another store is
        not an error to be caught — it is simply not read, so the receipt lands in
        the caller's own store regardless.
        """
        store_a, store_b, ing = two_stores
        c = make_authed_client(db, make_staff("MANAGER", store_id=store_a))

        r = c.post("/inventory/purchase-receipts", headers=_idem(), json={
            "ingredient_id": ing.id,
            "quantity": "30.000",
            "reason": "mal kabul",
            "store_id": store_b,          # ← the attack
        })
        assert r.status_code == 200
        assert r.json()["store_id"] == store_a       # not store_b

        assert _on_hand(db, store_a, ing.id) == Decimal("130.000")
        assert _on_hand(db, store_b, ing.id) == Decimal("100.000")   # never touched

    def test_store_a_staff_cannot_see_store_b_movements(
        self, db, make_staff, two_stores
    ):
        store_a, store_b, ing = two_stores
        c_a = make_authed_client(db, make_staff("MANAGER", store_id=store_a))
        c_b = make_authed_client(db, make_staff("MANAGER", store_id=store_b))

        c_b.post("/inventory/waste", headers=_idem(), json={
            "ingredient_id": ing.id, "quantity": "7.000", "reason": "sadece B",
        })

        a_reasons = [m["reason"] for m in c_a.get("/inventory/movements").json()["items"]]
        b_reasons = [m["reason"] for m in c_b.get("/inventory/movements").json()["items"]]
        assert "sadece B" in b_reasons
        assert "sadece B" not in a_reasons

    def test_stock_read_shows_only_own_store(self, db, make_staff, two_stores):
        store_a, store_b, ing = two_stores
        c_a = make_authed_client(db, make_staff("MANAGER", store_id=store_a))
        c_b = make_authed_client(db, make_staff("MANAGER", store_id=store_b))

        c_a.post("/inventory/purchase-receipts", headers=_idem(), json={
            "ingredient_id": ing.id, "quantity": "50.000", "reason": "mal kabul",
        })

        def on_hand_via_api(c) -> float:
            rows = c.get("/inventory/stock").json()["items"]
            return float(next(x for x in rows if x["ingredient_id"] == ing.id)["on_hand_quantity"])

        assert on_hand_via_api(c_a) == 150.0
        assert on_hand_via_api(c_b) == 100.0

    def test_stock_not_configured_for_this_store_is_a_404_not_a_fallback(
        self, db, make_store, make_staff
    ):
        """
        A store that does not stock an ingredient gets a clear "not configured
        here" — never another store's row. Stock is initialised per store
        explicitly; nothing is inherited.
        """
        store_b = make_store()
        ing, _ = make_ingredient(db, on_hand=Decimal("80.000"), store_id=DEFAULT_STORE_ID)
        try:
            c_b = make_authed_client(db, make_staff("MANAGER", store_id=store_b.id))
            r = c_b.post("/inventory/manual-adjustments", headers=_idem(), json={
                "ingredient_id": ing.id, "delta": "-5.000", "reason": "sayim",
            })
            assert r.status_code == 404
            assert r.json()["detail"]["error"] == "stock_not_configured"

            # Store A's stock was of course not touched by store B's attempt.
            assert _on_hand(db, DEFAULT_STORE_ID, ing.id) == Decimal("80.000")
        finally:
            cleanup_ingredient(db, ing.id)

    def test_cashier_has_no_inventory_access(self, db, make_staff, two_stores):
        store_a, _, ing = two_stores
        c = make_authed_client(db, make_staff("CASHIER", store_id=store_a))
        assert c.get("/inventory/stock").status_code == 403
        assert c.post("/inventory/waste", headers=_idem(), json={
            "ingredient_id": ing.id, "quantity": "1.000", "reason": "x",
        }).status_code == 403


class TestManualIdempotencyIsStoreScoped:

    def test_same_key_replays_within_one_store(self, db, make_staff, two_stores):
        store_a, _, ing = two_stores
        c = make_authed_client(db, make_staff("MANAGER", store_id=store_a))
        key = uuid.uuid4().hex
        body = {"ingredient_id": ing.id, "quantity": "10.000", "reason": "mal kabul"}

        r1 = c.post("/inventory/purchase-receipts", headers=_idem(key), json=body)
        r2 = c.post("/inventory/purchase-receipts", headers=_idem(key), json=body)
        assert r1.status_code == r2.status_code == 200
        assert r2.json()["idempotent_replay"] is True
        assert r1.json()["movement_id"] == r2.json()["movement_id"]

        assert _on_hand(db, store_a, ing.id) == Decimal("110.000")   # applied once

    def test_same_key_different_payload_is_409_within_a_store(
        self, db, make_staff, two_stores
    ):
        store_a, _, ing = two_stores
        c = make_authed_client(db, make_staff("MANAGER", store_id=store_a))
        key = uuid.uuid4().hex

        c.post("/inventory/purchase-receipts", headers=_idem(key), json={
            "ingredient_id": ing.id, "quantity": "10.000", "reason": "mal kabul",
        })
        r = c.post("/inventory/purchase-receipts", headers=_idem(key), json={
            "ingredient_id": ing.id, "quantity": "99.000", "reason": "mal kabul",
        })
        assert r.status_code == 409
        assert r.json()["detail"]["error"] == "idempotency_mismatch"

    def test_the_same_key_is_independent_in_two_stores(
        self, db, make_staff, two_stores
    ):
        """
        Two managers working from the same printed run-book send the same
        Idempotency-Key on the same day. That is a coincidence, not a replay —
        and if it were treated as one, Beşiktaş's delivery would silently vanish.
        Both receipts must land.
        """
        store_a, store_b, ing = two_stores
        c_a = make_authed_client(db, make_staff("MANAGER", store_id=store_a))
        c_b = make_authed_client(db, make_staff("MANAGER", store_id=store_b))
        # One key, deliberately used by BOTH stores. Fresh per run only so that a
        # previous run's ledger row (the ledger is append-only — nothing deletes
        # it) is not mistaken for today's replay.
        shared_key = f"gunluk-mal-kabul-{uuid.uuid4().hex}"
        body = {"ingredient_id": ing.id, "quantity": "40.000", "reason": "mal kabul"}

        r_a = c_a.post("/inventory/purchase-receipts", headers=_idem(shared_key), json=body)
        r_b = c_b.post("/inventory/purchase-receipts", headers=_idem(shared_key), json=body)

        assert r_a.status_code == r_b.status_code == 200
        assert r_a.json()["idempotent_replay"] is False
        assert r_b.json()["idempotent_replay"] is False       # NOT a replay of A's
        assert r_a.json()["movement_id"] != r_b.json()["movement_id"]

        # Both branches really received their goods.
        assert _on_hand(db, store_a, ing.id) == Decimal("140.000")
        assert _on_hand(db, store_b, ing.id) == Decimal("140.000")

    def test_raw_key_is_never_stored(self, db, make_staff, two_stores):
        store_a, _, ing = two_stores
        c = make_authed_client(db, make_staff("MANAGER", store_id=store_a))
        raw = f"raw-secret-{uuid.uuid4().hex}"
        c.post("/inventory/purchase-receipts", headers=_idem(raw), json={
            "ingredient_id": ing.id, "quantity": "1.000", "reason": "mal kabul",
        })
        hit = db.execute(text(
            "SELECT count(*) FROM ingredient_stock_movements "
            "WHERE idempotency_key_hash = :raw OR request_hash = :raw"
        ), {"raw": raw}).scalar()
        assert hit == 0


# ═══════════════════════════════════════════════════════════════════════════
# 5. Analytics
# ═══════════════════════════════════════════════════════════════════════════

class TestAnalyticsAreStoreScoped:

    def test_stockout_risk_uses_each_stores_own_available_quantity(
        self, db, make_store, make_staff
    ):
        """
        Store A is nearly out and burning fast; store B is comfortable. The two
        stores must get two different verdicts about the SAME ingredient.
        """
        store_b = make_store()
        make_staff("OWNER", store_id=store_b.id)   # a second operational store
        ing, _ = make_ingredient(
            db, on_hand=Decimal("12.000"), standard_quantity=Decimal("10.00"),
            store_id=DEFAULT_STORE_ID,
        )
        stock_for(db, ing, store_b.id, on_hand=Decimal("5000.000"))
        try:
            # Consumption in store A only, to give store A a burn rate.
            oid = _order_in(db, DEFAULT_STORE_ID, ing.id)
            c = make_authed_client(db, make_staff("KITCHEN", store_id=DEFAULT_STORE_ID))
            c.patch(f"/kitchen/orders/{oid}/status", json={"status": "IN_PREP"})

            def signal_for(store_id):
                return next(
                    (s for s in _stock_risk_signals(db, store_id)
                     if s["data"]["ingredient_id"] == ing.id),
                    None,
                )

            a = signal_for(DEFAULT_STORE_ID)
            b = signal_for(store_b.id)

            # Store A: at risk, judged on ITS 2 g remaining (12 − 10 consumed).
            assert a is not None
            assert a["data"]["available_quantity"] == pytest.approx(2.0)
            # Store B: 5 kg on the shelf and nothing consumed — no signal at all,
            # and certainly not one computed from store A's burn rate.
            assert b is None
        finally:
            cleanup_ingredient(db, ing.id)

    def test_consumption_velocity_excludes_other_stores_movements(
        self, db, make_store, make_staff
    ):
        """
        Store B consumes heavily; store A consumes nothing. Store A's velocity
        must be zero — a burn rate borrowed from another branch would cry
        stockout in a shop that is selling nothing.
        """
        store_b = make_store()
        ing, _ = make_ingredient(
            db, on_hand=Decimal("60.000"), standard_quantity=Decimal("10.00"),
            store_id=DEFAULT_STORE_ID,
        )
        stock_for(db, ing, store_b.id, on_hand=Decimal("60.000"))
        try:
            oid_b = _order_in(db, store_b.id, ing.id, quantity=5)   # 50 g in store B
            c_b = make_authed_client(db, make_staff("KITCHEN", store_id=store_b.id))
            c_b.patch(f"/kitchen/orders/{oid_b}/status", json={"status": "IN_PREP"})

            a = next(
                (s for s in _stock_risk_signals(db, DEFAULT_STORE_ID)
                 if s["data"]["ingredient_id"] == ing.id),
                None,
            )
            # Store A has 60 g and a zero burn rate → no stockout risk.
            assert a is None

            # And store A shows up as SLOW-MOVING, precisely because store B's
            # consumption is not credited to it.
            slow = [
                s for s in _slow_moving_signals(db, DEFAULT_STORE_ID)
                if s["data"]["ingredient_id"] == ing.id
            ]
            assert slow, "store A's untouched stock must read as slow-moving"
        finally:
            cleanup_ingredient(db, ing.id)

    def test_waste_metrics_do_not_cross_stores(self, db, make_staff, two_stores):
        store_a, store_b, ing = two_stores
        c_b = make_authed_client(db, make_staff("MANAGER", store_id=store_b))
        c_b.post("/inventory/waste", headers=_idem(), json={
            "ingredient_id": ing.id, "quantity": "30.000", "reason": "dolap bozuldu",
        })

        def waste_total(store_id) -> Decimal:
            return Decimal(str(db.execute(text("""
                SELECT COALESCE(SUM(quantity), 0) FROM ingredient_stock_movements
                WHERE store_id = :s AND ingredient_id = :i AND movement_type = 'WASTE'
            """), {"s": store_id, "i": ing.id}).scalar()))

        assert waste_total(store_b) == Decimal("30.000")
        assert waste_total(store_a) == Decimal("0")     # A wasted nothing

    def test_missing_stock_in_one_store_never_reads_another_stores_stock(
        self, db, make_store, make_staff
    ):
        """
        Store B does not stock this ingredient at all. Its analytics must report
        nothing for it — NOT store A's quantity.
        """
        store_b = make_store()
        ing, _ = make_ingredient(db, on_hand=Decimal("77.000"), store_id=DEFAULT_STORE_ID)
        try:
            signals = _stock_risk_signals(db, store_b.id)
            assert not any(s["data"]["ingredient_id"] == ing.id for s in signals)

            c_b = make_authed_client(db, make_staff("OWNER", store_id=store_b.id))
            rows = c_b.get("/owner/stock-status").json()["items"]
            assert not any(r["ingredient_id"] == ing.id for r in rows)
        finally:
            cleanup_ingredient(db, ing.id)
