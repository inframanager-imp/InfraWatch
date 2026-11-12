"""a custom log path can live inside a container

Revision ID: b5e27a4c1f93
Revises: a7c3f91b2e58
Create Date: 2026-11-12

An app that writes to its own filesystem rather than to stdout or a mounted
volume leaves a file `tail -F` on the VM cannot see. Naming the container
lets the agent read the same path through `docker exec` instead.

Null means what every existing row means: the path is on the VM.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'b5e27a4c1f93'
down_revision: Union[str, None] = 'a7c3f91b2e58'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('log_sources', sa.Column('container', sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column('log_sources', 'container')
