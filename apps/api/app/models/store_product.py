"""
Which branch offers which product — the customer menu's publication boundary.

``products`` is CATALOG: the definition of a thing the chain can sell. It says
nothing about whether any branch actually sells it, and a row can get there by
routes that were never a menu decision (a seed, an import, an interrupted test
run). A customer-facing list built from the catalog therefore shows whatever
happens to be in the table.

A row HERE is a decision: "branch X offers product Y to guests." The customer
menu is built from these rows and nothing else, so a product nobody published is
invisible to every guest — not because its name was filtered out, but because
there is no relationship connecting it to the branch the guest is sitting in.

  is_available  the branch sells this, but not right now (sold out for the day).
                Distinct from deleting the row, which would mean "we stopped
                selling this", and from products.is_active, which retires the
                item chain-wide.
  sort_order    menu order within the branch. Ties broken by name so the list is
                deterministic.

There is deliberately no price column: a branch publishes the chain's product at
the chain's price. Per-branch pricing is a separate, larger decision (P1-B) —
see docs/CUSTOMER_MENU_SCOPING.md.
"""
from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from .base import Base


class StoreProduct(Base):
    __tablename__ = "store_products"
    __table_args__ = (
        UniqueConstraint(
            "store_id", "product_id", name="uq_store_products_store_product"
        ),
    )

    id = Column(Integer, primary_key=True, index=True)
    store_id = Column(Integer, ForeignKey("stores.id"), nullable=False, index=True)
    product_id = Column(
        Integer, ForeignKey("products.id"), nullable=False, index=True
    )
    is_available = Column(Boolean, nullable=False, default=True)
    sort_order = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    product = relationship("Product")
