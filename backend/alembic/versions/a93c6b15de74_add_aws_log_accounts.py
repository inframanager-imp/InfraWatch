"""add aws log accounts

Revision ID: a93c6b15de74
Revises: f71d4e8a2c66
Create Date: 2026-10-13

One row per AWS account InfraWatch reads CloudWatch Logs from. No secret is
stored: access is by assuming a read-only role from the EC2 instance role, so
the row holds only the role ARN, the external ID and a region.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'a93c6b15de74'
down_revision: Union[str, None] = 'f71d4e8a2c66'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'aws_log_accounts',
        sa.Column('id', sa.UUID(as_uuid=False), nullable=False),
        sa.Column('name', sa.String(), nullable=False),
        sa.Column('role_arn', sa.String(), nullable=False),
        sa.Column('external_id', sa.String(), nullable=True),
        sa.Column('region', sa.String(), nullable=False, server_default='us-east-1'),
        sa.Column('enabled', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('last_checked_at', sa.DateTime(), nullable=True),
        sa.Column('last_check_ok', sa.Boolean(), nullable=True),
        sa.Column('last_check_detail', sa.String(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('name', name='uq_aws_log_account_name'),
    )


def downgrade() -> None:
    op.drop_table('aws_log_accounts')
