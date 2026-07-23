"""
Regression tests for order-item quantity accounting.

Bug being guarded against:
    Order-item quantity multiplied the base product price but was NOT applied
    to ingredient modifier pricing or physical ingredient consumption. An order
    for 3 waffles charged 1× the banana modifier and consumed 1× the banana
    grams — under-charging the customer and silently over-reporting stock.

Correct formulas (canonical, see order_service.calculate_consumed_quantity):
    modifier_total  = ingredient.price        × selected_quantity × item_quantity
    consumed_qty    = ingredient.standard_qty × selected_quantity × item_quantity

Every test below FAILS on the pre-fix code (item_quantity omitted) and PASSES
after the fix.

Under the inventory-reservation lifecycle these same formulas now drive the
RESERVATION at order creation rather than an immediate physical deduction, so
the assertions below check reserved/available quantities and RESERVATION_CREATED
movements. The arithmetic being guarded is identical; only what it moves changed.
"""
import threading
import uuid
from decimal import Decimal

import pytest

from app.models.ingredient_stock import IngredientStock, IngredientStockMovement
from app.models.order import Order
from app.models.order_item import OrderItem
from app.models.order_item_ingredient import OrderItemIngredient
from app.models.order_status_event import OrderStatusEvent
from tests.conftest import (
    cleanup_ingredient,
    cleanup_product,
    make_ingredient,
    make_product,
    purge_inventory_for_orders,
)


# ---------------------------------------------------------------------------
# Local helpers
# ---------------------------------------------------------------------------
# ``make_product`` / ``cleanup_product`` come from conftest. They used to be
# local, created a row named ``TestWaffle_<hex>``, and deleted only the product
# — which is exactly how eight ₺100.00 "TestWaffle" rows ended up permanently in
# a customer-facing table (RUNTIME_PRODUCT_GAP_REVIEW F-23): every interrupted
# run leaked one. The shared helper publishes the product on the store's menu
# and the cleanup withdraws it again, so an interrupted run now leaves at worst
# an UNPUBLISHED row, which no guest can reach.


def build_payload(store_id, table_id, product_id, item_quantity, ingredients, idem_key=None):
    """ingredients: list of (ingredient_id, selected_quantity)."""
    headers = {"Idempotency-Key": idem_key} if idem_key else {}
    payload = {
        "store_id": store_id,
        "table_id": table_id,
        "items": [
            {
                "product_id": product_id,
                "quantity": item_quantity,
                "ingredients": [
                    {"ingredient_id": iid, "quantity": q} for iid, q in ingredients
                ],
            }
        ],
    }
    return payload, headers


def oii_for_ingredient(db, ingredient_id):
    return (
        db.query(OrderItemIngredient)
        .filter(OrderItemIngredient.ingredient_id == ingredient_id)
        .all()
    )


def reservation_movements(db, ingredient_id):
    return (
        db.query(IngredientStockMovement)
        .filter_by(ingredient_id=ingredient_id, movement_type="RESERVATION_CREATED")
        .all()
    )


def cleanup_order(db, order_id):
    """
    Delete a single order and its child rows in FK-safe order.

    Needed when one order references several ingredients (Scenario 4/5): the
    shared ``cleanup_ingredient`` helper deletes order_items per-ingredient and
    would trip the order_item_ingredients FK if another ingredient still
    references the same order_item. Removing the whole order graph up front
    sidesteps that ordering hazard.
    """
    purge_inventory_for_orders(db, [order_id])
    item_ids = [
        row.id
        for row in db.query(OrderItem).filter(OrderItem.order_id == order_id).all()
    ]
    if item_ids:
        db.query(OrderItemIngredient).filter(
            OrderItemIngredient.order_item_id.in_(item_ids)
        ).delete(synchronize_session=False)
        db.query(OrderItem).filter(
            OrderItem.id.in_(item_ids)
        ).delete(synchronize_session=False)
    db.query(OrderStatusEvent).filter(
        OrderStatusEvent.order_id == order_id
    ).delete(synchronize_session=False)
    db.query(Order).filter(Order.id == order_id).delete(synchronize_session=False)
    db.commit()


# ---------------------------------------------------------------------------
# Scenario 1 — single product, single ingredient (existing behaviour intact)
# ---------------------------------------------------------------------------

