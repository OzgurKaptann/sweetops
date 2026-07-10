"""
Cashier / payment-settlement schemas.

Money is always modelled as Decimal and serialised with two fractional digits.
None of these request models is trusted for store/actor context — those come
only from the authenticated session. A client-supplied pay-all amount is never
accepted; the server computes outstanding balances from locked ledger state.

Currency is NEVER accepted from the client. SweetOps is single-currency (TRY);
the settlement currency is fixed by server configuration and the refund currency
is derived from the original settlement. Request models therefore carry no
`currency` field.

Financial mutation requests are STRICT: they set `extra="forbid"`, so an unknown
field (a `currency` a client tries to inject, or any other unexpected key) is
REJECTED with a 422 validation error rather than silently dropped. Silently
discarding a financial instruction lets a client believe a currency/amount
override was honoured when it was ignored; forbidding extras makes the API
contract explicit — the server currency (TRY) stays authoritative because there
is simply no accepted channel to supply one.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field

from .common import BaseSchema


# ── Enums ─────────────────────────────────────────────────────────────────────

class PaymentMethodEnum(str, Enum):
    CASH = "CASH"
    CARD = "CARD"
    OTHER = "OTHER"


class PaymentStatusEnum(str, Enum):
    UNPAID = "UNPAID"
    PARTIALLY_PAID = "PARTIALLY_PAID"
    PAID = "PAID"


class RefundStatusEnum(str, Enum):
    NONE = "NONE"
    PARTIALLY_REFUNDED = "PARTIALLY_REFUNDED"
    REFUNDED = "REFUNDED"


# ── Requests ──────────────────────────────────────────────────────────────────

class SettlementCreateRequest(BaseModel):
    """
    Settle one or several outstanding orders for a table in one action.

    The server derives the store from the session, verifies the table and every
    order belong to it, and collects the EXACT outstanding balance of the
    selected orders — no client-supplied amount is accepted here.
    """
    model_config = ConfigDict(extra="forbid")

    table_id: int
    order_ids: List[int] = Field(..., min_length=1)
    payment_method: PaymentMethodEnum
    # No `currency` field — the server fixes it to the configured store currency
    # (TRY). A client cannot create a settlement in another currency.
    note: Optional[str] = Field(default=None, max_length=500)
    terminal_reference: Optional[str] = Field(default=None, max_length=64)


class OrderPaymentRequest(BaseModel):
    """
    Collect payment against a single order. When `amount` is omitted the full
    outstanding balance is collected; when supplied it must be positive and not
    exceed the outstanding balance (partial payment).
    """
    model_config = ConfigDict(extra="forbid")

    payment_method: PaymentMethodEnum
    amount: Optional[Decimal] = None
    # No `currency` field — see SettlementCreateRequest. Server-fixed to TRY.
    note: Optional[str] = Field(default=None, max_length=500)
    terminal_reference: Optional[str] = Field(default=None, max_length=64)


class RefundCreateRequest(BaseModel):
    """Refund previously-collected money for one allocation. Reason mandatory."""
    model_config = ConfigDict(extra="forbid")

    amount: Decimal
    reason: str = Field(..., min_length=1, max_length=500)


# ── Responses ─────────────────────────────────────────────────────────────────

class AllocationReceipt(BaseSchema):
    id: int
    order_id: int
    order_code: str
    amount: Decimal


class SettlementReceipt(BaseSchema):
    settlement_id: int
    table_id: Optional[int]
    table_number: Optional[str] = None
    payment_method: str
    currency: str
    gross_amount: Decimal
    status: str
    cashier_display: str
    completed_at: datetime
    allocations: List[AllocationReceipt] = []
    idempotent_replay: bool = False


class RefundReceipt(BaseSchema):
    refund_id: int
    settlement_id: int
    allocation_id: int
    order_id: int
    order_code: str
    amount: Decimal
    currency: str
    reason: str
    refunded_by_display: str
    created_at: datetime
    idempotent_replay: bool = False


# ── Cashier read models ───────────────────────────────────────────────────────

class OpenTableSummary(BaseSchema):
    table_id: int
    table_number: Optional[str]
    open_order_count: int
    gross_amount: Decimal
    paid_amount: Decimal
    remaining_amount: Decimal
    oldest_order_at: Optional[datetime]


class OpenTablesResponse(BaseSchema):
    tables: List[OpenTableSummary]


class OrderBillLine(BaseSchema):
    order_id: int
    order_code: str
    created_at: datetime
    preparation_status: str
    payment_status: str
    refund_status: str
    order_total: Decimal
    paid_amount: Decimal
    refunded_amount: Decimal
    net_paid: Decimal
    remaining_amount: Decimal
    payable: bool


class TableBillResponse(BaseSchema):
    table_id: int
    table_number: Optional[str]
    currency: str
    gross_amount: Decimal
    paid_amount: Decimal
    remaining_amount: Decimal
    orders: List[OrderBillLine]


class OrderDetailResponse(OrderBillLine):
    store_id: int
    table_id: Optional[int]


class RecentTransaction(BaseSchema):
    kind: str  # "COLLECTION" | "REFUND"
    settlement_id: int
    refund_id: Optional[int] = None
    table_id: Optional[int]
    payment_method: Optional[str]
    currency: str
    amount: Decimal
    actor_display: str
    at: datetime


class RecentTransactionsResponse(BaseSchema):
    transactions: List[RecentTransaction]


class PaymentSummaryResponse(BaseSchema):
    """
    Store-scoped financial summary that keeps ORDERED value and COLLECTED money
    strictly separate. The order-total snapshot is the source of truth for gross
    order value; the append-only ledger is the source of truth for cash.
    """
    store_id: int
    currency: str
    as_of: datetime
    gross_order_value: Decimal      # Σ persisted totals of non-cancelled orders
    collected_amount: Decimal       # Σ completed allocations
    refunded_amount: Decimal        # Σ completed refunds
    net_collected_amount: Decimal   # collected − refunded
    outstanding_amount: Decimal     # gross_order_value − net_collected
    cancelled_orders_excluded: bool = True
