"""Add waffle MVP columns and tables

Revision ID: b7e5f2a9c341
Revises: 2478943c11df
Create Date: 2026-03-25 22:55:00.000000

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = 'b7e5f2a9c341'
down_revision: Union[str, None] = '2478943c11df'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # --- ingredients: add new columns ---
    op.add_column('ingredients', sa.Column('unit', sa.String(10), nullable=False, server_default='g'))
    op.add_column('ingredients', sa.Column('standard_quantity', sa.Numeric(8, 2), nullable=False, server_default='0'))
    op.add_column('ingredients', sa.Column('cost_per_unit', sa.Numeric(8, 4), nullable=True))
    op.add_column('ingredients', sa.Column('shelf_life_days', sa.Integer(), nullable=True))
    op.add_column('ingredients', sa.Column('allows_portion_choice', sa.Boolean(), nullable=False, server_default='false'))
    op.add_column('ingredients', sa.Column('is_active', sa.Boolean(), nullable=False, server_default='true'))

    # --- order_item_ingredients: add consumption snapshot ---
    op.add_column('order_item_ingredients', sa.Column('consumed_quantity', sa.Numeric(8, 2), nullable=True))
    op.add_column('order_item_ingredients', sa.Column('consumed_unit', sa.String(10), nullable=True))

    # --- ingredient_stock: new table ---
    op.create_table('ingredient_stock',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('ingredient_id', sa.Integer(), nullable=False),
        sa.Column('stock_quantity', sa.Numeric(10, 2), nullable=False, server_default='0'),
        sa.Column('unit', sa.String(10), nullable=False),
        sa.Column('reorder_level', sa.Numeric(10, 2), nullable=True),
        sa.Column('last_restocked', sa.DateTime(timezone=True), nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()')),
        sa.ForeignKeyConstraint(['ingredient_id'], ['ingredients.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('ingredient_id'),
    )
    op.create_index(op.f('ix_ingredient_stock_id'), 'ingredient_stock', ['id'], unique=False)

    # --- ingredient_stock_movements: new table ---
    op.create_table('ingredient_stock_movements',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('ingredient_id', sa.Integer(), nullable=False),
        sa.Column('movement_type', sa.String(30), nullable=False),
        sa.Column('quantity_delta', sa.Numeric(10, 2), nullable=False),
        sa.Column('unit', sa.String(10), nullable=False),
        sa.Column('reference_type', sa.String(30), nullable=True),
        sa.Column('reference_id', sa.Integer(), nullable=True),
        sa.Column('note', sa.String(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()')),
        sa.ForeignKeyConstraint(['ingredient_id'], ['ingredients.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_ingredient_stock_movements_id'), 'ingredient_stock_movements', ['id'], unique=False)
    op.create_index(op.f('ix_ingredient_stock_movements_ingredient_id'), 'ingredient_stock_movements', ['ingredient_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_ingredient_stock_movements_ingredient_id'), table_name='ingredient_stock_movements')
    op.drop_index(op.f('ix_ingredient_stock_movements_id'), table_name='ingredient_stock_movements')
    op.drop_table('ingredient_stock_movements')
    op.drop_index(op.f('ix_ingredient_stock_id'), table_name='ingredient_stock')
    op.drop_table('ingredient_stock')
    op.drop_column('order_item_ingredients', 'consumed_unit')
    op.drop_column('order_item_ingredients', 'consumed_quantity')
    op.drop_column('ingredients', 'is_active')
    op.drop_column('ingredients', 'allows_portion_choice')
    op.drop_column('ingredients', 'shelf_life_days')
    op.drop_column('ingredients', 'cost_per_unit')
    op.drop_column('ingredients', 'standard_quantity')
    op.drop_column('ingredients', 'unit')
