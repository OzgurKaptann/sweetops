"""payment settlement: ledger tables + order payment summary

Revision ID: b8c4d1e6f207
Revises: a7d3f9b21c05
Create Date: 2026-07-10

Adds:
  - payment_settlements  (one cashier collection action),
  - payment_allocations  (money applied to one order),
  - payment_refunds      (append-only reversals),
  - order payment-summary columns (payment_status, refund_status,
    paid_amount, refunded_amount) with domain + non-negativity constraints,
  - supporting indexes and per-store idempotency uniqueness.

Database-enforced financial integrity (PL/pgSQL triggers — see section below):
  - Cross-entity consistency: a settlement's table and cashier must belong to
    its store; an allocation's order must share the settlement's store AND
    table; a refund's allocation/settlement/order/store/currency must all be
    mutually consistent. These hold even if the service layer is bypassed.
  - Ledger immutability: UPDATE and DELETE on the three ledger tables are
    unconditionally refused (append-only). There is NO application-accessible
    runtime bypass — the trigger honours no GUC, session variable, or any other
    value the application role can set via SET / SET LOCAL / set_config. The only
    way to mutate history is a controlled migration or privileged, ownership-
    gated trigger administration (ALTER TABLE ... DISABLE TRIGGER) performed
    outside the application runtime. Corrections are append-only refund rows.
  - Settlement total: a DEFERRABLE INITIALLY DEFERRED constraint trigger
    checks, at COMMIT, that gross_amount == SUM(allocation amounts).

Trigger-function hardening:
  - All trigger functions are SECURITY INVOKER (the default) — no elevated
    privilege is needed, so none is taken.
  - Each function pins a fixed ``search_path = pg_catalog`` and references every
    table schema-qualified (``public.<table>``), so object resolution can never
    be diverted by an attacker-controlled ``search_path``.
  - EXECUTE on every trigger function is revoked from PUBLIC; only the trigger
    machinery invokes them.
  - No dynamic SQL, no client value interpolated into executable SQL, and every
    raised exception uses the stable SQLSTATE 'integrity_constraint_violation'
    (23000) without leaking secret data.

Safety:
  - Existing order rows are preserved. Every order backfills to UNPAID / NONE
    with zero paid & refunded amounts — no prior payment is ever fabricated.
  - No cashier users and no credentials are created here.
  - downgrade() removes ONLY payment-related schema. It drops every payment
    trigger and trigger FUNCTION explicitly BEFORE dropping the payment tables;
    order rows and their pre-existing columns are untouched.
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "b8c4d1e6f207"
down_revision = "a7d3f9b21c05"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── 1. Order payment-summary columns ────────────────────────────────────
    # Added with safe server defaults so existing rows backfill deterministically
    # to an unpaid / never-refunded state.
    op.add_column("orders", sa.Column("payment_status", sa.String(length=20),
                                      server_default="UNPAID", nullable=False))
    op.add_column("orders", sa.Column("refund_status", sa.String(length=20),
                                      server_default="NONE", nullable=False))
    op.add_column("orders", sa.Column("paid_amount", sa.Numeric(12, 2),
                                      server_default="0", nullable=False))
    op.add_column("orders", sa.Column("refunded_amount", sa.Numeric(12, 2),
                                      server_default="0", nullable=False))

    op.create_index("ix_orders_payment_status", "orders", ["payment_status"])

    op.create_check_constraint("ck_order_paid_nonneg", "orders", "paid_amount >= 0")
    op.create_check_constraint("ck_order_refunded_nonneg", "orders", "refunded_amount >= 0")
    op.create_check_constraint("ck_order_refund_le_paid", "orders", "refunded_amount <= paid_amount")
    op.create_check_constraint(
        "ck_order_payment_status_domain", "orders",
        "payment_status IN ('UNPAID','PARTIALLY_PAID','PAID')",
    )
    op.create_check_constraint(
        "ck_order_refund_status_domain", "orders",
        "refund_status IN ('NONE','PARTIALLY_REFUNDED','REFUNDED')",
    )

    # ── 2. payment_settlements ──────────────────────────────────────────────
    op.create_table(
        "payment_settlements",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("store_id", sa.Integer(), nullable=False),
        sa.Column("table_id", sa.Integer(), nullable=True),
        sa.Column("cashier_user_id", sa.Integer(), nullable=False),
        sa.Column("payment_method", sa.String(length=16), nullable=False),
        sa.Column("currency", sa.String(length=3), server_default="TRY", nullable=False),
        sa.Column("gross_amount", sa.Numeric(12, 2), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="COMPLETED", nullable=False),
        sa.Column("note", sa.String(length=500), nullable=True),
        sa.Column("terminal_reference", sa.String(length=64), nullable=True),
        sa.Column("idempotency_key_hash", sa.String(length=64), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["store_id"], ["stores.id"]),
        sa.ForeignKeyConstraint(["table_id"], ["tables.id"]),
        sa.ForeignKeyConstraint(["cashier_user_id"], ["users.id"]),
        sa.CheckConstraint("gross_amount > 0", name="ck_settlement_amount_positive"),
        sa.CheckConstraint("payment_method IN ('CASH','CARD','OTHER')", name="ck_settlement_method_domain"),
        sa.CheckConstraint("status IN ('COMPLETED')", name="ck_settlement_status_domain"),
        sa.CheckConstraint("char_length(currency) BETWEEN 1 AND 3", name="ck_settlement_currency_len"),
    )
    op.create_index("ix_payment_settlements_store_id", "payment_settlements", ["store_id"])
    op.create_index("ix_payment_settlements_table_id", "payment_settlements", ["table_id"])
    op.create_index("ix_payment_settlements_cashier_user_id", "payment_settlements", ["cashier_user_id"])
    op.create_index("uq_settlement_store_idem", "payment_settlements",
                    ["store_id", "idempotency_key_hash"], unique=True)

    # ── 3. payment_allocations ──────────────────────────────────────────────
    op.create_table(
        "payment_allocations",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("settlement_id", sa.BigInteger(), nullable=False),
        sa.Column("order_id", sa.Integer(), nullable=False),
        sa.Column("amount", sa.Numeric(12, 2), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["settlement_id"], ["payment_settlements.id"]),
        sa.ForeignKeyConstraint(["order_id"], ["orders.id"]),
        sa.CheckConstraint("amount > 0", name="ck_allocation_amount_positive"),
    )
    op.create_index("ix_payment_allocations_settlement_id", "payment_allocations", ["settlement_id"])
    op.create_index("ix_payment_allocations_order_id", "payment_allocations", ["order_id"])
    op.create_index("ix_allocation_settlement_order", "payment_allocations", ["settlement_id", "order_id"])

    # ── 4. payment_refunds ──────────────────────────────────────────────────
    op.create_table(
        "payment_refunds",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("store_id", sa.Integer(), nullable=False),
        sa.Column("settlement_id", sa.BigInteger(), nullable=False),
        sa.Column("allocation_id", sa.BigInteger(), nullable=False),
        sa.Column("order_id", sa.Integer(), nullable=False),
        sa.Column("amount", sa.Numeric(12, 2), nullable=False),
        sa.Column("currency", sa.String(length=3), server_default="TRY", nullable=False),
        sa.Column("reason", sa.String(length=500), nullable=False),
        sa.Column("refunded_by_user_id", sa.Integer(), nullable=False),
        sa.Column("idempotency_key_hash", sa.String(length=64), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["store_id"], ["stores.id"]),
        sa.ForeignKeyConstraint(["settlement_id"], ["payment_settlements.id"]),
        sa.ForeignKeyConstraint(["allocation_id"], ["payment_allocations.id"]),
        sa.ForeignKeyConstraint(["order_id"], ["orders.id"]),
        sa.ForeignKeyConstraint(["refunded_by_user_id"], ["users.id"]),
        sa.CheckConstraint("amount > 0", name="ck_refund_amount_positive"),
        sa.CheckConstraint("char_length(reason) > 0", name="ck_refund_reason_present"),
    )
    op.create_index("ix_payment_refunds_store_id", "payment_refunds", ["store_id"])
    op.create_index("ix_payment_refunds_settlement_id", "payment_refunds", ["settlement_id"])
    op.create_index("ix_payment_refunds_allocation_id", "payment_refunds", ["allocation_id"])
    op.create_index("ix_payment_refunds_order_id", "payment_refunds", ["order_id"])
    op.create_index("ix_payment_refunds_refunded_by_user_id", "payment_refunds", ["refunded_by_user_id"])
    op.create_index("uq_refund_store_idem", "payment_refunds",
                    ["store_id", "idempotency_key_hash"], unique=True)

    # ── 5. Database-enforced financial integrity (PL/pgSQL) ──────────────────
    # These guarantees do not depend on the authenticated service layer. Even a
    # direct SQL client cannot create an internally inconsistent ledger row,
    # mutate a completed record, or commit a settlement whose parts do not add
    # up. All raise SQLSTATE 23000 (integrity_constraint_violation) so drivers
    # surface them as IntegrityError.

    # 5a. Cross-entity consistency on settlement insert:
    #     table.store_id == settlement.store_id (when a table is referenced),
    #     cashier.store_id == settlement.store_id.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION public.sweetops_settlement_refs_check()
        RETURNS trigger LANGUAGE plpgsql
        SECURITY INVOKER
        SET search_path = pg_catalog
        AS $fn$
        DECLARE
            tbl_store integer;
            usr_store integer;
        BEGIN
            IF NEW.table_id IS NOT NULL THEN
                SELECT store_id INTO tbl_store FROM public.tables WHERE id = NEW.table_id;
                IF tbl_store IS DISTINCT FROM NEW.store_id THEN
                    RAISE EXCEPTION
                        'settlement table % belongs to store %, not settlement store %',
                        NEW.table_id, tbl_store, NEW.store_id
                        USING ERRCODE = 'integrity_constraint_violation';
                END IF;
            END IF;
            SELECT store_id INTO usr_store FROM public.users WHERE id = NEW.cashier_user_id;
            IF usr_store IS DISTINCT FROM NEW.store_id THEN
                RAISE EXCEPTION
                    'settlement cashier % belongs to store %, not settlement store %',
                    NEW.cashier_user_id, usr_store, NEW.store_id
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            RETURN NEW;
        END;
        $fn$;
        """
    )
    op.execute(
        "REVOKE ALL ON FUNCTION public.sweetops_settlement_refs_check() FROM PUBLIC"
    )
    op.execute(
        """
        CREATE TRIGGER trg_settlement_refs
        BEFORE INSERT ON payment_settlements
        FOR EACH ROW EXECUTE FUNCTION public.sweetops_settlement_refs_check();
        """
    )

    # 5b. Cross-entity consistency on allocation insert:
    #     order.store_id == settlement.store_id AND
    #     order.table_id IS NOT DISTINCT FROM settlement.table_id
    #     (null-safe so a table-less single-order settlement is still exact).
    op.execute(
        """
        CREATE OR REPLACE FUNCTION public.sweetops_allocation_refs_check()
        RETURNS trigger LANGUAGE plpgsql
        SECURITY INVOKER
        SET search_path = pg_catalog
        AS $fn$
        DECLARE
            s_store integer; s_table integer;
            o_store integer; o_table integer;
        BEGIN
            SELECT store_id, table_id INTO s_store, s_table
                FROM public.payment_settlements WHERE id = NEW.settlement_id;
            SELECT store_id, table_id INTO o_store, o_table
                FROM public.orders WHERE id = NEW.order_id;
            IF o_store IS DISTINCT FROM s_store THEN
                RAISE EXCEPTION
                    'allocation order % is in store %, settlement store is %',
                    NEW.order_id, o_store, s_store
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            IF o_table IS DISTINCT FROM s_table THEN
                RAISE EXCEPTION
                    'allocation order % is on table %, settlement table is %',
                    NEW.order_id, o_table, s_table
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            RETURN NEW;
        END;
        $fn$;
        """
    )
    op.execute(
        "REVOKE ALL ON FUNCTION public.sweetops_allocation_refs_check() FROM PUBLIC"
    )
    op.execute(
        """
        CREATE TRIGGER trg_allocation_refs
        BEFORE INSERT ON payment_allocations
        FOR EACH ROW EXECUTE FUNCTION public.sweetops_allocation_refs_check();
        """
    )

    # 5c. Cross-entity consistency on refund insert: the refund's allocation,
    #     settlement, order, store and currency must all describe ONE original
    #     financial allocation.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION public.sweetops_refund_refs_check()
        RETURNS trigger LANGUAGE plpgsql
        SECURITY INVOKER
        SET search_path = pg_catalog
        AS $fn$
        DECLARE
            a_settlement bigint; a_order integer;
            s_store integer; s_currency varchar;
        BEGIN
            SELECT settlement_id, order_id INTO a_settlement, a_order
                FROM public.payment_allocations WHERE id = NEW.allocation_id;
            IF a_settlement IS DISTINCT FROM NEW.settlement_id THEN
                RAISE EXCEPTION
                    'refund allocation % belongs to settlement %, not %',
                    NEW.allocation_id, a_settlement, NEW.settlement_id
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            IF a_order IS DISTINCT FROM NEW.order_id THEN
                RAISE EXCEPTION
                    'refund allocation % is for order %, not %',
                    NEW.allocation_id, a_order, NEW.order_id
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            SELECT store_id, currency INTO s_store, s_currency
                FROM public.payment_settlements WHERE id = NEW.settlement_id;
            IF s_store IS DISTINCT FROM NEW.store_id THEN
                RAISE EXCEPTION
                    'refund store % does not match settlement store %',
                    NEW.store_id, s_store
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            IF NEW.currency IS DISTINCT FROM s_currency THEN
                RAISE EXCEPTION
                    'refund currency % does not match settlement currency %',
                    NEW.currency, s_currency
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            RETURN NEW;
        END;
        $fn$;
        """
    )
    op.execute(
        "REVOKE ALL ON FUNCTION public.sweetops_refund_refs_check() FROM PUBLIC"
    )
    op.execute(
        """
        CREATE TRIGGER trg_refund_refs
        BEFORE INSERT ON payment_refunds
        FOR EACH ROW EXECUTE FUNCTION public.sweetops_refund_refs_check();
        """
    )

    # 5d. Settlement total == sum of its allocations, verified at COMMIT so the
    #     normal insert-settlement → insert-allocations → commit sequence works,
    #     but a completed settlement whose parts do not add up is rejected.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION public.sweetops_settlement_total_check()
        RETURNS trigger LANGUAGE plpgsql
        SECURITY INVOKER
        SET search_path = pg_catalog
        AS $fn$
        DECLARE
            sid bigint;
            s_gross numeric(12,2);
            a_sum numeric(12,2);
        BEGIN
            IF TG_TABLE_NAME = 'payment_allocations' THEN
                sid := NEW.settlement_id;
            ELSE
                sid := NEW.id;
            END IF;
            SELECT gross_amount INTO s_gross FROM public.payment_settlements WHERE id = sid;
            IF s_gross IS NULL THEN
                -- Settlement no longer present at commit (rolled back / removed
                -- under maintenance) — there is nothing to reconcile.
                RETURN NULL;
            END IF;
            SELECT COALESCE(SUM(amount), 0) INTO a_sum
                FROM public.payment_allocations WHERE settlement_id = sid;
            IF s_gross <> a_sum THEN
                RAISE EXCEPTION
                    'settlement % gross_amount % <> allocation total %',
                    sid, s_gross, a_sum
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            RETURN NULL;
        END;
        $fn$;
        """
    )
    op.execute(
        "REVOKE ALL ON FUNCTION public.sweetops_settlement_total_check() FROM PUBLIC"
    )
    op.execute(
        """
        CREATE CONSTRAINT TRIGGER trg_settlement_total_on_settlement
        AFTER INSERT ON payment_settlements
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION public.sweetops_settlement_total_check();
        """
    )
    op.execute(
        """
        CREATE CONSTRAINT TRIGGER trg_settlement_total_on_allocation
        AFTER INSERT ON payment_allocations
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION public.sweetops_settlement_total_check();
        """
    )

    # 5e. Append-only immutability: UNCONDITIONALLY refuse UPDATE and DELETE on
    #     every ledger table. Corrections are new refund rows, never edits.
    #
    #     There is deliberately NO runtime bypass. The function honours no GUC,
    #     session variable, or any other value the application role can set with
    #     SET / SET LOCAL / set_config — a custom dotted GUC such as
    #     'sweetops.ledger_admin' is settable by ANY role (including through an
    #     SQL-injection path in an ordinary query), so it was never a real
    #     privilege boundary. Mutating financial history is therefore only
    #     possible via a controlled migration or an ownership-gated
    #     ALTER TABLE ... DISABLE TRIGGER performed outside the application
    #     runtime; the ordinary application role, which is not the table owner in
    #     a correctly provisioned deployment, cannot do either.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION public.sweetops_block_ledger_mutation()
        RETURNS trigger LANGUAGE plpgsql
        SECURITY INVOKER
        SET search_path = pg_catalog
        AS $fn$
        BEGIN
            RAISE EXCEPTION
                'payment ledger is append-only: % on % is not permitted',
                TG_OP, TG_TABLE_NAME
                USING ERRCODE = 'integrity_constraint_violation';
        END;
        $fn$;
        """
    )
    op.execute(
        "REVOKE ALL ON FUNCTION public.sweetops_block_ledger_mutation() FROM PUBLIC"
    )
    for _table in ("payment_settlements", "payment_allocations", "payment_refunds"):
        op.execute(
            f"""
            CREATE TRIGGER trg_{_table}_immutable
            BEFORE UPDATE OR DELETE ON {_table}
            FOR EACH ROW EXECUTE FUNCTION public.sweetops_block_ledger_mutation();
            """
        )


def downgrade() -> None:
    # Remove ONLY payment schema — order rows and their pre-existing columns
    # are left intact. Every payment trigger and trigger FUNCTION is dropped
    # EXPLICITLY here, before the tables, so the teardown is deterministic and
    # never relies on cascade-from-table-drop to remove the immutability guard.
    _triggers = (
        ("trg_settlement_refs", "payment_settlements"),
        ("trg_settlement_total_on_settlement", "payment_settlements"),
        ("trg_payment_settlements_immutable", "payment_settlements"),
        ("trg_allocation_refs", "payment_allocations"),
        ("trg_settlement_total_on_allocation", "payment_allocations"),
        ("trg_payment_allocations_immutable", "payment_allocations"),
        ("trg_refund_refs", "payment_refunds"),
        ("trg_payment_refunds_immutable", "payment_refunds"),
    )
    for _trg, _tbl in _triggers:
        op.execute(f"DROP TRIGGER IF EXISTS {_trg} ON {_tbl}")

    for _fn in (
        "sweetops_settlement_refs_check",
        "sweetops_allocation_refs_check",
        "sweetops_refund_refs_check",
        "sweetops_settlement_total_check",
        "sweetops_block_ledger_mutation",
    ):
        op.execute(f"DROP FUNCTION IF EXISTS public.{_fn}()")

    op.drop_table("payment_refunds")
    op.drop_table("payment_allocations")
    op.drop_table("payment_settlements")

    op.drop_constraint("ck_order_refund_status_domain", "orders", type_="check")
    op.drop_constraint("ck_order_payment_status_domain", "orders", type_="check")
    op.drop_constraint("ck_order_refund_le_paid", "orders", type_="check")
    op.drop_constraint("ck_order_refunded_nonneg", "orders", type_="check")
    op.drop_constraint("ck_order_paid_nonneg", "orders", type_="check")
    op.drop_index("ix_orders_payment_status", table_name="orders")
    op.drop_column("orders", "refunded_amount")
    op.drop_column("orders", "paid_amount")
    op.drop_column("orders", "refund_status")
    op.drop_column("orders", "payment_status")
