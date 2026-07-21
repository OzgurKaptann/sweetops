"""
Order issue — a first-class, auditable record of something going wrong with an
order, and the controlled decision taken to resolve it.

Why this table exists
---------------------
SweetOps already records orders, an append-only payment/refund ledger, an
inventory lifecycle and cashier shift reconciliation. What it lacked was a
controlled, auditable way to handle the everyday operational reality of a waffle
shop:

    "Müşteri siparişi iptal etti. Ürün yanlış hazırlandı. Ürün eksik verildi.
     Sipariş ödendi ama iade gerekiyor. Sipariş kısmen iade edilecek."

Without a first-class issue workflow, staff would cancel orders, refund money and
adjust stock in disconnected, unexplainable ways. An order issue is a COORDINATION
record: it never bypasses the payment refund ledger, the inventory lifecycle, the
cashier shift snapshot or the audit log — it drives them through their existing,
safe primitives and ties the result to one explainable row.

Lifecycle
---------
An issue is CREATED (status OPEN) recording only the problem — creation moves no
money and touches no stock. It is later RESOLVED with exactly one resolution:

    NO_REFUND       the problem is acknowledged and closed; no money, no cancel.
    CANCEL_ONLY     the order is cancelled (reservation released if still merely
                    reserved; already-consumed stock is NOT restored). No refund.
    FULL_REFUND     the whole remaining refundable amount is refunded through the
                    existing payment refund ledger, and the order is cancelled.
    PARTIAL_REFUND  a specified amount (<= remaining refundable) is refunded. The
                    order is left active.

The refund ledger stays the single source of truth for refunded money: an issue
resolution CREATES payment_refunds rows (each stamped with this issue's id) and
links the primary one back through ``refund_id``. It never duplicates or restates
a refunded amount.

Immutability
------------
A resolved issue is frozen by a DB trigger (see the migration): DELETE is always
refused (issues are history), an already-resolved row can never be updated, and an
OPEN row may only transition OPEN → RESOLVED/VOIDED with its creation snapshot
carried over unchanged. There is no application-reachable bypass.

Idempotency
-----------
Creation and resolution each require an Idempotency-Key; only SHA-256 hashes are
stored, never the raw key or payload. Creation uniqueness is store-scoped
(uq_order_issue_store_create_idem); resolution uniqueness is inherently
issue-scoped (the resolve writes onto the issue's own row).
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
from sqlalchemy.sql import func

from .base import Base

MONEY = Numeric(12, 2)

# ── Domains (the English wire contract; never rendered raw to a screen) ──────────
ISSUE_TYPES = (
    "CUSTOMER_CANCELLED",
    "WRONG_ITEM",
    "MISSING_ITEM",
    "QUALITY_PROBLEM",
    "DUPLICATE_ORDER",
    "STAFF_ERROR",
    "OTHER",
)

ISSUE_STATUS_OPEN = "OPEN"
ISSUE_STATUS_RESOLVED = "RESOLVED"
ISSUE_STATUS_VOIDED = "VOIDED"
ISSUE_STATUSES = (ISSUE_STATUS_OPEN, ISSUE_STATUS_RESOLVED, ISSUE_STATUS_VOIDED)

RESOLUTION_NO_REFUND = "NO_REFUND"
RESOLUTION_FULL_REFUND = "FULL_REFUND"
RESOLUTION_PARTIAL_REFUND = "PARTIAL_REFUND"
RESOLUTION_CANCEL_ONLY = "CANCEL_ONLY"
RESOLUTION_TYPES = (
    RESOLUTION_NO_REFUND,
    RESOLUTION_FULL_REFUND,
    RESOLUTION_PARTIAL_REFUND,
    RESOLUTION_CANCEL_ONLY,
)
# The resolutions that create money movement (and therefore require a refund link).
REFUNDING_RESOLUTIONS = (RESOLUTION_FULL_REFUND, RESOLUTION_PARTIAL_REFUND)

_TYPE_SQL = ",".join(f"'{t}'" for t in ISSUE_TYPES)
_STATUS_SQL = ",".join(f"'{s}'" for s in ISSUE_STATUSES)
_RESOLUTION_SQL = ",".join(f"'{r}'" for r in RESOLUTION_TYPES)

# Resolution-snapshot columns: NULL while OPEN, NOT NULL once RESOLVED/VOIDED.
_RESOLVED_COLUMNS = (
    "resolution_type",
    "approved_refund_amount",
    "resolved_by_user_id",
    "resolved_at",
    "resolved_idempotency_key_hash",
    "resolved_request_hash",
)
_OPEN_ALL_NULL = " AND ".join(f"{c} IS NULL" for c in _RESOLVED_COLUMNS)
_RESOLVED_ALL_SET = " AND ".join(f"{c} IS NOT NULL" for c in _RESOLVED_COLUMNS)


class OrderIssue(Base):
    """One problem raised against one order, and the controlled decision taken."""

    __tablename__ = "order_issues"

    id = Column(BigInteger, primary_key=True, autoincrement=True)

    # Both derived from the authenticated staff session, never the request body.
    store_id = Column(Integer, ForeignKey("stores.id"), nullable=False, index=True)
    order_id = Column(Integer, ForeignKey("orders.id"), nullable=False, index=True)

    issue_type = Column(String(32), nullable=False)
    status = Column(String(16), nullable=False, server_default=ISSUE_STATUS_OPEN)

    # ── Resolution snapshot (all NULL while OPEN, all set atomically at resolve) ──
    resolution_type = Column(String(16), nullable=True)
    requested_refund_amount = Column(MONEY, nullable=True)  # what was asked at create
    approved_refund_amount = Column(MONEY, nullable=True)   # what was granted at resolve

    # Primary link to the refund ledger. When a resolution creates several refund
    # rows (an order paid across multiple settlements), every row carries this
    # issue's id in payment_refunds.order_issue_id; refund_id points at the first.
    refund_id = Column(BigInteger, ForeignKey("payment_refunds.id"), nullable=True)

    reason = Column(String(500), nullable=False)
    note = Column(String(500), nullable=True)

    created_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    resolved_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    resolved_at = Column(DateTime(timezone=True), nullable=True)

    # Idempotency: only hashes, never the raw Idempotency-Key or request body.
    created_idempotency_key_hash = Column(String(64), nullable=False)
    created_request_hash = Column(String(64), nullable=False)
    resolved_idempotency_key_hash = Column(String(64), nullable=True)
    resolved_request_hash = Column(String(64), nullable=True)

    # No ORM relationships: this table has both single-column and composite FKs to
    # stores/orders/users/payment_refunds, which SQLAlchemy cannot disambiguate
    # automatically, and the service navigates by explicit db.get() instead.

    __table_args__ = (
        # Domains.
        CheckConstraint(f"issue_type IN ({_TYPE_SQL})", name="ck_order_issue_type_domain"),
        CheckConstraint(f"status IN ({_STATUS_SQL})", name="ck_order_issue_status_domain"),
        CheckConstraint(
            f"resolution_type IS NULL OR resolution_type IN ({_RESOLUTION_SQL})",
            name="ck_order_issue_resolution_domain",
        ),
        # Money is never negative.
        CheckConstraint(
            "requested_refund_amount IS NULL OR requested_refund_amount >= 0",
            name="ck_order_issue_requested_nonneg",
        ),
        CheckConstraint(
            "approved_refund_amount IS NULL OR approved_refund_amount >= 0",
            name="ck_order_issue_approved_nonneg",
        ),
        # Status ⟺ resolution-snapshot nullability. Carries "resolved_at &
        # resolved_by required when RESOLVED" (rules 10-11): both are in the set.
        CheckConstraint(
            f"(status = '{ISSUE_STATUS_OPEN}' AND {_OPEN_ALL_NULL}) "
            f"OR (status IN ('{ISSUE_STATUS_RESOLVED}','{ISSUE_STATUS_VOIDED}') "
            f"AND {_RESOLVED_ALL_SET})",
            name="ck_order_issue_status_snapshot",
        ),
        # A refunding resolution with a positive approved amount MUST carry a link.
        CheckConstraint(
            "resolution_type NOT IN ('FULL_REFUND','PARTIAL_REFUND') "
            "OR approved_refund_amount IS NULL OR approved_refund_amount = 0 "
            "OR refund_id IS NOT NULL",
            name="ck_order_issue_refund_required",
        ),
        # ...and a NON-refunding resolution must NOT carry a refund link.
        CheckConstraint(
            "refund_id IS NULL OR resolution_type IN ('FULL_REFUND','PARTIAL_REFUND')",
            name="ck_order_issue_refund_only_when_refunding",
        ),
        # The issue BELONGS to its order's store: (store_id, order_id) →
        # orders(store_id, id). A Store-A session can never raise an issue on a
        # Store-B order even if the service were wrong.
        ForeignKeyConstraint(
            ["store_id", "order_id"],
            ["orders.store_id", "orders.id"],
            name="fk_order_issue_order_store",
        ),
        # The creator belongs to the store: (store_id, created_by_user_id) →
        # users(store_id, id).
        ForeignKeyConstraint(
            ["store_id", "created_by_user_id"],
            ["users.store_id", "users.id"],
            name="fk_order_issue_creator_store",
        ),
        # The resolver, when present, belongs to the store too. NULL skips the
        # check (MATCH SIMPLE), so an OPEN issue with no resolver is fine.
        ForeignKeyConstraint(
            ["store_id", "resolved_by_user_id"],
            ["users.store_id", "users.id"],
            name="fk_order_issue_resolver_store",
        ),
        # The linked refund, when present, belongs to the SAME store AND order:
        # (store_id, order_id, refund_id) → payment_refunds(store_id, order_id, id).
        ForeignKeyConstraint(
            ["store_id", "order_id", "refund_id"],
            ["payment_refunds.store_id", "payment_refunds.order_id", "payment_refunds.id"],
            name="fk_order_issue_refund_context",
        ),
        # Store-scoped creation idempotency, exactly like the payment ledger's.
        UniqueConstraint(
            "store_id",
            "created_idempotency_key_hash",
            name="uq_order_issue_store_create_idem",
        ),
        Index("ix_order_issue_store_status", "store_id", "status"),
        Index("ix_order_issue_store_created", "store_id", "created_at"),
    )
