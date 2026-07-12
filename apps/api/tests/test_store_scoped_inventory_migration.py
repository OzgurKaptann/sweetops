"""
Alembic migration integrity for store-scoped inventory (``e2c9a4b16d38``).

Two things are being proved here, and the second matters more than the first.

  1. The schema really is store-scoped: the columns, the (store_id,
     ingredient_id) grain, the composite foreign keys and the append-only guard
     all exist in PostgreSQL, and the migration round-trips.

  2. The migration REFUSES TO GUESS. Global stock names no store. If more than
     one store is being operated when that stock is found, there is no rule that
     can split "4 kg of pistachio" across branches — 4 and 0? 2 and 2? — and any
     answer the migration invented would not fail loudly. It would produce a
     database that looks completely fine and is quietly wrong about where the
     physical stock is, which is the worst outcome available. So it aborts, and
     `test_multi_store_global_stock_fails_closed` is the test that holds it to
     that.

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

_API_DIR = Path(__file__).resolve().parents[1]

_REVISION = "e2c9a4b16d38"          # store-scoped inventory (head)
_PREVIOUS = "c3b7e01f9a24"          # inventory lifecycle
_IMMUTABLE_TRIGGER = ("ingredient_stock_movements",
                      "trg_ingredient_stock_movements_immutable")

_SCOPED_TABLES = (
    "ingredient_stock",
    "ingredient_stock_movements",
    "order_inventory_lines",
)

# The cross-store integrity keys. Each one makes a class of corruption
# unrepresentable rather than merely unwritten by the current code.
_COMPOSITE_KEYS = (
    "uq_stock_store_ingredient",     # 1. no duplicate (store, ingredient)
    "fk_movement_stock_store",       # 2. movement store == stock store
    "fk_oil_order_store",            # 3. line store == order store
    "fk_oil_stock_store",            # 4. line store == stock store
    "fk_movement_order_store",       # 5. movement's order is in its store
    "fk_movement_line_store",        # 6. movement's line is in its store
    "fk_movement_actor_store",       # 7. manual actor belongs to the store
)


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
    """
    Fully release the test session's connection before running DDL.

    engine.dispose() only discards POOLED connections; the `db` fixture holds a
    checked-out one that is idle-in-transaction, and PostgreSQL makes the
    migration's ALTER TABLE wait on it forever. Without this the suite hangs
    instead of failing.
    """
    db.rollback()
    db.close()


def _scalar(sql: str, **params):
    with engine.connect() as conn:
        return conn.execute(text(sql), params).scalar()


def _exec(sql: str, **params) -> None:
    with engine.begin() as conn:
        conn.execute(text(sql), params)


def _exec_scalar(sql: str, **params):
    """INSERT ... RETURNING, COMMITTED. `_scalar` uses connect() and would roll back."""
    with engine.begin() as conn:
        return conn.execute(text(sql), params).scalar()


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


def _single_head() -> str:
    proc = _alembic_raw("heads")
    assert proc.returncode == 0, proc.stderr
    heads = [ln for ln in proc.stdout.splitlines() if ln.strip()]
    assert len(heads) == 1, f"Alembic must have exactly one head, got: {heads}"
    return heads[0]


@pytest.fixture()
def restore_head(db):
    """Put the database back at head however the test exits."""
    yield
    _release(db)
    _alembic("upgrade", "head")
    assert _trigger_exists(*_IMMUTABLE_TRIGGER)


# ═══════════════════════════════════════════════════════════════════════════
# 1. Schema at head
# ═══════════════════════════════════════════════════════════════════════════

def test_alembic_has_a_single_head():
    """
    Exactly one head — two would make `alembic upgrade head` ambiguous and turn
    deployment into a coin toss.

    This asserts that THIS revision is in the applied history, not that it IS the
    head: later branches stack on top of it, and a test that pins it to head would
    fail every time the schema legitimately moves forward. The single-head
    invariant is the thing worth guarding; being the newest is not.
    """
    _single_head()   # asserts there is exactly one

    proc = _alembic_raw("history")
    assert proc.returncode == 0, proc.stderr
    assert _REVISION in proc.stdout, "the store-scoped inventory revision is not in history"

    current = _scalar("SELECT version_num FROM alembic_version")
    assert current is not None


def test_store_id_columns_exist():
    for table in _SCOPED_TABLES:
        assert _column_exists(table, "store_id"), f"{table}.store_id is missing"


def test_store_foreign_keys_exist():
    for fk in ("fk_stock_store", "fk_movement_store", "fk_oil_store"):
        assert _constraint_exists(fk), f"{fk} is missing"


def test_stock_grain_is_store_and_ingredient():
    """
    The grain change IS the feature: one row per ingredient meant one jar of
    Nutella for the whole chain.
    """
    assert _constraint_exists("uq_stock_store_ingredient")

    # ...and the old chain-wide UNIQUE(ingredient_id) must be gone, or a second
    # store could never stock the same ingredient at all.
    single_col_unique = _scalar(
        """
        SELECT count(*)
        FROM pg_constraint con
        JOIN pg_class rel ON rel.oid = con.conrelid
        JOIN pg_attribute att
          ON att.attrelid = rel.oid AND att.attnum = con.conkey[1]
        WHERE rel.relname = 'ingredient_stock'
          AND con.contype = 'u'
          AND array_length(con.conkey, 1) = 1
          AND att.attname = 'ingredient_id'
        """
    )
    assert single_col_unique == 0, (
        "the global UNIQUE(ingredient_id) must be gone — with it, a second store "
        "could not hold the same ingredient"
    )


def test_all_cross_store_integrity_keys_exist():
    for name in _COMPOSITE_KEYS:
        assert _constraint_exists(name), f"cross-store guard {name} is missing"


def test_idempotency_uniqueness_is_store_scoped():
    """
    Two branch managers sending the same Idempotency-Key are two commands, not a
    replay. The unique index must therefore include the store.
    """
    assert _index_exists("uq_movement_store_idem")
    assert not _index_exists("uq_movement_idem"), (
        "the global idempotency index must be gone — it would make Beşiktaş's "
        "receipt replay Kadıköy's result"
    )


def test_append_only_guard_survives_the_migration():
    assert _trigger_exists(*_IMMUTABLE_TRIGGER)


# ═══════════════════════════════════════════════════════════════════════════
# 2. Round trip: downgrade removes only this branch, re-upgrade restores it
# ═══════════════════════════════════════════════════════════════════════════

def test_downgrade_and_reupgrade_preserve_orders_and_payments(
    restore_head, db, client, make_staff
):
    """
    A migration you cannot reverse is a migration you cannot deploy. Rolling
    store scoping back must remove store scoping and NOTHING else — every order
    and every lira must survive the round trip.
    """
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
        assert r.status_code == 200, r.text
        oid = r.json()["order_id"]
        order_total = Decimal(str(r.json()["total_amount"]))

        pay = cashier.post(
            f"/cashier/orders/{oid}/payments",
            json={"payment_method": "CASH"},
            headers={"Idempotency-Key": uuid.uuid4().hex},
        )
        assert pay.status_code == 200, pay.text

        orders_before = _scalar("SELECT count(*) FROM orders")
        settlements_before = _scalar("SELECT count(*) FROM payment_settlements")
        movements_before = _scalar("SELECT count(*) FROM ingredient_stock_movements")

        # ── Downgrade ──────────────────────────────────────────────────────
        # By REVISION, not by "-1". This file is about the store-scoped migration,
        # and later branches now sit on top of it, so a relative step would undo
        # whatever happens to be head instead of the thing under test.
        _release(db)
        _alembic("downgrade", _PREVIOUS)

        assert _scalar("SELECT version_num FROM alembic_version") == _PREVIOUS
        # This branch's schema is gone...
        for table in _SCOPED_TABLES:
            assert not _column_exists(table, "store_id"), f"{table}.store_id survived"
        for name in _COMPOSITE_KEYS:
            assert not _constraint_exists(name), f"{name} survived"
        # ...and the previous branch's schema is untouched: the inventory
        # LIFECYCLE (reservations, lines, the ledger) is not ours to remove.
        assert _column_exists("ingredient_stock", "reserved_quantity")
        assert _column_exists("ingredient_stock", "available_quantity")
        assert _scalar("SELECT to_regclass('public.order_inventory_lines')") is not None
        assert _trigger_exists(*_IMMUTABLE_TRIGGER)
        assert _constraint_exists("ck_movement_delta_matches_type")

        # Orders, payments and the whole ledger came through intact.
        assert _scalar("SELECT count(*) FROM orders") == orders_before
        assert _scalar("SELECT count(*) FROM payment_settlements") == settlements_before
        assert _scalar("SELECT count(*) FROM ingredient_stock_movements") == movements_before
        assert _scalar("SELECT paid_amount FROM orders WHERE id = :i", i=oid) == order_total

        # ── Re-upgrade ─────────────────────────────────────────────────────
        _alembic("upgrade", "head")

        for table in _SCOPED_TABLES:
            assert _column_exists(table, "store_id")
        for name in _COMPOSITE_KEYS:
            assert _constraint_exists(name)
        assert _trigger_exists(*_IMMUTABLE_TRIGGER)
        assert _index_exists("uq_movement_store_idem")

        # Still intact after the full cycle.
        assert _scalar("SELECT count(*) FROM orders") == orders_before
        assert _scalar("SELECT count(*) FROM payment_settlements") == settlements_before
        assert _scalar("SELECT count(*) FROM ingredient_stock_movements") == movements_before
        assert _scalar("SELECT paid_amount FROM orders WHERE id = :i", i=oid) == order_total
        _single_head()
    finally:
        engine.dispose()
        db.rollback()
        if oid is not None:
            purge_payments_for_orders(db, [oid])
        cleanup_ingredient(db, ing_id)


def test_backfill_assigns_existing_stock_to_the_only_operational_store(
    restore_head, db
):
    """
    The one-store backfill. After the round trip, every stock row, movement and
    inventory line carries a store — and the quantities are exactly what they
    were, because a store label was added, not stock invented.
    """
    from tests.conftest import cleanup_ingredient, make_ingredient

    ing, stock = make_ingredient(db, on_hand=Decimal("42.000"))
    # Plain ints, captured NOW: _release() closes the session, and touching an
    # ORM attribute afterwards would try to lazy-load from a dead connection.
    ing_id, stock_id = ing.id, stock.id
    try:
        before = _scalar(
            "SELECT on_hand_quantity FROM ingredient_stock WHERE id = :i", i=stock_id
        )

        _release(db)
        _alembic("downgrade", _PREVIOUS)  # back to global stock
        _alembic("upgrade", "head")       # ...and forward again: this is the backfill

        # Nothing anywhere is store-less.
        for table in _SCOPED_TABLES:
            orphans = _scalar(f"SELECT count(*) FROM {table} WHERE store_id IS NULL")
            assert orphans == 0, f"{orphans} row(s) in {table} have no store"

        # The quantity is byte-for-byte what it was.
        assert _scalar(
            "SELECT on_hand_quantity FROM ingredient_stock WHERE id = :i", i=stock_id
        ) == before

        # Every inventory line agrees with its order's store — the derivable
        # half of the backfill, which needs no assumption at all.
        mismatched = _scalar(
            """
            SELECT count(*) FROM order_inventory_lines l
            JOIN orders o ON o.id = l.order_id
            WHERE l.store_id <> o.store_id
            """
        )
        assert mismatched == 0
    finally:
        engine.dispose()
        db.rollback()
        cleanup_ingredient(db, ing_id)


# ═══════════════════════════════════════════════════════════════════════════
# 3. Fail closed — the test this migration exists to pass
# ═══════════════════════════════════════════════════════════════════════════

def test_multi_store_global_stock_fails_closed(restore_head, db):
    """
    Global stock + two operational stores ⇒ ABORT.

    This is the whole safety argument. A global `ingredient_stock` row records a
    quantity and no store. With two branches running, assigning it is a guess,
    duplicating it into both fabricates stock that exists on no shelf, and
    dumping it all on "store 1" is a coin flip dressed up as a default. None of
    those would raise an error — they would each produce a plausible, wrong
    database. The only honest move is to stop and make a human do the physical
    count, so that is what the migration must do.

    Reproduced end-to-end: downgrade to the global schema, stand up a genuine
    second operational store, then try to migrate forward.
    """
    from tests.conftest import DEFAULT_STORE_ID, cleanup_ingredient, make_ingredient

    ing, _ = make_ingredient(db, on_hand=Decimal("50.000"))
    ing_id = ing.id          # plain int: _release() closes the session below
    created_users: list[int] = []
    store_b_id = None
    try:
        _release(db)
        _alembic("downgrade", _PREVIOUS)  # inventory is global again

        # Global stock exists (the ingredient above), and no row of it names a
        # store — that is exactly the ambiguity being reproduced.
        assert _scalar("SELECT count(*) FROM ingredient_stock") > 0
        assert not _column_exists("ingredient_stock", "store_id")

        role_id = _scalar("SELECT id FROM roles ORDER BY id LIMIT 1")
        store_b_id = _exec_scalar(
            "INSERT INTO stores (name, location) VALUES (:n, 'Test') RETURNING id",
            n=f"AmbiguousStore_{uuid.uuid4().hex[:8]}",
        )
        # TWO genuinely operational stores: staff working in each. Both are
        # created explicitly rather than relying on whatever ambient rows the
        # suite happens to have left, so the ambiguity is real and deterministic.
        for sid in (DEFAULT_STORE_ID, store_b_id):
            created_users.append(_exec_scalar(
                "INSERT INTO users (username, password_hash, role_id, store_id, is_active) "
                "VALUES (:u, 'x', :r, :s, true) RETURNING id",
                u=f"ambiguous_{uuid.uuid4().hex[:8]}", r=role_id, s=sid,
            ))

        # ── The migration must refuse ───────────────────────────────────────
        proc = _alembic_raw("upgrade", "head")
        assert proc.returncode != 0, (
            "the migration MUST fail closed with ambiguous multi-store global "
            "stock — it silently succeeded, which means it guessed"
        )
        combined = proc.stdout + proc.stderr
        assert "AmbiguousInventoryStore" in combined or "operational stores" in combined, (
            f"expected a clear ambiguity error, got:\n{combined}"
        )

        # It aborted rather than half-applying: the schema is still global, and
        # not one row was invented.
        assert not _column_exists("ingredient_stock", "store_id")
        assert _scalar("SELECT version_num FROM alembic_version") == _PREVIOUS
    finally:
        # Resolve the ambiguity the way a real operator would — by removing the
        # second store — and PROVE it is gone before migrating forward, so a
        # botched cleanup can never leave the backfill pointing at a test store.
        engine.dispose()
        for uid in created_users:
            _exec("DELETE FROM users WHERE id = :i", i=uid)
        if store_b_id is not None:
            _exec("DELETE FROM stores WHERE id = :s", s=store_b_id)
            assert _scalar(
                "SELECT count(*) FROM stores WHERE id = :s", s=store_b_id
            ) == 0, "cleanup failed: the ambiguous store is still present"

        db.rollback()
        _release(db)
        _alembic("upgrade", "head")

        # With one store left, the backfill is unambiguous — and it must have
        # gone to the REAL store, not to a leftover fixture store.
        stores_with_stock = _scalar("SELECT count(DISTINCT store_id) FROM ingredient_stock")
        assert stores_with_stock == 1
        assert _scalar("SELECT DISTINCT store_id FROM ingredient_stock") == DEFAULT_STORE_ID

        cleanup_ingredient(db, ing_id)
