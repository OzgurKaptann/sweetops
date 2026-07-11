"""
Alembic migration integrity for the payment ledger.

Proves, against the real database, that the payment migration
(``b8c4d1e6f207``) is reversible and re-appliable and that its append-only
immutability guard is (re)installed on every upgrade:

  9.  Downgrading past the payment revision succeeds after the immutable
      triggers are installed, and removes every payment trigger, trigger
      function and table.
  10. ``alembic upgrade head`` recreates all three immutability triggers.

The payment migration is no longer the head revision — the inventory lifecycle
sits on top of it — so the downgrade names the revision BELOW payment instead of
using a relative ``-1``. Alembic unwinds the intervening revision on the way,
which is exactly what a real rollback past payment would have to do.

The test drops the engine's pooled connections around each Alembic run so the
schema-level ALTER/DROP statements are not blocked by an idle in-transaction
connection, and it always restores the database to ``head`` on the way out.
"""
import subprocess
from pathlib import Path

import pytest
from sqlalchemy import text

from app.core.db import engine

_API_DIR = Path(__file__).resolve().parents[1]

_IMMUTABLE_TRIGGERS = (
    ("payment_settlements", "trg_payment_settlements_immutable"),
    ("payment_allocations", "trg_payment_allocations_immutable"),
    ("payment_refunds", "trg_payment_refunds_immutable"),
)
_LEDGER_TABLES = ("payment_settlements", "payment_allocations", "payment_refunds")

# The revision immediately below the payment migration (staff auth + RBAC).
_BELOW_PAYMENT = "a7d3f9b21c05"


def _alembic(*args: str) -> None:
    # Dispose pooled connections first: an idle-in-transaction connection would
    # block the ACCESS EXCLUSIVE locks that DROP/ALTER TABLE need.
    engine.dispose()
    proc = subprocess.run(
        ["alembic", *args],
        cwd=str(_API_DIR),
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, (
        f"alembic {' '.join(args)} failed:\n{proc.stdout}\n{proc.stderr}"
    )


def _trigger_exists(table: str, trigger: str) -> bool:
    with engine.connect() as conn:
        return conn.execute(
            text(
                "SELECT count(*) FROM pg_trigger t "
                "JOIN pg_class c ON t.tgrelid = c.oid "
                "WHERE t.tgname = :trig AND c.relname = :tbl "
                "AND NOT t.tgisinternal"
            ),
            {"trig": trigger, "tbl": table},
        ).scalar() > 0


def _table_exists(table: str) -> bool:
    with engine.connect() as conn:
        return conn.execute(
            text("SELECT to_regclass(:t)"), {"t": f"public.{table}"}
        ).scalar() is not None


def _function_exists(name: str) -> bool:
    with engine.connect() as conn:
        return conn.execute(
            text("SELECT count(*) FROM pg_proc WHERE proname = :n"), {"n": name}
        ).scalar() > 0


@pytest.fixture()
def restore_head():
    """Guarantee the DB is back at head no matter how the test exits."""
    yield
    _alembic("upgrade", "head")
    for table, trig in _IMMUTABLE_TRIGGERS:
        assert _trigger_exists(table, trig)


def test_downgrade_then_reupgrade_reinstalls_immutability(restore_head):
    # Precondition: at head every immutable trigger is present.
    for table, trig in _IMMUTABLE_TRIGGERS:
        assert _trigger_exists(table, trig), f"{trig} missing before downgrade"

    # 9. Downgrade past payment removes its triggers, functions and tables cleanly.
    _alembic("downgrade", _BELOW_PAYMENT)
    for table in _LEDGER_TABLES:
        assert not _table_exists(table), f"{table} still present after downgrade"
    assert not _function_exists("sweetops_block_ledger_mutation")

    # 10. Re-upgrade recreates all three immutability triggers (asserted here and
    #     again by the restore_head fixture).
    _alembic("upgrade", "head")
    for table, trig in _IMMUTABLE_TRIGGERS:
        assert _trigger_exists(table, trig), f"{trig} not recreated on re-upgrade"
    assert _function_exists("sweetops_block_ledger_mutation")
