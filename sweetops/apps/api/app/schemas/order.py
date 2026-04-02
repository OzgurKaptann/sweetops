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
    created_at: datetime
    items: List[OrderItemResponse] = []
