"""
Physical stock count — the shelf is counted, and the system is made to agree.

Why this is not a manual adjustment
-----------------------------------
A MANUAL_ADJUSTMENT is a one-off correction: someone knows a number is wrong and
types the difference. It records the CORRECTION and nothing else.

A physical count is a different act, and a manager doing one needs to be able to
answer a different question afterwards:

    "On the 14th, Kadıköy counted the chocolate freezer. The system said
     4.200 kg. The shelf held 3.850 kg. We were 0.350 kg short."

A signed ``-0.350`` in the ledger cannot say that. It records the difference and
throws away the two numbers the difference was derived from — so nobody can ever
check the arithmetic, nobody can tell a count from a guess, and a shelf that was
counted and found CORRECT leaves no trace at all (its delta is zero, and a
zero-delta adjustment is rejected as a no-op). "We counted it and it was right"
is a fact an owner is entitled to have on the record; under manual adjustment it
is indistinguishable from never having looked.

So a count is a row HERE. It stores the three numbers as a set:

    counted_quantity          what was physically on the shelf
    system_on_hand_quantity   what the system believed, AT THE MOMENT OF COUNTING
    system_reserved_quantity  what was already promised to accepted orders
    delta_quantity            counted - system_on_hand   (GENERATED — see below)

and the ledger movement, if there is one, points back at it.

The count is the event; the movement is its stock effect; the database refuses to
let the two disagree.

delta_quantity is GENERATED
---------------------------
``delta_quantity`` is GENERATED ALWAYS ... STORED from the other two columns, not
written by the application. A count whose stated delta does not match its own two
numbers would be a lie that the whole table exists to prevent, so it is not a row
that can be stored.

Zero-delta counts are RECORDED, and carry no movement
-----------------------------------------------------
When the shelf agrees with the system, the count row is written and NO movement is
written. That is the point: nothing physical happened, so nothing belongs in the
physical ledger — a zero-delta movement would be a ledger row that moves no stock,
which is noise in the one record an auditor reads. But the COUNT still happened,
and it is still evidence, so it is still stored.

The deferred pairing trigger (see the migration) enforces exactly this, both ways:

    delta <> 0  ⟹  exactly ONE STOCK_COUNT_ADJUSTMENT movement, matching this
                   count's store, ingredient, sign and magnitude
    delta  = 0  ⟹  exactly ZERO movements

A non-zero count with no movement is stock that was corrected on paper and never
on the shelf, and it cannot be committed.

Counting below reserved is refused
----------------------------------
``ck_stock_count_counted_ge_reserved``: counted_quantity >= system_reserved_quantity.

If a manager counts 3 kg of chocolate while 5 kg is already promised to accepted
orders, the honest reading is not "the system was wrong by 2 kg" — it is "this
shop has sold 2 kg of chocolate it does not have". Silently writing on-hand down
to 3 would break ck_stock_reserved_le_on_hand anyway, and quietly breaking a
promise made to a waiting customer is not a stock correction. That is an
operational incident: cancel or re-source the orders first, THEN count. The
constraint says so, and the service returns a Turkish message that says so.

Status
------
One status, APPLIED, as a CHECK constraint rather than a workflow. The count row
and its movement post in ONE transaction, so a count is never draft, never
pending, never half-applied. DRAFT/SUBMITTED/APPROVED would be a mutable column
with no state machine behind it — a lie inviting one to be written later. When a
real approval flow is built, the domain widens then; see
docs/PHYSICAL_STOCK_COUNT_WORKFLOW.md § Deferred.

Immutable
---------
UPDATE and DELETE are refused by a trigger, exactly as for the ledger. A count
that was got wrong is not edited — it is superseded by counting again, which is
what a manager would physically have to do anyway.
"""
from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    Computed,
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

# The only status a count can hold. The row and its movement post atomically, so
# there is no intermediate state to represent.
STOCK_COUNT_APPLIED = "APPLIED"
STOCK_COUNT_STATUSES = (STOCK_COUNT_APPLIED,)

_STATUS_SQL = ",".join(f"'{s}'" for s in STOCK_COUNT_STATUSES)


