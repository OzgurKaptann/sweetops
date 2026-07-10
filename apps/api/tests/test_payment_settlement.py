"""
Settlement flow: single-order, partial, full-table, receipts, and the
independence of payment state from preparation state.
"""
import uuid
from decimal import Decimal


def _key() -> str:
    return uuid.uuid4().hex


def test_full_single_order_payment_marks_paid(cashier_env, make_order):
    env = cashier_env
    order = make_order(env.store.id, env.table.id, Decimal("100.00"))

    res = env.client.post(
        f"/cashier/orders/{order.id}/payments",
        json={"payment_method": "CASH"},
        headers={"Idempotency-Key": _key()},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["gross_amount"] == "100.00"
    assert body["payment_method"] == "CASH"
    assert body["cashier_display"] == env.cashier.username
    assert len(body["allocations"]) == 1
    assert body["allocations"][0]["order_code"] == f"SIP-{order.id:06d}"

    detail = env.client.get(f"/cashier/orders/{order.id}").json()
    assert detail["payment_status"] == "PAID"
    assert detail["remaining_amount"] == "0.00"


def test_partial_payment_marks_partially_paid(cashier_env, make_order):
    env = cashier_env
    order = make_order(env.store.id, env.table.id, Decimal("100.00"))

    res = env.client.post(
        f"/cashier/orders/{order.id}/payments",
        json={"payment_method": "CARD", "amount": "40.00"},
        headers={"Idempotency-Key": _key()},
    )
    assert res.status_code == 200, res.text
    detail = env.client.get(f"/cashier/orders/{order.id}").json()
    assert detail["payment_status"] == "PARTIALLY_PAID"
    assert detail["paid_amount"] == "40.00"
    assert detail["remaining_amount"] == "60.00"


def test_second_payment_completes_remaining(cashier_env, make_order):
    env = cashier_env
    order = make_order(env.store.id, env.table.id, Decimal("100.00"))

    env.client.post(
        f"/cashier/orders/{order.id}/payments",
        json={"payment_method": "CASH", "amount": "40.00"},
        headers={"Idempotency-Key": _key()},
    )
    res = env.client.post(
        f"/cashier/orders/{order.id}/payments",
        json={"payment_method": "CASH", "amount": "60.00"},
        headers={"Idempotency-Key": _key()},
    )
    assert res.status_code == 200, res.text
    detail = env.client.get(f"/cashier/orders/{order.id}").json()
    assert detail["payment_status"] == "PAID"
    assert detail["remaining_amount"] == "0.00"


def test_full_table_settlement_pays_all_selected(cashier_env, make_order):
    env = cashier_env
    o1 = make_order(env.store.id, env.table.id, Decimal("30.00"))
    o2 = make_order(env.store.id, env.table.id, Decimal("70.50"))

    res = env.client.post(
        "/cashier/settlements",
        json={
            "table_id": env.table.id,
            "order_ids": [o1.id, o2.id],
            "payment_method": "CARD",
        },
        headers={"Idempotency-Key": _key()},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    # Server computes the pay-all amount — no client amount was sent.
    assert body["gross_amount"] == "100.50"
    assert len(body["allocations"]) == 2

    for o in (o1, o2):
        d = env.client.get(f"/cashier/orders/{o.id}").json()
        assert d["payment_status"] == "PAID"


def test_cancelled_order_cannot_be_collected(cashier_env, make_order):
    env = cashier_env
    order = make_order(env.store.id, env.table.id, Decimal("50.00"), status="CANCELLED")
    res = env.client.post(
        f"/cashier/orders/{order.id}/payments",
        json={"payment_method": "CASH"},
        headers={"Idempotency-Key": _key()},
    )
    assert res.status_code == 409
    assert res.json()["detail"]["error"] == "order_cancelled"


def test_already_paid_order_cannot_be_paid_again(cashier_env, make_order):
    env = cashier_env
    order = make_order(env.store.id, env.table.id, Decimal("50.00"))
    env.client.post(
        f"/cashier/orders/{order.id}/payments",
        json={"payment_method": "CASH"},
        headers={"Idempotency-Key": _key()},
    )
    res = env.client.post(
        f"/cashier/orders/{order.id}/payments",
        json={"payment_method": "CASH"},
        headers={"Idempotency-Key": _key()},
    )
    assert res.status_code == 409
    assert res.json()["detail"]["error"] == "no_balance"


def test_overpayment_rejected(cashier_env, make_order):
    env = cashier_env
    order = make_order(env.store.id, env.table.id, Decimal("50.00"))
    res = env.client.post(
        f"/cashier/orders/{order.id}/payments",
        json={"payment_method": "CASH", "amount": "60.00"},
        headers={"Idempotency-Key": _key()},
    )
    assert res.status_code == 409
    assert res.json()["detail"]["error"] == "overpayment"


def test_card_settlement_stores_no_card_data(cashier_env, make_order, db):
    from app.models.payment_settlement import PaymentSettlement
    env = cashier_env
    order = make_order(env.store.id, env.table.id, Decimal("25.00"))
    res = env.client.post(
        f"/cashier/orders/{order.id}/payments",
        json={"payment_method": "CARD", "terminal_reference": "TERM-7"},
        headers={"Idempotency-Key": _key()},
    )
    assert res.status_code == 200, res.text
    sid = res.json()["settlement_id"]
    row = db.get(PaymentSettlement, sid)
    # Only non-sensitive columns exist; assert no PAN-like attributes present.
    cols = {c.name for c in row.__table__.columns}
    assert "pan" not in cols and "cvv" not in cols and "card_number" not in cols
    assert row.terminal_reference == "TERM-7"


def test_preparation_status_unchanged_by_payment(cashier_env, make_order):
    env = cashier_env
    order = make_order(env.store.id, env.table.id, Decimal("40.00"), status="IN_PREP")
    env.client.post(
        f"/cashier/orders/{order.id}/payments",
        json={"payment_method": "CASH"},
        headers={"Idempotency-Key": _key()},
    )
    detail = env.client.get(f"/cashier/orders/{order.id}").json()
    assert detail["preparation_status"] == "IN_PREP"
    assert detail["payment_status"] == "PAID"


def test_responses_are_no_store(cashier_env, make_order):
    env = cashier_env
    order = make_order(env.store.id, env.table.id, Decimal("40.00"))
    res = env.client.get(f"/cashier/orders/{order.id}")
    assert res.headers.get("Cache-Control") == "no-store"


def test_idempotency_key_required(cashier_env, make_order):
    env = cashier_env
    order = make_order(env.store.id, env.table.id, Decimal("40.00"))
    res = env.client.post(
        f"/cashier/orders/{order.id}/payments",
        json={"payment_method": "CASH"},
    )
    assert res.status_code == 400
    assert res.json()["detail"]["error"] == "idempotency_required"
