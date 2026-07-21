"""
Order issue & controlled refund API.

Covers issue creation, resolution (no-refund / cancel-only / full / partial),
store scoping, CSRF/idempotency, the cashier-vs-supervisor refund boundary, and the
audit trail. Money moves only through the existing refund ledger; creation moves
nothing.
"""
import uuid
from decimal import Decimal

import pytest

from tests.conftest import make_authed_client
from app.models.audit_log import AuditLog
from app.models.order_issue import OrderIssue
from app.models.payment_refund import PaymentRefund


def _key() -> str:
    return uuid.uuid4().hex


@pytest.fixture()
def env(db, make_store, make_table, make_staff, make_order):
    """A store with a table, a CASHIER client, a MANAGER client, and an order maker."""
    class Env:
        pass
    e = Env()
    e.db = db
    e.store = make_store()
    e.table = make_table(e.store.id)
    e.cashier = make_staff("CASHIER", store_id=e.store.id)
    e.manager = make_staff("MANAGER", store_id=e.store.id)
    e.cashier_client = make_authed_client(db, e.cashier)
    e.manager_client = make_authed_client(db, e.manager)
    e._make_order = make_order

    def new_order(total="100.00", status="READY"):
        return make_order(e.store.id, e.table.id, Decimal(total), status=status)

    e.new_order = new_order
    return e


def _pay(client, order_id, method="CASH", amount=None):
    body = {"payment_method": method}
    if amount is not None:
        body["amount"] = amount
    return client.post(
        f"/cashier/orders/{order_id}/payments",
        json=body,
        headers={"Idempotency-Key": _key()},
    )


def _create_issue(client, order_id, *, issue_type="CUSTOMER_CANCELLED", reason="müşteri iptal etti",
                  requested=None, note=None, key=None, extra=None):
    body = {"issue_type": issue_type, "reason": reason}
    if requested is not None:
        body["requested_refund_amount"] = requested
    if note is not None:
        body["note"] = note
    if extra:
        body.update(extra)
    return client.post(
        f"/orders/{order_id}/issues",
        json=body,
        headers={"Idempotency-Key": key or _key()},
    )


def _resolve(client, issue_id, resolution, *, approved=None, reason="çözüldü", note=None, key=None):
    body = {"resolution_type": resolution, "reason": reason}
    if approved is not None:
        body["approved_refund_amount"] = approved
    if note is not None:
        body["note"] = note
    return client.post(
        f"/order-issues/{issue_id}/resolve",
        json=body,
        headers={"Idempotency-Key": key or _key()},
    )


# ── Create ────────────────────────────────────────────────────────────────────

def test_cashier_can_create_issue_for_own_store_order(env):
    order = env.new_order()
    res = _create_issue(env.cashier_client, order.id)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["status"] == "OPEN"
    assert body["issue_type"] == "CUSTOMER_CANCELLED"
    assert body["order_code"] == f"SIP-{order.id:06d}"
    assert body["created_by_display"] == env.cashier.username


def test_manager_can_create_issue(env):
    order = env.new_order()
    res = _create_issue(env.manager_client, order.id)
    assert res.status_code == 200, res.text


def test_create_does_not_move_money_or_status(env):
    order = env.new_order()
    _pay(env.manager_client, order.id)  # fully paid
    _create_issue(env.cashier_client, order.id, requested="20.00")
    detail = env.manager_client.get(f"/cashier/orders/{order.id}").json()
    assert detail["paid_amount"] == "100.00"
    assert detail["refunded_amount"] == "0.00"
    assert detail["preparation_status"] == "READY"


def test_cross_store_order_rejected(env, make_store, make_table, make_staff, db):
    other_store = make_store()
    other_order = env._make_order(other_store.id, make_table(other_store.id).id, Decimal("50.00"))
    # env.cashier belongs to env.store; the order is in another store → 404.
    res = _create_issue(env.cashier_client, other_order.id)
    assert res.status_code == 404


def test_unknown_field_rejected(env):
    order = env.new_order()
    res = _create_issue(env.cashier_client, order.id, extra={"amount": "5.00"})
    assert res.status_code == 422


