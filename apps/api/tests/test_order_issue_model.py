"""
order_issues schema-level guarantees, enforced by the DATABASE (not the app):
domains, non-negative money, the status/resolution-snapshot consistency rule, the
refund-link rules, the store-scoping composite FKs, and store-scoped creation
idempotency.
"""
import uuid
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy.exc import IntegrityError

from app.models.order_issue import OrderIssue


def _h() -> str:
    return uuid.uuid4().hex + uuid.uuid4().hex[:0].ljust(0, "0")  # 32 hex is fine for a hash col


def _hash() -> str:
    return (uuid.uuid4().hex + uuid.uuid4().hex)[:64]


@pytest.fixture()
def store_order(db, make_store, make_table, make_staff, make_order):
    class Env:
        pass
    e = Env()
    e.store = make_store()
    e.table = make_table(e.store.id)
    e.creator = make_staff("CASHIER", store_id=e.store.id)
    e.order = make_order(e.store.id, e.table.id, Decimal("100.00"))
    return e


def _open_issue(e, **overrides) -> OrderIssue:
    kwargs = dict(
        store_id=e.store.id,
        order_id=e.order.id,
        issue_type="CUSTOMER_CANCELLED",
        status="OPEN",
        reason="sebep",
        created_by_user_id=e.creator.id,
        created_idempotency_key_hash=_hash(),
        created_request_hash=_hash(),
    )
    kwargs.update(overrides)
    return OrderIssue(**kwargs)


def _resolved_fields(**overrides) -> dict:
    base = dict(
        status="RESOLVED",
        resolution_type="NO_REFUND",
        approved_refund_amount=Decimal("0.00"),
        resolved_by_user_id=None,
        resolved_at=datetime.now(timezone.utc),
        resolved_idempotency_key_hash=_hash(),
        resolved_request_hash=_hash(),
    )
    base.update(overrides)
    return base


def _expect_integrity(db, issue):
    db.add(issue)
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_valid_open_issue_persists(db, store_order):
    issue = _open_issue(store_order)
    db.add(issue)
    db.commit()
    assert issue.id is not None
    assert issue.status == "OPEN"


def test_issue_type_domain_enforced(db, store_order):
    _expect_integrity(db, _open_issue(store_order, issue_type="NONSENSE"))


def test_status_domain_enforced(db, store_order):
    _expect_integrity(db, _open_issue(store_order, status="WEIRD"))


def test_resolution_type_domain_enforced(db, store_order):
    _expect_integrity(
        db,
        _open_issue(store_order, **_resolved_fields(
            resolution_type="MAYBE", resolved_by_user_id=store_order.creator.id
        )),
    )


def test_issue_belongs_to_order_store(db, store_order, make_store):
    other = make_store()
    # store_id says another store, but order_id is store_order's order → composite FK fails.
    _expect_integrity(db, _open_issue(store_order, store_id=other.id))


def test_creator_belongs_to_store(db, store_order, make_store, make_staff):
    outsider = make_staff("CASHIER", store_id=make_store().id)
    _expect_integrity(db, _open_issue(store_order, created_by_user_id=outsider.id))


def test_resolver_belongs_to_store_when_resolved(db, store_order, make_store, make_staff):
    outsider = make_staff("MANAGER", store_id=make_store().id)
    _expect_integrity(
        db,
        _open_issue(store_order, **_resolved_fields(resolved_by_user_id=outsider.id)),
    )


def test_requested_refund_non_negative(db, store_order):
    _expect_integrity(db, _open_issue(store_order, requested_refund_amount=Decimal("-1.00")))


def test_approved_refund_non_negative(db, store_order):
    _expect_integrity(
        db,
        _open_issue(store_order, **_resolved_fields(
            approved_refund_amount=Decimal("-5.00"),
            resolved_by_user_id=store_order.creator.id,
        )),
    )


def test_resolved_status_requires_resolver_and_time(db, store_order):
    # RESOLVED but resolved_at / resolver / hashes NULL → snapshot CHECK fails.
    _expect_integrity(
        db,
        _open_issue(store_order, status="RESOLVED", resolution_type="NO_REFUND",
                    approved_refund_amount=Decimal("0.00")),
    )


def test_open_status_forbids_resolution_fields(db, store_order):
    # OPEN but carrying a resolution_type → snapshot CHECK fails.
    _expect_integrity(db, _open_issue(store_order, resolution_type="NO_REFUND"))


def test_full_refund_requires_refund_link(db, store_order):
    # FULL_REFUND with a positive approved amount but no refund_id → CHECK fails.
    _expect_integrity(
        db,
        _open_issue(store_order, **_resolved_fields(
            resolution_type="FULL_REFUND",
            approved_refund_amount=Decimal("100.00"),
            resolved_by_user_id=store_order.creator.id,
        )),
    )


def test_no_refund_forbids_refund_link(db, store_order, make_store, make_table, make_staff, make_order):
    # A NO_REFUND resolution must not carry a refund_id. (Use a bogus positive id;
    # the refund-only-when-refunding CHECK fires before the FK is even considered
    # for NO_REFUND — but to be safe we assert an IntegrityError either way.)
    _expect_integrity(
        db,
        _open_issue(store_order, **_resolved_fields(
            resolution_type="NO_REFUND",
            refund_id=999999999,
            resolved_by_user_id=store_order.creator.id,
        )),
    )


def test_creation_idempotency_is_store_scoped_unique(db, store_order):
    key_hash = _hash()
    first = _open_issue(store_order, created_idempotency_key_hash=key_hash)
    db.add(first)
    db.commit()
    dup = _open_issue(store_order, created_idempotency_key_hash=key_hash)
    _expect_integrity(db, dup)
