"""add users.last_login_at

Revision ID: e5b1c93f7a20
Revises: d8f3a21c7b45
Create Date: 2026-09-14

Nullable with no backfill: existing users genuinely have no recorded login
until their next one, and NULL says that honestly where a default of "now"
or the creation date would invent history that never happened.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'e5b1c93f7a20'
down_revision: Union[str, None] = 'd8f3a21c7b45'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('users', sa.Column('last_login_at', sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.drop_column('users', 'last_login_at')
