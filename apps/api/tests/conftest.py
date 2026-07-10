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

from app.core.config import settings
from app.core.db import SessionLocal, engine
from app.core.permissions import CANONICAL_ROLES
from app.core.security import hash_password
from app.main import app
from app.models.auth_session import AuthSession
from app.models.ingredient import Ingredient
from app.models.ingredient_stock import IngredientStock, IngredientStockMovement
from app.models.order import Order
from app.models.order_item import OrderItem
from app.models.order_item_ingredient import OrderItemIngredient
from app.models.order_status_event import OrderStatusEvent
from app.models.owner_decision import OwnerDecision
from app.models.role import Role
from app.models.store import Store
from app.models.table import Table
from app.models.table_qr_token import TableQrToken
from app.models.user import User
from app.models.audit_log import AuditLog  # noqa — ensure registered
from app.services import auth_service, qr_token_service

# Store id used by the legacy order path in the pre-existing test suite.
DEFAULT_STORE_ID = 1
DEFAULT_PASSWORD = "testpassw0rd"


# ---------------------------------------------------------------------------
# Async backend contract
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def anyio_backend() -> str:
    """
    Pin every ``@pytest.mark.anyio`` test to the asyncio backend.

    SweetOps is an asyncio-only runtime: it is served by uvicorn's asyncio loop,
    talks to PostgreSQL through the synchronous psycopg2 driver, and its own
    async helpers (WebSocket manager, ``asyncio.run`` in tests) are written
    against asyncio. Trio is not a supported runtime and is not a dependency.

    AnyIO's pytest plugin otherwise parametrises anyio tests across *both*
    asyncio and trio; without trio installed the trio variants fail with
    ``KeyError: 'anyio._backends._trio'``. Returning only "asyncio" makes the
    supported backend explicit and deterministic instead of silently
    advertising a backend the product does not support. See
    docs/TEST_SUITE_BASELINE.md for the full rationale.
    """
    return "asyncio"


# ---------------------------------------------------------------------------
# Legacy order context (non-production transition mode)
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _enable_legacy_order_context():
    """
    Enable the legacy (client-supplied store_id/table_id) order path for the
    test suite. Production defaults this OFF so client-supplied context is never
    trusted; the test environment is explicitly non-production and opts in so
    the pre-existing order/kitchen/audit tests keep exercising the pipeline.
    Tests that must prove the secure default set it back to False themselves.
    """
    original = settings.ALLOW_LEGACY_ORDER_CONTEXT
    settings.ALLOW_LEGACY_ORDER_CONTEXT = True
    try:
        yield
    finally:
        settings.ALLOW_LEGACY_ORDER_CONTEXT = original


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


# ---------------------------------------------------------------------------
# QR token helpers (secure-path tests)
# ---------------------------------------------------------------------------

def make_store_table(
    db: Session,
    *,
    store_name: str | None = None,
    table_number: str | None = None,
) -> tuple[Store, Table]:
    """
    Create a store + table with NO QR token yet.

    Used by tests that need to exercise the first `issue` on a table (the
    one-active-token invariant rejects a second `issue`).
    """
    uid = uuid.uuid4().hex[:8]
    store = Store(
        name=store_name if store_name is not None else f"TestStore_{uid}",
        location="Test",
    )
    db.add(store)
    db.flush()

    table = Table(
        store_id=store.id,
        table_number=table_number if table_number is not None else uid,
        qr_code=f"test-table-{uid}",
    )
    db.add(table)
    db.commit()
    db.refresh(store)
    db.refresh(table)
    return store, table


def make_store_table_token(
    db: Session,
    *,
    store_name: str | None = None,
    table_number: str | None = None,
) -> tuple[Store, Table, TableQrToken, str]:
    """
    Create a store + table and issue one ACTIVE QR token for it.

    Returns (store, table, token_record, raw_token). The raw token is available
    only here (as it would be only at issuance time in production).
    """
    store, table = make_store_table(
        db, store_name=store_name, table_number=table_number
    )
    record, raw = qr_token_service.issue_token(
        db, table.id, created_reason="test", commit=False
    )
    db.commit()
    db.refresh(store)
    db.refresh(table)
    db.refresh(record)
    return store, table, record, raw


def cleanup_store_table(db: Session, store_id: int, table_id: int) -> None:
    """Delete QR tokens, table and store created by make_store_table_token."""
    db.query(TableQrToken).filter(
        TableQrToken.table_id == table_id
    ).delete(synchronize_session=False)
    db.query(Table).filter(Table.id == table_id).delete(synchronize_session=False)
    db.query(Store).filter(Store.id == store_id).delete(synchronize_session=False)
    db.commit()


