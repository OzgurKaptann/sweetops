from sqlalchemy import (
    Column,
    Integer,
    String,
    ForeignKey,
    DateTime,
    Boolean,
)
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from .base import Base


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String, unique=True, index=True, nullable=False)
    password_hash = Column(String, nullable=True)
    role_id = Column(Integer, ForeignKey("roles.id"), nullable=False)
    store_id = Column(Integer, ForeignKey("stores.id"), nullable=True)

    # ── Security / lifecycle fields ──────────────────────────────────────────
    is_active = Column(Boolean, nullable=False, server_default="true")
    failed_login_count = Column(Integer, nullable=False, server_default="0")
    locked_until = Column(DateTime(timezone=True), nullable=True)
    last_login_at = Column(DateTime(timezone=True), nullable=True)
    password_changed_at = Column(DateTime(timezone=True), nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )

    role = relationship("Role", back_populates="users")
    store = relationship("Store", back_populates="users")
    sessions = relationship(
        "AuthSession",
        back_populates="user",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
