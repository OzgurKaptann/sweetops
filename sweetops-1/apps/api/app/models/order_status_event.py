from sqlalchemy import Column, Integer, String, ForeignKey, DateTime
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from .base import Base


class OrderStatusEvent(Base):
    __tablename__ = "order_status_events"

    id = Column(Integer, primary_key=True, index=True)
    order_id = Column(Integer, ForeignKey("orders.id"), nullable=False, index=True)
    status_from = Column(String, nullable=True)
    status_to = Column(String, nullable=False)

    # Who made this transition
    actor_type = Column(String(20), nullable=True)   # CUSTOMER | STAFF | SYSTEM
    actor_id = Column(String(64), nullable=True)     # session_id or staff identifier

    # Device clock at time of tap (for lag detection)
    client_timestamp = Column(DateTime(timezone=True), nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    order = relationship("Order", back_populates="status_events")
