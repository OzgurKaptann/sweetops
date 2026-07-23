"""Store-scoped customer menu: products.is_active + store_products offerings

Revision ID: a9e4c7b25d13
Revises: e7f2a9c04d18
Create Date: 2026-07-23 10:00:00.000000

Why this migration exists
-------------------------
The customer-facing menu read the WHOLE ``products`` table with no filter
(``menu_service.get_menu`` → ``db.query(Product).all()``). ``products`` had
neither an activation flag nor any relationship to a store, so there was
nothing to filter on: every row that ever landed in that table — including the
``TestWaffle_<hex>`` rows an interrupted test run leaves behind — was one
rendered list away from a guest's phone (RUNTIME_PRODUCT_GAP_REVIEW F-02/F-23).

This is the schema decision REAL_USE_READINESS_ROADMAP P0-D says must be taken
explicitly. Two orthogonal boundaries, mirroring the catalog/physical split that
``ingredients`` + ``ingredient_stock`` already use:

  products.is_active   CATALOG state. Chain-wide. A retired item is switched off
                       once, everywhere.

  store_products       PUBLICATION. One row = "this branch offers this product".
                       A product with no row is not on anybody's menu; it is
                       merely a row in a table. ``is_available`` lets a branch
                       switch an offering off for today without forgetting that
                       it sells it.

Deliberately NOT backfilled
---------------------------
The obvious backfill — one row per (store × product) — would re-publish, on the
very first upgrade, exactly the debris this boundary exists to contain. And it
would be a lie: no explicit publication decision was ever taken for any existing
row, so none can be inferred. A customer-facing catalog must fail closed, so it
starts closed: after this upgrade every store's menu is EMPTY until someone
offers something. ``scripts/seed_demo_data.py`` does that for the demo stores;
the authenticated surface that will do it for a real shop is P0-E
(store onboarding) and is not in this migration's branch.

Deliberately NOT included: per-store price overrides. A branch publishes the
chain's product at the chain's price. Per-branch pricing is P1-B, named and
deferred — see docs/CUSTOMER_MENU_SCOPING.md.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "a9e4c7b25d13"
down_revision: Union[str, None] = "e7f2a9c04d18"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── Catalog activation ───────────────────────────────────────────────────
    # Existing rows become active: is_active is about RETIREMENT, not about
    # publication. Nothing becomes customer-visible from this column alone —
    # visibility needs a store_products row, and there are none yet.
    op.add_column(
        "products",
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="true"),
    )
    op.create_index("ix_products_is_active", "products", ["is_active"])

    # ── Publication: which branch offers which product ───────────────────────
    op.create_table(
        "store_products",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("store_id", sa.Integer(), nullable=False),
        sa.Column("product_id", sa.Integer(), nullable=False),
        # A branch that is out of Türk Kahvesi today switches the offering off
        # rather than deleting it — the publication decision survives the day.
        sa.Column(
            "is_available", sa.Boolean(), nullable=False, server_default="true"
        ),
        sa.Column(
            "sort_order", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["store_id"], ["stores.id"]),
        sa.ForeignKeyConstraint(["product_id"], ["products.id"]),
        # One publication decision per (branch, product). Makes "offered twice,
        # once available and once not" unrepresentable rather than merely
        # unwritten.
        sa.UniqueConstraint(
            "store_id", "product_id", name="uq_store_products_store_product"
        ),
    )
    op.create_index("ix_store_products_store_id", "store_products", ["store_id"])
    op.create_index(
        "ix_store_products_product_id", "store_products", ["product_id"]
    )


def downgrade() -> None:
    # Downgrading returns the customer menu to a global, unfiltered catalog.
    # The publication decisions are dropped with the table — they have nowhere
    # to go in the older schema.
    op.drop_index("ix_store_products_product_id", table_name="store_products")
    op.drop_index("ix_store_products_store_id", table_name="store_products")
    op.drop_table("store_products")
    op.drop_index("ix_products_is_active", table_name="products")
    op.drop_column("products", "is_active")
