"""log sources can be a folder with files under it

Revision ID: d6a4f27b8e91
Revises: b5e27a4c1f93
Create Date: 2026-11-16

A folder entry is a path with a wildcard in it. Syncing one asks the VM
what matches and writes a row per file, each pointing back at the folder
that found it -- so the files can be listed under it, and go away with it.

Both columns are null on every row that already exists, which is right:
those are paths someone typed by hand and nothing discovered.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'd6a4f27b8e91'
down_revision: Union[str, None] = 'b5e27a4c1f93'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('log_sources', sa.Column('parent_id', sa.UUID(as_uuid=False), nullable=True))
    op.add_column('log_sources', sa.Column('synced_at', sa.DateTime(), nullable=True))
    op.create_index('ix_log_sources_parent_id', 'log_sources', ['parent_id'])
    op.create_foreign_key('fk_log_sources_parent_id', 'log_sources', 'log_sources',
                          ['parent_id'], ['id'])


def downgrade() -> None:
    op.drop_constraint('fk_log_sources_parent_id', 'log_sources', type_='foreignkey')
    op.drop_index('ix_log_sources_parent_id', table_name='log_sources')
    op.drop_column('log_sources', 'synced_at')
    op.drop_column('log_sources', 'parent_id')