class InventoryStockCount(Base):
    """One physical count of one ingredient, in one branch, at one moment."""

    __tablename__ = "inventory_stock_counts"

    id = Column(BigInteger, primary_key=True, autoincrement=True)

    # Derived from the authenticated staff session, never from the request body.
    store_id = Column(Integer, ForeignKey("stores.id"), nullable=False, index=True)
    ingredient_id = Column(Integer, ForeignKey("ingredients.id"), nullable=False, index=True)

    # What the manager physically counted.
    counted_quantity = Column(QTY, nullable=False)

    # What the system believed at the instant the count was applied — captured
    # under the stock row's lock, so it is the figure the delta was actually
    # computed against and not a value that has since moved on.
    system_on_hand_quantity = Column(QTY, nullable=False)
    system_reserved_quantity = Column(QTY, nullable=False)

    # GENERATED by PostgreSQL. The application cannot write it, so a count cannot
    # claim a delta its own two numbers do not support.
    delta_quantity = Column(
        QTY,
        Computed("counted_quantity - system_on_hand_quantity", persisted=True),
        nullable=False,
    )

    unit = Column(String(10), nullable=False)

    # Why the shelf was counted. Mandatory: an unexplained correction to physical
    # stock is indistinguishable from theft, and that is as true of a count as it
    # is of a manual adjustment.
    reason = Column(String(500), nullable=False)
    note = Column(String(500), nullable=True)

    status = Column(String(20), nullable=False, server_default=STOCK_COUNT_APPLIED)

    counted_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)

    # Only hashes, never the raw Idempotency-Key and never the raw request body.
    idempotency_key_hash = Column(String(64), nullable=False)
    request_hash = Column(String(64), nullable=False)

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    applied_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    # Explicit foreign_keys: counted_by_user_id has a second, composite path to
    # users, so the ORM cannot infer which columns to join on.
    store = relationship("Store", foreign_keys=[store_id])
    ingredient = relationship("Ingredient", foreign_keys=[ingredient_id])
    counted_by = relationship("User", foreign_keys=[counted_by_user_id])

    __table_args__ = (
        # A shelf cannot hold a negative amount of anything.
        CheckConstraint("counted_quantity >= 0", name="ck_stock_count_counted_nonneg"),
        CheckConstraint(
            "system_on_hand_quantity >= 0", name="ck_stock_count_system_on_hand_nonneg"
        ),
        CheckConstraint(
            "system_reserved_quantity >= 0", name="ck_stock_count_system_reserved_nonneg"
        ),
        # The safety rule, in the database and not merely in the service: a count
        # may not write on-hand below what accepted orders are already promised.
        # See the module docstring — that case is an incident, not a count.
        CheckConstraint(
            "counted_quantity >= system_reserved_quantity",
            name="ck_stock_count_counted_ge_reserved",
        ),
        CheckConstraint(
            f"status IN ({_STATUS_SQL})", name="ck_stock_count_status_domain"
        ),
        CheckConstraint(
            "char_length(btrim(reason)) > 0", name="ck_stock_count_reason_present"
        ),
        # The counter BELONGS to the store whose shelf they counted. users.store_id
        # is nullable, so a member of staff with no store assignment can never
        # count — which is the correct answer, not an accident. A Kadıköy manager
        # counting Beşiktaş's freezer is unrepresentable, not merely forbidden.
        ForeignKeyConstraint(
            ["store_id", "counted_by_user_id"],
            ["users.store_id", "users.id"],
            name="fk_stock_count_actor_store",
        ),
        # The count is OF a stock row that exists in this branch. A store that has
        # never stocked an ingredient has no shelf to count, and is not silently
        # given one.
        ForeignKeyConstraint(
            ["store_id", "ingredient_id"],
            ["ingredient_stock.store_id", "ingredient_stock.ingredient_id"],
            name="fk_stock_count_stock_store",
        ),
        # Idempotency is scoped to the STORE, exactly as for the movement ledger
        # and for transfers: two branch managers working from the same printed
        # count sheet will legitimately send the same Idempotency-Key, and that
        # collision is a coincidence, not a replay. Beşiktaş's count must never
        # return Kadıköy's result.
        UniqueConstraint(
            "store_id", "idempotency_key_hash", name="uq_stock_count_store_idem"
        ),
        # FK target for the movement's composite key, which pins the movement to
        # this count's store AND ingredient. Redundant against the primary key,
        # but PostgreSQL requires a unique constraint on exactly the referenced
        # tuple before it will accept the foreign key.
        UniqueConstraint(
            "id", "store_id", "ingredient_id", name="uq_stock_count_movement_leg"
        ),
        Index("ix_stock_count_store_created", "store_id", "created_at"),
        Index(
            "ix_stock_count_store_ingredient_created",
            "store_id",
            "ingredient_id",
            "created_at",
        ),
    )
