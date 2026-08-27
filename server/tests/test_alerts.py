"""Telling someone their own tracking stopped working.

Two failures are being guarded against at once, pulling in opposite directions.
A tracker that dies silently produces a wrong number nobody questions — so the
alert has to fire. An alert that also fires every Saturday gets filtered within
a fortnight, taking the real ones with it — so it has to stay quiet through
every ordinary silence: evenings, weekends, leave, a paused account.

Most of what follows is the second half.
"""
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.models import AgentAlert, Device, Session
from app.services import alerts as A
from app.services.sessions import close_orphaned_sessions
from app.services.users import create_user

UTC = timezone.utc
NOW = datetime(2026, 8, 27, 12, 0, tzinfo=UTC)


@pytest.fixture
def user(db, password):
    return create_user(db, 'worker@example.com', 'A Worker', password)


@pytest.fixture(autouse=True)
def _app_context(flask_app):
    """Rendering an alert needs an application context, exactly as rendering a
    report does. The worker runs inside one; this is that, for tests."""
    yield


@pytest.fixture
def sent(monkeypatch):
    """Capture mail instead of sending it."""
    box = []
    monkeypatch.setattr(A.mail, 'send',
                        lambda to, subject, html, **kw: box.append((to, subject, html)))
    return box


def device(db, user, name='thinkpad', last_seen=None, revoked=None):
    d = Device(user_id=user.id, name=name, token_hash=uuid.uuid4().hex,
               last_seen_at=last_seen, revoked_at=revoked)
    db.add(d)
    db.commit()
    return d


def dropped(db, user, project='Alpha', started=None, beat=None, now=NOW):
    """A session the server actually had to cap — via the real code path, so
    orphaned_at is set the way production sets it rather than by hand."""
    s = Session(user_id=user.id, client_uuid=uuid.uuid4(), project=project,
                started_at=started or now - timedelta(hours=5),
                last_heartbeat_at=beat or now - timedelta(hours=4))
    db.add(s)
    db.commit()
    close_orphaned_sessions(db, now=now)
    db.expire_all()
    return db.get(Session, s.id)


# ── The session the agent abandoned ──────────────────────────────────────────

def test_capping_a_session_records_that_the_server_did_it(db, user):
    """orphaned_at is the difference between an end somebody asserted and one
    we inferred. Without it there is no way to tell the two apart later."""
    s = dropped(db, user)
    assert s.orphaned_at is not None
    assert s.ended_at == NOW - timedelta(hours=4)


def test_a_normally_closed_session_is_not_an_alert(db, user, sent):
    """The common case by far: the agent ended its own session. Nothing
    inferred, nothing to report."""
    s = Session(user_id=user.id, client_uuid=uuid.uuid4(), project='Alpha',
                started_at=NOW - timedelta(hours=3),
                ended_at=NOW - timedelta(hours=1),
                last_heartbeat_at=NOW - timedelta(hours=1))
    db.add(s)
    db.commit()
    assert A.run_due(db, [user], now=NOW) == []
    assert sent == []


def test_a_dropped_session_alerts_its_owner(db, user, sent):
    dropped(db, user)
    results = A.run_due(db, [user], now=NOW)
    assert [r[3] for r in results] == ['sent']
    assert len(sent) == 1
    to, subject, _ = sent[0]
    assert to == 'worker@example.com'
    assert 'Alpha' in subject


def test_the_mail_says_how_much_went_unrecorded(db, user, sent):
    """The number is the point. "Your session ended" is not actionable; "and
    four hours after that were not recorded" is."""
    dropped(db, user, beat=NOW - timedelta(hours=4))
    A.run_due(db, [user], now=NOW)
    _, _, html = sent[0]
    assert '4h 00m' in html


def test_the_same_session_is_only_ever_reported_once(db, user, sent):
    dropped(db, user)
    A.run_due(db, [user], now=NOW)
    for minute in range(1, 4):
        A.run_due(db, [user], now=NOW + timedelta(minutes=minute))
    assert len(sent) == 1


