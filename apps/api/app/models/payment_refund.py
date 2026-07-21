from sqlalchemy import (
    Column,
    Integer,
    BigInteger,
    String,
    ForeignKey,
    DateTime,
    Numeric,
    CheckConstraint,
    Index,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from .base import Base


class PaymentRefund(Base):
    """
    An append-only reversal of previously-collected money. Each refund
    references the original settlement and allocation (the collected money it
    reverses) and the order it belongs to. Refunds are never edited or deleted;
    the refundable balance of an allocation is its amount minus the sum of its
    refunds.

    SweetOps records the operational refund AFTER the real-world cash/card
    reversal has been performed — it does not talk to a card gateway.
    """

    __tablename__ = "payment_refunds"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    store_id = Column(Integer, ForeignKey("stores.id"), nullable=False, index=True)
    settlement_id = Column(
        BigInteger, ForeignKey("payment_settlements.id"), nullable=False, index=True
    )
    allocation_id = Column(
        BigInteger, ForeignKey("payment_allocations.id"), nullable=False, index=True
    )
    order_id = Column(Integer, ForeignKey("orders.id"), nullable=False, index=True)

    amount = Column(Numeric(12, 2), nullable=False)
    currency = Column(String(3), nullable=False, server_default="TRY")
    reason = Column(String(500), nullable=False)

    refunded_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)

    # Set when this refund was created by resolving an order issue. The refund
    # ledger stays the source of truth for refunded money; this is only the link
    # back to the operational decision that caused it. NULL for an ordinary
    # per-allocation refund taken directly at the till. A resolution that spans
    # several allocations stamps every refund row it creates with the same id.
    order_issue_id = Column(BigInteger, ForeignKey("order_issues.id"), nullable=True, index=True)

    # Idempotency: only SHA-256 hashes are stored, never the raw key/payload.
    idempotency_key_hash = Column(String(64), nullable=False)
    request_hash = Column(String(64), nullable=False)

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    store = relationship("Store")
    settlement = relationship("PaymentSettlement", back_populates="refunds")
    allocation = relationship("PaymentAllocation", back_populates="refunds")
    order = relationship("Order")
    refunded_by = relationship("User")

    __table_args__ = (
        CheckConstraint("amount > 0", name="ck_refund_amount_positive"),
        CheckConstraint("char_length(reason) > 0", name="ck_refund_reason_present"),
        Index(
            "uq_refund_store_idem",
            "store_id",
            "idempotency_key_hash",
            unique=True,
        ),
        # Lets an order issue carry a composite FK to (store_id, order_id, id), so a
        # linked refund is structurally guaranteed to belong to the same store AND
        # the same order as the issue. Redundant against the primary key, but
        # PostgreSQL requires a unique constraint on exactly the referenced tuple.
        UniqueConstraint(
            "store_id", "order_id", "id", name="uq_refund_store_order_id"
        ),
    )
