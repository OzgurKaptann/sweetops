#!/usr/bin/env python
"""
SweetOps deterministic demo seed — one command, a coherent operational story.

Purpose
-------
SweetOps now has many operational surfaces — customer QR ordering, the kitchen
board and its prep-timing cards, cashier payments/refunds/shifts, order issues,
the store-scoped inventory lifecycle with threshold alerts, transfers and physical
counts, and the owner operational dashboard. A reviewer who opens any of them on a
fresh database sees empty screens and cannot understand the product.

This script populates ONE realistic small Turkish waffle shop demo so that every
major screen becomes meaningful immediately:

    python scripts/seed_demo_data.py      # from the repo root, after migrations
    npm run seed:demo                     # equivalent, from the repo root

Safety model (see docs/DEMO_SEED_DATA.md)
-----------------------------------------
* Deterministic     — no random data; every value is fixed in this file. The only
                      "now" used is deliberate: kitchen-timing and today's metrics
                      are relative to the moment you seed, exactly as a live shop's
                      would be.
* Idempotent        — safe to run any number of times. Catalog/store/user/table
                      rows are upserted by natural key; orders carry deterministic
                      idempotency keys; every money/stock/shift/issue command is
                      driven through the SAME idempotent services the API uses, so
                      a rerun replays rather than duplicates.
* Demo-scoped       — everything lives inside the demo stores (see DEMO_STORES).
                      Store 1 and any other non-demo store are never read for
                      writing, never mutated, and never deleted.
* Non-destructive   — this script only ever creates or upserts. It deletes nothing,
                      wipes no tables, drops no volumes and recreates no containers.
* Ledger-honest     — all stock is built through the inventory service (every
                      on-hand change has a matching movement) and all money through
                      the payment ledger, so the reconcilers stay green after seeding.

Local demo credentials
-----------------------
The demo staff share one password, printed at the end and documented in
docs/DEMO_SEED_DATA.md. They are LOCAL/DEMO ONLY — never real accounts, never
secrets. Do not run this against a production database.
"""
from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal

# ── Import path setup (repo-root or in-container both work) ───────────────────
_CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
_API_DIR = os.path.join(_CURRENT_DIR, "..", "apps", "api")
sys.path.insert(0, _API_DIR)
sys.path.insert(0, "/app")

from sqlalchemy import func, text
from sqlalchemy.exc import OperationalError, ProgrammingError
from sqlalchemy.orm import Session

from app.core.db import SessionLocal
from app.core.permissions import (
    ROLE_CASHIER,
    ROLE_KITCHEN,
    ROLE_MANAGER,
    ROLE_OWNER,
    permissions_for_role,
)
from app.core.security import hash_password
from app.models.ingredient import Ingredient
from app.models.ingredient_stock import IngredientStock
from app.models.order import Order
from app.models.order_item import OrderItem
from app.models.order_item_ingredient import OrderItemIngredient
from app.models.order_status_event import OrderStatusEvent
from app.models.product import Product
from app.models.role import Role
from app.models.store import Store
from app.models.store_product import StoreProduct
from app.models.table import Table
from app.models.user import User
from app.schemas.order_issue import IssueCreateRequest, IssueResolveRequest
from app.schemas.payment import OrderPaymentRequest, RefundCreateRequest
from app.schemas.cashier_shift import ShiftCloseRequest, ShiftOpenRequest
from app.services import (
    cashier_shift_service,
    inventory_service,
    order_issue_service,
    payment_service,
)
from app.services.auth_service import CurrentStaff

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("seed_demo_data")

# ── Demo namespace ────────────────────────────────────────────────────────────
DEMO_MARKER = "[DEMO]"
DEMO_PASSWORD = "demo1234"  # LOCAL/DEMO ONLY — printed at the end, documented.

PRIMARY_STORE_NAME = "SweetOps Demo - Kadıköy"
SECONDARY_STORE_NAME = "SweetOps Demo - Moda"  # transfer destination only
DEMO_STORES = (PRIMARY_STORE_NAME, SECONDARY_STORE_NAME)


def _dq(value: str | int | float) -> Decimal:
    return Decimal(str(value))


# ── Deterministic idempotency-key helper ──────────────────────────────────────
def key(*parts: object) -> str:
    """A stable, human-readable idempotency key from its parts."""
    return "demo-seed:" + ":".join(str(p) for p in parts)


