"""
Alembic migration integrity for the inventory transfer workflow (``f4b8c1d90e26``).

Two things are proved here, and the second matters more than the first.

  1. The schema really exists in PostgreSQL: the transfer table, the movement
     columns, the composite leg keys, the widened movement-type domain, the
     source-store-scoped idempotency uniqueness, and the deferred pairing trigger.
     And it round-trips: downgrade removes exactly this branch and nothing else,
     re-upgrade restores it, and Alembic keeps a single head.

  2. The downgrade REFUSES TO DESTROY EVIDENCE. Dropping inventory_transfers would
     delete the only record that a shipment between two branches ever happened,
     while leaving the stock it moved exactly where it moved it — a bare -2 kg in
     one store's ledger and +2 kg in another's, with nothing to explain either, and
     no way to reconstruct the link afterwards. So it aborts while transfers exist,
     and ``test_downgrade_refuses_while_transfers_exist`` is what holds it to that.

The engine's pooled connections are disposed around every Alembic run, and the
database is always restored to head on the way out.
"""
import subprocess
import uuid
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import text

from app.core.db import engine
from tests.conftest import (
    DEFAULT_STORE_ID,
    cleanup_ingredient,
    make_authed_client,
    make_ingredient,
)

_API_DIR = Path(__file__).resolve().parents[1]

_REVISION = "f4b8c1d90e26"          # inventory transfer workflow (head)
_PREVIOUS = "e2c9a4b16d38"          # store-scoped inventory

_PAIRING_TRIGGERS = (
    ("inventory_transfers", "trg_inventory_transfers_paired"),
    ("ingredient_stock_movements", "trg_transfer_movement_paired"),
)

# Each of these makes a class of corruption unrepresentable rather than merely
# unwritten by the current code.
_TRANSFER_CONSTRAINTS = (
    "ck_transfer_quantity_positive",       # 1. quantity > 0
    "ck_transfer_stores_differ",           # 2. source <> destination
    "fk_transfer_source_store",            # 3. source store exists
    "fk_transfer_destination_store",       # 4. destination store exists
    "fk_transfer_ingredient",              # 5. ingredient exists
    "fk_transfer_actor_source_store",      # 6. initiator belongs to the source store
    "uq_transfer_source_idem",             # 7. idempotency scoped by source store
    "fk_movement_transfer_source_leg",     # 8. OUT leg ↔ transfer's source side
    "fk_movement_transfer_destination_leg",# 9. IN  leg ↔ transfer's destination side
    "ck_movement_transfer_link",           # transfer_id ⟺ a transfer movement type
    "ck_movement_transfer_in_no_actor",    # the inbound leg carries no actor
)

_MOVEMENT_TRANSFER_COLUMNS = ("transfer_id", "transfer_out_store_id", "transfer_in_store_id")


# ---------------------------------------------------------------------------
# Alembic / introspection helpers
# ---------------------------------------------------------------------------

def _alembic_raw(*args: str) -> subprocess.CompletedProcess:
    # Dispose pooled connections: an idle-in-transaction connection would block
    # the ACCESS EXCLUSIVE locks DDL needs, and the migration would hang.
    engine.dispose()
    return subprocess.run(
        ["alembic", *args], cwd=str(_API_DIR), capture_output=True, text=True
    )


def _alembic(*args: str) -> None:
    proc = _alembic_raw(*args)
    assert proc.returncode == 0, (
        f"alembic {' '.join(args)} failed:\n{proc.stdout}\n{proc.stderr}"
    )


def _release(db) -> None:
    """Fully release the test session's connection before running DDL — an
    idle-in-transaction connection makes ALTER TABLE wait forever."""
    db.rollback()
    db.close()


def _scalar(sql: str, **params):
    with engine.connect() as conn:
        return conn.execute(text(sql), params).scalar()


def _table_exists(table: str) -> bool:
    return _scalar(
        "SELECT count(*) FROM information_schema.tables WHERE table_name = :t", t=table
    ) > 0


def _column_exists(table: str, column: str) -> bool:
    return _scalar(
        "SELECT count(*) FROM information_schema.columns "
        "WHERE table_name = :t AND column_name = :c",
        t=table, c=column,
    ) > 0


def _constraint_exists(name: str) -> bool:
    return _scalar("SELECT count(*) FROM pg_constraint WHERE conname = :n", n=name) > 0


def _index_exists(name: str) -> bool:
    return _scalar("SELECT count(*) FROM pg_indexes WHERE indexname = :n", n=name) > 0


def _trigger_exists(table: str, trigger: str) -> bool:
    return _scalar(
        "SELECT count(*) FROM pg_trigger t JOIN pg_class c ON t.tgrelid = c.oid "
        "WHERE t.tgname = :trig AND c.relname = :tbl AND NOT t.tgisinternal",
        trig=trigger, tbl=table,
    ) > 0


def _check_clause(name: str) -> str:
    return _scalar(
        "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = :n", n=name
    ) or ""


