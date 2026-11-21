"""index the two sample tables on how they are actually read

Revision ID: c7e1b93d5a48
Revises: d6a4f27b8e91
Create Date: 2026-11-21

Both tables are written once per check or heartbeat and read by a range
over one owner, but neither was indexed that way.

url_check_samples had only monitor_id, so the seven-day uptime count read
every sample a monitor had ever kept and discarded most of them; and the
retention prune, which runs every sixty seconds, filtered on checked_at
with no index at all.

metric_samples had vm_id and recorded_at as two separate indexes, so a
chart query over one VM's recent history could use one of them but never
both. The composite is what that query asks for.

Built plainly rather than CONCURRENTLY: a week of retention keeps these to
a few hundred thousand rows, so the build is quick and the brief lock costs
less than the failure mode of a concurrent build, which leaves an invalid
index behind for someone to find later.
"""
from typing import Sequence, Union

from alembic import op

revision: str = 'c7e1b93d5a48'
down_revision: Union[str, None] = 'd6a4f27b8e91'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index('ix_url_check_samples_monitor_checked', 'url_check_samples',
                    ['monitor_id', 'checked_at'])
    op.create_index('ix_url_check_samples_checked_at', 'url_check_samples',
                    ['checked_at'])
    op.create_index('ix_metric_samples_vm_recorded', 'metric_samples',
                    ['vm_id', 'recorded_at'])


def downgrade() -> None:
    op.drop_index('ix_metric_samples_vm_recorded', table_name='metric_samples')
    op.drop_index('ix_url_check_samples_checked_at', table_name='url_check_samples')
    op.drop_index('ix_url_check_samples_monitor_checked', table_name='url_check_samples')
