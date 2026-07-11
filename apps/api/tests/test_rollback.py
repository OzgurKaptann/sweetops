"""
Rollback tests — prove stock is never mutated when an order fails.

Invariants:
  1. A 422 response means the transaction was rolled back.
  2. Every quantity after a failed order equals what it was before: nothing
     reserved, and certainly nothing consumed.
  3. Partial reservation is impossible — all-or-nothing semantics across every
     ingredient in the order.
  4. No movement records of any kind exist for a failed order.

A successful order RESERVES (it does not consume), so the success-path tests
here assert on reserved/available; physical on-hand only moves when the kitchen
starts cooking. See docs/INVENTORY_LIFECYCLE.md.
"""
import uuid
from decimal import Decimal

import pytest

from tests.conftest import cleanup_ingredient, make_ingredient, order_payload


class TestOutOfStockRejection:

    def test_zero_stock_returns_422(self, db, client):
        """
        An ingredient with zero stock must be rejected before any DB write.
        """
        ing, _ = make_ingredient(db, on_hand=Decimal("0.00"))

        payload, headers = order_payload(ing.id, idem_key=uuid.uuid4().hex)
        r = client.post("/public/orders/", json=payload, headers=headers)

        assert r.status_code == 422
        body = r.json()
        assert body["detail"]["error"] == "out_of_stock"
        assert ing.name in body["detail"]["items"]

        cleanup_ingredient(db, ing.id)

    def test_insufficient_stock_returns_422(self, db, client):
        """
        Order needs 10g; only 5g available → 422.
        """
        ing, _ = make_ingredient(
            db,
            on_hand=Decimal("5.00"),
            standard_quantity=Decimal("10.00"),
        )

        payload, headers = order_payload(ing.id, idem_key=uuid.uuid4().hex)
        r = client.post("/public/orders/", json=payload, headers=headers)

        assert r.status_code == 422
        assert r.json()["detail"]["error"] == "out_of_stock"

        cleanup_ingredient(db, ing.id)

    def test_stock_unchanged_after_failed_order(self, db, client):
        """
        A failed order must leave every quantity exactly as it was — nothing
        reserved, nothing consumed.
        """
        from app.models.ingredient_stock import IngredientStock

        initial = Decimal("8.00")
        ing, _ = make_ingredient(
            db,
            on_hand=initial,
            standard_quantity=Decimal("10.00"),
        )

        payload, headers = order_payload(ing.id, idem_key=uuid.uuid4().hex)
        client.post("/public/orders/", json=payload, headers=headers)

        db.expire_all()
        stock_after = db.query(IngredientStock).filter_by(ingredient_id=ing.id).first()
        assert stock_after.on_hand_quantity == initial, (
            f"On-hand must be unchanged. Before={initial}, After={stock_after.on_hand_quantity}"
        )
        assert stock_after.reserved_quantity == Decimal("0"), "Nothing may be reserved"
        assert stock_after.available_quantity == initial

        cleanup_ingredient(db, ing.id)

    def test_no_movement_record_after_failed_order(self, db, client):
        """
        Failed orders must not produce any stock movement records.
        """
        from app.models.ingredient_stock import IngredientStockMovement

        ing, _ = make_ingredient(
            db,
            on_hand=Decimal("0.00"),
            standard_quantity=Decimal("10.00"),
        )

        payload, headers = order_payload(ing.id, idem_key=uuid.uuid4().hex)
        client.post("/public/orders/", json=payload, headers=headers)

        movements = (
            db.query(IngredientStockMovement)
            .filter_by(ingredient_id=ing.id)
            .count()
        )
        assert movements == 0, f"Expected 0 movement records for failed order, got {movements}"

        cleanup_ingredient(db, ing.id)

    def test_partial_deduction_impossible_two_ingredients(self, db, client):
        """
        Order requires two ingredients: A (sufficient) and B (out of stock).
        Expected: 422, and A's stock must NOT be deducted.
        All-or-nothing atomicity.
        """
        from app.models.ingredient_stock import IngredientStock

        ing_a, _ = make_ingredient(db, on_hand=Decimal("100.00"))
        ing_b, _ = make_ingredient(db, on_hand=Decimal("0.00"))

        stock_a_before = Decimal("100.00")

        payload = {
            "store_id": 1,
            "table_id": 1,
            "items": [
                {
                    "product_id": 1,
                    "quantity": 1,
                    "ingredients": [
                        {"ingredient_id": ing_a.id, "quantity": 1},
                        {"ingredient_id": ing_b.id, "quantity": 1},
                    ],
                }
            ],
        }
        r = client.post(
            "/public/orders/",
            json=payload,
            headers={"Idempotency-Key": uuid.uuid4().hex},
        )

        assert r.status_code == 422
        assert "out_of_stock" in r.json()["detail"]["error"]

        db.expire_all()
        stock_a_after = db.query(IngredientStock).filter_by(ingredient_id=ing_a.id).first()
        assert stock_a_after.on_hand_quantity == stock_a_before, (
            f"Ingredient A must not be partially deducted. "
            f"Before={stock_a_before}, After={stock_a_after.on_hand_quantity}"
        )
        assert stock_a_after.reserved_quantity == Decimal("0"), (
            "Ingredient A must not hold a partial reservation from a rejected order"
        )

        cleanup_ingredient(db, ing_a.id)
        cleanup_ingredient(db, ing_b.id)


