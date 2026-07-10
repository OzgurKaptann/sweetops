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
)
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from .base import Base


class PaymentSettlement(Base):
    """
    A single cashier collection action — the top of the append-only financial
    ledger. One settlement collects one gross amount, by one method, in one
    currency, and allocates it across one or more orders (see PaymentAllocation).

    A completed settlement row is never edited or deleted; a reversal is a
    separate append-only PaymentRefund row.

    Status is COMPLETED only. Cash/card entries are recorded after the
    real-world collection has already succeeded, so there is no pending/void
    lifecycle to model; a correction is always a new refund record, never a
    mutation of this row. The database enforces both the COMPLETED-only domain
    and full row immutability (see the payment migration).
    """

    __tablename__ = "payment_settlements"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    store_id = Column(Integer, ForeignKey("stores.id"), nullable=False, index=True)
    table_id = Column(Integer, ForeignKey("tables.id"), nullable=True, index=True)
    cashier_user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)

    payment_method = Column(String(16), nullable=False)   # CASH | CARD | OTHER
    currency = Column(String(3), nullable=False, server_default="TRY")
    gross_amount = Column(Numeric(12, 2), nullable=False)

    status = Column(String(16), nullable=False, server_default="COMPLETED")  # COMPLETED (only)
    note = Column(String(500), nullable=True)

    # Non-sensitive external terminal reference (never card data).
    terminal_reference = Column(String(64), nullable=True)

    # Idempotency: only SHA-256 hashes are stored, never the raw key/payload.
    idempotency_key_hash = Column(String(64), nullable=False)
    request_hash = Column(String(64), nullable=False)

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    completed_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    store = relationship("Store")
    table = relationship("Table")
    cashier = relationship("User")
    allocations = relationship(
        "PaymentAllocation",
        back_populates="settlement",
        order_by="PaymentAllocation.id",
    )
    refunds = relationship("PaymentRefund", back_populates="settlement")

    __table_args__ = (
        CheckConstraint("gross_amount > 0", name="ck_settlement_amount_positive"),
        CheckConstraint(
            "payment_method IN ('CASH','CARD','OTHER')",
            name="ck_settlement_method_domain",
        ),
        CheckConstraint(
            "status IN ('COMPLETED')",
            name="ck_settlement_status_domain",
        ),
        CheckConstraint("char_length(currency) BETWEEN 1 AND 3", name="ck_settlement_currency_len"),
        # A given idempotency key is unique per store — the same raw key from a
        # different store never collides, and a replay within the store maps to
        # exactly one settlement.
        Index(
            "uq_settlement_store_idem",
            "store_id",
            "idempotency_key_hash",
            unique=True,
        ),
    )
