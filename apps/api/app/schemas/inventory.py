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


# ── Physical stock count ─────────────────────────────────────────────────────

class StockCountRequest(BaseModel):
    """
    Apply a physical count to the caller's own store.

    ``extra="forbid"``, so this schema does not merely IGNORE a smuggled
    ``store_id`` / ``actor_user_id`` / ``movement_type`` / ``delta_quantity`` /
    ``system_on_hand_quantity`` / ``idempotency_key_hash`` / ``request_hash`` — it
    REJECTS the whole request with a 422. Silently ignoring them would leave a
    client believing it had counted another branch's freezer, or had dictated the
    delta, and cheerfully told so.

    Note what is NOT here: the delta, and the system's figures. The client does not
    get to state them — the server reads them from the locked stock row at the
    instant the count is applied and computes the delta itself. A client-supplied
    delta would be computed against whatever the manager's browser last saw, which
    an order placed thirty seconds ago has already made stale.

    ``counted_quantity`` may be ZERO: an empty shelf is a valid, and important,
    count. It may not be negative (``ge=0``).
    """
    model_config = ConfigDict(extra="forbid")

    ingredient_id: int
    counted_quantity: Decimal = Field(ge=0)
    reason: str = Field(min_length=1, max_length=500)
    note: Optional[str] = Field(default=None, max_length=500)


class StockCountReceipt(BaseModel):
    """
    Result of a physical count: what was counted, what the system believed, and the
    difference that was applied.

    ``movement_id`` is None for a zero-delta count — the shelf agreed with the
    system, so no ledger row was written. The count itself still exists and is still
    evidence that the shelf was checked. See docs/PHYSICAL_STOCK_COUNT_WORKFLOW.md.

    Neither the raw Idempotency-Key nor the request hash is ever exposed — they are
    stored only as SHA-256 digests, and echoing either back would hand a client a
    replay token.
    """
    stock_count_id: int
    store_id: int
    ingredient_id: int
    ingredient_name: Optional[str] = None
    counted_quantity: Decimal
    system_on_hand_quantity: Decimal
    system_reserved_quantity: Decimal
    delta_quantity: Decimal
    unit: str
    reason: str
    note: Optional[str] = None
    status: str
    counted_by_user_id: int
    # Null when the count found the shelf correct: no stock moved, so no ledger row.
    movement_id: Optional[int] = None
    # The stock state AFTER the count was applied. on_hand now equals counted;
    # reserved is untouched by definition.
    on_hand_quantity: Decimal
    reserved_quantity: Decimal
    available_quantity: Decimal
    created_at: datetime
    applied_at: datetime
    idempotent_replay: bool = False

    model_config = ConfigDict(from_attributes=True)


class StockCountItem(BaseModel):
    """One count as it appears in the history list."""
    stock_count_id: int
    store_id: int
    ingredient_id: int
    ingredient_name: Optional[str] = None
    counted_quantity: Decimal
    system_on_hand_quantity: Decimal
    system_reserved_quantity: Decimal
    delta_quantity: Decimal
    unit: str
    reason: str
    note: Optional[str] = None
    status: str
    counted_by_user_id: int
    movement_id: Optional[int] = None
    created_at: datetime
    applied_at: datetime

    model_config = ConfigDict(from_attributes=True)


class StockCountListResponse(BaseModel):
    total: int
    items: list[StockCountItem]


# ── Inventory threshold alerts ───────────────────────────────────────────────
#
# Thresholds are CONFIGURATION, not stock. Nothing in this section moves a gram, and
# no schema here has a quantity the shop owns — only the levels at which a branch
# wants to be warned. See docs/INVENTORY_THRESHOLD_ALERTS.md.


