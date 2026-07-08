"""Add is_promoted flag to ingredients for owner-driven menu ranking

Revision ID: e5f3a2d9c847
Revises: d4e2f1a8b753
Create Date: 2026-04-01 13:00:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "e5f3a2d9c847"
down_revision: Union[str, None] = "d4e2f1a8b753"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "ingredients",
        sa.Column(
            "is_promoted",
            sa.Boolean(),
            nullable=False,
            server_default="false",
        ),
    )
    op.create_index("ix_ingredients_is_promoted", "ingredients", ["is_promoted"])


def downgrade() -> None:
    op.drop_index("ix_ingredients_is_promoted", table_name="ingredients")
    op.drop_column("ingredients", "is_promoted")
