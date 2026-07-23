"""
Owner Operational Dashboard — a single read-only aggregation over the existing
source-of-truth systems.

This service is a READER. It computes no new money, invents no stock levels,
fabricates no timing metric, writes nothing and stores nothing. Every figure is
re-derived live from the system that already owns it, so the dashboard can never
disagree with the screen a metric came from:

  * orders / kitchen tempo  → kitchen_timing_service (the SAME live board & today
                              averages the /kitchen/timing endpoints serve).
  * money collected / refunded → the append-only payment ledger
                              (payment_allocations + payment_settlements for
                              collected, payment_refunds for refunds), the same
                              tables payment_analytics_service reads — here scoped
                              to today.
  * open issues / resolved today → order_issues (status OPEN, resolved_at today).
  * open shifts / discrepancy → cashier_shifts frozen CLOSED snapshots.
  * stock alerts            → inventory_service.threshold_status, the SAME
                              classifier the threshold-alerts screen uses.

"Today" is the BUSINESS calendar day (``app.core.business_time``), matching the
day boundary the kitchen timing summary and owner metrics layer use. Stored
timestamps stay UTC; the business day is expressed as the half-open UTC interval
``[day_start, day_end)`` that covers it, and every "today" filter is a range
predicate over that interval rather than ``func.date(col) == today``. In Istanbul
that interval opens at 21:00Z on the previous UTC date, so a 01:00 local sale is
counted on the day the shop actually made it — see
``docs/RUNTIME_PRODUCT_GAP_REVIEW.md`` F-04.

Store scope is the caller's responsibility: ``store_id`` comes from the
authenticated session, never the client, exactly as everywhere else.

Why this is not forecasting/BI/accounting
-----------------------------------------
Everything here is a COUNT or a SUM of things that have already happened, plus a
deterministic threshold comparison for the attention list. Nothing predicts demand,
estimates a completion time, or builds a historical warehouse. The attention list
is a fixed set of if-count-positive rules — no scoring model, no ranking heuristic,
no LLM.
"""
from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.business_time import business_day_bounds_utc, business_today, utc_now
from app.models.cashier_shift import CashierShift, SHIFT_CLOSED, SHIFT_OPEN
from app.models.ingredient import Ingredient
from app.models.ingredient_stock import IngredientStock
from app.models.inventory_threshold import (
    THRESHOLD_STATUS_BELOW_RESERVED,
    THRESHOLD_STATUS_CRITICAL,
    THRESHOLD_STATUS_HEALTHY,
    THRESHOLD_STATUS_LOW,
    THRESHOLD_STATUS_NOT_CONFIGURED,
    THRESHOLD_STATUS_OUT_OF_STOCK,
)
from app.models.order import Order
from app.models.order_issue import (
    ISSUE_STATUS_OPEN,
    ISSUE_STATUS_RESOLVED,
    REFUNDING_RESOLUTIONS,
    OrderIssue,
)
from app.models.payment_allocation import PaymentAllocation
from app.models.payment_refund import PaymentRefund
from app.models.payment_settlement import PaymentSettlement
from app.schemas.owner_dashboard import (
    DashboardAttentionItem,
    DashboardInventory,
    DashboardIssues,
    DashboardKitchen,
    DashboardOrders,
    DashboardPayments,
    DashboardShifts,
    OperationalDashboardResponse,
)
from app.services import inventory_service
from app.services.kitchen_timing_service import get_timing_summary
from app.services.payment_service import DEFAULT_CURRENCY

TWO_PLACES = Decimal("0.01")
ZERO = Decimal("0.00")
# Below this a signed shift discrepancy is treated as "balanced" (Denk) — mirrors
# owner-web's discrepancyClass tolerance so backend and screen agree on "farklı".
DISCREPANCY_EPSILON = Decimal("0.005")


def _q2(value) -> Decimal:
    return Decimal(str(value if value is not None else "0")).quantize(
        TWO_PLACES, rounding=ROUND_HALF_UP
    )


