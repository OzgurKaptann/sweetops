"""Production hardening: idempotency, audit log, actor tracking

Revision ID: c9f1d3e8a042
Revises: b7e5f2a9c341
Create Date: 2026-03-31 12:00:00.000000
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = 'c9f1d3e8a042'
down_revision: Union[str, None] = 'b7e5f2a9c341'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # --- orders: idempotency key ---
    op.add_column('orders', sa.Column(
        'idempotency_key', sa.String(64), nullable=True
    ))
    op.create_unique_constraint(
        'uq_orders_idempotency_key', 'orders', ['idempotency_key']
    )
    op.create_index('ix_orders_idempotency_key', 'orders', ['idempotency_key'])

    # --- order_status_events: actor tracking ---
    op.add_column('order_status_events', sa.Column(
        'actor_type', sa.String(20), nullable=True  # CUSTOMER | STAFF | SYSTEM
    ))
    op.add_column('order_status_events', sa.Column(
        'actor_id', sa.String(64), nullable=True    # session_id or staff_id
    ))
    op.add_column('order_status_events', sa.Column(
        'client_timestamp', sa.DateTime(timezone=True), nullable=True
    ))
    op.create_index(
        'ix_order_status_events_order_id',
        'order_status_events', ['order_id']
    )

    # --- sweetops_audit_log table ---
    op.create_table(
        'sweetops_audit_log',
        sa.Column('id', sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column('entity_type', sa.String(50), nullable=False),
        sa.Column('entity_id', sa.BigInteger(), nullable=False),
        sa.Column('action', sa.String(50), nullable=False),
        sa.Column('actor_type', sa.String(20), nullable=True),
        sa.Column('actor_id', sa.String(64), nullable=True),
        sa.Column('payload_before', sa.JSON(), nullable=True),
        sa.Column('payload_after', sa.JSON(), nullable=True),
        sa.Column('ip_address', sa.String(45), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True),
                  server_default=sa.text('now()'), nullable=False),
    )
    op.create_index('ix_sweetops_audit_log_entity', 'sweetops_audit_log', ['entity_type', 'entity_id'])
    op.create_index('ix_sweetops_audit_log_created_at', 'sweetops_audit_log', ['created_at'])


def downgrade() -> None:
    op.drop_table('sweetops_audit_log')
    op.drop_column('order_status_events', 'client_timestamp')
    op.drop_column('order_status_events', 'actor_id')
    op.drop_column('order_status_events', 'actor_type')
    op.drop_index('ix_orders_idempotency_key', table_name='orders')
    op.drop_constraint('uq_orders_idempotency_key', 'orders')
    op.drop_column('orders', 'idempotency_key')
