"""store-scoped inventory: stock, movement ledger and order inventory lines per store

Revision ID: e2c9a4b16d38
Revises: c3b7e01f9a24
Create Date: 2026-07-12

Physical stock stops being a property of the chain and becomes a property of a
branch.

Schema
------
  ingredient_stock
      + store_id (FK stores.id, NOT NULL)
      grain changes from  UNIQUE(ingredient_id)
                     to   UNIQUE(store_id, ingredient_id)
      That single line is the feature. One row per ingredient meant one jar of
      Nutella for the whole company; one row per (store, ingredient) means each
      branch has its own jar, its own availability, and its own stockout.

  ingredient_stock_movements
      + store_id (FK stores.id, NOT NULL)
      Composite FKs so a ledger row cannot cross stores:
          (store_id, ingredient_id)           → ingredient_stock
          (store_id, order_id)                → orders
          (store_id, order_inventory_line_id) → order_inventory_lines
          (store_id, actor_user_id)           → users
      Idempotency uniqueness becomes (store_id, idempotency_key_hash): two
      branches may use the same Idempotency-Key independently, because they are
      two independent operations and their collision is coincidence, not replay.

  order_inventory_lines
      + store_id (FK stores.id, NOT NULL)
      Composite FKs:
          (store_id, order_id)     → orders           (a line's store IS its order's store)
          (store_id, ingredient_id)→ ingredient_stock (a store cannot reserve what it does not stock)

  orders / users
      + UNIQUE(store_id, id). Redundant against their primary keys, but
      PostgreSQL requires a unique constraint on exactly the referenced pair
      before it will accept the composite foreign keys above.

Why composite foreign keys rather than application filters
----------------------------------------------------------
"Remember to add WHERE store_id = ?" is not an invariant, it is a habit. One
forgotten filter in one analytics query and Store A's order is eating Store B's
chocolate, silently, with a plausible-looking number on the dashboard. A
composite FK makes the cross-store row unrepresentable: the database rejects it
whatever the application believes. There is no runtime bypass.

Backfill — and where it refuses to guess
----------------------------------------
Two classes of row, and they are very different:

  DERIVABLE (exact, no assumption, works with any number of stores)
      order_inventory_lines.store_id  ← its order's store_id
      movements.store_id (order rows) ← its order's store_id
    An order already knows its store. Its inventory is that store's, necessarily.

  AMBIGUOUS (needs an assumption)
      ingredient_stock rows            — a global stock row names no store
      movements with order_id IS NULL  — opening balances, manual adjustments,
                                         waste, purchase receipts
    A global row saying "4 kg of pistachio" cannot be split across branches by
    any rule the database knows. 4 kg in Kadıköy? 2 and 2? Nobody can tell from
    the data.

So: if any AMBIGUOUS row exists, this migration requires exactly one operational
store and assigns them all to it. If it finds more than one, it ABORTS with an
explicit error rather than guessing. Guessing here would not throw an error — it
would produce a database that looks fine and is quietly wrong, which is the worst
possible outcome for stock. Splitting real stock across real branches is a
physical-count decision for the owner, not an inference for a migration.

  "Operational" = a store with at least one staff user or at least one order,
  i.e. evidence it is actually being run. A Store row created ahead of an opening
  (or by a fixture) has neither, and cannot be where four kilos of pistachio have
  been sitting.

Nothing is duplicated across stores. A new branch created after this migration
starts with NO stock and must receive it explicitly (purchase receipt, manual
adjustment, or seed) — inheriting another branch's stock would fabricate
inventory that does not physically exist. Inventory transfer between stores is a
deliberate non-goal of this branch.

Data safety
-----------
  - Every order, order item and payment row is untouched.
  - Every existing stock quantity is preserved exactly; only a store label is
    added.
  - Every existing movement row is preserved; the ledger is not rewritten.
  - The append-only trigger is dropped only for the duration of the backfill
    UPDATE (it refuses UPDATE, including this one) and reinstalled immediately
    afterwards, before any application traffic can reach the table.

downgrade() removes only this branch's schema. It refuses to run when more than
one store holds stock, because collapsing two branches' shelves into one global
row is not a schema change — it is destroying the record of which branch owned
what, and there is no correct answer to reconstruct afterwards.
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "e2c9a4b16d38"
down_revision = "c3b7e01f9a24"
branch_labels = None
depends_on = None


_IMMUTABLE_TRIGGER = "trg_ingredient_stock_movements_immutable"


class AmbiguousInventoryStore(Exception):
    """
    Raised when historical global stock cannot be attributed to a store without
    guessing. Aborts the migration; nothing is committed.
    """


def _resolve_backfill_store(conn) -> int | None:
    """
    The store that ambiguous (non-order-derived) inventory belongs to, or None
    when there is no ambiguous inventory to place.

    Fails closed rather than guessing. See the module docstring.
    """
    has_ambiguous = conn.execute(
        sa.text(
            """
            SELECT EXISTS (SELECT 1 FROM ingredient_stock)
                OR EXISTS (SELECT 1 FROM ingredient_stock_movements
                           WHERE order_id IS NULL)
            """
        )
    ).scalar()

    if not has_ambiguous:
        # A fresh install, or one whose every movement is order-derived. Nothing
        # needs an assumption; the derivable backfill below does all the work.
        return None

    operational = [
        row[0]
        for row in conn.execute(
            sa.text(
                """
                SELECT s.id
                FROM stores s
                WHERE EXISTS (SELECT 1 FROM users  u WHERE u.store_id = s.id)
                   OR EXISTS (SELECT 1 FROM orders o WHERE o.store_id = s.id)
                ORDER BY s.id
                """
            )
        )
    ]

    if len(operational) == 1:
        return operational[0]

    if not operational:
        # Stock exists but nobody has ever worked or ordered anywhere. If the
        # installation has exactly one store, that is unambiguously where the
        # stock is; a seeded shop that has not hired staff yet is the normal way
        # to reach this state.
        all_stores = [row[0] for row in conn.execute(sa.text("SELECT id FROM stores ORDER BY id"))]
        if len(all_stores) == 1:
            return all_stores[0]
        raise AmbiguousInventoryStore(
            "Cannot store-scope inventory: global stock rows exist, but the "
            f"installation has {len(all_stores)} store(s) and none of them is "
            "operational (no staff, no orders). Refusing to guess which store "
            "the existing stock physically sits in. Create exactly one "
            "operational store, or split the stock manually, then re-run."
        )

    raise AmbiguousInventoryStore(
        "Cannot store-scope inventory: global stock rows exist and there are "
        f"{len(operational)} operational stores (ids {operational}). Existing "
        "global stock names no store, so assigning it would be a guess — and "
        "duplicating it into every store would fabricate inventory that does "
        "not physically exist.\n\n"
        "Resolve manually before migrating:\n"
        "  1. Perform a physical count per store.\n"
        "  2. Decide the per-store split of every ingredient_stock row.\n"
        "  3. Pre-create the per-store rows, or reduce the installation to one "
        "operational store.\n"
        "See docs/STORE_SCOPED_INVENTORY.md § Migration."
    )


def upgrade() -> None:
    conn = op.get_bind()
    backfill_store = _resolve_backfill_store(conn)

    # ═══════════════════════════════════════════════════════════════════════
    # 1. Add the store column (nullable for now — backfilled below)
    # ═══════════════════════════════════════════════════════════════════════
    for table in ("ingredient_stock", "ingredient_stock_movements", "order_inventory_lines"):
        op.add_column(table, sa.Column("store_id", sa.Integer(), nullable=True))

    # ═══════════════════════════════════════════════════════════════════════
    # 2. Backfill — derivable rows first, ambiguous rows only under the
    #    single-operational-store assumption resolved above.
    # ═══════════════════════════════════════════════════════════════════════

    # 2a. DERIVABLE. An order knows its store, so its inventory lines do too.
    #     This is exact and needs no assumption — it would be correct even in a
    #     hundred-store installation.
    op.execute(
        """
        UPDATE order_inventory_lines l
        SET store_id = o.store_id
        FROM orders o
        WHERE o.id = l.order_id
        """
    )

    # 2b. DERIVABLE. Same for every order-linked ledger row.
    #     The append-only trigger refuses UPDATE — including this one — so it is
    #     dropped for the backfill and reinstalled in step 6, before any
    #     application traffic can reach the table.
    op.execute(f"DROP TRIGGER IF EXISTS {_IMMUTABLE_TRIGGER} ON ingredient_stock_movements")
    op.execute(
        """
        UPDATE ingredient_stock_movements m
        SET store_id = o.store_id
        FROM orders o
        WHERE o.id = m.order_id
        """
    )

    if backfill_store is not None:
        # 2c. AMBIGUOUS. Global stock rows, and every movement with no order to
        #     derive from (opening balances, manual adjustments, waste, purchase
        #     receipts). _resolve_backfill_store has already proved there is
        #     exactly one operational store, so "all of it is that store's" is
        #     not a guess — it is the only physical possibility.
        op.execute(
            sa.text("UPDATE ingredient_stock SET store_id = :sid WHERE store_id IS NULL")
            .bindparams(sid=backfill_store)
        )
        op.execute(
            sa.text(
                "UPDATE ingredient_stock_movements SET store_id = :sid WHERE store_id IS NULL"
            ).bindparams(sid=backfill_store)
        )

    # 2d. A movement or line may reference an ingredient this store has no
    #     summary row for — a latent inconsistency inherited from before the
    #     lifecycle migration, where a stock row could be deleted while its
    #     ledger history remained. The composite FKs below point AT the summary
    #     row, so it must exist. Materialise it at zero: that neither creates nor
    #     destroys stock, and reconciliation will now correctly report the
    #     pre-existing drift instead of hiding it behind a missing row.
    op.execute(
        """
        INSERT INTO ingredient_stock (store_id, ingredient_id, on_hand_quantity,
                                      reserved_quantity, unit)
        SELECT DISTINCT need.store_id, need.ingredient_id, 0, 0, i.unit
        FROM (
            SELECT store_id, ingredient_id FROM ingredient_stock_movements
            UNION
            SELECT store_id, ingredient_id FROM order_inventory_lines
        ) AS need
        JOIN ingredients i ON i.id = need.ingredient_id
        WHERE need.store_id IS NOT NULL
          AND NOT EXISTS (
              SELECT 1 FROM ingredient_stock s
              WHERE s.store_id = need.store_id
                AND s.ingredient_id = need.ingredient_id
          )
        """
    )

    for table in ("ingredient_stock", "ingredient_stock_movements", "order_inventory_lines"):
        op.alter_column(table, "store_id", nullable=False)

    # ═══════════════════════════════════════════════════════════════════════
    # 3. Re-grain ingredient_stock: one row per STORE and ingredient
    # ═══════════════════════════════════════════════════════════════════════
    # The old UNIQUE(ingredient_id) was created from the column's unique=True, so
    # its name is PostgreSQL's default. Look it up rather than hard-coding it.
    op.execute(
        """
        DO $$
        DECLARE cname text;
        BEGIN
            SELECT con.conname INTO cname
            FROM pg_constraint con
            JOIN pg_class rel ON rel.oid = con.conrelid
            JOIN pg_attribute att
              ON att.attrelid = rel.oid AND att.attnum = con.conkey[1]
            WHERE rel.relname = 'ingredient_stock'
              AND con.contype = 'u'
              AND array_length(con.conkey, 1) = 1
              AND att.attname = 'ingredient_id';
            IF cname IS NOT NULL THEN
                EXECUTE format('ALTER TABLE ingredient_stock DROP CONSTRAINT %I', cname);
            END IF;
        END $$;
        """
    )
    op.create_unique_constraint(
        "uq_stock_store_ingredient", "ingredient_stock", ["store_id", "ingredient_id"]
    )
    op.create_foreign_key(
        "fk_stock_store", "ingredient_stock", "stores", ["store_id"], ["id"]
    )
    op.create_index("ix_ingredient_stock_store_id", "ingredient_stock", ["store_id"])
    op.create_index("ix_ingredient_stock_ingredient_id", "ingredient_stock", ["ingredient_id"])

    # ═══════════════════════════════════════════════════════════════════════
    # 4. FK targets on orders / users for the composite keys
    # ═══════════════════════════════════════════════════════════════════════
    op.create_unique_constraint("uq_orders_store_id", "orders", ["store_id", "id"])
    op.create_unique_constraint("uq_users_store_id", "users", ["store_id", "id"])

    # ═══════════════════════════════════════════════════════════════════════
    # 5. order_inventory_lines — cross-store integrity
    # ═══════════════════════════════════════════════════════════════════════
    op.create_foreign_key("fk_oil_store", "order_inventory_lines", "stores", ["store_id"], ["id"])
    # A line's store must BE its order's store.
    op.create_foreign_key(
        "fk_oil_order_store", "order_inventory_lines", "orders",
        ["store_id", "order_id"], ["store_id", "id"],
    )
    # ...and it can only allocate against a stock row that exists in that store.
    op.create_foreign_key(
        "fk_oil_stock_store", "order_inventory_lines", "ingredient_stock",
        ["store_id", "ingredient_id"], ["store_id", "ingredient_id"],
    )
    op.create_unique_constraint(
        "uq_oil_id_store", "order_inventory_lines", ["id", "store_id"]
    )
    op.create_index(
        "ix_oil_store_order_ingredient",
        "order_inventory_lines",
        ["store_id", "order_id", "ingredient_id"],
    )

    # ═══════════════════════════════════════════════════════════════════════
    # 6. ingredient_stock_movements — cross-store integrity + store idempotency
    # ═══════════════════════════════════════════════════════════════════════
    op.create_foreign_key(
        "fk_movement_store", "ingredient_stock_movements", "stores", ["store_id"], ["id"]
    )
    op.create_foreign_key(
        "fk_movement_stock_store", "ingredient_stock_movements", "ingredient_stock",
        ["store_id", "ingredient_id"], ["store_id", "ingredient_id"],
    )
    op.create_foreign_key(
        "fk_movement_order_store", "ingredient_stock_movements", "orders",
        ["store_id", "order_id"], ["store_id", "id"],
    )
    op.create_foreign_key(
        "fk_movement_line_store", "ingredient_stock_movements", "order_inventory_lines",
        ["store_id", "order_inventory_line_id"], ["store_id", "id"],
    )
    # Staff may only move stock in their own store. MATCH SIMPLE: a NULL actor
    # (every order-driven movement) is exempt; a present actor must belong to
    # this movement's store.
    op.create_foreign_key(
        "fk_movement_actor_store", "ingredient_stock_movements", "users",
        ["store_id", "actor_user_id"], ["store_id", "id"],
    )

    # Idempotency becomes store-scoped. Two branches sending the same key are two
    # different commands, not a replay of one.
    op.execute("DROP INDEX IF EXISTS uq_movement_idem")
    op.execute(
        """
        CREATE UNIQUE INDEX uq_movement_store_idem
        ON ingredient_stock_movements (store_id, idempotency_key_hash)
        WHERE idempotency_key_hash IS NOT NULL
        """
    )
    op.create_index(
        "ix_ingredient_stock_movements_store_id", "ingredient_stock_movements", ["store_id"]
    )
    op.create_index(
        "ix_movement_store_ingredient_created",
        "ingredient_stock_movements",
        ["store_id", "ingredient_id", "created_at"],
    )

    # Reinstate append-only enforcement. The function itself was never dropped —
    # only the trigger, and only for the backfill UPDATE above.
    op.execute(
        f"""
        CREATE TRIGGER {_IMMUTABLE_TRIGGER}
        BEFORE UPDATE OR DELETE ON ingredient_stock_movements
        FOR EACH ROW EXECUTE FUNCTION public.sweetops_block_inventory_mutation();
        """
    )


def downgrade() -> None:
    conn = op.get_bind()

    # Collapsing store-scoped stock back to one global row per ingredient is only
    # meaningful if exactly one store holds stock. With two, the "global"
    # quantity would have to be a sum, a pick, or a deletion — and every one of
    # those destroys the record of which branch physically owned what, with no
    # way to reconstruct it. Refuse; a lossy downgrade is worse than no downgrade.
    stores_with_stock = conn.execute(
        sa.text("SELECT COUNT(DISTINCT store_id) FROM ingredient_stock")
    ).scalar() or 0
    if stores_with_stock > 1:
        raise AmbiguousInventoryStore(
            f"Cannot downgrade store-scoped inventory: {stores_with_stock} stores "
            "hold stock. The pre-migration schema has one global row per "
            "ingredient and no way to express that, so this downgrade would have "
            "to merge or discard real per-store quantities. Export or reconcile "
            "the per-store stock first, then reduce the data to a single store."
        )

    op.execute(f"DROP TRIGGER IF EXISTS {_IMMUTABLE_TRIGGER} ON ingredient_stock_movements")

    # ── movements ──────────────────────────────────────────────────────────
    op.execute("DROP INDEX IF EXISTS ix_movement_store_ingredient_created")
    op.execute("DROP INDEX IF EXISTS ix_ingredient_stock_movements_store_id")
    op.execute("DROP INDEX IF EXISTS uq_movement_store_idem")
    op.execute(
        """
        CREATE UNIQUE INDEX uq_movement_idem
        ON ingredient_stock_movements (idempotency_key_hash)
        WHERE idempotency_key_hash IS NOT NULL
        """
    )
    for _fk in (
        "fk_movement_actor_store",
        "fk_movement_line_store",
        "fk_movement_order_store",
        "fk_movement_stock_store",
        "fk_movement_store",
    ):
        op.drop_constraint(_fk, "ingredient_stock_movements", type_="foreignkey")
    op.drop_column("ingredient_stock_movements", "store_id")

    # ── order_inventory_lines ──────────────────────────────────────────────
    op.execute("DROP INDEX IF EXISTS ix_oil_store_order_ingredient")
    op.drop_constraint("uq_oil_id_store", "order_inventory_lines", type_="unique")
    for _fk in ("fk_oil_stock_store", "fk_oil_order_store", "fk_oil_store"):
        op.drop_constraint(_fk, "order_inventory_lines", type_="foreignkey")
    op.drop_column("order_inventory_lines", "store_id")

    # ── FK targets ─────────────────────────────────────────────────────────
    op.drop_constraint("uq_users_store_id", "users", type_="unique")
    op.drop_constraint("uq_orders_store_id", "orders", type_="unique")

    # ── ingredient_stock: back to one global row per ingredient ────────────
    op.execute("DROP INDEX IF EXISTS ix_ingredient_stock_ingredient_id")
    op.execute("DROP INDEX IF EXISTS ix_ingredient_stock_store_id")
    op.drop_constraint("fk_stock_store", "ingredient_stock", type_="foreignkey")
    op.drop_constraint("uq_stock_store_ingredient", "ingredient_stock", type_="unique")
    op.drop_column("ingredient_stock", "store_id")
    op.create_unique_constraint(
        "ingredient_stock_ingredient_id_key", "ingredient_stock", ["ingredient_id"]
    )

    op.execute(
        f"""
        CREATE TRIGGER {_IMMUTABLE_TRIGGER}
        BEFORE UPDATE OR DELETE ON ingredient_stock_movements
        FOR EACH ROW EXECUTE FUNCTION public.sweetops_block_inventory_mutation();
        """
    )
