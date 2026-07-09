from sqlalchemy import (
    Column,
    Integer,
    String,
    ForeignKey,
    DateTime,
)
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from .base import Base


class AuthSession(Base):
    """
    Opaque server-side staff session.

    The raw session token and raw CSRF token are NEVER stored. Only their
    deterministic SHA-256 hashes are persisted here. The raw session token lives
    solely in the user's HttpOnly cookie; the raw CSRF token lives in a
    non-HttpOnly cookie (double-submit) and is echoed back in X-CSRF-Token.

    Revoked rows are retained (not deleted) for short-term forensic history —
    revoked_at / revoked_reason record why a session ended.
    """

    __tablename__ = "auth_sessions"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # SHA-256 hex digests — never the raw values.
    token_hash = Column(String(64), nullable=False, unique=True, index=True)
    csrf_token_hash = Column(String(64), nullable=False)

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    expires_at = Column(DateTime(timezone=True), nullable=False, index=True)
    last_seen_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    revoked_at = Column(DateTime(timezone=True), nullable=True)
    revoked_reason = Column(String(64), nullable=True)

    # Optional safe metadata: hash of the user-agent, never the raw string.
    user_agent_hash = Column(String(64), nullable=True)

    user = relationship("User", back_populates="sessions")