# ── Run summary ───────────────────────────────────────────────────────────────
@dataclass
class Summary:
    created: dict[str, int] = field(default_factory=dict)
    reused: dict[str, int] = field(default_factory=dict)

    def note(self, kind: str, *, created: bool) -> None:
        bucket = self.created if created else self.reused
        bucket[kind] = bucket.get(kind, 0) + 1

    def print(self) -> None:
        logger.info("\n" + "=" * 60)
        logger.info("Demo seed summary")
        logger.info("=" * 60)
        kinds = sorted(set(self.created) | set(self.reused))
        for kind in kinds:
            logger.info(
                "  %-22s created %3d   reused %3d",
                kind, self.created.get(kind, 0), self.reused.get(kind, 0),
            )
        logger.info("=" * 60)


SUMMARY = Summary()


# ── Small get-or-create helpers (upsert by natural key) ───────────────────────
def get_or_create_role(db: Session, name: str) -> Role:
    role = db.query(Role).filter(Role.name == name).first()
    if role is None:
        role = Role(name=name)
        db.add(role)
        db.flush()
    return role


def get_or_create_store(db: Session, name: str, location: str) -> Store:
    store = db.query(Store).filter(Store.name == name).first()
    created = store is None
    if created:
        store = Store(name=name, location=location)
        db.add(store)
        db.flush()
    SUMMARY.note("store", created=created)
    return store


def get_or_create_table(db: Session, store: Store, number: str, qr_code: str) -> Table:
    table = db.query(Table).filter(Table.qr_code == qr_code).first()
    created = table is None
    if created:
        table = Table(store_id=store.id, table_number=number, qr_code=qr_code)
        db.add(table)
        db.flush()
    SUMMARY.note("table", created=created)
    return table


def get_or_create_product(db: Session, name: str, category: str, base_price: Decimal) -> Product:
    product = db.query(Product).filter(Product.name == name).first()
    created = product is None
    if created:
        product = Product(name=name, category=category, base_price=base_price)
        db.add(product)
        db.flush()
    SUMMARY.note("product", created=created)
    return product


def offer_product(
    db: Session, store: Store, product: Product, sort_order: int
) -> StoreProduct:
    """
    Publish a product on a branch's customer menu.

    This is the demo equivalent of the decision a shop makes when it puts an
    item on the board. Without it the product exists in the catalog and is
    invisible to every guest — which is exactly the boundary migration
    a9e4c7b25d13 introduced, and the reason the seed has to be explicit about
    what the demo branches sell.
    """
    offering = (
        db.query(StoreProduct)
        .filter(
            StoreProduct.store_id == store.id,
            StoreProduct.product_id == product.id,
        )
        .first()
    )
    created = offering is None
    if created:
        offering = StoreProduct(
            store_id=store.id,
            product_id=product.id,
            is_available=True,
            sort_order=sort_order,
        )
        db.add(offering)
        db.flush()
    SUMMARY.note("menu offering", created=created)
    return offering


def get_or_create_ingredient(
    db: Session,
    name: str,
    category: str,
    *,
    price: Decimal,
    unit: str,
    standard_quantity: Decimal,
) -> Ingredient:
    """Ingredients are GLOBAL catalog rows — reused across stores if they exist."""
    ing = db.query(Ingredient).filter(Ingredient.name == name).first()
    created = ing is None
    if created:
        ing = Ingredient(
            name=name,
            category=category,
            price=price,
            unit=unit,
            standard_quantity=standard_quantity,
            is_active=True,
        )
        db.add(ing)
        db.flush()
    SUMMARY.note("ingredient", created=created)
    return ing


def get_or_create_user(db: Session, username: str, role: Role, store: Store) -> User:
    user = db.query(User).filter(func.lower(User.username) == username.lower()).first()
    created = user is None
    if created:
        user = User(
            username=username,
            password_hash=hash_password(DEMO_PASSWORD),
            role_id=role.id,
            store_id=store.id,
            is_active=True,
        )
        db.add(user)
        db.flush()
    SUMMARY.note("user", created=created)
    return user


def ensure_stock_row(db: Session, store: Store, ing: Ingredient) -> IngredientStock:
    """
    Materialise a store's stock row at ZERO if it has never held this ingredient.

    Stock is physical and per-branch: a store with no row has no stock. The
    manual inventory commands (receipt/waste/adjust/count) require the row to
    already exist, so this creates it first. on-hand only ever moves from here
    through a service that also writes the matching ledger movement, so the row
    stays reconciled.
    """
    stock = (
        db.query(IngredientStock)
        .filter(
            IngredientStock.store_id == store.id,
            IngredientStock.ingredient_id == ing.id,
        )
        .first()
    )
    created = stock is None
    if created:
        stock = IngredientStock(
            store_id=store.id,
            ingredient_id=ing.id,
            on_hand_quantity=Decimal("0"),
            reserved_quantity=Decimal("0"),
            unit=ing.unit,
        )
        db.add(stock)
        db.flush()
    SUMMARY.note("stock_row", created=created)
    return stock


