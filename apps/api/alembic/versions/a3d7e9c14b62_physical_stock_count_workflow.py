"""physical stock count workflow: counts as first-class events, not adjustments

Revision ID: a3d7e9c14b62
Revises: f4b8c1d90e26
Create Date: 2026-07-14

Counting the shelf stops being a signed MANUAL_ADJUSTMENT and becomes a first-class
business event that remembers what was counted, what the system believed, and what
the difference was.

Why that is a schema change and not a convention
------------------------------------------------
Before this migration, counting the chocolate freezer and finding 3.850 kg where
the system said 4.200 kg could only be typed in as ``MANUAL_ADJUSTMENT -0.350``.
That row records the CORRECTION and destroys the evidence:

  * the two numbers the difference came from are gone, so nobody can ever check
    the arithmetic or tell a real count from a guess;
  * a count that found the shelf CORRECT leaves no trace at all — its delta is
    zero, and a zero-delta adjustment is rejected as a no-op. "We counted it and
    it was right" becomes indistinguishable from "nobody looked";
  * to analytics a counted shrinkage is identical to a deliberate one-off
    correction, so the owner cannot see how much stock is quietly walking out of
    the door;
  * nothing binds the correction to the act of counting, so nothing can prove the
    shelf was ever physically inspected.

A convention ("always write 'sayım farkı' in the reason") fixes none of that,
because nothing enforces a convention. A table and a set of constraints can.

Schema
------
  inventory_stock_counts                                                  (NEW)
      One row per count: what was counted, what the system believed AT THAT
      MOMENT, who counted it, why, when, and the hashes that make the request
      idempotent.

      delta_quantity                    GENERATED ALWAYS AS
                                        (counted - system_on_hand) STORED. The
                                        application cannot write it, so a count
                                        cannot claim a delta its own two numbers
                                        do not support.
      ck_stock_count_counted_nonneg     a shelf cannot hold a negative amount
      ck_stock_count_counted_ge_reserved
                                        THE safety rule. A count may not push
                                        on-hand below what accepted orders are
                                        already promised — see below.
      ck_stock_count_status_domain      status = 'APPLIED'. The count row and its
                                        movement post atomically; there IS no
                                        other state.
      ck_stock_count_reason_present     an unexplained correction to physical
                                        stock is indistinguishable from theft
      fk_stock_count_actor_store        (store_id, counted_by_user_id) →
                                        users(store_id, id). The counter BELONGS
                                        to the store whose shelf they counted.
      fk_stock_count_stock_store        (store_id, ingredient_id) →
                                        ingredient_stock. You cannot count a shelf
                                        this branch does not have.
      uq_stock_count_store_idem         (store_id, idempotency_key_hash), scoped
                                        to the store exactly like the movement
                                        ledger's and the transfer's.

  ingredient_stock_movements
      + stock_count_id  → inventory_stock_counts.id, NULL for everything else

      fk_movement_stock_count_leg   (stock_count_id, store_id, ingredient_id) →
                                    the count's own (id, store_id, ingredient_id).
                                    A count's correction booked against another
                                    branch's shelf is unrepresentable.
      ck_movement_stock_count_link  STOCK_COUNT_ADJUSTMENT ⟺ stock_count_id present
      uq_movement_stock_count       at most ONE movement per count

  Movement type domain gains STOCK_COUNT_ADJUSTMENT, and the delta rule:
      STOCK_COUNT_ADJUSTMENT   on_hand ±quantity   reserved 0
  Reserved never moves. A count corrects what is physically on the shelf; it does
  not alter a promise already made to a customer.

Why counting below reserved is REFUSED
--------------------------------------
If a manager counts 3 kg while 5 kg is promised to accepted orders, the honest
reading is not "the system was wrong by 2 kg" — it is "this shop has sold 2 kg of
chocolate it does not physically have". Writing on-hand down to 3 would break
ck_stock_reserved_le_on_hand anyway; suppressing that by silently releasing
reservations would break a promise to a customer who is sitting at a table waiting
for their waffle. That is an operational incident (cancel or re-source the orders,
THEN count), not a stock correction, so the count is refused —
ck_stock_count_counted_ge_reserved in the database, ``stock_count_below_reserved``
with a Turkish message in the service.

The zero-delta policy, and why the schema alone almost enforces it
-----------------------------------------------------------------
Policy: a zero-delta count IS RECORDED, and writes NO movement.

That is the whole point of the table. Nothing physical happened, so nothing belongs
in the physical ledger — a zero-delta movement is a ledger row that moves no stock,
which is noise in the one record an auditor reads. But the COUNT happened, it is
evidence that the shelf was checked, and it is stored.

Note what the existing constraints already give for free: a STOCK_COUNT_ADJUSTMENT
must satisfy ``abs(quantity_delta_on_hand) = quantity`` AND the pre-existing
``ck_movement_quantity_positive`` (quantity > 0). A zero-delta movement would need
quantity = 0 and is therefore structurally unrepresentable. The policy is not
merely enforced by the trigger below — under the delta rule it is the only thing
the schema permits.

The pairing invariant (why a trigger is unavoidable here)
---------------------------------------------------------
Every constraint above is per-row. None of them can say "this count has the
movement its own delta demands", because that is a statement about a SET of rows —
and the failure it prevents is the worst one this feature can produce: a count row
that says the shelf was corrected while the shelf's stock was never actually moved
(or moved by the wrong amount).

So: a DEFERRED constraint trigger, checked at COMMIT, on both tables:

    delta <> 0  ⟹  exactly ONE STOCK_COUNT_ADJUSTMENT for this count, in its store,
                   for its ingredient, with quantity = abs(delta),
                   quantity_delta_on_hand = delta, quantity_delta_reserved = 0
    delta  = 0  ⟹  exactly ZERO movements

Deferred, because the count row and its movement cannot both exist at the instant
the first one is inserted. On BOTH tables, because the count catches a movement
that was never written and the movement catches one bolted on afterwards.

Written to the same rules as the append-only and transfer-pairing triggers beside
it: SECURITY INVOKER, a pinned search_path, schema-qualified references, no dynamic
SQL, EXECUTE revoked from PUBLIC. There is deliberately no GUC, session variable or
other application-reachable bypass.

Immutability
------------
inventory_stock_counts gets the SAME append-only trigger as the ledger
(public.sweetops_block_inventory_mutation, installed by c3b7e01f9a24 and reused
here — this migration does not own it and does not drop it). A count that was got
wrong is not edited; it is superseded by counting again, which is what a manager
would physically have to do anyway.

Data safety
-----------
Purely additive. No existing row of any table is read, rewritten or deleted: there
are no counts to backfill, because before this migration a count was not a thing
that could be recorded. A historical MANUAL_ADJUSTMENT that was *morally* a count
is deliberately left exactly as it is — retro-labelling it a count would fabricate
two system snapshots that were never captured, which is precisely the lie this
table exists to prevent. Every existing movement keeps stock_count_id = NULL and is
untouched by every new constraint (each is vacuously true when it is NULL). Orders,
payments, transfers and stock quantities are not touched at all.

downgrade() removes only this branch's schema — the table, the column, the new
constraints, the indexes and the two triggers — and restores the previous movement
type domain and delta rule verbatim. It refuses to run if any count exists, because
dropping inventory_stock_counts would destroy the only record that a shelf was ever
physically counted while leaving the stock the count moved in place: the ledger
would be left with a bare −0.350 kg and nothing to explain it, and the
STOCK_COUNT_ADJUSTMENT rows themselves cannot be expressed in the restored type
domain. Export the counts first.
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "a3d7e9c14b62"
down_revision = "f4b8c1d90e26"
branch_labels = None
depends_on = None


class StockCountsExist(Exception):
    """
    Raised when a downgrade would destroy the record of real physical counts.
    Aborts the migration; nothing is committed.
    """


# The movement type domain, before and after. Spelled out rather than imported from
# the models: a migration must keep describing the schema it created even after the
# application's constants have moved on.
_TYPES_BEFORE = (
    "RESERVATION_CREATED", "RESERVATION_RELEASED", "CONSUMPTION", "WASTE",
    "RETURNED", "MANUAL_ADJUSTMENT", "PURCHASE_RECEIPT", "TRANSFER_OUT",
    "TRANSFER_IN",
)
_TYPES_AFTER = _TYPES_BEFORE + ("STOCK_COUNT_ADJUSTMENT",)

_MANUAL_BEFORE = (
    "MANUAL_ADJUSTMENT", "WASTE", "RETURNED", "PURCHASE_RECEIPT", "TRANSFER_OUT",
)
_MANUAL_AFTER = _MANUAL_BEFORE + ("STOCK_COUNT_ADJUSTMENT",)

_REASON_BEFORE = ("MANUAL_ADJUSTMENT", "WASTE", "TRANSFER_OUT", "TRANSFER_IN")
_REASON_AFTER = _REASON_BEFORE + ("STOCK_COUNT_ADJUSTMENT",)


def _sql_list(types) -> str:
    return ",".join(f"'{t}'" for t in types)


# The per-type delta rule. The pre-count clauses are reproduced verbatim so that
# downgrade() restores exactly what was there before.
_DELTA_CLAUSES_BEFORE = """
    legacy_backfill
    OR (movement_type = 'RESERVATION_CREATED'
        AND quantity_delta_on_hand = 0
        AND quantity_delta_reserved = quantity)
    OR (movement_type = 'RESERVATION_RELEASED'
        AND quantity_delta_on_hand = 0
        AND quantity_delta_reserved = -quantity)
    OR (movement_type = 'CONSUMPTION'
        AND quantity_delta_on_hand = -quantity
        AND quantity_delta_reserved = -quantity)
    OR (movement_type = 'WASTE'
        AND quantity_delta_on_hand = -quantity
        AND quantity_delta_reserved = 0)
    OR (movement_type IN ('RETURNED', 'PURCHASE_RECEIPT')
        AND quantity_delta_on_hand = quantity
        AND quantity_delta_reserved = 0)
    OR (movement_type = 'MANUAL_ADJUSTMENT'
        AND abs(quantity_delta_on_hand) = quantity
        AND quantity_delta_reserved = 0)

    OR (movement_type = 'TRANSFER_OUT'
        AND quantity_delta_on_hand = -quantity
        AND quantity_delta_reserved = 0)
    OR (movement_type = 'TRANSFER_IN'
        AND quantity_delta_on_hand = quantity
        AND quantity_delta_reserved = 0)
