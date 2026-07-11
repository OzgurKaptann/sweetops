"""
Alembic migration integrity for the inventory lifecycle (``c3b7e01f9a24``).

Proves, against the real database, that the migration is reversible and
re-appliable, that its constraints and append-only guard really exist, and — the
part that matters most in production — that a rollback does not destroy orders
or payment data.

The engine's pooled connections are disposed around each Alembic run so the
schema-level ALTER/DROP statements are not blocked by an idle in-transaction
connection, and the database is always restored to ``head`` on the way out.
"""
import subprocess
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import text

from app.core.db import engine

_API_DIR = Path(__file__).resolve().parents[1]

_REVISION = "c3b7e01f9a24"
_PREVIOUS = "b8c4d1e6f207"          # payment settlement
_IMMUTABLE_TRIGGER = ("ingredient_stock_movements",
                      "trg_ingredient_stock_movements_immutable")


def _alembic(*args: str) -> None:
    # Dispose pooled connections: an idle-in-transaction connection would block
    # the ACCESS EXCLUSIVE locks that DROP/ALTER TABLE need, and the migration
    # would hang rather than fail.
    engine.dispose()
    proc = subprocess.run(
        ["alembic", *args], cwd=str(_API_DIR), capture_output=True, text=True
    )
    assert proc.returncode == 0, (
        f"alembic {' '.join(args)} failed:\n{proc.stdout}\n{proc.stderr}"
    )


def _alembic_after_releasing(db, *args: str) -> None:
    """
    Run Alembic after fully releasing the test session's connection.

    engine.dispose() only discards POOLED connections; the `db` fixture holds a
    checked-out one that is idle-in-transaction, and PostgreSQL will make the
    migration's ALTER TABLE wait on it indefinitely. Roll it back and close it
    first — otherwise the whole suite hangs instead of failing.
    """
    db.rollback()
    db.close()
    _alembic(*args)


def _scalar(sql: str, **params):
    with engine.connect() as conn:
        return conn.execute(text(sql), params).scalar()


def _column_exists(table: str, column: str) -> bool:
    return _scalar(
        "SELECT count(*) FROM information_schema.columns "
        "WHERE table_name = :t AND column_name = :c",
        t=table, c=column,
    ) > 0


def _constraint_exists(name: str) -> bool:
    return _scalar(
        "SELECT count(*) FROM pg_constraint WHERE conname = :n", n=name
    ) > 0


def _table_exists(table: str) -> bool:
    return _scalar("SELECT to_regclass(:t)", t=f"public.{table}") is not None


def _trigger_exists(table: str, trigger: str) -> bool:
    return _scalar(
        "SELECT count(*) FROM pg_trigger t JOIN pg_class c ON t.tgrelid = c.oid "
        "WHERE t.tgname = :trig AND c.relname = :tbl AND NOT t.tgisinternal",
        trig=trigger, tbl=table,
    ) > 0


def _single_head() -> str:
    engine.dispose()
    proc = subprocess.run(
        ["alembic", "heads"], cwd=str(_API_DIR), capture_output=True, text=True
    )
    assert proc.returncode == 0, proc.stderr
    heads = [ln for ln in proc.stdout.splitlines() if ln.strip()]
    assert len(heads) == 1, f"Alembic must have exactly one head, got: {heads}"
    return heads[0]


@pytest.fixture()
def restore_head(db):
    """Guarantee the DB is back at head no matter how the test exits."""
    yield
    _alembic_after_releasing(db, "upgrade", "head")
    assert _trigger_exists(*_IMMUTABLE_TRIGGER)


# ---------------------------------------------------------------------------
# Schema at head
# ---------------------------------------------------------------------------

def test_alembic_has_a_single_head():
    assert _REVISION in _single_head()


def test_schema_objects_exist_at_head():
    # Stock summary
    for col in ("on_hand_quantity", "reserved_quantity", "available_quantity"):
        assert _column_exists("ingredient_stock", col), f"missing {col}"
    assert not _column_exists("ingredient_stock", "stock_quantity"), (
        "the conflated stock_quantity column must be gone"
    )

    # available_quantity is GENERATED, not merely a column the app maintains.
    assert _scalar(
        "SELECT is_generated FROM information_schema.columns "
        "WHERE table_name = 'ingredient_stock' AND column_name = 'available_quantity'"
    ) == "ALWAYS"

    # Order inventory lines
    assert _table_exists("order_inventory_lines")

    # Movement ledger
    for col in ("quantity", "quantity_delta_on_hand", "quantity_delta_reserved",
                "order_id", "order_inventory_line_id", "actor_user_id",
                "idempotency_key_hash", "request_hash", "reason", "legacy_backfill"):
        assert _column_exists("ingredient_stock_movements", col), f"missing {col}"
    assert not _column_exists("ingredient_stock_movements", "quantity_delta"), (
        "the ambiguous signed quantity_delta column must be gone"
    )


def test_constraints_exist_at_head():
    for name in (
        # stock summary
        "ck_stock_on_hand_nonneg",
        "ck_stock_reserved_nonneg",
        "ck_stock_reserved_le_on_hand",
        # order inventory lines
        "ck_oil_settled_le_reserved",
        "ck_oil_reserved_nonneg",
        "ck_oil_consumed_nonneg",
        # movement ledger
        "ck_movement_quantity_positive",
        "ck_movement_type_domain",
        "ck_movement_actor_required",
        "ck_movement_reason_required",
        "ck_movement_delta_matches_type",
    ):
        assert _constraint_exists(name), f"missing constraint {name}"


