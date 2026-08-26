"""The daily prompt.

Every rule here is about not being annoying, and each is tested against the
alternative it exists to avoid: a day silently lost, a card answered at midnight
about an evening spent elsewhere, or a day reopened to show an empty box.
"""
import uuid
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from app.models import ActivityLog, AppUsage
from app.services import activity_log as AL
from app.services.users import create_user

UTC = timezone.utc
NBO = ZoneInfo('Africa/Nairobi')


@pytest.fixture
def user(db, password):
    return create_user(db, 'a@example.com', 'A', password,
                       timezone_name='Africa/Nairobi')


def at(y, m, d, hh=0, mm=0):
    """A local Nairobi moment, as the aware instant it really is."""
    return datetime(y, m, d, hh, mm, tzinfo=NBO)


def usage(db, user, day, start_hour, minutes, app='cursor',
          title='main.py - ttcloud - Cursor'):
    start = at(day.year, day.month, day.day, start_hour)
    db.add(AppUsage(user_id=user.id, client_uuid=uuid.uuid4(), app_name=app,
                    window_title=title, started_at=start,
                    ended_at=start + timedelta(minutes=minutes),
                    duration_seconds=minutes * 60))
    db.commit()


DAY = date(2026, 8, 26)


# ── The draft ────────────────────────────────────────────────────────────────

def test_a_draft_describes_the_day(db, user):
    usage(db, user, DAY, 9, 120)
    log = AL.refresh_draft(db, user, DAY, now=at(2026, 8, 26, 22))
    assert log.status == 'draft'
    assert 'ttcloud' in log.headline
    assert log.tracked_seconds == 2 * 3600


def test_refreshing_rewrites_rather_than_accumulates(db, user):
    """It runs repeatedly all evening; one row per attempt would be a mess."""
    usage(db, user, DAY, 9, 60)
    AL.refresh_draft(db, user, DAY, now=at(2026, 8, 26, 22))
    usage(db, user, DAY, 14, 60)
    AL.refresh_draft(db, user, DAY, now=at(2026, 8, 26, 22))
    assert db.query(ActivityLog).count() == 1
    assert db.query(ActivityLog).one().tracked_seconds == 2 * 3600


def test_refreshing_never_touches_your_note(db, user):
    """The note is the only thing here a person wrote."""
    usage(db, user, DAY, 9, 60)
    AL.refresh_draft(db, user, DAY, now=at(2026, 8, 26, 22))
    AL.answer(db, user, DAY, 'Shipped the ingest endpoint.', now=at(2026, 8, 26, 22))
    usage(db, user, DAY, 15, 60)
    log = AL.refresh_draft(db, user, DAY, now=at(2026, 8, 26, 23))
    assert log.note == 'Shipped the ingest endpoint.'
    assert log.tracked_seconds == 2 * 3600


def test_the_draft_uses_the_users_local_day(db, user):
    """01:00 Nairobi is 22:00 UTC the day before."""
    usage(db, user, DAY, 1, 60)
    log = AL.refresh_draft(db, user, DAY, now=at(2026, 8, 26, 22))
    assert log.tracked_seconds == 3600


# ── When it is asked ─────────────────────────────────────────────────────────

def test_today_is_not_asked_before_the_evening_window(db, user):
    usage(db, user, DAY, 9, 120)
    AL.refresh_draft(db, user, DAY, now=at(2026, 8, 26, 14))
    assert AL.pending(db, user, now=at(2026, 8, 26, 14)) == []


def test_today_is_asked_once_the_window_opens(db, user):
    usage(db, user, DAY, 9, 120)
    AL.refresh_draft(db, user, DAY, now=at(2026, 8, 26, 21, 30))
    due = AL.pending(db, user, now=at(2026, 8, 26, 21, 30))
    assert [d['kind'] for d in due] == ['new'] and due[0]['is_today']


def test_a_past_day_is_always_due(db, user):
    """The "waiting for me when I get back" behaviour — a day spent away from
    the machine must not be silently lost."""
    usage(db, user, DAY, 9, 120)
    AL.refresh_draft(db, user, DAY, now=at(2026, 8, 26, 22))
    due = AL.pending(db, user, now=at(2026, 8, 28, 10))
    assert len(due) == 1 and not due[0]['is_today']


