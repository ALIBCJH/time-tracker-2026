"""disk space alerts

Everything shares one volume — Postgres, the nightly dumps, Docker's layers and,
without S3, every screen capture. It fills from several directions at once, and
the first symptom of a full disk is Postgres refusing to write.

Nothing was watching it. This widens the alert kinds so the existing send-once
machinery can carry the warning.

Revision ID: e5c71a0d9f38
Revises: d3e8b1c05f74
Create Date: 2026-08-27 19:40:00.000000

"""
from typing import Sequence, Union

from alembic import op


revision: str = 'e5c71a0d9f38'
down_revision: Union[str, None] = 'd3e8b1c05f74'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

OLD = "kind IN ('session_dropped', 'device_dormant')"
NEW = "kind IN ('session_dropped', 'device_dormant', 'disk_space')"


def upgrade() -> None:
    op.drop_constraint('ck_agent_alerts_kind', 'agent_alerts', type_='check')
    op.create_check_constraint('ck_agent_alerts_kind', 'agent_alerts', NEW)


def downgrade() -> None:
    # Rows of the new kind would violate the narrower constraint, so they go
    # first. They are a record of a warning already delivered, not data anybody
    # can act on later.
    op.execute("DELETE FROM agent_alerts WHERE kind = 'disk_space'")
    op.drop_constraint('ck_agent_alerts_kind', 'agent_alerts', type_='check')
    op.create_check_constraint('ck_agent_alerts_kind', 'agent_alerts', OLD)
