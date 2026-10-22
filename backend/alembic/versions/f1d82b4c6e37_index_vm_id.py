"""index vm_id on containers, services and log_sources

Revision ID: f1d82b4c6e37
Revises: e3c91a7b25d8
Create Date: 2026-10-22

Every heartbeat deletes a VM's containers and services by vm_id before
re-inserting them -- 72 times a minute across the fleet, each one a
sequential scan because the column was never indexed. Fleet search filters
on the same column.

Built plainly rather than CONCURRENTLY: these tables hold a few thousand
rows, so the build is milliseconds and the brief lock costs less than the
failure mode of a concurrent build, which leaves an invalid index behind.
"""
from typing import Sequence, Union

from alembic import op

revision: str = 'f1d82b4c6e37'
down_revision: Union[str, None] = 'e3c91a7b25d8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index('ix_containers_vm_id', 'containers', ['vm_id'])
    op.create_index('ix_services_vm_id', 'services', ['vm_id'])
    op.create_index('ix_log_sources_vm_id', 'log_sources', ['vm_id'])


def downgrade() -> None:
    op.drop_index('ix_log_sources_vm_id', table_name='log_sources')
    op.drop_index('ix_services_vm_id', table_name='services')
    op.drop_index('ix_containers_vm_id', table_name='containers')
