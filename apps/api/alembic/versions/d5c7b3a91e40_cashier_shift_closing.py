"""cashier shift closing: open/close a till and reconcile it against the ledger

Revision ID: d5c7b3a91e40
Revises: c8a4e7b13f92
Create Date: 2026-07-15

A cashier shift is a RECONCILIATION EVENT laid over the existing append-only
payment ledger. It never mutates a settlement, never mutates a refund, never
touches inventory, and never writes an accounting entry. It records the cash the
drawer started with, a SNAPSHOT (taken at close) of what the ledger says happened
during the shift window, what the cashier physically counted, and the difference.

Schema
------
  cashier_shifts                                                          (NEW)
      One row per till session. Opened with a starting cash figure; closed with a
      counted cash figure plus an immutable snapshot of the windowed ledger totals.

      ck_cashier_shift_status_domain      status = OPEN | CLOSED. No fake states.
      ck_cashier_shift_opening_nonneg     a drawer cannot start with negative cash.
      ck_cashier_shift_status_snapshot    THE consistency rule: every close-snapshot
                                          column is NULL exactly when OPEN and
                                          NOT NULL exactly when CLOSED. A
                                          half-populated close is unrepresentable,
                                          and this single CHECK also carries the
                                          closed_at / counted nullability rules.
      ck_cashier_shift_*_nonneg           counted cash and every PURE-SUM total are
                                          sums of positive ledger amounts, so a
                                          negative one is corruption. expected /
                                          net / discrepancy are deliberately NOT
                                          constrained — they are signed nets.
      ck_cashier_shift_gross_ge_parts     gross >= cash + card payments (gross also
      ck_cashier_shift_refunds_ge_parts   counts OTHER methods); refunds likewise.
      fk_cashier_shift_actor_store        (store_id, cashier_user_id) →
                                          users(store_id, id): the cashier BELONGS
                                          to the store whose till they run.
      uq_cashier_shift_store_open_idem    (store_id, opened_idempotency_key_hash):
                                          store-scoped opening idempotency, exactly
                                          like the payment ledger's.
      uq_cashier_shift_one_open           PARTIAL UNIQUE (store_id, cashier_user_id)
                                          WHERE status = OPEN: at most one open
                                          shift per cashier per store, so two
                                          overlapping windows never double-count.

Immutability trigger
--------------------
A CLOSED shift is a snapshot and must be frozen — otherwise a payment recorded
after the close could retroactively change what the shift reported, and a closed
shift could be reopened. A trigger enforces, with no application-reachable bypass:

    DELETE            — always refused (shifts are append-only history).
    UPDATE, CLOSED    — always refused (immutable; cannot be reopened).
    UPDATE, OPEN      — permitted ONLY as the OPEN → CLOSED transition, and only if
                        the opening snapshot (store, cashier, opened_at,
                        opening_cash_amount, open_note, opened idempotency hashes)
                        is unchanged. Anything else is refused.

Same hardening as the ledger and inventory triggers beside it: SECURITY INVOKER,
a pinned search_path, schema-qualified references, no dynamic SQL, EXECUTE revoked
from PUBLIC. No GUC or session variable turns it off.

Data safety
-----------
Purely additive: no existing table is read, rewritten or deleted. Payments,
refunds, orders, inventory and stock are not touched at all.

downgrade() removes only this branch's schema (the table, its constraints,
indexes and the trigger + function). It REFUSES to run while any shift row exists,
because dropping cashier_shifts would destroy the record of how a till was
reconciled — the counted cash, the discrepancy a manager signed off on — while the
payments and refunds it summarised remain. That is lossy and irreversible; export
the shifts first.
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "d5c7b3a91e40"
down_revision = "c8a4e7b13f92"
branch_labels = None
depends_on = None


class CashierShiftsExist(Exception):
    """
    Raised when a downgrade would destroy real till reconciliations. Aborts the
    migration; nothing is committed.
    """


# Close-snapshot columns: NULL while OPEN, NOT NULL once CLOSED. Spelled out here
# so the migration keeps describing the schema it created even if the model moves on.
_CLOSED_COLS = (
    "closed_at",
    "counted_closing_cash_amount",
    "cash_payments_amount",
    "cash_refunds_amount",
    "expected_closing_cash_amount",
    "cash_discrepancy_amount",
    "card_payments_amount",
    "card_refunds_amount",
    "gross_payments_amount",
    "total_refunds_amount",
    "net_collected_amount",
)
_OPEN_ALL_NULL = " AND ".join(f"{c} IS NULL" for c in _CLOSED_COLS)
_CLOSED_ALL_SET = " AND ".join(f"{c} IS NOT NULL" for c in _CLOSED_COLS)

_GUARD_FN = "public.sweetops_guard_cashier_shift"
_TRG_GUARD = "trg_cashier_shifts_guard"


def upgrade() -> None:
    op.create_table(
        "cashier_shifts",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("store_id", sa.Integer(), nullable=False),
        sa.Column("cashier_user_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="OPEN"),
        sa.Column(
            "opened_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("opening_cash_amount", sa.Numeric(12, 2), nullable=False),
        sa.Column("open_note", sa.String(500), nullable=True),
        sa.Column("close_note", sa.String(500), nullable=True),
        sa.Column("counted_closing_cash_amount", sa.Numeric(12, 2), nullable=True),
        sa.Column("cash_payments_amount", sa.Numeric(12, 2), nullable=True),
        sa.Column("cash_refunds_amount", sa.Numeric(12, 2), nullable=True),
        sa.Column("expected_closing_cash_amount", sa.Numeric(12, 2), nullable=True),
        sa.Column("cash_discrepancy_amount", sa.Numeric(12, 2), nullable=True),
        sa.Column("card_payments_amount", sa.Numeric(12, 2), nullable=True),
        sa.Column("card_refunds_amount", sa.Numeric(12, 2), nullable=True),
        sa.Column("gross_payments_amount", sa.Numeric(12, 2), nullable=True),
        sa.Column("total_refunds_amount", sa.Numeric(12, 2), nullable=True),
        sa.Column("net_collected_amount", sa.Numeric(12, 2), nullable=True),
        sa.Column("opened_idempotency_key_hash", sa.String(64), nullable=False),
        sa.Column("opened_request_hash", sa.String(64), nullable=False),
        sa.Column("closed_idempotency_key_hash", sa.String(64), nullable=True),
        sa.Column("closed_request_hash", sa.String(64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["store_id"], ["stores.id"], name="fk_cashier_shift_store"),
        sa.ForeignKeyConstraint(
            ["cashier_user_id"], ["users.id"], name="fk_cashier_shift_cashier"
        ),
        sa.ForeignKeyConstraint(
            ["store_id", "cashier_user_id"],
            ["users.store_id", "users.id"],
            name="fk_cashier_shift_actor_store",
        ),
        sa.CheckConstraint(
            "status IN ('OPEN','CLOSED')", name="ck_cashier_shift_status_domain"
        ),
        sa.CheckConstraint("opening_cash_amount >= 0", name="ck_cashier_shift_opening_nonneg"),
        sa.CheckConstraint(
            f"(status = 'OPEN' AND {_OPEN_ALL_NULL}) "
            f"OR (status = 'CLOSED' AND {_CLOSED_ALL_SET})",
            name="ck_cashier_shift_status_snapshot",
        ),
        sa.CheckConstraint(
            "counted_closing_cash_amount IS NULL OR counted_closing_cash_amount >= 0",
            name="ck_cashier_shift_counted_nonneg",
        ),
        sa.CheckConstraint(
            "cash_payments_amount IS NULL OR cash_payments_amount >= 0",
            name="ck_cashier_shift_cash_pay_nonneg",
        ),
        sa.CheckConstraint(
            "cash_refunds_amount IS NULL OR cash_refunds_amount >= 0",
            name="ck_cashier_shift_cash_ref_nonneg",
        ),
        sa.CheckConstraint(
            "card_payments_amount IS NULL OR card_payments_amount >= 0",
            name="ck_cashier_shift_card_pay_nonneg",
        ),
        sa.CheckConstraint(
            "card_refunds_amount IS NULL OR card_refunds_amount >= 0",
            name="ck_cashier_shift_card_ref_nonneg",
        ),
        sa.CheckConstraint(
            "gross_payments_amount IS NULL OR gross_payments_amount >= 0",
            name="ck_cashier_shift_gross_nonneg",
        ),
        sa.CheckConstraint(
            "total_refunds_amount IS NULL OR total_refunds_amount >= 0",
            name="ck_cashier_shift_refunds_nonneg",
        ),
        sa.CheckConstraint(
            "gross_payments_amount IS NULL "
            "OR gross_payments_amount >= cash_payments_amount + card_payments_amount",
            name="ck_cashier_shift_gross_ge_parts",
        ),
        sa.CheckConstraint(
            "total_refunds_amount IS NULL "
            "OR total_refunds_amount >= cash_refunds_amount + card_refunds_amount",
            name="ck_cashier_shift_refunds_ge_parts",
        ),
        sa.UniqueConstraint(
            "store_id",
            "opened_idempotency_key_hash",
            name="uq_cashier_shift_store_open_idem",
        ),
    )
    op.create_index("ix_cashier_shifts_store_id", "cashier_shifts", ["store_id"])
    op.create_index(
        "ix_cashier_shifts_cashier_user_id", "cashier_shifts", ["cashier_user_id"]
    )
    op.create_index(
        "ix_cashier_shift_store_cashier",
        "cashier_shifts",
        ["store_id", "cashier_user_id"],
    )
    op.create_index(
        "ix_cashier_shift_store_opened", "cashier_shifts", ["store_id", "opened_at"]
    )

    # At most ONE open shift per (store, cashier). Two overlapping open windows
    # would both claim the same windowed payments — a double count.
    op.execute(
        """
        CREATE UNIQUE INDEX uq_cashier_shift_one_open
        ON cashier_shifts (store_id, cashier_user_id)
        WHERE status = 'OPEN'
        """
    )

    # ── Immutability / lifecycle guard ──────────────────────────────────────────
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
                    'cashier_shifts is append-only: a shift is history and is '
                    'never deleted'
                    USING ERRCODE = 'restrict_violation';
            END IF;

            -- UPDATE from here on.
            IF OLD.status = 'CLOSED' THEN
                RAISE EXCEPTION
                    'cashier shift % is closed: the close snapshot is immutable '
                    'and a closed shift cannot be reopened', OLD.id
                    USING ERRCODE = 'restrict_violation';
            END IF;

            -- OLD.status = 'OPEN'. The only permitted change is OPEN -> CLOSED,
            -- and the opening snapshot must be carried over unchanged.
            IF NEW.status <> 'CLOSED' THEN
                RAISE EXCEPTION
                    'cashier shift % may only transition from OPEN to CLOSED', OLD.id
                    USING ERRCODE = 'restrict_violation';
            END IF;

            IF NEW.store_id <> OLD.store_id
               OR NEW.cashier_user_id <> OLD.cashier_user_id
               OR NEW.opened_at <> OLD.opened_at
               OR NEW.opening_cash_amount <> OLD.opening_cash_amount
               OR NEW.opened_idempotency_key_hash <> OLD.opened_idempotency_key_hash
               OR NEW.opened_request_hash <> OLD.opened_request_hash
               OR NEW.open_note IS DISTINCT FROM OLD.open_note
            THEN
                RAISE EXCEPTION
                    'cashier shift % opening snapshot is immutable', OLD.id
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
        BEFORE UPDATE OR DELETE ON cashier_shifts
        FOR EACH ROW EXECUTE FUNCTION {_GUARD_FN}();
        """
    )


