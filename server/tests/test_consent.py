"""Consent and the pause control.

This is the shortest module in the system and the reason the rest of it is
defensible. An admin watching two colleagues' screens on a timer is workplace
monitoring; the difference between a tool a team accepts and one they resent is
almost entirely here.
"""
import io
import uuid
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from app.models import Consent, Screenshot
from app.services import consent as C
from app.services.users import create_user, issue_device_token

UTC = timezone.utc
NBO = ZoneInfo('Africa/Nairobi')
NOW = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)


@pytest.fixture
def user(db, password):
    return create_user(db, 'a@example.com', 'A', password,
                       timezone_name='Africa/Nairobi')


def sign_in(client, email, password):
    return client.post('/login', data={'email': email, 'password': password})


def accept(client):
    return client.post('/consent')


# ── The gate ─────────────────────────────────────────────────────────────────

def test_nothing_is_shown_before_the_policy_is_accepted(client, db, user, password):
    sign_in(client, 'a@example.com', password)
    r = client.get('/')
    assert r.status_code == 302 and '/consent' in r.headers['Location']


def test_the_policy_itself_is_reachable_without_accepting_it(client, db, user, password):
    """Someone must be able to read what is collected — and leave — without
    first agreeing to it."""
    sign_in(client, 'a@example.com', password)
    body = client.get('/consent').data
    assert b'What this records' in body and b'Screen captures' in body


@pytest.mark.parametrize('path', ['/', '/history', '/screenshots', '/log', '/settings'])
def test_every_page_is_gated(client, db, user, password, path):
    sign_in(client, 'a@example.com', password)
    assert '/consent' in client.get(path).headers.get('Location', '')


def test_accepting_opens_the_dashboard(client, db, user, password):
    sign_in(client, 'a@example.com', password)
    accept(client)
    assert client.get('/').status_code == 200


def test_the_agent_is_not_gated(client, db, user, password):
    """An agent already installed must keep uploading rather than silently
    losing a day while somebody reads a page."""
    _, token = issue_device_token(db, user, 'laptop')
    r = client.post('/api/agent/heartbeat',
                    headers={'Authorization': f'Bearer {token}'})
    assert r.status_code == 200


# ── The record ───────────────────────────────────────────────────────────────

def test_acceptance_is_recorded_with_its_version(db, user):
    C.record(db, user, now=NOW)
    entry = db.query(Consent).one()
    assert entry.policy_version == C.POLICY_VERSION
    assert entry.accepted_at == NOW


def test_accepting_twice_records_once(db, user):
    C.record(db, user, now=NOW)
    C.record(db, user, now=NOW + timedelta(days=1))
    assert db.query(Consent).count() == 1


def test_a_new_policy_version_must_be_accepted_again(db, user):
    """Changing what is collected requires asking again rather than silently
    inheriting an agreement to something else."""
    C.record(db, user, version='2026-08-1', now=NOW)
    assert C.has_consented(db, user, '2026-08-1')
    assert not C.has_consented(db, user, '2027-01-1')


def test_the_record_survives_as_a_log_not_a_flag(db, user):
    """The question asked later is "what were they told, and when" — a boolean
    cannot answer it."""
    C.record(db, user, version='2026-08-1', now=NOW)
    C.record(db, user, version='2027-01-1', now=NOW + timedelta(days=200))
    assert len(C.history(db, user)) == 2


def test_deleting_a_person_removes_their_consent_records(db, user):
    C.record(db, user, now=NOW)
    db.delete(user)
    db.commit()
    assert db.query(Consent).count() == 0


# ── Pausing ──────────────────────────────────────────────────────────────────

def test_pausing_stops_tracking(db, user):
    C.pause(db, user, minutes=15, now=NOW)
    assert C.is_paused(user, NOW)


def test_a_pause_expires_on_its_own(db, user):
    C.pause(db, user, minutes=15, now=NOW)
    assert not C.is_paused(user, NOW + timedelta(minutes=16))


def test_rest_of_the_day_means_the_users_local_midnight(db, user):
    """Not the server's. 15:00 Nairobi ends at 21:00 UTC, not at midnight UTC."""
    until = C.pause(db, user, minutes=None, now=datetime(2026, 8, 26, 12, tzinfo=UTC))
    assert until == datetime(2026, 8, 26, 21, 0, tzinfo=UTC)


def test_an_indefinite_pause_does_not_quietly_expire(db, user):
    C.pause_indefinitely(db, user, now=NOW)
    assert C.is_paused(user, NOW + timedelta(days=365))


def test_resuming_clears_it(db, user):
    C.pause(db, user, minutes=60, now=NOW)
    C.resume(db, user)
    assert not C.is_paused(user, NOW)
    assert user.settings.pause_reason is None


