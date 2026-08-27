"""idle pauses instead of closing

The behaviour change: reaching the idle threshold used to CLOSE the running
session and open a fresh one on return, which chopped a day on one project into
a dozen fragments. Now it pauses — the same session continues, and the idle time
is excluded from the total instead of ending the session.

A pause waits indefinitely. Deciding on somebody's behalf that they have been
away long enough to have gone home needs working hours nobody is asked for;
stopping deliberately is what the pause control is already for.

That means a session may now contain gaps, so every total has to subtract them
explicitly. Two records carry that: idle_periods rows for idle that finished,
and sessions.idle_since for the stretch still happening.

The threshold itself moves from 10 minutes to 15.

Revision ID: c94a2f1e7b30
Revises: b71d4e9c30aa
Create Date: 2026-08-27 14:05:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c94a2f1e7b30'
down_revision: Union[str, None] = 'b71d4e9c30aa'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

OLD_DEFAULT = 600
NEW_DEFAULT = 900


def upgrade() -> None:
    op.add_column('sessions',
                  sa.Column('idle_since', sa.DateTime(timezone=True), nullable=True))

    # Everyone still sitting on the old ten-minute default moves to fifteen.
    # A value somebody chose deliberately is left alone: this migration is
    # changing a default, and overwriting a deliberate choice with it would be
    # a different and much ruder thing.
    op.execute(sa.text(
        'UPDATE user_settings SET idle_threshold_seconds = :new '
        'WHERE idle_threshold_seconds = :old'
    ).bindparams(new=NEW_DEFAULT, old=OLD_DEFAULT))


def downgrade() -> None:
    op.execute(sa.text(
        'UPDATE user_settings SET idle_threshold_seconds = :old '
        'WHERE idle_threshold_seconds = :new'
    ).bindparams(new=NEW_DEFAULT, old=OLD_DEFAULT))
    op.drop_column('sessions', 'idle_since')
