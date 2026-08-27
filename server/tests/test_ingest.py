"""Agent upload.

The agent is offline-first, so the properties that matter are not "does it
store a row" but: can a batch be replayed safely, does a bad record cost the
rest of the batch, and can a client lie about how long it worked.
"""
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.db import db_session
from app.models import AppUsage, IdlePeriod, Session
from app.services.ingest import RecordError, ingest_batch, parse_instant
from app.services.users import create_user, issue_device_token

UTC = timezone.utc
T0 = datetime(2026, 8, 26, 9, 0, tzinfo=UTC)


def iso(dt):
    return dt.isoformat()


@pytest.fixture
def user(db, password):
    return create_user(db, 'agent@example.com', 'Agent User', password)


@pytest.fixture
def agent(client, db, user):
    _, token = issue_device_token(db, user, 'laptop')

    def _post(payload):
        return client.post('/api/agent/sync', json=payload,
                           headers={'Authorization': f'Bearer {token}'})
    return _post


def a_session(client_uuid=None, **over):
    record = {'client_uuid': str(client_uuid or uuid.uuid4()), 'project': 'Alpha',
              'task': '', 'started_at': iso(T0)}
    record.update(over)
    return record


def some_usage(client_uuid=None, **over):
    record = {'client_uuid': str(client_uuid or uuid.uuid4()), 'app_name': 'code',
              'window_title': 'main.py', 'started_at': iso(T0),
              'ended_at': iso(T0 + timedelta(minutes=30))}
    record.update(over)
    return record


# ── Timestamps ───────────────────────────────────────────────────────────────

def test_a_naive_timestamp_is_refused():
    """'Assume UTC' is how the local app came to mean 'whatever zone this
    process runs in'. A guess here files someone's evening under the wrong day."""
    with pytest.raises(RecordError, match='UTC offset'):
        parse_instant('2026-08-26T09:00:00', 'started_at')


def test_an_offset_timestamp_is_kept_as_the_same_instant():
    got = parse_instant('2026-08-26T12:00:00+03:00', 'started_at')
    assert got.astimezone(UTC).hour == 9


def test_a_future_timestamp_is_refused():
    future = datetime.now(UTC) + timedelta(hours=2)
    with pytest.raises(RecordError, match='future'):
        parse_instant(iso(future), 'started_at')


def test_small_clock_drift_is_tolerated():
    """Laptop clocks drift and resync; minutes are drift, hours are a wrong clock."""
    soon = datetime.now(UTC) + timedelta(minutes=2)
    assert parse_instant(iso(soon), 'started_at') is not None


# ── Idempotency ──────────────────────────────────────────────────────────────

def test_replaying_a_batch_changes_nothing(agent, db):
    """The property the whole spool depends on: a lost response is harmless."""
    batch = {'sessions': [a_session()], 'app_usage': [some_usage()],
             'idle_periods': []}
    first = agent(batch).get_json()
    second = agent(batch).get_json()

    assert first['accepted']['sessions'] == 1
    assert second['rejected'] == []
    assert db.query(Session).count() == 1
    assert db.query(AppUsage).count() == 1


def test_the_same_work_sent_twice_is_not_counted_twice(agent, db):
    usage = some_usage()
    agent({'app_usage': [usage]})
    agent({'app_usage': [usage]})
    rows = db.query(AppUsage).all()
    assert len(rows) == 1 and rows[0].duration_seconds == 1800


def test_closing_a_session_updates_the_row_it_opened(agent, db):
    cu = uuid.uuid4()
    agent({'sessions': [a_session(cu)]})
    assert db.query(Session).one().ended_at is None

    agent({'sessions': [a_session(cu, ended_at=iso(T0 + timedelta(hours=2)))]})
    db.expire_all()
    row = db.query(Session).one()
    assert db.query(Session).count() == 1
    assert row.ended_at == T0 + timedelta(hours=2)


def test_a_resend_cannot_move_when_a_session_began(agent, db):
    """When work started is settled by the first upload."""
    cu = uuid.uuid4()
    agent({'sessions': [a_session(cu)]})
    agent({'sessions': [a_session(cu, started_at=iso(T0 - timedelta(hours=5)))]})
    db.expire_all()
    assert db.query(Session).one().started_at == T0


# ── The client is not trusted ────────────────────────────────────────────────

def test_duration_is_computed_by_the_server(agent, db):
    """A client that miscounts, or lies, cannot inflate a total."""
    agent({'app_usage': [some_usage(duration_seconds=999999)]})
    assert db.query(AppUsage).one().duration_seconds == 1800


