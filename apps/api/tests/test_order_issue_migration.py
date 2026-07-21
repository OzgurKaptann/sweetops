"""
order_issues migration: single Alembic head, the immutability trigger, and the
downgrade guard that refuses to destroy real issue history.
"""
import importlib.util
import subprocess
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.models.order_issue import OrderIssue
from tests.conftest import _order_issue_maintenance

API_DIR = Path(__file__).resolve().parents[1]
_MIGRATION_PATH = (
    API_DIR / "alembic" / "versions" / "e7f2a9c04d18_order_issue_refund_workflow.py"
)


def _load_migration():
    spec = importlib.util.spec_from_file_location("_issue_migration", _MIGRATION_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _hash() -> str:
    return (uuid.uuid4().hex + uuid.uuid4().hex)[:64]


@pytest.fixture()
def open_issue(db, make_store, make_table, make_staff, make_order):
    store = make_store()
    table = make_table(store.id)
    creator = make_staff("CASHIER", store_id=store.id)
    order = make_order(store.id, table.id, Decimal("100.00"))
    issue = OrderIssue(
        store_id=store.id, order_id=order.id, issue_type="OTHER", status="OPEN",
        reason="sebep", created_by_user_id=creator.id,
        created_idempotency_key_hash=_hash(), created_request_hash=_hash(),
    )
    db.add(issue)
    db.commit()
    return issue


# ── Immutability trigger ──────────────────────────────────────────────────────

def test_delete_refused_without_ownership_bypass(db, open_issue):
    with pytest.raises(IntegrityError):
        db.execute(text("DELETE FROM order_issues WHERE id=:id"), {"id": open_issue.id})
        db.flush()
    db.rollback()
    # The ownership-gated teardown escape hatch still works.
    with _order_issue_maintenance(db):
        db.query(OrderIssue).filter(OrderIssue.id == open_issue.id).delete()
    db.commit()


def test_resolved_issue_is_immutable(db, open_issue):
    # Resolve it first (the one permitted OPEN → RESOLVED transition).
    db.execute(
        text(
            "UPDATE order_issues SET status='RESOLVED', resolution_type='NO_REFUND', "
            "approved_refund_amount=0, resolved_by_user_id=created_by_user_id, "
            "resolved_at=now(), resolved_idempotency_key_hash=:h, resolved_request_hash=:h "
            "WHERE id=:id"
        ),
        {"h": _hash(), "id": open_issue.id},
    )
    db.commit()
    # A second UPDATE of a resolved issue is refused.
    with pytest.raises(IntegrityError):
        db.execute(text("UPDATE order_issues SET reason='tampered' WHERE id=:id"),
                   {"id": open_issue.id})
        db.flush()
    db.rollback()
    with _order_issue_maintenance(db):
        db.query(OrderIssue).filter(OrderIssue.id == open_issue.id).delete()
    db.commit()


def test_open_creation_snapshot_is_immutable(db, open_issue):
    # An OPEN → RESOLVED transition that also tampers with the creation snapshot fails.
    with pytest.raises(IntegrityError):
        db.execute(
            text(
                "UPDATE order_issues SET status='RESOLVED', resolution_type='NO_REFUND', "
                "approved_refund_amount=0, resolved_by_user_id=created_by_user_id, "
                "resolved_at=now(), resolved_idempotency_key_hash=:h, resolved_request_hash=:h, "
                "reason='changed-during-resolve' WHERE id=:id"
            ),
            {"h": _hash(), "id": open_issue.id},
        )
        db.flush()
    db.rollback()


# ── Migration structure ───────────────────────────────────────────────────────

def test_alembic_single_head():
    out = subprocess.run(
        ["python", "-m", "alembic", "heads"],
        cwd=API_DIR, capture_output=True, text=True,
    )
    heads = [l for l in out.stdout.splitlines() if l.strip() and "head" in l]
    assert len(heads) == 1, out.stdout


def test_downgrade_refuses_while_issues_exist(db, open_issue):
    """
    A downgrade that would destroy real issue history must abort. We prove the guard
    fires while an issue exists — without actually downgrading the live test database.
    """
    migration = _load_migration()
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    ctx = MigrationContext.configure(db.connection())
    with Operations.context(ctx):
        with pytest.raises(migration.OrderIssuesExist):
            migration.downgrade()
    db.rollback()

    with _order_issue_maintenance(db):
        db.query(OrderIssue).filter(OrderIssue.id == open_issue.id).delete()
    db.commit()
