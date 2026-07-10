"""
Settlement total == sum of its allocations, enforced at COMMIT by a DEFERRABLE
INITIALLY DEFERRED constraint trigger.

The normal write sequence (insert settlement → insert allocations → commit) must
succeed, but a completed settlement whose allocation total differs from
gross_amount must be rejected — reconciliation is a backstop, not the first line
of defence. These tests drive raw SQL and prove:

  * a matching allocation total commits,
  * a lower allocation total fails at commit,
  * a higher allocation total fails at commit,
  * a multi-order settlement total is validated correctly (match commits,
    mismatch fails).
"""
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.core.db import engine
from app.models.payment_settlement import PaymentSettlement


def _h() -> str:
    return uuid.uuid4().hex


def _insert_settlement(conn, *, store_id, cashier_user_id, gross) -> int:
    return conn.execute(
        text(
            "INSERT INTO payment_settlements "
            "(store_id, cashier_user_id, payment_method, currency, gross_amount, "
            " status, idempotency_key_hash, request_hash) "
            "VALUES (:s, :u, 'CASH', 'TRY', :g, 'COMPLETED', :k, :r) RETURNING id"
        ),
        {"s": store_id, "u": cashier_user_id, "g": gross, "k": _h(), "r": _h()},
    ).scalar()


def _insert_allocation(conn, *, settlement_id, order_id, amount) -> None:
    conn.execute(
        text(
            "INSERT INTO payment_allocations (settlement_id, order_id, amount) "
            "VALUES (:s, :o, :a)"
        ),
        {"s": settlement_id, "o": order_id, "a": amount},
    )


def test_matching_total_commits(db, make_store, make_staff, make_order):
    a = make_store(); cashier = make_staff("CASHIER", store_id=a.id)
    order = make_order(a.id, None, Decimal("100.00"))
    with engine.begin() as conn:
        sid = _insert_settlement(conn, store_id=a.id, cashier_user_id=cashier.id, gross="100.00")
        _insert_allocation(conn, settlement_id=sid, order_id=order.id, amount="100.00")
    # Committed — the row is really there.
    assert db.get(PaymentSettlement, sid) is not None


def test_lower_total_fails(db, make_store, make_staff, make_order):
    a = make_store(); cashier = make_staff("CASHIER", store_id=a.id)
    order = make_order(a.id, None, Decimal("100.00"))
    with pytest.raises(IntegrityError):
        with engine.begin() as conn:
            sid = _insert_settlement(conn, store_id=a.id, cashier_user_id=cashier.id, gross="100.00")
            _insert_allocation(conn, settlement_id=sid, order_id=order.id, amount="60.00")


def test_higher_total_fails(db, make_store, make_staff, make_order):
    a = make_store(); cashier = make_staff("CASHIER", store_id=a.id)
    order = make_order(a.id, None, Decimal("100.00"))
    with pytest.raises(IntegrityError):
        with engine.begin() as conn:
            sid = _insert_settlement(conn, store_id=a.id, cashier_user_id=cashier.id, gross="100.00")
            _insert_allocation(conn, settlement_id=sid, order_id=order.id, amount="120.00")


def test_settlement_without_allocations_fails(db, make_store, make_staff):
    """A completed settlement with no allocations at all cannot commit."""
    a = make_store(); cashier = make_staff("CASHIER", store_id=a.id)
    with pytest.raises(IntegrityError):
        with engine.begin() as conn:
            _insert_settlement(conn, store_id=a.id, cashier_user_id=cashier.id, gross="10.00")


def test_multi_order_total_validated(db, make_store, make_staff, make_order):
    a = make_store(); cashier = make_staff("CASHIER", store_id=a.id)
    o1 = make_order(a.id, None, Decimal("40.00"))
    o2 = make_order(a.id, None, Decimal("60.00"))

    # 40 + 60 == 100 → commits.
    with engine.begin() as conn:
        sid = _insert_settlement(conn, store_id=a.id, cashier_user_id=cashier.id, gross="100.00")
        _insert_allocation(conn, settlement_id=sid, order_id=o1.id, amount="40.00")
        _insert_allocation(conn, settlement_id=sid, order_id=o2.id, amount="60.00")
    assert db.get(PaymentSettlement, sid) is not None

    # 40 + 40 != 100 → rejected.
    with pytest.raises(IntegrityError):
        with engine.begin() as conn:
            sid2 = _insert_settlement(conn, store_id=a.id, cashier_user_id=cashier.id, gross="100.00")
            _insert_allocation(conn, settlement_id=sid2, order_id=o1.id, amount="40.00")
            _insert_allocation(conn, settlement_id=sid2, order_id=o2.id, amount="40.00")
