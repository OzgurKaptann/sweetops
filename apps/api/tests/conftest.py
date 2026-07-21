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
from contextlib import contextmanager
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
from app.models.ingredient_stock import (
    IngredientStock,
    IngredientStockMovement,
    OrderInventoryLine,
)
from app.models.inventory_stock_count import InventoryStockCount
from app.models.inventory_threshold import InventoryThresholdUpdate
from app.models.inventory_transfer import InventoryTransfer
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
from app.models.payment_settlement import PaymentSettlement
from app.models.payment_allocation import PaymentAllocation
from app.models.payment_refund import PaymentRefund
from app.models.cashier_shift import CashierShift
from app.models.order_issue import OrderIssue
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
    on_hand: Decimal,
    reserved: Decimal = Decimal("0"),
    standard_quantity: Decimal = Decimal("10.00"),
    price: Decimal = Decimal("5.00"),
    unit: str = "g",
    name: str | None = None,
    store_id: int = DEFAULT_STORE_ID,
) -> tuple[Ingredient, IngredientStock]:
    """
    Create a test ingredient (catalog, global) + ONE store's stock row for it.

    ``on_hand`` is PHYSICAL stock; ``reserved`` is stock already promised to
    accepted orders. available_quantity is generated by the database as
    on_hand - reserved, so it is never passed in.

    ``store_id`` defaults to the store the pre-existing suite orders from, so
    single-store tests read exactly as before. Multi-store tests pass a second
    store and use ``stock_for`` below to give the SAME catalog ingredient an
    independent quantity there.
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
        store_id=store_id,
        ingredient_id=ing.id,
        on_hand_quantity=on_hand,
        reserved_quantity=reserved,
        unit=unit,
        reorder_level=Decimal("5.00"),
    )
    db.add(stock)
    db.commit()
    db.refresh(ing)
    db.refresh(stock)
    return ing, stock


def stock_for(
    db: Session,
    ingredient: Ingredient,
    store_id: int,
    *,
    on_hand: Decimal,
    reserved: Decimal = Decimal("0"),
) -> IngredientStock:
    """
    Give an EXISTING catalog ingredient its own independent stock in another store.

    This is the fixture that makes the central property of store-scoped inventory
    testable: one ingredient, two stores, two unrelated quantities. Store A can be
    sold out of the very same pistachio that Store B has 5 kg of.
    """
    stock = IngredientStock(
        store_id=store_id,
        ingredient_id=ingredient.id,
        on_hand_quantity=on_hand,
        reserved_quantity=reserved,
        unit=ingredient.unit,
        reorder_level=Decimal("5.00"),
    )
    db.add(stock)
    db.commit()
    db.refresh(stock)
    return stock


# The inventory ledger is append-only: a trigger (migration c3b7e01f9a24) refuses
# UPDATE and DELETE with NO runtime bypass — no GUC or session variable can turn
# it off, exactly as for the payment ledger. Test teardown must remove the rows
# it committed, so it uses the only sanctioned escape hatch: ownership-gated DDL.
# ALTER TABLE ... DISABLE TRIGGER requires table ownership (the migration/test
# role owns the table) and is unreachable from ordinary application DML or an
# SQL-injection path, so it is not a production bypass. The trigger is restored
# before the transaction commits.
#
# inventory_stock_counts is append-only for the same reason and by the same
# function: a count that was got wrong is superseded by counting again, never
# edited, so today's manager cannot rewrite what yesterday's manager says they saw
# on the shelf.
#
# inventory_threshold_updates is append-only for the same reason and by the same
# function: a threshold decision that was got wrong is superseded by making another
# one, never edited — otherwise today's manager could rewrite what yesterday's manager
# decided, which is exactly what somebody quietly disarming an alert would want to do.
_INVENTORY_TRIGGERS = (
    ("ingredient_stock_movements", "trg_ingredient_stock_movements_immutable"),
    ("inventory_stock_counts", "trg_inventory_stock_counts_immutable"),
    ("inventory_threshold_updates", "trg_inventory_threshold_updates_immutable"),
)


@contextmanager
def _inventory_maintenance(db: Session):
    """Ownership-gated teardown escape hatch — see note above."""
    from sqlalchemy import text
    for table, trig in _INVENTORY_TRIGGERS:
        db.execute(text(f"ALTER TABLE {table} DISABLE TRIGGER {trig}"))
    try:
        yield
    finally:
        for table, trig in _INVENTORY_TRIGGERS:
            db.execute(text(f"ALTER TABLE {table} ENABLE TRIGGER {trig}"))


def purge_inventory_for_orders(db: Session, order_ids: list[int]) -> None:
    """
    Remove the inventory rows anchored to these orders (movements → lines).

    Must run before the orders themselves are deleted: movements and inventory
    lines both hold FKs to orders and order_items.
    """
    if not order_ids:
        return
    with _inventory_maintenance(db):
        db.query(IngredientStockMovement).filter(
            IngredientStockMovement.order_id.in_(order_ids)
        ).delete(synchronize_session=False)
    db.query(OrderInventoryLine).filter(
        OrderInventoryLine.order_id.in_(order_ids)
    ).delete(synchronize_session=False)


# An order issue is guarded by a trigger (migration e7f2a9c04d18) that refuses every
# DELETE and every UPDATE of a resolved issue — with NO runtime bypass, exactly like
# the ledger. Teardown removes committed issue rows through the same ownership-gated
# escape hatch: ALTER TABLE ... DISABLE TRIGGER requires table ownership and is
# unreachable from application DML or an injection path, so it is not a production
# bypass. The trigger is restored before the transaction commits.
_ORDER_ISSUE_TRIGGER = ("order_issues", "trg_order_issues_guard")


@contextmanager
def _order_issue_maintenance(db: Session):
    """Ownership-gated teardown escape hatch for order_issues."""
    from sqlalchemy import text
    table, trig = _ORDER_ISSUE_TRIGGER
    db.execute(text(f"ALTER TABLE {table} DISABLE TRIGGER {trig}"))
    try:
        yield
    finally:
        db.execute(text(f"ALTER TABLE {table} ENABLE TRIGGER {trig}"))


def purge_order_issues_for_orders(db: Session, order_ids: list[int]) -> None:
    """
    Delete the order issues raised against these orders, breaking the circular link
    with payment_refunds first (order_issues.refund_id ↔ payment_refunds.order_issue_id).

    Must run BEFORE the payment ledger of these orders is deleted: an issue's
    refund_id FK would otherwise block the refund delete.
    """
    if not order_ids:
        return
    with _ledger_maintenance(db):
        db.query(PaymentRefund).filter(
            PaymentRefund.order_id.in_(order_ids)
        ).update({"order_issue_id": None}, synchronize_session=False)
    with _order_issue_maintenance(db):
        db.query(OrderIssue).filter(
            OrderIssue.order_id.in_(order_ids)
        ).delete(synchronize_session=False)
    db.commit()


def purge_order_issues_for_store(db: Session, store_id: int) -> None:
    """Delete every order issue for a store (needed before its refunds/orders go)."""
    with _ledger_maintenance(db):
        db.query(PaymentRefund).filter(
            PaymentRefund.store_id == store_id
        ).update({"order_issue_id": None}, synchronize_session=False)
    with _order_issue_maintenance(db):
        db.query(OrderIssue).filter(
            OrderIssue.store_id == store_id
        ).delete(synchronize_session=False)
    db.commit()


def cleanup_orders(db: Session, order_ids: list[int]) -> None:
    """
    Delete whole orders (issues → inventory → payment → ingredients → items → events).

    Needed whenever ONE order references SEVERAL ingredients: the per-ingredient
    cleanup_ingredient deletes order_items for its own ingredient, which would
    trip the order_item_ingredients FK while another ingredient still points at
    the same order_item. Remove the order graph up front and that hazard is gone.
    """
    if not order_ids:
        return
    purge_order_issues_for_orders(db, order_ids)
    purge_inventory_for_orders(db, order_ids)
    purge_payments_for_orders(db, order_ids)

    item_ids = [
        r.id for r in db.query(OrderItem).filter(OrderItem.order_id.in_(order_ids)).all()
    ]
    if item_ids:
        db.query(OrderItemIngredient).filter(
            OrderItemIngredient.order_item_id.in_(item_ids)
        ).delete(synchronize_session=False)
        db.query(OrderItem).filter(
            OrderItem.id.in_(item_ids)
        ).delete(synchronize_session=False)
    db.query(OrderStatusEvent).filter(
        OrderStatusEvent.order_id.in_(order_ids)
    ).delete(synchronize_session=False)
    db.query(Order).filter(Order.id.in_(order_ids)).delete(synchronize_session=False)
    db.commit()


def purge_inventory_for_store(db: Session, store_id: int) -> None:
    """
    Remove every inventory row belonging to a store, in FK order.

    Needed by the ``make_store`` teardown for two reasons:

      * movements FK to users(store_id, actor_user_id), so a manual adjustment
        made by that store's manager pins the user row, which pins the store;
      * ingredient_stock FKs to stores, so a leftover stock row pins the store
        outright.

    It also keeps the test database single-store AT REST, which is what lets the
    migration downgrade test run: downgrading to a global-stock schema refuses,
    correctly, while a second store still holds stock.
    """
    # A transfer's two legs live in TWO different stores, and both FK to the
    # transfer row. Deleting only THIS store's movements would leave the
    # counterparty's leg pointing at a transfer that is about to go, so every
    # transfer this store took part in is removed whole — both legs, then the
    # transfer — regardless of which side of it this store was on.
    transfer_ids = [
        row.id
        for row in db.query(InventoryTransfer.id)
        .filter(
            (InventoryTransfer.source_store_id == store_id)
            | (InventoryTransfer.destination_store_id == store_id)
        )
        .all()
    ]

    with _inventory_maintenance(db):
        if transfer_ids:
            db.query(IngredientStockMovement).filter(
                IngredientStockMovement.transfer_id.in_(transfer_ids)
            ).delete(synchronize_session=False)
        db.query(IngredientStockMovement).filter(
            IngredientStockMovement.store_id == store_id
        ).delete(synchronize_session=False)

        # Stock counts, once the movements that FK to them are gone. Unlike a
        # transfer, a count lives entirely in ONE store, so this store's counts are
        # exactly the ones to remove.
        db.query(InventoryStockCount).filter(
            InventoryStockCount.store_id == store_id
        ).delete(synchronize_session=False)

        # Threshold decisions. They FK to this store's users and to its stock rows, so
        # they pin both. No movement FKs to them (a threshold writes none), so they can
        # go at any point once the trigger is off.
        db.query(InventoryThresholdUpdate).filter(
            InventoryThresholdUpdate.store_id == store_id
        ).delete(synchronize_session=False)

    if transfer_ids:
        db.query(InventoryTransfer).filter(
            InventoryTransfer.id.in_(transfer_ids)
        ).delete(synchronize_session=False)

    db.query(OrderInventoryLine).filter(
        OrderInventoryLine.store_id == store_id
    ).delete(synchronize_session=False)

    # Un-stamp the threshold actor. ingredient_stock.threshold_updated_by_user_id FKs to
    # users, so a manager who configured a threshold PINS their own user row — and the
    # caller is about to delete this store's users while its stock rows may still be
    # around (a stock row outlives its store's staff whenever an ingredient fixture tears
    # down after a store fixture). In production nothing deletes a user, so the FK is
    # exactly right there; here it is teardown order that has to give way, and it gives
    # way by dropping the stamp rather than by weakening the constraint.
    db.query(IngredientStock).filter(
        IngredientStock.store_id == store_id
    ).update(
        {"threshold_updated_by_user_id": None, "threshold_updated_at": None},
        synchronize_session=False,
    )
    db.commit()


def purge_stock_rows_for_store(db: Session, store_id: int) -> None:
    """Drop a store's summary rows. Movements and lines must already be gone."""
    db.query(IngredientStock).filter(
        IngredientStock.store_id == store_id
    ).delete(synchronize_session=False)
    db.commit()