def test_missing_csrf_rejected(env):
    order = env.new_order()
    # A client with no CSRF header.
    bad = make_authed_client(env.db, env.cashier)
    bad.headers.pop("X-CSRF-Token", None)
    res = bad.post(
        f"/orders/{order.id}/issues",
        json={"issue_type": "OTHER", "reason": "x"},
        headers={"Idempotency-Key": _key()},
    )
    assert res.status_code == 403


def test_missing_idempotency_key_rejected(env):
    order = env.new_order()
    res = env.cashier_client.post(
        f"/orders/{order.id}/issues",
        json={"issue_type": "OTHER", "reason": "x"},
    )
    assert res.status_code == 400


def test_requested_refund_over_refundable_rejected(env):
    order = env.new_order()
    _pay(env.manager_client, order.id)  # net paid 100
    res = _create_issue(env.cashier_client, order.id, requested="150.00")
    assert res.status_code == 409
    assert res.json()["detail"]["error"] == "requested_over_refundable"


def test_same_key_same_payload_replays(env):
    order = env.new_order()
    key = _key()
    first = _create_issue(env.cashier_client, order.id, key=key)
    second = _create_issue(env.cashier_client, order.id, key=key)
    assert first.status_code == 200 and second.status_code == 200
    assert first.json()["id"] == second.json()["id"]
    assert second.json()["idempotent_replay"] is True
    rows = env.db.query(OrderIssue).filter(OrderIssue.order_id == order.id).all()
    assert len(rows) == 1


def test_same_key_different_payload_conflicts(env):
    order = env.new_order()
    key = _key()
    _create_issue(env.cashier_client, order.id, reason="ilk", key=key)
    res = _create_issue(env.cashier_client, order.id, reason="farklı", key=key)
    assert res.status_code == 409
    assert res.json()["detail"]["error"] == "idempotency_mismatch"


def test_raw_idempotency_key_never_stored(env):
    order = env.new_order()
    key = "raw-secret-key-" + uuid.uuid4().hex
    _create_issue(env.cashier_client, order.id, key=key)
    row = env.db.query(OrderIssue).filter(OrderIssue.order_id == order.id).first()
    assert row.created_idempotency_key_hash != key
    assert len(row.created_idempotency_key_hash) == 64
    for value in (row.reason, row.note, row.created_request_hash):
        assert key not in (value or "")


def _max_audit_id(db) -> int:
    from sqlalchemy import func
    return db.query(func.coalesce(func.max(AuditLog.id), 0)).scalar()


def test_audit_create_written_once(env):
    order = env.new_order()
    # Scope to audit rows created by THIS operation: AuditLog is append-only and is
    # never cleaned, and the order_issues id sequence can be reset by a migration
    # round-trip test, so a bare entity_id count can collide with a stale row.
    before = _max_audit_id(env.db)
    res = _create_issue(env.cashier_client, order.id)
    issue_id = res.json()["id"]
    n = env.db.query(AuditLog).filter(
        AuditLog.id > before,
        AuditLog.entity_type == "order_issue",
        AuditLog.entity_id == issue_id,
        AuditLog.action == "ORDER_ISSUE_CREATED",
    ).count()
    assert n == 1


# ── Resolve ───────────────────────────────────────────────────────────────────

def test_cashier_can_resolve_no_refund(env):
    order = env.new_order()
    issue_id = _create_issue(env.cashier_client, order.id).json()["id"]
    res = _resolve(env.cashier_client, issue_id, "NO_REFUND")
    assert res.status_code == 200, res.text
    assert res.json()["status"] == "RESOLVED"
    assert res.json()["resolution_type"] == "NO_REFUND"
    assert res.json()["refund_id"] is None


def test_cashier_cannot_refund(env):
    order = env.new_order()
    _pay(env.manager_client, order.id)
    issue_id = _create_issue(env.cashier_client, order.id).json()["id"]
    res = _resolve(env.cashier_client, issue_id, "FULL_REFUND")
    assert res.status_code == 403
    assert res.json()["detail"]["error"] == "forbidden"