def test_a_batch_cannot_write_against_another_user(agent, db, password):
    """Identity comes from the token; a user_id in the payload is ignored."""
    victim = create_user(db, 'victim@example.com', 'V', password)
    agent({'sessions': [a_session(user_id=str(victim.id))]})
    stored = db.query(Session).one()
    assert stored.user_id != victim.id


def test_oversized_text_is_truncated_not_rejected(agent, db):
    """A pathological window title is a browser tab, not an attack — keep the
    row, keep the time, drop the excess."""
    agent({'app_usage': [some_usage(window_title='x' * 50_000)]})
    assert len(db.query(AppUsage).one().window_title) == 2000


# ── One bad record must not cost the batch ───────────────────────────────────

def test_a_bad_record_is_reported_and_the_rest_commits(agent, db):
    """An agent back from a week offline must not lose the week to one row."""
    result = agent({'app_usage': [
        some_usage(),
        some_usage(started_at='not-a-date'),
        some_usage(),
    ]}).get_json()

    assert result['accepted']['app_usage'] == 2
    assert len(result['rejected']) == 1
    assert result['rejected'][0]['index'] == 1
    assert db.query(AppUsage).count() == 2


def test_reversed_span_is_rejected(agent):
    result = agent({'app_usage': [
        some_usage(started_at=iso(T0), ended_at=iso(T0 - timedelta(hours=1)))]}).get_json()
    assert 'before' in result['rejected'][0]['error']


def test_a_missing_project_is_rejected(agent):
    result = agent({'sessions': [a_session(project='  ')]}).get_json()
    assert result['accepted']['sessions'] == 0
    assert 'project' in result['rejected'][0]['error']


def test_two_open_sessions_are_refused_with_a_usable_message(agent, db):
    """The database invariant surfacing as guidance, not as a 500."""
    result = agent({'sessions': [a_session(), a_session()]}).get_json()
    assert result['accepted']['sessions'] == 1
    assert 'open session' in result['rejected'][0]['error']
    assert db.query(Session).count() == 1


def test_an_oversized_batch_is_refused(agent):
    result = agent({'idle_periods': [
        {'client_uuid': str(uuid.uuid4()), 'started_at': iso(T0), 'ended_at': iso(T0)}
        for _ in range(1001)]}).get_json()
    assert result['accepted']['idle_periods'] == 0
    assert 'at most' in result['rejected'][0]['error']


def test_a_non_object_body_is_refused(client, db, user):
    _, token = issue_device_token(db, user, 'laptop')
    r = client.post('/api/agent/sync', json=[1, 2, 3],
                    headers={'Authorization': f'Bearer {token}'})
    assert r.status_code == 400


# ── Linking usage to its session ─────────────────────────────────────────────

def test_usage_is_attached_to_its_session(agent, db):
    cu = uuid.uuid4()
    agent({'sessions': [a_session(cu)],
           'app_usage': [some_usage(session_client_uuid=str(cu))]})
    session = db.query(Session).one()
    assert db.query(AppUsage).one().session_id == session.id


def test_usage_for_an_unknown_session_is_still_kept(agent, db):
    """The work happened either way — unattributed time still counts."""
    agent({'app_usage': [some_usage(session_client_uuid=str(uuid.uuid4()))]})
    assert db.query(AppUsage).one().session_id is None


def test_a_session_reference_cannot_reach_another_users_session(agent, db, password):
    """Resolution is scoped to the token's user."""
    other = create_user(db, 'other@example.com', 'O', password)
    theirs = Session(user_id=other.id, client_uuid=uuid.uuid4(),
                     project='Theirs', started_at=T0, ended_at=T0)
    db.add(theirs)
    db.commit()
    agent({'app_usage': [some_usage(session_client_uuid=str(theirs.client_uuid))]})
    assert db.query(AppUsage).one().session_id is None


def test_idle_periods_are_stored(agent, db):
    agent({'idle_periods': [{'client_uuid': str(uuid.uuid4()),
                             'started_at': iso(T0),
                             'ended_at': iso(T0 + timedelta(minutes=20))}]})
    assert db.query(IdlePeriod).one().duration_seconds == 1200


def test_sync_needs_a_token(client):
    assert client.post('/api/agent/sync', json={}).status_code == 401


# ── Pausing ──────────────────────────────────────────────────────────────────

