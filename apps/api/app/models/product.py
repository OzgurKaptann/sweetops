from sqlalchemy import Column, Integer, String, Numeric, DateTime, Boolean
from sqlalchemy.sql import func
from .base import Base

class Product(Base):
    """
    Catalog definition of something the chain can sell.

    ``is_active`` retires an item CHAIN-WIDE; it does not publish one. A guest
    only ever sees a product that some branch has explicitly offered — see
    ``StoreProduct`` in store_product.py. An inactive product is excluded from
    every customer menu and refused at order creation even where a stale
    offering row still points at it.
    """
    __tablename__ = "products"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, index=True)
    category = Column(String, index=True)
    base_price = Column(Numeric(10, 2))
    is_active = Column(Boolean, nullable=False, default=True, index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
