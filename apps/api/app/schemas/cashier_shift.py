"""
Cashier shift schemas.

A shift is a reconciliation over the existing payment ledger, so the money fields
mirror the ledger's discipline: Decimal end-to-end, two fractional digits, and no
store/actor context is ever trusted from the request body — the store and the
cashier come only from the authenticated session.

Request models are STRICT (`extra="forbid"`): an unknown field is rejected with a
422 rather than silently dropped, exactly like the payment schemas. Currency is
never accepted from the client (SweetOps is single-currency, TRY).
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field

from .common import BaseSchema


# ── Requests ──────────────────────────────────────────────────────────────────

class ShiftOpenRequest(BaseModel):
    """Open a shift with the cash the cashier starts the drawer with."""
    model_config = ConfigDict(extra="forbid")

    opening_cash_amount: Decimal
    open_note: Optional[str] = Field(default=None, max_length=500)


class ShiftCloseRequest(BaseModel):
    """Close a shift with the cash the cashier physically counted."""
    model_config = ConfigDict(extra="forbid")

    counted_closing_cash_amount: Decimal
    close_note: Optional[str] = Field(default=None, max_length=500)


# ── Responses ─────────────────────────────────────────────────────────────────

class ShiftResponse(BaseSchema):
    """
    One shift, open or closed. The close-snapshot fields are null while the shift
    is OPEN and populated once CLOSED. Raw status is included as the stable wire
    contract; the UI maps it to Turkish and never renders it directly.
    """
    id: int
    store_id: int
    cashier_user_id: int
    cashier_display: str
    status: str

    opened_at: datetime
    closed_at: Optional[datetime] = None

    opening_cash_amount: Decimal
    open_note: Optional[str] = None
    close_note: Optional[str] = None

    # Close snapshot (null while OPEN).
    counted_closing_cash_amount: Optional[Decimal] = None
    cash_payments_amount: Optional[Decimal] = None
    cash_refunds_amount: Optional[Decimal] = None
    expected_closing_cash_amount: Optional[Decimal] = None
    cash_discrepancy_amount: Optional[Decimal] = None
    card_payments_amount: Optional[Decimal] = None
    card_refunds_amount: Optional[Decimal] = None
    gross_payments_amount: Optional[Decimal] = None
    total_refunds_amount: Optional[Decimal] = None
    net_collected_amount: Optional[Decimal] = None

    # True only when this response is an idempotent replay of an earlier command.
    idempotent_replay: bool = False


class CurrentShiftResponse(BaseSchema):
    """The authenticated cashier's currently-open shift, or null if none."""
    current_shift: Optional[ShiftResponse] = None


class ShiftListResponse(BaseSchema):
    shifts: List[ShiftResponse]
