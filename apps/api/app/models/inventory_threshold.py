"""
Inventory alert thresholds — the record of who decided what "low" means, and when.

What this module is NOT
----------------------
It is not stock. Nothing here is a quantity the shop physically owns, nothing here
moves a gram, and nothing here writes to the movement ledger. A threshold is a
SETTING: the level at which a manager wants to be warned. Changing it changes what
the alert screen says, and changes literally nothing else.

That separation is the whole point, and it is structural rather than conventional:

  * thresholds live on ingredient_stock (the store-scoped configuration row) and in
    THIS table (the change log). Neither is ingredient_stock_movements.
  * there is no movement type for a threshold change, so one cannot be written even
    by mistake — ck_movement_type_domain would refuse the row.
  * reconciliation compares the summary against the LEDGER, and a threshold column
    appears in neither side of that comparison.

So a manager who edits a threshold cannot, by any path, alter consumption velocity,
waste totals, purchase-receipt totals, transfer metrics, or the stock-vs-ledger
reconciliation. See docs/INVENTORY_THRESHOLD_ALERTS.md § "Reconciliation and
analytics".

Why the current thresholds are NOT enough on their own
------------------------------------------------------
ingredient_stock holds the thresholds that are in force RIGHT NOW — that is what the
alert screen reads, and it is one indexed row per (store, ingredient).

But an idempotency key has to be remembered, and a column on the stock row could
only ever remember the LAST one. A manager whose network dropped mid-request would
then retry a key the row no longer knows about, and the retry would be applied a
second time — which is precisely what the Idempotency-Key exists to prevent. So each
CHANGE is its own row here, carrying its own key hash, and the store-scoped unique
constraint below is what makes the replay safe.

The row also carries the OLD values beside the new ones, which is what turns the log
into an answer to the question an owner actually asks: "who lowered the critical
level on chocolate, and what was it before?"

Immutable
---------
UPDATE and DELETE are refused by the same trigger that guards the ledger and the
stock counts. A threshold decision that was got wrong is not edited — it is
superseded by making another one, which leaves both on the record.
"""
from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from .base import Base

QTY = Numeric(12, 3)


# ── Threshold status domain ──────────────────────────────────────────────────
#
# The six answers the alert screen can give. They are ordered by OPERATIONAL
# SEVERITY, not by arithmetic, and the order is load-bearing — see
# threshold_status() in app/services/inventory_service.py.

# on_hand < reserved: the branch has promised stock it does not physically hold.
# The database makes this unrepresentable (ck_stock_reserved_le_on_hand), so it
# should never appear. It is reported anyway, and reported FIRST, because if it ever
# does appear it is not a stock level — it is an incident, and burying it under
# "stokta yok" would hide the one row a manager must act on today.
THRESHOLD_STATUS_BELOW_RESERVED = "BELOW_RESERVED"

# available <= 0. There may still be stock on the shelf — every gram of it is
# already promised to an accepted order.
THRESHOLD_STATUS_OUT_OF_STOCK = "OUT_OF_STOCK"

THRESHOLD_STATUS_CRITICAL = "CRITICAL"
THRESHOLD_STATUS_LOW = "LOW"
THRESHOLD_STATUS_HEALTHY = "HEALTHY"

# Nobody has said what "low" means for this ingredient in this branch. Deliberately
# NOT reported as HEALTHY: an unconfigured threshold is an absence of information,
# and rendering it as an all-clear would be the system inventing reassurance it has
# no basis for.
THRESHOLD_STATUS_NOT_CONFIGURED = "NOT_CONFIGURED"

THRESHOLD_STATUSES = (
    THRESHOLD_STATUS_BELOW_RESERVED,
    THRESHOLD_STATUS_OUT_OF_STOCK,
    THRESHOLD_STATUS_CRITICAL,
    THRESHOLD_STATUS_LOW,
    THRESHOLD_STATUS_HEALTHY,
    THRESHOLD_STATUS_NOT_CONFIGURED,
)