class TestSuccessfulOrderReservation:

    def test_stock_reserved_not_consumed_on_success(self, db, client):
        """
        A successful order RESERVES exactly standard_quantity. Physical on-hand
        stock is untouched — nothing has been cooked yet.
        """
        from app.models.ingredient_stock import IngredientStock

        initial = Decimal("50.00")
        std_qty = Decimal("10.00")
        ing, _ = make_ingredient(
            db,
            on_hand=initial,
            standard_quantity=std_qty,
        )

        payload, headers = order_payload(ing.id, idem_key=uuid.uuid4().hex)
        r = client.post("/public/orders/", json=payload, headers=headers)
        assert r.status_code == 200

        db.expire_all()
        stock_after = db.query(IngredientStock).filter_by(ingredient_id=ing.id).first()
        assert stock_after.on_hand_quantity == initial, (
            f"Order creation must NOT consume physical stock. "
            f"Expected on-hand {initial}, got {stock_after.on_hand_quantity}"
        )
        assert stock_after.reserved_quantity == std_qty
        assert stock_after.available_quantity == initial - std_qty

        cleanup_ingredient(db, ing.id)

    def test_idempotent_order_does_not_double_reserve(self, db, client):
        """
        Submitting the same order twice (same Idempotency-Key) must reserve
        stock exactly once — not twice.
        """
        from app.models.ingredient_stock import IngredientStock

        initial = Decimal("50.00")
        std_qty = Decimal("10.00")
        ing, _ = make_ingredient(
            db,
            on_hand=initial,
            standard_quantity=std_qty,
        )

        idem = uuid.uuid4().hex
        payload, headers = order_payload(ing.id, idem_key=idem)

        r1 = client.post("/public/orders/", json=payload, headers=headers)
        r2 = client.post("/public/orders/", json=payload, headers=headers)

        assert r1.status_code == 200
        assert r2.status_code == 200
        assert r1.json()["order_id"] == r2.json()["order_id"], "Must return same order"

        db.expire_all()
        stock_after = db.query(IngredientStock).filter_by(ingredient_id=ing.id).first()
        assert stock_after.reserved_quantity == std_qty, (
            f"Idempotent retry double-reserved stock. "
            f"Expected reserved {std_qty}, got {stock_after.reserved_quantity}"
        )
        assert stock_after.on_hand_quantity == initial
        assert stock_after.available_quantity == initial - std_qty

        cleanup_ingredient(db, ing.id)

    def test_movement_records_correct_after_success(self, db, client):
        """
        A successful order produces exactly one RESERVATION_CREATED movement:
        reserved goes up by standard_quantity, on-hand does not move at all.
        """
        from app.models.ingredient_stock import IngredientStockMovement

        std_qty = Decimal("10.00")
        ing, _ = make_ingredient(
            db,
            on_hand=Decimal("50.00"),
            standard_quantity=std_qty,
        )

        idem = uuid.uuid4().hex
        payload, headers = order_payload(ing.id, idem_key=idem)
        r = client.post("/public/orders/", json=payload, headers=headers)
        assert r.status_code == 200

        movements = (
            db.query(IngredientStockMovement)
            .filter_by(ingredient_id=ing.id, movement_type="RESERVATION_CREATED")
            .all()
        )
        assert len(movements) == 1, f"Expected 1 movement, got {len(movements)}"
        assert Decimal(str(movements[0].quantity)) == std_qty
        assert Decimal(str(movements[0].quantity_delta_reserved)) == std_qty
        assert Decimal(str(movements[0].quantity_delta_on_hand)) == Decimal("0"), (
            "Reserving must not move physical stock"
        )

        # And no consumption happened at creation time.
        assert db.query(IngredientStockMovement).filter_by(
            ingredient_id=ing.id, movement_type="CONSUMPTION"
        ).count() == 0

        cleanup_ingredient(db, ing.id)
