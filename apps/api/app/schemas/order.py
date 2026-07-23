from pydantic import BaseModel, Field
from typing import List, Optional
from datetime import datetime
from decimal import Decimal
from .common import BaseSchema
from enum import Enum

class OrderStatusEnum(str, Enum):
    NEW = "NEW"
    IN_PREP = "IN_PREP"
    READY = "READY"
    DELIVERED = "DELIVERED"
    CANCELLED = "CANCELLED"

# ── Customer order bounds ───────────────────────────────────────────────────
# A guest ordering from a table is not an operator placing a wholesale order.
# Unbounded quantities were reachable from the public endpoint: `quantity` was a
# bare `int` with a default, so 0, -3 and 10_000_000 all validated. A negative
# multiplied straight through calculate_consumed_quantity into a NEGATIVE stock
# requirement — a "sale" that RELEASES stock and reduces the bill.
#
# These bounds are the outer edge of what a table can plausibly order in one
# submission, not a UI preference: the customer app offers a smaller range still
# (MAX_QUANTITY in order-selection.ts). A shop that genuinely needs 30 of one
# item raises the constant deliberately; nothing here should be the thing that
# silently allows it.
MAX_ITEM_QUANTITY = 20        # portions of one product on one line
MAX_INGREDIENT_PORTIONS = 5   # portions of one ingredient on one product
MAX_ORDER_ITEMS = 20          # lines in one submission


class OrderItemIngredientCreate(BaseModel):
    ingredient_id: int
    quantity: int = Field(1, ge=1, le=MAX_INGREDIENT_PORTIONS)

class OrderItemCreate(BaseModel):
    product_id: int
    quantity: int = Field(1, ge=1, le=MAX_ITEM_QUANTITY)
    ingredients: List[OrderItemIngredientCreate] = []

class OrderCreateRequest(BaseModel):
    # Opaque QR token — the trusted source of store/table context. The backend
    # resolves it server-side and derives store_id/table_id from it; any
    # client-supplied store_id/table_id below are IGNORED whenever qr_token is
    # present. The legacy fields remain only for the non-production transition
    # mode (settings.ALLOW_LEGACY_ORDER_CONTEXT) and are never trusted in prod.
    qr_token: Optional[str] = None
    store_id: Optional[int] = None
    table_id: Optional[int] = None
    items: List[OrderItemCreate] = Field(..., min_length=1, max_length=MAX_ORDER_ITEMS)

class OrderCreatedResponse(BaseSchema):
    order_id: int
    store_id: int
    table_id: Optional[int]
    status: str
    created_at: datetime
    item_count: int
    total_amount: Decimal

class StatusUpdateRequest(BaseModel):
    status: OrderStatusEnum

class OrderItemIngredientResponse(BaseSchema):
    id: int
    ingredient_id: int
    ingredient_name: Optional[str] = None
    quantity: int

class OrderItemResponse(BaseSchema):
    id: int
    product_id: int
    product_name: Optional[str] = None
    quantity: int
    ingredients: List[OrderItemIngredientResponse] = []

class OrderListResponse(BaseSchema):
    id: int
    store_id: int
    table_id: Optional[int]
    status: str
    created_at: str               # UTC ISO-8601, e.g. "2026-04-01T10:00:00+00:00"
    computed_age_minutes: float
    priority_score: float
    sla_severity: str             # "ok" | "warning" | "critical"
    should_be_started: bool
    urgency_reason: str
    action_hint: str
    items: List[OrderItemResponse] = []


class KitchenLoadResponse(BaseSchema):
    load_level: str               # "low" | "medium" | "high"
    active_orders_count: int
    in_prep_count: int
    average_age_minutes: float
    explanation: str


class BatchingSuggestion(BaseSchema):
    grouped_order_ids: List[int]
    shared_ingredients: List[str]
    estimated_time_saved: str     # e.g. "60s"


class KitchenDashboardResponse(BaseSchema):
    orders: List[OrderListResponse]
    kitchen_load: KitchenLoadResponse
    batching_suggestions: List[BatchingSuggestion]