def make_ctx(user: User) -> CurrentStaff:
    """A CurrentStaff for driving the services, exactly as a real session would."""
    return CurrentStaff(
        user_id=user.id,
        username=user.username,
        role=user.role.name if user.role else "",
        store_id=user.store_id,
        permissions=tuple(permissions_for_role(user.role.name if user.role else None)),
        session_id=0,  # not persisted; no service reads it against the DB
        csrf_token_hash="",
    )


# ══════════════════════════════════════════════════════════════════════════════
# Catalog, stores, tables, users
# ══════════════════════════════════════════════════════════════════════════════
_PRODUCTS = [
    ("Klasik Waffle", "Waffle", _dq("90.00")),
    ("Çilekli Waffle", "Waffle", _dq("110.00")),
    ("Muzlu Nutellalı Waffle", "Waffle", _dq("130.00")),
    ("Türk Kahvesi", "İçecek", _dq("60.00")),
    ("Limonata", "İçecek", _dq("55.00")),
]

# (name, category, price, unit, standard_quantity). Names that already exist in
# the global catalog are reused, not duplicated.
_INGREDIENTS = [
    ("Nutella", "Çikolatalar / Soslar", _dq("10.00"), "g", _dq("30.00")),
    ("Çilek", "Meyveler", _dq("10.00"), "g", _dq("40.00")),
    ("Muz", "Meyveler", _dq("8.00"), "g", _dq("50.00")),
    ("Lotus Biscoff", "Kuruyemiş / Süslemeler", _dq("12.00"), "piece", _dq("2.00")),
    ("Oreo", "Kuruyemiş / Süslemeler", _dq("10.00"), "piece", _dq("2.00")),
    ("Karamel", "Çikolatalar / Soslar", _dq("8.00"), "ml", _dq("20.00")),
    ("Fındık", "Kuruyemiş / Süslemeler", _dq("12.00"), "g", _dq("15.00")),
    ("Fıstık", "Kuruyemiş / Süslemeler", _dq("15.00"), "g", _dq("12.00")),
    ("Bonibon", "Kuruyemiş / Süslemeler", _dq("8.00"), "g", _dq("12.00")),
]

_PRIMARY_TABLES = [
    ("Masa 1", "sweetops-demo-kadikoy-masa-1"),
    ("Masa 2", "sweetops-demo-kadikoy-masa-2"),
    ("Masa 3", "sweetops-demo-kadikoy-masa-3"),
    ("Masa 4", "sweetops-demo-kadikoy-masa-4"),
    ("Paket Servis", "sweetops-demo-kadikoy-paket"),
]

_PRIMARY_USERS = [
    ("owner.demo@sweetops.local", ROLE_OWNER),
    ("manager.demo@sweetops.local", ROLE_MANAGER),
    ("kitchen.demo@sweetops.local", ROLE_KITCHEN),
    ("cashier.demo@sweetops.local", ROLE_CASHIER),
    ("cashier2.demo@sweetops.local", ROLE_CASHIER),  # runs the closed shifts
]


@dataclass
class DemoWorld:
    primary: Store
    secondary: Store
    tables: dict[str, Table]
    products: dict[str, Product]
    ingredients: dict[str, Ingredient]
    users: dict[str, User]

    @property
    def owner(self) -> User:
        return self.users["owner.demo@sweetops.local"]

    @property
    def manager(self) -> User:
        return self.users["manager.demo@sweetops.local"]

    @property
    def cashier(self) -> User:
        return self.users["cashier.demo@sweetops.local"]

    @property
    def cashier2(self) -> User:
        return self.users["cashier2.demo@sweetops.local"]

    @property
    def moda_manager(self) -> User:
        return self.users["manager.moda.demo@sweetops.local"]