class ThresholdUpdateRequest(BaseModel):
    """
    Set one ingredient's alert thresholds in the caller's own store.

    ``extra="forbid"``, so this schema does not merely IGNORE a smuggled ``store_id``
    / ``actor_user_id`` / ``status`` / ``on_hand_quantity`` / ``idempotency_key_hash``
    — it REJECTS the whole request with a 422. Silently ignoring them would leave a
    client believing it had configured another branch's alerts, or dictated its own
    status, and cheerfully told so. The store comes from the authenticated session and
    nowhere else; the ingredient comes from the path. There is deliberately no field
    here for either.

    The body states the COMPLETE threshold configuration, not a patch of it. An
    omitted or explicitly null threshold means NOT CONFIGURED, and clearing one is a
    real decision that is logged like any other. Partial-update semantics would make a
    null ambiguous between "leave this alone" and "clear this", and the request hash
    that idempotency compares could not tell those two intents apart either.

    Note there is no field for a STATUS. A client does not get to say an ingredient is
    healthy; the server derives that from the stock row and these levels.
    """
    model_config = ConfigDict(extra="forbid")

    # Nullable and unconstrained here on purpose: the non-negativity and ordering rules
    # are enforced in the service, which can answer with a Turkish sentence naming the
    # rule that was broken. A bare Pydantic ``ge=0`` would produce a 422 whose body is
    # an English validation trace, which is exactly what a manager must never be shown.
    critical_quantity: Optional[Decimal] = None
    minimum_quantity: Optional[Decimal] = None
    target_quantity: Optional[Decimal] = None

    # Mandatory. A threshold quietly lowered until it stops firing is how a branch
    # discovers a stockout at the counter, and the record must say who decided that
    # and why.
    reason: str = Field(min_length=1, max_length=500)


class ThresholdReceipt(BaseModel):
    """
    Result of a threshold decision: the levels now in force, and the resulting status.

    The stock quantities are echoed back UNCHANGED — they are here so the manager's
    screen can show the new status against the stock it was computed from, and they
    are the proof that the operation moved nothing. There is no ``movement_id`` field
    on this receipt because a threshold change writes no movement, and there is
    nowhere for one to appear.

    Neither the raw Idempotency-Key nor the request hash is ever exposed — they are
    stored only as SHA-256 digests, and echoing either back would hand a client a
    replay token.
    """
    ingredient_id: int
    store_id: int
    ingredient_name: Optional[str] = None
    unit: str

    critical_quantity: Optional[Decimal] = None
    minimum_quantity: Optional[Decimal] = None
    target_quantity: Optional[Decimal] = None

    # Unchanged by this operation, by construction.
    on_hand_quantity: Decimal
    reserved_quantity: Decimal
    available_quantity: Decimal

    status: str
    status_label: str
    recommended_restock_quantity: Optional[Decimal] = None

    reason: str
    threshold_updated_at: Optional[datetime] = None
    threshold_updated_by_user_id: Optional[int] = None
    idempotent_replay: bool = False

    model_config = ConfigDict(from_attributes=True)


class ThresholdAlertItem(BaseModel):
    """
    One ingredient's alert line for the caller's branch.

    ``status`` is the English wire value (CRITICAL, NOT_CONFIGURED …) and stays the
    stable contract. ``status_label`` is the Turkish sentence a manager reads. Both are
    sent so the client never has to invent a translation — and so that a client which
    somehow received an unknown status still has something safe to display.
    """
    ingredient_id: int
    ingredient_name: str
    unit: str

    on_hand_quantity: Decimal
    reserved_quantity: Decimal
    available_quantity: Decimal

    critical_quantity: Optional[Decimal] = None
    minimum_quantity: Optional[Decimal] = None
    target_quantity: Optional[Decimal] = None

    status: str
    status_label: str
    # target - available, when a target is configured and available is below it.
    # A SUGGESTION on a screen: it orders nothing, reserves nothing, and names no
    # supplier. See docs/INVENTORY_THRESHOLD_ALERTS.md.
    recommended_restock_quantity: Optional[Decimal] = None

    last_movement_at: Optional[datetime] = None
    threshold_updated_at: Optional[datetime] = None
    threshold_updated_by_user_id: Optional[int] = None

    model_config = ConfigDict(from_attributes=True)


class ThresholdAlertSummary(BaseModel):
    """
    The counts behind the summary cards, computed SERVER-SIDE.

    Deliberately not left to the client. ``total_recommended_restock`` is a sum of
    decimal quantities, and a browser adding JSON number strings is how "0.1 + 0.2"
    ends up on a stock report. The backend does stock arithmetic; the client formats
    what it is given.
    """
    below_reserved: int
    out_of_stock: int
    critical: int
    low: int
    healthy: int
    not_configured: int
    total_recommended_restock: Decimal


class ThresholdAlertListResponse(BaseModel):
    total: int
    summary: ThresholdAlertSummary
    items: list[ThresholdAlertItem]


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
