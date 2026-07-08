"""Add owner_decisions table for action lifecycle management

Revision ID: d4e2f1a8b753
Revises: c9f1d3e8a042
Create Date: 2026-04-01 12:00:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "d4e2f1a8b753"
down_revision: Union[str, None] = "c9f1d3e8a042"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "owner_decisions",
        sa.Column("decision_id", sa.String(128), primary_key=True, nullable=False),
        sa.Column("type",        sa.String(40),  nullable=False),
        sa.Column("severity",    sa.String(10),  nullable=False),
        sa.Column("decision_score",           sa.Float,   nullable=False, server_default="0"),
        sa.Column("blocking_vs_non_blocking", sa.Boolean, nullable=False, server_default="false"),
        sa.Column("title",              sa.String(200), nullable=False),
        sa.Column("description",        sa.Text,        nullable=False),
        sa.Column("impact",             sa.Text,        nullable=False),
        sa.Column("recommended_action", sa.Text,        nullable=False),
        sa.Column("why_now",            sa.Text,        nullable=False),
        sa.Column("expected_impact",    sa.Text,        nullable=False),
        sa.Column("data",               postgresql.JSONB, nullable=True),
        sa.Column("status",         sa.String(20), nullable=False, server_default="pending"),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at",    sa.DateTime(timezone=True), nullable=True),
        sa.Column("actor_id",       sa.String(64), nullable=True),
        sa.Column("resolution_note", sa.Text,      nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
    )
    op.create_index("ix_owner_decisions_type",     "owner_decisions", ["type"])
    op.create_index("ix_owner_decisions_status",   "owner_decisions", ["status"])
    op.create_index("ix_owner_decisions_severity", "owner_decisions", ["severity"])


def downgrade() -> None:
    op.drop_index("ix_owner_decisions_severity", table_name="owner_decisions")
    op.drop_index("ix_owner_decisions_status",   table_name="owner_decisions")
    op.drop_index("ix_owner_decisions_type",     table_name="owner_decisions")
    op.drop_table("owner_decisions")