def seed_foundation(db: Session) -> DemoWorld:
    logger.info("→ Catalog, stores, tables, staff…")

    # Roles (canonical set already exists after auth/RBAC; upsert defensively).
    roles = {name: get_or_create_role(db, name)
             for name in (ROLE_OWNER, ROLE_MANAGER, ROLE_KITCHEN, ROLE_CASHIER)}

    primary = get_or_create_store(db, PRIMARY_STORE_NAME, "Kadıköy, İstanbul")
    secondary = get_or_create_store(db, SECONDARY_STORE_NAME, "Moda, İstanbul")

    products = {name: get_or_create_product(db, name, cat, price)
                for name, cat, price in _PRODUCTS}
    ingredients = {
        name: get_or_create_ingredient(db, name, cat, price=price, unit=unit,
                                       standard_quantity=sq)
        for name, cat, price, unit, sq in _INGREDIENTS
    }
    tables = {num: get_or_create_table(db, primary, num, qr)
              for num, qr in _PRIMARY_TABLES}

    # Publish the menu. A product in the catalog is not on anybody's menu until
    # a branch offers it, so this is what makes the demo QR flow show anything
    # at all. The two branches deliberately publish DIFFERENT menus — Moda is a
    # small satellite that sells the waffles but no drinks — so the demo data
    # exercises store scoping instead of merely permitting it.
    primary_menu = [name for name, _cat, _price in _PRODUCTS]
    secondary_menu = [name for name, cat, _price in _PRODUCTS if cat == "Waffle"]
    for order_index, name in enumerate(primary_menu):
        offer_product(db, primary, products[name], order_index)
    for order_index, name in enumerate(secondary_menu):
        offer_product(db, secondary, products[name], order_index)

    users = {
        uname: get_or_create_user(db, uname, roles[role_name], primary)
        for uname, role_name in _PRIMARY_USERS
    }
    users["manager.moda.demo@sweetops.local"] = get_or_create_user(
        db, "manager.moda.demo@sweetops.local", roles[ROLE_MANAGER], secondary
    )

    db.commit()
    return DemoWorld(
        primary=primary, secondary=secondary, tables=tables,
        products=products, ingredients=ingredients, users=users,
    )


# ══════════════════════════════════════════════════════════════════════════════
# Inventory: threshold states + stock operations (all ledger-honest)
# ══════════════════════════════════════════════════════════════════════════════
# (ingredient, target on-hand, critical, minimum, target). Thresholds are tested
# against AVAILABLE (= on-hand here, since demo orders reserve no stock), so the
# target on-hand lands each row in a chosen threshold state.
_THRESHOLD_PLAN = [
    # name,            on_hand, critical, minimum, target,  expected state
    ("Nutella", "5000", "500", "1000", "4000"),   # HEALTHY
    ("Çilek", "900", "300", "1000", "3000"),      # LOW
    ("Muz", "250", "300", "800", "2000"),         # CRITICAL
    ("Lotus Biscoff", "0", "10", "30", "120"),    # OUT_OF_STOCK (received then wasted)
    ("Oreo", "60", None, None, None),             # NOT_CONFIGURED
]


