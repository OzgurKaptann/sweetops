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

class OrderItemIngredientCreate(BaseModel):
    ingredient_id: int
    quantity: int = 1

class OrderItemCreate(BaseModel):
    product_id: int
    quantity: int = 1
    ingredients: List[OrderItemIngredientCreate] = []

class OrderCreateRequest(BaseModel):
    store_id: int
    table_id: Optional[int] = None
    items: List[OrderItemCreate]

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
