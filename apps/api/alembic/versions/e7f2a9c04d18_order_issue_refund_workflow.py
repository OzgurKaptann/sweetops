"""order issue refund workflow: controlled order issues, cancellations & refund links

Revision ID: e7f2a9c04d18
Revises: d5c7b3a91e40
Create Date: 2026-07-15

An order issue is a first-class, auditable record of something going wrong with an
order and the controlled decision taken to resolve it. It COORDINATES the systems
that already exist — it never bypasses the payment refund ledger, the inventory
lifecycle, the cashier shift snapshot or the audit log.

Schema
------
  payment_refunds                                                    (ALTERED)
      + order_issue_id            nullable FK → order_issues(id). The link back
                                  from a refund to the operational decision that
                                  caused it. NULL for an ordinary till refund.
      + uq_refund_store_order_id  UNIQUE (store_id, order_id, id). Lets an issue
                                  carry a composite FK proving a linked refund is
                                  the same store AND the same order.

  order_issues                                                           (NEW)
      One row per problem raised against one order.

      ck_order_issue_type_domain        issue_type ∈ the seven documented types.
      ck_order_issue_status_domain      status ∈ OPEN | RESOLVED | VOIDED.
      ck_order_issue_resolution_domain  resolution_type NULL or ∈ the four types.
      ck_order_issue_*_nonneg           requested / approved refund amounts >= 0.
      ck_order_issue_status_snapshot    THE consistency rule: every resolution-
                                        snapshot column is NULL exactly when OPEN and
                                        NOT NULL exactly when RESOLVED/VOIDED. A
                                        half-resolved row is unrepresentable, and this
                                        single CHECK carries "resolved_at /
                                        resolved_by required when resolved".
      ck_order_issue_refund_required    a FULL/PARTIAL refund with a positive approved
                                        amount MUST carry refund_id.
      ck_order_issue_refund_only_when_refunding
                                        a non-refunding resolution must NOT carry one.
      fk_order_issue_order_store        (store_id, order_id) → orders(store_id, id):
                                        the issue belongs to its order's store.
      fk_order_issue_creator_store      (store_id, created_by_user_id) →
                                        users(store_id, id): the creator belongs to it.
      fk_order_issue_resolver_store     (store_id, resolved_by_user_id) → same; NULL
                                        skips the check while OPEN.
      fk_order_issue_refund_context     (store_id, order_id, refund_id) →
                                        payment_refunds(store_id, order_id, id): a
                                        linked refund is the same store AND order.
      uq_order_issue_store_create_idem  (store_id, created_idempotency_key_hash):
                                        store-scoped creation idempotency.

Immutability trigger
--------------------
A resolved issue is history and is frozen, with no application-reachable bypass:

    DELETE               — always refused (issues are append-only history).
    UPDATE, not OPEN     — always refused (a resolved/voided issue is immutable).
    UPDATE, OPEN         — permitted ONLY as OPEN → RESOLVED/VOIDED, and only if the
                           creation snapshot (store, order, issue_type, requested
                           amount, reason, note, creator, created_at, created hashes)
                           is unchanged. Anything else is refused.

Same hardening as the ledger / shift triggers beside it: SECURITY INVOKER, a pinned
search_path, no dynamic SQL, EXECUTE revoked from PUBLIC. No GUC turns it off.

Data safety
-----------
Additive. The only change to an existing table is a nullable column and a redundant
unique constraint on payment_refunds; no refund, settlement, order, shift or stock
row is read, rewritten or deleted.

downgrade() removes only this branch's schema (the table, its constraints, indexes
and trigger; the payment_refunds column and unique constraint). It REFUSES to run
while any order issue exists, because dropping order_issues would destroy the record
of why money was refunded and orders cancelled while the refunds themselves remain —
lossy and irreversible. Export the issues first.
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "e7f2a9c04d18"
down_revision = "d5c7b3a91e40"
branch_labels = None
depends_on = None


class OrderIssuesExist(Exception):
    """
    Raised when a downgrade would destroy real order-issue history. Aborts the
    migration; nothing is committed.
    """


# Resolution-snapshot columns: NULL while OPEN, NOT NULL once RESOLVED/VOIDED.
_RESOLVED_COLS = (
    "resolution_type",
    "approved_refund_amount",
    "resolved_by_user_id",
    "resolved_at",
    "resolved_idempotency_key_hash",
    "resolved_request_hash",
)
_OPEN_ALL_NULL = " AND ".join(f"{c} IS NULL" for c in _RESOLVED_COLS)
_RESOLVED_ALL_SET = " AND ".join(f"{c} IS NOT NULL" for c in _RESOLVED_COLS)

_ISSUE_TYPES = (
    "CUSTOMER_CANCELLED",
    "WRONG_ITEM",
    "MISSING_ITEM",
    "QUALITY_PROBLEM",
    "DUPLICATE_ORDER",
    "STAFF_ERROR",
    "OTHER",
)
_STATUSES = ("OPEN", "RESOLVED", "VOIDED")
_RESOLUTIONS = ("NO_REFUND", "FULL_REFUND", "PARTIAL_REFUND", "CANCEL_ONLY")

_TYPE_SQL = ",".join(f"'{t}'" for t in _ISSUE_TYPES)
_STATUS_SQL = ",".join(f"'{s}'" for s in _STATUSES)
_RESOLUTION_SQL = ",".join(f"'{r}'" for r in _RESOLUTIONS)

_GUARD_FN = "public.sweetops_guard_order_issue"
_TRG_GUARD = "trg_order_issues_guard"


def upgrade() -> None:
    # 1. The unique constraint the issue's refund-context FK will reference.
    op.create_unique_constraint(
        "uq_refund_store_order_id",
        "payment_refunds",
        ["store_id", "order_id", "id"],
    )

    # 2. order_issues.
    op.create_table(
        "order_issues",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("store_id", sa.Integer(), nullable=False),
        sa.Column("order_id", sa.Integer(), nullable=False),
        sa.Column("issue_type", sa.String(32), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="OPEN"),
        sa.Column("resolution_type", sa.String(16), nullable=True),
        sa.Column("requested_refund_amount", sa.Numeric(12, 2), nullable=True),
        sa.Column("approved_refund_amount", sa.Numeric(12, 2), nullable=True),
        sa.Column("refund_id", sa.BigInteger(), nullable=True),
        sa.Column("reason", sa.String(500), nullable=False),
        sa.Column("note", sa.String(500), nullable=True),
        sa.Column("created_by_user_id", sa.Integer(), nullable=False),
        sa.Column("resolved_by_user_id", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_idempotency_key_hash", sa.String(64), nullable=False),
        sa.Column("created_request_hash", sa.String(64), nullable=False),
        sa.Column("resolved_idempotency_key_hash", sa.String(64), nullable=True),
        sa.Column("resolved_request_hash", sa.String(64), nullable=True),
        # Plain single-column FKs (identity references).
        sa.ForeignKeyConstraint(["store_id"], ["stores.id"], name="fk_order_issue_store"),
        sa.ForeignKeyConstraint(["order_id"], ["orders.id"], name="fk_order_issue_order"),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"], ["users.id"], name="fk_order_issue_creator"
        ),
        sa.ForeignKeyConstraint(
            ["resolved_by_user_id"], ["users.id"], name="fk_order_issue_resolver"
        ),
        sa.ForeignKeyConstraint(
            ["refund_id"], ["payment_refunds.id"], name="fk_order_issue_refund"
        ),
        # Composite scoping FKs (isolation boundaries).
        sa.ForeignKeyConstraint(
            ["store_id", "order_id"],
            ["orders.store_id", "orders.id"],
            name="fk_order_issue_order_store",
        ),
        sa.ForeignKeyConstraint(
            ["store_id", "created_by_user_id"],
            ["users.store_id", "users.id"],
            name="fk_order_issue_creator_store",
        ),
        sa.ForeignKeyConstraint(
            ["store_id", "resolved_by_user_id"],
            ["users.store_id", "users.id"],
            name="fk_order_issue_resolver_store",
        ),
        sa.ForeignKeyConstraint(
            ["store_id", "order_id", "refund_id"],
            ["payment_refunds.store_id", "payment_refunds.order_id", "payment_refunds.id"],
            name="fk_order_issue_refund_context",
        ),
        sa.CheckConstraint(f"issue_type IN ({_TYPE_SQL})", name="ck_order_issue_type_domain"),
        sa.CheckConstraint(f"status IN ({_STATUS_SQL})", name="ck_order_issue_status_domain"),
        sa.CheckConstraint(
            f"resolution_type IS NULL OR resolution_type IN ({_RESOLUTION_SQL})",
            name="ck_order_issue_resolution_domain",
        ),
        sa.CheckConstraint(
            "requested_refund_amount IS NULL OR requested_refund_amount >= 0",
            name="ck_order_issue_requested_nonneg",
        ),
        sa.CheckConstraint(
            "approved_refund_amount IS NULL OR approved_refund_amount >= 0",
            name="ck_order_issue_approved_nonneg",
        ),
        sa.CheckConstraint(
            f"(status = 'OPEN' AND {_OPEN_ALL_NULL}) "
            f"OR (status IN ('RESOLVED','VOIDED') AND {_RESOLVED_ALL_SET})",
            name="ck_order_issue_status_snapshot",
        ),
        sa.CheckConstraint(
            "resolution_type NOT IN ('FULL_REFUND','PARTIAL_REFUND') "
            "OR approved_refund_amount IS NULL OR approved_refund_amount = 0 "
            "OR refund_id IS NOT NULL",
            name="ck_order_issue_refund_required",
        ),
        sa.CheckConstraint(
            "refund_id IS NULL OR resolution_type IN ('FULL_REFUND','PARTIAL_REFUND')",
            name="ck_order_issue_refund_only_when_refunding",
        ),
        sa.UniqueConstraint(
            "store_id",
            "created_idempotency_key_hash",
            name="uq_order_issue_store_create_idem",
        ),
    )
    op.create_index("ix_order_issues_store_id", "order_issues", ["store_id"])
    op.create_index("ix_order_issues_order_id", "order_issues", ["order_id"])
    op.create_index(
        "ix_order_issues_created_by_user_id", "order_issues", ["created_by_user_id"]
    )
    op.create_index(
        "ix_order_issue_store_status", "order_issues", ["store_id", "status"]
    )
    op.create_index(
        "ix_order_issue_store_created", "order_issues", ["store_id", "created_at"]
    )

    # 3. The reverse link on the refund ledger (order_issues now exists).
    op.add_column(
        "payment_refunds",
        sa.Column("order_issue_id", sa.BigInteger(), nullable=True),
    )
    op.create_foreign_key(
        "fk_payment_refund_order_issue",
        "payment_refunds",
        "order_issues",
        ["order_issue_id"],
        ["id"],
    )
    op.create_index(
        "ix_payment_refunds_order_issue_id", "payment_refunds", ["order_issue_id"]
    )

    # 4. Immutability / lifecycle guard.
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION {_GUARD_FN}()
        RETURNS trigger LANGUAGE plpgsql
        SECURITY INVOKER
        SET search_path = pg_catalog
        AS $fn$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION
                    'order_issues is append-only: an issue is history and is '
                    'never deleted'
                    USING ERRCODE = 'restrict_violation';
            END IF;

            -- UPDATE from here on.
            IF OLD.status <> 'OPEN' THEN
                RAISE EXCEPTION
                    'order issue % is resolved: it is immutable and cannot be '
                    'changed or reopened', OLD.id
                    USING ERRCODE = 'restrict_violation';
            END IF;

            -- OLD.status = 'OPEN'. The only permitted change is a resolution, and
            -- the creation snapshot must be carried over unchanged.
            IF NEW.status NOT IN ('RESOLVED', 'VOIDED') THEN
                RAISE EXCEPTION
                    'order issue % may only transition from OPEN to RESOLVED/VOIDED',
                    OLD.id
                    USING ERRCODE = 'restrict_violation';
            END IF;

            IF NEW.store_id <> OLD.store_id
               OR NEW.order_id <> OLD.order_id
               OR NEW.issue_type <> OLD.issue_type
               OR NEW.requested_refund_amount IS DISTINCT FROM OLD.requested_refund_amount
               OR NEW.reason <> OLD.reason
               OR NEW.note IS DISTINCT FROM OLD.note
               OR NEW.created_by_user_id <> OLD.created_by_user_id
               OR NEW.created_at <> OLD.created_at
               OR NEW.created_idempotency_key_hash <> OLD.created_idempotency_key_hash
               OR NEW.created_request_hash <> OLD.created_request_hash
            THEN
                RAISE EXCEPTION
                    'order issue % creation snapshot is immutable', OLD.id
                    USING ERRCODE = 'restrict_violation';
            END IF;

            RETURN NEW;
        END;
        $fn$;
        """
    )
    op.execute(f"REVOKE ALL ON FUNCTION {_GUARD_FN}() FROM PUBLIC")
    op.execute(
        f"""
        CREATE TRIGGER {_TRG_GUARD}
        BEFORE UPDATE OR DELETE ON order_issues
        FOR EACH ROW EXECUTE FUNCTION {_GUARD_FN}();
        """
    )


