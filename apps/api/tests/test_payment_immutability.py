"""
Append-only ledger immutability, enforced at the database boundary.

The settlement / allocation / refund tables are append-only: completed rows can
never be edited or deleted through a normal connection. A correction is always a
new refund row, never a mutation. These tests drive raw SQL (bypassing the
service) and prove PostgreSQL rejects:

  1. UPDATE of a completed settlement,
  2. DELETE of a completed settlement,
  3. UPDATE of an allocation,
  4. DELETE of an allocation,
  5. UPDATE of a refund,
  6. DELETE of a refund.

Adversarial GUC tests then prove the immutability trigger has NO application-
accessible runtime bypass. An earlier design honoured a custom GUC
(``sweetops.ledger_admin``); because a dotted custom GUC is settable by ANY role
— including through an SQL-injection path in an ordinary query — it was never a
real privilege boundary and has been removed. These tests prove that neither
``SET LOCAL sweetops.ledger_admin = 'on'`` nor
``set_config('sweetops.ledger_admin', 'on', true)`` permits any UPDATE or DELETE.

Test teardown removes the committed ``collected_ledger`` rows through the
fixtures, which disable the immutability triggers with ownership-gated DDL
(``ALTER TABLE ... DISABLE TRIGGER``) — a privilege the ordinary application role
does not hold in a correctly provisioned deployment and which no SET/set_config
can substitute for.
"""
import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.core.db import engine


def _exec(sql: str, params: dict) -> None:
    with engine.begin() as conn:
        conn.execute(text(sql), params)


def _exec_with_guc(guc_sql: str, sql: str, params: dict) -> None:
    """Set the (formerly privileged) GUC and attempt a mutation in ONE
    transaction, so a GUC that was honoured would take effect for the mutation."""
    with engine.begin() as conn:
        conn.execute(text(guc_sql))
        conn.execute(text(sql), params)


# ── Settlement ─────────────────────────────────────────────────────────────────

def test_update_completed_settlement_rejected(collected_ledger):
    with pytest.raises(IntegrityError):
        _exec("UPDATE payment_settlements SET note = 'edited' WHERE id = :id",
              {"id": collected_ledger.settlement_id})


def test_delete_completed_settlement_rejected(collected_ledger):
    with pytest.raises(IntegrityError):
        _exec("DELETE FROM payment_settlements WHERE id = :id",
              {"id": collected_ledger.settlement_id})


# ── Allocation ─────────────────────────────────────────────────────────────────

def test_update_allocation_rejected(collected_ledger):
    with pytest.raises(IntegrityError):
        _exec("UPDATE payment_allocations SET amount = 1 WHERE id = :id",
              {"id": collected_ledger.allocation_id})


def test_delete_allocation_rejected(collected_ledger):
    with pytest.raises(IntegrityError):
        _exec("DELETE FROM payment_allocations WHERE id = :id",
              {"id": collected_ledger.allocation_id})


# ── Refund ─────────────────────────────────────────────────────────────────────

def test_update_refund_rejected(collected_ledger):
    with pytest.raises(IntegrityError):
        _exec("UPDATE payment_refunds SET amount = 1 WHERE id = :id",
              {"id": collected_ledger.refund_id})


def test_delete_refund_rejected(collected_ledger):
    with pytest.raises(IntegrityError):
        _exec("DELETE FROM payment_refunds WHERE id = :id",
              {"id": collected_ledger.refund_id})


# ── Adversarial: SET LOCAL sweetops.ledger_admin does NOT bypass ────────────────

_SET_LOCAL = "SET LOCAL sweetops.ledger_admin = 'on'"


def test_guc_set_local_does_not_permit_settlement_update(collected_ledger):
    with pytest.raises(IntegrityError):
        _exec_with_guc(_SET_LOCAL,
                       "UPDATE payment_settlements SET note = 'edited' WHERE id = :id",
                       {"id": collected_ledger.settlement_id})


