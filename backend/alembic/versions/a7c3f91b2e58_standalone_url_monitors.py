"""allow url monitors and alerts with no vm

Revision ID: a7c3f91b2e58
Revises: f1d82b4c6e37
Create Date: 2026-11-11

Some endpoints worth watching do not run on a VM in this fleet -- a partner
API, a managed service, something on a server InfraWatch has no agent on.

Dropping NOT NULL on url_monitors.vm_id is the easy half. The half that
matters is alerts.vm_id: without it a VM-less monitor could be checked and
charted but could never raise anything, which is worse than not having it.

Both unique constraints are keyed on vm_id, and SQL treats NULLs as
distinct, so neither covers the rows this change introduces. Two partial
unique indexes carry identity for those: one monitor name, and one alert per
(resource_type, resource_name, rule), among the rows with no VM.

Both ALTERs are metadata-only in Postgres -- DROP NOT NULL does not rewrite
the table -- so this is quick regardless of how many alerts have piled up.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'a7c3f91b2e58'
down_revision: Union[str, None] = 'f1d82b4c6e37'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column('url_monitors', 'vm_id', existing_type=sa.UUID(as_uuid=False), nullable=True)
    op.alter_column('alerts', 'vm_id', existing_type=sa.UUID(as_uuid=False), nullable=True)
    op.create_index('uq_monitor_name_global', 'url_monitors', ['name'], unique=True,
                    postgresql_where=sa.text('vm_id IS NULL'))
    op.create_index('uq_alert_identity_global', 'alerts',
                    ['resource_type', 'resource_name', 'rule'], unique=True,
                    postgresql_where=sa.text('vm_id IS NULL'))


def downgrade() -> None:
    # Rows with no VM cannot satisfy NOT NULL, so they go first. Deliberately
    # destructive and deliberately explicit: there is nothing to migrate them
    # to, since the column they lack is the one being made mandatory again.
    op.execute('DELETE FROM alerts WHERE vm_id IS NULL')
    op.execute('DELETE FROM url_monitors WHERE vm_id IS NULL')
    op.drop_index('uq_alert_identity_global', table_name='alerts')
    op.drop_index('uq_monitor_name_global', table_name='url_monitors')
    op.alter_column('alerts', 'vm_id', existing_type=sa.UUID(as_uuid=False), nullable=False)
    op.alter_column('url_monitors', 'vm_id', existing_type=sa.UUID(as_uuid=False), nullable=False)