def seed_inventory(db: Session, world: DemoWorld) -> None:
    logger.info("→ Inventory threshold states + stock operations…")
    store = world.primary
    mgr = world.manager.id
    ing = world.ingredients

    # ── Threshold states ──────────────────────────────────────────────────────
    for name, on_hand, crit, minimum, target in _THRESHOLD_PLAN:
        stock_ing = ing[name]
        ensure_stock_row(db, store, stock_ing)
        # Lotus reaches OUT_OF_STOCK via a receipt then a waste, so the ledger
        # tells the honest story (goods arrived, then were thrown away).
        if name == "Lotus Biscoff":
            inventory_service.record_purchase_receipt(
                db, store_id=store.id, ingredient_id=stock_ing.id, quantity=_dq("60"),
                actor_user_id=mgr, reason=f"{DEMO_MARKER} açılış teslimatı",
                idempotency_key=key("receipt", store.id, name),
            )
            inventory_service.record_waste(
                db, store_id=store.id, ingredient_id=stock_ing.id, quantity=_dq("60"),
                reason=f"{DEMO_MARKER} bayat — tamamı imha", actor_user_id=mgr,
                idempotency_key=key("waste", store.id, name),
            )
        elif _dq(on_hand) > 0:
            inventory_service.record_purchase_receipt(
                db, store_id=store.id, ingredient_id=stock_ing.id, quantity=_dq(on_hand),
                actor_user_id=mgr, reason=f"{DEMO_MARKER} açılış teslimatı",
                idempotency_key=key("receipt", store.id, name),
            )
        if crit is not None or minimum is not None or target is not None:
            inventory_service.update_thresholds(
                db, store_id=store.id, ingredient_id=stock_ing.id,
                critical_quantity=_dq(crit) if crit else None,
                minimum_quantity=_dq(minimum) if minimum else None,
                target_quantity=_dq(target) if target else None,
                reason=f"{DEMO_MARKER} alarm eşikleri", actor_user_id=mgr,
                idempotency_key=key("threshold", store.id, name),
            )
        SUMMARY.note("threshold_state", created=True)

    # ── Stock operations (distinct ingredients so state math stays clean) ──────
    # Purchase receipt + transfer: Karamel arrives at Kadıköy, some ships to Moda.
    karamel = ing["Karamel"]
    ensure_stock_row(db, store, karamel)
    inventory_service.record_purchase_receipt(
        db, store_id=store.id, ingredient_id=karamel.id, quantity=_dq("2000"),
        actor_user_id=mgr, reason=f"{DEMO_MARKER} toptan alım",
        idempotency_key=key("receipt", store.id, "Karamel"),
    )
    inventory_service.transfer_stock(
        db, source_store_id=store.id, destination_store_id=world.secondary.id,
        ingredient_id=karamel.id, quantity=_dq("500"),
        reason=f"{DEMO_MARKER} şubeye sevk", note=f"{DEMO_MARKER} Moda açılış",
        actor_user_id=mgr, idempotency_key=key("transfer", store.id, "Karamel"),
    )
    SUMMARY.note("transfer", created=True)

    # Manual adjustment: a weigh-in correction on Fındık.
    findik = ing["Fındık"]
    ensure_stock_row(db, store, findik)
    inventory_service.record_purchase_receipt(
        db, store_id=store.id, ingredient_id=findik.id, quantity=_dq("400"),
        actor_user_id=mgr, reason=f"{DEMO_MARKER} toptan alım",
        idempotency_key=key("receipt", store.id, "Fındık"),
    )
    inventory_service.record_manual_adjustment(
        db, store_id=store.id, ingredient_id=findik.id, delta=_dq("-25"),
        reason=f"{DEMO_MARKER} tartım düzeltmesi", actor_user_id=mgr,
        idempotency_key=key("adjust", store.id, "Fındık"),
    )
    SUMMARY.note("manual_adjustment", created=True)

    # Physical stock count: Fıstık counted 20 g short, then counted again & correct.
    fistik = ing["Fıstık"]
    ensure_stock_row(db, store, fistik)
    inventory_service.record_purchase_receipt(
        db, store_id=store.id, ingredient_id=fistik.id, quantity=_dq("500"),
        actor_user_id=mgr, reason=f"{DEMO_MARKER} toptan alım",
        idempotency_key=key("receipt", store.id, "Fıstık"),
    )
    inventory_service.record_stock_count(
        db, store_id=store.id, ingredient_id=fistik.id, counted_quantity=_dq("480"),
        reason=f"{DEMO_MARKER} raf sayımı", actor_user_id=mgr,
        idempotency_key=key("count", store.id, "Fıstık", 1),
    )
    inventory_service.record_stock_count(
        db, store_id=store.id, ingredient_id=fistik.id, counted_quantity=_dq("480"),
        reason=f"{DEMO_MARKER} raf sayımı — doğrulama", actor_user_id=mgr,
        idempotency_key=key("count", store.id, "Fıstık", 2),
    )
    SUMMARY.note("stock_count", created=True)

    # Waste example on Bonibon (separate from Lotus, no threshold noise).
    bonibon = ing["Bonibon"]
    ensure_stock_row(db, store, bonibon)
    inventory_service.record_purchase_receipt(
        db, store_id=store.id, ingredient_id=bonibon.id, quantity=_dq("300"),
        actor_user_id=mgr, reason=f"{DEMO_MARKER} toptan alım",
        idempotency_key=key("receipt", store.id, "Bonibon"),
    )
    inventory_service.record_waste(
        db, store_id=store.id, ingredient_id=bonibon.id, quantity=_dq("50"),
        reason=f"{DEMO_MARKER} yere döküldü", actor_user_id=mgr,
        idempotency_key=key("waste", store.id, "Bonibon"),
    )
    SUMMARY.note("waste", created=True)

    db.commit()


# ══════════════════════════════════════════════════════════════════════════════
# Orders (bare — no reservation) with deterministic kitchen-timing timelines
# ══════════════════════════════════════════════════════════════════════════════
NOW = datetime.now(timezone.utc)


def _at(minutes_ago: int) -> datetime:
    return NOW - timedelta(minutes=minutes_ago)


@dataclass
class OrderSpec:
    slug: str
    table: str
    product: str
    toppings: list[str]
    status: str
    # (status_to, minutes_ago) event timeline, earliest first.
    timeline: list[tuple[str, int]]


