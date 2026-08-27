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


def close_sessions_paused_overnight(db, users, now=None):
    """End a session nobody came back to before their day rolled over.

    A pause waits indefinitely, which is the right rule while somebody might
    walk back in and is the wrong one for ever: a laptop left running would
    keep Monday's session open on Wednesday. The numbers would stay correct —
    the idle is subtracted either way — but "session" would stop meaning
    anything, and a history of one item three days long is not a history.

    The test is deliberately not a guess about the person. It asks whether the
    pause began before the local day they are now in, using the same day
    boundary every report already uses. Somebody genuinely working at half past
    midnight is not paused, so nothing here touches them; their session runs on
    and the reports split it at the boundary as they always have.

    The session ends where input stopped, never at the boundary and never at
    now. The hours in between were already excluded as idle, and moving the end
    forward would either credit them or leave a gap nothing accounts for.
    """
    now = now or datetime.now(timezone.utc)
    closed = []

    for user in users:
        # Imported here rather than at module scope: reporting imports models,
        # and a top-level import each way is a cycle.
        from app.services.reporting import day_window, logical_today

        day_start, _ = day_window(user, logical_today(user, now))
        sessions = (db.query(Session)
                    .filter(Session.user_id == user.id,
                            Session.ended_at.is_(None),
                            Session.idle_since.isnot(None),
                            Session.idle_since < day_start)
                    .all())
        for session in sessions:
            session.ended_at = session.idle_since
            closed.append({
                'id': session.id,
                'user_id': session.user_id,
                'project': session.project,
                'ended_at': session.idle_since,
            })
            logger.info(
                f"Closed #{session.id} ('{session.project}') at "
                f"{session.idle_since.isoformat(timespec='seconds')} — paused "
                f"since before {user.email}'s day began")

    if closed:
        db.commit()
    return closed
