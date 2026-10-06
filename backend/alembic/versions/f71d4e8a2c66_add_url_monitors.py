"""add url monitors

Revision ID: f71d4e8a2c66
Revises: e5b1c93f7a20
Create Date: 2026-10-06

URL and certificate monitors, each belonging to a VM, plus a sample per check
so the UI can show recent uptime. Samples are pruned on the same schedule as
metric samples.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'f71d4e8a2c66'
down_revision: Union[str, None] = 'e5b1c93f7a20'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'url_monitors',
        sa.Column('id', sa.UUID(as_uuid=False), nullable=False),
        sa.Column('vm_id', sa.UUID(as_uuid=False), nullable=False),
        sa.Column('name', sa.String(), nullable=False),
        sa.Column('url', sa.String(), nullable=False),
        sa.Column('check_from', sa.String(), nullable=False, server_default='server'),
        sa.Column('method', sa.String(), nullable=False, server_default='GET'),
        sa.Column('expected_status', sa.String(), nullable=True),
        sa.Column('body_contains', sa.String(), nullable=True),
        sa.Column('headers', sa.Text(), nullable=True),
        sa.Column('interval_seconds', sa.Integer(), nullable=False, server_default='60'),
        sa.Column('timeout_seconds', sa.Integer(), nullable=False, server_default='10'),
        sa.Column('failure_threshold', sa.Integer(), nullable=False, server_default='2'),
        sa.Column('slow_ms', sa.Integer(), nullable=True),
        sa.Column('verify_tls', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('cert_warn_days', sa.String(), nullable=True),
        sa.Column('enabled', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        # last-result columns live on the row itself: the table is read far more
        # often than it is written, and every read wants the current state.
        sa.Column('last_checked_at', sa.DateTime(), nullable=True),
        sa.Column('last_status', sa.String(), nullable=True),
        sa.Column('last_code', sa.Integer(), nullable=True),
        sa.Column('last_response_ms', sa.Integer(), nullable=True),
        sa.Column('last_error', sa.String(), nullable=True),
        sa.Column('consecutive_failures', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('cert_expires_at', sa.DateTime(), nullable=True),
        sa.Column('cert_issuer', sa.String(), nullable=True),
        sa.Column('cert_error', sa.String(), nullable=True),
        sa.ForeignKeyConstraint(['vm_id'], ['vms.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('vm_id', 'name', name='uq_vm_monitor_name'),
    )
    op.create_index('ix_url_monitors_vm_id', 'url_monitors', ['vm_id'])

    op.create_table(
        'url_check_samples',
        sa.Column('id', sa.UUID(as_uuid=False), nullable=False),
        sa.Column('monitor_id', sa.UUID(as_uuid=False), nullable=False),
        sa.Column('checked_at', sa.DateTime(), nullable=False),
        sa.Column('ok', sa.Boolean(), nullable=False),
        sa.Column('response_ms', sa.Integer(), nullable=True),
        sa.Column('status_code', sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(['monitor_id'], ['url_monitors.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_url_check_samples_monitor', 'url_check_samples', ['monitor_id', 'checked_at'])


def downgrade() -> None:
    op.drop_index('ix_url_check_samples_monitor', table_name='url_check_samples')
    op.drop_table('url_check_samples')
    op.drop_index('ix_url_monitors_vm_id', table_name='url_monitors')
    op.drop_table('url_monitors')