def test_manager_full_refund_uses_remaining_refundable(env):
    order = env.new_order()
    _pay(env.manager_client, order.id)  # 100 paid
    issue_id = _create_issue(env.manager_client, order.id).json()["id"]
    res = _resolve(env.manager_client, issue_id, "FULL_REFUND")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["approved_refund_amount"] == "100.00"
    assert body["refund_id"] is not None
    detail = env.manager_client.get(f"/cashier/orders/{order.id}").json()
    assert detail["refunded_amount"] == "100.00"
    assert detail["refund_status"] == "REFUNDED"
    # A full refund voids the order.
    assert detail["preparation_status"] == "CANCELLED"


def test_manager_partial_refund_uses_approved_amount(env):
    order = env.new_order()
    _pay(env.manager_client, order.id)
    issue_id = _create_issue(env.manager_client, order.id).json()["id"]
    res = _resolve(env.manager_client, issue_id, "PARTIAL_REFUND", approved="40.00")
    assert res.status_code == 200, res.text
    assert res.json()["approved_refund_amount"] == "40.00"
    detail = env.manager_client.get(f"/cashier/orders/{order.id}").json()
    assert detail["refunded_amount"] == "40.00"
    # A partial refund leaves the order active.
    assert detail["preparation_status"] == "READY"


def test_no_refund_creates_no_refund_row(env):
    order = env.new_order()
    _pay(env.manager_client, order.id)
    issue_id = _create_issue(env.manager_client, order.id).json()["id"]
    _resolve(env.manager_client, issue_id, "NO_REFUND")
    n = env.db.query(PaymentRefund).filter(PaymentRefund.order_id == order.id).count()
    assert n == 0


def test_partial_refund_over_remaining_rejected(env):
    order = env.new_order()
    _pay(env.manager_client, order.id)
    issue_id = _create_issue(env.manager_client, order.id).json()["id"]
    res = _resolve(env.manager_client, issue_id, "PARTIAL_REFUND", approved="150.00")
    assert res.status_code == 409
    assert res.json()["detail"]["error"] == "approved_over_refundable"


def test_full_refund_with_nothing_refundable_rejected(env):
    order = env.new_order()  # unpaid
    issue_id = _create_issue(env.manager_client, order.id).json()["id"]
    res = _resolve(env.manager_client, issue_id, "FULL_REFUND")
    assert res.status_code == 409
    assert res.json()["detail"]["error"] == "nothing_refundable"


def test_cancel_only_on_unpaid_order_cancels(env):
    order = env.new_order()  # unpaid
    issue_id = _create_issue(env.cashier_client, order.id).json()["id"]
    res = _resolve(env.cashier_client, issue_id, "CANCEL_ONLY")
    assert res.status_code == 200, res.text
    detail = env.manager_client.get(f"/cashier/orders/{order.id}").json()
    assert detail["preparation_status"] == "CANCELLED"


def test_cancel_only_on_paid_order_rejected(env):
    order = env.new_order()
    _pay(env.manager_client, order.id)
    issue_id = _create_issue(env.cashier_client, order.id).json()["id"]
    res = _resolve(env.cashier_client, issue_id, "CANCEL_ONLY")
    assert res.status_code == 409
    assert res.json()["detail"]["error"] == "cancel_blocked_paid"


def test_replay_resolve_does_not_duplicate_refund(env):
    order = env.new_order()
    _pay(env.manager_client, order.id)
    issue_id = _create_issue(env.manager_client, order.id).json()["id"]
    key = _key()
    first = _resolve(env.manager_client, issue_id, "FULL_REFUND", key=key)
    second = _resolve(env.manager_client, issue_id, "FULL_REFUND", key=key)
    assert first.status_code == 200 and second.status_code == 200
    assert second.json()["idempotent_replay"] is True
    n = env.db.query(PaymentRefund).filter(PaymentRefund.order_id == order.id).count()
    assert n == 1
    detail = env.manager_client.get(f"/cashier/orders/{order.id}").json()
    assert detail["refunded_amount"] == "100.00"