"""

# A count's correction moves on-hand in EITHER direction (the shelf may hold more
# than the system thought) and never touches reserved. Together with the existing
# quantity > 0 check, this makes a zero-delta movement unrepresentable.
_DELTA_CLAUSES_AFTER = _DELTA_CLAUSES_BEFORE + """
    OR (movement_type = 'STOCK_COUNT_ADJUSTMENT'
        AND abs(quantity_delta_on_hand) = quantity
        AND quantity_delta_reserved = 0)
"""

_PAIRING_FN = "public.sweetops_check_stock_count_movement"
_TRG_COUNT = "trg_inventory_stock_counts_movement"
_TRG_MOVEMENT = "trg_stock_count_movement_matches"

# Reused from c3b7e01f9a24 (it is generic — it reports TG_TABLE_NAME). This
# migration does NOT own it and must not drop it on downgrade.
_IMMUTABLE_FN = "public.sweetops_block_inventory_mutation"
_TRG_IMMUTABLE = "trg_inventory_stock_counts_immutable"


def _retype_movement_constraints(*, types, manual, reason, deltas) -> None:
    """Rewrite the four movement-type CHECK constraints to a given domain."""
    for name in (
        "ck_movement_type_domain",
        "ck_movement_actor_required",
        "ck_movement_reason_required",
        "ck_movement_delta_matches_type",
    ):
        op.drop_constraint(name, "ingredient_stock_movements", type_="check")

    op.create_check_constraint(
        "ck_movement_type_domain",
        "ingredient_stock_movements",
        f"movement_type IN ({_sql_list(types)})",
    )
    op.create_check_constraint(
        "ck_movement_actor_required",
        "ingredient_stock_movements",
        f"legacy_backfill OR movement_type NOT IN ({_sql_list(manual)})"
        " OR actor_user_id IS NOT NULL",
    )
    op.create_check_constraint(
        "ck_movement_reason_required",
        "ingredient_stock_movements",
        f"legacy_backfill OR movement_type NOT IN ({_sql_list(reason)})"
        " OR (reason IS NOT NULL AND char_length(btrim(reason)) > 0)",
    )
    op.create_check_constraint(
        "ck_movement_delta_matches_type", "ingredient_stock_movements", deltas
    )


def upgrade() -> None:
    # ═══════════════════════════════════════════════════════════════════════
    # 1. The count itself — the event the movement is merely the effect of
    # ═══════════════════════════════════════════════════════════════════════
    op.create_table(
        "inventory_stock_counts",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("store_id", sa.Integer(), nullable=False),
        sa.Column("ingredient_id", sa.Integer(), nullable=False),
        sa.Column("counted_quantity", sa.Numeric(12, 3), nullable=False),
        # The system's belief AT THE MOMENT OF COUNTING, captured under the stock
        # row's lock. Without these two columns a count is just an adjustment with
        # extra words: they are what makes the delta checkable after the fact.
        sa.Column("system_on_hand_quantity", sa.Numeric(12, 3), nullable=False),
        sa.Column("system_reserved_quantity", sa.Numeric(12, 3), nullable=False),
        sa.Column(
            "delta_quantity",
            sa.Numeric(12, 3),
            sa.Computed("counted_quantity - system_on_hand_quantity", persisted=True),
            nullable=False,
        ),
        sa.Column("unit", sa.String(10), nullable=False),
        sa.Column("reason", sa.String(500), nullable=False),
        sa.Column("note", sa.String(500), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="APPLIED"),
        sa.Column("counted_by_user_id", sa.Integer(), nullable=False),
        sa.Column("idempotency_key_hash", sa.String(64), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "applied_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["store_id"], ["stores.id"], name="fk_stock_count_store"),
        sa.ForeignKeyConstraint(
            ["ingredient_id"], ["ingredients.id"], name="fk_stock_count_ingredient"
        ),
        sa.ForeignKeyConstraint(
            ["counted_by_user_id"], ["users.id"], name="fk_stock_count_actor"
        ),
        # The counter BELONGS to the store whose shelf they counted. users.store_id
        # is nullable, so a member of staff with no store assignment can never
        # count — which is the correct answer, not an accident.
        sa.ForeignKeyConstraint(
            ["store_id", "counted_by_user_id"],
            ["users.store_id", "users.id"],
            name="fk_stock_count_actor_store",
        ),
        # You cannot count a shelf this branch does not have. Deliberately NOT
        # materialised-on-demand the way a transfer's destination row is: a
        # transfer brings stock with it, whereas a count of an ingredient the
        # branch does not stock is a manager counting the wrong thing.
        sa.ForeignKeyConstraint(
            ["store_id", "ingredient_id"],
            ["ingredient_stock.store_id", "ingredient_stock.ingredient_id"],
            name="fk_stock_count_stock_store",
        ),
        sa.CheckConstraint("counted_quantity >= 0", name="ck_stock_count_counted_nonneg"),
        sa.CheckConstraint(
            "system_on_hand_quantity >= 0", name="ck_stock_count_system_on_hand_nonneg"
        ),
        sa.CheckConstraint(
            "system_reserved_quantity >= 0", name="ck_stock_count_system_reserved_nonneg"
        ),
        # THE safety rule. See the module docstring: counting below reserved is an
        # operational incident, not a stock correction.
        sa.CheckConstraint(
            "counted_quantity >= system_reserved_quantity",
            name="ck_stock_count_counted_ge_reserved",
        ),
        sa.CheckConstraint("status IN ('APPLIED')", name="ck_stock_count_status_domain"),
        sa.CheckConstraint(
            "char_length(btrim(reason)) > 0", name="ck_stock_count_reason_present"
        ),
        # Store-scoped idempotency: two branch managers sending the same
        # Idempotency-Key from the same printed count sheet is a coincidence, not a
        # replay.
        sa.UniqueConstraint(
            "store_id", "idempotency_key_hash", name="uq_stock_count_store_idem"
        ),
        # FK target for the movement's composite key below. Redundant against the
        # primary key, but PostgreSQL requires a unique constraint on exactly the
        # referenced tuple.
        sa.UniqueConstraint(
            "id", "store_id", "ingredient_id", name="uq_stock_count_movement_leg"
        ),
    )
    op.create_index(
        "ix_inventory_stock_counts_store_id", "inventory_stock_counts", ["store_id"]
    )
    op.create_index(
        "ix_inventory_stock_counts_ingredient_id",
        "inventory_stock_counts",
        ["ingredient_id"],
    )
    op.create_index(
        "ix_inventory_stock_counts_counted_by_user_id",
        "inventory_stock_counts",
        ["counted_by_user_id"],
    )
    op.create_index(
        "ix_stock_count_store_created",
        "inventory_stock_counts",
        ["store_id", "created_at"],
    )
    op.create_index(
        "ix_stock_count_store_ingredient_created",
        "inventory_stock_counts",
        ["store_id", "ingredient_id", "created_at"],
    )

    # ═══════════════════════════════════════════════════════════════════════
    # 2. The ledger learns about counts
    # ═══════════════════════════════════════════════════════════════════════
    # Every existing movement gets stock_count_id = NULL, which is exactly right:
    # none of them was a count, because a count could not be recorded.
    op.add_column(
        "ingredient_stock_movements",
        sa.Column("stock_count_id", sa.BigInteger(), nullable=True),
    )
    op.create_index(
        "ix_ingredient_stock_movements_stock_count_id",
        "ingredient_stock_movements",
        ["stock_count_id"],
    )
    op.create_foreign_key(
        "fk_movement_stock_count",
        "ingredient_stock_movements",
        "inventory_stock_counts",
        ["stock_count_id"],
        ["id"],
    )
    # The movement's (count, store, ingredient) triple must BE the count's own.
    # MATCH SIMPLE: when stock_count_id is NULL the constraint does not apply.
    op.create_foreign_key(
        "fk_movement_stock_count_leg",
        "ingredient_stock_movements",
        "inventory_stock_counts",
        ["stock_count_id", "store_id", "ingredient_id"],
        ["id", "store_id", "ingredient_id"],
    )

    # Biconditional, both halves load-bearing. A STOCK_COUNT_ADJUSTMENT with no
    # count is a correction with no evidence; a stock_count_id on any other type
    # would let a waste write-off masquerade as a counted discrepancy.
    op.create_check_constraint(
        "ck_movement_stock_count_link",
        "ingredient_stock_movements",
        "(movement_type = 'STOCK_COUNT_ADJUSTMENT') = (stock_count_id IS NOT NULL)",
    )
    op.execute(
        """
        CREATE UNIQUE INDEX uq_movement_stock_count
        ON ingredient_stock_movements (stock_count_id)
        WHERE stock_count_id IS NOT NULL
        """
    )

    _retype_movement_constraints(
        types=_TYPES_AFTER,
        manual=_MANUAL_AFTER,
        reason=_REASON_AFTER,
        deltas=_DELTA_CLAUSES_AFTER,
    )

    # ═══════════════════════════════════════════════════════════════════════
    # 3. The count/movement invariant — what no per-row constraint can say
    # ═══════════════════════════════════════════════════════════════════════
    # "This count has exactly the movement its own delta demands" is a statement
    # about a SET of rows. The failure it prevents is a count row claiming the
    # shelf was corrected while the shelf's stock never actually moved — a lie in
    # the one record an auditor trusts.
    #
    # Checked at COMMIT by a DEFERRED constraint trigger on BOTH tables:
    #
    #   on inventory_stock_counts        — catches a count whose movement was never written
    #   on ingredient_stock_movements    — catches a movement bolted on afterwards
    #
    # Same hardening as the triggers beside it: SECURITY INVOKER, pinned
    # search_path, schema-qualified references, no dynamic SQL, EXECUTE revoked
    # from PUBLIC. No GUC or session variable turns it off, because any role —
    # including one reached through an injection path — could set one.
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION {_PAIRING_FN}()
        RETURNS trigger LANGUAGE plpgsql
        SECURITY INVOKER
        SET search_path = pg_catalog
        AS $fn$
        DECLARE
            v_count_id bigint;
            c          public.inventory_stock_counts%ROWTYPE;
            v_matching integer;
            v_total    integer;
        BEGIN
            IF TG_TABLE_NAME = 'inventory_stock_counts' THEN
                v_count_id := NEW.id;
            ELSE
                v_count_id := NEW.stock_count_id;
            END IF;

            IF v_count_id IS NULL THEN
                RETURN NULL;
            END IF;

            SELECT * INTO c
            FROM public.inventory_stock_counts
            WHERE id = v_count_id;

            IF NOT FOUND THEN
                -- The count was rolled back within this transaction. The
                -- movement's own foreign key has already refused the orphan.
                RETURN NULL;
            END IF;

            SELECT
                count(*) FILTER (
                    WHERE m.movement_type           = 'STOCK_COUNT_ADJUSTMENT'
                      AND m.store_id                = c.store_id
                      AND m.ingredient_id           = c.ingredient_id
                      AND m.quantity                = abs(c.delta_quantity)
                      AND m.quantity_delta_on_hand  = c.delta_quantity
                      AND m.quantity_delta_reserved = 0
                ),
                count(*)
            INTO v_matching, v_total
            FROM public.ingredient_stock_movements m
            WHERE m.stock_count_id = v_count_id;

            IF c.delta_quantity = 0 THEN
                -- Zero-delta policy: the count is RECORDED as evidence the shelf
                -- was checked, and writes NO movement. Nothing physical happened,
                -- so nothing belongs in the physical ledger.
                IF v_total <> 0 THEN
                    RAISE EXCEPTION
                        'stock count % has a zero delta but % ledger movement(s): a '
                        'count that changed nothing must not move stock',
                        v_count_id, v_total
                        USING ERRCODE = 'integrity_constraint_violation';
                END IF;
            ELSIF v_matching <> 1 OR v_total <> 1 THEN
                RAISE EXCEPTION
                    'stock count % is not balanced: expected exactly one '
                    'STOCK_COUNT_ADJUSTMENT of % in store % for ingredient %, '
                    'found matching=%, total=%',
                    v_count_id, c.delta_quantity, c.store_id, c.ingredient_id,
                    v_matching, v_total
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;

            RETURN NULL;
        END;
        $fn$;
        """
    )
    op.execute(f"REVOKE ALL ON FUNCTION {_PAIRING_FN}() FROM PUBLIC")

    op.execute(
        f"""
        CREATE CONSTRAINT TRIGGER {_TRG_COUNT}
        AFTER INSERT ON inventory_stock_counts
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION {_PAIRING_FN}();
        """
    )
    op.execute(
        f"""
        CREATE CONSTRAINT TRIGGER {_TRG_MOVEMENT}
        AFTER INSERT ON ingredient_stock_movements
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW WHEN (NEW.stock_count_id IS NOT NULL)
        EXECUTE FUNCTION {_PAIRING_FN}();
        """
    )

    # ═══════════════════════════════════════════════════════════════════════
    # 4. Counts are append-only, exactly like the ledger
    # ═══════════════════════════════════════════════════════════════════════
    # Reuses the generic block function from c3b7e01f9a24 (it reports
    # TG_TABLE_NAME, so it needs no per-table variant). This migration does not own
    # that function and does not drop it on downgrade.
    #
    # A count that was got wrong is not edited — editing it would let today's
    # manager rewrite what yesterday's manager says they saw on the shelf. It is
    # superseded by counting again, which is what one would physically have to do.
    op.execute(
        f"""
        CREATE TRIGGER {_TRG_IMMUTABLE}
        BEFORE UPDATE OR DELETE ON inventory_stock_counts
        FOR EACH ROW EXECUTE FUNCTION {_IMMUTABLE_FN}();
        """
    )