# ── Attention list (deterministic, derived) ──────────────────────────────────
# Fixed rules, evaluated in a fixed order. severity_rank orders the output so the
# most urgent card is always first; code is the stable tiebreak within a severity.
_SEVERITY_RANK = {"critical": 3, "warning": 2, "info": 1}


def _build_attention(
    *,
    orders: DashboardOrders,
    payments: DashboardPayments,
    kitchen: DashboardKitchen,
    issues: DashboardIssues,
    shifts: DashboardShifts,
    inventory: DashboardInventory,
) -> list[DashboardAttentionItem]:
    items: list[DashboardAttentionItem] = []

    # Nothing available to sell is the most urgent operational fact.
    out = inventory.out_of_stock_count + inventory.below_reserved_count
    if out > 0:
        items.append(DashboardAttentionItem(
            severity="critical", code="OUT_OF_STOCK", count=out, target_route="/inventory",
        ))
    if inventory.critical_count > 0:
        items.append(DashboardAttentionItem(
            severity="warning", code="CRITICAL_STOCK",
            count=inventory.critical_count, target_route="/inventory",
        ))
    if kitchen.delayed_orders > 0:
        items.append(DashboardAttentionItem(
            severity="warning", code="DELAYED_KITCHEN",
            count=kitchen.delayed_orders, target_route="/kitchen",
        ))
    if issues.open_count > 0:
        items.append(DashboardAttentionItem(
            severity="warning", code="OPEN_ISSUES",
            count=issues.open_count, target_route="/order-issues",
        ))
    if shifts.shifts_with_discrepancy_today > 0:
        items.append(DashboardAttentionItem(
            severity="warning", code="SHIFT_DISCREPANCY",
            count=shifts.shifts_with_discrepancy_today, target_route="/shifts",
        ))
    if shifts.open_shift_count > 0:
        items.append(DashboardAttentionItem(
            severity="info", code="OPEN_SHIFTS",
            count=shifts.open_shift_count, target_route="/shifts",
        ))
    if payments.unpaid_or_partially_paid_orders > 0:
        # No owner-web page owns "unpaid orders", so no deep link — surface only.
        items.append(DashboardAttentionItem(
            severity="info", code="UNPAID_ORDERS",
            count=payments.unpaid_or_partially_paid_orders, target_route=None,
        ))

    # Deterministic: severity desc, then a stable code order (insertion order above).
    items.sort(key=lambda i: _SEVERITY_RANK.get(i.severity, 0), reverse=True)
    return items


# ── Block builders ────────────────────────────────────────────────────────────

def _orders_block(
    db: Session, store_id: int, day_start, day_end, kitchen_summary: dict
) -> DashboardOrders:
    # Live counts come straight from the kitchen timing summary (source of truth
    # for the active board), so orders and kitchen tempo can never disagree.
    completed_today = (
        db.query(func.count(Order.id))
        .filter(
            Order.store_id == store_id,
            Order.status == "DELIVERED",
            Order.created_at >= day_start,
            Order.created_at < day_end,
        )
        .scalar()
    ) or 0
    cancelled_today = (
        db.query(func.count(Order.id))
        .filter(
            Order.store_id == store_id,
            Order.status == "CANCELLED",
            Order.created_at >= day_start,
            Order.created_at < day_end,
        )
        .scalar()
    ) or 0
    return DashboardOrders(
        active_count=kitchen_summary["active_orders"],
        waiting_count=kitchen_summary["waiting_orders"],
        in_prep_count=kitchen_summary["in_prep_orders"],
        ready_count=kitchen_summary["ready_orders"],
        completed_today=int(completed_today),
        cancelled_today=int(cancelled_today),
    )