def test_guc_set_local_does_not_permit_settlement_delete(collected_ledger):
    with pytest.raises(IntegrityError):
        _exec_with_guc(_SET_LOCAL,
                       "DELETE FROM payment_settlements WHERE id = :id",
                       {"id": collected_ledger.settlement_id})


def test_guc_set_local_does_not_permit_allocation_update(collected_ledger):
    with pytest.raises(IntegrityError):
        _exec_with_guc(_SET_LOCAL,
                       "UPDATE payment_allocations SET amount = 1 WHERE id = :id",
                       {"id": collected_ledger.allocation_id})


def test_guc_set_local_does_not_permit_allocation_delete(collected_ledger):
    with pytest.raises(IntegrityError):
        _exec_with_guc(_SET_LOCAL,
                       "DELETE FROM payment_allocations WHERE id = :id",
                       {"id": collected_ledger.allocation_id})


def test_guc_set_local_does_not_permit_refund_update(collected_ledger):
    with pytest.raises(IntegrityError):
        _exec_with_guc(_SET_LOCAL,
                       "UPDATE payment_refunds SET amount = 1 WHERE id = :id",
                       {"id": collected_ledger.refund_id})


def test_guc_set_local_does_not_permit_refund_delete(collected_ledger):
    with pytest.raises(IntegrityError):
        _exec_with_guc(_SET_LOCAL,
                       "DELETE FROM payment_refunds WHERE id = :id",
                       {"id": collected_ledger.refund_id})


# ── Adversarial: set_config(...) does NOT bypass ────────────────────────────────

_SET_CONFIG = "SELECT set_config('sweetops.ledger_admin', 'on', true)"


def test_set_config_does_not_permit_settlement_delete(collected_ledger):
    with pytest.raises(IntegrityError):
        _exec_with_guc(_SET_CONFIG,
                       "DELETE FROM payment_settlements WHERE id = :id",
                       {"id": collected_ledger.settlement_id})


def test_set_config_does_not_permit_allocation_update(collected_ledger):
    with pytest.raises(IntegrityError):
        _exec_with_guc(_SET_CONFIG,
                       "UPDATE payment_allocations SET amount = 1 WHERE id = :id",
                       {"id": collected_ledger.allocation_id})


def test_set_config_does_not_permit_refund_delete(collected_ledger):
    with pytest.raises(IntegrityError):
        _exec_with_guc(_SET_CONFIG,
                       "DELETE FROM payment_refunds WHERE id = :id",
                       {"id": collected_ledger.refund_id})


# ── Transaction rollback still works (rollback != DELETE) ───────────────────────

def test_transaction_rollback_discards_appended_row(collected_ledger):
    """
    Rollback is not the forbidden DELETE. An append (a new refund row) that is
    rolled back must simply vanish, and the committed ledger must be untouched —
    proving ordinary rollback-based flows remain fully supported without any
    bypass.
    """
    env = collected_ledger
    import uuid

    def _refund_count() -> int:
        with engine.connect() as conn:
            return conn.execute(
                text("SELECT count(*) FROM payment_refunds WHERE allocation_id = :a"),
                {"a": env.allocation_id},
            ).scalar()

    before = _refund_count()

    conn = engine.connect()
    trans = conn.begin()
    conn.execute(
        text(
            "INSERT INTO payment_refunds "
            "(store_id, settlement_id, allocation_id, order_id, amount, currency, "
            " reason, refunded_by_user_id, idempotency_key_hash, request_hash) "
            "VALUES (:store, :s, :a, :o, 1.00, 'TRY', 'rollback probe', :u, :k, :r)"
        ),
        {"store": env.store.id, "s": env.settlement_id, "a": env.allocation_id,
         "o": env.order.id, "u": env.manager.id, "k": uuid.uuid4().hex,
         "r": uuid.uuid4().hex},
    )
    # Visible inside the open transaction …
    assert conn.execute(
        text("SELECT count(*) FROM payment_refunds WHERE allocation_id = :a"),
        {"a": env.allocation_id},
    ).scalar() == before + 1
    # … then rolled back — the speculative row disappears.
    trans.rollback()
    conn.close()

    assert _refund_count() == before
