"""
Cashier shift — the daily reconciliation of a till against the payment ledger.

Why this table exists
---------------------
SweetOps already records every collection and refund in an append-only payment
ledger (see payment_settlement / payment_refund). That ledger is financially
correct, but it never answers the question a shop actually asks at the end of a
day:

    "Kasiyer vardiyaya 200 TL bozuk parayla başladı, gün boyu nakit tahsilat
     yaptı, birkaç iade oldu — gün sonunda kasada ne kadar nakit OLMALI, kasiyer
     ne SAYDI, ve arada fark var mı?"

A shift is a RECONCILIATION EVENT laid over the existing ledger. It never mutates
a settled payment, never mutates a refund, never touches inventory, and never
creates an accounting entry. It records:

  * the cash the cashier started the drawer with (opening_cash_amount),
  * a SNAPSHOT, taken at close time, of what the ledger says happened during the
    shift window (cash/card payments and refunds),
  * what the cashier physically COUNTED (counted_closing_cash_amount),
  * and the discrepancy between the two.

Attribution rule (documented, and matched exactly by scripts/reconcile_payments.py)
-----------------------------------------------------------------------------------
A shift belongs to ONE cashier at ONE store. Its totals are derived from the
ledger by ``store_id + cashier_user_id + time window`` where the window is
``opened_at <= t < closed_at``:

  * cash/card PAYMENTS  — settlements this cashier collected in the window,
                          classified by the settlement's own payment_method.
  * cash/card REFUNDS   — refunds of money THIS cashier collected (join through
                          the settlement) whose refund timestamp falls in the
                          window. Refunds are performed by a MANAGER/OWNER, not by
                          the cashier, so they are attributed by whose money was
                          reversed (settlement.cashier_user_id), NOT by who
                          pressed the refund button. That is the figure the
                          physical drawer actually loses.

Payments are NOT required to happen inside an open shift: the existing cashier
flow keeps working with or without one. A close simply summarises the ledger for
its window. Enforcing "no payment without an open shift" is a larger operational
policy and is deliberately out of scope for this branch.

One OPEN shift per (store, cashier)
-----------------------------------
A partial unique index (see the migration) allows at most one OPEN row per
(store_id, cashier_user_id). Two overlapping open shifts for the same cashier
would both claim the same windowed payments — a double count — so the second
open is refused (the service returns the existing open shift instead).

Immutability
------------
A CLOSED shift is a snapshot and is frozen: a trigger (see the migration) refuses
every UPDATE/DELETE once status is CLOSED, so a payment recorded AFTER the close
can never retroactively change what the shift reported, and a closed shift can
never be reopened. While OPEN, the opening snapshot (store, cashier, opened_at,
opening_cash_amount, opened idempotency hashes) is likewise frozen; the only
permitted transition is OPEN → CLOSED.

Idempotency
-----------
Opening and closing each require an Idempotency-Key; only SHA-256 hashes are
stored, never the raw key or payload. Opening uniqueness is store-scoped
(uq_cashier_shift_store_open_idem); closing uniqueness is inherently shift-scoped
(the close writes onto the shift's own row).
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

MONEY = Numeric(12, 2)

SHIFT_OPEN = "OPEN"
SHIFT_CLOSED = "CLOSED"
SHIFT_STATUSES = (SHIFT_OPEN, SHIFT_CLOSED)

_STATUS_SQL = ",".join(f"'{s}'" for s in SHIFT_STATUSES)

# The nine snapshot columns that are NULL while OPEN and NOT NULL once CLOSED.
# Kept in one place so the model, the consistency CHECK and the tests agree.
_CLOSED_SNAPSHOT_COLUMNS = (
    "closed_at",
    "counted_closing_cash_amount",
    "cash_payments_amount",
    "cash_refunds_amount",
    "expected_closing_cash_amount",
    "cash_discrepancy_amount",
    "card_payments_amount",
    "card_refunds_amount",
    "gross_payments_amount",
    "total_refunds_amount",
    "net_collected_amount",
)


class CashierShift(Base):
    """One cashier's till session at one store: opened, then closed with a count."""

    __tablename__ = "cashier_shifts"

    id = Column(BigInteger, primary_key=True, autoincrement=True)

    # Both come from the authenticated staff session, never from the request body.
    store_id = Column(Integer, ForeignKey("stores.id"), nullable=False, index=True)
    cashier_user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)

    status = Column(String(16), nullable=False, server_default=SHIFT_OPEN)

    opened_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    closed_at = Column(DateTime(timezone=True), nullable=True)

    # What the cashier started the drawer with. May be zero, never negative.
    opening_cash_amount = Column(MONEY, nullable=False)

    open_note = Column(String(500), nullable=True)
    close_note = Column(String(500), nullable=True)

    # ── Close snapshot (all NULL while OPEN, all set atomically at close) ────────
    counted_closing_cash_amount = Column(MONEY, nullable=True)

    cash_payments_amount = Column(MONEY, nullable=True)
    cash_refunds_amount = Column(MONEY, nullable=True)
    expected_closing_cash_amount = Column(MONEY, nullable=True)
    # counted - expected. INTENTIONALLY SIGNED: negative = eksik (short),
    # positive = fazla (over), zero = denk. Never constrained non-negative.
    cash_discrepancy_amount = Column(MONEY, nullable=True)

    card_payments_amount = Column(MONEY, nullable=True)
    card_refunds_amount = Column(MONEY, nullable=True)

    gross_payments_amount = Column(MONEY, nullable=True)
    total_refunds_amount = Column(MONEY, nullable=True)
    # gross - total refunds. May be negative in the rare case where refunds of a
    # PRIOR shift's payments land in this window; not constrained non-negative.
    net_collected_amount = Column(MONEY, nullable=True)

    # Idempotency: only hashes, never the raw Idempotency-Key or request body.
    opened_idempotency_key_hash = Column(String(64), nullable=False)
    opened_request_hash = Column(String(64), nullable=False)
    closed_idempotency_key_hash = Column(String(64), nullable=True)
    closed_request_hash = Column(String(64), nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    store = relationship("Store", foreign_keys=[store_id])
    cashier = relationship("User", foreign_keys=[cashier_user_id])

    # Consistency between status and the close snapshot: everything in
    # _CLOSED_SNAPSHOT_COLUMNS is NULL exactly when OPEN and NOT NULL exactly when
    # CLOSED. Written out so a half-populated snapshot is unrepresentable.
    _open_all_null = " AND ".join(f"{c} IS NULL" for c in _CLOSED_SNAPSHOT_COLUMNS)
    _closed_all_set = " AND ".join(f"{c} IS NOT NULL" for c in _CLOSED_SNAPSHOT_COLUMNS)

    __table_args__ = (
        CheckConstraint(f"status IN ({_STATUS_SQL})", name="ck_cashier_shift_status_domain"),
        CheckConstraint("opening_cash_amount >= 0", name="ck_cashier_shift_opening_nonneg"),
        # Status ⟺ close-snapshot nullability. Also carries rules 4-7 (closed_at &
        # counted null when OPEN, not null when CLOSED) because both are in the set.
        CheckConstraint(
            f"(status = '{SHIFT_OPEN}' AND {_open_all_null}) "
            f"OR (status = '{SHIFT_CLOSED}' AND {_closed_all_set})",
            name="ck_cashier_shift_status_snapshot",
        ),
        # The counted amount and every PURE-SUM total are non-negative: they are
        # sums of positive ledger amounts, so a negative here is corruption, not a
        # legitimate value. expected_closing / net_collected / discrepancy are
        # deliberately absent — they are signed nets (see column comments).
        CheckConstraint(
            "counted_closing_cash_amount IS NULL OR counted_closing_cash_amount >= 0",
            name="ck_cashier_shift_counted_nonneg",
        ),
        CheckConstraint(
            "cash_payments_amount IS NULL OR cash_payments_amount >= 0",
            name="ck_cashier_shift_cash_pay_nonneg",
        ),
        CheckConstraint(
            "cash_refunds_amount IS NULL OR cash_refunds_amount >= 0",
            name="ck_cashier_shift_cash_ref_nonneg",
        ),
        CheckConstraint(
            "card_payments_amount IS NULL OR card_payments_amount >= 0",
            name="ck_cashier_shift_card_pay_nonneg",
        ),
        CheckConstraint(
            "card_refunds_amount IS NULL OR card_refunds_amount >= 0",
            name="ck_cashier_shift_card_ref_nonneg",
        ),
        CheckConstraint(
            "gross_payments_amount IS NULL OR gross_payments_amount >= 0",
            name="ck_cashier_shift_gross_nonneg",
        ),
        CheckConstraint(
            "total_refunds_amount IS NULL OR total_refunds_amount >= 0",
            name="ck_cashier_shift_refunds_nonneg",
        ),
        # Gross includes CASH + CARD + any OTHER method, so it can never be less
        # than cash + card; likewise for total refunds. A snapshot that violated
        # this would have lost or invented money between the parts and the whole.
        CheckConstraint(
            "gross_payments_amount IS NULL "
            "OR gross_payments_amount >= cash_payments_amount + card_payments_amount",
            name="ck_cashier_shift_gross_ge_parts",
        ),
        CheckConstraint(
            "total_refunds_amount IS NULL "
            "OR total_refunds_amount >= cash_refunds_amount + card_refunds_amount",
            name="ck_cashier_shift_refunds_ge_parts",
        ),
        # The cashier BELONGS to the store whose till they are running. users has a
        # unique (store_id, id) exactly for composite FKs like this one; a member of
        # staff with no store assignment can never open a shift.
        ForeignKeyConstraint(
            ["store_id", "cashier_user_id"],
            ["users.store_id", "users.id"],
            name="fk_cashier_shift_actor_store",
        ),
        # Store-scoped opening idempotency, exactly like the payment ledger's.
        UniqueConstraint(
            "store_id",
            "opened_idempotency_key_hash",
            name="uq_cashier_shift_store_open_idem",
        ),
        Index("ix_cashier_shift_store_cashier", "store_id", "cashier_user_id"),
        Index("ix_cashier_shift_store_opened", "store_id", "opened_at"),
    )