# One rich order per screen state. Timings are relative to seed time so the
# kitchen board shows live warning/critical cards. Thresholds (min): queue
# warning 10 / critical 15; prep warning 12 / critical 20.
_ORDERS: list[OrderSpec] = [
    OrderSpec("new-ok", "Masa 1", "Klasik Waffle", ["Nutella"],
              "NEW", [("NEW", 4)]),
    OrderSpec("new-warn", "Masa 2", "Çilekli Waffle", ["Çilek", "Nutella"],
              "NEW", [("NEW", 12)]),
    OrderSpec("prep-ok", "Masa 3", "Muzlu Nutellalı Waffle", ["Muz", "Nutella"],
              "IN_PREP", [("NEW", 22), ("IN_PREP", 4)]),
    OrderSpec("prep-critical", "Masa 4", "Klasik Waffle", ["Nutella", "Fındık"],
              "IN_PREP", [("NEW", 40), ("IN_PREP", 25)]),
    OrderSpec("ready", "Masa 1", "Çilekli Waffle", ["Çilek"],
              "READY", [("NEW", 30), ("IN_PREP", 24), ("READY", 6)]),
    OrderSpec("paid-cash", "Masa 2", "Klasik Waffle", ["Nutella"],
              "DELIVERED", [("NEW", 70), ("IN_PREP", 66), ("READY", 60), ("DELIVERED", 55)]),
    OrderSpec("paid-card", "Masa 3", "Muzlu Nutellalı Waffle", ["Muz", "Nutella"],
              "DELIVERED", [("NEW", 80), ("IN_PREP", 76), ("READY", 69), ("DELIVERED", 64)]),
    OrderSpec("partial-pay", "Paket Servis", "Çilekli Waffle", ["Çilek", "Nutella"],
              "DELIVERED", [("NEW", 95), ("IN_PREP", 90), ("READY", 83), ("DELIVERED", 78)]),
    OrderSpec("cancelled", "Masa 4", "Limonata", [],
              "CANCELLED", [("NEW", 50), ("CANCELLED", 42)]),
    OrderSpec("unpaid", "Masa 1", "Türk Kahvesi", [],
              "DELIVERED", [("NEW", 48), ("IN_PREP", 45), ("READY", 40), ("DELIVERED", 36)]),
    OrderSpec("partial-refund", "Masa 2", "Klasik Waffle", ["Nutella", "Muz"],
              "DELIVERED", [("NEW", 110), ("IN_PREP", 106), ("READY", 99), ("DELIVERED", 94)]),
    OrderSpec("issue-open", "Masa 3", "Çilekli Waffle", ["Çilek"],
              "DELIVERED", [("NEW", 100), ("IN_PREP", 96), ("READY", 90), ("DELIVERED", 85)]),
    # Left IN_PREP, not READY/DELIVERED: the FULL_REFUND resolution cancels this
    # order, and reaching a ready/delivered state before a cancel is a
    # contradictory terminal history (terminal_both).
    OrderSpec("issue-full-refund", "Masa 4", "Muzlu Nutellalı Waffle", ["Muz", "Nutella"],
              "IN_PREP", [("NEW", 120), ("IN_PREP", 116)]),
    OrderSpec("issue-partial-refund", "Paket Servis", "Klasik Waffle", ["Nutella"],
              "DELIVERED", [("NEW", 130), ("IN_PREP", 126), ("READY", 119), ("DELIVERED", 114)]),
    OrderSpec("issue-no-refund", "Masa 1", "Limonata", [],
              "DELIVERED", [("NEW", 60), ("IN_PREP", 57), ("READY", 52), ("DELIVERED", 48)]),
]

_ACTOR_FOR = {"NEW": "CUSTOMER", "IN_PREP": "STAFF", "READY": "STAFF",
              "DELIVERED": "STAFF", "CANCELLED": "STAFF"}


def _order_total(world: DemoWorld, spec: OrderSpec) -> Decimal:
    total = world.products[spec.product].base_price
    for t in spec.toppings:
        total += world.ingredients[t].price
    return Decimal(total)


def _create_order(db: Session, world: DemoWorld, spec: OrderSpec) -> Order:
    """
    Upsert one demo order by its deterministic idempotency key.

    Demo orders are BARE (no inventory reservation): the payment layer settles
    against the persisted total_amount snapshot, and the kitchen board reads the
    status-event timeline — neither needs an order_inventory_line, so reconcilers
    are unaffected by these orders. Items are recorded for display only, with no
    consumption claim.
    """
    idem = f"demo:{spec.slug}"
    existing = db.query(Order).filter(Order.idempotency_key == idem).first()
    if existing is not None:
        SUMMARY.note("order", created=False)
        return existing

    table = world.tables[spec.table]
    product = world.products[spec.product]
    total = _order_total(world, spec)

    order = Order(
        store_id=world.primary.id,
        table_id=table.id,
        status=spec.status,
        total_amount=total,
        idempotency_key=idem,
    )
    order.created_at = _at(spec.timeline[0][1])
    db.add(order)
    db.flush()

    item = OrderItem(order_id=order.id, product_id=product.id, quantity=1,
                     price=product.base_price)
    db.add(item)
    db.flush()
    for topping in spec.toppings:
        ting = world.ingredients[topping]
        db.add(OrderItemIngredient(
            order_item_id=item.id, ingredient_id=ting.id, quantity=1,
            price_modifier=ting.price,  # selection only — no consumed_quantity claim
        ))

    prev = None
    for status_to, minutes_ago in spec.timeline:
        ev = OrderStatusEvent(
            order_id=order.id, status_from=prev, status_to=status_to,
            actor_type=_ACTOR_FOR.get(status_to, "STAFF"),
        )
        ev.created_at = _at(minutes_ago)
        db.add(ev)
        prev = status_to

    SUMMARY.note("order", created=True)
    return order