@pytest.fixture(autouse=True)
def _restore_head():
    """Whatever a test does to the schema, put the database back at head."""
    yield
    _alembic("upgrade", "head")


# ═══════════════════════════════════════════════════════════════════════════
# 1. The schema exists
# ═══════════════════════════════════════════════════════════════════════════

def test_alembic_has_a_single_head():
    """Two heads mean `alembic upgrade head` is ambiguous and deployment is a
    coin toss. See docs/ALEMBIC_SINGLE_HEAD_RESOLUTION.md."""
    proc = _alembic_raw("heads")
    assert proc.returncode == 0, proc.stderr
    heads = [ln for ln in proc.stdout.splitlines() if ln.strip()]
    assert len(heads) == 1, f"Alembic must have exactly one head, got: {heads}"
    assert _REVISION in heads[0]


def test_transfer_table_and_movement_columns_exist():
    assert _table_exists("inventory_transfers")
    for column in _MOVEMENT_TRANSFER_COLUMNS:
        assert _column_exists("ingredient_stock_movements", column), column


def test_every_transfer_constraint_exists():
    for name in _TRANSFER_CONSTRAINTS:
        assert _constraint_exists(name), f"missing constraint {name}"


def test_movement_type_domain_includes_both_transfer_types():
    clause = _check_clause("ck_movement_type_domain")
    assert "TRANSFER_OUT" in clause
    assert "TRANSFER_IN" in clause


def test_delta_rule_pins_each_transfer_direction_to_its_sign():
    """TRANSFER_OUT must remove stock and TRANSFER_IN must add it — and neither may
    touch reserved. A TRANSFER_OUT that ADDS stock is not a transfer, it is a
    fabrication."""
    clause = _check_clause("ck_movement_delta_matches_type")
    assert "TRANSFER_OUT" in clause
    assert "TRANSFER_IN" in clause


def test_the_direction_columns_are_generated_by_the_database():
    """
    The application must not be able to forge which SIDE of a transfer a movement
    is on — that is the whole basis of the leg foreign keys. So the columns are
    GENERATED ALWAYS ... STORED, not application-written.
    """
    for column in ("transfer_out_store_id", "transfer_in_store_id"):
        generated = _scalar(
            "SELECT is_generated FROM information_schema.columns "
            "WHERE table_name = 'ingredient_stock_movements' AND column_name = :c",
            c=column,
        )
        assert generated == "ALWAYS", f"{column} is not generated: {generated}"


def test_transfer_idempotency_uniqueness_is_scoped_to_the_source_store():
    """Not a global unique key: two branches sending the same Idempotency-Key are
    two independent commands, and a global key would make one silently replay the
    other's result."""
    clause = _scalar(
        "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
        "WHERE conname = 'uq_transfer_source_idem'"
    )
    assert "source_store_id" in clause
    assert "idempotency_key_hash" in clause


def test_the_pairing_trigger_is_installed_and_deferred():
    """
    A one-sided transfer — stock that left a branch and arrived nowhere — cannot be
    caught by any per-row constraint, because "this transfer has both halves" is a
    statement about a SET of rows. It is a DEFERRED constraint trigger, checked at
    COMMIT, and it must be deferred: the two legs cannot both exist at the instant
    the first one is inserted.
    """
    for table, trigger in _PAIRING_TRIGGERS:
        assert _trigger_exists(table, trigger), f"missing {trigger} on {table}"
        deferred = _scalar(
            "SELECT t.tgdeferrable AND t.tginitdeferred FROM pg_trigger t "
            "JOIN pg_class c ON c.oid = t.tgrelid "
            "WHERE t.tgname = :trig AND c.relname = :tbl",
            trig=trigger, tbl=table,
        )
        assert deferred is True, f"{trigger} is not DEFERRABLE INITIALLY DEFERRED"


def test_the_pairing_function_has_no_application_reachable_bypass():
    """Same rules as the append-only trigger it sits beside: a pinned search_path so
    object resolution cannot be diverted, and EXECUTE revoked from PUBLIC."""
    config = _scalar(
        "SELECT array_to_string(proconfig, ',') FROM pg_proc "
        "WHERE proname = 'sweetops_check_transfer_pairing'"
    )
    assert config and "search_path=pg_catalog" in config.replace(" ", "")

    public_can_execute = _scalar(
        "SELECT has_function_privilege('public', "
        "'public.sweetops_check_transfer_pairing()', 'EXECUTE')"
    )
    assert public_can_execute is False


def test_the_append_only_ledger_guard_still_stands():
    """This branch adds columns to the ledger; it must not have disturbed the
    trigger that makes it append-only."""
    assert _trigger_exists(
        "ingredient_stock_movements", "trg_ingredient_stock_movements_immutable"
    )


# ═══════════════════════════════════════════════════════════════════════════
# 2. Round trip: downgrade removes only this branch, re-upgrade restores it
# ═══════════════════════════════════════════════════════════════════════════

