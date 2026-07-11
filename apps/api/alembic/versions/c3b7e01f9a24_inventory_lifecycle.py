"""inventory lifecycle: reservation/consumption model, order inventory lines, ledger

Revision ID: c3b7e01f9a24
Revises: b8c4d1e6f207
Create Date: 2026-07-11

Turns the single conflated stock column into a real inventory lifecycle.

Schema
------
  ingredient_stock
      stock_quantity      → on_hand_quantity   (renamed; value preserved as-is)
      + reserved_quantity                      (default 0)
      + available_quantity                     GENERATED ALWAYS AS
                                               (on_hand - reserved) STORED
      Constraints: both quantities non-negative, and reserved <= on_hand
      (backorders are NOT permitted — a shop cannot promise batter it does not
      physically have).

  order_inventory_lines   (new)
      One row per (order_item, ingredient). Holds reserved / consumed /
      released / waste / returned quantities, with a database CHECK that
      consumed + released <= reserved. That single constraint is what makes
      double-consumption and double-release structurally impossible.

  ingredient_stock_movements  (reshaped into a proper append-only ledger)
      + quantity                  (always the positive magnitude)
      + quantity_delta_on_hand    ) direction lives here, and the movement type
      + quantity_delta_reserved   ) constrains both — see ck_movement_delta_matches_type
      + order_id / order_item_id / order_inventory_line_id   (lineage)
      + actor_user_id, idempotency_key_hash, request_hash
      + legacy_backfill
      note        → reason (widened)
      - quantity_delta, reference_type, reference_id   (dropped: an unqualified
        signed delta is exactly the ambiguity this lifecycle removes)

Data safety
-----------
  - Existing orders, order items and ALL payment data are untouched.
  - Existing physical stock values are preserved byte-for-byte: on_hand keeps
    whatever stock_quantity held, and reserved starts at 0.
  - No physical consumption is fabricated. Every historical order is backfilled
    to the state the OLD code had actually already put the world in — see the
    backfill assumptions below.
  - An opening-balance row is reconstructed per ingredient so that the ledger
    sums exactly to the stored on-hand quantity, making reconciliation
    meaningful from the first run rather than reporting a false drift on every
    ingredient that existed before this migration.

Backfill assumptions (deterministic, documented, not guessed)
------------------------------------------------------------
The pre-lifecycle code physically deducted stock at ORDER CREATION and restored
it on cancellation. So, for every existing order_item_ingredient with a
persisted consumed_quantity:

  * order CANCELLED
        reserved = consumed = returned = q
        The old code deducted q at creation and added q back on cancel. Marking
        it consumed-then-returned reproduces exactly that pair of physical
        events; net effect on on-hand is zero, and nothing is left outstanding.

  * order NEW / IN_PREP / READY / DELIVERED
        reserved = consumed = q, released = 0
        The old code had ALREADY deducted q from physical stock, whatever the
        preparation status. Recording it as consumed is the only classification
        that keeps stored on-hand correct.

        This deliberately includes NEW orders. Treating a historical NEW order
        as merely "reserved" would be the more elegant story, but it would be a
        lie about the database we inherited: the stock is already gone. It would
        also double-count — reserved would rise against an on-hand figure that
        had already been reduced — and would then be consumed a SECOND time when
        the kitchen started it. Historical NEW orders therefore carry no
        outstanding reservation: starting one consumes nothing further, and
        cancelling one releases nothing. New orders placed after this migration
        get the full reservation lifecycle.

Movement type mapping (old → new):
    ORDER_DEDUCTION     → CONSUMPTION
    CANCELLATION_RETURN → RETURNED
    RESTOCK             → PURCHASE_RECEIPT
    MANUAL_ADJUST       → MANUAL_ADJUSTMENT
    WASTE               → WASTE

Every backfilled movement is flagged legacy_backfill = true. The actor, reason
and delta-consistency constraints exempt those rows and only those rows: the old
ledger never captured an actor or a reason, and inventing one after the fact
would be fabricating an audit trail. Rows written from now on carry the full
constraints.

Append-only
-----------
UPDATE and DELETE on ingredient_stock_movements are refused by a trigger with no
application-accessible bypass (same hardening as the payment ledger: SECURITY
INVOKER, pinned search_path, schema-qualified, no dynamic SQL, EXECUTE revoked
from PUBLIC). The trigger is installed only AFTER the backfill has finished.

downgrade() removes only this branch's schema, restores the previous
stock_quantity / quantity_delta shape, and leaves every order and payment row
intact.
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "c3b7e01f9a24"
down_revision = "b8c4d1e6f207"
branch_labels = None
depends_on = None


QTY = sa.Numeric(12, 3)

_MOVEMENT_TYPES = (
    "RESERVATION_CREATED",
    "RESERVATION_RELEASED",
    "CONSUMPTION",
    "WASTE",
    "RETURNED",
    "MANUAL_ADJUSTMENT",
    "PURCHASE_RECEIPT",
)
_MANUAL_TYPES = ("MANUAL_ADJUSTMENT", "WASTE", "RETURNED", "PURCHASE_RECEIPT")
_REASON_TYPES = ("MANUAL_ADJUSTMENT", "WASTE")

_TYPES_SQL = ",".join(f"'{t}'" for t in _MOVEMENT_TYPES)
_MANUAL_SQL = ",".join(f"'{t}'" for t in _MANUAL_TYPES)
_REASON_SQL = ",".join(f"'{t}'" for t in _REASON_TYPES)


def upgrade() -> None:
    # ═══════════════════════════════════════════════════════════════════════
    # 1. ingredient_stock — split the conflated quantity into on-hand/reserved
    # ═══════════════════════════════════════════════════════════════════════
    op.alter_column(
        "ingredient_stock",
        "stock_quantity",
        new_column_name="on_hand_quantity",
        type_=QTY,
        existing_nullable=False,
    )
    op.add_column(
        "ingredient_stock",
        sa.Column("reserved_quantity", QTY, nullable=False, server_default="0"),
    )
    # Generated by PostgreSQL, so available can never drift from its inputs.
    op.execute(
        """
        ALTER TABLE ingredient_stock
        ADD COLUMN available_quantity numeric(12,3)
        GENERATED ALWAYS AS (on_hand_quantity - reserved_quantity) STORED
        """
    )

    op.create_check_constraint(
        "ck_stock_on_hand_nonneg", "ingredient_stock", "on_hand_quantity >= 0"
    )
    op.create_check_constraint(
        "ck_stock_reserved_nonneg", "ingredient_stock", "reserved_quantity >= 0"
    )
    op.create_check_constraint(
        "ck_stock_reserved_le_on_hand",
        "ingredient_stock",
        "reserved_quantity <= on_hand_quantity",
    )

    # ═══════════════════════════════════════════════════════════════════════
    # 2. order_inventory_lines
    # ═══════════════════════════════════════════════════════════════════════
    op.create_table(
        "order_inventory_lines",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("order_id", sa.Integer(), nullable=False),
        sa.Column("order_item_id", sa.Integer(), nullable=False),
        sa.Column("ingredient_id", sa.Integer(), nullable=False),
        sa.Column("reserved_quantity", QTY, nullable=False, server_default="0"),
        sa.Column("consumed_quantity", QTY, nullable=False, server_default="0"),
        sa.Column("released_quantity", QTY, nullable=False, server_default="0"),
        sa.Column("waste_quantity", QTY, nullable=False, server_default="0"),
        sa.Column("returned_quantity", QTY, nullable=False, server_default="0"),
        sa.Column("unit", sa.String(length=10), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["order_id"], ["orders.id"]),
        sa.ForeignKeyConstraint(["order_item_id"], ["order_items.id"]),
        sa.ForeignKeyConstraint(["ingredient_id"], ["ingredients.id"]),
        sa.CheckConstraint("reserved_quantity >= 0", name="ck_oil_reserved_nonneg"),
        sa.CheckConstraint("consumed_quantity >= 0", name="ck_oil_consumed_nonneg"),
        sa.CheckConstraint("released_quantity >= 0", name="ck_oil_released_nonneg"),
        sa.CheckConstraint("waste_quantity >= 0", name="ck_oil_waste_nonneg"),
        sa.CheckConstraint("returned_quantity >= 0", name="ck_oil_returned_nonneg"),
        sa.CheckConstraint(
            "consumed_quantity + released_quantity <= reserved_quantity",
            name="ck_oil_settled_le_reserved",
        ),
    )
    op.create_index("ix_order_inventory_lines_order_id", "order_inventory_lines", ["order_id"])
    op.create_index("ix_order_inventory_lines_order_item_id", "order_inventory_lines", ["order_item_id"])
    op.create_index("ix_order_inventory_lines_ingredient_id", "order_inventory_lines", ["ingredient_id"])
    op.create_index(
        "uq_oil_item_ingredient",
        "order_inventory_lines",
        ["order_item_id", "ingredient_id"],
        unique=True,
    )

    # ═══════════════════════════════════════════════════════════════════════
    # 3. ingredient_stock_movements — reshape into the lifecycle ledger
    # ═══════════════════════════════════════════════════════════════════════
    op.add_column("ingredient_stock_movements", sa.Column("quantity", QTY, nullable=True))
    op.add_column("ingredient_stock_movements",
                  sa.Column("quantity_delta_on_hand", QTY, nullable=False, server_default="0"))
    op.add_column("ingredient_stock_movements",
                  sa.Column("quantity_delta_reserved", QTY, nullable=False, server_default="0"))
    op.add_column("ingredient_stock_movements", sa.Column("order_id", sa.Integer(), nullable=True))
    op.add_column("ingredient_stock_movements", sa.Column("order_item_id", sa.Integer(), nullable=True))
    op.add_column("ingredient_stock_movements",
                  sa.Column("order_inventory_line_id", sa.BigInteger(), nullable=True))
    op.add_column("ingredient_stock_movements", sa.Column("actor_user_id", sa.Integer(), nullable=True))
    op.add_column("ingredient_stock_movements",
                  sa.Column("idempotency_key_hash", sa.String(length=64), nullable=True))
    op.add_column("ingredient_stock_movements",
                  sa.Column("request_hash", sa.String(length=64), nullable=True))
    op.add_column("ingredient_stock_movements",
                  sa.Column("legacy_backfill", sa.Boolean(), nullable=False, server_default="false"))

    # `note` was free text nobody wrote; `reason` is mandatory for waste and
    # manual adjustments, so reuse the column rather than leaving a dead one.
    op.alter_column(
        "ingredient_stock_movements",
        "note",
        new_column_name="reason",
        type_=sa.String(length=500),
        existing_nullable=True,
    )

    # ── 3a. Backfill the historical ledger ─────────────────────────────────
    # A zero-delta movement records nothing physical and cannot satisfy
    # quantity > 0. There is no information to preserve, so drop those rows.
    op.execute("DELETE FROM ingredient_stock_movements WHERE quantity_delta = 0")

    op.execute(
        """
        UPDATE ingredient_stock_movements
        SET quantity               = abs(quantity_delta),
            quantity_delta_on_hand = quantity_delta,
            quantity_delta_reserved = 0,
            legacy_backfill        = true,
            order_id = CASE WHEN reference_type = 'order' THEN reference_id END,
            movement_type = CASE movement_type
                WHEN 'ORDER_DEDUCTION'     THEN 'CONSUMPTION'
                WHEN 'CANCELLATION_RETURN' THEN 'RETURNED'
                WHEN 'RESTOCK'             THEN 'PURCHASE_RECEIPT'
                WHEN 'MANUAL_ADJUST'       THEN 'MANUAL_ADJUSTMENT'
                WHEN 'WASTE'               THEN 'WASTE'
                ELSE 'MANUAL_ADJUSTMENT'
            END
        """
    )
    # A referenced order may since have been deleted; keep the FK honest.
    op.execute(
        """
        UPDATE ingredient_stock_movements m
        SET order_id = NULL
        WHERE m.order_id IS NOT NULL
          AND NOT EXISTS (SELECT 1 FROM orders o WHERE o.id = m.order_id)
        """
    )
    op.alter_column("ingredient_stock_movements", "quantity", nullable=False)

    # ── 3b. Backfill order_inventory_lines from persisted requirements ─────
    # Grain: one row per (order_item, ingredient), summing any duplicate
    # ingredient rows within the same item.
    op.execute(
        """
        INSERT INTO order_inventory_lines (
            order_id, order_item_id, ingredient_id,
            reserved_quantity, consumed_quantity, released_quantity,
            waste_quantity, returned_quantity, unit
        )
        SELECT
            oi.order_id,
            oii.order_item_id,
            oii.ingredient_id,
            SUM(oii.consumed_quantity)                                   AS reserved_quantity,
            SUM(oii.consumed_quantity)                                   AS consumed_quantity,
            0                                                            AS released_quantity,
            0                                                            AS waste_quantity,
            CASE WHEN o.status = 'CANCELLED'
                 THEN SUM(oii.consumed_quantity) ELSE 0 END              AS returned_quantity,
            COALESCE(MAX(oii.consumed_unit), MAX(i.unit), 'g')           AS unit
        FROM order_item_ingredients oii
        JOIN order_items oi ON oi.id = oii.order_item_id
        JOIN orders      o  ON o.id  = oi.order_id
        JOIN ingredients i  ON i.id  = oii.ingredient_id
        WHERE oii.consumed_quantity IS NOT NULL
          AND oii.consumed_quantity > 0
        GROUP BY oi.order_id, oii.order_item_id, oii.ingredient_id, o.status
        """
    )

    # ── 3c. Retire the ambiguous signed-delta columns ──────────────────────
    # Done before any new row is written, so the opening-balance insert below
    # does not have to satisfy the old NOT NULL quantity_delta column.
    op.drop_column("ingredient_stock_movements", "quantity_delta")
    op.drop_column("ingredient_stock_movements", "reference_type")
    op.drop_column("ingredient_stock_movements", "reference_id")

    # ── 3d. Reconstruct an opening balance so the ledger sums to on-hand ───
    # Without this every pre-existing ingredient would reconcile as "drifted"
    # forever, because the old ledger only ever recorded deltas and never the
    # stock the shop started with.
    op.execute(
        f"""
        INSERT INTO ingredient_stock_movements (
            ingredient_id, movement_type, quantity,
            quantity_delta_on_hand, quantity_delta_reserved,
            unit, reason, legacy_backfill, created_at
        )
        SELECT
            s.ingredient_id,
            CASE WHEN opening.delta > 0 THEN 'PURCHASE_RECEIPT'
                 ELSE 'MANUAL_ADJUSTMENT' END,
            abs(opening.delta),
            opening.delta,
            0,
            s.unit,
            'migration c3b7e01f9a24: opening balance reconstructed so the ledger '
              'sums to the stock recorded before the inventory lifecycle existed',
            true,
            now()
        FROM ingredient_stock s
        CROSS JOIN LATERAL (
            SELECT s.on_hand_quantity - COALESCE((
                SELECT SUM(m.quantity_delta_on_hand)
                FROM ingredient_stock_movements m
                WHERE m.ingredient_id = s.ingredient_id
            ), 0) AS delta
        ) AS opening
        WHERE opening.delta <> 0
        """
    )

    # ── 3e. Keys, indexes and the movement-integrity constraints ───────────
    op.create_foreign_key(
        "fk_movement_order", "ingredient_stock_movements", "orders", ["order_id"], ["id"]
    )
    op.create_foreign_key(
        "fk_movement_order_item", "ingredient_stock_movements", "order_items",
        ["order_item_id"], ["id"],
    )
    op.create_foreign_key(
        "fk_movement_order_inventory_line", "ingredient_stock_movements",
        "order_inventory_lines", ["order_inventory_line_id"], ["id"],
    )
    op.create_foreign_key(
        "fk_movement_actor", "ingredient_stock_movements", "users", ["actor_user_id"], ["id"]
    )

    op.create_index("ix_ingredient_stock_movements_movement_type",
                    "ingredient_stock_movements", ["movement_type"])
    op.create_index("ix_ingredient_stock_movements_order_id",
                    "ingredient_stock_movements", ["order_id"])
    op.create_index("ix_ingredient_stock_movements_order_inventory_line_id",
                    "ingredient_stock_movements", ["order_inventory_line_id"])
    op.create_index("ix_ingredient_stock_movements_actor_user_id",
                    "ingredient_stock_movements", ["actor_user_id"])
    op.create_index("ix_movement_type_created",
                    "ingredient_stock_movements", ["movement_type", "created_at"])
    # Partial: only rows carrying a key take part in the uniqueness guarantee,
    # so the many order-driven movements (no key of their own) never collide.
    op.execute(
        """
        CREATE UNIQUE INDEX uq_movement_idem
        ON ingredient_stock_movements (idempotency_key_hash)
        WHERE idempotency_key_hash IS NOT NULL
        """
    )

    op.create_check_constraint(
        "ck_movement_quantity_positive", "ingredient_stock_movements", "quantity > 0"
    )
    op.create_check_constraint(
        "ck_movement_type_domain",
        "ingredient_stock_movements",
        f"movement_type IN ({_TYPES_SQL})",
    )
    op.create_check_constraint(
        "ck_movement_actor_required",
        "ingredient_stock_movements",
        f"legacy_backfill OR movement_type NOT IN ({_MANUAL_SQL})"
        " OR actor_user_id IS NOT NULL",
    )
    op.create_check_constraint(
        "ck_movement_reason_required",
        "ingredient_stock_movements",
        f"legacy_backfill OR movement_type NOT IN ({_REASON_SQL})"
        " OR (reason IS NOT NULL AND char_length(btrim(reason)) > 0)",
    )
    # The deltas must agree with the movement type. This is what makes a bare
    # sign impossible: a row cannot claim CONSUMPTION while adding to on-hand.
    op.create_check_constraint(
        "ck_movement_delta_matches_type",
        "ingredient_stock_movements",
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
        """,
    )

    # ═══════════════════════════════════════════════════════════════════════
    # 4. Append-only enforcement (installed AFTER the backfill has run)
    # ═══════════════════════════════════════════════════════════════════════
    # Same hardening as the payment ledger: SECURITY INVOKER (no privilege is
    # needed, so none is taken), a pinned search_path so object resolution can
    # never be diverted, schema-qualified references, no dynamic SQL, and EXECUTE
    # revoked from PUBLIC. There is deliberately no runtime bypass — no GUC or
    # session variable can switch it off, because any role (including via an
    # injection path) can set those. Correcting stock history is a new
    # compensating movement, never an edit.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION public.sweetops_block_inventory_mutation()
        RETURNS trigger LANGUAGE plpgsql
        SECURITY INVOKER
        SET search_path = pg_catalog
        AS $fn$
        BEGIN
            RAISE EXCEPTION
                'inventory ledger is append-only: % on % is not permitted',
                TG_OP, TG_TABLE_NAME
                USING ERRCODE = 'integrity_constraint_violation';
        END;
        $fn$;
        """
    )
    op.execute(
        "REVOKE ALL ON FUNCTION public.sweetops_block_inventory_mutation() FROM PUBLIC"
    )
    op.execute(
        """
        CREATE TRIGGER trg_ingredient_stock_movements_immutable
        BEFORE UPDATE OR DELETE ON ingredient_stock_movements
        FOR EACH ROW EXECUTE FUNCTION public.sweetops_block_inventory_mutation();
        """
    )


def downgrade() -> None:
    # Removes ONLY this branch's inventory schema. Orders, order items and every
    # payment row are left untouched.
    op.execute(
        "DROP TRIGGER IF EXISTS trg_ingredient_stock_movements_immutable "
        "ON ingredient_stock_movements"
    )
    op.execute("DROP FUNCTION IF EXISTS public.sweetops_block_inventory_mutation()")

    # ── Movement ledger back to the pre-lifecycle shape ────────────────────
    for _ck in (
        "ck_movement_delta_matches_type",
        "ck_movement_reason_required",
        "ck_movement_actor_required",
        "ck_movement_type_domain",
        "ck_movement_quantity_positive",
    ):
        op.drop_constraint(_ck, "ingredient_stock_movements", type_="check")

    op.execute("DROP INDEX IF EXISTS uq_movement_idem")
    for _ix in (
        "ix_movement_type_created",
        "ix_ingredient_stock_movements_actor_user_id",
        "ix_ingredient_stock_movements_order_inventory_line_id",
        "ix_ingredient_stock_movements_order_id",
        "ix_ingredient_stock_movements_movement_type",
    ):
        op.drop_index(_ix, table_name="ingredient_stock_movements")

    for _fk in (
        "fk_movement_actor",
        "fk_movement_order_inventory_line",
        "fk_movement_order_item",
        "fk_movement_order",
    ):
        op.drop_constraint(_fk, "ingredient_stock_movements", type_="foreignkey")

    # Restore the signed-delta columns from the typed deltas.
    op.add_column("ingredient_stock_movements",
                  sa.Column("quantity_delta", sa.Numeric(10, 2), nullable=True))
    op.add_column("ingredient_stock_movements",
                  sa.Column("reference_type", sa.String(length=30), nullable=True))
    op.add_column("ingredient_stock_movements",
                  sa.Column("reference_id", sa.Integer(), nullable=True))
    op.execute(
        """
        UPDATE ingredient_stock_movements
        SET quantity_delta = quantity_delta_on_hand,
            reference_type = CASE WHEN order_id IS NOT NULL THEN 'order' END,
            reference_id   = order_id,
            movement_type  = CASE movement_type
                WHEN 'CONSUMPTION'       THEN 'ORDER_DEDUCTION'
                WHEN 'RETURNED'          THEN 'CANCELLATION_RETURN'
                WHEN 'PURCHASE_RECEIPT'  THEN 'RESTOCK'
                WHEN 'MANUAL_ADJUSTMENT' THEN 'MANUAL_ADJUST'
                WHEN 'WASTE'             THEN 'WASTE'
                ELSE 'MANUAL_ADJUST'
            END
        """
    )
    # Reservation rows have no meaning in the pre-lifecycle model — they never
    # moved physical stock (delta_on_hand = 0), so dropping them loses nothing
    # about on-hand history.
    op.execute(
        "DELETE FROM ingredient_stock_movements "
        "WHERE movement_type IN ('RESERVATION_CREATED', 'RESERVATION_RELEASED')"
    )
    op.alter_column("ingredient_stock_movements", "quantity_delta", nullable=False)

    for _col in (
        "legacy_backfill",
        "request_hash",
        "idempotency_key_hash",
        "actor_user_id",
        "order_inventory_line_id",
        "order_item_id",
        "order_id",
        "quantity_delta_reserved",
        "quantity_delta_on_hand",
        "quantity",
    ):
        op.drop_column("ingredient_stock_movements", _col)

    op.alter_column(
        "ingredient_stock_movements",
        "reason",
        new_column_name="note",
        type_=sa.String(),
        existing_nullable=True,
    )

    # ── order_inventory_lines ──────────────────────────────────────────────
    op.drop_index("uq_oil_item_ingredient", table_name="order_inventory_lines")
    op.drop_index("ix_order_inventory_lines_ingredient_id", table_name="order_inventory_lines")
    op.drop_index("ix_order_inventory_lines_order_item_id", table_name="order_inventory_lines")
    op.drop_index("ix_order_inventory_lines_order_id", table_name="order_inventory_lines")
    op.drop_table("order_inventory_lines")

    # ── ingredient_stock ───────────────────────────────────────────────────
    op.drop_constraint("ck_stock_reserved_le_on_hand", "ingredient_stock", type_="check")
    op.drop_constraint("ck_stock_reserved_nonneg", "ingredient_stock", type_="check")
    op.drop_constraint("ck_stock_on_hand_nonneg", "ingredient_stock", type_="check")
    op.drop_column("ingredient_stock", "available_quantity")
    op.drop_column("ingredient_stock", "reserved_quantity")
    op.alter_column(
        "ingredient_stock",
        "on_hand_quantity",
        new_column_name="stock_quantity",
        type_=sa.Numeric(10, 2),
        existing_nullable=False,
    )