def test_a_paused_session_arrives_marked_as_paused(agent, db):
    """The break in progress has no idle_periods row yet, so the mark on the
    session is the only thing that stops the total counting up through it."""
    cu = uuid.uuid4()
    agent({'sessions': [a_session(cu)]})
    agent({'sessions': [a_session(cu, idle_since=iso(T0 + timedelta(hours=2)))]})

    row = db.query(Session).filter_by(client_uuid=cu).one()
    assert row.idle_since == T0 + timedelta(hours=2)
    assert row.ended_at is None          # paused, not finished


def test_resuming_clears_the_mark(agent, db):
    """It goes back to NULL the same way it was set — by the agent resending
    the session. Nothing else may clear it, or a resume would be invented."""
    cu = uuid.uuid4()
    agent({'sessions': [a_session(cu, idle_since=iso(T0 + timedelta(hours=2)))]})
    agent({'sessions': [a_session(cu, idle_since=None)]})

    assert db.query(Session).filter_by(client_uuid=cu).one().idle_since is None


def test_a_session_that_never_paused_has_no_mark(agent, db):
    cu = uuid.uuid4()
    agent({'sessions': [a_session(cu)]})
    assert db.query(Session).filter_by(client_uuid=cu).one().idle_since is None


def test_a_malformed_pause_mark_is_refused(agent, db):
    """Same treatment as every other instant on the record."""
    response = agent({'sessions': [a_session(idle_since='the day before')]})
    assert response.status_code == 200
    assert response.get_json()['rejected']


# ── Activity windows ─────────────────────────────────────────────────────────

def a_window(client_uuid=None, **over):
    record = {'client_uuid': str(client_uuid or uuid.uuid4()),
              'started_at': iso(T0), 'ended_at': iso(T0 + timedelta(minutes=10)),
              'active_minutes': 7, 'tracked_minutes': 10}
    record.update(over)
    return record


def test_an_activity_window_is_stored(agent, db):
    from app.models import ActivityWindow

    agent({'activity_windows': [a_window()]})
    row = db.query(ActivityWindow).one()
    assert (row.active_minutes, row.tracked_minutes) == (7, 10)
    assert row.percent == 70


def test_a_window_claiming_more_active_than_tracked_is_refused(agent, db):
    """The agent computes these on a machine its owner controls. Trusting the
    pair would let an activity figure above 100% into the database."""
    from app.models import ActivityWindow

    response = agent({'activity_windows': [a_window(active_minutes=11)]})
    assert response.get_json()['rejected']
    assert db.query(ActivityWindow).count() == 0


def test_a_negative_count_is_refused(agent, db):
    from app.models import ActivityWindow

    assert agent({'activity_windows': [a_window(active_minutes=-1)]}).get_json()['rejected']
    assert db.query(ActivityWindow).count() == 0


def test_a_non_numeric_count_is_refused(agent, db):
    assert agent({'activity_windows': [a_window(tracked_minutes='lots')]}).get_json()['rejected']


def test_true_is_not_a_number_of_minutes(agent, db):
    """In Python True is an int, so an unguarded check would store it as one
    minute rather than refusing the record."""
    assert agent({'activity_windows': [a_window(active_minutes=True)]}).get_json()['rejected']


def test_an_implausible_count_is_refused(agent, db):
    assert agent({'activity_windows': [a_window(tracked_minutes=999999,
                                                active_minutes=999999)]}).get_json()['rejected']


def test_resending_a_window_does_not_duplicate_it(agent, db):
    from app.models import ActivityWindow

    cu = uuid.uuid4()
    agent({'activity_windows': [a_window(cu)]})
    agent({'activity_windows': [a_window(cu)]})
    assert db.query(ActivityWindow).count() == 1


def test_a_window_is_attached_to_its_session(agent, db):
    from app.models import ActivityWindow, Session

    session_cu = uuid.uuid4()
    agent({'sessions': [a_session(session_cu)],
           'activity_windows': [a_window(session_client_uuid=str(session_cu))]})
    session = db.query(Session).filter_by(client_uuid=session_cu).one()
    assert db.query(ActivityWindow).one().session_id == session.id


def test_a_window_for_an_unknown_session_is_still_kept(agent, db):
    """The minutes happened either way. Unattributed evidence still counts."""
    from app.models import ActivityWindow

    agent({'activity_windows': [a_window(session_client_uuid=str(uuid.uuid4()))]})
    assert db.query(ActivityWindow).one().session_id is None


def test_a_bad_window_does_not_cost_the_rest_of_the_batch(agent, db):
    from app.models import ActivityWindow

    response = agent({'activity_windows': [a_window(active_minutes=11), a_window()]})
    body = response.get_json()
    assert body['accepted']['activity_windows'] == 1
    assert len(body['rejected']) == 1
    assert db.query(ActivityWindow).count() == 1