def downgrade() -> None:
    conn = op.get_bind()

    # Dropping inventory_stock_counts would destroy the only record that a shelf was
    # ever physically counted — while leaving the stock those counts moved exactly
    # where they moved it. The ledger would be left with a bare −0.350 kg and
    # nothing to explain it, and the STOCK_COUNT_ADJUSTMENT rows themselves would
    # violate the restored type domain. There is no correct way to reconstruct that
    # afterwards, so refuse: a lossy downgrade is worse than no downgrade.
    counts = conn.execute(
        sa.text("SELECT COUNT(*) FROM inventory_stock_counts")
    ).scalar() or 0
    if counts:
        raise StockCountsExist(
            f"Cannot downgrade the physical stock count workflow: {counts} stock "
            "count(s) exist. Dropping the table would destroy the record of real "
            "physical counts while leaving the stock they corrected in place, and "
            "the STOCK_COUNT_ADJUSTMENT ledger rows cannot be expressed in the "
            "pre-count movement type domain. Export the counts first."
        )

    op.execute(f"DROP TRIGGER IF EXISTS {_TRG_IMMUTABLE} ON inventory_stock_counts")
    op.execute(f"DROP TRIGGER IF EXISTS {_TRG_MOVEMENT} ON ingredient_stock_movements")
    op.execute(f"DROP TRIGGER IF EXISTS {_TRG_COUNT} ON inventory_stock_counts")
    op.execute(f"DROP FUNCTION IF EXISTS {_PAIRING_FN}()")
    # _IMMUTABLE_FN is deliberately NOT dropped: it belongs to c3b7e01f9a24 and the
    # movement ledger's own immutability trigger still depends on it.

    # Restore the pre-count movement domain and delta rule verbatim.
    _retype_movement_constraints(
        types=_TYPES_BEFORE,
        manual=_MANUAL_BEFORE,
        reason=_REASON_BEFORE,
        deltas=_DELTA_CLAUSES_BEFORE,
    )

    op.execute("DROP INDEX IF EXISTS uq_movement_stock_count")
    op.drop_constraint(
        "ck_movement_stock_count_link", "ingredient_stock_movements", type_="check"
    )
    for _fk in ("fk_movement_stock_count_leg", "fk_movement_stock_count"):
        op.drop_constraint(_fk, "ingredient_stock_movements", type_="foreignkey")
    op.execute("DROP INDEX IF EXISTS ix_ingredient_stock_movements_stock_count_id")
    op.drop_column("ingredient_stock_movements", "stock_count_id")

    op.drop_table("inventory_stock_counts")