def seed_orders_payments_issues(db: Session, world: DemoWorld) -> None:
    logger.info("→ Orders, payments, refunds, issues…")
    cashier = make_ctx(world.cashier)
    manager = make_ctx(world.manager)

    orders: dict[str, Order] = {}
    for spec in _ORDERS:
        orders[spec.slug] = _create_order(db, world, spec)
    db.commit()

    def pay(slug: str, method: str, amount: Decimal | None) -> object:
        order = orders[slug]
        receipt = payment_service.collect_order_payment(
            db, cashier, order.id,
            OrderPaymentRequest(payment_method=method, amount=amount,
                                note=f"{DEMO_MARKER} tahsilat"),
            idempotency_key=key("pay", slug),
        )
        SUMMARY.note("payment", created=not receipt.idempotent_replay)
        return receipt

    # ── Payments ──────────────────────────────────────────────────────────────
    pay("paid-cash", "CASH", None)                       # full cash
    pay("paid-card", "CARD", None)                       # full card
    pay("partial-pay", "CASH",                           # partial
        (_order_total(world, next(s for s in _ORDERS if s.slug == "partial-pay")) * _dq("0.4"))
        .quantize(Decimal("0.01")))

    # Orders that need a paid balance before a refund/issue-refund.
    full_cash = pay("partial-refund", "CASH", None)
    pay("issue-open", "CARD", None)
    pay("issue-full-refund", "CASH", None)
    pay("issue-partial-refund", "CARD", None)

    # ── Direct partial refund (manager) on the fully-paid order ────────────────
    alloc_id = full_cash.allocations[0].id
    refund = payment_service.refund_allocation(
        db, manager, alloc_id,
        RefundCreateRequest(
            amount=(_order_total(world, next(s for s in _ORDERS if s.slug == "partial-refund"))
                    * _dq("0.3")).quantize(Decimal("0.01")),
            reason=f"{DEMO_MARKER} kısmi iade — müşteri memnuniyeti"),
        idempotency_key=key("refund", "partial-refund"),
    )
    SUMMARY.note("refund", created=not refund.idempotent_replay)
    db.commit()

    # ── Order issues ──────────────────────────────────────────────────────────
    def raise_issue(slug: str, issue_type: str, reason: str) -> object:
        order = orders[slug]
        resp = order_issue_service.create_issue(
            db, manager, order.id,
            IssueCreateRequest(issue_type=issue_type, reason=f"{DEMO_MARKER} {reason}"),
            idempotency_key=key("issue-create", slug),
        )
        SUMMARY.note("issue", created=not resp.idempotent_replay)
        return resp

    def resolve(slug: str, resolution: str, reason: str,
                approved: Decimal | None = None) -> None:
        issue = orders_issue[slug]
        resp = order_issue_service.resolve_issue(
            db, manager, issue.id,
            IssueResolveRequest(resolution_type=resolution,
                                approved_refund_amount=approved,
                                reason=f"{DEMO_MARKER} {reason}"),
            idempotency_key=key("issue-resolve", slug),
        )
        SUMMARY.note("issue_resolved", created=not resp.idempotent_replay)

    orders_issue = {
        "issue-open": raise_issue("issue-open", "MISSING_ITEM", "eksik ürün verildi"),
        "issue-full-refund": raise_issue("issue-full-refund", "QUALITY_PROBLEM",
                                         "ürün yanlış hazırlandı"),
        "issue-partial-refund": raise_issue("issue-partial-refund", "WRONG_ITEM",
                                            "yanlış sos kondu"),
        "issue-no-refund": raise_issue("issue-no-refund", "CUSTOMER_CANCELLED",
                                       "müşteri vazgeçti"),
    }
    db.commit()

    # issue-open stays OPEN (an attention item). The rest are resolved.
    resolve("issue-full-refund", "FULL_REFUND", "tam iade yapıldı")
    resolve("issue-partial-refund", "PARTIAL_REFUND", "yarısı iade edildi",
            approved=(_order_total(world, next(s for s in _ORDERS if s.slug == "issue-partial-refund"))
                      * _dq("0.5")).quantize(Decimal("0.01")))
    resolve("issue-no-refund", "NO_REFUND", "bilgilendirildi, iade yok")
    db.commit()