def test_an_almost_empty_day_is_never_asked_about(db, user):
    """A weekend where the laptop was opened to check one thing."""
    usage(db, user, DAY, 9, 2)
    AL.refresh_draft(db, user, DAY, now=at(2026, 8, 26, 22))
    assert AL.pending(db, user, now=at(2026, 8, 28, 10)) == []


# ── Interrupting ─────────────────────────────────────────────────────────────

def test_a_card_is_only_active_while_you_are_at_the_machine(db, user):
    """Otherwise it sits on an empty desktop since 21:00 and gets answered at
    midnight about an evening spent elsewhere."""
    usage(db, user, DAY, 9, 120)
    AL.refresh_draft(db, user, DAY, now=at(2026, 8, 26, 22))
    away = AL.pending(db, user, now=at(2026, 8, 26, 22), idle_seconds=3600)
    assert away and not away[0]['active']


def test_a_card_is_active_when_you_are_there(db, user):
    usage(db, user, DAY, 9, 120)
    AL.refresh_draft(db, user, DAY, now=at(2026, 8, 26, 22))
    here = AL.pending(db, user, now=at(2026, 8, 26, 22), idle_seconds=10)
    assert here[0]['active']


def test_tracking_switched_off_suppresses_the_interruption(db, user):
    usage(db, user, DAY, 9, 120)
    AL.refresh_draft(db, user, DAY, now=at(2026, 8, 26, 22))
    due = AL.pending(db, user, now=at(2026, 8, 26, 22), idle_seconds=10,
                     tracking_enabled=False)
    assert due and not due[0]['active']


def test_unknown_presence_reads_as_present(db, user):
    """A CLI or a test has no idea; suppressing everything would be worse."""
    usage(db, user, DAY, 9, 120)
    AL.refresh_draft(db, user, DAY, now=at(2026, 8, 26, 22))
    assert AL.pending(db, user, now=at(2026, 8, 26, 22))[0]['active']


# ── Answering ────────────────────────────────────────────────────────────────

def test_answering_clears_the_queue(db, user):
    usage(db, user, DAY, 9, 120)
    AL.refresh_draft(db, user, DAY, now=at(2026, 8, 26, 22))
    AL.answer(db, user, DAY, 'Built the thing.', now=at(2026, 8, 26, 22))
    assert AL.pending(db, user, now=at(2026, 8, 27, 10)) == []


def test_an_empty_confirm_is_refused(db, user):
    """It would record the day as answered while saying nothing about it —
    worse than leaving it in the queue."""
    usage(db, user, DAY, 9, 120)
    AL.refresh_draft(db, user, DAY, now=at(2026, 8, 26, 22))
    with pytest.raises(ValueError, match='empty'):
        AL.answer(db, user, DAY, '   ', now=at(2026, 8, 26, 22))


def test_a_skipped_day_never_comes_back(db, user):
    """Waving a day away is meant to be final."""
    usage(db, user, DAY, 9, 120)
    AL.refresh_draft(db, user, DAY, now=at(2026, 8, 26, 22))
    AL.answer(db, user, DAY, '', status='skipped', now=at(2026, 8, 26, 22))
    usage(db, user, DAY, 15, 180)
    AL.refresh_draft(db, user, DAY, now=at(2026, 8, 27, 10))
    assert AL.pending(db, user, now=at(2026, 8, 28, 10)) == []


# ── Top-ups ──────────────────────────────────────────────────────────────────

def test_a_day_answered_early_comes_back_once_it_has_grown(db, user):
    usage(db, user, DAY, 9, 120)
    AL.refresh_draft(db, user, DAY, now=at(2026, 8, 26, 21))
    AL.answer(db, user, DAY, 'Morning work.', now=at(2026, 8, 26, 21))

    usage(db, user, DAY, 22, 40)                       # kept going afterwards
    AL.refresh_draft(db, user, DAY, now=at(2026, 8, 27, 9))

    due = AL.pending(db, user, now=at(2026, 8, 27, 10))
    assert [d['kind'] for d in due] == ['topup']
    assert due[0]['undescribed_seconds'] == 40 * 60


