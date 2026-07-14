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


class TransferRequest(BaseModel):
    """
    Ship stock from the caller's own store to another store.

    ``extra="forbid"``, so this schema does not merely IGNORE a smuggled
    ``source_store_id`` / ``actor_user_id`` / ``movement_type`` /
    ``quantity_delta_on_hand`` / ``idempotency_key_hash`` — it REJECTS the whole
    request with a 422. Silently ignoring them would leave a client believing it
    had shipped Store B's chocolate and cheerfully told so. The source store comes
    from the authenticated session and nowhere else; there is deliberately no
    field here to set it.

    ``destination_store_id`` IS client-supplied, because only the manager knows
    which branch the van is going to — and it is validated server-side: the store
    must exist and must not be the source.
    """
    model_config = ConfigDict(extra="forbid")

    destination_store_id: int
    ingredient_id: int
    quantity: Decimal = Field(gt=0)
    reason: str = Field(min_length=1, max_length=500)
    note: Optional[str] = Field(default=None, max_length=500)


class TransferDestination(BaseModel):
    """
    A store the caller MAY ship stock to — i.e. every store except their own.

    This is a display list for a destination picker and nothing more: it carries
    a name and an id, never another branch's stock, staff, takings or table map.
    Knowing that a sibling branch exists is already implied by the transfer
    feature itself; knowing what is on its shelves is not, and is not here.
    """
    store_id: int
    name: str
    location: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)


class TransferDestinationListResponse(BaseModel):
    total: int
    items: list[TransferDestination]


class TransferReceipt(BaseModel):
    """
    Result of a transfer: the business event, and the id of each of its two legs.

    Neither the raw Idempotency-Key nor the request hash is ever exposed — they
    are stored only as SHA-256 digests, and echoing either back would hand a
    client a replay token.
    """
    transfer_id: int
    source_store_id: int
    destination_store_id: int
    ingredient_id: int
    ingredient_name: Optional[str] = None
    quantity: Decimal
    unit: str
    status: str
    reason: str
    note: Optional[str] = None
    initiated_by_user_id: int
    source_movement_id: int
    destination_movement_id: int
    # Post-transfer stock in the SOURCE store — the branch the caller is
    # accountable for. The destination's shelf is not the caller's business.
    source_on_hand_quantity: Decimal
    source_reserved_quantity: Decimal
    source_available_quantity: Decimal
    created_at: datetime
    idempotent_replay: bool = False

    model_config = ConfigDict(from_attributes=True)


class TransferItem(BaseModel):
    """One transfer as it appears in a list — inbound or outbound."""
    transfer_id: int
    source_store_id: int
    destination_store_id: int
    ingredient_id: int
    ingredient_name: Optional[str] = None
    quantity: Decimal
    unit: str
    status: str
    reason: str
    note: Optional[str] = None
    initiated_by_user_id: int
    # Which side of this transfer the CALLER's store is on. A manager reading the
    # list needs to know whether the crate left or arrived, and the raw store ids
    # alone make that a mental subtraction.
    direction: str
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class TransferListResponse(BaseModel):
    total: int
    items: list[TransferItem]


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
