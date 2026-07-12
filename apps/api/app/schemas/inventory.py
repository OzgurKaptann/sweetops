"""
Inventory API schemas.

Quantities are Decimal end-to-end (never float), and every mutating request
carries the reason/quantity the ledger constraints demand.

No request model here has a ``store_id`` field, and that is deliberate. The
store is derived from the authenticated session (see routers/inventory.py), so
there is nothing for a client to set. Pydantic ignores unknown keys, so a body
that smuggles in ``"store_id": 2`` is simply not read — the movement still lands
in the caller's own store. Responses DO carry ``store_id``, so a client can
always see which branch a figure belongs to.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class StockItem(BaseModel):
    ingredient_id: int
    ingredient_name: str
    category: Optional[str] = None
    unit: str
    on_hand_quantity: Decimal
    reserved_quantity: Decimal
    available_quantity: Decimal
    reorder_level: Optional[Decimal] = None

    model_config = ConfigDict(from_attributes=True)


class StockListResponse(BaseModel):
    total: int
    items: list[StockItem]


class MovementItem(BaseModel):
    id: int
    ingredient_id: int
    ingredient_name: Optional[str] = None
    movement_type: str
    quantity: Decimal
    quantity_delta_on_hand: Decimal
    quantity_delta_reserved: Decimal
    unit: str
    order_id: Optional[int] = None
    reason: Optional[str] = None
    actor_user_id: Optional[int] = None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class MovementListResponse(BaseModel):
    total: int
    items: list[MovementItem]


# ── Mutating commands ────────────────────────────────────────────────────────

class PurchaseReceiptRequest(BaseModel):
    """Goods received from a supplier."""
    ingredient_id: int
    quantity: Decimal = Field(gt=0)
    reason: Optional[str] = Field(default=None, max_length=500)


class ManualAdjustmentRequest(BaseModel):
    """
    Correct on-hand stock to a real physical count.

    ``delta`` is signed — negative writes stock off, positive adds it — and a
    reason is mandatory: an unexplained stock correction is indistinguishable
    from theft.
    """
    ingredient_id: int
    delta: Decimal
    reason: str = Field(min_length=1, max_length=500)


class WasteRequest(BaseModel):
    """Stock physically thrown away (burnt, dropped, spoiled)."""
    ingredient_id: int
    quantity: Decimal = Field(gt=0)
    reason: str = Field(min_length=1, max_length=500)


class MovementReceipt(BaseModel):
    """Result of a manual stock command, including the resulting stock state.

    ``store_id`` echoes the store the movement was actually booked against —
    always the session's store, never anything the request asked for.
    """
    movement_id: int
    store_id: int
    ingredient_id: int
    movement_type: str
    quantity: Decimal
    quantity_delta_on_hand: Decimal
    unit: str
    reason: Optional[str] = None
    on_hand_quantity: Decimal
    reserved_quantity: Decimal
    available_quantity: Decimal
    created_at: datetime
    idempotent_replay: bool = False