def test_a_trivial_tail_does_not_reopen_a_day(db, user):
    """A browser tab left open is not an evening."""
    usage(db, user, DAY, 9, 120)
    AL.refresh_draft(db, user, DAY, now=at(2026, 8, 26, 21))
    AL.answer(db, user, DAY, 'Morning work.', now=at(2026, 8, 26, 21))
    usage(db, user, DAY, 22, 5)
    AL.refresh_draft(db, user, DAY, now=at(2026, 8, 27, 9))
    assert AL.pending(db, user, now=at(2026, 8, 27, 10)) == []


def test_a_topup_is_not_offered_before_the_day_closes(db, user):
    """Asking the same evening interrupts the very work it asks about, and the
    gap would still be growing while you answered."""
    usage(db, user, DAY, 9, 120)
    AL.refresh_draft(db, user, DAY, now=at(2026, 8, 26, 21))
    AL.answer(db, user, DAY, 'Morning work.', now=at(2026, 8, 26, 21))
    usage(db, user, DAY, 21, 40)
    AL.refresh_draft(db, user, DAY, now=at(2026, 8, 26, 23))
    assert AL.pending(db, user, now=at(2026, 8, 26, 23)) == []


def test_leaving_it_as_is_settles_the_day_without_touching_the_note(db, user):
    usage(db, user, DAY, 9, 120)
    AL.refresh_draft(db, user, DAY, now=at(2026, 8, 26, 21))
    AL.answer(db, user, DAY, 'Morning work.', now=at(2026, 8, 26, 21))
    usage(db, user, DAY, 22, 40)
    AL.refresh_draft(db, user, DAY, now=at(2026, 8, 27, 9))

    AL.rebaseline(db, user, DAY)
    assert AL.pending(db, user, now=at(2026, 8, 27, 10)) == []
    assert db.query(ActivityLog).one().note == 'Morning work.'


def test_a_day_settled_before_the_column_existed_is_not_reopened(db, user):
    """NULL answered_seconds reads as finished, so a deployment does not reopen
    history."""
    usage(db, user, DAY, 9, 300)
    log = AL.refresh_draft(db, user, DAY, now=at(2026, 8, 26, 22))
    log.status, log.note, log.answered_seconds = 'confirmed', 'Old note', None
    db.commit()
    assert AL.pending(db, user, now=at(2026, 8, 28, 10)) == []


# ── Isolation and history ────────────────────────────────────────────────────

def test_one_persons_queue_never_shows_anothers_day(db, user, password):
    other = create_user(db, 'b@example.com', 'B', password)
    usage(db, other, DAY, 9, 120)
    AL.refresh_draft(db, other, DAY, now=at(2026, 8, 26, 22))
    assert AL.pending(db, user, now=at(2026, 8, 28, 10)) == []


def test_two_people_can_have_a_log_for_the_same_day(db, user, password):
    other = create_user(db, 'b@example.com', 'B', password)
    usage(db, user, DAY, 9, 60)
    usage(db, other, DAY, 9, 60)
    AL.refresh_draft(db, user, DAY, now=at(2026, 8, 26, 22))
    AL.refresh_draft(db, other, DAY, now=at(2026, 8, 26, 22))
    assert db.query(ActivityLog).count() == 2


def test_history_holds_only_answered_days(db, user):
    usage(db, user, DAY, 9, 120)
    AL.refresh_draft(db, user, DAY, now=at(2026, 8, 26, 22))
    assert AL.history(db, user) == []
    AL.answer(db, user, DAY, 'Built the thing.', now=at(2026, 8, 26, 22))
    got = AL.history(db, user)
    assert len(got) == 1 and got[0]['note'] == 'Built the thing.'


# ── Over the wire ────────────────────────────────────────────────────────────

@pytest.fixture
def agent(client, db, user):
    from app.services.users import issue_device_token
    _, token = issue_device_token(db, user, 'laptop')
    auth = {'Authorization': f'Bearer {token}'}

    class Agent:
        def pending(self, **params):
            return client.get('/api/agent/activity-log/pending',
                              query_string=params, headers=auth)

        def answer(self, payload):
            return client.post('/api/agent/activity-log/answer', json=payload,
                               headers=auth)
    return Agent()


# These go over the wire, so the server uses the real clock. A PAST day is
# always due regardless of the hour; using "today" would make the test pass or
# fail depending on what time of day it is run.
PAST = date(2020, 6, 15)


