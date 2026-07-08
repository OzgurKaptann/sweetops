"""merge decision outcome and ingredient promotion heads

Revision ID: 4299b615f7aa
Revises: a1b2c3d4e5f6, e5f3a2d9c847
Create Date: 2026-07-08 15:03:54.092683

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '4299b615f7aa'
down_revision: Union[str, None] = ('a1b2c3d4e5f6', 'e5f3a2d9c847')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

def upgrade() -> None:
    pass

def downgrade() -> None:
    pass
