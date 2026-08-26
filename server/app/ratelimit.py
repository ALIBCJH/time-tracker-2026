"""Rate limiting, in the database.

In Postgres rather than in memory, because an in-memory counter is per-process:
with four Gunicorn workers an attacker gets four times the allowance, and the
limit silently loosens every time the service is scaled up. It is also lost on
every deploy, which is when someone hammering a login form would most like it
to be.

Two things are limited, for different reasons. Login, because a password is
guessable and the whole point is to make guessing expensive. Agent ingest,
because a looping client can bury the server without meaning any harm at all.
"""
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import Column, DateTime, Integer, String, text

from app.models import Base

logger = logging.getLogger('ratelimit')
UTC = timezone.utc

LOGIN_ATTEMPTS = 10
LOGIN_WINDOW = timedelta(minutes=15)


class RateBucket(Base):
    """One counter per (scope, key) window."""
    __tablename__ = 'rate_buckets'

    scope = Column(String(32), primary_key=True)
    key = Column(String(255), primary_key=True)
    window_start = Column(DateTime(timezone=True), primary_key=True)
    count = Column(Integer, nullable=False, default=0)


def _window(now, length):
    """Fixed windows, not a sliding log. A sliding window is more precise and
    needs a row per attempt; at this scale the precision buys nothing and the
    rows cost real space."""
    seconds = int(length.total_seconds())
    epoch = int(now.timestamp())
    return datetime.fromtimestamp(epoch - epoch % seconds, tz=UTC)


def hit(db, scope, key, limit, window=LOGIN_WINDOW, now=None):
    """Record an attempt. Returns (allowed, remaining).

    The increment is an upsert so two workers counting the same attacker at the
    same instant cannot both read 4 and write 5.
    """
    now = now or datetime.now(UTC)
    start = _window(now, window)
    key = (key or '')[:255]

    db.execute(text("""
        INSERT INTO rate_buckets (scope, key, window_start, count)
        VALUES (:scope, :key, :start, 1)
        ON CONFLICT (scope, key, window_start)
        DO UPDATE SET count = rate_buckets.count + 1
    """), {'scope': scope, 'key': key, 'start': start})
    db.commit()

    count = db.execute(text("""
        SELECT count FROM rate_buckets
        WHERE scope=:scope AND key=:key AND window_start=:start
    """), {'scope': scope, 'key': key, 'start': start}).scalar() or 0

    if count > limit:
        logger.warning(f'Rate limit hit: {scope} {key} ({count}/{limit})')
    return count <= limit, max(0, limit - count)


def clear(db, scope, key, window=LOGIN_WINDOW, now=None):
    """Forget the counter — called after a SUCCESSFUL login, so someone who
    mistypes twice and then gets it right is not left near a lockout."""
    now = now or datetime.now(UTC)
    db.execute(text("""
        DELETE FROM rate_buckets
        WHERE scope=:scope AND key=:key AND window_start=:start
    """), {'scope': scope, 'key': (key or '')[:255],
           'start': _window(now, window)})
    db.commit()


def prune(db, older_than=timedelta(days=1), now=None):
    now = now or datetime.now(UTC)
    db.execute(text('DELETE FROM rate_buckets WHERE window_start < :cutoff'),
               {'cutoff': now - older_than})
    db.commit()
