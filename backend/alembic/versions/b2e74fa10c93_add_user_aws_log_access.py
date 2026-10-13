"""add user aws log access

Revision ID: b2e74fa10c93
Revises: a93c6b15de74
Create Date: 2026-10-13

Which AWS accounts a read-only user may read logs from. Same shape as
user_vm_access, so there is one permission model to learn rather than two.

Starts empty, which means no read-only user can read AWS logs until they are
granted an account. Logs carry more than metrics do, so the safe default is
nothing rather than everything.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'b2e74fa10c93'
down_revision: Union[str, None] = 'a93c6b15de74'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'user_aws_log_access',
        sa.Column('id', sa.UUID(as_uuid=False), nullable=False),
        sa.Column('user_id', sa.UUID(as_uuid=False), nullable=False),
        sa.Column('account_id', sa.UUID(as_uuid=False), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['account_id'], ['aws_log_accounts.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('user_id', 'account_id', name='uq_user_aws_account'),
    )
    op.create_index('ix_user_aws_log_access_user', 'user_aws_log_access', ['user_id'])


def downgrade() -> None:
    op.drop_index('ix_user_aws_log_access_user', table_name='user_aws_log_access')
    op.drop_table('user_aws_log_access')