def qr_order_payload(
    ingredient_id: int,
    qr_token: str,
    *,
    product_id: int = 1,
    idem_key: str | None = None,
) -> tuple[dict, dict]:
    """Order payload that carries only the opaque qr_token (no numeric ids)."""
    headers = {}
    if idem_key:
        headers["Idempotency-Key"] = idem_key
    payload = {
        "qr_token": qr_token,
        "items": [
            {
                "product_id": product_id,
                "quantity": 1,
                "ingredients": [{"ingredient_id": ingredient_id, "quantity": 1}],
            }
        ],
    }
    return payload, headers


# ---------------------------------------------------------------------------
# Staff auth fixtures
# ---------------------------------------------------------------------------

def _ensure_role(db: Session, name: str) -> Role:
    role = db.query(Role).filter(Role.name == name).first()
    if role is None:
        role = Role(name=name)
        db.add(role)
        db.commit()
        db.refresh(role)
    return role


@pytest.fixture()
def ensure_roles(db: Session):
    """Ensure the canonical staff roles exist (idempotent)."""
    for name in CANONICAL_ROLES:
        _ensure_role(db, name)
    return CANONICAL_ROLES


@pytest.fixture()
def make_staff(db: Session):
    """
    Factory: create a staff User with a hashed password. Tracks created users
    and sessions and cleans them up afterwards.
    """
    created_user_ids: list[int] = []

    def _make(
        role_name: str,
        *,
        store_id: int | None = DEFAULT_STORE_ID,
        username: str | None = None,
        password: str = DEFAULT_PASSWORD,
        is_active: bool = True,
    ) -> User:
        role = _ensure_role(db, role_name)
        uname = username if username is not None else f"user_{uuid.uuid4().hex[:10]}"
        user = User(
            username=uname,
            password_hash=hash_password(password),
            role_id=role.id,
            store_id=store_id,
            is_active=is_active,
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        created_user_ids.append(user.id)
        return user

    yield _make

    for uid in created_user_ids:
        db.query(AuthSession).filter(AuthSession.user_id == uid).delete(synchronize_session=False)
        db.query(User).filter(User.id == uid).delete(synchronize_session=False)
    db.commit()


def make_authed_client(db: Session, user: User) -> TestClient:
    """
    Build a TestClient carrying a valid session for `user`. The raw CSRF token is
    preset as the default X-CSRF-Token header so state-changing calls pass the
    double-submit check. Tests that exercise CSRF rejection craft their own.
    """
    _session, raw_token, raw_csrf = auth_service.create_session(db, user)
    client = TestClient(app)
    client.cookies.set(settings.SESSION_COOKIE_NAME, raw_token)
    client.cookies.set(settings.CSRF_COOKIE_NAME, raw_csrf)
    client.headers.update({"X-CSRF-Token": raw_csrf})
    return client


@pytest.fixture()
def make_store(db: Session):
    """
    Factory: create an extra Store for multi-store isolation tests. On teardown
    it removes everything anchored to that store (sessions, users, decisions,
    order chain) so foreign keys never block cleanup.
    """
    created_ids: list[int] = []

    def _make(name: str | None = None) -> Store:
        store = Store(
            name=name if name is not None else f"Store_{uuid.uuid4().hex[:8]}",
            location="Test",
        )
        db.add(store)
        db.commit()
        db.refresh(store)
        created_ids.append(store.id)
        return store

    yield _make

    for sid in created_ids:
        user_ids = [u.id for u in db.query(User).filter(User.store_id == sid).all()]
        if user_ids:
            db.query(AuthSession).filter(AuthSession.user_id.in_(user_ids)).delete(synchronize_session=False)
            db.query(User).filter(User.id.in_(user_ids)).delete(synchronize_session=False)
        db.query(OwnerDecision).filter(OwnerDecision.store_id == sid).delete(synchronize_session=False)
        order_ids = [o.id for o in db.query(Order).filter(Order.store_id == sid).all()]
        if order_ids:
            oi_ids = [oi.id for oi in db.query(OrderItem).filter(OrderItem.order_id.in_(order_ids)).all()]
            if oi_ids:
                db.query(OrderItemIngredient).filter(
                    OrderItemIngredient.order_item_id.in_(oi_ids)
                ).delete(synchronize_session=False)
                db.query(OrderItem).filter(OrderItem.id.in_(oi_ids)).delete(synchronize_session=False)
            db.query(OrderStatusEvent).filter(
                OrderStatusEvent.order_id.in_(order_ids)
            ).delete(synchronize_session=False)
            db.query(Order).filter(Order.id.in_(order_ids)).delete(synchronize_session=False)
        db.query(Store).filter(Store.id == sid).delete(synchronize_session=False)
    db.commit()


@pytest.fixture()
def owner_client(db: Session, make_staff) -> TestClient:
    user = make_staff("OWNER", store_id=DEFAULT_STORE_ID)
    return make_authed_client(db, user)


@pytest.fixture()
def kitchen_client(db: Session, make_staff) -> TestClient:
    user = make_staff("KITCHEN", store_id=DEFAULT_STORE_ID)
    return make_authed_client(db, user)
