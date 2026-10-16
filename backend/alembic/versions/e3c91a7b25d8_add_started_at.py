"""add started_at to containers and services

Revision ID: e3c91a7b25d8
Revises: c58b3e7d4190
Create Date: 2026-10-16

How long something has been up is the first thing asked of a service that
just alerted. Nullable because every row predates this column and agents
update themselves on their own schedule -- the value fills in on the next
heartbeat from an updated agent, with no backfill possible.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'e3c91a7b25d8'
down_revision: Union[str, None] = 'c58b3e7d4190'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('containers', sa.Column('started_at', sa.DateTime(), nullable=True))
    op.add_column('services', sa.Column('started_at', sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.drop_column('services', 'started_at')
    op.drop_column('containers', 'started_at')
