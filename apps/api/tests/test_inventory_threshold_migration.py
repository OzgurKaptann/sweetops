"""
Alembic migration + database integrity for inventory threshold alerts
(``c8a4e7b13f92``).

Two things are proved here, and the second matters more than the first.

  1. The schema really exists in PostgreSQL: the five threshold columns, the change-log
     table, the pairwise ordering constraints, the composite actor key, the store-scoped
     idempotency uniqueness and the immutability trigger. And it round-trips: downgrade
     removes exactly this branch and nothing else, re-upgrade restores it, and Alembic
     keeps a single head.

  2. The DATABASE — not the service — is what refuses an incoherent threshold. Every
     test below goes around app/services/inventory_service.py entirely and writes raw
     SQL, because a check that lives only in a service is a check the next refactor can
     delete. The specific corruptions being made unrepresentable:

       * a negative threshold — an alert that can never fire, on a shelf that can never
         hold less than nothing, so the manager is protected by a control that silently
         does nothing
       * an inverted ladder (critical above minimum) — the ingredient reaches CRITICAL
         before it ever reaches LOW, so the early warning the manager configured never
         appears at all
       * a target below the level it is supposed to restore (minimum or critical above
         target) — a replenishment that is a warning the moment it lands
       * a threshold stamped by somebody who does not work at that branch
       * a threshold configured for a shelf the branch does not have
       * an edit to a threshold decision after the fact

  ...and one thing that is proved by ABSENCE, and is the point of the whole feature:
  the migration does not add a movement type, does not touch a stock quantity, and does
  not backfill anything. A threshold cannot move stock because there is nothing in the
  schema for it to move stock WITH.

The engine's pooled connections are disposed around every Alembic run, and the database
is always restored to head on the way out.
"""
import subprocess
import uuid
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.core.db import engine
from tests.conftest import (
    DEFAULT_STORE_ID,
    cleanup_ingredient,
    make_ingredient,
)

_API_DIR = Path(__file__).resolve().parents[1]

_REVISION = "c8a4e7b13f92"          # inventory threshold alerts (head)
_PREVIOUS = "a3d7e9c14b62"          # physical stock count workflow

_DB_REJECTS = (IntegrityError, DBAPIError)

_THRESHOLD_COLUMNS = (
    "critical_quantity",
    "minimum_quantity",
    "target_quantity",
    "threshold_updated_at",
    "threshold_updated_by_user_id",
)

# Each of these makes a class of nonsense unrepresentable rather than merely unwritten
# by the current code.
_THRESHOLD_CONSTRAINTS = (
    "ck_stock_threshold_critical_nonneg",        # 1. an alert that can never fire
    "ck_stock_threshold_minimum_nonneg",
    "ck_stock_threshold_target_nonneg",
    "ck_stock_threshold_critical_le_minimum",    # 2. the ladder, pairwise…
    "ck_stock_threshold_minimum_le_target",
    "ck_stock_threshold_critical_le_target",     # 3. …and this one holds it alone
    "fk_stock_threshold_actor_store",            # 4. the setter works at that branch
    "fk_threshold_update_actor_store",
    "fk_threshold_update_stock_store",           # 5. the branch has that shelf
    "uq_threshold_update_store_idem",            # 6. idempotency scoped by store
    "ck_threshold_update_reason_present",        # 7. an unexplained change is a red flag
)


# ---------------------------------------------------------------------------
# Alembic / introspection helpers
# ---------------------------------------------------------------------------

def _alembic_raw(*args: str) -> subprocess.CompletedProcess:
    # Dispose pooled connections: an idle-in-transaction connection would block the
    # ACCESS EXCLUSIVE locks DDL needs, and the migration would hang.
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


def _column_nullable(table: str, column: str) -> bool:
    return _scalar(
        "SELECT is_nullable FROM information_schema.columns "
        "WHERE table_name = :t AND column_name = :c",
        t=table, c=column,
    ) == "YES"


def _constraint_exists(name: str) -> bool:
    return _scalar("SELECT count(*) FROM pg_constraint WHERE conname = :n", n=name) > 0


def _trigger_exists(table: str, trigger: str) -> bool:
    return _scalar(
        "SELECT count(*) FROM pg_trigger t JOIN pg_class c ON t.tgrelid = c.oid "
        "WHERE t.tgname = :trig AND c.relname = :tbl AND NOT t.tgisinternal",
        trig=trigger, tbl=table,
    ) > 0


@pytest.fixture(autouse=True)
def _restore_head():
    """Whatever a test does to the schema, put the database back at head."""
    yield
    _alembic("upgrade", "head")


