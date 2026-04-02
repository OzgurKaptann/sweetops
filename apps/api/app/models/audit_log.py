from sqlalchemy import Column, BigInteger, String, DateTime, JSON
from sqlalchemy.sql import func
from .base import Base


class AuditLog(Base):
    """
    Append-only forensic trail for every mutation in the system.
    Never updated, never deleted.
    """
    __tablename__ = "sweetops_audit_log"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    entity_type = Column(String(50), nullable=False)    # 'order', 'ingredient_stock', ...
    entity_id = Column(BigInteger, nullable=False)
    action = Column(String(50), nullable=False)          # 'created', 'status_changed', 'stock_deducted'
    actor_type = Column(String(20), nullable=True)       # CUSTOMER | STAFF | SYSTEM
    actor_id = Column(String(64), nullable=True)
    payload_before = Column(JSON, nullable=True)         # state before mutation
    payload_after = Column(JSON, nullable=True)          # state after mutation
    ip_address = Column(String(45), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