def test_the_agent_can_fetch_its_queue(db, user, agent):
    usage(db, user, PAST, 9, 120)
    AL.refresh_draft(db, user, PAST, now=at(2020, 6, 15, 22))
    body = agent.pending(idle_seconds=10).get_json()
    assert len(body) == 1 and body[0]['date'] == PAST.isoformat()


def test_the_agent_passes_its_own_idle_counter(db, user, agent):
    """The idle counter lives on the laptop; presence is decided on the server
    so every client gets the same answer."""
    usage(db, user, PAST, 9, 120)
    AL.refresh_draft(db, user, PAST, now=at(2020, 6, 15, 22))
    assert agent.pending(idle_seconds=99999).get_json()[0]['active'] is False


def test_an_unparseable_idle_value_is_treated_as_unknown(db, user, agent):
    usage(db, user, PAST, 9, 120)
    AL.refresh_draft(db, user, PAST, now=at(2020, 6, 15, 22))
    assert agent.pending(idle_seconds='banana').status_code == 200


def test_the_agent_can_answer_a_day(db, user, agent):
    usage(db, user, DAY, 9, 120)
    AL.refresh_draft(db, user, DAY, now=at(2026, 8, 26, 22))
    r = agent.answer({'date': DAY.isoformat(), 'note': 'Shipped it.'})
    assert r.status_code == 200
    assert db.query(ActivityLog).one().note == 'Shipped it.'


@pytest.mark.parametrize('payload, code', [
    ({}, 400),
    ({'date': 'not-a-date'}, 400),
    ({'date': '2026-08-26', 'status': 'maybe'}, 400),
    ({'date': '2026-08-26', 'note': '  '}, 400),
])
def test_bad_answers_are_refused(db, user, agent, payload, code):
    usage(db, user, DAY, 9, 120)
    AL.refresh_draft(db, user, DAY, now=at(2026, 8, 26, 22))
    assert agent.answer(payload).status_code == code


def test_answering_a_day_with_no_log_is_a_404(db, user, agent):
    assert agent.answer({'date': '2020-01-01', 'note': 'x'}).status_code == 404


def test_leave_as_is_over_the_wire(db, user, agent):
    usage(db, user, DAY, 9, 120)
    AL.refresh_draft(db, user, DAY, now=at(2026, 8, 26, 21))
    AL.answer(db, user, DAY, 'Morning work.', now=at(2026, 8, 26, 21))
    usage(db, user, DAY, 22, 40)
    AL.refresh_draft(db, user, DAY, now=at(2026, 8, 27, 9))

    assert agent.answer({'date': DAY.isoformat(), 'status': 'unchanged',
                         'note': ''}).status_code == 200
    assert db.query(ActivityLog).one().note == 'Morning work.'


def test_leave_as_is_needs_an_answered_day(db, user, agent):
    usage(db, user, DAY, 9, 120)
    AL.refresh_draft(db, user, DAY, now=at(2026, 8, 26, 22))
    assert agent.answer({'date': DAY.isoformat(),
                         'status': 'unchanged'}).status_code == 400


def test_the_queue_needs_a_token(client):
    assert client.get('/api/agent/activity-log/pending').status_code == 401


def test_the_log_page_shows_answered_days(client, db, user, password):
    usage(db, user, DAY, 9, 120)
    AL.refresh_draft(db, user, DAY, now=at(2026, 8, 26, 22))
    AL.answer(db, user, DAY, 'A distinctive sentence.', now=at(2026, 8, 26, 22))
    client.post('/login', data={'email': 'a@example.com', 'password': password})
    assert b'A distinctive sentence.' in client.get('/log').data


def test_the_log_page_is_scoped_to_you(client, db, user, password):
    other = create_user(db, 'b@example.com', 'B', password)
    usage(db, other, DAY, 9, 120)
    AL.refresh_draft(db, other, DAY, now=at(2026, 8, 26, 22))
    AL.answer(db, other, DAY, 'SomeoneElsesSecret', now=at(2026, 8, 26, 22))
    client.post('/login', data={'email': 'a@example.com', 'password': password})
    assert b'SomeoneElsesSecret' not in client.get(f'/log?user={other.id}').data
