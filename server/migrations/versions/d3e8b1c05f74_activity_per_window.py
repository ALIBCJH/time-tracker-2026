"""activity per window

Credited time measures presence: a session open and not idle. One keystroke
every fourteen minutes holds the idle counter below its threshold all day, so
presence can be manufactured for the price of about thirty keystrokes.

This is the second number. Per ten-minute slice, how many of its minutes had any
input at all — around 60-70% for a real morning, under 10% for a tapped keyboard.
It is reported, never subtracted: reading and calls are work at 0%.

Revision ID: d3e8b1c05f74
Revises: c94a2f1e7b30
Create Date: 2026-08-27 16:20:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd3e8b1c05f74'
down_revision: Union[str, None] = 'c94a2f1e7b30'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'activity_windows',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('user_id', sa.UUID(), nullable=False),
        sa.Column('client_uuid', sa.UUID(), nullable=False),
        sa.Column('session_id', sa.BigInteger(), nullable=True),
        sa.Column('started_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('ended_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('active_minutes', sa.Integer(), nullable=False),
        sa.Column('tracked_minutes', sa.Integer(), nullable=False),
        sa.CheckConstraint('active_minutes >= 0 AND tracked_minutes >= 0',
                           name='ck_activity_non_negative'),
        sa.CheckConstraint('active_minutes <= tracked_minutes',
                           name='ck_activity_within_tracked'),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['session_id'], ['sessions.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('user_id', 'client_uuid', name='uq_activity_client_uuid'),
    )
    op.create_index('ix_activity_user_started', 'activity_windows',
                    ['user_id', 'started_at'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_activity_user_started', table_name='activity_windows')
    op.drop_table('activity_windows')