# ══════════════════════════════════════════════════════════════════════════════
# Cashier shifts (one open, one closed clean, one closed with discrepancy)
# ══════════════════════════════════════════════════════════════════════════════
def seed_shifts(db: Session, world: DemoWorld) -> None:
    logger.info("→ Cashier shifts…")

    # Open shift — cashier.demo, who also collected today's payments. Open shifts
    # carry no snapshot, so their window capturing the demo payments is harmless.
    cashier = make_ctx(world.cashier)
    opened = cashier_shift_service.open_shift(
        db, cashier,
        ShiftOpenRequest(opening_cash_amount=_dq("250.00"),
                         open_note=f"{DEMO_MARKER} açılış"),
        idempotency_key=key("shift-open", "cashier"),
    )
    SUMMARY.note("shift_open", created=not opened.idempotent_replay)
    db.commit()

    # Closed shifts — cashier2.demo, who collected nothing, so expected == opening
    # and the discrepancy is exactly what we count. First: zero discrepancy.
    cashier2 = make_ctx(world.cashier2)
    clean = cashier_shift_service.open_shift(
        db, cashier2,
        ShiftOpenRequest(opening_cash_amount=_dq("200.00"),
                         open_note=f"{DEMO_MARKER} temiz vardiya"),
        idempotency_key=key("shift-open", "clean"),
    )
    SUMMARY.note("shift_open", created=not clean.idempotent_replay)
    db.commit()
    closed_clean = cashier_shift_service.close_shift(
        db, cashier2, clean.id,
        ShiftCloseRequest(counted_closing_cash_amount=_dq("200.00"),
                          close_note=f"{DEMO_MARKER} denk kapanış"),
        idempotency_key=key("shift-close", "clean"),
    )
    SUMMARY.note("shift_closed", created=not closed_clean.idempotent_replay)
    db.commit()

    # Second closed shift for the same cashier: a small cash shortage (eksik).
    short = cashier_shift_service.open_shift(
        db, cashier2,
        ShiftOpenRequest(opening_cash_amount=_dq("150.00"),
                         open_note=f"{DEMO_MARKER} farklı vardiya"),
        idempotency_key=key("shift-open", "discrepancy"),
    )
    SUMMARY.note("shift_open", created=not short.idempotent_replay)
    db.commit()
    closed_short = cashier_shift_service.close_shift(
        db, cashier2, short.id,
        ShiftCloseRequest(counted_closing_cash_amount=_dq("145.00"),
                          close_note=f"{DEMO_MARKER} 5 TL eksik"),
        idempotency_key=key("shift-close", "discrepancy"),
    )
    SUMMARY.note("shift_closed", created=not closed_short.idempotent_replay)
    db.commit()


# ══════════════════════════════════════════════════════════════════════════════
# Preflight + main
# ══════════════════════════════════════════════════════════════════════════════
def _assert_migrated(db: Session) -> None:
    """Fail early and clearly if the schema has not been migrated to head."""
    required = ("stores", "cashier_shifts", "order_issues", "payment_settlements",
                "ingredient_stock_movements", "inventory_stock_counts")
    try:
        for tbl in required:
            db.execute(text(f"SELECT 1 FROM {tbl} LIMIT 1"))
    except (OperationalError, ProgrammingError) as exc:
        db.rollback()
        raise SystemExit(
            "\nERROR: the database schema is not migrated.\n"
            f"  Missing/unreadable table while checking '{tbl}'.\n"
            "  Run migrations first:\n"
            "      cd apps/api && python -m alembic upgrade head\n"
            f"  (underlying error: {type(exc).__name__})\n"
        )


def main() -> None:
    logger.info("=" * 60)
    logger.info("SweetOps deterministic demo seed")
    logger.info("=" * 60)
    db = SessionLocal()
    try:
        _assert_migrated(db)
        world = seed_foundation(db)
        seed_inventory(db, world)
        seed_orders_payments_issues(db, world)
        seed_shifts(db, world)
        SUMMARY.print()
        logger.info(
            "\nDemo ready. Stores: %s (primary) + %s (transfer dest).",
            PRIMARY_STORE_NAME, SECONDARY_STORE_NAME,
        )
        logger.info("Local demo login (DEV ONLY) — password for every account: %s",
                    DEMO_PASSWORD)
        for uname, _ in _PRIMARY_USERS:
            logger.info("    %s", uname)
        logger.info("    manager.moda.demo@sweetops.local")
        logger.info("\nNon-demo data (store 1 and any other store) was not touched.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
