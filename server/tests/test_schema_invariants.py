"""The invariants that used to live in Python, and leaked.

Each test here corresponds to a bug the local app actually hit: duplicate open
sessions (July 2026, the timer visibly jumped), and a duplicate report send
(today, a restart with no state file mailed a backdated report). Both are now
the database's job.
"""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy.exc import IntegrityError

from app.models import ActivityLog, ReportSend, Session as WorkSession

UTC = timezone.utc
T0 = datetime(2026, 8, 26, 9, 0, tzinfo=UTC)


# ── One open session per user ────────────────────────────────────────────────

def test_a_user_cannot_have_two_open_sessions(db, make_user):
    u = make_user()
    db.add(WorkSession(user_id=u.id, project='A', started_at=T0))
    db.commit()
    db.add(WorkSession(user_id=u.id, project='B', started_at=T0 + timedelta(minutes=1)))
    with pytest.raises(IntegrityError):
        db.commit()


def test_a_closed_session_frees_the_slot(db, make_user):
    u = make_user()
    first = WorkSession(user_id=u.id, project='A', started_at=T0)
    db.add(first)
    db.commit()
    first.ended_at = T0 + timedelta(hours=1)
    db.commit()
    db.add(WorkSession(user_id=u.id, project='B', started_at=T0 + timedelta(hours=1)))
    db.commit()          # must not raise
    assert db.query(WorkSession).count() == 2


def test_two_users_may_each_have_an_open_session(db, make_user):
    """The constraint is per person — it must not serialise the whole team."""
    a, b = make_user(), make_user()
    db.add_all([WorkSession(user_id=a.id, project='A', started_at=T0),
                WorkSession(user_id=b.id, project='B', started_at=T0)])
    db.commit()
    assert db.query(WorkSession).count() == 2


def test_a_session_cannot_end_before_it_started(db, make_user):
    u = make_user()
    db.add(WorkSession(user_id=u.id, project='A', started_at=T0,
                       ended_at=T0 - timedelta(minutes=5)))
    with pytest.raises(IntegrityError):
        db.commit()


# ── One report per user per period ───────────────────────────────────────────

def test_the_same_report_cannot_be_sent_twice(db, make_user):
    """The guard that a missing JSON state file defeated this morning."""
    u = make_user()
    db.add(ReportSend(user_id=u.id, kind='monthly', period_key='2026-08',
                      recipient='a@example.com'))
    db.commit()
    db.add(ReportSend(user_id=u.id, kind='monthly', period_key='2026-08',
                      recipient='a@example.com'))
    with pytest.raises(IntegrityError):
        db.commit()


def test_weekly_and_monthly_are_independent(db, make_user):
    u = make_user()
    db.add_all([ReportSend(user_id=u.id, kind='weekly', period_key='2026-W35',
                           recipient='a@example.com'),
                ReportSend(user_id=u.id, kind='monthly', period_key='2026-08',
                           recipient='a@example.com')])
    db.commit()
    assert db.query(ReportSend).count() == 2


def test_each_user_gets_their_own_send(db, make_user):
    a, b = make_user(), make_user()
    db.add_all([ReportSend(user_id=a.id, kind='monthly', period_key='2026-08',
                           recipient='a@example.com'),
                ReportSend(user_id=b.id, kind='monthly', period_key='2026-08',
                           recipient='b@example.com')])
    db.commit()
    assert db.query(ReportSend).count() == 2


# ── Activity logs are per user per day ───────────────────────────────────────

def test_two_people_can_log_the_same_day(db, make_user):
    """The local schema's UNIQUE(log_date) would have made this impossible."""
    a, b = make_user(), make_user()
    db.add_all([ActivityLog(user_id=a.id, log_date=T0.date()),
                ActivityLog(user_id=b.id, log_date=T0.date())])
    db.commit()
    assert db.query(ActivityLog).count() == 2


def test_one_person_cannot_log_the_same_day_twice(db, make_user):
    u = make_user()
    db.add(ActivityLog(user_id=u.id, log_date=T0.date()))
    db.commit()
    db.add(ActivityLog(user_id=u.id, log_date=T0.date()))
    with pytest.raises(IntegrityError):
        db.commit()


def test_an_unknown_status_is_rejected(db, make_user):
    u = make_user()
    db.add(ActivityLog(user_id=u.id, log_date=T0.date(), status='maybe'))
    with pytest.raises(IntegrityError):
        db.commit()


# ── Timezone correctness ─────────────────────────────────────────────────────

def test_instants_survive_a_round_trip_across_zones(db, make_user):
    """Stored as UTC, read back as the same instant however it was written.

    The local app used naive datetime.now() everywhere, which silently meant
    'whatever zone this process runs in' — the bug that appears the day the
    server runs in UTC and a Nairobi user's week shifts by three hours.
    """
    u = make_user()
    nairobi = datetime(2026, 8, 26, 2, 30, tzinfo=ZoneInfo('Africa/Nairobi'))
    db.add(WorkSession(user_id=u.id, project='A', started_at=nairobi))
    db.commit()
    db.expire_all()

    got = db.query(WorkSession).one().started_at
    assert got == nairobi                                  # same instant
    assert got.astimezone(UTC).hour == 23                  # 02:30 EAT = 23:30 UTC
    assert got.astimezone(UTC).date().day == 25            # ...the previous day
    assert got.astimezone(ZoneInfo('Africa/Nairobi')).day == 26


def test_deleting_a_user_removes_their_data(db, make_user):
    """A person must be able to be removed completely — GDPR/DPA erasure."""
    u = make_user()
    db.add_all([WorkSession(user_id=u.id, project='A', started_at=T0),
                ActivityLog(user_id=u.id, log_date=T0.date())])
    db.commit()
    db.delete(u)
    db.commit()
    assert db.query(WorkSession).count() == 0
    assert db.query(ActivityLog).count() == 0
