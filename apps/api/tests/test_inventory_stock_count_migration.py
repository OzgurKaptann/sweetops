"""
Alembic migration + database integrity for the physical stock count workflow
(``a3d7e9c14b62``).

Two things are proved here, and the second matters more than the first.

  1. The schema really exists in PostgreSQL: the count table, its generated delta,
     the movement column, the composite leg key, the widened movement-type domain,
     the store-scoped idempotency uniqueness, the immutability trigger and the
     deferred count/movement trigger. And it round-trips: downgrade removes exactly
     this branch and nothing else, re-upgrade restores it, and Alembic keeps a
     single head.

  2. The DATABASE — not the service — is what refuses a corrupt count. Every test
     below goes around app/services/inventory_service.py entirely and writes raw
     SQL, because a check that lives only in a service is a check that the next
     refactor can delete. The specific corruptions being made unrepresentable:

       * a count that claims a delta its own two numbers do not support
       * a count whose movement moved a different amount, in the wrong direction,
         in the wrong store, for the wrong ingredient, or with the wrong type
       * a non-zero count with NO movement — a shelf "corrected" on paper only
       * a zero-delta count WITH a movement — stock moved by a count that found
         nothing wrong
       * a count that touches reserved stock
       * a count below reserved
       * a count taken by someone who does not work at that branch
       * a count of a shelf the branch does not have
       * an edit to a count after the fact

The engine's pooled connections are disposed around every Alembic run, and the
database is always restored to head on the way out.
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
    make_authed_client,
    make_ingredient,
)

_API_DIR = Path(__file__).resolve().parents[1]

_REVISION = "a3d7e9c14b62"          # physical stock count workflow (head)
_PREVIOUS = "f4b8c1d90e26"          # inventory transfer workflow

_DB_REJECTS = (IntegrityError, DBAPIError)

_COUNT_TRIGGERS = (
    ("inventory_stock_counts", "trg_inventory_stock_counts_movement"),
    ("ingredient_stock_movements", "trg_stock_count_movement_matches"),
    ("inventory_stock_counts", "trg_inventory_stock_counts_immutable"),
)

# Each of these makes a class of corruption unrepresentable rather than merely
# unwritten by the current code.
_COUNT_CONSTRAINTS = (
    "ck_stock_count_counted_nonneg",           # 1. a shelf cannot hold a negative
    "ck_stock_count_counted_ge_reserved",      # 2. THE safety rule
    "ck_stock_count_status_domain",            # 3. APPLIED only — no fake workflow
    "ck_stock_count_reason_present",           # 4. an unexplained correction is theft
    "fk_stock_count_actor_store",              # 5. the counter works at that branch
    "fk_stock_count_stock_store",              # 6. the branch has that shelf
    "uq_stock_count_store_idem",               # 7. idempotency scoped by store
    "fk_movement_stock_count_leg",             # 8. movement ↔ count's store+ingredient
    "ck_movement_stock_count_link",            # 9. STOCK_COUNT_ADJUSTMENT ⟺ count
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


# ---------------------------------------------------------------------------
# Raw-SQL fixtures — everything below writes the tables DIRECTLY
# ---------------------------------------------------------------------------

@pytest.fixture()
def env(db, make_staff):
    """
    A manager, an ingredient and a 10 kg shelf, for direct-SQL corruption tests.

    The ids are held as PLAIN INTS as well as ORM objects. The migration tests close
    the session (``_release``) before running Alembic — DDL waits forever behind an
    idle-in-transaction connection — which detaches every ORM instance, so reading
    ``ingredient.id`` in teardown would raise DetachedInstanceError and leave the
    rows behind.
    """
    class Env:
        pass

    e = Env()
    e.manager = make_staff("MANAGER", store_id=DEFAULT_STORE_ID)
    e.manager_id = e.manager.id
    e.client = make_authed_client(db, e.manager)
    e.ingredient, e.stock = make_ingredient(
        db,
        on_hand=Decimal("10.000"),
        standard_quantity=Decimal("2.000"),
        unit="kg",
        store_id=DEFAULT_STORE_ID,
    )
    e.ingredient_id = e.ingredient.id
    yield e
    cleanup_ingredient(db, e.ingredient_id)


def _insert_count(
    db,
    env,
    *,
    counted="9.250",
    system_on_hand="10.000",
    system_reserved="0",
    store_id=None,
    ingredient_id=None,
    actor_id=None,
    status="APPLIED",
    reason="forced",
) -> int:
    """Insert a count row directly and return its id. Deliberately bypasses the
    service — these tests are about what the DATABASE will accept."""
    return db.execute(
        text(
            """
            INSERT INTO inventory_stock_counts (
                store_id, ingredient_id, counted_quantity,
                system_on_hand_quantity, system_reserved_quantity, unit,
                reason, status, counted_by_user_id,
                idempotency_key_hash, request_hash
            ) VALUES (
                :store, :ing, :counted, :soh, :sres, 'kg',
                :reason, :status, :actor, :k, :r
            ) RETURNING id
            """
        ),
        {
            "store": store_id if store_id is not None else DEFAULT_STORE_ID,
            "ing": ingredient_id if ingredient_id is not None else env.ingredient_id,
            "counted": counted,
            "soh": system_on_hand,
            "sres": system_reserved,
            "reason": reason,
            "status": status,
            "actor": actor_id if actor_id is not None else env.manager_id,
            "k": uuid.uuid4().hex,
            "r": uuid.uuid4().hex,
        },
    ).scalar()


def _insert_movement(
    db,
    env,
    *,
    count_id,
    movement_type="STOCK_COUNT_ADJUSTMENT",
    quantity="0.750",
    delta_on_hand="-0.750",
    delta_reserved="0",
    store_id=None,
    ingredient_id=None,
    actor_id=None,
) -> None:
    db.execute(
        text(
            """
            INSERT INTO ingredient_stock_movements (
                store_id, ingredient_id, movement_type, quantity,
                quantity_delta_on_hand, quantity_delta_reserved, unit,
                reason, actor_user_id, stock_count_id
            ) VALUES (
                :store, :ing, :mtype, :qty, :doh, :dres, 'kg',
                'forced', :actor, :count
            )
            """
        ),
        {
            "store": store_id if store_id is not None else DEFAULT_STORE_ID,
            "ing": ingredient_id if ingredient_id is not None else env.ingredient_id,
            "mtype": movement_type,
            "qty": quantity,
            "doh": delta_on_hand,
            "dres": delta_reserved,
            "actor": actor_id if actor_id is not None else env.manager_id,
            "count": count_id,
        },
    )


# ═══════════════════════════════════════════════════════════════════════════
# 1. The schema exists
# ═══════════════════════════════════════════════════════════════════════════

def test_alembic_has_a_single_head():
    """Two heads mean `alembic upgrade head` is ambiguous and deployment is a coin
    toss."""
    proc = _alembic_raw("heads")
    assert proc.returncode == 0, proc.stderr
    heads = [ln for ln in proc.stdout.splitlines() if ln.strip()]
    assert len(heads) == 1, f"Alembic must have exactly one head, got: {heads}"
    assert _REVISION in heads[0]


def test_stock_count_table_and_movement_column_exist():
    assert _table_exists("inventory_stock_counts")
    assert _column_exists("ingredient_stock_movements", "stock_count_id")


def test_every_stock_count_constraint_exists():
    for name in _COUNT_CONSTRAINTS:
        assert _constraint_exists(name), f"missing constraint {name}"


def test_uniqueness_and_triggers_exist():
    assert _index_exists("uq_movement_stock_count")
    for table, trigger in _COUNT_TRIGGERS:
        assert _trigger_exists(table, trigger), f"missing trigger {trigger} on {table}"


def test_movement_type_domain_includes_stock_count_adjustment():
    assert "STOCK_COUNT_ADJUSTMENT" in _check_clause("ck_movement_type_domain")


def test_delta_rule_lets_a_count_move_on_hand_either_way_and_never_reserved():
    """
    A count corrects UP as well as DOWN (the shelf may hold more than the system
    thought), so the rule is abs(delta) — and reserved must be pinned to zero,
    because a count observes the shelf and never un-promises a waffle.
    """
    # PostgreSQL normalises the stored clause (numeric literals become
    # `(0)::numeric`), so match on its own rendering rather than the source text.
    clause = " ".join(_check_clause("ck_movement_delta_matches_type").split())
    assert "STOCK_COUNT_ADJUSTMENT" in clause

    count_rule = clause[clause.index("STOCK_COUNT_ADJUSTMENT"):]
    # abs(): the shelf may hold MORE than the system thought, so a count corrects in
    # either direction — unlike WASTE, which may only ever remove.
    assert "abs(quantity_delta_on_hand) = quantity" in count_rule
    # ...and reserved is pinned to zero: a count observes the shelf, and never
    # un-promises a waffle an accepted order is counting on.
    assert "quantity_delta_reserved = (0)::numeric" in count_rule


def test_delta_quantity_is_generated_not_written():
    """
    A count that claims a delta its own two numbers do not support is the exact lie
    this table exists to prevent — so the application does not get to write it.
    """
    generated = _scalar(
        "SELECT is_generated FROM information_schema.columns "
        "WHERE table_name = 'inventory_stock_counts' AND column_name = 'delta_quantity'"
    )
    assert generated == "ALWAYS"


def test_reason_and_actor_are_required_for_a_count_movement():
    assert "STOCK_COUNT_ADJUSTMENT" in _check_clause("ck_movement_actor_required")
    assert "STOCK_COUNT_ADJUSTMENT" in _check_clause("ck_movement_reason_required")


# ═══════════════════════════════════════════════════════════════════════════
# 2. The database refuses a corrupt count
# ═══════════════════════════════════════════════════════════════════════════

class TestCountRowIntegrity:
    def test_negative_counted_quantity_is_refused(self, db, env):
        with pytest.raises(_DB_REJECTS):
            _insert_count(db, env, counted="-1.000")
            db.commit()
        db.rollback()

    def test_count_below_reserved_is_refused(self, db, env):
        """ck_stock_count_counted_ge_reserved. Counting 1 kg while 4 kg is promised
        does not mean the system was wrong — it means the shop sold what it has not
        got. That is an incident, not a count."""
        with pytest.raises(_DB_REJECTS):
            _insert_count(db, env, counted="1.000", system_reserved="4.000")
            db.commit()
        db.rollback()

    def test_empty_reason_is_refused(self, db, env):
        with pytest.raises(_DB_REJECTS):
            _insert_count(db, env, reason="   ")
            db.commit()
        db.rollback()

    def test_workflow_statuses_are_refused(self, db, env):
        """There is one status, APPLIED. DRAFT/SUBMITTED/APPROVED would be a mutable
        column with no state machine behind it — a lie inviting one to be written."""
        for bogus in ("DRAFT", "SUBMITTED", "APPROVED", "PENDING"):
            with pytest.raises(_DB_REJECTS):
                _insert_count(db, env, status=bogus)
                db.commit()
            db.rollback()

    def test_counter_must_belong_to_the_counted_store(self, db, env, make_store, make_staff):
        """fk_stock_count_actor_store. A Kadıköy manager counting Beşiktaş's freezer
        is unrepresentable, not merely forbidden."""
        other = make_store()
        outsider = make_staff("MANAGER", store_id=other.id)

        with pytest.raises(_DB_REJECTS):
            _insert_count(db, env, actor_id=outsider.id)  # store_id is still ours
            db.commit()
        db.rollback()

    def test_count_must_reference_an_existing_store_scoped_stock_row(
        self, db, env, make_store, make_staff
    ):
        """fk_stock_count_stock_store. You cannot count a shelf this branch does not
        have."""
        other = make_store()                      # no stock row for this ingredient
        their_manager = make_staff("MANAGER", store_id=other.id)

        with pytest.raises(_DB_REJECTS):
            _insert_count(db, env, store_id=other.id, actor_id=their_manager.id)
            db.commit()
        db.rollback()

    def test_idempotency_uniqueness_is_scoped_to_the_store(
        self, db, env, make_store, make_staff
    ):
        """
        The SAME key in the SAME store collides (a replay); the same key in ANOTHER
        store does not (a coincidence between two managers with the same run-book).
        """
        from tests.conftest import stock_for

        other = make_store()
        stock_for(db, env.ingredient, other.id, on_hand=Decimal("4.000"))
        their_manager = make_staff("MANAGER", store_id=other.id)

        shared = uuid.uuid4().hex

        def _insert(store_id, actor_id, soh):
            db.execute(
                text(
                    """
                    INSERT INTO inventory_stock_counts (
                        store_id, ingredient_id, counted_quantity,
                        system_on_hand_quantity, system_reserved_quantity, unit,
                        reason, status, counted_by_user_id,
                        idempotency_key_hash, request_hash
                    ) VALUES (
                        :store, :ing, :soh, :soh, 0, 'kg',
                        'forced', 'APPLIED', :actor, :k, :r
                    )
                    """
                ),
                {
                    "store": store_id, "ing": env.ingredient_id, "soh": soh,
                    "actor": actor_id, "k": shared, "r": uuid.uuid4().hex,
                },
            )

        # Zero-delta rows (counted == system_on_hand), so no movement is needed and
        # the deferred trigger is satisfied.
        _insert(DEFAULT_STORE_ID, env.manager_id, "10.000")
        _insert(other.id, their_manager.id, "4.000")
        db.commit()   # different stores, same key → both stand

        with pytest.raises(_DB_REJECTS):
            _insert(DEFAULT_STORE_ID, env.manager_id, "10.000")  # same store, same key
            db.commit()
        db.rollback()

    def test_count_row_is_immutable(self, db, env):
        """A count that was got wrong is superseded by counting again, never edited:
        today's manager does not get to rewrite what yesterday's manager saw."""
        count_id = _insert_count(db, env, counted="10.000", system_on_hand="10.000")
        db.commit()

        with pytest.raises(_DB_REJECTS):
            db.execute(
                text("UPDATE inventory_stock_counts SET counted_quantity = 1 WHERE id = :i"),
                {"i": count_id},
            )
            db.commit()
        db.rollback()

        with pytest.raises(_DB_REJECTS):
            db.execute(
                text("DELETE FROM inventory_stock_counts WHERE id = :i"), {"i": count_id}
            )
            db.commit()
        db.rollback()


class TestCountMovementIntegrity:
    """The deferred trigger and the composite key: a count and its movement can
    never tell different stories."""

    def test_a_matching_count_and_movement_commit(self, db, env):
        """The control case — the shape everything below deviates from."""
        count_id = _insert_count(db, env, counted="9.250")
        _insert_movement(db, env, count_id=count_id)
        db.commit()   # the deferred trigger fires here and is satisfied

        assert _scalar(
            "SELECT count(*) FROM ingredient_stock_movements WHERE stock_count_id = :i",
            i=count_id,
        ) == 1

    def test_non_zero_count_without_a_movement_is_refused(self, db, env):
        """
        THE worst outcome this feature can produce: the count sheet says the shelf
        was corrected, and the shelf's stock never moved. Both records look
        internally consistent; only comparing them finds it.
        """
        with pytest.raises(_DB_REJECTS):
            _insert_count(db, env, counted="9.250")   # delta -0.750, no movement
            db.commit()
        db.rollback()

    def test_zero_delta_count_with_a_movement_is_refused(self, db, env):
        """The other half of the policy: a count that found nothing wrong must not
        move stock."""
        count_id = _insert_count(db, env, counted="10.000", system_on_hand="10.000")
        with pytest.raises(_DB_REJECTS):
            _insert_movement(
                db, env, count_id=count_id, quantity="1.000", delta_on_hand="-1.000"
            )
            db.commit()
        db.rollback()

    def test_zero_delta_count_alone_commits(self, db, env):
        """...and a zero-delta count with NO movement is exactly right, and is kept
        as evidence that the shelf was checked."""
        count_id = _insert_count(db, env, counted="10.000", system_on_hand="10.000")
        db.commit()

        assert _scalar(
            "SELECT delta_quantity FROM inventory_stock_counts WHERE id = :i", i=count_id
        ) == Decimal("0.000")
        assert _scalar(
            "SELECT count(*) FROM ingredient_stock_movements WHERE stock_count_id = :i",
            i=count_id,
        ) == 0

    def test_movement_delta_must_match_the_counts_delta(self, db, env):
        """A count that says −0.750 and a movement that moved −0.500 is a ledger that
        disagrees with the count sheet."""
        count_id = _insert_count(db, env, counted="9.250")     # delta -0.750
        with pytest.raises(_DB_REJECTS):
            _insert_movement(
                db, env, count_id=count_id, quantity="0.500", delta_on_hand="-0.500"
            )
            db.commit()
        db.rollback()

    def test_movement_direction_must_match_the_counts_sign(self, db, env):
        """A shortage that ADDS stock to the shelf."""
        count_id = _insert_count(db, env, counted="9.250")     # delta -0.750
        with pytest.raises(_DB_REJECTS):
            _insert_movement(
                db, env, count_id=count_id, quantity="0.750", delta_on_hand="0.750"
            )
            db.commit()
        db.rollback()

    def test_movement_quantity_must_equal_the_absolute_delta(self, db, env):
        """quantity is the magnitude of the event. A |quantity| that disagrees with
        the delta is refused by ck_movement_delta_matches_type before the trigger
        even runs."""
        count_id = _insert_count(db, env, counted="9.250")
        with pytest.raises(_DB_REJECTS):
            _insert_movement(
                db, env, count_id=count_id, quantity="5.000", delta_on_hand="-0.750"
            )
            db.commit()
        db.rollback()

    def test_movement_reserved_delta_must_be_zero(self, db, env):
        """A count observes the shelf. It does not un-promise a waffle."""
        count_id = _insert_count(db, env, counted="9.250")
        with pytest.raises(_DB_REJECTS):
            _insert_movement(
                db, env, count_id=count_id,
                quantity="0.750", delta_on_hand="-0.750", delta_reserved="-0.750",
            )
            db.commit()
        db.rollback()

    def test_movement_type_must_be_stock_count_adjustment(self, db, env):
        """
        ck_movement_stock_count_link, both halves. A WASTE row carrying a
        stock_count_id would let a write-off masquerade as a counted discrepancy and
        vanish from the waste report...
        """
        count_id = _insert_count(db, env, counted="9.250")
        with pytest.raises(_DB_REJECTS):
            _insert_movement(
                db, env, count_id=count_id, movement_type="WASTE",
                quantity="0.750", delta_on_hand="-0.750",
            )
            db.commit()
        db.rollback()

    def test_stock_count_adjustment_without_a_count_is_refused(self, db, env):
        """...and the converse: a correction with no count behind it is a correction
        with no evidence, which is the very thing this feature exists to stop."""
        with pytest.raises(_DB_REJECTS):
            _insert_movement(
                db, env, count_id=None, quantity="0.750", delta_on_hand="-0.750"
            )
            db.commit()
        db.rollback()

    def test_movement_must_match_the_counts_store(self, db, env, make_store, make_staff):
        """fk_movement_stock_count_leg. A Kadıköy count's correction booked against
        Beşiktaş's shelf."""
        from tests.conftest import stock_for

        other = make_store()
        stock_for(db, env.ingredient, other.id, on_hand=Decimal("4.000"))
        their_manager = make_staff("MANAGER", store_id=other.id)

        count_id = _insert_count(db, env, counted="9.250")   # in OUR store
        with pytest.raises(_DB_REJECTS):
            _insert_movement(
                db, env, count_id=count_id,
                store_id=other.id, actor_id=their_manager.id,  # ...booked in THEIRS
            )
            db.commit()
        db.rollback()

    def test_movement_must_match_the_counts_ingredient(self, db, env):
        """A count of the chocolate whose correction was applied to the pistachio."""
        other_ing, _ = make_ingredient(
            db, on_hand=Decimal("5.000"), unit="kg", store_id=DEFAULT_STORE_ID
        )
        try:
            count_id = _insert_count(db, env, counted="9.250")
            with pytest.raises(_DB_REJECTS):
                _insert_movement(db, env, count_id=count_id, ingredient_id=other_ing.id)
                db.commit()
            db.rollback()
        finally:
            cleanup_ingredient(db, other_ing.id)

    def test_at_most_one_movement_per_count(self, db, env):
        """uq_movement_stock_count — the same correction posted twice."""
        count_id = _insert_count(db, env, counted="9.250")
        _insert_movement(db, env, count_id=count_id)
        with pytest.raises(_DB_REJECTS):
            _insert_movement(db, env, count_id=count_id)
            db.commit()
        db.rollback()


