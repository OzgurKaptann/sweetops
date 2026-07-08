"""Add table_qr_tokens for secure, revocable QR store/table context

Creates the token table that backs opaque, server-resolved QR context. Raw
tokens are never persisted — only their SHA-256 hash. This migration is purely
additive: it creates one new table plus its indexes and constraints, and does
not touch existing data. No raw tokens are generated here; issuance happens
through scripts/manage_qr_tokens.py (raw tokens cannot be recovered after
hashing, so they must be minted by a controlled application script).

Revision ID: f7a1c2b9d8e3
Revises: 4299b615f7aa
Create Date: 2026-07-08 16:00:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "f7a1c2b9d8e3"
down_revision: Union[str, None] = "4299b615f7aa"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "table_qr_tokens",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("table_id", sa.Integer(), nullable=False),
        # SHA-256 hex digest of the raw token (64 chars).
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        # Non-secret leading fragment for operational support.
        sa.Column("token_prefix", sa.String(length=16), nullable=False),
        sa.Column(
            "status",
            sa.String(),
            nullable=False,
            server_default="ACTIVE",
        ),
        # DB-enforced status domain: application validation is not trusted alone.
        sa.CheckConstraint(
            "status IN ('ACTIVE', 'REVOKED')",
            name="ck_table_qr_tokens_status",
        ),
        sa.Column("created_reason", sa.String(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("replaced_by_id", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(
            ["table_id"], ["tables.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["replaced_by_id"], ["table_qr_tokens.id"], ondelete="SET NULL"
        ),
    )
    op.create_index(
        op.f("ix_table_qr_tokens_id"), "table_qr_tokens", ["id"], unique=False
    )
    op.create_index(
        op.f("ix_table_qr_tokens_table_id"),
        "table_qr_tokens",
        ["table_id"],
        unique=False,
    )
    # Unique + indexed: resolution is a single indexed lookup; duplicate
    # hashes are impossible at the DB level.
    op.create_index(
        op.f("ix_table_qr_tokens_token_hash"),
        "table_qr_tokens",
        ["token_hash"],
        unique=True,
    )
    op.create_index(
        op.f("ix_table_qr_tokens_token_prefix"),
        "table_qr_tokens",
        ["token_prefix"],
        unique=False,
    )
    op.create_index(
        op.f("ix_table_qr_tokens_status"),
        "table_qr_tokens",
        ["status"],
        unique=False,
    )
    # At most one ACTIVE token per table. Partial unique index: only ACTIVE rows
    # are constrained, so unlimited REVOKED history rows remain allowed. This is
    # the hard database guarantee behind the "one current sticker per table"
    # invariant — a concurrent issue/rotate race cannot leave two active tokens.
    op.create_index(
        "uq_table_qr_tokens_one_active_per_table",
        "table_qr_tokens",
        ["table_id"],
        unique=True,
        postgresql_where=sa.text("status = 'ACTIVE'"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_table_qr_tokens_one_active_per_table", table_name="table_qr_tokens"
    )
    op.drop_index(op.f("ix_table_qr_tokens_status"), table_name="table_qr_tokens")
    op.drop_index(
        op.f("ix_table_qr_tokens_token_prefix"), table_name="table_qr_tokens"
    )
    op.drop_index(
        op.f("ix_table_qr_tokens_token_hash"), table_name="table_qr_tokens"
    )
    op.drop_index(
        op.f("ix_table_qr_tokens_table_id"), table_name="table_qr_tokens"
    )
    op.drop_index(op.f("ix_table_qr_tokens_id"), table_name="table_qr_tokens")
    op.drop_table("table_qr_tokens")
