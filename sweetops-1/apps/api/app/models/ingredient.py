from sqlalchemy import Column, Integer, String, Numeric, DateTime, Boolean
from sqlalchemy.sql import func
from .base import Base

class Ingredient(Base):
    __tablename__ = "ingredients"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, index=True)
    category = Column(String, index=True)
    price = Column(Numeric(10, 2), default=0)
    unit = Column(String(10), nullable=False, default="g")
    standard_quantity = Column(Numeric(8, 2), nullable=False, default=0)
    cost_per_unit = Column(Numeric(8, 4), nullable=True)
    shelf_life_days = Column(Integer, nullable=True)
    allows_portion_choice = Column(Boolean, nullable=False, default=False)
    is_active    = Column(Boolean, nullable=False, default=True)
    is_promoted  = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
