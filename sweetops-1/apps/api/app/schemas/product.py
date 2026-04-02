from .common import BaseSchema
from decimal import Decimal

class ProductResponse(BaseSchema):
    id: int
    name: str
    category: str
    base_price: Decimal
