"""inventory transfer workflow: transfers as one event with two linked movements

Revision ID: f4b8c1d90e26
Revises: e2c9a4b16d38
Create Date: 2026-07-12

Moving stock between branches stops being two unrelated manual adjustments and
becomes a first-class business event.

Why that is a schema change and not a convention
------------------------------------------------
Before this migration, shipping 2 kg of chocolate from Kadıköy to Beşiktaş could
only be typed in as ``MANUAL_ADJUSTMENT -2`` in one store and
``MANUAL_ADJUSTMENT +2`` in the other. Nothing in the database says those two
rows are the same chocolate. So:

  * one can commit and the other fail, and 2 kg of chocolate simply ceases to
    exist — with a perfectly consistent-looking ledger on each side;
  * reconciliation sees a shortage in one branch and a surplus in the other and
    has no way to know they are the same event;
  * to the owner's reports the outbound half looks like WASTE (a branch that
    threw away chocolate it actually shipped) and, had a purchase receipt been
    used, the inbound half looks like a supplier delivery that never happened.

A convention ("always fill in the same reason text") cannot fix any of that,
because nothing enforces a convention. A table and a set of foreign keys can.

Schema
------
  inventory_transfers                                                    (NEW)
      One row per transfer: what moved, from which store, to which store, who
      initiated it, why, when, and the hashes that make the request idempotent.

      ck_transfer_quantity_positive     quantity > 0
      ck_transfer_stores_differ         source <> destination
      ck_transfer_status_domain         status = 'COMPLETED' (both legs post
                                        atomically; there IS no other state)
      ck_transfer_reason_present        a shipment out of a branch is always
                                        explained
      fk_transfer_actor_source_store    (source_store_id, initiated_by_user_id)
                                        → users(store_id, id). The initiator
                                        BELONGS to the source store; a Store A
                                        manager shipping Store B's stock is
                                        unrepresentable, not merely forbidden.
      fk_transfer_source_stock          (source_store_id, ingredient_id)
      fk_transfer_destination_stock     (destination_store_id, ingredient_id)
                                        → ingredient_stock. Both sides really
                                        hold a stock row for the thing that moved.
      uq_transfer_source_idem           (source_store_id, idempotency_key_hash).
                                        Source-store scoped, exactly like the
                                        movement ledger's: two branch managers
                                        sending the same Idempotency-Key is a
                                        coincidence, not a replay.

  ingredient_stock_movements
      + transfer_id            → inventory_transfers.id, NULL for everything else
      + transfer_out_store_id  GENERATED: store_id when the row is a TRANSFER_OUT
      + transfer_in_store_id   GENERATED: store_id when the row is a TRANSFER_IN

      The two generated columns exist to turn "the OUT leg is booked in the
      transfer's source store, the IN leg in its destination store" into a
      FOREIGN KEY. A single FK cannot conditionally target two different columns
      of inventory_transfers, so the direction is projected into its own column
      and each FK is left MATCH SIMPLE — on a TRANSFER_OUT row transfer_in_store_id
      is NULL and the inbound FK does not apply, and vice versa. They are
      GENERATED ALWAYS ... STORED, so the application cannot forge them.

      fk_movement_transfer_source_leg       OUT leg ↔ transfer's source side
      fk_movement_transfer_destination_leg  IN  leg ↔ transfer's destination side
      ck_movement_transfer_link             transfer_id present ⟺ a transfer type
      ck_movement_transfer_in_no_actor      see below
      uq_movement_transfer_direction        at most ONE leg of each direction

  Movement type domain gains TRANSFER_OUT and TRANSFER_IN, and the delta rule:
      TRANSFER_OUT   on_hand -quantity   reserved 0
      TRANSFER_IN    on_hand +quantity   reserved 0
  Reserved never moves. A transfer moves physical stock, not a promise made to a
  customer.

Why the inbound leg carries no actor
------------------------------------
fk_movement_actor_store (from the store-scoped refactor) says staff only move
stock in their OWN store. The person who authorises a transfer works in the
SOURCE store, but the inbound movement lands in the DESTINATION store — so naming
them as its actor would break that constraint, and weakening the constraint to
allow it would re-open exactly the cross-store hole it was added to close.

So the inbound leg has actor_user_id IS NULL (enforced:
ck_movement_transfer_in_no_actor) and accountability lives on the transfer row's
initiated_by_user_id, which is bound to the source store by its own composite FK.
Nothing is lost: the transfer is one event, and one event has one initiator.

The pairing invariant (why a trigger is unavoidable here)
---------------------------------------------------------
Every constraint above is per-row. None of them can say "this transfer has both
of its halves", because that is a statement about a SET of rows — and a
one-sided transfer (stock that left a branch and arrived nowhere) is the single
worst outcome this feature can produce.

So: a DEFERRED constraint trigger, checked at COMMIT, on both tables. It refuses
any transfer that does not have exactly one TRANSFER_OUT in its source store and
exactly one TRANSFER_IN in its destination store, each for its ingredient, its
quantity, and the correct signs. Deferred, because the two legs cannot both exist
at the instant the first one is inserted.

Written to the same rules as the append-only trigger it sits beside:
schema-qualified references, a pinned search_path, no dynamic SQL, SECURITY
INVOKER, EXECUTE revoked from PUBLIC, and no GUC or session variable that can
switch it off. There is deliberately no application-reachable bypass.

Data safety
-----------
Purely additive. No existing row of any table is read, rewritten or deleted:
there are no transfers to backfill, because before this migration a transfer was
not a thing that could be recorded. Every existing movement keeps
transfer_id = NULL and is untouched by every new constraint (each is written to
be vacuously true when transfer_id IS NULL). Orders, payments and stock
quantities are not touched at all.

downgrade() removes only this branch's schema — the table, the three columns, the
new constraints, the indexes and the trigger — and restores the previous movement
type domain and delta rule verbatim. It refuses to run if any transfer exists,
because dropping inventory_transfers would delete the only record that a shipment
between two branches ever happened, while leaving the stock it moved in place:
the ledger would then show a bare -2 kg in one store and +2 kg in another with
nothing to explain either. Export or reverse the transfers first.
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "f4b8c1d90e26"
down_revision = "e2c9a4b16d38"
branch_labels = None
depends_on = None


class TransfersExist(Exception):
    """
    Raised when a downgrade would destroy the record of real branch transfers.
    Aborts the migration; nothing is committed.
    """


# The movement type domain, before and after. Spelled out rather than imported
# from the models: a migration must keep describing the schema it created even
# after the application's constants have moved on.
_TYPES_BEFORE = (
    "RESERVATION_CREATED", "RESERVATION_RELEASED", "CONSUMPTION", "WASTE",
    "RETURNED", "MANUAL_ADJUSTMENT", "PURCHASE_RECEIPT",
)
_TYPES_AFTER = _TYPES_BEFORE + ("TRANSFER_OUT", "TRANSFER_IN")

_MANUAL_BEFORE = ("MANUAL_ADJUSTMENT", "WASTE", "RETURNED", "PURCHASE_RECEIPT")
# TRANSFER_OUT requires an actor. TRANSFER_IN deliberately does not — see the
# module docstring.
_MANUAL_AFTER = _MANUAL_BEFORE + ("TRANSFER_OUT",)

_REASON_BEFORE = ("MANUAL_ADJUSTMENT", "WASTE")
_REASON_AFTER = _REASON_BEFORE + ("TRANSFER_OUT", "TRANSFER_IN")

_TRANSFER_TYPES = ("TRANSFER_OUT", "TRANSFER_IN")


def _sql_list(types) -> str:
    return ",".join(f"'{t}'" for t in types)


# The per-type delta rule. The pre-transfer clauses are reproduced verbatim so
# that downgrade() restores exactly what was there before.
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
"""

