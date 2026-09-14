"""add alert groups

Revision ID: d8f3a21c7b45
Revises: c41a7b6e9d02
Create Date: 2026-09-14

Named recipient groups for alert email, assignable to VMs. Starts empty:
a VM with no group assigned keeps notifying every admin, which is what
every VM did before this existed.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'd8f3a21c7b45'
down_revision: Union[str, None] = 'c41a7b6e9d02'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'alert_groups',
        sa.Column('id', sa.UUID(as_uuid=False), nullable=False),
        sa.Column('name', sa.String(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('name', name='uq_alert_group_name'),
    )
    op.create_table(
        'alert_group_members',
        sa.Column('id', sa.UUID(as_uuid=False), nullable=False),
        sa.Column('group_id', sa.UUID(as_uuid=False), nullable=False),
        sa.Column('email', sa.String(), nullable=False),
        sa.ForeignKeyConstraint(['group_id'], ['alert_groups.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('group_id', 'email', name='uq_group_email'),
    )
    op.create_index('ix_alert_group_members_group_id', 'alert_group_members', ['group_id'])
    op.create_table(
        'vm_alert_groups',
        sa.Column('id', sa.UUID(as_uuid=False), nullable=False),
        sa.Column('vm_id', sa.UUID(as_uuid=False), nullable=False),
        sa.Column('group_id', sa.UUID(as_uuid=False), nullable=False),
        sa.ForeignKeyConstraint(['vm_id'], ['vms.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['group_id'], ['alert_groups.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('vm_id', 'group_id', name='uq_vm_alert_group'),
    )
    op.create_index('ix_vm_alert_groups_vm_id', 'vm_alert_groups', ['vm_id'])


def downgrade() -> None:
    op.drop_index('ix_vm_alert_groups_vm_id', table_name='vm_alert_groups')
    op.drop_table('vm_alert_groups')
    op.drop_index('ix_alert_group_members_group_id', table_name='alert_group_members')
    op.drop_table('alert_group_members')
    op.drop_table('alert_groups')