# ── The pause is enforced on the server ──────────────────────────────────────

def test_a_paused_account_refuses_uploads(client, db, user):
    """Not enforced by asking the agent nicely. Someone who pauses should not
    have to trust a program on their machine to honour it."""
    C.pause_indefinitely(db, user)
    _, token = issue_device_token(db, user, 'laptop')
    r = client.post('/api/agent/screenshot',
                    data={'client_uuid': str(uuid.uuid4()),
                          'captured_at': NOW.isoformat(),
                          'full': (io.BytesIO(b'RIFF----WEBP'), 'f.webp')},
                    content_type='multipart/form-data',
                    headers={'Authorization': f'Bearer {token}'})
    assert r.status_code == 409
    assert db.query(Screenshot).count() == 0


def test_the_agent_is_told_it_is_paused(client, db, user):
    C.pause_indefinitely(db, user)
    _, token = issue_device_token(db, user, 'laptop')
    body = client.get('/api/agent/me',
                      headers={'Authorization': f'Bearer {token}'}).get_json()
    assert body['paused'] is True
    # Reported as off too, so an agent that only reads this much still stops.
    assert body['settings']['screenshots_enabled'] is False
    assert body['settings']['tracking_enabled'] is False


def test_an_unpaused_agent_is_told_so(client, db, user):
    _, token = issue_device_token(db, user, 'laptop')
    body = client.get('/api/agent/me',
                      headers={'Authorization': f'Bearer {token}'}).get_json()
    assert body['paused'] is False and body['settings']['tracking_enabled'] is True


# ── The switch belongs to the person being recorded ──────────────────────────

def test_an_admin_cannot_pause_someone_else(client, db, user, password):
    """A switch someone else can flip is not a control. There is no ?user= on
    the pause route at all."""
    create_user(db, 'boss@example.com', 'Boss', password, role='admin')
    sign_in(client, 'boss@example.com', password)
    accept(client)
    client.post('/pause', data={'action': 'indefinite', 'user': str(user.id)})
    db.expire_all()
    assert user.settings.tracking_paused_until is None


def test_an_admin_cannot_resume_someone_else(client, db, user, password):
    C.pause_indefinitely(db, user)
    create_user(db, 'boss@example.com', 'Boss', password, role='admin')
    sign_in(client, 'boss@example.com', password)
    accept(client)
    client.post('/pause', data={'action': 'resume', 'user': str(user.id)})
    db.expire_all()
    assert C.is_paused(user)


def test_pausing_from_the_page_works(client, db, user, password):
    sign_in(client, 'a@example.com', password)
    accept(client)
    client.post('/pause', data={'minutes': '15', 'reason': 'call'})
    db.expire_all()
    assert C.is_paused(user) and user.settings.pause_reason == 'call'


# ── Settings ─────────────────────────────────────────────────────────────────

def test_settings_can_be_changed(client, db, user, password):
    sign_in(client, 'a@example.com', password)
    accept(client)
    client.post('/settings', data={
        'timezone': 'Europe/London', 'idle_threshold_minutes': '5',
        'day_goal_hours': '6', 'week_goal_hours': '30',
        'screenshot_interval_minutes': '20', 'screenshots_enabled': 'on',
        'private_labels': 'whatsapp\nbudget',
        'streams': 'Content: content-evangelism, vercel\nTracker: timetracker',
        'catch_all_stream': 'Everything else'})
    db.expire_all()
    s = user.settings
    assert s.timezone == 'Europe/London' and s.idle_threshold_seconds == 300
    assert s.private_labels == ['whatsapp', 'budget']
    assert s.streams == [['Content', ['content-evangelism', 'vercel']],
                         ['Tracker', ['timetracker']]]
    assert s.catch_all_stream == 'Everything else'


def test_an_invalid_timezone_is_ignored_not_saved(client, db, user, password):
    """A bad zone here would break every date the person sees."""
    sign_in(client, 'a@example.com', password)
    accept(client)
    client.post('/settings', data={'timezone': 'Mars/Olympus'})
    db.expire_all()
    assert user.settings.timezone == 'Africa/Nairobi'


def test_an_absurd_goal_is_ignored(client, db, user, password):
    sign_in(client, 'a@example.com', password)
    accept(client)
    client.post('/settings', data={'day_goal_hours': '900'})
    db.expire_all()
    assert user.settings.day_goal_seconds == 8 * 3600


def test_unchecking_screenshots_turns_them_off(client, db, user, password):
    sign_in(client, 'a@example.com', password)
    accept(client)
    client.post('/settings', data={'timezone': 'Africa/Nairobi'})   # no checkbox
    db.expire_all()
    assert user.settings.screenshots_enabled is False
