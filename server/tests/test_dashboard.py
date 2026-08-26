"""Dashboard views.

The interesting question is not "does the page render" but "whose data does it
render, and in whose timezone".
"""
import uuid
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from app.models import Session
from app.services.users import create_user

UTC = timezone.utc
NAIROBI = ZoneInfo('Africa/Nairobi')


@pytest.fixture
def worker(db, password):
    return create_user(db, 'worker@example.com', 'Worker', password)


@pytest.fixture
def boss(db, password):
    return create_user(db, 'boss@example.com', 'Boss', password, role='admin')


def sign_in(client, email, password):
    return client.post('/login', data={'email': email, 'password': password})


def add_session(db, user, hours_ago=3, length_hours=2, project='Alpha'):
    start = datetime.now(UTC) - timedelta(hours=hours_ago)
    db.add(Session(user_id=user.id, client_uuid=uuid.uuid4(), project=project,
                   started_at=start, ended_at=start + timedelta(hours=length_hours)))
    db.commit()


# ── A worker sees themselves ─────────────────────────────────────────────────

def test_the_dashboard_shows_your_own_time(client, db, worker, password):
    add_session(db, worker, project='Alpha')
    sign_in(client, 'worker@example.com', password)
    body = client.get('/').data
    assert b'Alpha' in body and b'2h 00m' in body


def test_it_names_the_timezone_the_day_is_measured_in(client, db, worker, password):
    sign_in(client, 'worker@example.com', password)
    assert b'Africa/Nairobi' in client.get('/').data


def test_history_lists_your_sessions(client, db, worker, password):
    add_session(db, worker, project='Distinctive')
    sign_in(client, 'worker@example.com', password)
    assert b'Distinctive' in client.get('/history').data


def test_the_status_endpoint_reports_tracking(client, db, worker, password):
    start = datetime.now(UTC) - timedelta(hours=1)
    db.add(Session(user_id=worker.id, client_uuid=uuid.uuid4(), project='Live',
                   started_at=start))
    db.commit()
    sign_in(client, 'worker@example.com', password)
    body = client.get('/api/status').get_json()
    assert body['is_tracking'] and body['project'] == 'Live'
    assert 3500 < body['elapsed_seconds'] < 3700


# ── A worker cannot see anyone else ──────────────────────────────────────────

def test_a_worker_asking_for_another_user_gets_their_own_data(
        client, db, worker, boss, password):
    """Silently, not with a 403 — an error would confirm the other account
    exists, and there is nothing useful to tell them."""
    add_session(db, boss, project='BossSecret')
    add_session(db, worker, project='MyOwnWork')
    sign_in(client, 'worker@example.com', password)
    body = client.get(f'/?user={boss.id}').data
    assert b'BossSecret' not in body
    assert b'MyOwnWork' in body


def test_a_worker_cannot_reach_the_team_page(client, db, worker, password):
    sign_in(client, 'worker@example.com', password)
    assert client.get('/admin/team').status_code == 404


def test_history_is_scoped_the_same_way(client, db, worker, boss, password):
    add_session(db, boss, project='BossSecret')
    sign_in(client, 'worker@example.com', password)
    assert b'BossSecret' not in client.get(f'/history?user={boss.id}').data


# ── An admin can ─────────────────────────────────────────────────────────────

def test_an_admin_sees_the_team(client, db, worker, boss, password):
    add_session(db, worker)
    sign_in(client, 'boss@example.com', password)
    body = client.get('/admin/team').data
    assert b'Worker' in body and b'worker@example.com' in body


def test_an_admin_can_open_someones_dashboard(client, db, worker, boss, password):
    add_session(db, worker, project='WorkerProject')
    sign_in(client, 'boss@example.com', password)
    body = client.get(f'/?user={worker.id}').data
    assert b'WorkerProject' in body and b'Viewing' in body


def test_an_admin_sees_that_persons_timezone_not_their_own(
        client, db, boss, password):
    """When Benson's Tuesday started is a fact about Benson."""
    londoner = create_user(db, 'ldn@example.com', 'Londoner', password,
                           timezone_name='Europe/London')
    sign_in(client, 'boss@example.com', password)
    assert b'Europe/London' in client.get(f'/?user={londoner.id}').data


def test_an_unknown_user_id_is_a_404_for_an_admin(client, db, boss, password):
    sign_in(client, 'boss@example.com', password)
    assert client.get(f'/?user={uuid.uuid4()}').status_code == 404


def test_a_malformed_user_id_does_not_error(client, db, worker, boss, password):
    sign_in(client, 'boss@example.com', password)
    assert client.get('/?user=not-a-uuid').status_code == 404


# ── Signed out ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize('path', ['/', '/history', '/api/status', '/admin/team'])
def test_every_page_needs_a_session(client, path):
    r = client.get(path)
    assert r.status_code in (302, 404)
    if r.status_code == 302:
        assert '/login' in r.headers['Location']


def test_an_anonymous_visitor_to_the_admin_page_is_sent_to_login(client):
    """Not a bare 401 — behaving differently here marks the URL as special
    before anyone has signed in. The 404 rule is for signed-in workers."""
    r = client.get('/admin/team')
    assert r.status_code == 302 and '/login' in r.headers['Location']
