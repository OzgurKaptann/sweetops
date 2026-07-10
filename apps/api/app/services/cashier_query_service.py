"""
Cashier read-side queries — open tables, order search, table bill, order
detail, and recent transactions. Every query is scoped to the authenticated
store; a cross-store id yields a non-disclosing 404 at the router.

An order is "open" (outstanding) when it is not CANCELLED and its net paid
amount is below its persisted total. Fully paid and cancelled orders are never
shown as outstanding and never counted in a payable balance.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.order import Order
from app.models.payment_refund import PaymentRefund
from app.models.payment_settlement import PaymentSettlement
from app.models.table import Table
from app.models.user import User
from app.schemas.payment import (
    OpenTableSummary,
    OpenTablesResponse,
    OrderBillLine,
    OrderDetailResponse,
    RecentTransaction,
    RecentTransactionsResponse,
    TableBillResponse,
)
from app.services.payment_service import (
    DEFAULT_CURRENCY,
    is_payable,
    net_paid,
    order_code,
    outstanding,
    q2,
)

# Preparation states that still count as an active/open order for the cashier.
OPEN_PREP_STATES = ("NEW", "IN_PREP", "READY", "DELIVERED")


def _bill_line(order: Order) -> OrderBillLine:
    total = q2(order.total_amount)
    paid = q2(order.paid_amount)
    refunded = q2(order.refunded_amount)
    return OrderBillLine(
        order_id=order.id,
        order_code=order_code(order.id),
        created_at=order.created_at,
        preparation_status=order.status,
        payment_status=order.payment_status,
        refund_status=order.refund_status,
        order_total=total,
        paid_amount=paid,
        refunded_amount=refunded,
        net_paid=net_paid(order),
        remaining_amount=outstanding(order),
        payable=is_payable(order),
    )


def list_open_tables(db: Session, store_id: int) -> OpenTablesResponse:
    """Tables of this store that have at least one order with a remaining balance."""
    orders = (
        db.query(Order)
        .filter(
            Order.store_id == store_id,
            Order.status != "CANCELLED",
            Order.table_id.isnot(None),
        )
        .all()
    )

    buckets: dict[int, list[Order]] = {}
    for o in orders:
        if outstanding(o) > 0:
            buckets.setdefault(o.table_id, []).append(o)

    if not buckets:
        return OpenTablesResponse(tables=[])

    tables = {
        t.id: t
        for t in db.query(Table).filter(Table.id.in_(buckets.keys())).all()
    }

    summaries: list[OpenTableSummary] = []
    for table_id, group in buckets.items():
        gross = sum((q2(o.total_amount) for o in group), Decimal("0.00"))
        paid = sum((net_paid(o) for o in group), Decimal("0.00"))
        remaining = sum((outstanding(o) for o in group), Decimal("0.00"))
        oldest = min(o.created_at for o in group)
        tbl = tables.get(table_id)
        summaries.append(OpenTableSummary(
            table_id=table_id,
            table_number=tbl.table_number if tbl else None,
            open_order_count=len(group),
            gross_amount=q2(gross),
            paid_amount=q2(paid),
            remaining_amount=q2(remaining),
            oldest_order_at=oldest,
        ))

    summaries.sort(key=lambda s: (s.oldest_order_at is None, s.oldest_order_at))
    return OpenTablesResponse(tables=summaries)


def get_order_detail(db: Session, store_id: int, order_id: int) -> Optional[OrderDetailResponse]:
    order = db.get(Order, order_id)
    if order is None or order.store_id != store_id:
        return None
    line = _bill_line(order)
    return OrderDetailResponse(
        store_id=order.store_id,
        table_id=order.table_id,
        **line.model_dump(),
    )


def search_order(db: Session, store_id: int, order_id: int) -> Optional[OrderDetailResponse]:
    """Search by the numeric id parsed from a staff-facing order code."""
    return get_order_detail(db, store_id, order_id)


def get_table_bill(db: Session, store_id: int, table_id: int) -> Optional[TableBillResponse]:
    table = db.get(Table, table_id)
    if table is None or table.store_id != store_id:
        return None

    orders = (
        db.query(Order)
        .filter(
            Order.store_id == store_id,
            Order.table_id == table_id,
            Order.status != "CANCELLED",
        )
        .order_by(Order.created_at)
        .all()
    )

    lines = [_bill_line(o) for o in orders]
    gross = sum((l.order_total for l in lines), Decimal("0.00"))
    paid = sum((l.net_paid for l in lines), Decimal("0.00"))
    remaining = sum((l.remaining_amount for l in lines), Decimal("0.00"))

    return TableBillResponse(
        table_id=table_id,
        table_number=table.table_number,
        currency=DEFAULT_CURRENCY,
        gross_amount=q2(gross),
        paid_amount=q2(paid),
        remaining_amount=q2(remaining),
        orders=lines,
    )


def get_settlement_receipt_row(db: Session, store_id: int, settlement_id: int) -> Optional[PaymentSettlement]:
    s = db.get(PaymentSettlement, settlement_id)
    if s is None or s.store_id != store_id:
        return None
    return s


def recent_transactions(db: Session, store_id: int, limit: int = 20) -> RecentTransactionsResponse:
    """Most recent collections and refunds for the authenticated store."""
    settlements = (
        db.query(PaymentSettlement)
        .filter(PaymentSettlement.store_id == store_id)
        .order_by(PaymentSettlement.completed_at.desc())
        .limit(limit)
        .all()
    )
    refunds = (
        db.query(PaymentRefund)
        .filter(PaymentRefund.store_id == store_id)
        .order_by(PaymentRefund.created_at.desc())
        .limit(limit)
        .all()
    )

    user_ids = {s.cashier_user_id for s in settlements} | {r.refunded_by_user_id for r in refunds}
    users = {u.id: u for u in db.query(User).filter(User.id.in_(user_ids)).all()} if user_ids else {}

    def display(uid: int) -> str:
        u = users.get(uid)
        return u.username if u else f"user:{uid}"

    txns: list[RecentTransaction] = []
    for s in settlements:
        txns.append(RecentTransaction(
            kind="COLLECTION",
            settlement_id=s.id,
            table_id=s.table_id,
            payment_method=s.payment_method,
            currency=s.currency,
            amount=q2(s.gross_amount),
            actor_display=display(s.cashier_user_id),
            at=s.completed_at,
        ))
    for r in refunds:
        txns.append(RecentTransaction(
            kind="REFUND",
            settlement_id=r.settlement_id,
            refund_id=r.id,
            table_id=None,
            payment_method=None,
            currency=r.currency,
            amount=q2(r.amount),
            actor_display=display(r.refunded_by_user_id),
            at=r.created_at,
        ))

    txns.sort(key=lambda t: t.at, reverse=True)
    return RecentTransactionsResponse(transactions=txns[:limit])
