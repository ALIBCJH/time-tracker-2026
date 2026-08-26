"""Capping sessions whose agent died.

The failure being prevented: a process is killed mid-session, the row stays
open, and every summary counts it as "running until now" — crediting the whole
absence, potentially days, as work.
"""
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.models import Session
from app.services.sessions import SILENCE_BEFORE_ORPHANED, close_orphaned_sessions
from app.services.users import create_user

UTC = timezone.utc
NOW = datetime(2026, 8, 26, 18, 0, tzinfo=UTC)


@pytest.fixture
def user(db, password):
    return create_user(db, 'a@example.com', 'A', password)


def open_session(db, user, started, beat=None, project='Alpha'):
    s = Session(user_id=user.id, client_uuid=uuid.uuid4(), project=project,
                started_at=started, last_heartbeat_at=beat)
    db.add(s)
    db.commit()
    return s


def test_a_silent_session_is_closed(db, user):
    s = open_session(db, user, NOW - timedelta(hours=6),
                     beat=NOW - timedelta(hours=5))
    closed = close_orphaned_sessions(db, now=NOW)
    db.expire_all()
    assert len(closed) == 1
    assert db.get(Session, s.id).ended_at == NOW - timedelta(hours=5)


def test_it_is_capped_at_the_last_heartbeat_not_at_now(db, user):
    """The five hours between the crash and the cleanup are not work. Capping
    at now is exactly the bug this exists to prevent."""
    open_session(db, user, NOW - timedelta(hours=6), beat=NOW - timedelta(hours=5))
    closed = close_orphaned_sessions(db, now=NOW)
    assert closed[0]['ended_at'] == NOW - timedelta(hours=5)
    assert closed[0]['ended_at'] != NOW


def test_a_live_session_is_left_alone(db, user):
    """A recent heartbeat means someone is working right now."""
    s = open_session(db, user, NOW - timedelta(hours=2), beat=NOW - timedelta(minutes=1))
    assert close_orphaned_sessions(db, now=NOW) == []
    db.expire_all()
    assert db.get(Session, s.id).ended_at is None


def test_a_brief_silence_is_tolerated(db, user):
    """A slept laptop or a blinked network must not close a live session."""
    open_session(db, user, NOW - timedelta(hours=1),
                 beat=NOW - SILENCE_BEFORE_ORPHANED + timedelta(minutes=1))
    assert close_orphaned_sessions(db, now=NOW) == []


def test_a_session_that_never_beat_is_capped_at_its_start(db, user):
    """The agent died immediately after opening it; the only defensible end is
    where it began."""
    started = NOW - timedelta(hours=8)
    open_session(db, user, started, beat=None)
    closed = close_orphaned_sessions(db, now=NOW)
    assert closed[0]['ended_at'] == started


def test_an_already_closed_session_is_untouched(db, user):
    s = open_session(db, user, NOW - timedelta(hours=6), beat=NOW - timedelta(hours=5))
    s.ended_at = NOW - timedelta(hours=4)
    db.commit()
    assert close_orphaned_sessions(db, now=NOW) == []


def test_each_user_is_handled_independently(db, user, password):
    other = create_user(db, 'b@example.com', 'B', password)
    open_session(db, user, NOW - timedelta(hours=6), beat=NOW - timedelta(hours=5))
    open_session(db, other, NOW - timedelta(hours=2), beat=NOW - timedelta(minutes=2))
    closed = close_orphaned_sessions(db, now=NOW)
    assert len(closed) == 1 and closed[0]['user_id'] == user.id


def test_it_can_be_scoped_to_one_user(db, user, password):
    other = create_user(db, 'b@example.com', 'B', password)
    open_session(db, user, NOW - timedelta(hours=6), beat=NOW - timedelta(hours=5))
    open_session(db, other, NOW - timedelta(hours=6), beat=NOW - timedelta(hours=5))
    assert len(close_orphaned_sessions(db, now=NOW, user_id=user.id)) == 1


def test_capping_frees_the_slot_for_a_new_session(db, user):
    """The partial unique index allows one open session; a capped one must not
    keep blocking the next."""
    open_session(db, user, NOW - timedelta(hours=6), beat=NOW - timedelta(hours=5))
    close_orphaned_sessions(db, now=NOW)
    open_session(db, user, NOW, beat=NOW, project='Beta')   # must not raise
    assert db.query(Session).filter(Session.ended_at.is_(None)).count() == 1


def test_the_agent_can_reopen_what_the_server_capped(db, user, password):
    """This is a fallback, not a verdict. If the agent comes back and says the
    session is still open, it knows and the server only guessed."""
    from app.services.ingest import ingest_batch

    s = open_session(db, user, NOW - timedelta(hours=6), beat=NOW - timedelta(hours=5))
    cu = s.client_uuid
    close_orphaned_sessions(db, now=NOW)
    db.expire_all()
    assert db.get(Session, s.id).ended_at is not None

    ingest_batch(db, user, {'sessions': [{
        'client_uuid': str(cu), 'project': 'Alpha',
        'started_at': (NOW - timedelta(hours=6)).isoformat(),
        'ended_at': None,
        'last_heartbeat_at': NOW.isoformat()}]}, now=NOW)
    db.expire_all()
    assert db.get(Session, s.id).ended_at is None