class TestScenario1SingleProduct:

    def test_quantity_one_unchanged(self, db, client):
        base = Decimal("100.00")
        prod = make_product(db, base_price=base)
        ing, _ = make_ingredient(
            db,
            on_hand=Decimal("1000.00"),
            standard_quantity=Decimal("50.00"),
            price=Decimal("10.00"),
        )
        try:
            payload, headers = build_payload(
                1, 1, prod.id, 1, [(ing.id, 1)], idem_key=uuid.uuid4().hex
            )
            r = client.post("/public/orders/", json=payload, headers=headers)
            assert r.status_code == 200, r.json()

            # 100 base + 10 modifier = 110
            assert Decimal(str(r.json()["total_amount"])) == Decimal("110.00")

            oiis = oii_for_ingredient(db, ing.id)
            assert len(oiis) == 1
            assert Decimal(str(oiis[0].consumed_quantity)) == Decimal("50.00")

            movements = reservation_movements(db, ing.id)
            assert len(movements) == 1
            assert Decimal(str(movements[0].quantity)) == Decimal("50.00")
            assert Decimal(str(movements[0].quantity_delta_reserved)) == Decimal("50.00")
            assert Decimal(str(movements[0].quantity_delta_on_hand)) == Decimal("0")
        finally:
            cleanup_ingredient(db, ing.id)
            cleanup_product(db, prod.id)


# ---------------------------------------------------------------------------
# Scenario 2 — multiple products, one portion each
# ---------------------------------------------------------------------------

class TestScenario2MultipleProducts:

    def test_quantity_three_multiplies_price_and_consumption(self, db, client):
        base = Decimal("100.00")
        prod = make_product(db, base_price=base)
        ing, _ = make_ingredient(
            db,
            on_hand=Decimal("1000.00"),
            standard_quantity=Decimal("50.00"),
            price=Decimal("10.00"),
        )
        initial_stock = Decimal("1000.00")
        try:
            payload, headers = build_payload(
                1, 1, prod.id, 3, [(ing.id, 1)], idem_key=uuid.uuid4().hex
            )
            r = client.post("/public/orders/", json=payload, headers=headers)
            assert r.status_code == 200, r.json()

            # base 100*3 = 300, modifier 10*1*3 = 30, total = 330
            assert Decimal(str(r.json()["total_amount"])) == Decimal("330.00")

            oiis = oii_for_ingredient(db, ing.id)
            assert len(oiis) == 1
            assert Decimal(str(oiis[0].consumed_quantity)) == Decimal("150.00")

            db.expire_all()
            stock = db.query(IngredientStock).filter_by(ingredient_id=ing.id).first()
            assert stock.reserved_quantity == Decimal("150.00")
            assert stock.available_quantity == initial_stock - Decimal("150.00")
            assert stock.on_hand_quantity == initial_stock  # reserved, not cooked

            movements = reservation_movements(db, ing.id)
            assert len(movements) == 1
            assert Decimal(str(movements[0].quantity)) == Decimal("150.00")
        finally:
            cleanup_ingredient(db, ing.id)
            cleanup_product(db, prod.id)


# ---------------------------------------------------------------------------
# Scenario 3 — multiple products AND multiple ingredient portions
# ---------------------------------------------------------------------------

class TestScenario3MultipleProductsAndPortions:

    def test_item_quantity_times_selected_quantity(self, db, client):
        base = Decimal("100.00")
        prod = make_product(db, base_price=base)
        ing, _ = make_ingredient(
            db,
            on_hand=Decimal("1000.00"),
            standard_quantity=Decimal("50.00"),
            price=Decimal("10.00"),
        )
        try:
            # item_quantity = 2, selected_quantity = 3 → multiplier 6
            payload, headers = build_payload(
                1, 1, prod.id, 2, [(ing.id, 3)], idem_key=uuid.uuid4().hex
            )
            r = client.post("/public/orders/", json=payload, headers=headers)
            assert r.status_code == 200, r.json()

            # base 100*2 = 200, modifier 10*3*2 = 60, total = 260
            assert Decimal(str(r.json()["total_amount"])) == Decimal("260.00")

            oiis = oii_for_ingredient(db, ing.id)
            assert len(oiis) == 1
            # 50 * 3 * 2 = 300
            assert Decimal(str(oiis[0].consumed_quantity)) == Decimal("300.00")

            movements = reservation_movements(db, ing.id)
            assert Decimal(str(movements[0].quantity)) == Decimal("300.00")
        finally:
            cleanup_ingredient(db, ing.id)
            cleanup_product(db, prod.id)


# ---------------------------------------------------------------------------
# Scenario 4 & 5 — multiple order items / shared ingredient across items
# ---------------------------------------------------------------------------

