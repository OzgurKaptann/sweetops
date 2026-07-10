"""
Payment analytics — trustworthy financial measures that never conflate ORDERED
revenue with COLLECTED cash.

Definitions (all store-scoped)
------------------------------
  gross_order_value    = Σ orders.total_amount WHERE status <> 'CANCELLED'
  collected_amount     = Σ payment_allocations.amount  (COMPLETED settlements)
  refunded_amount      = Σ payment_refunds.amount
  net_collected_amount = collected_amount − refunded_amount
  outstanding_amount   = gross_order_value − net_collected_amount

Cancelled orders are excluded from gross_order_value; their allocations/refunds
would already be blocked (an order cannot be cancelled while money is held), so
they never contribute to collected/net either. The existing owner "revenue"
KPI (gross ordered value) is left untouched — this is an additive endpoint.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.schemas.payment import PaymentSummaryResponse
from app.services.payment_service import DEFAULT_CURRENCY, q2


def fetch_payment_summary(db: Session, store_id: int) -> PaymentSummaryResponse:
    gross = db.execute(
        text(
            "SELECT COALESCE(SUM(total_amount), 0) FROM orders "
            "WHERE store_id = :sid AND status <> 'CANCELLED'"
        ),
        {"sid": store_id},
    ).scalar() or 0

    collected = db.execute(
        text(
            "SELECT COALESCE(SUM(a.amount), 0) "
            "FROM payment_allocations a "
            "JOIN payment_settlements s ON s.id = a.settlement_id "
            "WHERE s.store_id = :sid AND s.status = 'COMPLETED'"
        ),
        {"sid": store_id},
    ).scalar() or 0

    refunded = db.execute(
        text("SELECT COALESCE(SUM(amount), 0) FROM payment_refunds WHERE store_id = :sid"),
        {"sid": store_id},
    ).scalar() or 0

    gross_d = q2(gross)
    collected_d = q2(collected)
    refunded_d = q2(refunded)
    net_d = collected_d - refunded_d
    outstanding_d = gross_d - net_d

    return PaymentSummaryResponse(
        store_id=store_id,
        currency=DEFAULT_CURRENCY,
        as_of=datetime.now(timezone.utc),
        gross_order_value=gross_d,
        collected_amount=collected_d,
        refunded_amount=refunded_d,
        net_collected_amount=net_d,
        outstanding_amount=outstanding_d,
        cancelled_orders_excluded=True,
    )
