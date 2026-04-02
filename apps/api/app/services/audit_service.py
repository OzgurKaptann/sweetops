"""
Audit Service — append-only forensic trail for every mutation.

Usage:
    audit(db, entity_type="order", entity_id=42, action="created",
          actor_type="CUSTOMER", payload_after={...})
"""
import logging
from typing import Any
from sqlalchemy.orm import Session
from app.models.audit_log import AuditLog

logger = logging.getLogger(__name__)


def audit(
    db: Session,
    entity_type: str,
    entity_id: int,
    action: str,
    actor_type: str | None = None,
    actor_id: str | None = None,
    payload_before: dict | None = None,
    payload_after: dict | None = None,
    ip_address: str | None = None,
) -> None:
    """
    Write one audit record. Never raises — audit failures must not
    block business operations.
    """
    try:
        entry = AuditLog(
            entity_type=entity_type,
            entity_id=entity_id,
            action=action,
            actor_type=actor_type,
            actor_id=actor_id,
            payload_before=_sanitize(payload_before),
            payload_after=_sanitize(payload_after),
            ip_address=ip_address,
        )
        db.add(entry)
        # Flush only — caller owns the transaction commit
        db.flush()
    except Exception as exc:
        # Log but never propagate — audit must not break business logic
        logger.error("audit_write_failed entity=%s id=%s action=%s err=%s",
                     entity_type, entity_id, action, exc)


def _sanitize(payload: Any) -> dict | None:
    """Convert non-JSON-serializable types (Decimal, datetime) to safe types."""
    if payload is None:
        return None
    if isinstance(payload, dict):
        return {k: _sanitize_value(v) for k, v in payload.items()}
    return payload


def _sanitize_value(v: Any) -> Any:
    from decimal import Decimal
    from datetime import datetime
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, datetime):
        return v.isoformat()
    return v
