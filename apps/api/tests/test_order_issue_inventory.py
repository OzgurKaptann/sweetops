"""
Order issue resolution preserves the inventory lifecycle EXACTLY:

  * cancelling before the kitchen starts releases the outstanding reservation,
  * cancelling after consumption restores nothing (the batter really was poured),
  * a refund never restores stock,
  * a resolution never writes a manual-adjustment or stock-count movement.

The issue workflow only ever calls the existing release primitive; it invents no
new stock movement type.
"""
import uuid
from decimal import Decimal

import pytest

from app.models.ingredient_stock import IngredientStock, IngredientStockMovement
from tests.conftest import cleanup_ingredient, make_authed_client, make_ingredient, order_payload

DEFAULT_STORE_ID = 1


def _key() -> str:
    return uuid.uuid4().hex


def _stock(db, ing_id: int) -> IngredientStock:
    db.expire_all()
    return db.query(IngredientStock).filter_by(ingredient_id=ing_id).first()


def _movement_types(db, ing_id: int) -> list[str]:
    return [
        m.movement_type
        for m in db.query(IngredientStockMovement).filter_by(ingredient_id=ing_id).all()
    ]


@pytest.fixture()
def env(db, make_staff):
    """Store-1 cashier + manager + kitchen clients (the legacy order path uses store 1)."""
    class Env:
        pass
    e = Env()
    e.db = db
    e.cashier_client = make_authed_client(db, make_staff("CASHIER", store_id=DEFAULT_STORE_ID))
    e.manager_client = make_authed_client(db, make_staff("MANAGER", store_id=DEFAULT_STORE_ID))
    e.kitchen_client = make_authed_client(db, make_staff("KITCHEN", store_id=DEFAULT_STORE_ID))
    return e


def _make_order(client, ing_id: int) -> int:
    payload, headers = order_payload(ing_id, idem_key=_key())
    r = client.post("/public/orders/", json=payload, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()["order_id"]


def _create_issue(client, order_id, issue_type="CUSTOMER_CANCELLED"):
    r = client.post(
        f"/orders/{order_id}/issues",
        json={"issue_type": issue_type, "reason": "sebep"},
        headers={"Idempotency-Key": _key()},
    )
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _resolve(client, issue_id, resolution, approved=None):
    body = {"resolution_type": resolution, "reason": "çözüm"}
    if approved is not None:
        body["approved_refund_amount"] = approved
    return client.post(
        f"/order-issues/{issue_id}/resolve",
        json=body,
        headers={"Idempotency-Key": _key()},
    )


def test_cancel_before_consumption_releases_reservation(env, client):
    ing, _ = make_ingredient(env.db, on_hand=Decimal("100.000"))
    try:
        order_id = _make_order(env.cashier_client, ing.id)
        assert _stock(env.db, ing.id).reserved_quantity == Decimal("10.000")

        issue_id = _create_issue(env.cashier_client, order_id)
        assert _resolve(env.cashier_client, issue_id, "CANCEL_ONLY").status_code == 200

        s = _stock(env.db, ing.id)
        assert s.reserved_quantity == Decimal("0.000")   # reservation released
        assert s.on_hand_quantity == Decimal("100.000")  # nothing physical moved
        assert "RESERVATION_RELEASED" in _movement_types(env.db, ing.id)
    finally:
        cleanup_ingredient(env.db, ing.id)


def test_cancel_after_consumption_does_not_restore_stock(env, client):
    ing, _ = make_ingredient(env.db, on_hand=Decimal("100.000"))
    try:
        order_id = _make_order(env.cashier_client, ing.id)
        # Kitchen starts cooking → consumption (on_hand and reserved both fall).
        env.kitchen_client.patch(f"/kitchen/orders/{order_id}/status", json={"status": "IN_PREP"})
        after_consume = _stock(env.db, ing.id)
        assert after_consume.on_hand_quantity == Decimal("90.000")
        assert after_consume.reserved_quantity == Decimal("0.000")

        issue_id = _create_issue(env.cashier_client, order_id)
        assert _resolve(env.cashier_client, issue_id, "CANCEL_ONLY").status_code == 200

        s = _stock(env.db, ing.id)
        assert s.on_hand_quantity == Decimal("90.000")  # consumed stock NOT restored
    finally:
        cleanup_ingredient(env.db, ing.id)


def test_refund_does_not_restore_stock(env, client, make_table):
    ing, _ = make_ingredient(env.db, on_hand=Decimal("100.000"))
    try:
        order_id = _make_order(env.cashier_client, ing.id)
        env.kitchen_client.patch(f"/kitchen/orders/{order_id}/status", json={"status": "IN_PREP"})
        # Pay, then partially refund through the issue workflow.
        env.manager_client.post(
            f"/cashier/orders/{order_id}/payments",
            json={"payment_method": "CASH"},
            headers={"Idempotency-Key": _key()},
        )
        issue_id = _create_issue(env.manager_client, order_id, issue_type="QUALITY_PROBLEM")
        assert _resolve(env.manager_client, issue_id, "PARTIAL_REFUND", approved="5.00").status_code == 200

        s = _stock(env.db, ing.id)
        assert s.on_hand_quantity == Decimal("90.000")  # refund moved money, not stock
    finally:
        cleanup_ingredient(env.db, ing.id)


def test_resolution_writes_no_manual_or_count_movement(env, client):
    ing, _ = make_ingredient(env.db, on_hand=Decimal("100.000"))
    try:
        order_id = _make_order(env.cashier_client, ing.id)
        issue_id = _create_issue(env.cashier_client, order_id)
        _resolve(env.cashier_client, issue_id, "CANCEL_ONLY")

        types = _movement_types(env.db, ing.id)
        assert "MANUAL_ADJUSTMENT" not in types
        assert "STOCK_COUNT_ADJUSTMENT" not in types
        # Only the lifecycle movements a cancellation legitimately produces.
        assert set(types) <= {"RESERVATION_CREATED", "RESERVATION_RELEASED"}
    finally:
        cleanup_ingredient(env.db, ing.id)