def test_downgrade_removes_only_this_branch_and_reupgrade_restores_it(db):
    """
    Downgrade with no transfers present (the only case it permits), and prove that
    what it takes away is exactly this branch's schema — the store-scoped inventory
    below it, and the orders and payments beside it, are untouched.
    """
    orders_before = _scalar("SELECT count(*) FROM orders")
    movements_before = _scalar("SELECT count(*) FROM ingredient_stock_movements")
    settlements_before = _scalar("SELECT count(*) FROM payment_settlements")

    _release(db)
    _alembic("downgrade", _PREVIOUS)

    assert _scalar("SELECT version_num FROM alembic_version") == _PREVIOUS

    # ── This branch's schema is gone... ────────────────────────────────────
    assert not _table_exists("inventory_transfers")
    for column in _MOVEMENT_TRANSFER_COLUMNS:
        assert not _column_exists("ingredient_stock_movements", column), column
    for name in _TRANSFER_CONSTRAINTS:
        assert not _constraint_exists(name), f"{name} survived the downgrade"
    assert not _index_exists("uq_movement_transfer_direction")
    for table, trigger in _PAIRING_TRIGGERS:
        assert not _trigger_exists(table, trigger)

    # ...including the widened type domain: the pre-transfer rule is back verbatim.
    clause = _check_clause("ck_movement_type_domain")
    assert "TRANSFER_OUT" not in clause
    assert "TRANSFER_IN" not in clause

    # ── ...and NOTHING else is. ────────────────────────────────────────────
    assert _scalar("SELECT count(*) FROM orders") == orders_before
    assert _scalar("SELECT count(*) FROM ingredient_stock_movements") == movements_before
    assert _scalar("SELECT count(*) FROM payment_settlements") == settlements_before
    # The store-scoped inventory below this branch survives...
    assert _column_exists("ingredient_stock", "store_id")
    assert _constraint_exists("fk_movement_actor_store")
    # ...as does the append-only guard.
    assert _trigger_exists(
        "ingredient_stock_movements", "trg_ingredient_stock_movements_immutable"
    )

    # ── Re-upgrade ─────────────────────────────────────────────────────────
    _alembic("upgrade", "head")

    assert _scalar("SELECT version_num FROM alembic_version") == _REVISION
    assert _table_exists("inventory_transfers")
    for name in _TRANSFER_CONSTRAINTS:
        assert _constraint_exists(name), f"{name} did not come back"
    for table, trigger in _PAIRING_TRIGGERS:
        assert _trigger_exists(table, trigger)
    assert "TRANSFER_OUT" in _check_clause("ck_movement_type_domain")

    assert _scalar("SELECT count(*) FROM orders") == orders_before
    assert _scalar("SELECT count(*) FROM payment_settlements") == settlements_before


def test_downgrade_refuses_while_transfers_exist(db, make_store, make_staff):
    """
    The migration REFUSES TO GUESS, in the same spirit as the store-scoped one
    before it.

    A completed transfer really moved chocolate between two branches. Dropping the
    table erases the event but not its effects: the source's ledger keeps a bare
    -2 kg and the destination's a bare +2 kg, with nothing left to say they are the
    same 2 kg — and those TRANSFER_OUT/TRANSFER_IN rows cannot even be expressed in
    the movement-type domain being restored. There is no correct reconstruction, so
    it aborts loudly rather than producing a database that looks fine and is quietly
    wrong about where the stock went.
    """
    dest = make_store("Beşiktaş")
    owner = make_staff("OWNER", store_id=DEFAULT_STORE_ID)
    client = make_authed_client(db, owner)
    ing, _ = make_ingredient(db, on_hand=Decimal("100.000"))
    ing_id = ing.id
    try:
        r = client.post(
            "/inventory/transfers",
            json={
                "destination_store_id": dest.id,
                "ingredient_id": ing_id,
                "quantity": "20.000",
                "reason": "şube takviyesi",
            },
            headers={"Idempotency-Key": uuid.uuid4().hex},
        )
        assert r.status_code == 200, r.text

        _release(db)
        proc = _alembic_raw("downgrade", _PREVIOUS)

        assert proc.returncode != 0, (
            "the downgrade destroyed a real transfer instead of refusing:\n"
            f"{proc.stdout}\n{proc.stderr}"
        )
        combined = proc.stdout + proc.stderr
        assert "TransfersExist" in combined or "transfer(s) exist" in combined, combined

        # Nothing was committed: the schema and the transfer are both intact.
        assert _scalar("SELECT version_num FROM alembic_version") == _REVISION
        assert _table_exists("inventory_transfers")
        assert _scalar(
            "SELECT count(*) FROM inventory_transfers WHERE ingredient_id = :i", i=ing_id
        ) == 1
    finally:
        _alembic("upgrade", "head")
        from app.core.db import SessionLocal
        fresh = SessionLocal()
        try:
            cleanup_ingredient(fresh, ing_id)
        finally:
            fresh.close()
