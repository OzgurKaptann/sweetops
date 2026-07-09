from sqlalchemy import Boolean, Column, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.sql import func

from .base import Base


class OwnerDecision(Base):
    """
    Persistent record of every owner decision signal.

    Created on first detection; updated on re-evaluation (mutable signal fields),
    lifecycle transitions (status, actor_id, timestamps), and cooldown resets.

    The decision_id is the stable natural key emitted by the signal functions
    (e.g. "stock_risk_42", "sla_risk_current"). Because the same natural key can
    legitimately recur in different stores, the primary key is COMPOSITE
    (store_id, decision_id): decisions never collide across stores and every
    lookup/mutation is scoped to the authenticated store.
    """
    __tablename__ = "owner_decisions"

    # Store scope — part of the composite primary key.
    store_id = Column(
        Integer,
        ForeignKey("stores.id"),
        primary_key=True,
        nullable=False,
        index=True,
    )

    # Natural / stable key — second half of the composite primary key.
    decision_id = Column(String(128), primary_key=True, nullable=False)

    # Signal classification
    type       = Column(String(40), nullable=False)   # stock_risk | demand_spike | …
    severity   = Column(String(10), nullable=False)   # high | medium | low

    # Prioritization
    decision_score          = Column(Float, nullable=False, default=0.0)
    blocking_vs_non_blocking = Column(Boolean, nullable=False, default=False)

    # Human-readable payload (updated on each re-evaluation)
    title              = Column(String(200), nullable=False)
    description        = Column(Text, nullable=False)
    impact             = Column(Text, nullable=False)
    recommended_action = Column(Text, nullable=False)
    why_now            = Column(Text, nullable=False)
    expected_impact    = Column(Text, nullable=False)
    data               = Column(JSONB, nullable=True)

    # Lifecycle
    status          = Column(String(20), nullable=False, default="pending")
    acknowledged_at = Column(DateTime(timezone=True), nullable=True)
    completed_at    = Column(DateTime(timezone=True), nullable=True)
    actor_id        = Column(String(64), nullable=True)
    resolution_note = Column(Text, nullable=True)

    # Outcome tracking (set when action=complete)
    resolution_quality      = Column(String(20), nullable=True)   # good | partial | failed
    estimated_revenue_saved = Column(Float, nullable=True)        # ₺ saved by acting

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