class TestScenario4And5MultipleItems:

    def test_multiple_items_and_shared_ingredient(self, db, client):
        prod = make_product(db, base_price=Decimal("100.00"))
        # shared ingredient used in both items
        shared, _ = make_ingredient(
            db,
            on_hand=Decimal("1000.00"),
            standard_quantity=Decimal("50.00"),
            price=Decimal("10.00"),
        )
        # ingredient only in the second item
        extra, _ = make_ingredient(
            db,
            on_hand=Decimal("1000.00"),
            standard_quantity=Decimal("20.00"),
            price=Decimal("5.00"),
        )
        order_id = None
        try:
            payload = {
                "store_id": 1,
                "table_id": 1,
                "items": [
                    {  # item A: qty 3, shared x1
                        "product_id": prod.id,
                        "quantity": 3,
                        "ingredients": [{"ingredient_id": shared.id, "quantity": 1}],
                    },
                    {  # item B: qty 2, shared x2 + extra x1
                        "product_id": prod.id,
                        "quantity": 2,
                        "ingredients": [
                            {"ingredient_id": shared.id, "quantity": 2},
                            {"ingredient_id": extra.id, "quantity": 1},
                        ],
                    },
                ],
            }
            headers = {"Idempotency-Key": uuid.uuid4().hex}
            r = client.post("/public/orders/", json=payload, headers=headers)
            assert r.status_code == 200, r.json()
            order_id = r.json()["order_id"]

            # Item A total: 100*3 + 10*1*3 = 330
            # Item B total: 100*2 + 10*2*2 + 5*1*2 = 200 + 40 + 10 = 250
            # Order total = 580
            assert Decimal(str(r.json()["total_amount"])) == Decimal("580.00")

            # Shared ingredient consumption: item A 50*1*3=150, item B 50*2*2=200 → 350
            db.expire_all()
            shared_stock = db.query(IngredientStock).filter_by(ingredient_id=shared.id).first()
            assert shared_stock.reserved_quantity == Decimal("350.00")
            assert shared_stock.available_quantity == Decimal("1000.00") - Decimal("350.00")

            shared_movs = reservation_movements(db, shared.id)
            total_shared = sum(Decimal(str(m.quantity)) for m in shared_movs)
            assert total_shared == Decimal("350.00")

            # Extra ingredient: 20*1*2 = 40
            extra_stock = db.query(IngredientStock).filter_by(ingredient_id=extra.id).first()
            assert extra_stock.reserved_quantity == Decimal("40.00")
            assert extra_stock.available_quantity == Decimal("1000.00") - Decimal("40.00")
        finally:
            if order_id is not None:
                cleanup_order(db, order_id)
            cleanup_ingredient(db, shared.id)
            cleanup_ingredient(db, extra.id)
            cleanup_product(db, prod.id)


# ---------------------------------------------------------------------------
# Scenario 6 — insufficient stock caused by item quantity
# ---------------------------------------------------------------------------

class TestScenario6InsufficientStockByQuantity:

    def test_enough_for_one_not_for_three(self, db, client):
        prod = make_product(db, base_price=Decimal("100.00"))
        # stock 120g, standard 50g. One product needs 50 (ok), three need 150 (fail).
        ing, _ = make_ingredient(
            db,
            on_hand=Decimal("120.00"),
            standard_quantity=Decimal("50.00"),
            price=Decimal("10.00"),
        )
        try:
            payload, headers = build_payload(
                1, 1, prod.id, 3, [(ing.id, 1)], idem_key=uuid.uuid4().hex
            )
            r = client.post("/public/orders/", json=payload, headers=headers)

            assert r.status_code == 422
            assert r.json()["detail"]["error"] == "out_of_stock"

            # Nothing committed: stock untouched, no movements, no order.
            db.expire_all()
            stock = db.query(IngredientStock).filter_by(ingredient_id=ing.id).first()
            assert stock.on_hand_quantity == Decimal("120.00")
            assert stock.reserved_quantity == Decimal("0")
            assert len(reservation_movements(db, ing.id)) == 0
            assert len(oii_for_ingredient(db, ing.id)) == 0
        finally:
            cleanup_ingredient(db, ing.id)
            cleanup_product(db, prod.id)


# ---------------------------------------------------------------------------
# Scenario 7 — idempotent retry with quantity > 1
# ---------------------------------------------------------------------------

class TestScenario7IdempotentRetry:

    def test_retry_deducts_once(self, db, client):
        prod = make_product(db, base_price=Decimal("100.00"))
        ing, _ = make_ingredient(
            db,
            on_hand=Decimal("1000.00"),
            standard_quantity=Decimal("50.00"),
            price=Decimal("10.00"),
        )
        try:
            idem = uuid.uuid4().hex
            payload, headers = build_payload(1, 1, prod.id, 3, [(ing.id, 1)], idem_key=idem)

            r1 = client.post("/public/orders/", json=payload, headers=headers)
            r2 = client.post("/public/orders/", json=payload, headers=headers)

            assert r1.status_code == 200
            assert r2.status_code == 200
            assert r1.json()["order_id"] == r2.json()["order_id"]
            assert Decimal(str(r2.json()["total_amount"])) == Decimal("330.00")

            # Reserved exactly once: 150g.
            db.expire_all()
            stock = db.query(IngredientStock).filter_by(ingredient_id=ing.id).first()
            assert stock.reserved_quantity == Decimal("150.00")
            assert stock.available_quantity == Decimal("1000.00") - Decimal("150.00")

            movements = reservation_movements(db, ing.id)
            assert len(movements) == 1
            assert Decimal(str(movements[0].quantity)) == Decimal("150.00")

            assert len(oii_for_ingredient(db, ing.id)) == 1
        finally:
            cleanup_ingredient(db, ing.id)
            cleanup_product(db, prod.id)