def cleanup_orders_for_ingredient(db: Session, ingredient_id: int) -> None:
    """Delete every order that used this ingredient, whole."""
    order_ids = [
        r.order_id
        for r in db.query(OrderInventoryLine.order_id)
        .filter(OrderInventoryLine.ingredient_id == ingredient_id)
        .distinct()
        .all()
    ]
    cleanup_orders(db, order_ids)


def cleanup_ingredient(db: Session, ingredient_id: int) -> None:
    """
    Delete all test data associated with an ingredient.
    Respects FK order: movements → inventory lines → order chain → stock → ingredient.
    """
    # Movements first: they FK to orders, order_items, order_inventory_lines,
    # users, ingredients and inventory_transfers, so nothing else can go until
    # they are gone. Both legs of a transfer share its ingredient, so filtering
    # by ingredient removes the pair whichever stores it spanned.
    with _inventory_maintenance(db):
        db.query(IngredientStockMovement).filter(
            IngredientStockMovement.ingredient_id == ingredient_id
        ).delete(synchronize_session=False)

        # Stock counts, once the movements that FK to them are gone. They also FK
        # to ingredient_stock (deleted below) and to users, so they cannot outlive
        # this call.
        db.query(InventoryStockCount).filter(
            InventoryStockCount.ingredient_id == ingredient_id
        ).delete(synchronize_session=False)

        # Threshold decisions, which FK to ingredient_stock (deleted below) and to
        # users. They are configuration, not stock — no movement points at them.
        db.query(InventoryThresholdUpdate).filter(
            InventoryThresholdUpdate.ingredient_id == ingredient_id
        ).delete(synchronize_session=False)

    # Transfers next: their legs are gone, and they FK to ingredient_stock (about
    # to be deleted below) and to users.
    db.query(InventoryTransfer).filter(
        InventoryTransfer.ingredient_id == ingredient_id
    ).delete(synchronize_session=False)

    db.query(OrderInventoryLine).filter(
        OrderInventoryLine.ingredient_id == ingredient_id
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

    # An order in this set may also carry inventory rows for OTHER ingredients
    # (a multi-ingredient order); those would block the order delete below.
    purge_inventory_for_orders(db, order_ids)
    # Order issues and the payment ledger of these orders FK to them too, so they
    # must go before the orders themselves (issues first — see the function).
    purge_order_issues_for_orders(db, order_ids)
    purge_payments_for_orders(db, order_ids)

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

    # A shift FKs to its cashier, so it must go before the user does.
    purge_shifts_for_users(db, created_user_ids)
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
        # Cashier shifts first: they FK to the store and its users, so a leftover
        # shift would block both the user and the store delete below.
        purge_shifts_for_store(db, sid)
        # Order issues next: they FK to the store, its users, its orders and its
        # refunds, and the refund link is circular, so they must go before the ledger.
        purge_order_issues_for_store(db, sid)
        # Payment ledger next — settlements/allocations/refunds FK to store,
        # table, user and order and would otherwise block cleanup.
        _purge_payments_for_store(db, sid)
        # Then this store's inventory: movements FK to its users and orders, and
        # its stock rows FK to the store itself, so both pin everything below.
        purge_inventory_for_store(db, sid)
        user_ids = [u.id for u in db.query(User).filter(User.store_id == sid).all()]
        if user_ids:
            db.query(AuthSession).filter(AuthSession.user_id.in_(user_ids)).delete(synchronize_session=False)
            db.query(User).filter(User.id.in_(user_ids)).delete(synchronize_session=False)
        db.query(OwnerDecision).filter(OwnerDecision.store_id == sid).delete(synchronize_session=False)
        order_ids = [o.id for o in db.query(Order).filter(Order.store_id == sid).all()]
        if order_ids:
            # Inventory movements/lines FK to orders and order_items.
            purge_inventory_for_orders(db, order_ids)
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
        # Stock summary rows last of the inventory chain: movements and lines
        # reference them by (store_id, ingredient_id), so they had to go first.
        purge_stock_rows_for_store(db, sid)
        # Tables + QR tokens anchored to the store (created by make_table).
        table_ids = [t.id for t in db.query(Table).filter(Table.store_id == sid).all()]
        if table_ids:
            db.query(TableQrToken).filter(
                TableQrToken.table_id.in_(table_ids)
            ).delete(synchronize_session=False)
            db.query(Table).filter(Table.id.in_(table_ids)).delete(synchronize_session=False)
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


# ---------------------------------------------------------------------------
# Payment / cashier fixtures
# ---------------------------------------------------------------------------

# The append-only immutability triggers (installed by migration b8c4d1e6f207)
# refuse UPDATE/DELETE on every ledger table with NO runtime bypass — no GUC,
# session variable, or set_config can turn them off. Test teardown must remove
# the committed rows it created, so it uses the ONLY sanctioned escape hatch:
# ownership-gated DDL. `ALTER TABLE ... DISABLE TRIGGER` requires table ownership
# (the migration/test role owns the payment tables) and is not reachable through
# ordinary application DML or an SQL-injection path, so it is not a production
# bypass. The triggers are re-enabled before the transaction commits, so ledger
# immutability is fully restored for the next test.
_IMMUTABLE_TRIGGERS = (
    ("payment_refunds", "trg_payment_refunds_immutable"),
    ("payment_allocations", "trg_payment_allocations_immutable"),
    ("payment_settlements", "trg_payment_settlements_immutable"),
)


@contextmanager
def _ledger_maintenance(db: Session):
    """Ownership-gated teardown escape hatch — see module note above."""
    from sqlalchemy import text
    for table, trig in _IMMUTABLE_TRIGGERS:
        db.execute(text(f"ALTER TABLE {table} DISABLE TRIGGER {trig}"))
    try:
        yield
    finally:
        for table, trig in _IMMUTABLE_TRIGGERS:
            db.execute(text(f"ALTER TABLE {table} ENABLE TRIGGER {trig}"))


def purge_payments_for_orders(db: Session, order_ids: list[int]) -> None:
    """
    Delete the payment ledger rows anchored to these orders (refunds →
    allocations → settlements), so the orders themselves can then be removed.

    Needed by any test that both collects money on an order and cleans that
    order up through cleanup_ingredient. Uses the same ownership-gated trigger
    escape hatch as the other ledger teardowns.
    """
    if not order_ids:
        return
    # Order issues link to refunds (circular FK) — remove them first.
    purge_order_issues_for_orders(db, order_ids)
    settlement_ids = [
        a.settlement_id
        for a in db.query(PaymentAllocation).filter(
            PaymentAllocation.order_id.in_(order_ids)
        ).all()
    ]
    with _ledger_maintenance(db):
        db.query(PaymentRefund).filter(
            PaymentRefund.order_id.in_(order_ids)
        ).delete(synchronize_session=False)
        db.query(PaymentAllocation).filter(
            PaymentAllocation.order_id.in_(order_ids)
        ).delete(synchronize_session=False)
        if settlement_ids:
            db.query(PaymentAllocation).filter(
                PaymentAllocation.settlement_id.in_(settlement_ids)
            ).delete(synchronize_session=False)
            db.query(PaymentSettlement).filter(
                PaymentSettlement.id.in_(settlement_ids)
            ).delete(synchronize_session=False)
    db.commit()


# A cashier shift is guarded by a trigger (migration d5c7b3a11e40) that refuses
# UPDATE/DELETE on a CLOSED shift and every DELETE — with NO runtime bypass, exactly
# like the ledger. Teardown removes committed shift rows via the same ownership-gated
# escape hatch: ALTER TABLE ... DISABLE TRIGGER requires table ownership and is
# unreachable from application DML or an injection path, so it is not a production
# bypass. The trigger is restored before the transaction commits.
_SHIFT_TRIGGER = ("cashier_shifts", "trg_cashier_shifts_guard")


@contextmanager
def _shift_maintenance(db: Session):
    """Ownership-gated teardown escape hatch for cashier_shifts."""
    from sqlalchemy import text
    table, trig = _SHIFT_TRIGGER
    db.execute(text(f"ALTER TABLE {table} DISABLE TRIGGER {trig}"))
    try:
        yield
    finally:
        db.execute(text(f"ALTER TABLE {table} ENABLE TRIGGER {trig}"))


def purge_shifts_for_store(db: Session, store_id: int) -> None:
    """Delete every cashier shift for a store (needed before its users/store go)."""
    with _shift_maintenance(db):
        db.query(CashierShift).filter(
            CashierShift.store_id == store_id
        ).delete(synchronize_session=False)
    db.commit()


def purge_shifts_for_users(db: Session, user_ids: list[int]) -> None:
    """Delete every cashier shift opened by these cashiers."""
    if not user_ids:
        return
    with _shift_maintenance(db):
        db.query(CashierShift).filter(
            CashierShift.cashier_user_id.in_(user_ids)
        ).delete(synchronize_session=False)
    db.commit()


def _purge_payments_for_store(db: Session, store_id: int) -> None:
    """Delete all ledger rows for a store (refunds → allocations → settlements)."""
    settlement_ids = [
        s.id for s in db.query(PaymentSettlement).filter(
            PaymentSettlement.store_id == store_id
        ).all()
    ]
    with _ledger_maintenance(db):
        db.query(PaymentRefund).filter(
            PaymentRefund.store_id == store_id
        ).delete(synchronize_session=False)
        if settlement_ids:
            db.query(PaymentAllocation).filter(
                PaymentAllocation.settlement_id.in_(settlement_ids)
            ).delete(synchronize_session=False)
            db.query(PaymentSettlement).filter(
                PaymentSettlement.id.in_(settlement_ids)
            ).delete(synchronize_session=False)


@pytest.fixture()
def make_table(db: Session):
    """Factory: create a Table on an existing store. Cleans up on teardown."""
    created: list[int] = []

    def _make(store_id: int, table_number: str | None = None) -> Table:
        uid = uuid.uuid4().hex[:8]
        table = Table(
            store_id=store_id,
            table_number=table_number if table_number is not None else uid,
            qr_code=f"cashier-test-{uid}",
        )
        db.add(table)
        db.commit()
        db.refresh(table)
        created.append(table.id)
        return table

    yield _make

    for tid in created:
        db.query(TableQrToken).filter(TableQrToken.table_id == tid).delete(synchronize_session=False)
        db.query(Table).filter(Table.id == tid).delete(synchronize_session=False)
    db.commit()


@pytest.fixture()
def make_order(db: Session):
    """
    Factory: create a bare Order with a persisted total. Tracks created orders
    (and any ledger rows referencing them) for teardown. No order items — the
    payment layer settles against the persisted total_amount snapshot only.
    """
    created: list[int] = []

    def _make(
        store_id: int,
        table_id: int | None,
        total: Decimal,
        *,
        status: str = "READY",
    ) -> Order:
        order = Order(
            store_id=store_id,
            table_id=table_id,
            total_amount=Decimal(str(total)),
            status=status,
            payment_status="UNPAID",
            refund_status="NONE",
            paid_amount=Decimal("0"),
            refunded_amount=Decimal("0"),
        )
        db.add(order)
        db.commit()
        db.refresh(order)
        created.append(order.id)
        return order

    yield _make

    if created:
        purge_order_issues_for_orders(db, created)
        purge_inventory_for_orders(db, created)
        settlement_ids = [
            a.settlement_id for a in db.query(PaymentAllocation).filter(
                PaymentAllocation.order_id.in_(created)
            ).all()
        ]
        with _ledger_maintenance(db):
            db.query(PaymentRefund).filter(
                PaymentRefund.order_id.in_(created)
            ).delete(synchronize_session=False)
            db.query(PaymentAllocation).filter(
                PaymentAllocation.order_id.in_(created)
            ).delete(synchronize_session=False)
            if settlement_ids:
                db.query(PaymentAllocation).filter(
                    PaymentAllocation.settlement_id.in_(settlement_ids)
                ).delete(synchronize_session=False)
                db.query(PaymentSettlement).filter(
                    PaymentSettlement.id.in_(settlement_ids)
                ).delete(synchronize_session=False)
        db.query(OrderStatusEvent).filter(
            OrderStatusEvent.order_id.in_(created)
        ).delete(synchronize_session=False)
        db.query(Order).filter(Order.id.in_(created)).delete(synchronize_session=False)
        db.commit()


@pytest.fixture()
def cashier_env(db: Session, make_store, make_table, make_staff):
    """
    A ready-to-use cashier environment: a fresh store, a table on it, and an
    authenticated CASHIER client. Returns a small namespace object.
    """
    class Env:
        pass

    store = make_store()
    table = make_table(store.id)
    cashier = make_staff("CASHIER", store_id=store.id)
    env = Env()
    env.store = store
    env.table = table
    env.cashier = cashier
    env.client = make_authed_client(db, cashier)
    return env


@pytest.fixture()
def manager_client_factory(db: Session, make_staff):
    """Factory: an authenticated MANAGER client for a given store."""
    def _make(store_id: int) -> TestClient:
        user = make_staff("MANAGER", store_id=store_id)
        return make_authed_client(db, user)
    return _make


@pytest.fixture()
def collected_ledger(db: Session, make_store, make_table, make_staff, make_order):
    """
    A committed real ledger for direct-SQL integrity/immutability tests: a store,
    a table, a MANAGER, a fully-paid order (one settlement + one allocation), and
    one partial refund. Returns a namespace of live ids. All rows are cleaned up
    through the make_order / make_store teardowns (which disable the immutability
    triggers via ownership-gated DDL), so no test needs a production-style delete.
    """
    class Env:
        pass

    env = Env()
    env.store = make_store()
    env.table = make_table(env.store.id)
    env.manager = make_staff("MANAGER", store_id=env.store.id)
    env.client = make_authed_client(db, env.manager)
    env.order = make_order(env.store.id, env.table.id, Decimal("100.00"))

    pay = env.client.post(
        f"/cashier/orders/{env.order.id}/payments",
        json={"payment_method": "CASH"},
        headers={"Idempotency-Key": uuid.uuid4().hex},
    )
    assert pay.status_code == 200, pay.text
    env.settlement_id = pay.json()["settlement_id"]
    env.allocation_id = pay.json()["allocations"][0]["id"]

    ref = env.client.post(
        f"/cashier/allocations/{env.allocation_id}/refunds",
        json={"amount": "10.00", "reason": "kismi iade"},
        headers={"Idempotency-Key": uuid.uuid4().hex},
    )
    assert ref.status_code == 200, ref.text
    env.refund_id = ref.json()["refund_id"]
    return env
