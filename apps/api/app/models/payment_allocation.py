from sqlalchemy import (
    Column,
    Integer,
    BigInteger,
    ForeignKey,
    DateTime,
    Numeric,
    CheckConstraint,
    Index,
)
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from .base import Base


class PaymentAllocation(Base):
    """
    The portion of a settlement's gross amount applied to one specific order.

    A settlement collecting a whole table produces one allocation per order.
    Allocations are append-only and never edited; a reversal is a PaymentRefund
    row that references the allocation.
    """

    __tablename__ = "payment_allocations"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    settlement_id = Column(
        BigInteger, ForeignKey("payment_settlements.id"), nullable=False, index=True
    )
    order_id = Column(Integer, ForeignKey("orders.id"), nullable=False, index=True)
    amount = Column(Numeric(12, 2), nullable=False)

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    settlement = relationship("PaymentSettlement", back_populates="allocations")
    order = relationship("Order")
    refunds = relationship("PaymentRefund", back_populates="allocation")

    __table_args__ = (
        CheckConstraint("amount > 0", name="ck_allocation_amount_positive"),
        Index("ix_allocation_settlement_order", "settlement_id", "order_id"),
    )