class InventoryThresholdUpdate(Base):
    """One threshold decision: what the levels were, what they became, and why."""

    __tablename__ = "inventory_threshold_updates"

    id = Column(BigInteger, primary_key=True, autoincrement=True)

    # Derived from the authenticated staff session, never from the request body.
    store_id = Column(Integer, ForeignKey("stores.id"), nullable=False, index=True)
    ingredient_id = Column(Integer, ForeignKey("ingredients.id"), nullable=False, index=True)

    # What the thresholds were BEFORE. NULL means that threshold was not configured
    # — which is a real, distinct previous state, and the one nearly every first
    # update comes from.
    old_critical_quantity = Column(QTY, nullable=True)
    old_minimum_quantity = Column(QTY, nullable=True)
    old_target_quantity = Column(QTY, nullable=True)

    # What they became. Deliberately NOT constrained to be different from the old
    # values: re-affirming a threshold after reviewing it is a decision a manager
    # genuinely makes, and it is worth having on the record that they looked.
    new_critical_quantity = Column(QTY, nullable=True)
    new_minimum_quantity = Column(QTY, nullable=True)
    new_target_quantity = Column(QTY, nullable=True)

    # Why. Mandatory, for the same reason a manual adjustment's reason is: a
    # threshold quietly lowered until it stops firing is how a branch ends up
    # discovering a stockout at the counter, and the record must say who decided
    # that and what they were thinking.
    reason = Column(String(500), nullable=False)

    updated_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)

    # Only hashes, never the raw Idempotency-Key and never the raw request body.
    idempotency_key_hash = Column(String(64), nullable=False)
    request_hash = Column(String(64), nullable=False)

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    # Explicit foreign_keys: updated_by_user_id has a second, composite path to
    # users, so the ORM cannot infer which columns to join on.
    store = relationship("Store", foreign_keys=[store_id])
    ingredient = relationship("Ingredient", foreign_keys=[ingredient_id])
    updated_by = relationship("User", foreign_keys=[updated_by_user_id])

    __table_args__ = (
        # The same non-negativity and ordering rules the live stock row is held to.
        # Enforced here as well as there so the LOG cannot record a decision the
        # configuration itself would have refused — an audit trail that can hold a
        # state the system rejects is not an audit trail.
        CheckConstraint(
            "new_critical_quantity IS NULL OR new_critical_quantity >= 0",
            name="ck_threshold_update_critical_nonneg",
        ),
        CheckConstraint(
            "new_minimum_quantity IS NULL OR new_minimum_quantity >= 0",
            name="ck_threshold_update_minimum_nonneg",
        ),
        CheckConstraint(
            "new_target_quantity IS NULL OR new_target_quantity >= 0",
            name="ck_threshold_update_target_nonneg",
        ),
        CheckConstraint(
            "new_critical_quantity IS NULL OR new_minimum_quantity IS NULL"
            " OR new_critical_quantity <= new_minimum_quantity",
            name="ck_threshold_update_critical_le_minimum",
        ),
        CheckConstraint(
            "new_minimum_quantity IS NULL OR new_target_quantity IS NULL"
            " OR new_minimum_quantity <= new_target_quantity",
            name="ck_threshold_update_minimum_le_target",
        ),
        CheckConstraint(
            "new_critical_quantity IS NULL OR new_target_quantity IS NULL"
            " OR new_critical_quantity <= new_target_quantity",
            name="ck_threshold_update_critical_le_target",
        ),
        CheckConstraint(
            "char_length(btrim(reason)) > 0", name="ck_threshold_update_reason_present"
        ),
        # The person who set the threshold BELONGS to the branch it governs.
        ForeignKeyConstraint(
            ["store_id", "updated_by_user_id"],
            ["users.store_id", "users.id"],
            name="fk_threshold_update_actor_store",
        ),
        # Thresholds are configured FOR a stock row that exists in this branch. A
        # store that does not stock an ingredient has no shelf to warn about, and is
        # not silently given one — this table does not create stock.
        ForeignKeyConstraint(
            ["store_id", "ingredient_id"],
            ["ingredient_stock.store_id", "ingredient_stock.ingredient_id"],
            name="fk_threshold_update_stock_store",
        ),
        # Idempotency is scoped to the STORE, exactly as for the movement ledger,
        # transfers and stock counts. Two branch managers working from the same
        # printed run-book will legitimately send the same Idempotency-Key, and that
        # collision is a coincidence, not a replay: Beşiktaş's threshold update must
        # never return Kadıköy's result and quietly configure nothing.
        UniqueConstraint(
            "store_id", "idempotency_key_hash", name="uq_threshold_update_store_idem"
        ),
        Index(
            "ix_threshold_update_store_ingredient_created",
            "store_id",
            "ingredient_id",
            "created_at",
        ),
    )