# ---------------------------------------------------------------------------
# Scenario 8 — cancellation releases the exact reserved quantity, once
# ---------------------------------------------------------------------------

class TestScenario8CancellationRelease:

    def test_cancel_releases_exact_reserved_quantity_once(self, db, client, kitchen_client):
        prod = make_product(db, base_price=Decimal("100.00"))
        initial = Decimal("1000.00")
        ing, _ = make_ingredient(
            db,
            on_hand=initial,
            standard_quantity=Decimal("50.00"),
            price=Decimal("10.00"),
        )
        try:
            payload, headers = build_payload(
                1, 1, prod.id, 3, [(ing.id, 1)], idem_key=uuid.uuid4().hex
            )
            r = client.post("/public/orders/", json=payload, headers=headers)
            assert r.status_code == 200
            oid = r.json()["order_id"]

            db.expire_all()
            after_order = db.query(IngredientStock).filter_by(ingredient_id=ing.id).first()
            assert after_order.reserved_quantity == Decimal("150.00")
            assert after_order.available_quantity == initial - Decimal("150.00")
            assert after_order.on_hand_quantity == initial

            # Cancel before the kitchen starts → the reservation is released.
            rc = kitchen_client.patch(f"/kitchen/orders/{oid}/status", json={"status": "CANCELLED"})
            assert rc.status_code == 200

            db.expire_all()
            after_cancel = db.query(IngredientStock).filter_by(ingredient_id=ing.id).first()
            assert after_cancel.reserved_quantity == Decimal("0")
            assert after_cancel.available_quantity == initial
            assert after_cancel.on_hand_quantity == initial  # never physically moved

            releases = (
                db.query(IngredientStockMovement)
                .filter_by(ingredient_id=ing.id, movement_type="RESERVATION_RELEASED")
                .all()
            )
            assert len(releases) == 1
            assert Decimal(str(releases[0].quantity)) == Decimal("150.00")
            assert Decimal(str(releases[0].quantity_delta_on_hand)) == Decimal("0")

            # Second cancel must not release twice.
            rc2 = kitchen_client.patch(f"/kitchen/orders/{oid}/status", json={"status": "CANCELLED"})
            assert rc2.status_code == 409
            db.expire_all()
            after_second = db.query(IngredientStock).filter_by(ingredient_id=ing.id).first()
            assert after_second.available_quantity == initial
            assert after_second.reserved_quantity == Decimal("0")
        finally:
            cleanup_ingredient(db, ing.id)
            cleanup_product(db, prod.id)


# ---------------------------------------------------------------------------
# Scenario 9 — concurrency with item quantity > 1
# ---------------------------------------------------------------------------

class TestScenario9Concurrency:

    def test_concurrent_quantity_orders_never_negative(self, db, client):
        prod = make_product(db, base_price=Decimal("100.00"))
        # Each order (qty 3, std 50) needs 150g. Stock 300g fits exactly 2.
        ing, _ = make_ingredient(
            db,
            on_hand=Decimal("300.00"),
            standard_quantity=Decimal("50.00"),
            price=Decimal("10.00"),
        )
        try:
            results: list[int] = []
            lock = threading.Lock()

            def fire(idem_key: str):
                payload, headers = build_payload(
                    1, 1, prod.id, 3, [(ing.id, 1)], idem_key=idem_key
                )
                r = client.post("/public/orders/", json=payload, headers=headers)
                with lock:
                    results.append(r.status_code)

            threads = [
                threading.Thread(target=fire, args=(f"qacc-{uuid.uuid4().hex}",))
                for _ in range(5)
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            successes = results.count(200)
            assert successes == 2, f"Expected exactly 2 successes, got {results}"

            db.expire_all()
            stock = db.query(IngredientStock).filter_by(ingredient_id=ing.id).first()
            assert stock.available_quantity == Decimal("0.00")
            assert stock.available_quantity >= Decimal("0")
            assert stock.reserved_quantity == Decimal("300.00")
            assert stock.on_hand_quantity == Decimal("300.00")

            # Movement total must equal the reservation of the successful orders.
            movements = reservation_movements(db, ing.id)
            total = sum(Decimal(str(m.quantity)) for m in movements)
            assert total == Decimal("300.00")
            assert len(movements) == successes
        finally:
            cleanup_ingredient(db, ing.id)
            cleanup_product(db, prod.id)
