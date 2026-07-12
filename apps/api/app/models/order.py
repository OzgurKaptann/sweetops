from sqlalchemy import (
    Column,
    Integer,
    String,
    ForeignKey,
    DateTime,
    Numeric,
    CheckConstraint,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from .base import Base


class Order(Base):
    __tablename__ = "orders"

    id = Column(Integer, primary_key=True, index=True)
    store_id = Column(Integer, ForeignKey("stores.id"), nullable=False)
    table_id = Column(Integer, ForeignKey("tables.id"), nullable=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    # Immutable order-value snapshot, captured at checkout from persisted
    # order-item pricing. Payment always settles against this — never a total
    # recalculated from current menu prices, and never a client-supplied total.
    total_amount = Column(Numeric(10, 2), default=0)
    status = Column(String, default="NEW", index=True, nullable=False)

    # ── Payment summary (fast-query mirror of the ledger) ────────────────────
    # The append-only settlement/allocation/refund ledger is the source of
    # truth. These fields are a denormalised summary maintained inside the same
    # transaction as the ledger writes for cheap cashier/list queries.
    payment_status = Column(
        String(20), nullable=False, server_default="UNPAID", index=True
    )  # UNPAID | PARTIALLY_PAID | PAID
    refund_status = Column(
        String(20), nullable=False, server_default="NONE"
    )  # NONE | PARTIALLY_REFUNDED | REFUNDED
    paid_amount = Column(Numeric(12, 2), nullable=False, server_default="0")
    refunded_amount = Column(Numeric(12, 2), nullable=False, server_default="0")

    # Idempotency: client-generated UUID, prevents double-submit
    idempotency_key = Column(String(64), unique=True, nullable=True, index=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    __table_args__ = (
        CheckConstraint("paid_amount >= 0", name="ck_order_paid_nonneg"),
        CheckConstraint("refunded_amount >= 0", name="ck_order_refunded_nonneg"),
        CheckConstraint(
            "refunded_amount <= paid_amount", name="ck_order_refund_le_paid"
        ),
        CheckConstraint(
            "payment_status IN ('UNPAID','PARTIALLY_PAID','PAID')",
            name="ck_order_payment_status_domain",
        ),
        CheckConstraint(
            "refund_status IN ('NONE','PARTIALLY_REFUNDED','REFUNDED')",
            name="ck_order_refund_status_domain",
        ),
        # Redundant given the primary key, but PostgreSQL requires a unique
        # constraint on exactly the referenced pair. It is what lets inventory
        # rows carry a composite FK to (store_id, order_id) and so be structurally
        # unable to attach an order to another store's stock.
        UniqueConstraint("store_id", "id", name="uq_orders_store_id"),
    )

    store = relationship("Store", back_populates="orders")
    table = relationship("Table", back_populates="orders")
    items = relationship("OrderItem", back_populates="order")
    status_events = relationship("OrderStatusEvent", back_populates="order",
                                  order_by="OrderStatusEvent.created_at")