@pytest.fixture()
def env(db, make_staff):
    """
    A manager, an ingredient and a 10 kg shelf, for direct-SQL corruption tests.

    The ids are held as PLAIN INTS as well as ORM objects: the migration tests close the
    session before running Alembic, which detaches every ORM instance, so reading
    ``ingredient.id`` in teardown would raise DetachedInstanceError and leave rows
    behind.
    """
    class Env:
        pass

    e = Env()
    e.manager = make_staff("MANAGER", store_id=DEFAULT_STORE_ID)
    e.manager_id = e.manager.id
    e.ingredient, e.stock = make_ingredient(
        db, on_hand=Decimal("10.000"), unit="kg", store_id=DEFAULT_STORE_ID
    )
    e.ingredient_id = e.ingredient.id
    yield e
    cleanup_ingredient(db, e.ingredient_id)


def _set_thresholds(critical=None, minimum=None, target=None, *, store_id, ingredient_id):
    """Write thresholds with RAW SQL, entirely around the service."""
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                UPDATE ingredient_stock
                SET critical_quantity = :c,
                    minimum_quantity  = :m,
                    target_quantity   = :t
                WHERE store_id = :s AND ingredient_id = :i
                """
            ),
            {"c": critical, "m": minimum, "t": target, "s": store_id, "i": ingredient_id},
        )


def _insert_update(**kw):
    """Insert a threshold-log row with RAW SQL."""
    params = {
        "store_id": DEFAULT_STORE_ID,
        "old_critical": None, "old_minimum": None, "old_target": None,
        "new_critical": None, "new_minimum": None, "new_target": None,
        "reason": "Kis sezonu",
        "key_hash": uuid.uuid4().hex * 2,
        "request_hash": uuid.uuid4().hex * 2,
        **kw,
    }
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO inventory_threshold_updates (
                    store_id, ingredient_id,
                    old_critical_quantity, old_minimum_quantity, old_target_quantity,
                    new_critical_quantity, new_minimum_quantity, new_target_quantity,
                    reason, updated_by_user_id, idempotency_key_hash, request_hash
                ) VALUES (
                    :store_id, :ingredient_id,
                    :old_critical, :old_minimum, :old_target,
                    :new_critical, :new_minimum, :new_target,
                    :reason, :actor, :key_hash, :request_hash
                )
                """
            ),
            params,
        )


# ═══════════════════════════════════════════════════════════════════════════
# The schema exists
# ═══════════════════════════════════════════════════════════════════════════

class TestSchema:
    def test_threshold_columns_exist_and_are_nullable(self):
        """
        NULL means NOT CONFIGURED — a real, distinct state that every branch starts in.
        A NOT NULL column with a default of 0 would silently tell every branch that
        every ingredient is critical the moment it empties: an opinion nobody expressed.
        """
        for column in _THRESHOLD_COLUMNS:
            assert _column_exists("ingredient_stock", column), column
            assert _column_nullable("ingredient_stock", column), column

    def test_the_change_log_table_exists(self):
        assert _table_exists("inventory_threshold_updates")

    def test_every_threshold_constraint_exists(self):
        for name in _THRESHOLD_CONSTRAINTS:
            assert _constraint_exists(name), name

    def test_the_change_log_is_append_only(self):
        assert _trigger_exists(
            "inventory_threshold_updates", "trg_inventory_threshold_updates_immutable"
        )

    def test_the_migration_adds_no_movement_type(self):
        """
        The load-bearing ABSENCE. There is no threshold movement type, so a threshold
        change is not a movement this schema can express — the ledger, and therefore
        every analytics report built on it, is protected by the type domain itself and
        not by anybody remembering not to write one.
        """
        domain = _scalar(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conname = 'ck_movement_type_domain'"
        )
        assert "THRESHOLD" not in domain.upper()
        # ...and the ten types that were there before are all still there.
        for movement_type in (
            "RESERVATION_CREATED", "RESERVATION_RELEASED", "CONSUMPTION", "WASTE",
            "RETURNED", "MANUAL_ADJUSTMENT", "PURCHASE_RECEIPT", "TRANSFER_OUT",
            "TRANSFER_IN", "STOCK_COUNT_ADJUSTMENT",
        ):
            assert movement_type in domain

    def test_existing_stock_rows_were_not_backfilled(self, db, env):
        """
        Every existing row becomes NOT CONFIGURED, which is the truthful answer: no
        branch had configured a threshold, because until now it could not.

        In particular the legacy ``reorder_level`` is NOT promoted into
        ``minimum_quantity``. seed.py sets it to a flat 15% of opening stock for every
        ingredient, and turning a seeded guess into an operational alert level would
        fill the new screen with warnings nobody chose — and the first thing anybody
        does with an alert screen they do not believe is stop reading it.
        """
        row = _scalar(
            "SELECT count(*) FROM ingredient_stock "
            "WHERE critical_quantity IS NOT NULL OR minimum_quantity IS NOT NULL "
            "   OR target_quantity IS NOT NULL"
        )
        assert row == 0, "the migration must not invent a threshold for anybody"
        # reorder_level is untouched and still doing its old job.
        assert _column_exists("ingredient_stock", "reorder_level")


