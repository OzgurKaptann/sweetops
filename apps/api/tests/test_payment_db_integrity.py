"""
Database-enforced cross-entity financial integrity.

These tests bypass the service layer entirely and drive raw SQL against the real
PostgreSQL database. They prove that the PL/pgSQL trigger layer (see the payment
migration) refuses to persist an internally inconsistent ledger row even if the
authenticated service is bypassed:

  1. settlement referencing a table from another store,
  2. settlement referencing a cashier from another store,
  3. allocation whose order is in another store,
  4. allocation whose order is on another table,
  5. refund whose allocation belongs to a different settlement,
  6. refund whose order differs from its allocation's order,
  7. refund whose store differs from its settlement's store,
  8. refund whose currency differs from its settlement's currency.

Every violation surfaces as an IntegrityError (SQLSTATE 23000).
"""
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.core.db import engine


def _h() -> str:
    return uuid.uuid4().hex


def _insert_settlement(conn, *, store_id, cashier_user_id, table_id=None,
                       gross="10.00", currency="TRY") -> int:
    return conn.execute(
        text(
            "INSERT INTO payment_settlements "
            "(store_id, table_id, cashier_user_id, payment_method, currency, "
            " gross_amount, status, idempotency_key_hash, request_hash) "
            "VALUES (:s, :t, :u, 'CASH', :cur, :g, 'COMPLETED', :k, :r) "
            "RETURNING id"
        ),
        {"s": store_id, "t": table_id, "u": cashier_user_id, "cur": currency,
         "g": gross, "k": _h(), "r": _h()},
    ).scalar()


def _insert_allocation(conn, *, settlement_id, order_id, amount="10.00") -> int:
    return conn.execute(
        text(
            "INSERT INTO payment_allocations (settlement_id, order_id, amount) "
            "VALUES (:s, :o, :a) RETURNING id"
        ),
        {"s": settlement_id, "o": order_id, "a": amount},
    ).scalar()


def _insert_refund(conn, *, store_id, settlement_id, allocation_id, order_id,
                   refunded_by, amount="5.00", currency="TRY") -> int:
    return conn.execute(
        text(
            "INSERT INTO payment_refunds "
            "(store_id, settlement_id, allocation_id, order_id, amount, currency, "
            " reason, refunded_by_user_id, idempotency_key_hash, request_hash) "
            "VALUES (:store, :s, :a, :o, :amt, :cur, 'x', :u, :k, :r) "
            "RETURNING id"
        ),
        {"store": store_id, "s": settlement_id, "a": allocation_id, "o": order_id,
         "amt": amount, "cur": currency, "u": refunded_by, "k": _h(), "r": _h()},
    ).scalar()


# ── 1. Settlement / table / store ──────────────────────────────────────────────

def test_settlement_table_from_another_store_rejected(db, make_store, make_table, make_staff):
    a = make_store(); b = make_store()
    table_b = make_table(b.id)
    cashier_a = make_staff("CASHIER", store_id=a.id)
    with pytest.raises(IntegrityError):
        with engine.begin() as conn:
            _insert_settlement(conn, store_id=a.id, cashier_user_id=cashier_a.id,
                               table_id=table_b.id)


# ── 2. Settlement / cashier / store ────────────────────────────────────────────

def test_settlement_cashier_from_another_store_rejected(db, make_store, make_table, make_staff):
    a = make_store(); b = make_store()
    table_a = make_table(a.id)
    cashier_b = make_staff("CASHIER", store_id=b.id)
    with pytest.raises(IntegrityError):
        with engine.begin() as conn:
            _insert_settlement(conn, store_id=a.id, cashier_user_id=cashier_b.id,
                               table_id=table_a.id)


# ── 3. Allocation / order / store ──────────────────────────────────────────────

def test_allocation_order_from_another_store_rejected(db, make_store, make_staff, make_order):
    a = make_store(); b = make_store()
    cashier_a = make_staff("CASHIER", store_id=a.id)
    order_b = make_order(b.id, None, Decimal("10.00"))
    with pytest.raises(IntegrityError):
        with engine.begin() as conn:
            sid = _insert_settlement(conn, store_id=a.id, cashier_user_id=cashier_a.id)
            _insert_allocation(conn, settlement_id=sid, order_id=order_b.id)


# ── 4. Allocation / order / table ──────────────────────────────────────────────

def test_allocation_order_from_another_table_rejected(db, make_store, make_table, make_staff, make_order):
    a = make_store()
    table1 = make_table(a.id); table2 = make_table(a.id)
    cashier_a = make_staff("CASHIER", store_id=a.id)
    order_t2 = make_order(a.id, table2.id, Decimal("10.00"))
    with pytest.raises(IntegrityError):
        with engine.begin() as conn:
            sid = _insert_settlement(conn, store_id=a.id, cashier_user_id=cashier_a.id,
                                     table_id=table1.id)
            _insert_allocation(conn, settlement_id=sid, order_id=order_t2.id)


# ── 5. Refund / settlement ─────────────────────────────────────────────────────

def test_refund_mismatched_settlement_rejected(db, collected_ledger, make_order):
    env = collected_ledger
    # A second, unrelated settlement for the same store/table.
    other_order = make_order(env.store.id, env.table.id, Decimal("50.00"))
    r = env.client.post(
        f"/cashier/orders/{other_order.id}/payments",
        json={"payment_method": "CASH"},
        headers={"Idempotency-Key": uuid.uuid4().hex},
    )
    other_settlement = r.json()["settlement_id"]

    with pytest.raises(IntegrityError):
        with engine.begin() as conn:
            _insert_refund(
                conn,
                store_id=env.store.id,
                settlement_id=other_settlement,          # wrong settlement
                allocation_id=env.allocation_id,          # allocation from env settlement
                order_id=env.order.id,
                refunded_by=env.manager.id,
            )


# ── 6. Refund / order ──────────────────────────────────────────────────────────

def test_refund_mismatched_order_rejected(db, collected_ledger, make_order):
    env = collected_ledger
    other_order = make_order(env.store.id, env.table.id, Decimal("50.00"))
    with pytest.raises(IntegrityError):
        with engine.begin() as conn:
            _insert_refund(
                conn,
                store_id=env.store.id,
                settlement_id=env.settlement_id,
                allocation_id=env.allocation_id,
                order_id=other_order.id,                  # allocation is for env.order
                refunded_by=env.manager.id,
            )


# ── 7. Refund / store ──────────────────────────────────────────────────────────

def test_refund_mismatched_store_rejected(db, collected_ledger, make_store):
    env = collected_ledger
    other_store = make_store()
    with pytest.raises(IntegrityError):
        with engine.begin() as conn:
            _insert_refund(
                conn,
                store_id=other_store.id,                  # settlement is in env.store
                settlement_id=env.settlement_id,
                allocation_id=env.allocation_id,
                order_id=env.order.id,
                refunded_by=env.manager.id,
            )


# ── 8. Refund / currency ───────────────────────────────────────────────────────

def test_refund_mismatched_currency_rejected(db, collected_ledger):
    env = collected_ledger
    with pytest.raises(IntegrityError):
        with engine.begin() as conn:
            _insert_refund(
                conn,
                store_id=env.store.id,
                settlement_id=env.settlement_id,
                allocation_id=env.allocation_id,
                order_id=env.order.id,
                refunded_by=env.manager.id,
                currency="USD",                           # settlement is TRY
            )
