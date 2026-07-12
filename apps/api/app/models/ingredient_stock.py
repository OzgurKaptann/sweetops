"""
Store-scoped inventory summary + append-only movement ledger.

Catalog versus physical stock
-----------------------------
``ingredients`` is CATALOG: the definition of a thing the chain sells (name,
unit, recipe standard quantity, price). It is deliberately global — every branch
sells the same Nutella, and duplicating the recipe per store would make a menu
change a per-branch migration.

Everything in THIS module is PHYSICAL: jars actually sitting on a shelf in one
named branch. Physical stock cannot be global, because a jar in Kadıköy is not a
jar in Beşiktaş. Every row here therefore carries ``store_id``, and the database
— not the application — is what refuses to mix two stores (see the composite
foreign keys below).

Quantity model
--------------
    available_quantity = on_hand_quantity - reserved_quantity

  on_hand_quantity  physical stock in THIS store, right now. Only a real physical
                    event moves it: consumption in the kitchen, waste, a return,
                    a purchase receipt, or a manual count adjustment.
  reserved_quantity claimed by accepted-but-not-yet-cooked orders OF THIS STORE.
                    It is a promise, not a physical fact — it never touches
                    on-hand.
  available_quantity generated (STORED) by PostgreSQL, so the identity above can
                    never drift from the two columns it is derived from.

Placing an order reserves; it does not consume. The waffle batter is only gone
once the kitchen actually starts cooking.

Cross-store integrity
---------------------
Scoping is not "the application remembers to add WHERE store_id = ?". A single
forgotten filter would let Store A's order eat Store B's chocolate, and no test
would necessarily catch it. So the invariants are composite foreign keys, which
a query cannot forget:

    movement (store_id, ingredient_id)          → ingredient_stock
    movement (store_id, order_id)               → orders
    movement (store_id, order_inventory_line_id)→ order_inventory_lines
    movement (store_id, actor_user_id)          → users
    line     (store_id, order_id)               → orders
    line     (store_id, ingredient_id)          → ingredient_stock

Each one makes a cross-store row unrepresentable rather than merely unwritten.
"""
from sqlalchemy import (
    CheckConstraint,
    Column,
    Computed,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    BigInteger,
    Boolean,
    Numeric,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from .base import Base

# All inventory quantities are exact decimals. Never binary floating point:
# 0.1 g of vanilla paste added a thousand times must still be exactly 100 g.
QTY = Numeric(12, 3)


# ── Movement type domain ─────────────────────────────────────────────────────
MOVEMENT_RESERVATION_CREATED = "RESERVATION_CREATED"
MOVEMENT_RESERVATION_RELEASED = "RESERVATION_RELEASED"
MOVEMENT_CONSUMPTION = "CONSUMPTION"
MOVEMENT_WASTE = "WASTE"
MOVEMENT_RETURNED = "RETURNED"
MOVEMENT_MANUAL_ADJUSTMENT = "MANUAL_ADJUSTMENT"
MOVEMENT_PURCHASE_RECEIPT = "PURCHASE_RECEIPT"

# The two halves of ONE store-to-store transfer. They are deliberately their own
# types rather than a signed MANUAL_ADJUSTMENT, because analytics must be able to
# tell shipped-to-the-other-branch apart from thrown-in-the-bin and
# bought-from-a-supplier. See app/models/inventory_transfer.py.
MOVEMENT_TRANSFER_OUT = "TRANSFER_OUT"
MOVEMENT_TRANSFER_IN = "TRANSFER_IN"

MOVEMENT_TYPES = (
    MOVEMENT_RESERVATION_CREATED,
    MOVEMENT_RESERVATION_RELEASED,
    MOVEMENT_CONSUMPTION,
    MOVEMENT_WASTE,
    MOVEMENT_RETURNED,
    MOVEMENT_MANUAL_ADJUSTMENT,
    MOVEMENT_PURCHASE_RECEIPT,
    MOVEMENT_TRANSFER_OUT,
    MOVEMENT_TRANSFER_IN,
)

TRANSFER_MOVEMENT_TYPES = (MOVEMENT_TRANSFER_OUT, MOVEMENT_TRANSFER_IN)

# Physical outflow that is NOT sale-driven consumption. Grouped so a reporting
# query cannot accidentally treat a branch shipment as a customer eating a waffle.
NON_CONSUMPTION_OUTFLOW_TYPES = (MOVEMENT_WASTE, MOVEMENT_TRANSFER_OUT)

# Movement types that only ever originate from a deliberate human action, and
# therefore require an authenticated actor.
#
# TRANSFER_IN is pointedly NOT here. Its actor is the SOURCE store's manager, and
# the movement lands in the DESTINATION store — so naming them as its actor would
# violate fk_movement_actor_store, which says staff only move stock in their own
# store. That constraint is right and the transfer does not get to weaken it: the
# inbound leg carries no actor, and accountability lives on the transfer row's
# initiated_by_user_id instead, which is exactly as traceable and does not
# fabricate a Beşiktaş action by a Kadıköy manager.
MANUAL_MOVEMENT_TYPES = (
    MOVEMENT_MANUAL_ADJUSTMENT,
    MOVEMENT_WASTE,
    MOVEMENT_RETURNED,
    MOVEMENT_PURCHASE_RECEIPT,
    MOVEMENT_TRANSFER_OUT,
)
# Both transfer legs carry the transfer's reason: an unexplained shipment of stock
# out of a branch is indistinguishable from stock walking out of the door, and the
# receiving branch is entitled to know why a crate turned up.
REASON_REQUIRED_TYPES = (
    MOVEMENT_MANUAL_ADJUSTMENT,
    MOVEMENT_WASTE,
    MOVEMENT_TRANSFER_OUT,
    MOVEMENT_TRANSFER_IN,
)

_MOVEMENT_TYPE_SQL = ",".join(f"'{t}'" for t in MOVEMENT_TYPES)
_MANUAL_TYPE_SQL = ",".join(f"'{t}'" for t in MANUAL_MOVEMENT_TYPES)
_REASON_TYPE_SQL = ",".join(f"'{t}'" for t in REASON_REQUIRED_TYPES)
_TRANSFER_TYPE_SQL = ",".join(f"'{t}'" for t in TRANSFER_MOVEMENT_TYPES)


class IngredientStock(Base):
    """
    One summary row per (store, ingredient) — a fast-query mirror of that
    store's slice of the ledger.

    The grain is the whole point of this model. Before store scoping the grain
    was one row per ingredient for the entire chain, which silently asserted
    that all branches share one jar of Nutella.
    """

    __tablename__ = "ingredient_stock"

    id = Column(Integer, primary_key=True, index=True)
    store_id = Column(Integer, ForeignKey("stores.id"), nullable=False, index=True)
    ingredient_id = Column(Integer, ForeignKey("ingredients.id"), nullable=False, index=True)

    # Renamed from the pre-lifecycle `stock_quantity`, which conflated on-hand
    # with available because orders deducted physical stock at creation time.
    on_hand_quantity = Column(QTY, nullable=False, server_default="0")
    reserved_quantity = Column(QTY, nullable=False, server_default="0")

    # Generated by the database, never written by the application.
    available_quantity = Column(
        QTY,
        Computed("on_hand_quantity - reserved_quantity", persisted=True),
        nullable=False,
    )

    unit = Column(String(10), nullable=False)
    reorder_level = Column(Numeric(10, 2), nullable=True)
    last_restocked = Column(DateTime(timezone=True), nullable=True)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    ingredient = relationship("Ingredient", foreign_keys=[ingredient_id])
    store = relationship("Store", foreign_keys=[store_id])

    __table_args__ = (
        # The grain. Two rows for the same ingredient in one store would let two
        # concurrent orders lock different rows and each believe it had the last
        # 200 g of pistachio. It is also the target the composite foreign keys
        # below point at, so it is what makes cross-store rows unrepresentable.
        UniqueConstraint("store_id", "ingredient_id", name="uq_stock_store_ingredient"),
        CheckConstraint("on_hand_quantity >= 0", name="ck_stock_on_hand_nonneg"),
        CheckConstraint("reserved_quantity >= 0", name="ck_stock_reserved_nonneg"),
        # Backorders are NOT allowed: a shop cannot promise batter it does not
        # physically have. This also makes available_quantity >= 0 structurally.
        CheckConstraint(
            "reserved_quantity <= on_hand_quantity",
            name="ck_stock_reserved_le_on_hand",
        ),
    )


class OrderInventoryLine(Base):
    """
    Per-order inventory allocation — the bridge between an order and physical
    stock, at one row per (order_item, ingredient).

    ``reserved_quantity`` is written once at order creation and never changes.
    The lifecycle then plays out inside this row:

        outstanding reservation = reserved - consumed - released

    which is what start-of-preparation consumes and what cancellation releases.
    Because both are driven by the same expression and the database enforces
    ``consumed + released <= reserved``, an order can never be consumed twice,
    released twice, or both consumed and released for the same quantity.

    ``waste_quantity`` / ``returned_quantity`` record physical events booked
    against this order after consumption. They do not unwind the reservation.

    ``store_id`` is the order's store, and the database enforces that: a line
    cannot claim a store its order does not belong to, and it cannot point at a
    stock row in a different store.
    """

    __tablename__ = "order_inventory_lines"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    store_id = Column(Integer, ForeignKey("stores.id"), nullable=False, index=True)
    order_id = Column(Integer, ForeignKey("orders.id"), nullable=False, index=True)
    order_item_id = Column(Integer, ForeignKey("order_items.id"), nullable=False, index=True)
    ingredient_id = Column(Integer, ForeignKey("ingredients.id"), nullable=False, index=True)

    reserved_quantity = Column(QTY, nullable=False, server_default="0")
    consumed_quantity = Column(QTY, nullable=False, server_default="0")
    released_quantity = Column(QTY, nullable=False, server_default="0")
    waste_quantity = Column(QTY, nullable=False, server_default="0")
    returned_quantity = Column(QTY, nullable=False, server_default="0")

    unit = Column(String(10), nullable=False)

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    # foreign_keys is required, not decorative: there are now TWO foreign keys
    # from this table to orders — the plain order_id, and the composite
    # (store_id, order_id) that enforces the store match — so the ORM cannot
    # infer which one to join on.
    order = relationship("Order", foreign_keys=[order_id])
    order_item = relationship("OrderItem")
    ingredient = relationship("Ingredient", foreign_keys=[ingredient_id])
    store = relationship("Store", foreign_keys=[store_id])

    __table_args__ = (
        # A line's store must BE its order's store — not merely equal it at the
        # moment the application wrote the row.
        ForeignKeyConstraint(
            ["store_id", "order_id"],
            ["orders.store_id", "orders.id"],
            name="fk_oil_order_store",
        ),
        # ...and it must allocate against a stock row that exists in that same
        # store. A store cannot reserve an ingredient it does not stock.
        ForeignKeyConstraint(
            ["store_id", "ingredient_id"],
            ["ingredient_stock.store_id", "ingredient_stock.ingredient_id"],
            name="fk_oil_stock_store",
        ),
        CheckConstraint("reserved_quantity >= 0", name="ck_oil_reserved_nonneg"),
        CheckConstraint("consumed_quantity >= 0", name="ck_oil_consumed_nonneg"),
        CheckConstraint("released_quantity >= 0", name="ck_oil_released_nonneg"),
        CheckConstraint("waste_quantity >= 0", name="ck_oil_waste_nonneg"),
        CheckConstraint("returned_quantity >= 0", name="ck_oil_returned_nonneg"),
        # The core anti-double-mutation invariant.
        CheckConstraint(
            "consumed_quantity + released_quantity <= reserved_quantity",
            name="ck_oil_settled_le_reserved",
        ),
        Index("uq_oil_item_ingredient", "order_item_id", "ingredient_id", unique=True),
        # Target for the movement ledger's (store_id, order_inventory_line_id)
        # composite FK: a movement cannot cite a line from another store.
        UniqueConstraint("id", "store_id", name="uq_oil_id_store"),
        Index("ix_oil_store_order_ingredient", "store_id", "order_id", "ingredient_id"),
    )


class IngredientStockMovement(Base):
    """
    The append-only inventory ledger — the source of truth for stock.

    Every row states, explicitly, what kind of event happened and how it moved
    each of the two summary quantities. There are no ambiguous bare signs: a
    ``-500`` is meaningless on its own, whereas ``CONSUMPTION`` with
    ``delta_on_hand = -500, delta_reserved = -500`` is unambiguous.

    Per movement type (enforced by ck_movement_delta_matches_type):

        RESERVATION_CREATED    on_hand  0          reserved +quantity
        RESERVATION_RELEASED   on_hand  0          reserved -quantity
        CONSUMPTION            on_hand -quantity   reserved -quantity
        WASTE                  on_hand -quantity   reserved  0
        RETURNED               on_hand +quantity   reserved  0
        PURCHASE_RECEIPT       on_hand +quantity   reserved  0
        MANUAL_ADJUSTMENT      on_hand ±quantity   reserved  0
        TRANSFER_OUT           on_hand -quantity   reserved  0
        TRANSFER_IN            on_hand +quantity   reserved  0

    The last two are never written alone. They come in pairs, they carry a
    ``transfer_id``, and the composite keys below pin each one to the correct SIDE
    of that transfer — the OUT leg to its source store, the IN leg to its
    destination store, both to its ingredient. A deferred constraint trigger then
    refuses, at COMMIT, any transfer that does not have exactly one of each. So a
    TRANSFER_OUT with no matching TRANSFER_IN — stock that left one branch and
    arrived nowhere — cannot be committed at all.

    UPDATE and DELETE are refused by a database trigger. A correction is a new
    compensating row, never an edit.

    Every row belongs to exactly one store, and every one of its references —
    the stock row it moves, the order it serves, the line it settles, the member
    of staff who caused it — must belong to that same store. Those are composite
    foreign keys, so a cross-store ledger row cannot be written at all.
    """

    __tablename__ = "ingredient_stock_movements"

    id = Column(Integer, primary_key=True, index=True)
    store_id = Column(Integer, ForeignKey("stores.id"), nullable=False, index=True)
    ingredient_id = Column(Integer, ForeignKey("ingredients.id"), nullable=False, index=True)

    movement_type = Column(String(30), nullable=False, index=True)

    # Always the positive magnitude of the event. The direction lives in the
    # deltas below, which the movement type constrains.
    quantity = Column(QTY, nullable=False)
    quantity_delta_on_hand = Column(QTY, nullable=False, server_default="0")
    quantity_delta_reserved = Column(QTY, nullable=False, server_default="0")

    unit = Column(String(10), nullable=False)

    # Order lineage — null for manual, non-order movements.
    order_id = Column(Integer, ForeignKey("orders.id"), nullable=True, index=True)
    order_item_id = Column(Integer, ForeignKey("order_items.id"), nullable=True)
    order_inventory_line_id = Column(
        BigInteger, ForeignKey("order_inventory_lines.id"), nullable=True, index=True
    )

    reason = Column(String(500), nullable=True)
    actor_user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)

    # Transfer lineage — the shared identity that makes the two legs ONE event.
    # Null for every non-transfer movement, and (by ck_movement_transfer_link)
    # mandatory for both transfer movements.
    transfer_id = Column(
        BigInteger, ForeignKey("inventory_transfers.id"), nullable=True, index=True
    )

    # Direction discriminators, GENERATED by PostgreSQL from movement_type and
    # store_id — the application cannot write or forge them.
    #
    # They exist to turn "the OUT leg must be booked in the transfer's source
    # store, the IN leg in its destination store" from an application rule into a
    # foreign key. A single FK cannot conditionally target two different columns
    # of inventory_transfers, so the direction is projected into its own column
    # and each FK is left MATCH SIMPLE: on a TRANSFER_OUT row, transfer_in_store_id
    # is NULL and the inbound FK simply does not apply, and vice versa. On a
    # non-transfer row transfer_id is NULL and neither applies.
    transfer_out_store_id = Column(
        Integer,
        Computed(
            f"CASE WHEN movement_type = '{MOVEMENT_TRANSFER_OUT}' THEN store_id END",
            persisted=True,
        ),
        nullable=True,
    )
    transfer_in_store_id = Column(
        Integer,
        Computed(
            f"CASE WHEN movement_type = '{MOVEMENT_TRANSFER_IN}' THEN store_id END",
            persisted=True,
        ),
        nullable=True,
    )

    # Idempotency for manual mutations: only SHA-256 hashes are ever stored,
    # never the raw Idempotency-Key or the raw request body.
    idempotency_key_hash = Column(String(64), nullable=True)
    request_hash = Column(String(64), nullable=True)

    # True only for rows reconstructed by the inventory-lifecycle migration from
    # the pre-lifecycle ledger, where an actor and a reason were never captured
    # and cannot honestly be invented after the fact. The actor/reason/delta
    # constraints exempt these rows and only these rows.
    legacy_backfill = Column(Boolean, nullable=False, server_default="false")

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    # Explicit foreign_keys throughout: the composite cross-store keys below add
    # a second FK path to users (and to orders), so the ORM can no longer infer
    # which columns a relationship joins on.
    ingredient = relationship("Ingredient", foreign_keys=[ingredient_id])
    actor = relationship("User", foreign_keys=[actor_user_id])
    store = relationship("Store", foreign_keys=[store_id])

    __table_args__ = (
        # ── Cross-store integrity, enforced by the database ──────────────────
        # Each of these is MATCH SIMPLE: when the nullable half is NULL (a
        # manual movement has no order; an order movement has no actor) the
        # constraint simply does not apply. When it is present, the referenced
        # row MUST be in this movement's store.
        ForeignKeyConstraint(
            ["store_id", "ingredient_id"],
            ["ingredient_stock.store_id", "ingredient_stock.ingredient_id"],
            name="fk_movement_stock_store",
        ),
        ForeignKeyConstraint(
            ["store_id", "order_id"],
            ["orders.store_id", "orders.id"],
            name="fk_movement_order_store",
        ),
        ForeignKeyConstraint(
            ["store_id", "order_inventory_line_id"],
            ["order_inventory_lines.store_id", "order_inventory_lines.id"],
            name="fk_movement_line_store",
        ),
        # Staff may only move stock in their own store. Because users.store_id
        # is nullable, a user with no store assignment can never be the actor of
        # a movement — which is the correct answer, not an accident.
        ForeignKeyConstraint(
            ["store_id", "actor_user_id"],
            ["users.store_id", "users.id"],
            name="fk_movement_actor_store",
        ),
        # ── Transfer leg integrity ───────────────────────────────────────────
        # The OUT leg's (transfer, store, ingredient) must BE the transfer's
        # (id, source_store, ingredient); the IN leg's must be its
        # (id, destination_store, ingredient). Booking the outbound half of a
        # Kadıköy → Beşiktaş transfer against Beşiktaş's shelf, or against the
        # wrong ingredient, is therefore not a bug that review has to catch — it
        # is a row PostgreSQL will not store.
        ForeignKeyConstraint(
            ["transfer_id", "transfer_out_store_id", "ingredient_id"],
            [
                "inventory_transfers.id",
                "inventory_transfers.source_store_id",
                "inventory_transfers.ingredient_id",
            ],
            name="fk_movement_transfer_source_leg",
        ),
        ForeignKeyConstraint(
            ["transfer_id", "transfer_in_store_id", "ingredient_id"],
            [
                "inventory_transfers.id",
                "inventory_transfers.destination_store_id",
                "inventory_transfers.ingredient_id",
            ],
            name="fk_movement_transfer_destination_leg",
        ),
        CheckConstraint("quantity > 0", name="ck_movement_quantity_positive"),
        CheckConstraint(
            f"movement_type IN ({_MOVEMENT_TYPE_SQL})",
            name="ck_movement_type_domain",
        ),
        CheckConstraint(
            f"legacy_backfill OR movement_type NOT IN ({_MANUAL_TYPE_SQL})"
            " OR actor_user_id IS NOT NULL",
            name="ck_movement_actor_required",
        ),
        CheckConstraint(
            f"legacy_backfill OR movement_type NOT IN ({_REASON_TYPE_SQL})"
            " OR (reason IS NOT NULL AND char_length(btrim(reason)) > 0)",
            name="ck_movement_reason_required",
        ),
        CheckConstraint(
            """
            legacy_backfill
            OR (movement_type = 'RESERVATION_CREATED'
                AND quantity_delta_on_hand = 0
                AND quantity_delta_reserved = quantity)
            OR (movement_type = 'RESERVATION_RELEASED'
                AND quantity_delta_on_hand = 0
                AND quantity_delta_reserved = -quantity)
            OR (movement_type = 'CONSUMPTION'
                AND quantity_delta_on_hand = -quantity
                AND quantity_delta_reserved = -quantity)
            OR (movement_type = 'WASTE'
                AND quantity_delta_on_hand = -quantity
                AND quantity_delta_reserved = 0)
            OR (movement_type IN ('RETURNED', 'PURCHASE_RECEIPT')
                AND quantity_delta_on_hand = quantity
                AND quantity_delta_reserved = 0)
            OR (movement_type = 'MANUAL_ADJUSTMENT'
                AND abs(quantity_delta_on_hand) = quantity
                AND quantity_delta_reserved = 0)
            OR (movement_type = 'TRANSFER_OUT'
                AND quantity_delta_on_hand = -quantity
                AND quantity_delta_reserved = 0)
            OR (movement_type = 'TRANSFER_IN'
                AND quantity_delta_on_hand = quantity
                AND quantity_delta_reserved = 0)
            """,
            name="ck_movement_delta_matches_type",
        ),
        # A transfer movement without a transfer is an orphan half of an event
        # that nothing can reconcile; a transfer_id on any other type would let a
        # purchase receipt masquerade as an arriving shipment. Both are refused.
        CheckConstraint(
            f"(movement_type IN ({_TRANSFER_TYPE_SQL})) = (transfer_id IS NOT NULL)",
            name="ck_movement_transfer_link",
        ),
        # The inbound leg happens in the destination store, but the person who
        # authorised it works in the SOURCE store. Rather than fabricate a
        # destination-store actor (which fk_movement_actor_store would rightly
        # refuse), the inbound leg carries none: the transfer row's
        # initiated_by_user_id is who did it.
        CheckConstraint(
            "movement_type <> 'TRANSFER_IN' OR actor_user_id IS NULL",
            name="ck_movement_transfer_in_no_actor",
        ),
        # At most one movement per direction per transfer. Combined with the
        # deferred pairing trigger (exactly one of each at COMMIT) and
        # ck_movement_transfer_link (only transfer types may carry a transfer_id),
        # a transfer has EXACTLY two movements — never one, never three.
        Index(
            "uq_movement_transfer_direction",
            "transfer_id",
            "movement_type",
            unique=True,
            postgresql_where=text("transfer_id IS NOT NULL"),
        ),
        # Idempotency is scoped to the store. Two branches are two independent
        # operations run by two independent managers; that they both happened to
        # send "Idempotency-Key: 1" is a coincidence, not a replay, and must not
        # make Beşiktaş's purchase receipt silently return Kadıköy's result.
        #
        # Partial: only rows that actually carry a key participate, so the many
        # order-driven movements (which have no key of their own) never collide
        # on NULL.
        Index(
            "uq_movement_store_idem",
            "store_id",
            "idempotency_key_hash",
            unique=True,
            postgresql_where=text("idempotency_key_hash IS NOT NULL"),
        ),
        Index("ix_movement_type_created", "movement_type", "created_at"),
        Index("ix_movement_store_ingredient_created",
              "store_id", "ingredient_id", "created_at"),
    )