# ═══════════════════════════════════════════════════════════════════════════
# 3. The migration round-trips, and refuses to destroy evidence
# ═══════════════════════════════════════════════════════════════════════════

def test_downgrade_removes_only_this_branchs_schema(db):
    _release(db)
    _alembic("downgrade", "-1")

    # Gone: this branch.
    assert not _table_exists("inventory_stock_counts")
    assert not _column_exists("ingredient_stock_movements", "stock_count_id")
    assert not _index_exists("uq_movement_stock_count")
    assert not _constraint_exists("ck_movement_stock_count_link")
    assert "STOCK_COUNT_ADJUSTMENT" not in _check_clause("ck_movement_type_domain")

    # Still there: everything the previous branches own. The transfer workflow's
    # table, the ledger, and the shared append-only function that this migration
    # BORROWED and therefore must not have dropped.
    assert _table_exists("inventory_transfers")
    assert _table_exists("ingredient_stock_movements")
    assert _trigger_exists(
        "ingredient_stock_movements", "trg_ingredient_stock_movements_immutable"
    )
    assert "TRANSFER_OUT" in _check_clause("ck_movement_type_domain")


def test_re_upgrade_restores_everything(db):
    _release(db)
    _alembic("downgrade", "-1")
    _alembic("upgrade", "head")

    assert _table_exists("inventory_stock_counts")
    assert _column_exists("ingredient_stock_movements", "stock_count_id")
    assert "STOCK_COUNT_ADJUSTMENT" in _check_clause("ck_movement_type_domain")
    for name in _COUNT_CONSTRAINTS:
        assert _constraint_exists(name), name
    for table, trigger in _COUNT_TRIGGERS:
        assert _trigger_exists(table, trigger), trigger

    proc = _alembic_raw("current")
    assert _REVISION in proc.stdout


def test_downgrade_refuses_while_counts_exist(db, env):
    """
    Dropping the table would destroy the only record that a shelf was ever
    physically counted, while leaving the stock those counts moved in place — a bare
    −0.750 kg in the ledger with nothing to explain it. A lossy downgrade is worse
    than no downgrade, so it aborts.
    """
    res = env.client.post(
        "/inventory/stock-counts",
        json={
            "ingredient_id": env.ingredient_id,
            "counted_quantity": "9.250",
            "reason": "Haftalik sayim",
        },
        headers={"Idempotency-Key": uuid.uuid4().hex},
    )
    assert res.status_code == 200, res.text
    _release(db)

    proc = _alembic_raw("downgrade", "-1")
    assert proc.returncode != 0, "downgrade must refuse while counts exist"
    assert "StockCountsExist" in (proc.stdout + proc.stderr)

    # The schema — and the count — survive the refusal untouched.
    assert _table_exists("inventory_stock_counts")
    assert _scalar("SELECT count(*) FROM inventory_stock_counts") >= 1
