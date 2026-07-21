"""
Order issue & controlled refund schemas.

Store and actor context are NEVER read from these request models — they come only
from the authenticated session. Both mutation requests are STRICT (extra="forbid"):
an unknown field is REJECTED with a 422 rather than silently dropped, so a client
can never believe an ignored field was honoured.

Amounts are Decimal end-to-end. Currency is not accepted from the client (SweetOps
is single-currency TRY; the refund currency is derived from the original settlement).
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field

from .common import BaseSchema


# ── Enums (the English wire contract; the UI renders Turkish labels, never these) ─

class IssueTypeEnum(str, Enum):
    CUSTOMER_CANCELLED = "CUSTOMER_CANCELLED"
    WRONG_ITEM = "WRONG_ITEM"
    MISSING_ITEM = "MISSING_ITEM"
    QUALITY_PROBLEM = "QUALITY_PROBLEM"
    DUPLICATE_ORDER = "DUPLICATE_ORDER"
    STAFF_ERROR = "STAFF_ERROR"
    OTHER = "OTHER"


class IssueStatusEnum(str, Enum):
    OPEN = "OPEN"
    RESOLVED = "RESOLVED"
    VOIDED = "VOIDED"


class ResolutionTypeEnum(str, Enum):
    NO_REFUND = "NO_REFUND"
    FULL_REFUND = "FULL_REFUND"
    PARTIAL_REFUND = "PARTIAL_REFUND"
    CANCEL_ONLY = "CANCEL_ONLY"


# ── Requests ──────────────────────────────────────────────────────────────────

class IssueCreateRequest(BaseModel):
    """Record a problem against an order. Creation moves no money and no stock."""
    model_config = ConfigDict(extra="forbid")

    issue_type: IssueTypeEnum
    # What the customer/staff is asking to be refunded. Optional — an issue can be
    # raised with no refund request at all. Validated against the order's refundable
    # balance server-side.
    requested_refund_amount: Optional[Decimal] = Field(default=None, ge=0)
    reason: str = Field(..., min_length=1, max_length=500)
    note: Optional[str] = Field(default=None, max_length=500)


class IssueResolveRequest(BaseModel):
    """Resolve an OPEN issue with exactly one controlled resolution."""
    model_config = ConfigDict(extra="forbid")

    resolution_type: ResolutionTypeEnum
    # Only meaningful for PARTIAL_REFUND (the exact amount granted). For FULL_REFUND
    # the server uses the whole remaining refundable amount; for NO_REFUND /
    # CANCEL_ONLY it must be absent or zero.
    approved_refund_amount: Optional[Decimal] = Field(default=None, ge=0)
    reason: str = Field(..., min_length=1, max_length=500)
    note: Optional[str] = Field(default=None, max_length=500)


# ── Responses ─────────────────────────────────────────────────────────────────

class OrderIssueResponse(BaseSchema):
    id: int
    store_id: int
    order_id: int
    order_code: str
    issue_type: str
    status: str
    resolution_type: Optional[str] = None
    requested_refund_amount: Optional[Decimal] = None
    approved_refund_amount: Optional[Decimal] = None
    refund_id: Optional[int] = None
    reason: str
    note: Optional[str] = None
    created_by_user_id: int
    created_by_display: str
    resolved_by_user_id: Optional[int] = None
    resolved_by_display: Optional[str] = None
    created_at: datetime
    resolved_at: Optional[datetime] = None
    # The order's remaining refundable amount at read time (net paid). Lets the UI
    # show a cashier exactly how much may still be refunded without a second call.
    order_refundable_amount: Decimal = Decimal("0.00")
    idempotent_replay: bool = False


class OrderIssueListResponse(BaseSchema):
    issues: List[OrderIssueResponse]
