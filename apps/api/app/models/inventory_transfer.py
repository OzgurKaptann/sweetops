"""
Store-to-store inventory transfer — ONE business event, TWO ledger movements.

Why this table exists at all
---------------------------
A branch transfer *could* be typed into SweetOps as two manual adjustments: -2 kg
of chocolate in Kadıköy, +2 kg in Beşiktaş. That is exactly what this table
exists to make impossible, because two manual adjustments are two unrelated
events and the chocolate is one:

  * nothing links them, so nothing can prove the 2 kg that left Kadıköy is the
    2 kg that arrived in Beşiktaş — reconciliation sees a shortage and a surplus;
  * one can succeed while the other fails, and stock simply evaporates;
  * the outbound leg looks like WASTE and the inbound leg looks like a
    PURCHASE_RECEIPT, so the owner's waste report accuses a branch of throwing
    away chocolate it actually shipped, and the purchasing report double-counts
    stock that was never bought;
  * consumption velocity — the number reorder decisions are made from — is
    computed from physical outflow, and a transfer is not consumption.

So a transfer is a row HERE, and the two ledger movements point back at it. The
transfer is the event; the movements are its two halves; the database refuses to
let the halves disagree with the event or with each other.

The pairing invariant
---------------------
Exactly one TRANSFER_OUT (in the source store) and exactly one TRANSFER_IN (in
the destination store), for this transfer's ingredient and this transfer's
quantity. Nothing less is a transfer — a lone TRANSFER_OUT is stock that has
vanished. It is enforced three ways, none of which the application can talk its
way out of:

  1. Composite foreign keys (see IngredientStockMovement) tie each movement's
     (transfer, store, ingredient) triple to the correct SIDE of this row. A
     TRANSFER_OUT booked in the wrong store is not rejected by a code review —
     it is unrepresentable.
  2. A partial unique index on (transfer_id, movement_type) permits at most one
     movement of each direction per transfer.
  3. A DEFERRED constraint trigger re-checks, at COMMIT, that both halves exist
     with the right signs and magnitude. This is the check that makes a
     one-sided transfer impossible rather than merely unlikely.

Who may do it
-------------
``initiated_by_user_id`` is bound to ``source_store_id`` by a composite foreign
key: a member of staff can only ship stock OUT of the store they actually belong
to. A Store A manager naming Store B as the source in a request body does not get
a permission error — the row does not exist that would let it happen.

Status
------
There is exactly one status, COMPLETED, and it is a CHECK constraint rather than
a workflow. Both legs are posted inside one database transaction, so a transfer
is never half-done, never in transit, and never pending approval. A mutable
status column with no state machine behind it would be a lie that invites one to
be written later. When an approval or in-transit flow is genuinely built, the
status domain widens then — see docs/INVENTORY_TRANSFER_WORKFLOW.md § Limitations.
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

# The only status a transfer can hold. Both legs post atomically, so there is no
# intermediate state to represent.
TRANSFER_COMPLETED = "COMPLETED"
TRANSFER_STATUSES = (TRANSFER_COMPLETED,)

_STATUS_SQL = ",".join(f"'{s}'" for s in TRANSFER_STATUSES)


class InventoryTransfer(Base):
    """One store-to-store movement of one ingredient, as a single business event."""

    __tablename__ = "inventory_transfers"

    id = Column(BigInteger, primary_key=True, autoincrement=True)

    # Derived from the authenticated staff session, never from the request body.
    source_store_id = Column(Integer, ForeignKey("stores.id"), nullable=False, index=True)
    destination_store_id = Column(
        Integer, ForeignKey("stores.id"), nullable=False, index=True
    )
    ingredient_id = Column(Integer, ForeignKey("ingredients.id"), nullable=False, index=True)

    quantity = Column(QTY, nullable=False)
    unit = Column(String(10), nullable=False)

    status = Column(String(20), nullable=False, server_default=TRANSFER_COMPLETED)

    # Why the stock moved. Mandatory: an unexplained shipment of stock between
    # branches is indistinguishable from stock walking out of the door.
    reason = Column(String(500), nullable=False)
    note = Column(String(500), nullable=True)

    initiated_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)

    # Only hashes, never the raw Idempotency-Key and never the raw request body.
    idempotency_key_hash = Column(String(64), nullable=False)
    request_hash = Column(String(64), nullable=False)

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    completed_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    # Explicit foreign_keys everywhere: source and destination are two FKs to the
    # same table, and initiated_by_user_id has a second, composite path to users.
    source_store = relationship("Store", foreign_keys=[source_store_id])
    destination_store = relationship("Store", foreign_keys=[destination_store_id])
    ingredient = relationship("Ingredient", foreign_keys=[ingredient_id])
    initiated_by = relationship("User", foreign_keys=[initiated_by_user_id])

    __table_args__ = (
        CheckConstraint("quantity > 0", name="ck_transfer_quantity_positive"),
        # Shipping stock to yourself is not a transfer, it is a no-op that would
        # pollute the ledger with a cancelling pair of movements.
        CheckConstraint(
            "source_store_id <> destination_store_id",
            name="ck_transfer_stores_differ",
        ),
        CheckConstraint(f"status IN ({_STATUS_SQL})", name="ck_transfer_status_domain"),
        CheckConstraint(
            "char_length(btrim(reason)) > 0", name="ck_transfer_reason_present"
        ),
        # The initiator must BELONG to the source store — not merely have claimed
        # it. users.store_id is nullable, so an unassigned member of staff can
        # never initiate a transfer, which is the correct answer.
        ForeignKeyConstraint(
            ["source_store_id", "initiated_by_user_id"],
            ["users.store_id", "users.id"],
            name="fk_transfer_actor_source_store",
        ),
        # Both sides must already have a stock row for the ingredient. The source
        # obviously must (it is shipping the stuff); the destination row is
        # materialised at zero by the service before the transfer is written, so
        # a branch can legitimately receive an ingredient it has never held.
        ForeignKeyConstraint(
            ["source_store_id", "ingredient_id"],
            ["ingredient_stock.store_id", "ingredient_stock.ingredient_id"],
            name="fk_transfer_source_stock",
        ),
        ForeignKeyConstraint(
            ["destination_store_id", "ingredient_id"],
            ["ingredient_stock.store_id", "ingredient_stock.ingredient_id"],
            name="fk_transfer_destination_stock",
        ),
        # Idempotency is scoped to the SOURCE store, for the same reason the
        # movement ledger's is scoped to the store: two branch managers working
        # from the same run-book will legitimately send the same Idempotency-Key,
        # and that collision is a coincidence, not a replay. Kadıköy's transfer
        # must never return Beşiktaş's result.
        UniqueConstraint(
            "source_store_id",
            "idempotency_key_hash",
            name="uq_transfer_source_idem",
        ),
        # FK targets for the movement ledger's composite keys. Each pins a
        # movement to the correct SIDE of the transfer: the OUT leg to the
        # source store, the IN leg to the destination store, both to the
        # transfer's own ingredient. Redundant against the primary key, but
        # PostgreSQL requires a unique constraint on exactly the referenced tuple.
        UniqueConstraint(
            "id", "source_store_id", "ingredient_id", name="uq_transfer_source_leg"
        ),
        UniqueConstraint(
            "id",
            "destination_store_id",
            "ingredient_id",
            name="uq_transfer_destination_leg",
        ),
        Index("ix_transfer_source_created", "source_store_id", "created_at"),
        Index("ix_transfer_destination_created", "destination_store_id", "created_at"),
    )
