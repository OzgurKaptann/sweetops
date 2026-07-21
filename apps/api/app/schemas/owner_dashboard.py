"""
Owner operational dashboard schemas.

One read-only aggregate that answers the owner's daily "how is it going?" from the
systems that already exist — orders, the kitchen timing derivation, the payment
ledger, order issues, cashier-shift snapshots and inventory threshold alerts. It
computes NO new money, invents NO stock levels and stores NOTHING; every figure is
re-read live from a source of truth.

Wire contract
-------------
Money is Decimal (serialised as a 2dp string, exactly like the payment summary).
Durations are integer seconds (or null when no completed data exists — the kitchen
timing layer never fabricates a zero). Structural fields on the attention list stay
as English enums/codes; owner-web owns the Turkish rendering, so no raw enum ever
reaches a screen.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import List, Optional

from .common import BaseSchema


class DashboardOrders(BaseSchema):
    active_count: int          # live NEW + IN_PREP + READY
    waiting_count: int         # live NEW
    in_prep_count: int         # live IN_PREP
    ready_count: int           # live READY
    completed_today: int       # DELIVERED, created today
    cancelled_today: int       # CANCELLED, created today


class DashboardPayments(BaseSchema):
    currency: str
    gross_collected_today: Decimal          # Σ completed allocations collected today
    refunds_today: Decimal                  # Σ refund ledger amounts created today
    net_collected_today: Decimal            # gross − refunds
    unpaid_or_partially_paid_orders: int    # open money owed (non-cancelled orders)


class DashboardKitchen(BaseSchema):
    active_orders: int
    delayed_orders: int
    average_prep_seconds_today: Optional[int] = None
    average_time_to_ready_seconds_today: Optional[int] = None
    p95_prep_seconds_today: Optional[int] = None


class DashboardIssues(BaseSchema):
    open_count: int
    resolved_today: int
    refund_amount_today: Decimal            # Σ approved refund on issues resolved today


class DashboardShifts(BaseSchema):
    open_shift_count: int
    closed_today: int
    total_discrepancy_today: Decimal        # Σ frozen cash_discrepancy of shifts closed today
    shifts_with_discrepancy_today: int      # closed today with a non-zero discrepancy


class DashboardInventory(BaseSchema):
    out_of_stock_count: int
    below_reserved_count: int
    critical_count: int
    low_count: int
    healthy_count: int
    not_configured_count: int


class DashboardAttentionItem(BaseSchema):
    """
    One deterministic "look at this" signal, derived purely from the metrics above.
    ``severity`` and ``code`` are English wire enums; owner-web maps them to Turkish.
    ``target_route`` is an owner-web path to deep-link to, or null when no page owns it.
    """
    severity: str          # critical | warning | info
    code: str              # OUT_OF_STOCK | CRITICAL_STOCK | DELAYED_KITCHEN | ...
    count: int             # how many things triggered it (e.g. 3 critical ingredients)
    target_route: Optional[str] = None


class OperationalDashboardResponse(BaseSchema):
    business_date: str     # YYYY-MM-DD (server/UTC day, as the kitchen-timing layer uses)
    as_of: datetime
    store_id: int
    orders: DashboardOrders
    payments: DashboardPayments
    kitchen: DashboardKitchen
    issues: DashboardIssues
    shifts: DashboardShifts
    inventory: DashboardInventory
    attention: List[DashboardAttentionItem] = []