def downgrade() -> None:
    conn = op.get_bind()

    # Dropping cashier_shifts would destroy the record of how each till was
    # reconciled — the counted cash and the signed-off discrepancy — while the
    # payments and refunds it summarised remain. That is lossy and irreversible.
    shifts = conn.execute(sa.text("SELECT COUNT(*) FROM cashier_shifts")).scalar() or 0
    if shifts:
        raise CashierShiftsExist(
            f"Cannot downgrade cashier shift closing: {shifts} shift(s) exist. "
            "Dropping the table would destroy the record of how each till was "
            "reconciled (counted cash and discrepancy) while leaving the payments "
            "and refunds it summarised in place. Export the shifts first."
        )

    op.execute(f"DROP TRIGGER IF EXISTS {_TRG_GUARD} ON cashier_shifts")
    op.execute(f"DROP FUNCTION IF EXISTS {_GUARD_FN}()")
    op.execute("DROP INDEX IF EXISTS uq_cashier_shift_one_open")
    op.drop_index("ix_cashier_shift_store_opened", table_name="cashier_shifts")
    op.drop_index("ix_cashier_shift_store_cashier", table_name="cashier_shifts")
    op.drop_index("ix_cashier_shifts_cashier_user_id", table_name="cashier_shifts")
    op.drop_index("ix_cashier_shifts_store_id", table_name="cashier_shifts")
    op.drop_table("cashier_shifts")