# ═══════════════════════════════════════════════════════════════════════════
# The database refuses nonsense
# ═══════════════════════════════════════════════════════════════════════════

class TestDatabaseRefuses:
    def test_a_negative_threshold_is_refused(self, env):
        for field in ("critical", "minimum", "target"):
            with pytest.raises(_DB_REJECTS):
                _set_thresholds(
                    **{field: Decimal("-1")},
                    store_id=DEFAULT_STORE_ID,
                    ingredient_id=env.ingredient_id,
                )

    def test_critical_above_minimum_is_refused(self, env):
        with pytest.raises(_DB_REJECTS):
            _set_thresholds(
                critical=Decimal("9"), minimum=Decimal("5"),
                store_id=DEFAULT_STORE_ID, ingredient_id=env.ingredient_id,
            )

    def test_minimum_above_target_is_refused(self, env):
        with pytest.raises(_DB_REJECTS):
            _set_thresholds(
                minimum=Decimal("30"), target=Decimal("20"),
                store_id=DEFAULT_STORE_ID, ingredient_id=env.ingredient_id,
            )

    def test_critical_above_target_is_refused_without_a_minimum(self, env):
        """
        ck_stock_threshold_critical_le_target is NOT redundant: with minimum NULL, it is
        the only constraint relating critical to target at all.
        """
        with pytest.raises(_DB_REJECTS):
            _set_thresholds(
                critical=Decimal("30"), target=Decimal("20"),
                store_id=DEFAULT_STORE_ID, ingredient_id=env.ingredient_id,
            )

    def test_a_coherent_full_ladder_is_accepted(self, env):
        _set_thresholds(
            critical=Decimal("2"), minimum=Decimal("5"), target=Decimal("20"),
            store_id=DEFAULT_STORE_ID, ingredient_id=env.ingredient_id,
        )

    def test_every_partial_combination_is_accepted(self, env):
        """The documented policy: configure one, two or three, and whichever ones you DID
        configure must make sense together."""
        combos = (
            {"critical": Decimal("2")},
            {"minimum": Decimal("5")},
            {"target": Decimal("20")},
            {"critical": Decimal("2"), "minimum": Decimal("5")},
            {"minimum": Decimal("5"), "target": Decimal("20")},
            {"critical": Decimal("2"), "target": Decimal("20")},
            {"critical": Decimal("2"), "minimum": Decimal("2")},   # equal is allowed
        )
        for combo in combos:
            _set_thresholds(
                **combo, store_id=DEFAULT_STORE_ID, ingredient_id=env.ingredient_id
            )

    def test_zero_is_a_valid_threshold(self, env):
        """"Warn me only when it is actually gone" is a legitimate decision."""
        _set_thresholds(
            critical=Decimal("0"), minimum=Decimal("0"), target=Decimal("0"),
            store_id=DEFAULT_STORE_ID, ingredient_id=env.ingredient_id,
        )

    def test_a_log_row_with_an_empty_reason_is_refused(self, env):
        with pytest.raises(_DB_REJECTS):
            _insert_update(
                ingredient_id=env.ingredient_id, actor=env.manager_id, reason="   "
            )

    def test_a_log_row_with_an_inverted_ladder_is_refused(self, env):
        """The LOG is held to the same rules as the live row. An audit trail that can
        record a decision the system would refuse is not an audit trail."""
        with pytest.raises(_DB_REJECTS):
            _insert_update(
                ingredient_id=env.ingredient_id, actor=env.manager_id,
                new_critical=Decimal("9"), new_minimum=Decimal("5"),
            )

    def test_the_same_key_cannot_be_reused_within_one_store(self, env):
        key = uuid.uuid4().hex * 2
        _insert_update(
            ingredient_id=env.ingredient_id, actor=env.manager_id, key_hash=key
        )
        with pytest.raises(_DB_REJECTS):
            _insert_update(
                ingredient_id=env.ingredient_id, actor=env.manager_id, key_hash=key
            )

    def test_an_outsider_cannot_be_stamped_on_a_threshold(self, db, env, make_store, make_staff):
        """fk_stock_threshold_actor_store. A Kadıköy manager on Beşiktaş's threshold row
        is unrepresentable, not merely forbidden."""
        other = make_store()
        outsider_id = make_staff("MANAGER", store_id=other.id).id

        with pytest.raises(_DB_REJECTS):
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "UPDATE ingredient_stock SET threshold_updated_by_user_id = :u "
                        "WHERE store_id = :s AND ingredient_id = :i"
                    ),
                    {"u": outsider_id, "s": DEFAULT_STORE_ID, "i": env.ingredient_id},
                )

    def test_a_log_row_for_a_shelf_this_branch_does_not_have_is_refused(
        self, db, env, make_store, make_staff
    ):
        """Configuring a threshold does not CREATE stock."""
        other = make_store()
        other_manager_id = make_staff("MANAGER", store_id=other.id).id

        with pytest.raises(_DB_REJECTS):
            _insert_update(
                store_id=other.id,
                ingredient_id=env.ingredient_id,   # other store has no stock row for it
                actor=other_manager_id,
            )

    def test_a_threshold_decision_cannot_be_edited(self, env):
        _insert_update(ingredient_id=env.ingredient_id, actor=env.manager_id)
        with pytest.raises(_DB_REJECTS):
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "UPDATE inventory_threshold_updates SET reason = 'rewritten' "
                        "WHERE ingredient_id = :i"
                    ),
                    {"i": env.ingredient_id},
                )

    def test_a_threshold_decision_cannot_be_deleted(self, env):
        _insert_update(ingredient_id=env.ingredient_id, actor=env.manager_id)
        with pytest.raises(_DB_REJECTS):
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "DELETE FROM inventory_threshold_updates WHERE ingredient_id = :i"
                    ),
                    {"i": env.ingredient_id},
                )