def test_same_resolve_key_different_payload_conflicts(env):
    order = env.new_order()
    _pay(env.manager_client, order.id)
    issue_id = _create_issue(env.manager_client, order.id).json()["id"]
    key = _key()
    _resolve(env.manager_client, issue_id, "PARTIAL_REFUND", approved="30.00", key=key)
    res = _resolve(env.manager_client, issue_id, "PARTIAL_REFUND", approved="40.00", key=key)
    assert res.status_code == 409
    assert res.json()["detail"]["error"] == "idempotency_mismatch"


def test_already_resolved_different_key_rejected(env):
    order = env.new_order()
    issue_id = _create_issue(env.cashier_client, order.id).json()["id"]
    _resolve(env.cashier_client, issue_id, "NO_REFUND")
    res = _resolve(env.cashier_client, issue_id, "NO_REFUND")  # fresh key
    assert res.status_code == 409
    assert res.json()["detail"]["error"] == "already_resolved"


def test_cross_store_issue_resolve_rejected(env, make_store, make_staff, db):
    order = env.new_order()
    issue_id = _create_issue(env.cashier_client, order.id).json()["id"]
    other_store = make_store()
    other_mgr = make_authed_client(db, make_staff("MANAGER", store_id=other_store.id))
    res = _resolve(other_mgr, issue_id, "NO_REFUND")
    assert res.status_code == 404


def test_audit_resolve_written_once(env):
    order = env.new_order()
    _pay(env.manager_client, order.id)
    issue_id = _create_issue(env.manager_client, order.id).json()["id"]
    before = _max_audit_id(env.db)
    _resolve(env.manager_client, issue_id, "FULL_REFUND")
    n = env.db.query(AuditLog).filter(
        AuditLog.id > before,
        AuditLog.entity_type == "order_issue",
        AuditLog.entity_id == issue_id,
        AuditLog.action == "ORDER_ISSUE_RESOLVED",
    ).count()
    assert n == 1


def test_refund_linked_to_issue(env):
    order = env.new_order()
    _pay(env.manager_client, order.id)
    issue_id = _create_issue(env.manager_client, order.id).json()["id"]
    body = _resolve(env.manager_client, issue_id, "FULL_REFUND").json()
    refund = env.db.get(PaymentRefund, body["refund_id"])
    assert refund.order_issue_id == issue_id


# ── Reads ─────────────────────────────────────────────────────────────────────

def test_order_issue_list_is_store_scoped(env, make_store, make_staff, db):
    order = env.new_order()
    _create_issue(env.cashier_client, order.id)
    other_store = make_store()
    other = make_authed_client(db, make_staff("CASHIER", store_id=other_store.id))
    assert other.get("/order-issues").json()["issues"] == []
    assert len(env.cashier_client.get("/order-issues").json()["issues"]) == 1


def test_issue_detail_store_scoped(env, make_store, make_staff, db):
    order = env.new_order()
    issue_id = _create_issue(env.cashier_client, order.id).json()["id"]
    other = make_authed_client(db, make_staff("CASHIER", store_id=make_store().id))
    assert other.get(f"/order-issues/{issue_id}").status_code == 404
    assert env.cashier_client.get(f"/order-issues/{issue_id}").status_code == 200


def test_order_specific_issues_endpoint(env):
    order = env.new_order()
    _create_issue(env.cashier_client, order.id, issue_type="WRONG_ITEM")
    _create_issue(env.cashier_client, order.id, issue_type="MISSING_ITEM")
    res = env.cashier_client.get(f"/orders/{order.id}/issues")
    assert res.status_code == 200
    assert len(res.json()["issues"]) == 2


def test_filters_status_and_type(env):
    o1 = env.new_order()
    o2 = env.new_order()
    i1 = _create_issue(env.cashier_client, o1.id, issue_type="QUALITY_PROBLEM").json()["id"]
    _create_issue(env.cashier_client, o2.id, issue_type="STAFF_ERROR")
    _resolve(env.cashier_client, i1, "NO_REFUND")
    open_only = env.cashier_client.get("/order-issues?status=OPEN").json()["issues"]
    assert all(i["status"] == "OPEN" for i in open_only)
    typed = env.cashier_client.get("/order-issues?issue_type=QUALITY_PROBLEM").json()["issues"]
    assert all(i["issue_type"] == "QUALITY_PROBLEM" for i in typed)
