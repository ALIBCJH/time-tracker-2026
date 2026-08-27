"""Warning somebody before the disk stops Postgres writing.

One volume carries the database, the nightly dumps, Docker's layers and — when
S3 is not configured — every screen capture. It fills from several directions
at once, and the first symptom is Postgres refusing to write, which surfaces as
the application failing in ways that look like anything except a disk.

Nothing was watching it.
"""
from datetime import datetime, timedelta, timezone

import pytest

from app import config
from app.models import AgentAlert
from app.services import alerts as A
from app.services.users import create_user

UTC = timezone.utc
NOW = datetime(2026, 8, 27, 12, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _app_context(flask_app):
    """Rendering the warning needs an application context, as the worker has."""
    yield


@pytest.fixture
def sent(monkeypatch):
    box = []
    monkeypatch.setattr(A.mail, 'send',
                        lambda to, subject, html, **kw: box.append((to, subject, html)))
    return box


@pytest.fixture
def admin(db, password):
    return create_user(db, 'boss@example.com', 'Boss', password, role='admin')


@pytest.fixture
def at_percent(monkeypatch):
    """Pretend the disk is a given fullness, without filling one."""
    def _set(percent, total_gib=20):
        total = total_gib * 2**30
        used = int(total * percent / 100)
        monkeypatch.setattr(A.shutil, 'disk_usage',
                            lambda _p: type('U', (), {'total': total, 'used': used,
                                                      'free': total - used})())
    return _set


# ── When it says something ───────────────────────────────────────────────────

def test_a_healthy_disk_says_nothing(db, admin, sent, at_percent):
    at_percent(40)
    assert A.run_disk_check(db, now=NOW) == []
    assert sent == []


def test_crossing_the_warning_line_tells_the_administrators(db, admin, sent, at_percent):
    at_percent(config.DISK_WARN_PERCENT + 1)
    results = A.run_disk_check(db, now=NOW)
    assert [r[3] for r in results] == ['sent']
    assert sent[0][0] == 'boss@example.com'
    assert 'disk' in sent[0][1].lower()


def test_a_critical_disk_says_so_in_the_subject(db, admin, sent, at_percent):
    """The difference between a message to read this week and one to read now."""
    at_percent(config.DISK_CRITICAL_PERCENT + 2)
    A.run_disk_check(db, now=NOW)
    assert 'act now' in sent[0][1]


def test_the_message_carries_the_number_and_what_to_do(db, admin, sent, at_percent):
    at_percent(88, total_gib=20)
    A.run_disk_check(db, now=NOW)
    _, _, html = sent[0]
    assert '88%' in html
    assert 'docker image prune' in html


# ── How often ────────────────────────────────────────────────────────────────

def test_it_does_not_repeat_itself_through_the_day(db, admin, sent, at_percent):
    at_percent(83)
    for hour in range(6):
        A.run_disk_check(db, now=NOW + timedelta(hours=hour))
    assert len(sent) == 1


def test_it_speaks_again_when_things_get_worse(db, admin, sent, at_percent):
    """Banded to five percent: drifting between 83 and 84 is the same news,
    going from 83 to 91 is not."""
    at_percent(83)
    A.run_disk_check(db, now=NOW)
    at_percent(84)
    A.run_disk_check(db, now=NOW)
    assert len(sent) == 1, 'same band, same day'

    at_percent(91)
    A.run_disk_check(db, now=NOW)
    assert len(sent) == 2, 'worse — worth saying immediately'


def test_it_says_it_again_tomorrow(db, admin, sent, at_percent):
    """A disk that is still full tomorrow is still a problem tomorrow."""
    at_percent(83)
    A.run_disk_check(db, now=NOW)
    A.run_disk_check(db, now=NOW + timedelta(days=1))
    assert len(sent) == 2


# ── Who hears it ─────────────────────────────────────────────────────────────

def test_workers_are_not_told_about_the_server(db, admin, sent, at_percent, password):
    """A full disk is not somebody's own tracking going wrong, and only a person
    with access to the machine can act on it."""
    create_user(db, 'worker@example.com', 'Worker', password)
    at_percent(95)
    A.run_disk_check(db, now=NOW)
    assert [to for to, _, _ in sent] == ['boss@example.com']


def test_an_admin_who_turned_alerts_off_is_not_told(db, admin, sent, at_percent):
    admin.settings.offline_alerts_enabled = False
    db.commit()
    at_percent(95)
    assert A.run_disk_check(db, now=NOW) == []


def test_an_inactive_admin_is_not_told(db, admin, sent, at_percent):
    admin.is_active = False
    db.commit()
    at_percent(95)
    assert A.run_disk_check(db, now=NOW) == []


def test_every_admin_is_told(db, admin, sent, at_percent, password):
    create_user(db, 'boss2@example.com', 'Boss Two', password, role='admin')
    at_percent(95)
    A.run_disk_check(db, now=NOW)
    assert sorted(to for to, _, _ in sent) == ['boss2@example.com', 'boss@example.com']


# ── Running it twice ─────────────────────────────────────────────────────────

def test_two_workers_cannot_both_send(db, admin, sent, at_percent):
    at_percent(95)
    A.run_disk_check(db, now=NOW)
    A.run_disk_check(db, now=NOW)
    assert len(sent) == 1
    assert db.query(AgentAlert).filter_by(kind='disk_space').count() == 1


def test_a_failed_send_is_retried_next_hour(db, admin, at_percent, monkeypatch):
    """A bad SMTP minute must not turn into a warning nobody ever gets."""
    def boom(*a, **kw):
        raise RuntimeError('smtp is down')
    monkeypatch.setattr(A.mail, 'send', boom)

    at_percent(95)
    assert [r[3] for r in A.run_disk_check(db, now=NOW)] == ['failed']
    assert db.query(AgentAlert).filter_by(kind='disk_space').count() == 0

    calls = []
    monkeypatch.setattr(A.mail, 'send',
                        lambda to, subject, html, **kw: calls.append(to))
    assert [r[3] for r in A.run_disk_check(db, now=NOW + timedelta(hours=1))] == ['sent']
    assert calls == ['boss@example.com']
