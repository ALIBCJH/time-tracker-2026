"""agent offline alerts

Three additions, all of them additive: a table recording which alerts have
already gone out, a stamp marking session end times the server inferred rather
than the agent asserted, and a per-person switch for the mail.

Revision ID: b71d4e9c30aa
Revises: 4a41375b9fde
Create Date: 2026-08-27 12:10:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b71d4e9c30aa'
down_revision: Union[str, None] = '4a41375b9fde'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'agent_alerts',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('user_id', sa.UUID(), nullable=False),
        sa.Column('kind', sa.String(length=24), nullable=False),
        sa.Column('dedupe_key', sa.String(length=160), nullable=False),
        sa.Column('sent_at', sa.DateTime(timezone=True),
                  server_default=sa.text('now()'), nullable=False),
        sa.Column('recipient', sa.String(length=255), nullable=False),
        sa.CheckConstraint("kind IN ('session_dropped', 'device_dormant')",
                           name='ck_agent_alerts_kind'),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('user_id', 'kind', 'dedupe_key',
                            name='uq_agent_alerts_dedupe'),
    )
    op.create_index('ix_agent_alerts_user_sent', 'agent_alerts',
                    ['user_id', 'sent_at'], unique=False)

    op.add_column('sessions',
                  sa.Column('orphaned_at', sa.DateTime(timezone=True), nullable=True))

    # Defaulted true, then the server default dropped: existing people get the
    # alerts without anyone editing rows, while the column keeps its NOT NULL
    # without a database-side default the model would have to mirror.
    op.add_column('user_settings',
                  sa.Column('offline_alerts_enabled', sa.Boolean(), nullable=False,
                            server_default=sa.true()))
    op.alter_column('user_settings', 'offline_alerts_enabled', server_default=None)


def downgrade() -> None:
    op.drop_column('user_settings', 'offline_alerts_enabled')
    op.drop_column('sessions', 'orphaned_at')
    op.drop_index('ix_agent_alerts_user_sent', table_name='agent_alerts')
    op.drop_table('agent_alerts')
