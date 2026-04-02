from sqlalchemy import Column, Integer, String, ForeignKey, Numeric, DateTime
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from .base import Base

class IngredientStock(Base):
    __tablename__ = "ingredient_stock"
    id = Column(Integer, primary_key=True, index=True)
    ingredient_id = Column(Integer, ForeignKey("ingredients.id"), unique=True, nullable=False)
    stock_quantity = Column(Numeric(10, 2), nullable=False, default=0)
    unit = Column(String(10), nullable=False)
    reorder_level = Column(Numeric(10, 2), nullable=True)
    last_restocked = Column(DateTime(timezone=True), nullable=True)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    ingredient = relationship("Ingredient")


class IngredientStockMovement(Base):
    __tablename__ = "ingredient_stock_movements"
    id = Column(Integer, primary_key=True, index=True)
    ingredient_id = Column(Integer, ForeignKey("ingredients.id"), nullable=False, index=True)
    movement_type = Column(String(30), nullable=False)  # ORDER_DEDUCTION, RESTOCK, MANUAL_ADJUST, CANCELLATION_RETURN, WASTE
    quantity_delta = Column(Numeric(10, 2), nullable=False)  # negative = consumed, positive = restocked
    unit = Column(String(10), nullable=False)
    reference_type = Column(String(30), nullable=True)  # 'order', 'manual', etc.
    reference_id = Column(Integer, nullable=True)
    note = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    ingredient = relationship("Ingredient")