def _payments_block(db: Session, store_id: int, day_start, day_end) -> DashboardPayments:
    # Collected today — the SAME "collected" definition as payment_analytics
    # (Σ completed allocations), scoped to settlements completed today. Order totals
    # are never used as money collected; unpaid orders never count as revenue.
    collected = (
        db.query(func.coalesce(func.sum(PaymentAllocation.amount), 0))
        .select_from(PaymentAllocation)
        .join(PaymentSettlement, PaymentSettlement.id == PaymentAllocation.settlement_id)
        .filter(
            PaymentSettlement.store_id == store_id,
            PaymentSettlement.status == "COMPLETED",
            PaymentSettlement.completed_at >= day_start,
            PaymentSettlement.completed_at < day_end,
        )
        .scalar()
    ) or 0

    refunds = (
        db.query(func.coalesce(func.sum(PaymentRefund.amount), 0))
        .filter(
            PaymentRefund.store_id == store_id,
            PaymentRefund.created_at >= day_start,
            PaymentRefund.created_at < day_end,
        )
        .scalar()
    ) or 0

    unpaid = (
        db.query(func.count(Order.id))
        .filter(
            Order.store_id == store_id,
            Order.status != "CANCELLED",
            Order.payment_status.in_(["UNPAID", "PARTIALLY_PAID"]),
        )
        .scalar()
    ) or 0

    gross_d = _q2(collected)
    refunds_d = _q2(refunds)
    return DashboardPayments(
        currency=DEFAULT_CURRENCY,
        gross_collected_today=gross_d,
        refunds_today=refunds_d,
        net_collected_today=_q2(gross_d - refunds_d),
        unpaid_or_partially_paid_orders=int(unpaid),
    )


def _kitchen_block(kitchen_summary: dict) -> DashboardKitchen:
    return DashboardKitchen(
        active_orders=kitchen_summary["active_orders"],
        delayed_orders=kitchen_summary["delayed_orders"],
        average_prep_seconds_today=kitchen_summary["average_prep_seconds_today"],
        average_time_to_ready_seconds_today=kitchen_summary["average_time_to_ready_seconds_today"],
        p95_prep_seconds_today=kitchen_summary["p95_prep_seconds_today"],
    )


def _issues_block(db: Session, store_id: int, day_start, day_end) -> DashboardIssues:
    open_count = (
        db.query(func.count(OrderIssue.id))
        .filter(OrderIssue.store_id == store_id, OrderIssue.status == ISSUE_STATUS_OPEN)
        .scalar()
    ) or 0
    resolved_today = (
        db.query(func.count(OrderIssue.id))
        .filter(
            OrderIssue.store_id == store_id,
            OrderIssue.status == ISSUE_STATUS_RESOLVED,
            OrderIssue.resolved_at >= day_start,
            OrderIssue.resolved_at < day_end,
        )
        .scalar()
    ) or 0
    # Refund total on issues RESOLVED today: the frozen approved amount, which by
    # construction equals the refund-ledger rows the resolution created (each
    # stamped with the issue id). Only refunding resolutions carry an amount.
    refund_today = (
        db.query(func.coalesce(func.sum(OrderIssue.approved_refund_amount), 0))
        .filter(
            OrderIssue.store_id == store_id,
            OrderIssue.status == ISSUE_STATUS_RESOLVED,
            OrderIssue.resolution_type.in_(list(REFUNDING_RESOLUTIONS)),
            OrderIssue.resolved_at >= day_start,
            OrderIssue.resolved_at < day_end,
        )
        .scalar()
    ) or 0
    return DashboardIssues(
        open_count=int(open_count),
        resolved_today=int(resolved_today),
        refund_amount_today=_q2(refund_today),
    )