def downgrade() -> None:
    conn = op.get_bind()

    # Dropping order_issues would destroy the record of WHY money was refunded and
    # orders cancelled, while the refunds it caused remain in the ledger. Lossy and
    # irreversible — refuse while any issue exists.
    issues = conn.execute(sa.text("SELECT COUNT(*) FROM order_issues")).scalar() or 0
    if issues:
        raise OrderIssuesExist(
            f"Cannot downgrade order issue refund workflow: {issues} order issue(s) "
            "exist. Dropping the table would destroy the record of why refunds were "
            "issued and orders cancelled while the refunds themselves remain. Export "
            "the issues first."
        )

    op.execute(f"DROP TRIGGER IF EXISTS {_TRG_GUARD} ON order_issues")
    op.execute(f"DROP FUNCTION IF EXISTS {_GUARD_FN}()")

    op.drop_index("ix_payment_refunds_order_issue_id", table_name="payment_refunds")
    op.drop_constraint(
        "fk_payment_refund_order_issue", "payment_refunds", type_="foreignkey"
    )
    op.drop_column("payment_refunds", "order_issue_id")

    op.drop_index("ix_order_issue_store_created", table_name="order_issues")
    op.drop_index("ix_order_issue_store_status", table_name="order_issues")
    op.drop_index("ix_order_issues_created_by_user_id", table_name="order_issues")
    op.drop_index("ix_order_issues_order_id", table_name="order_issues")
    op.drop_index("ix_order_issues_store_id", table_name="order_issues")
    op.drop_table("order_issues")

    op.drop_constraint(
        "uq_refund_store_order_id", "payment_refunds", type_="unique"
    )