# ═══════════════════════════════════════════════════════════════════════════
# The migration round-trips
# ═══════════════════════════════════════════════════════════════════════════

class TestMigrationRoundTrip:
    def test_alembic_has_a_single_head(self):
        proc = _alembic_raw("heads")
        assert proc.returncode == 0, proc.stderr
        heads = [ln for ln in proc.stdout.splitlines() if ln.strip()]
        assert len(heads) == 1, f"expected one head, got: {heads}"
        assert _REVISION in heads[0]

    def test_downgrade_removes_only_this_branch(self, db):
        """
        Down to the previous revision, and no further: the stock counts, the transfers
        and the ledger must all still be there afterwards. A migration that takes its
        neighbours with it is worse than one that cannot be reversed at all.
        """
        _release(db)
        _alembic("downgrade", _PREVIOUS)
        try:
            # This branch is gone…
            assert not _table_exists("inventory_threshold_updates")
            for column in _THRESHOLD_COLUMNS:
                assert not _column_exists("ingredient_stock", column), column
            for name in _THRESHOLD_CONSTRAINTS:
                assert not _constraint_exists(name), name

            # …and nothing else went with it.
            assert _table_exists("inventory_stock_counts")
            assert _table_exists("inventory_transfers")
            assert _table_exists("ingredient_stock_movements")
            assert _column_exists("ingredient_stock", "on_hand_quantity")
            assert _column_exists("ingredient_stock", "reserved_quantity")
            # The legacy column this feature deliberately did NOT touch.
            assert _column_exists("ingredient_stock", "reorder_level")
            # The stock-count trigger from the previous revision still stands.
            assert _trigger_exists(
                "inventory_stock_counts", "trg_inventory_stock_counts_immutable"
            )
        finally:
            _alembic("upgrade", "head")

    def test_re_upgrade_restores_the_schema(self, db):
        _release(db)
        _alembic("downgrade", _PREVIOUS)
        _alembic("upgrade", "head")

        assert _table_exists("inventory_threshold_updates")
        for column in _THRESHOLD_COLUMNS:
            assert _column_exists("ingredient_stock", column), column
        for name in _THRESHOLD_CONSTRAINTS:
            assert _constraint_exists(name), name
        assert _trigger_exists(
            "inventory_threshold_updates", "trg_inventory_threshold_updates_immutable"
        )

    def test_downgrade_refuses_while_a_real_decision_exists(self, db, env):
        """
        Dropping the log would destroy the record of who decided what "low" means in each
        branch and why — and dropping the columns would silently DISARM every alert those
        decisions configured. A branch would go from "warn me at 3 kg" to no warning at
        all, with nothing in the database to say it ever had one. A lossy downgrade is
        worse than no downgrade, so it refuses and says why.
        """
        _insert_update(ingredient_id=env.ingredient_id, actor=env.manager_id)
        _release(db)

        proc = _alembic_raw("downgrade", _PREVIOUS)
        assert proc.returncode != 0, "a lossy downgrade must not succeed"
        assert "ThresholdUpdatesExist" in proc.stderr or "threshold update" in proc.stderr

        # ...and it really did not run: the schema is intact.
        assert _table_exists("inventory_threshold_updates")
        assert _column_exists("ingredient_stock", "critical_quantity")