def test_an_old_drop_is_not_news(db, user, sent):
    """The guard that stops a restored database mailing everybody a history
    lesson the moment it comes up."""
    dropped(db, user, started=NOW - timedelta(days=9), beat=NOW - timedelta(days=8),
            now=NOW - timedelta(days=8) + timedelta(minutes=20))
    assert A.run_due(db, [user], now=NOW) == []
    assert sent == []


# ── The device that stopped reporting ────────────────────────────────────────

def test_a_device_silent_for_days_alerts(db, user, sent):
    device(db, user, last_seen=NOW - timedelta(days=5))
    results = A.run_due(db, [user], now=NOW)
    assert [r[3] for r in results] == ['sent']
    assert 'thinkpad' in sent[0][1]


def test_an_evening_of_silence_is_not_an_alert(db, user, sent):
    """The whole reason the threshold is days. Everyone's agent is silent
    overnight; mailing them about it teaches them to ignore the channel."""
    device(db, user, last_seen=NOW - timedelta(hours=14))
    assert A.run_due(db, [user], now=NOW) == []


def test_a_long_weekend_is_not_an_alert(db, user, sent):
    """Friday evening to Monday morning is roughly 62 hours. The threshold has
    to clear it, or every Monday starts with a false alarm."""
    device(db, user, last_seen=NOW - timedelta(hours=62))
    assert A.run_due(db, [user], now=NOW) == []
    assert sent == []


def test_an_abandoned_device_is_not_news(db, user, sent):
    """Past a month it is an old laptop or somebody who left, not a fault."""
    device(db, user, last_seen=NOW - timedelta(days=200))
    assert A.run_due(db, [user], now=NOW) == []


def test_a_device_that_never_reported_is_a_setup_step_not_a_failure(db, user, sent):
    device(db, user, last_seen=None)
    assert A.run_due(db, [user], now=NOW) == []


def test_a_revoked_device_is_silent_on_purpose(db, user, sent):
    device(db, user, last_seen=NOW - timedelta(days=5),
           revoked=NOW - timedelta(days=5))
    assert A.run_due(db, [user], now=NOW) == []


def test_one_working_device_covers_a_dormant_one_on_the_team_page(db, user):
    """Someone with a desktop and a laptop is reporting fine as long as one of
    them is. Flagging the drawer laptop would be a weekly false alarm."""
    device(db, user, name='drawer-laptop', last_seen=NOW - timedelta(days=9))
    device(db, user, name='desk', last_seen=NOW - timedelta(minutes=2))
    health = A.device_health(db, user, now=NOW)
    assert health['device'].name == 'desk'
    assert health['dormant'] is False


def test_dormancy_rearms_when_the_agent_comes_back(db, user, sent):
    """A device that dies, recovers and dies again is two episodes. Keying on
    the moment it fell silent is what re-arms it, with no flag to reset."""
    d = device(db, user, last_seen=NOW - timedelta(days=5))
    A.run_due(db, [user], now=NOW)
    assert len(sent) == 1

    later = NOW + timedelta(days=20)
    d.last_seen_at = later - timedelta(days=5)   # reported again, then died again
    db.commit()
    A.run_due(db, [user], now=later)
    assert len(sent) == 2


def test_a_device_still_silent_is_not_reported_again(db, user, sent):
    """The other half of re-arming: while it stays down, it stays quiet."""
    device(db, user, last_seen=NOW - timedelta(days=5))
    A.run_due(db, [user], now=NOW)
    A.run_due(db, [user], now=NOW + timedelta(days=1))
    A.run_due(db, [user], now=NOW + timedelta(days=2))
    assert len(sent) == 1


def test_dormancy_is_reported_in_days_not_three_digit_hours(db, user, sent):
    """format_hm renders four days as "103h 00m" — accurate and unreadable.
    Nobody counts in three-digit hours."""
    device(db, user, last_seen=NOW - timedelta(days=4, hours=7))
    A.run_due(db, [user], now=NOW)
    _, _, html = sent[0]
    assert '4 days, 7 hours' in html
    assert '103h' not in html


