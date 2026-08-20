"""default system service logs off retroactively

Revision ID: 4e95b00ce6a2
Revises: acbde94af839
Create Date: 2026-08-19

"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = '4e95b00ce6a2'
down_revision: Union[str, None] = 'acbde94af839'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # The service Monitor/Logs default changed to "both off for OS/package
    # services, both on for custom ones" — previously only Monitor followed
    # that rule, Logs always started on. This brings already-recorded
    # services in line with the new default.
    #
    # Caveat: there's no column tracking whether a value is still the
    # untouched default or something a user deliberately set, so this can't
    # tell "still default true" apart from "someone already turned Logs on
    # for this system service on purpose" — both look identical. It updates
    # every currently-true row indiscriminately. Accepted tradeoff: at the
    # time of this migration no user had used the Logs toggle on a system
    # service yet, so in practice this doesn't clobber a real choice — but
    # it would if run later, after that's no longer true.
    op.execute("""
        UPDATE resource_settings rs
        SET logs_enabled = false
        FROM services s
        WHERE rs.resource_type = 'service'
          AND rs.vm_id = s.vm_id
          AND rs.name = s.name
          AND s.is_custom = false
          AND rs.logs_enabled = true
    """)


def downgrade() -> None:
    # Not reversible — we don't record which rows this touched vs which
    # were already false for another reason.
    pass
