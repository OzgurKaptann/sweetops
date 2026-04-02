from .common import BaseSchema
from decimal import Decimal

class IngredientResponse(BaseSchema):
    id: int
    name: str
    category: str
    price: Decimal
