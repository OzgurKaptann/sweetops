"""staff auth + rbac: user security fields, auth_sessions, owner_decision store scope

Revision ID: a7d3f9b21c05
Revises: f7a1c2b9d8e3
Create Date: 2026-07-09

Adds:
  - user security/lifecycle columns (is_active, failed_login_count, locked_until,
    last_login_at, password_changed_at, updated_at),
  - a case-insensitive unique index on lower(username),
  - the auth_sessions table (opaque server-side staff sessions),
  - store scoping for owner_decisions (composite PK store_id + decision_id).

Safety:
  - Existing users/orders/decisions are preserved. No credentials are created.
  - The lower(username) index is preceded by a collision preflight.
  - owner_decisions backfill fails closed if it cannot be unambiguously attributed
    to a single store.
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "a7d3f9b21c05"
down_revision = "f7a1c2b9d8e3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()

    # ── 1. User security / lifecycle columns ────────────────────────────────
    op.add_column("users", sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False))
    op.add_column("users", sa.Column("failed_login_count", sa.Integer(), server_default=sa.text("0"), nullable=False))
    op.add_column("users", sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True))
    op.add_column("users", sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("users", sa.Column("password_changed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("users", sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True))

    # ── 2. Case-insensitive username uniqueness (preflight then index) ──────
    dupes = bind.execute(sa.text(
        "SELECT lower(username) AS u, count(*) FROM users "
        "GROUP BY lower(username) HAVING count(*) > 1"
    )).fetchall()
    if dupes:
        raise RuntimeError(
            "Cannot add case-insensitive username uniqueness: existing usernames "
            f"collide when lower-cased: {[d[0] for d in dupes]}. Resolve these "
            "duplicates manually before migrating (no user is deleted automatically)."
        )
    op.execute("CREATE UNIQUE INDEX uq_users_lower_username ON users (lower(username))")

    # ── 3. auth_sessions ─────────────────────────────────────────────────────
    op.create_table(
        "auth_sessions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("csrf_token_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_reason", sa.String(length=64), nullable=True),
        sa.Column("user_agent_hash", sa.String(length=64), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_auth_sessions_user_id", "auth_sessions", ["user_id"])
    op.create_index("ix_auth_sessions_token_hash", "auth_sessions", ["token_hash"], unique=True)
    op.create_index("ix_auth_sessions_expires_at", "auth_sessions", ["expires_at"])

    # ── 4. owner_decisions store scoping ────────────────────────────────────
    op.add_column("owner_decisions", sa.Column("store_id", sa.Integer(), nullable=True))

    dec_count = bind.execute(sa.text("SELECT count(*) FROM owner_decisions")).scalar() or 0
    if dec_count > 0:
        store_ids = [r[0] for r in bind.execute(sa.text("SELECT id FROM stores ORDER BY id"))]
        if len(store_ids) == 1:
            bind.execute(
                sa.text("UPDATE owner_decisions SET store_id = :sid WHERE store_id IS NULL"),
                {"sid": store_ids[0]},
            )
        else:
            raise RuntimeError(
                f"Cannot backfill owner_decisions.store_id: {dec_count} existing "
                f"decision(s) but {len(store_ids)} store(s) exist. Decision-to-store "
                "attribution is ambiguous; resolve manually before migrating."
            )

    op.alter_column("owner_decisions", "store_id", existing_type=sa.Integer(), nullable=False)
    op.drop_constraint("owner_decisions_pkey", "owner_decisions", type_="primary")
    op.create_primary_key("owner_decisions_pkey", "owner_decisions", ["store_id", "decision_id"])
    op.create_foreign_key(
        "fk_owner_decisions_store", "owner_decisions", "stores", ["store_id"], ["id"]
    )
    op.create_index("ix_owner_decisions_store_id", "owner_decisions", ["store_id"])


def downgrade() -> None:
    # ── owner_decisions: revert to single-column PK ─────────────────────────
    op.drop_index("ix_owner_decisions_store_id", table_name="owner_decisions")
    op.drop_constraint("fk_owner_decisions_store", "owner_decisions", type_="foreignkey")
    op.drop_constraint("owner_decisions_pkey", "owner_decisions", type_="primary")
    op.drop_column("owner_decisions", "store_id")
    op.create_primary_key("owner_decisions_pkey", "owner_decisions", ["decision_id"])

    # ── auth_sessions ────────────────────────────────────────────────────────
    op.drop_index("ix_auth_sessions_expires_at", table_name="auth_sessions")
    op.drop_index("ix_auth_sessions_token_hash", table_name="auth_sessions")
    op.drop_index("ix_auth_sessions_user_id", table_name="auth_sessions")
    op.drop_table("auth_sessions")

    # ── users ────────────────────────────────────────────────────────────────
    op.execute("DROP INDEX IF EXISTS uq_users_lower_username")
    op.drop_column("users", "updated_at")
    op.drop_column("users", "password_changed_at")
    op.drop_column("users", "last_login_at")
    op.drop_column("users", "locked_until")
    op.drop_column("users", "failed_login_count")
    op.drop_column("users", "is_active")
