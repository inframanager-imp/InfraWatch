"""add aws log labels

Revision ID: c58b3e7d4190
Revises: b2e74fa10c93
Create Date: 2026-10-13

A display name for a log group. ECS service names are discovered live where
the role allows it; this is the manual override, and the only way to name
anything ECS does not own (an RDS instance, VPC flow logs).
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'c58b3e7d4190'
down_revision: Union[str, None] = 'b2e74fa10c93'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'aws_log_labels',
        sa.Column('id', sa.UUID(as_uuid=False), nullable=False),
        sa.Column('account_id', sa.UUID(as_uuid=False), nullable=False),
        sa.Column('log_group', sa.String(), nullable=False),
        sa.Column('label', sa.String(), nullable=False),
        sa.ForeignKeyConstraint(['account_id'], ['aws_log_accounts.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('account_id', 'log_group', name='uq_aws_log_label'),
    )


def downgrade() -> None:
    op.drop_table('aws_log_labels')
