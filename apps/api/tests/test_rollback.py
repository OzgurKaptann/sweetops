"""
Rollback tests — prove stock is never mutated when an order fails.

Invariants:
  1. A 422 response means the transaction was rolled back.
  2. Stock quantity after a failed order equals stock quantity before.
  3. Partial deduction is impossible — all-or-nothing semantics.
  4. No ORDER_DEDUCTION movement records exist for failed orders.
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
        ing, _ = make_ingredient(db, stock_quantity=Decimal("0.00"))

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
            stock_quantity=Decimal("5.00"),
            standard_quantity=Decimal("10.00"),
        )

        payload, headers = order_payload(ing.id, idem_key=uuid.uuid4().hex)
        r = client.post("/public/orders/", json=payload, headers=headers)

        assert r.status_code == 422
        assert r.json()["detail"]["error"] == "out_of_stock"

        cleanup_ingredient(db, ing.id)

    def test_stock_quantity_unchanged_after_failed_order(self, db, client):
        """
        The stock row must have the exact same value before and after a failed order.
        """
        from app.models.ingredient_stock import IngredientStock

        initial = Decimal("8.00")
        ing, _ = make_ingredient(
            db,
            stock_quantity=initial,
            standard_quantity=Decimal("10.00"),
        )

        payload, headers = order_payload(ing.id, idem_key=uuid.uuid4().hex)
        client.post("/public/orders/", json=payload, headers=headers)

        db.expire_all()
        stock_after = db.query(IngredientStock).filter_by(ingredient_id=ing.id).first()
        assert stock_after.stock_quantity == initial, (
            f"Stock must be unchanged. Before={initial}, After={stock_after.stock_quantity}"
        )

        cleanup_ingredient(db, ing.id)

    def test_no_movement_record_after_failed_order(self, db, client):
        """
        Failed orders must not produce any stock movement records.
        """
        from app.models.ingredient_stock import IngredientStockMovement

        ing, _ = make_ingredient(
            db,
            stock_quantity=Decimal("0.00"),
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

        ing_a, _ = make_ingredient(db, stock_quantity=Decimal("100.00"))
        ing_b, _ = make_ingredient(db, stock_quantity=Decimal("0.00"))

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
        assert stock_a_after.stock_quantity == stock_a_before, (
            f"Ingredient A must not be partially deducted. "
            f"Before={stock_a_before}, After={stock_a_after.stock_quantity}"
        )

        cleanup_ingredient(db, ing_a.id)
        cleanup_ingredient(db, ing_b.id)


class TestSuccessfulOrderDeduction:

    def test_stock_deducted_exactly_on_success(self, db, client):
        """
        Successful order must deduct exactly standard_quantity from stock.
        """
        from app.models.ingredient_stock import IngredientStock

        initial = Decimal("50.00")
        std_qty = Decimal("10.00")
        ing, _ = make_ingredient(
            db,
            stock_quantity=initial,
            standard_quantity=std_qty,
        )

        payload, headers = order_payload(ing.id, idem_key=uuid.uuid4().hex)
        r = client.post("/public/orders/", json=payload, headers=headers)
        assert r.status_code == 200

        db.expire_all()
        stock_after = db.query(IngredientStock).filter_by(ingredient_id=ing.id).first()
        expected = initial - std_qty
        assert stock_after.stock_quantity == expected, (
            f"Expected {expected}, got {stock_after.stock_quantity}"
        )

        cleanup_ingredient(db, ing.id)

    def test_idempotent_order_does_not_double_deduct(self, db, client):
        """
        Submitting the same order twice (same Idempotency-Key) must deduct
        stock exactly once — not twice.
        """
        from app.models.ingredient_stock import IngredientStock

        initial = Decimal("50.00")
        std_qty = Decimal("10.00")
        ing, _ = make_ingredient(
            db,
            stock_quantity=initial,
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
        expected = initial - std_qty  # deducted exactly once
        assert stock_after.stock_quantity == expected, (
            f"Idempotent retry double-deducted stock. "
            f"Expected {expected}, got {stock_after.stock_quantity}"
        )

        cleanup_ingredient(db, ing.id)

    def test_movement_records_correct_after_success(self, db, client):
        """
        Successful order must produce exactly one ORDER_DEDUCTION with
        negative delta equal to standard_quantity.
        """
        from app.models.ingredient_stock import IngredientStockMovement

        std_qty = Decimal("10.00")
        ing, _ = make_ingredient(
            db,
            stock_quantity=Decimal("50.00"),
            standard_quantity=std_qty,
        )

        idem = uuid.uuid4().hex
        payload, headers = order_payload(ing.id, idem_key=idem)
        r = client.post("/public/orders/", json=payload, headers=headers)
        assert r.status_code == 200

        movements = (
            db.query(IngredientStockMovement)
            .filter_by(ingredient_id=ing.id, movement_type="ORDER_DEDUCTION")
            .all()
        )
        assert len(movements) == 1, f"Expected 1 movement, got {len(movements)}"
        assert Decimal(str(movements[0].quantity_delta)) == -std_qty, (
            f"Expected delta={-std_qty}, got {movements[0].quantity_delta}"
        )

        cleanup_ingredient(db, ing.id)
