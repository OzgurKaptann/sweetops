"""Money precision, model integrity, migration backfill, DB constraints."""
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, DataError

from app.core.db import engine
from app.models.order import Order
from app.models.payment_settlement import PaymentSettlement


def test_money_columns_are_numeric_not_float():
    import sqlalchemy as sa
    insp = sa.inspect(engine)

    def coltype(table, col):
        return {c["name"]: c["type"] for c in insp.get_columns(table)}[col]

    assert "NUMERIC" in str(coltype("orders", "paid_amount")).upper()
    assert "NUMERIC" in str(coltype("orders", "refunded_amount")).upper()
    assert "NUMERIC" in str(coltype("payment_settlements", "gross_amount")).upper()
    assert "NUMERIC" in str(coltype("payment_allocations", "amount")).upper()
    assert "NUMERIC" in str(coltype("payment_refunds", "amount")).upper()


def test_existing_orders_default_unpaid(db, make_store, make_table, make_order):
    store = make_store(); table = make_table(store.id)
    order = make_order(store.id, table.id, Decimal("10.00"))
    db.refresh(order)
    assert order.payment_status == "UNPAID"
    assert order.refund_status == "NONE"
    assert Decimal(str(order.paid_amount)) == Decimal("0")
    assert Decimal(str(order.refunded_amount)) == Decimal("0")


def test_settlement_amount_must_be_positive(db, make_store):
    store = make_store()
    with engine.begin() as conn:
        with pytest.raises((IntegrityError, DataError, Exception)):
            conn.execute(text(
                "INSERT INTO payment_settlements "
                "(store_id, cashier_user_id, payment_method, currency, gross_amount, "
                " status, idempotency_key_hash, request_hash) "
                "VALUES (:s, 1, 'CASH', 'TRY', 0, 'COMPLETED', :k, :r)"
            ), {"s": store.id, "k": uuid.uuid4().hex, "r": uuid.uuid4().hex})


def test_settlement_method_domain_enforced(db, make_store):
    store = make_store()
    with engine.begin() as conn:
        with pytest.raises((IntegrityError, Exception)):
            conn.execute(text(
                "INSERT INTO payment_settlements "
                "(store_id, cashier_user_id, payment_method, currency, gross_amount, "
                " status, idempotency_key_hash, request_hash) "
                "VALUES (:s, 1, 'BITCOIN', 'TRY', 10, 'COMPLETED', :k, :r)"
            ), {"s": store.id, "k": uuid.uuid4().hex, "r": uuid.uuid4().hex})


def test_order_refund_le_paid_constraint(db, make_store, make_table):
    from app.models.table import Table
    store = make_store()
    tbl = Table(store_id=store.id, table_number="x", qr_code=f"c-{uuid.uuid4().hex[:8]}")
    db.add(tbl); db.commit()
    with pytest.raises((IntegrityError, Exception)):
        db.execute(text(
            "INSERT INTO orders (store_id, table_id, total_amount, status, "
            " payment_status, refund_status, paid_amount, refunded_amount) "
            "VALUES (:s, :t, 100, 'NEW', 'PAID', 'NONE', 10, 50)"
        ), {"s": store.id, "t": tbl.id})
        db.commit()
    db.rollback()


def test_settlement_idempotency_unique_per_store(cashier_env, make_order, db):
    """A duplicate (store, key_hash) row is rejected by the unique index."""
    env = cashier_env
    order = make_order(env.store.id, env.table.id, Decimal("10.00"))
    res = env.client.post(f"/cashier/orders/{order.id}/payments",
                          json={"payment_method": "CASH"},
                          headers={"Idempotency-Key": "abc"})
    row = db.get(PaymentSettlement, res.json()["settlement_id"])
    with engine.begin() as conn:
        with pytest.raises((IntegrityError, Exception)):
            conn.execute(text(
                "INSERT INTO payment_settlements "
                "(store_id, cashier_user_id, payment_method, currency, gross_amount, "
                " status, idempotency_key_hash, request_hash) "
                "VALUES (:s, :c, 'CASH', 'TRY', 5, 'COMPLETED', :k, :r)"
            ), {"s": row.store_id, "c": row.cashier_user_id,
                "k": row.idempotency_key_hash, "r": uuid.uuid4().hex})
