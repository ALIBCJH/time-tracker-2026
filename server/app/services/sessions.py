"""Session housekeeping the server does on its own.

Almost nothing here overrides the agent. The agent is the authority on its own
sessions — it is the only thing that knows whether someone is still at the
keyboard — and this module exists for the one case the agent cannot handle: it
died. A killed process, a yanked power cable, a reinstalled laptop. The session
it had open will otherwise sit open for ever, and every summary that counts an
open session as "running until now" would credit the whole absence as work.
"""
import logging
from datetime import datetime, timedelta, timezone

from app.models import Session

logger = logging.getLogger('sessions')

# How long an open session may go without a heartbeat before the server assumes
# the agent is gone. The agent beats once a minute, so this is several missed
# beats — long enough not to fire on a laptop that slept briefly or a network
# that blinked, short enough that a dead agent does not inflate the same day's
# total for hours.
SILENCE_BEFORE_ORPHANED = timedelta(minutes=15)


def close_orphaned_sessions(db, now=None, silence=SILENCE_BEFORE_ORPHANED, user_id=None):
    """Cap sessions whose agent has gone silent. Returns what was closed.

    Each is capped at its last heartbeat — the last moment we have positive
    evidence the machine was alive — never at `now`. Capping at now would credit
    every hour between the crash and the cleanup as worked time, which is the
    exact failure this exists to prevent.

    This is a fallback, not a verdict. If the agent comes back and re-asserts
    that the session is still open, its upload wins: it knows, and this only
    ever guessed.
    """
    now = now or datetime.now(timezone.utc)
    cutoff = now - silence

    query = db.query(Session).filter(Session.ended_at.is_(None))
    if user_id is not None:
        query = query.filter(Session.user_id == user_id)

    closed = []
    for session in query.all():
        # No heartbeat at all means the agent never checked in after opening it;
        # the only defensible end is the moment it started.
        alive_until = session.last_heartbeat_at or session.started_at
        if alive_until > cutoff:
            continue

        session.ended_at = alive_until
        # Stamped so this end time is identifiable later as inferred rather
        # than asserted — both for the alert that tells the person, and so a
        # number that came from a guess never passes for one that did not.
        session.orphaned_at = now
        closed.append({
            'id': session.id,
            'user_id': session.user_id,
            'project': session.project,
            'ended_at': alive_until,
            'silent_for': int((now - alive_until).total_seconds()),
        })
        logger.info(
            f"Closed orphaned session #{session.id} ('{session.project}') at "
            f"{alive_until.isoformat(timespec='seconds')} — no heartbeat for "
            f"{int((now - alive_until).total_seconds()) // 60}m")

    if closed:
        db.commit()
    return closed