def test_durations_are_not_pluralised_wrongly():
    assert A.format_days(timedelta(days=29, hours=1)) == '29 days, 1 hour'
    assert A.format_days(timedelta(days=3)) == '3 days'
    assert A.format_days(timedelta(hours=40)) == '40 hours'


# ── Who is told, and who is not ──────────────────────────────────────────────

def test_a_paused_account_is_not_nagged(db, user, sent):
    """A pause is a control the tracked person holds. Mailing them to point out
    that the thing they switched off is off turns it into a nag."""
    from app.services import consent as C
    dropped(db, user)
    C.pause(db, user, minutes=600, now=NOW)
    assert A.run_due(db, [user], now=NOW) == []
    assert sent == []


def test_alerts_resume_once_the_pause_expires(db, user, sent):
    from app.services import consent as C
    dropped(db, user)
    C.pause(db, user, minutes=30, now=NOW)
    assert A.run_due(db, [user], now=NOW) == []
    assert [r[3] for r in A.run_due(db, [user], now=NOW + timedelta(hours=2))] == ['sent']


def test_opting_out_is_separate_from_opting_out_of_reports(db, user, sent):
    """"Do not send me a weekly summary" and "do not tell me my tracker is
    broken" are different requests."""
    dropped(db, user)
    user.settings.reports_enabled = False
    db.commit()
    assert [r[3] for r in A.run_due(db, [user], now=NOW)] == ['sent']

    sent.clear()
    user.settings.offline_alerts_enabled = False
    db.commit()
    device(db, user, last_seen=NOW - timedelta(days=5))
    assert A.run_due(db, [user], now=NOW) == []


def test_an_inactive_user_is_not_mailed(db, user, sent):
    dropped(db, user)
    user.is_active = False
    db.commit()
    assert A.run_due(db, [user], now=NOW) == []


def test_nobody_else_is_told(db, user, sent, password):
    """An admin does not get mail about somebody else's laptop. They have the
    team page; being notified about a colleague's tooling is a different thing
    from being able to look."""
    admin = create_user(db, 'admin@example.com', 'Admin', password, role='admin')
    admin.settings.report_cc = ['admin@example.com']
    db.commit()
    dropped(db, user)
    A.run_due(db, [user, admin], now=NOW)
    assert [to for to, _, _ in sent] == ['worker@example.com']


# ── Running it twice ─────────────────────────────────────────────────────────

def test_two_workers_cannot_both_send(db, user, sent):
    """The claim is taken before the mail goes out, so the loser's INSERT
    violates the unique constraint rather than a second email being sent."""
    s = dropped(db, user)
    key = A.dedupe_key('session_dropped', s)
    assert A.claim(db, user, 'session_dropped', key, user.email) is True
    assert A.claim(db, user, 'session_dropped', key, user.email) is False
    assert A.run_due(db, [user], now=NOW) == []


def test_a_failed_send_gives_the_claim_back(db, user, monkeypatch):
    """Keeping a claim after a bad SMTP minute would turn one outage into a
    permanently missing alert."""
    def boom(*a, **kw):
        raise RuntimeError('smtp is down')
    monkeypatch.setattr(A.mail, 'send', boom)

    dropped(db, user)
    assert [r[3] for r in A.run_due(db, [user], now=NOW)] == ['failed']
    assert db.query(AgentAlert).count() == 0


def test_the_alert_survives_a_retry_after_smtp_recovers(db, user, monkeypatch):
    calls = []

    def boom(*a, **kw):
        raise RuntimeError('smtp is down')
    monkeypatch.setattr(A.mail, 'send', boom)
    dropped(db, user)
    A.run_due(db, [user], now=NOW)

    monkeypatch.setattr(A.mail, 'send',
                        lambda to, subject, html, **kw: calls.append(to))
    assert [r[3] for r in A.run_due(db, [user], now=NOW + timedelta(minutes=10))] == ['sent']
    assert calls == ['worker@example.com']
