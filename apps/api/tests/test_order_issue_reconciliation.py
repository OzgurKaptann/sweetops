"""
Order-issue reconciliation (scripts/reconcile_order_issues.py).

The reconciler re-derives each resolved issue's refund story from the ledger and
compares. It must PASS on honest data, DETECT a corrupted refund link (including a
duplicate refund for one issue), and NEVER write.
"""
import importlib.util
import uuid
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import text

from tests.conftest import make_authed_client, _ledger_maintenance
from app.models.order_issue import OrderIssue
from app.models.payment_refund import PaymentRefund

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SCRIPT = _REPO_ROOT / "scripts" / "reconcile_order_issues.py"


def _load():
    spec = importlib.util.spec_from_file_location("reconcile_order_issues", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def reconciler():
    assert _SCRIPT.exists(), f"missing {_SCRIPT}"
    return _load()


def _key() -> str:
    return uuid.uuid4().hex


@pytest.fixture()
def env(db, make_store, make_table, make_staff, make_order):
    class Env:
        pass
    e = Env()
    e.db = db
    e.store = make_store()
    e.table = make_table(e.store.id)
    e.manager_client = make_authed_client(db, make_staff("MANAGER", store_id=e.store.id))
    e.make_order = lambda total="100.00": make_order(e.store.id, e.table.id, Decimal(total))
    return e


def _pay(e, order_id):
    e.manager_client.post(f"/cashier/orders/{order_id}/payments", json={"payment_method": "CASH"},
                          headers={"Idempotency-Key": _key()})


def _issue(e, order_id, itype="CUSTOMER_CANCELLED"):
    return e.manager_client.post(f"/orders/{order_id}/issues",
                                 json={"issue_type": itype, "reason": "sebep"},
                                 headers={"Idempotency-Key": _key()}).json()["id"]


def _resolve(e, issue_id, resolution, approved=None):
    body = {"resolution_type": resolution, "reason": "çözüm"}
    if approved is not None:
        body["approved_refund_amount"] = approved
    return e.manager_client.post(f"/order-issues/{issue_id}/resolve", json=body,
                                 headers={"Idempotency-Key": _key()})


def test_reconciles_valid_data(env, reconciler):
    o1 = env.make_order(); _pay(env, o1.id)
    _resolve(env, _issue(env, o1.id), "FULL_REFUND")
    o2 = env.make_order(); _pay(env, o2.id)
    _resolve(env, _issue(env, o2.id), "PARTIAL_REFUND", approved="30.00")
    o3 = env.make_order(); _pay(env, o3.id)
    _resolve(env, _issue(env, o3.id), "NO_REFUND")
    o4 = env.make_order()
    _resolve(env, _issue(env, o4.id), "CANCEL_ONLY")

    assert reconciler.reconcile_issues(store_id=env.store.id) == []
    assert reconciler.reconcile_order_refunds(store_id=env.store.id) == []


def test_detects_stray_refund_link(env, reconciler):
    # A NO_REFUND resolution must have no linked refund. Fabricate one behind the
    # service's back and demand the reconciler catches it.
    order = env.make_order(); _pay(env, order.id)
    issue_id = _issue(env, order.id)
    _resolve(env, issue_id, "NO_REFUND")

    alloc_id, settle_id = env.db.execute(text(
        "SELECT id, settlement_id FROM payment_allocations WHERE order_id = :oid LIMIT 1"
    ), {"oid": order.id}).fetchone()
    resolver = env.db.get(OrderIssue, issue_id).resolved_by_user_id
    stray = PaymentRefund(
        store_id=env.store.id, settlement_id=settle_id, allocation_id=alloc_id,
        order_id=order.id, amount=Decimal("5.00"), currency="TRY", reason="stray",
        refunded_by_user_id=resolver, idempotency_key_hash=_key(), request_hash=_key(),
        order_issue_id=issue_id,
    )
    env.db.add(stray)
    env.db.commit()

    rows = reconciler.reconcile_issues(store_id=env.store.id)
    match = next(r for r in rows if r["issue_id"] == issue_id)
    assert "unexpected_refund_link" in match["problems"]


def test_detects_duplicate_refund_for_issue(env, reconciler):
    # A FULL_REFUND issue with 100 linked. Fabricate a SECOND linked refund → the
    # linked sum (200) exceeds the approved amount (100): a duplicate.
    order = env.make_order(); _pay(env, order.id)
    issue_id = _issue(env, order.id)
    _resolve(env, issue_id, "FULL_REFUND")

    alloc_id, settle_id = env.db.execute(text(
        "SELECT id, settlement_id FROM payment_allocations WHERE order_id = :oid LIMIT 1"
    ), {"oid": order.id}).fetchone()
    resolver = env.db.get(OrderIssue, issue_id).resolved_by_user_id
    dup = PaymentRefund(
        store_id=env.store.id, settlement_id=settle_id, allocation_id=alloc_id,
        order_id=order.id, amount=Decimal("100.00"), currency="TRY", reason="dup",
        refunded_by_user_id=resolver, idempotency_key_hash=_key(), request_hash=_key(),
        order_issue_id=issue_id,
    )
    env.db.add(dup)
    env.db.commit()

    rows = reconciler.reconcile_issues(store_id=env.store.id)
    match = next(r for r in rows if r["issue_id"] == issue_id)
    assert "refund_amount_over" in match["problems"]


def test_reconciler_does_not_mutate(env, reconciler):
    order = env.make_order(); _pay(env, order.id)
    issue_id = _issue(env, order.id)
    _resolve(env, issue_id, "FULL_REFUND")

    before_issue = env.db.query(OrderIssue).filter(OrderIssue.store_id == env.store.id).count()
    before_ref = env.db.query(PaymentRefund).filter(PaymentRefund.store_id == env.store.id).count()
    reconciler.reconcile_issues(store_id=env.store.id)
    reconciler.reconcile_order_refunds(store_id=env.store.id)
    env.db.expire_all()
    assert env.db.query(OrderIssue).filter(OrderIssue.store_id == env.store.id).count() == before_issue
    assert env.db.query(PaymentRefund).filter(PaymentRefund.store_id == env.store.id).count() == before_ref
    # The resolved issue is untouched.
    row = env.db.get(OrderIssue, issue_id)
    assert row.status == "RESOLVED" and str(row.approved_refund_amount) == "100.00"