_DELTA_CLAUSES_AFTER = _DELTA_CLAUSES_BEFORE + """
    OR (movement_type = 'TRANSFER_OUT'
        AND quantity_delta_on_hand = -quantity
        AND quantity_delta_reserved = 0)
    OR (movement_type = 'TRANSFER_IN'
        AND quantity_delta_on_hand = quantity
        AND quantity_delta_reserved = 0)
"""

_PAIRING_FN = "public.sweetops_check_transfer_pairing"
_TRG_TRANSFER = "trg_inventory_transfers_paired"
_TRG_MOVEMENT = "trg_transfer_movement_paired"


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
    # 1. The transfer itself — the business event the two legs belong to
    # ═══════════════════════════════════════════════════════════════════════
    op.create_table(
        "inventory_transfers",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("source_store_id", sa.Integer(), nullable=False),
        sa.Column("destination_store_id", sa.Integer(), nullable=False),
        sa.Column("ingredient_id", sa.Integer(), nullable=False),
        sa.Column("quantity", sa.Numeric(12, 3), nullable=False),
        sa.Column("unit", sa.String(10), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="COMPLETED"),
        sa.Column("reason", sa.String(500), nullable=False),
        sa.Column("note", sa.String(500), nullable=True),
        sa.Column("initiated_by_user_id", sa.Integer(), nullable=False),
        sa.Column("idempotency_key_hash", sa.String(64), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "completed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["source_store_id"], ["stores.id"], name="fk_transfer_source_store"),
        sa.ForeignKeyConstraint(
            ["destination_store_id"], ["stores.id"], name="fk_transfer_destination_store"
        ),
        sa.ForeignKeyConstraint(["ingredient_id"], ["ingredients.id"], name="fk_transfer_ingredient"),
        sa.ForeignKeyConstraint(["initiated_by_user_id"], ["users.id"], name="fk_transfer_actor"),
        # The initiator BELONGS to the source store. users.store_id is nullable, so
        # a member of staff with no store assignment can never initiate a transfer.
        sa.ForeignKeyConstraint(
            ["source_store_id", "initiated_by_user_id"],
            ["users.store_id", "users.id"],
            name="fk_transfer_actor_source_store",
        ),
        # Both branches genuinely hold a stock row for what moved. The destination's
        # is materialised at zero by the service before the transfer is written, so
        # a branch can receive an ingredient it has never held.
        sa.ForeignKeyConstraint(
            ["source_store_id", "ingredient_id"],
            ["ingredient_stock.store_id", "ingredient_stock.ingredient_id"],
            name="fk_transfer_source_stock",
        ),
        sa.ForeignKeyConstraint(
            ["destination_store_id", "ingredient_id"],
            ["ingredient_stock.store_id", "ingredient_stock.ingredient_id"],
            name="fk_transfer_destination_stock",
        ),
        sa.CheckConstraint("quantity > 0", name="ck_transfer_quantity_positive"),
        sa.CheckConstraint(
            "source_store_id <> destination_store_id", name="ck_transfer_stores_differ"
        ),
        sa.CheckConstraint("status IN ('COMPLETED')", name="ck_transfer_status_domain"),
        sa.CheckConstraint(
            "char_length(btrim(reason)) > 0", name="ck_transfer_reason_present"
        ),
        sa.UniqueConstraint(
            "source_store_id", "idempotency_key_hash", name="uq_transfer_source_idem"
        ),
        # FK targets for the movement legs below. Redundant against the primary
        # key, but PostgreSQL requires a unique constraint on exactly the
        # referenced tuple before it will accept the composite foreign keys.
        sa.UniqueConstraint(
            "id", "source_store_id", "ingredient_id", name="uq_transfer_source_leg"
        ),
        sa.UniqueConstraint(
            "id", "destination_store_id", "ingredient_id", name="uq_transfer_destination_leg"
        ),
    )
    op.create_index("ix_inventory_transfers_source_store_id", "inventory_transfers", ["source_store_id"])
    op.create_index(
        "ix_inventory_transfers_destination_store_id", "inventory_transfers", ["destination_store_id"]
    )
    op.create_index("ix_inventory_transfers_ingredient_id", "inventory_transfers", ["ingredient_id"])
    op.create_index(
        "ix_inventory_transfers_initiated_by_user_id", "inventory_transfers", ["initiated_by_user_id"]
    )
    op.create_index("ix_transfer_source_created", "inventory_transfers", ["source_store_id", "created_at"])
    op.create_index(
        "ix_transfer_destination_created", "inventory_transfers", ["destination_store_id", "created_at"]
    )

    # ═══════════════════════════════════════════════════════════════════════
    # 2. The ledger learns about transfers
    # ═══════════════════════════════════════════════════════════════════════
    # Every existing movement gets transfer_id = NULL, which is exactly right:
    # none of them was a transfer, because a transfer could not be recorded.
    op.add_column(
        "ingredient_stock_movements", sa.Column("transfer_id", sa.BigInteger(), nullable=True)
    )
    # GENERATED ALWAYS ... STORED: PostgreSQL derives these from movement_type and
    # store_id. The application cannot write them, so it cannot lie about which
    # side of a transfer a movement is on.
    op.execute(
        """
        ALTER TABLE ingredient_stock_movements
        ADD COLUMN transfer_out_store_id integer
        GENERATED ALWAYS AS (
            CASE WHEN movement_type = 'TRANSFER_OUT' THEN store_id END
        ) STORED
        """
    )
    op.execute(
        """
        ALTER TABLE ingredient_stock_movements
        ADD COLUMN transfer_in_store_id integer
        GENERATED ALWAYS AS (
            CASE WHEN movement_type = 'TRANSFER_IN' THEN store_id END
        ) STORED
        """
    )
    op.create_index(
        "ix_ingredient_stock_movements_transfer_id", "ingredient_stock_movements", ["transfer_id"]
    )
    op.create_foreign_key(
        "fk_movement_transfer",
        "ingredient_stock_movements",
        "inventory_transfers",
        ["transfer_id"],
        ["id"],
    )

    # The OUT leg's (transfer, store, ingredient) must BE the transfer's
    # (id, source_store, ingredient); the IN leg's must be its
    # (id, destination_store, ingredient). MATCH SIMPLE: when the direction column
    # is NULL (the row is the other direction, or is not a transfer at all) the
    # constraint simply does not apply.
    op.create_foreign_key(
        "fk_movement_transfer_source_leg",
        "ingredient_stock_movements",
        "inventory_transfers",
        ["transfer_id", "transfer_out_store_id", "ingredient_id"],
        ["id", "source_store_id", "ingredient_id"],
    )
    op.create_foreign_key(
        "fk_movement_transfer_destination_leg",
        "ingredient_stock_movements",
        "inventory_transfers",
        ["transfer_id", "transfer_in_store_id", "ingredient_id"],
        ["id", "destination_store_id", "ingredient_id"],
    )

    # A transfer movement without a transfer is an orphan half of an event nothing
    # can reconcile; a transfer_id on any other type would let a purchase receipt
    # masquerade as an arriving shipment.
    op.create_check_constraint(
        "ck_movement_transfer_link",
        "ingredient_stock_movements",
        f"(movement_type IN ({_sql_list(_TRANSFER_TYPES)})) = (transfer_id IS NOT NULL)",
    )
    op.create_check_constraint(
        "ck_movement_transfer_in_no_actor",
        "ingredient_stock_movements",
        "movement_type <> 'TRANSFER_IN' OR actor_user_id IS NULL",
    )

    # At most one leg of each direction per transfer. With the deferred pairing
    # trigger (exactly one of each at COMMIT) and ck_movement_transfer_link (only
    # transfer types may carry a transfer_id), a transfer has EXACTLY two
    # movements — never one, never three.
    op.execute(
        """
        CREATE UNIQUE INDEX uq_movement_transfer_direction
        ON ingredient_stock_movements (transfer_id, movement_type)
        WHERE transfer_id IS NOT NULL
        """
    )

    _retype_movement_constraints(
        types=_TYPES_AFTER,
        manual=_MANUAL_AFTER,
        reason=_REASON_AFTER,
        deltas=_DELTA_CLAUSES_AFTER,
    )

    # ═══════════════════════════════════════════════════════════════════════
    # 3. The pairing invariant — the one thing no per-row constraint can say
    # ═══════════════════════════════════════════════════════════════════════
    # "This transfer has both of its halves" is a statement about a SET of rows.
    # A one-sided transfer is stock that left a branch and arrived nowhere, which
    # is the worst outcome this feature can produce, so it is checked at COMMIT by
    # a DEFERRED constraint trigger on BOTH tables:
    #
    #   on inventory_transfers  — catches a transfer whose legs were never written
    #   on the movement ledger  — catches a leg bolted onto a transfer afterwards
    #
    # Same rules as the append-only trigger beside it: SECURITY INVOKER, pinned
    # search_path, schema-qualified references, no dynamic SQL, EXECUTE revoked
    # from PUBLIC. There is no GUC or session variable that turns it off, because
    # any role — including one reached through an injection path — could set one.
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION {_PAIRING_FN}()
        RETURNS trigger LANGUAGE plpgsql
        SECURITY INVOKER
        SET search_path = pg_catalog
        AS $fn$
        DECLARE
            v_transfer_id bigint;
            t             public.inventory_transfers%ROWTYPE;
            v_out         integer;
            v_in          integer;
        BEGIN
            IF TG_TABLE_NAME = 'inventory_transfers' THEN
                v_transfer_id := NEW.id;
            ELSE
                v_transfer_id := NEW.transfer_id;
            END IF;

            IF v_transfer_id IS NULL THEN
                RETURN NULL;
            END IF;

            SELECT * INTO t
            FROM public.inventory_transfers
            WHERE id = v_transfer_id;

            IF NOT FOUND THEN
                -- The transfer was rolled back within this transaction. The
                -- movement's own foreign key has already refused the orphan.
                RETURN NULL;
            END IF;

            SELECT
                count(*) FILTER (
                    WHERE m.movement_type          = 'TRANSFER_OUT'
                      AND m.store_id               = t.source_store_id
                      AND m.ingredient_id          = t.ingredient_id
                      AND m.quantity               = t.quantity
                      AND m.quantity_delta_on_hand = -t.quantity
                      AND m.quantity_delta_reserved = 0
                ),
                count(*) FILTER (
                    WHERE m.movement_type          = 'TRANSFER_IN'
                      AND m.store_id               = t.destination_store_id
                      AND m.ingredient_id          = t.ingredient_id
                      AND m.quantity               = t.quantity
                      AND m.quantity_delta_on_hand = t.quantity
                      AND m.quantity_delta_reserved = 0
                )
            INTO v_out, v_in
            FROM public.ingredient_stock_movements m
            WHERE m.transfer_id = v_transfer_id;

            IF v_out <> 1 OR v_in <> 1 THEN
                RAISE EXCEPTION
                    'inventory transfer % is not balanced: expected exactly one '
                    'matching TRANSFER_OUT and one matching TRANSFER_IN, found '
                    'out=%, in=%',
                    v_transfer_id, v_out, v_in
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
        CREATE CONSTRAINT TRIGGER {_TRG_TRANSFER}
        AFTER INSERT ON inventory_transfers
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION {_PAIRING_FN}();
        """
    )
    op.execute(
        f"""
        CREATE CONSTRAINT TRIGGER {_TRG_MOVEMENT}
        AFTER INSERT ON ingredient_stock_movements
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW WHEN (NEW.transfer_id IS NOT NULL)
        EXECUTE FUNCTION {_PAIRING_FN}();
        """
    )


def downgrade() -> None:
    conn = op.get_bind()

    # Dropping inventory_transfers would delete the only record that a shipment
    # between two branches ever happened — while leaving the stock it moved
    # exactly where it moved it. The ledger would be left with a bare -2 kg in one
    # store and a bare +2 kg in another and nothing to explain either, and the
    # TRANSFER_OUT/TRANSFER_IN rows themselves would violate the restored type
    # domain. There is no correct way to reconstruct that afterwards, so refuse:
    # a lossy downgrade is worse than no downgrade.
    transfers = conn.execute(
        sa.text("SELECT COUNT(*) FROM inventory_transfers")
    ).scalar() or 0
    if transfers:
        raise TransfersExist(
            f"Cannot downgrade the inventory transfer workflow: {transfers} "
            "transfer(s) exist. Dropping the table would destroy the record of "
            "real stock movements between branches while leaving the stock they "
            "moved in place, and the TRANSFER_OUT/TRANSFER_IN ledger rows cannot "
            "be expressed in the pre-transfer movement type domain. Export or "
            "reverse the transfers first."
        )

    op.execute(f"DROP TRIGGER IF EXISTS {_TRG_MOVEMENT} ON ingredient_stock_movements")
    op.execute(f"DROP TRIGGER IF EXISTS {_TRG_TRANSFER} ON inventory_transfers")
    op.execute(f"DROP FUNCTION IF EXISTS {_PAIRING_FN}()")

    # Restore the pre-transfer movement domain and delta rule verbatim.
    _retype_movement_constraints(
        types=_TYPES_BEFORE,
        manual=_MANUAL_BEFORE,
        reason=_REASON_BEFORE,
        deltas=_DELTA_CLAUSES_BEFORE,
    )

    op.execute("DROP INDEX IF EXISTS uq_movement_transfer_direction")
    for _ck in ("ck_movement_transfer_in_no_actor", "ck_movement_transfer_link"):
        op.drop_constraint(_ck, "ingredient_stock_movements", type_="check")
    for _fk in (
        "fk_movement_transfer_destination_leg",
        "fk_movement_transfer_source_leg",
        "fk_movement_transfer",
    ):
        op.drop_constraint(_fk, "ingredient_stock_movements", type_="foreignkey")
    op.execute("DROP INDEX IF EXISTS ix_ingredient_stock_movements_transfer_id")

    op.drop_column("ingredient_stock_movements", "transfer_in_store_id")
    op.drop_column("ingredient_stock_movements", "transfer_out_store_id")
    op.drop_column("ingredient_stock_movements", "transfer_id")

    op.drop_table("inventory_transfers")
