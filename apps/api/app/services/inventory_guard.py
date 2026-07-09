"""
Global-inventory single-store guard.

The ingredient / ingredient_stock tables are GLOBAL in the current schema — they
carry no store_id. Any endpoint or signal that reads them can therefore only be
trusted while exactly one operational store exists. When more than one
operational store exists we must FAIL CLOSED rather than show one store's global
inventory to another store's staff.

The proper fix (adding store_id to inventory + stock movements) is deferred to a
dedicated branch: refactor/store-scoped-inventory.
"""
from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core import messages
from app.models.user import User


def operational_store_count(db: Session) -> int:
    """Number of distinct stores that have at least one staff user assigned."""
    return (
        db.query(func.count(func.distinct(User.store_id)))
        .filter(User.store_id.isnot(None))
        .scalar()
        or 0
    )


def is_single_operational_store(db: Session) -> bool:
    return operational_store_count(db) <= 1


def assert_single_operational_store(db: Session) -> None:
    """
    Raise a structured Turkish 409 when global inventory cannot be safely served
    because more than one operational store exists.
    """
    if not is_single_operational_store(db):
        raise HTTPException(
            status_code=409,
            detail={
                "error": "inventory_not_store_scoped",
                "message": messages.INVENTORY_MULTISTORE_BLOCKED,
            },
        )
