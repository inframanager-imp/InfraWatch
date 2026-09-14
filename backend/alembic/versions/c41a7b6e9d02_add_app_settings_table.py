"""add app_settings table

Revision ID: c41a7b6e9d02
Revises: 84496962add1
Create Date: 2026-09-14

Runtime-editable settings, keyed by name. Starts empty on purpose: every
lookup falls back to the environment value until an admin saves from the
UI, so an existing deployment behaves identically after this runs.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'c41a7b6e9d02'
down_revision: Union[str, None] = '84496962add1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'app_settings',
        sa.Column('key', sa.String(), nullable=False),
        sa.Column('value', sa.Text(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('key'),
    )


def downgrade() -> None:
    op.drop_table('app_settings')