def test_append_only_trigger_installed_at_head():
    assert _trigger_exists(*_IMMUTABLE_TRIGGER)


# ---------------------------------------------------------------------------
# Reversibility
# ---------------------------------------------------------------------------

def test_downgrade_preserves_orders_and_payments(restore_head, db, client, make_staff):
    """
    The one that really matters: rolling the inventory migration back must not
    take orders or money with it. A migration you cannot safely reverse is a
    migration you cannot safely deploy.
    """
    import uuid

    from tests.conftest import (
        cleanup_ingredient,
        make_authed_client,
        make_ingredient,
        order_payload,
        purge_payments_for_orders,
    )

    ing, _ = make_ingredient(
        db, on_hand=Decimal("100.000"), standard_quantity=Decimal("10.00")
    )
    # Running Alembic requires closing this session, which detaches every ORM
    # object bound to it — so keep the plain ids, not the instances.
    ing_id = ing.id
    cashier = make_authed_client(db, make_staff("CASHIER", store_id=1))
    oid = None
    try:
        payload, headers = order_payload(ing_id, idem_key=uuid.uuid4().hex)
        r = client.post("/public/orders/", json=payload, headers=headers)
        assert r.status_code == 200
        oid = r.json()["order_id"]
        order_total = Decimal(str(r.json()["total_amount"]))

        pay = cashier.post(
            f"/cashier/orders/{oid}/payments",
            json={"payment_method": "CASH"},
            headers={"Idempotency-Key": uuid.uuid4().hex},
        )
        assert pay.status_code == 200, pay.text
        settlement_id = pay.json()["settlement_id"]

        orders_before = _scalar("SELECT count(*) FROM orders")
        settlements_before = _scalar("SELECT count(*) FROM payment_settlements")

        # ── Downgrade ────────────────────────────────────────────────────────
        _alembic_after_releasing(db, "downgrade", "-1")

        assert _scalar("SELECT version_num FROM alembic_version") == _PREVIOUS
        # Inventory schema is gone...
        assert not _table_exists("order_inventory_lines")
        assert not _column_exists("ingredient_stock", "reserved_quantity")
        assert not _column_exists("ingredient_stock", "available_quantity")
        assert not _trigger_exists(*_IMMUTABLE_TRIGGER)
        # ...and the pre-lifecycle shape is back.
        assert _column_exists("ingredient_stock", "stock_quantity")
        assert _column_exists("ingredient_stock_movements", "quantity_delta")

        # ...but nothing financial or transactional was lost.
        assert _scalar("SELECT count(*) FROM orders") == orders_before
        assert _scalar("SELECT count(*) FROM payment_settlements") == settlements_before
        assert _scalar(
            "SELECT count(*) FROM orders WHERE id = :i", i=oid
        ) == 1, "the order survived the rollback"
        assert _scalar(
            "SELECT paid_amount FROM orders WHERE id = :i", i=oid
        ) == order_total, "the collected money survived the rollback"
        assert _scalar(
            "SELECT count(*) FROM payment_settlements WHERE id = :i", i=settlement_id
        ) == 1

        # Physical stock survives too (on_hand → stock_quantity).
        assert _scalar(
            "SELECT stock_quantity FROM ingredient_stock WHERE ingredient_id = :i",
            i=ing_id,
        ) == Decimal("100.000")

        # ── Re-upgrade ───────────────────────────────────────────────────────
        _alembic_after_releasing(db, "upgrade", "head")
        assert _table_exists("order_inventory_lines")
        assert _column_exists("ingredient_stock", "available_quantity")
        assert _trigger_exists(*_IMMUTABLE_TRIGGER), (
            "the append-only guard must be reinstalled on re-upgrade"
        )
        assert _constraint_exists("ck_movement_delta_matches_type")

        # Order and payment still intact after the round trip.
        assert _scalar("SELECT count(*) FROM orders WHERE id = :i", i=oid) == 1
        assert _scalar(
            "SELECT paid_amount FROM orders WHERE id = :i", i=oid
        ) == order_total
        assert _single_head().startswith(_REVISION)
    finally:
        # The re-upgrade rebuilt the schema, but the order's inventory lines were
        # dropped with the table, so clean up through the order graph.
        engine.dispose()
        db.rollback()
        if oid is not None:
            purge_payments_for_orders(db, [oid])
        cleanup_ingredient(db, ing_id)


def test_backfill_leaves_ledger_reconciled(restore_head, db):
    """
    After the migration, every ingredient's stored on-hand must equal the sum of
    its ledger deltas — otherwise reconciliation would report a false drift on
    every ingredient that existed before the lifecycle, on day one.
    """
    drifted = _scalar(
        """
        SELECT count(*) FROM ingredient_stock s
        WHERE s.on_hand_quantity <> COALESCE((
            SELECT SUM(m.quantity_delta_on_hand)
            FROM ingredient_stock_movements m
            WHERE m.ingredient_id = s.ingredient_id
        ), 0)
        """
    )
    assert drifted == 0, f"{drifted} ingredient(s) do not reconcile after backfill"
