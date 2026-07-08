"""
Test fixtures for SweetOps backend tests.

Design principles:
  - Every fixture creates its own data and cleans up after itself.
  - No shared mutable state between tests.
  - DB is the real PostgreSQL — this validates actual locking behaviour.
  - `db` fixture provides a real session (no automatic rollback) so that
    concurrency tests can commit and see each other's writes.
  - Cleanup always runs even if the test fails (yield + explicit delete).
"""
import uuid
from decimal import Decimal
from typing import Generator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.db import SessionLocal, engine
from app.main import app
from app.models.ingredient import Ingredient
from app.models.ingredient_stock import IngredientStock, IngredientStockMovement
from app.models.order import Order
from app.models.order_item import OrderItem
from app.models.order_item_ingredient import OrderItemIngredient
from app.models.order_status_event import OrderStatusEvent
from app.models.audit_log import AuditLog  # noqa — ensure registered


# ---------------------------------------------------------------------------
# Core fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def db() -> Generator[Session, None, None]:
    """
    Real database session.  Does NOT auto-rollback so concurrency tests
    can commit and observe concurrent writes.
    Caller is responsible for cleaning up test data.
    """
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture()
def client() -> TestClient:
    """
    ASGI test client wrapping the real FastAPI app.
    Thread-safe — multiple threads may call it concurrently.
    """
    return TestClient(app)


# ---------------------------------------------------------------------------
# Data helpers
# ---------------------------------------------------------------------------

def make_ingredient(
    db: Session,
    *,
    stock_quantity: Decimal,
    standard_quantity: Decimal = Decimal("10.00"),
    price: Decimal = Decimal("5.00"),
    unit: str = "g",
    name: str | None = None,
) -> tuple[Ingredient, IngredientStock]:
    """
    Create a test ingredient + stock row.  Returns both ORM objects.
    The caller must call db.commit() after this if needed.
    """
    uid = uuid.uuid4().hex[:8]
    ing = Ingredient(
        name=name if name is not None else f"TestIng_{uid}",
        category="Test",
        price=price,
        unit=unit,
        standard_quantity=standard_quantity,  # grams consumed per selection
        is_active=True,
    )
    db.add(ing)
    db.flush()

    stock = IngredientStock(
        ingredient_id=ing.id,
        stock_quantity=stock_quantity,
        unit=unit,
        reorder_level=Decimal("5.00"),
    )
    db.add(stock)
    db.commit()
    db.refresh(ing)
    db.refresh(stock)
    return ing, stock


def cleanup_ingredient(db: Session, ingredient_id: int) -> None:
    """
    Delete all test data associated with an ingredient.
    Respects FK order: movements → stock → order chain → ingredient.
    """
    # Stock movements
    db.query(IngredientStockMovement).filter(
        IngredientStockMovement.ingredient_id == ingredient_id
    ).delete(synchronize_session=False)

    # Find all OrderItemIngredient rows for this ingredient
    oii_ids = [
        row.id
        for row in db.query(OrderItemIngredient)
        .filter(OrderItemIngredient.ingredient_id == ingredient_id)
        .all()
    ]

    # Collect affected order_item ids
    oi_ids = list({
        row.order_item_id
        for row in db.query(OrderItemIngredient)
        .filter(OrderItemIngredient.ingredient_id == ingredient_id)
        .all()
    })

    # Collect affected order ids
    order_ids = list({
        row.order_id
        for row in db.query(OrderItem).filter(OrderItem.id.in_(oi_ids)).all()
    }) if oi_ids else []

    # Delete in FK order
    if oii_ids:
        db.query(OrderItemIngredient).filter(
            OrderItemIngredient.id.in_(oii_ids)
        ).delete(synchronize_session=False)

    if oi_ids:
        db.query(OrderItem).filter(
            OrderItem.id.in_(oi_ids)
        ).delete(synchronize_session=False)

    if order_ids:
        db.query(OrderStatusEvent).filter(
            OrderStatusEvent.order_id.in_(order_ids)
        ).delete(synchronize_session=False)
        db.query(Order).filter(
            Order.id.in_(order_ids)
        ).delete(synchronize_session=False)

    # Stock + ingredient
    db.query(IngredientStock).filter(
        IngredientStock.ingredient_id == ingredient_id
    ).delete(synchronize_session=False)

    db.query(Ingredient).filter(
        Ingredient.id == ingredient_id
    ).delete(synchronize_session=False)

    db.commit()


def order_payload(
    ingredient_id: int,
    *,
    store_id: int = 1,
    table_id: int = 1,
    product_id: int = 1,
    idem_key: str | None = None,
) -> tuple[dict, dict]:
    """
    Returns (payload_dict, headers_dict) for a single-ingredient order.
    """
    headers = {}
    if idem_key:
        headers["Idempotency-Key"] = idem_key

    payload = {
        "store_id": store_id,
        "table_id": table_id,
        "items": [
            {
                "product_id": product_id,
                "quantity": 1,
                "ingredients": [{"ingredient_id": ingredient_id, "quantity": 1}],
            }
        ],
    }
    return payload, headers
