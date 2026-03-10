from sqlalchemy import Column, Integer, ForeignKey, Numeric, DateTime
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from .base import Base

class OrderItemIngredient(Base):
    __tablename__ = "order_item_ingredients"
    id = Column(Integer, primary_key=True, index=True)
    order_item_id = Column(Integer, ForeignKey("order_items.id"))
    ingredient_id = Column(Integer, ForeignKey("ingredients.id"))
    quantity = Column(Integer, default=1)
    price_modifier = Column(Numeric(10, 2), default=0)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    
    order_item = relationship("OrderItem", back_populates="ingredients")
    ingredient = relationship("Ingredient")