def _shifts_block(db: Session, store_id: int, day_start, day_end) -> DashboardShifts:
    open_count = (
        db.query(func.count(CashierShift.id))
        .filter(CashierShift.store_id == store_id, CashierShift.status == SHIFT_OPEN)
        .scalar()
    ) or 0
    # Closed-today figures read ONLY the frozen snapshot columns — never recomputed.
    closed_shifts = (
        db.query(CashierShift.cash_discrepancy_amount)
        .filter(
            CashierShift.store_id == store_id,
            CashierShift.status == SHIFT_CLOSED,
            CashierShift.closed_at >= day_start,
            CashierShift.closed_at < day_end,
        )
        .all()
    )
    total_discrepancy = ZERO
    with_discrepancy = 0
    for (disc,) in closed_shifts:
        d = _q2(disc)
        total_discrepancy += d
        if abs(d) >= DISCREPANCY_EPSILON:
            with_discrepancy += 1
    return DashboardShifts(
        open_shift_count=int(open_count),
        closed_today=len(closed_shifts),
        total_discrepancy_today=_q2(total_discrepancy),
        shifts_with_discrepancy_today=with_discrepancy,
    )


def _inventory_block(db: Session, store_id: int) -> DashboardInventory:
    # The SAME classifier the threshold-alerts screen uses, over the SAME rows
    # (active ingredients this branch stocks). Available stock (on_hand − reserved)
    # is what threshold_status tests — never on-hand — so no new status is invented.
    rows = (
        db.query(IngredientStock)
        .join(Ingredient, Ingredient.id == IngredientStock.ingredient_id)
        .filter(
            IngredientStock.store_id == store_id,
            Ingredient.is_active == True,  # noqa: E712
        )
        .all()
    )
    counts = {
        THRESHOLD_STATUS_BELOW_RESERVED: 0,
        THRESHOLD_STATUS_OUT_OF_STOCK: 0,
        THRESHOLD_STATUS_CRITICAL: 0,
        THRESHOLD_STATUS_LOW: 0,
        THRESHOLD_STATUS_HEALTHY: 0,
        THRESHOLD_STATUS_NOT_CONFIGURED: 0,
    }
    for stock in rows:
        status = inventory_service.threshold_status(stock)
        counts[status] = counts.get(status, 0) + 1
    return DashboardInventory(
        out_of_stock_count=counts[THRESHOLD_STATUS_OUT_OF_STOCK],
        below_reserved_count=counts[THRESHOLD_STATUS_BELOW_RESERVED],
        critical_count=counts[THRESHOLD_STATUS_CRITICAL],
        low_count=counts[THRESHOLD_STATUS_LOW],
        healthy_count=counts[THRESHOLD_STATUS_HEALTHY],
        not_configured_count=counts[THRESHOLD_STATUS_NOT_CONFIGURED],
    )


# ── Public entry point ────────────────────────────────────────────────────────

def fetch_operational_dashboard(db: Session, store_id: int) -> OperationalDashboardResponse:
    """Read-only operational snapshot for one store, right now."""
    now = utc_now()
    # ``as_of`` stays UTC (an instant); ``business_date`` is the local shop day
    # every "today" figure below is scoped to.
    today = business_today()
    day_start, day_end = business_day_bounds_utc(today)

    # One kitchen-timing read powers both the orders live counts and the kitchen
    # tempo block, so the two are guaranteed consistent.
    kitchen_summary = get_timing_summary(db, store_id)

    orders = _orders_block(db, store_id, day_start, day_end, kitchen_summary)
    payments = _payments_block(db, store_id, day_start, day_end)
    kitchen = _kitchen_block(kitchen_summary)
    issues = _issues_block(db, store_id, day_start, day_end)
    shifts = _shifts_block(db, store_id, day_start, day_end)
    inventory = _inventory_block(db, store_id)

    attention = _build_attention(
        orders=orders, payments=payments, kitchen=kitchen,
        issues=issues, shifts=shifts, inventory=inventory,
    )

    return OperationalDashboardResponse(
        business_date=today.isoformat(),
        as_of=now,
        store_id=store_id,
        orders=orders,
        payments=payments,
        kitchen=kitchen,
        issues=issues,
        shifts=shifts,
        inventory=inventory,
        attention=attention,
    )
