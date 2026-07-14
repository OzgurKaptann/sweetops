"""inventory threshold alerts: early warning as configuration, never as stock

Revision ID: c8a4e7b13f92
Revises: a3d7e9c14b62
Create Date: 2026-07-15

The shop can now say exactly what it HAS. This migration lets a branch say what it
considers ENOUGH — so a manager finds out that the chocolate is running down while
there is still time to do something about it, rather than when a customer is standing
at the counter.

What is added, and what is deliberately NOT
-------------------------------------------
  ingredient_stock                                                    (COLUMNS)
      critical_quantity             at or below → operationally critical
      minimum_quantity              at or below → low, review/reorder
      target_quantity               the level replenishment aims back up to
      threshold_updated_at          when the levels last actually CHANGED
      threshold_updated_by_user_id  who changed them

  inventory_threshold_updates                                         (NEW TABLE)
      One append-only row per threshold decision: old values, new values, who, why,
      and the hashes that make the request idempotent.

Not added: any movement type, any stock column, any trigger that touches stock. A
threshold is a SETTING, and this migration cannot move a gram of anything. That is
not a promise made in a docstring — it is a consequence of where the columns are:
ingredient_stock_movements is not touched by this revision at all, and there is no
movement type a threshold change could be written as even if some future service
tried (ck_movement_type_domain would refuse the row).

Why NULL, and why NULL is not zero
----------------------------------
A threshold may be NULL, and NULL means NOT CONFIGURED — nobody has yet said what
"low" means for this ingredient in this branch.

It emphatically does not mean zero. Zero is a real and useful threshold ("warn me
only when it is actually gone"), and a manager who deliberately sets it must not have
their decision rendered as an absence of one. The two states are different facts and
they get different representations, which is why the alert view reports
NOT_CONFIGURED rather than quietly reporting HEALTHY: an unconfigured threshold is
missing information, and turning missing information into an all-clear is how a
monitoring system ends up lying to the person relying on it.

Nothing is backfilled. Every existing stock row gets three NULLs, which is the
truthful answer — no branch has configured a threshold, because until now it could
not. In particular the legacy ``reorder_level`` column is deliberately NOT copied
into ``minimum_quantity``: reorder_level is a coarse hint that the customer-facing
menu multiplies by 1.5 to colour a badge, and seed.py sets it to a flat 15% of
opening stock for every ingredient. Promoting a seeded guess into an operational
alert level would fill the manager's new alert screen with warnings nobody chose and
nobody believes — and the first thing anybody does with an alert screen they do not
believe is stop reading it. reorder_level stays exactly where it is, doing exactly
what it did, for the customer menu and the decision engine.

Why the ordering constraints are PAIRWISE
-----------------------------------------
    critical <= minimum <= target

is the rule when all three are set, but it must also hold for any PARTIAL
configuration, and "critical only" or "minimum + target" are perfectly legitimate.
So the rule is three pairwise CHECKs, each vacuously true when either side is NULL:

    ck_stock_threshold_critical_le_minimum
    ck_stock_threshold_minimum_le_target
    ck_stock_threshold_critical_le_target   <- not redundant: it is the ONLY thing
                                               holding critical + target together
                                               when minimum is NULL

An inverted ladder is not a cosmetic problem. critical > minimum means an ingredient
reaches CRITICAL before it ever reaches LOW, so the "low stock, go and look at it"
warning the manager was relying on never fires at all — the alert they set up to give
themselves warning time silently deletes their warning time.

Negative thresholds are refused for a related reason: no quantity can ever fall below
zero (ck_stock_on_hand_nonneg), so a negative threshold is a setting that promises an
alert which can never fire. A control that silently does nothing is worse than no
control, because the manager believes they are covered.

Why a separate table for the change log
---------------------------------------
The live thresholds are on ingredient_stock, because that is what the alert screen
reads and it must be one indexed row per (store, ingredient).

But an Idempotency-Key has to be REMEMBERED, and a key hash column on the stock row
could only ever remember the most recent one. A manager whose connection dropped
mid-request would retry a key the row had already forgotten, and the update would be
applied a second time — which is exactly what the header exists to prevent. So every
CHANGE is its own row in inventory_threshold_updates, and
uq_threshold_update_store_idem (store_id, idempotency_key_hash) is what makes the
replay safe.

Store-scoped, like every other idempotency constraint in this system: two branch
managers working from the same printed run-book will legitimately send the same key,
and that is a coincidence, not a replay.

Data safety
-----------
Purely additive. No existing row of any table is read, rewritten or deleted; no stock
quantity, ledger row, order, payment, transfer or count is touched. Every existing
stock row gets NULL thresholds and is untouched by every new constraint (each is
vacuously true when its column is NULL).

downgrade() removes only this branch's schema — the table, the five columns, their
constraints and the trigger. It refuses to run while any threshold update exists,
because dropping the log would destroy the record of who decided what "low" means and
why, and that record cannot be reconstructed from anything else. The thresholds
themselves would go with it, silently disarming every alert a branch had configured.
Export the updates first.
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "c8a4e7b13f92"
down_revision = "a3d7e9c14b62"
branch_labels = None
depends_on = None


class ThresholdUpdatesExist(Exception):
    """
    Raised when a downgrade would destroy the record of real threshold decisions.
    Aborts the migration; nothing is committed.
    """


# Reused from c3b7e01f9a24 (it is generic — it reports TG_TABLE_NAME). This migration
# does NOT own it and must not drop it on downgrade.
_IMMUTABLE_FN = "public.sweetops_block_inventory_mutation"
_TRG_IMMUTABLE = "trg_inventory_threshold_updates_immutable"


def upgrade() -> None:
    # ═══════════════════════════════════════════════════════════════════════
    # 1. The thresholds in force, on the store-scoped configuration row
    # ═══════════════════════════════════════════════════════════════════════
    # Nullable, with no server_default: every existing row becomes NOT CONFIGURED,
    # which is the truthful answer. A default of 0 would silently tell every branch
    # that every ingredient is critical the moment it is empty and healthy otherwise
    # — an opinion nobody expressed.
    op.add_column(
        "ingredient_stock", sa.Column("critical_quantity", sa.Numeric(12, 3), nullable=True)
    )
    op.add_column(
        "ingredient_stock", sa.Column("minimum_quantity", sa.Numeric(12, 3), nullable=True)
    )
    op.add_column(
        "ingredient_stock", sa.Column("target_quantity", sa.Numeric(12, 3), nullable=True)
    )
    op.add_column(
        "ingredient_stock",
        sa.Column("threshold_updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "ingredient_stock",
        sa.Column("threshold_updated_by_user_id", sa.Integer(), nullable=True),
    )
    op.create_index(
        "ix_ingredient_stock_threshold_updated_by_user_id",
        "ingredient_stock",
        ["threshold_updated_by_user_id"],
    )
    op.create_foreign_key(
        "fk_stock_threshold_actor",
        "ingredient_stock",
        "users",
        ["threshold_updated_by_user_id"],
        ["id"],
    )
    # Staff configure thresholds for their OWN branch. MATCH SIMPLE: when the column
    # is NULL (no threshold has ever been set) the constraint does not apply.
    op.create_foreign_key(
        "fk_stock_threshold_actor_store",
        "ingredient_stock",
        "users",
        ["store_id", "threshold_updated_by_user_id"],
        ["store_id", "id"],
    )

    # A negative threshold promises an alert that can never fire — see the module
    # docstring. Each check is vacuously true when its column is NULL.
    op.create_check_constraint(
        "ck_stock_threshold_critical_nonneg",
        "ingredient_stock",
        "critical_quantity IS NULL OR critical_quantity >= 0",
    )
    op.create_check_constraint(
        "ck_stock_threshold_minimum_nonneg",
        "ingredient_stock",
        "minimum_quantity IS NULL OR minimum_quantity >= 0",
    )
    op.create_check_constraint(
        "ck_stock_threshold_target_nonneg",
        "ingredient_stock",
        "target_quantity IS NULL OR target_quantity >= 0",
    )
    # The alert ladder, pairwise so that a PARTIAL configuration is still checked.
    op.create_check_constraint(
        "ck_stock_threshold_critical_le_minimum",
        "ingredient_stock",
        "critical_quantity IS NULL OR minimum_quantity IS NULL"
        " OR critical_quantity <= minimum_quantity",
    )
    op.create_check_constraint(
        "ck_stock_threshold_minimum_le_target",
        "ingredient_stock",
        "minimum_quantity IS NULL OR target_quantity IS NULL"
        " OR minimum_quantity <= target_quantity",
    )
    # NOT redundant: this is the only constraint holding critical and target together
    # when minimum is not configured.
    op.create_check_constraint(
        "ck_stock_threshold_critical_le_target",
        "ingredient_stock",
        "critical_quantity IS NULL OR target_quantity IS NULL"
        " OR critical_quantity <= target_quantity",
    )

    # ═══════════════════════════════════════════════════════════════════════
    # 2. The change log — old → new, who, why, and the idempotency hashes
    # ═══════════════════════════════════════════════════════════════════════
    op.create_table(
        "inventory_threshold_updates",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("store_id", sa.Integer(), nullable=False),
        sa.Column("ingredient_id", sa.Integer(), nullable=False),
        # NULL on either side is a real state: "was not configured" / "is no longer
        # configured". Clearing a threshold is a decision, and it is on the record.
        sa.Column("old_critical_quantity", sa.Numeric(12, 3), nullable=True),
        sa.Column("old_minimum_quantity", sa.Numeric(12, 3), nullable=True),
        sa.Column("old_target_quantity", sa.Numeric(12, 3), nullable=True),
        sa.Column("new_critical_quantity", sa.Numeric(12, 3), nullable=True),
        sa.Column("new_minimum_quantity", sa.Numeric(12, 3), nullable=True),
        sa.Column("new_target_quantity", sa.Numeric(12, 3), nullable=True),
        sa.Column("reason", sa.String(500), nullable=False),
        sa.Column("updated_by_user_id", sa.Integer(), nullable=False),
        # Only hashes. Never the raw Idempotency-Key, never the raw request body.
        sa.Column("idempotency_key_hash", sa.String(64), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["store_id"], ["stores.id"], name="fk_threshold_update_store"
        ),
        sa.ForeignKeyConstraint(
            ["ingredient_id"], ["ingredients.id"], name="fk_threshold_update_ingredient"
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_user_id"], ["users.id"], name="fk_threshold_update_actor"
        ),
        # The person who set the threshold BELONGS to the branch it governs.
        sa.ForeignKeyConstraint(
            ["store_id", "updated_by_user_id"],
            ["users.store_id", "users.id"],
            name="fk_threshold_update_actor_store",
        ),
        # Thresholds are configured FOR a stock row that exists in this branch. This
        # table never creates stock: a branch that does not carry an ingredient has
        # no shelf to warn about.
        sa.ForeignKeyConstraint(
            ["store_id", "ingredient_id"],
            ["ingredient_stock.store_id", "ingredient_stock.ingredient_id"],
            name="fk_threshold_update_stock_store",
        ),
        # The same rules the live row is held to. A log that can record a decision the
        # configuration itself would refuse is not an audit trail.
        sa.CheckConstraint(
            "new_critical_quantity IS NULL OR new_critical_quantity >= 0",
            name="ck_threshold_update_critical_nonneg",
        ),
        sa.CheckConstraint(
            "new_minimum_quantity IS NULL OR new_minimum_quantity >= 0",
            name="ck_threshold_update_minimum_nonneg",
        ),
        sa.CheckConstraint(
            "new_target_quantity IS NULL OR new_target_quantity >= 0",
            name="ck_threshold_update_target_nonneg",
        ),
        sa.CheckConstraint(
            "new_critical_quantity IS NULL OR new_minimum_quantity IS NULL"
            " OR new_critical_quantity <= new_minimum_quantity",
            name="ck_threshold_update_critical_le_minimum",
        ),
        sa.CheckConstraint(
            "new_minimum_quantity IS NULL OR new_target_quantity IS NULL"
            " OR new_minimum_quantity <= new_target_quantity",
            name="ck_threshold_update_minimum_le_target",
        ),
        sa.CheckConstraint(
            "new_critical_quantity IS NULL OR new_target_quantity IS NULL"
            " OR new_critical_quantity <= new_target_quantity",
            name="ck_threshold_update_critical_le_target",
        ),
        sa.CheckConstraint(
            "char_length(btrim(reason)) > 0", name="ck_threshold_update_reason_present"
        ),
        # Store-scoped idempotency. Beşiktaş's update must never return Kadıköy's
        # result and quietly configure nothing.
        sa.UniqueConstraint(
            "store_id", "idempotency_key_hash", name="uq_threshold_update_store_idem"
        ),
    )
    op.create_index(
        "ix_inventory_threshold_updates_store_id",
        "inventory_threshold_updates",
        ["store_id"],
    )
    op.create_index(
        "ix_inventory_threshold_updates_ingredient_id",
        "inventory_threshold_updates",
        ["ingredient_id"],
    )
    op.create_index(
        "ix_inventory_threshold_updates_updated_by_user_id",
        "inventory_threshold_updates",
        ["updated_by_user_id"],
    )
    op.create_index(
        "ix_threshold_update_store_ingredient_created",
        "inventory_threshold_updates",
        ["store_id", "ingredient_id", "created_at"],
    )

    # ═══════════════════════════════════════════════════════════════════════
    # 3. The log is append-only, exactly like the ledger and the counts
    # ═══════════════════════════════════════════════════════════════════════
    # Reuses the generic block function from c3b7e01f9a24 (it reports TG_TABLE_NAME,
    # so it needs no per-table variant). This migration does not own that function
    # and does not drop it on downgrade.
    #
    # A threshold decision that was got wrong is not edited — editing it would let
    # today's manager rewrite what yesterday's manager decided, which is precisely
    # what someone quietly disarming an alert would want to do. It is superseded by
    # making a new decision, and both stay on the record.
    op.execute(
        f"""
        CREATE TRIGGER {_TRG_IMMUTABLE}
        BEFORE UPDATE OR DELETE ON inventory_threshold_updates
        FOR EACH ROW EXECUTE FUNCTION {_IMMUTABLE_FN}();
        """
    )


def downgrade() -> None:
    conn = op.get_bind()

    # Dropping the log would destroy the record of who decided what "low" means in
    # each branch and why — a record nothing else in the system holds — and dropping
    # the columns would silently disarm every alert those decisions configured. A
    # branch would go from "warn me at 3 kg" to no warning at all, with nothing in the
    # database to say it ever had one. A lossy downgrade is worse than no downgrade.
    updates = conn.execute(
        sa.text("SELECT COUNT(*) FROM inventory_threshold_updates")
    ).scalar() or 0
    if updates:
        raise ThresholdUpdatesExist(
            f"Cannot downgrade inventory threshold alerts: {updates} threshold "
            "update(s) exist. Dropping the log would destroy the record of who "
            "configured each branch's alert levels and why, and dropping the columns "
            "would silently disarm every alert configured from it. Export the "
            "threshold updates first."
        )

    op.execute(
        f"DROP TRIGGER IF EXISTS {_TRG_IMMUTABLE} ON inventory_threshold_updates"
    )
    # _IMMUTABLE_FN is deliberately NOT dropped: it belongs to c3b7e01f9a24, and the
    # movement ledger and stock counts still depend on it.

    op.drop_table("inventory_threshold_updates")

    for _ck in (
        "ck_stock_threshold_critical_le_target",
        "ck_stock_threshold_minimum_le_target",
        "ck_stock_threshold_critical_le_minimum",
        "ck_stock_threshold_target_nonneg",
        "ck_stock_threshold_minimum_nonneg",
        "ck_stock_threshold_critical_nonneg",
    ):
        op.drop_constraint(_ck, "ingredient_stock", type_="check")

    for _fk in ("fk_stock_threshold_actor_store", "fk_stock_threshold_actor"):
        op.drop_constraint(_fk, "ingredient_stock", type_="foreignkey")
    op.execute("DROP INDEX IF EXISTS ix_ingredient_stock_threshold_updated_by_user_id")

    for _col in (
        "threshold_updated_by_user_id",
        "threshold_updated_at",
        "target_quantity",
        "minimum_quantity",
        "critical_quantity",
    ):
        op.drop_column("ingredient_stock", _col)
