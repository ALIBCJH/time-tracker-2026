"""A person's whole year, month by month.

The admin view that answers "how has this person's year gone" rather than
"what are they doing right now". Most of what follows is about the ways a year
view can quietly mislead: a month that is empty because nobody worked reading
the same as one that has not happened yet, a total that disagrees with the
weekly report, and one person's months being cut at another person's midnight.
"""
import uuid
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from app.models import ActivityWindow, IdlePeriod, Session
from app.services import reporting as R
from app.services.users import create_user

UTC = timezone.utc
NAIROBI = ZoneInfo('Africa/Nairobi')
# Late in the year, so most months are past and a few are still to come.
NOW = datetime(2026, 8, 27, 12, 0, tzinfo=NAIROBI).astimezone(UTC)


@pytest.fixture
def user(db, password):
    return create_user(db, 'nbo@example.com', 'Nairobi', password,
                       timezone_name='Africa/Nairobi')


def worked(db, user, y, m, d, hours=8, project='Alpha'):
    start = datetime(y, m, d, 9, 0, tzinfo=NAIROBI).astimezone(UTC)
    s = Session(user_id=user.id, client_uuid=uuid.uuid4(), project=project,
                started_at=start, ended_at=start + timedelta(hours=hours))
    db.add(s)
    db.commit()
    return s


def months(db, user, year=2026):
    return {m['month']: m for m in R.monthly_totals(db, user, year, now=NOW)}


# ── The shape of a year ──────────────────────────────────────────────────────

def test_every_month_is_present_even_when_empty(db, user):
    """A year with a hole in it reads as missing data."""
    worked(db, user, 2026, 3, 10)
    rows = R.monthly_totals(db, user, 2026, now=NOW)
    assert len(rows) == 12
    assert [r['month'] for r in rows] == list(range(1, 13))


def test_hours_land_in_the_month_they_were_worked(db, user):
    worked(db, user, 2026, 3, 10, hours=8)
    worked(db, user, 2026, 3, 11, hours=4)
    worked(db, user, 2026, 5, 2, hours=6)

    by_month = months(db, user)
    assert by_month[3]['total_seconds'] == 12 * 3600
    assert by_month[5]['total_seconds'] == 6 * 3600
    assert by_month[4]['total_seconds'] == 0


def test_a_month_that_has_not_happened_is_marked_future(db, user):
    """Distinct from an empty past month. One is "nothing yet", the other is
    "nothing", and a year that stops in August reads as somebody who left."""
    by_month = months(db, user)
    assert by_month[7]['is_future'] is False
    assert by_month[8]['is_current'] is True
    assert by_month[9]['is_future'] is True


def test_days_worked_counts_days_not_sessions(db, user):
    """Two sessions in one day is one day worked. Otherwise anyone who stops
    for lunch looks twice as diligent."""
    worked(db, user, 2026, 3, 10, hours=3)
    worked(db, user, 2026, 3, 10, hours=3)
    worked(db, user, 2026, 3, 11, hours=3)
    assert months(db, user)[3]['worked_days'] == 2


def test_the_average_day_ignores_days_nobody_worked(db, user):
    """Dividing by the days in the month would make a good week in a quiet
    month look like a bad one."""
    worked(db, user, 2026, 3, 10, hours=8)
    worked(db, user, 2026, 3, 11, hours=4)
    assert months(db, user)[3]['average_seconds'] == 6 * 3600


# ── Agreeing with the rest of the app ────────────────────────────────────────

def test_breaks_are_excluded_here_too(db, user):
    """Built on daily_totals rather than its own query, so a lunch break comes
    out the same way it does everywhere else. A second implementation of "how
    long was that" is how two pages start disagreeing."""
    start = datetime(2026, 3, 10, 9, 0, tzinfo=NAIROBI).astimezone(UTC)
    db.add(Session(user_id=user.id, client_uuid=uuid.uuid4(), project='Alpha',
                   started_at=start, ended_at=start + timedelta(hours=8)))
    db.add(IdlePeriod(user_id=user.id, client_uuid=uuid.uuid4(),
                      started_at=start + timedelta(hours=3),
                      ended_at=start + timedelta(hours=4), duration_seconds=3600))
    db.commit()
    assert months(db, user)[3]['total_seconds'] == 7 * 3600


def test_the_year_total_is_the_sum_of_its_months(db, user):
    worked(db, user, 2026, 2, 3)
    worked(db, user, 2026, 6, 4, hours=5)
    summary = R.year_summary(db, user, 2026, now=NOW)
    assert summary['total_seconds'] == sum(m['total_seconds'] for m in summary['months'])
    assert summary['total_seconds'] == 13 * 3600


def test_months_are_cut_at_the_persons_own_midnight(db, user, password):
    """23:30 on the 31st in Nairobi is still the 31st there, and already the
    next month in UTC. An admin elsewhere must see the worker's months."""
    honolulu = create_user(db, 'hi@example.com', 'Honolulu', password,
                           timezone_name='Pacific/Honolulu')
    # 21:00 on 31 March in Honolulu is 07:00 on 1 April UTC.
    start = datetime(2026, 3, 31, 21, 0, tzinfo=ZoneInfo('Pacific/Honolulu'))
    db.add(Session(user_id=honolulu.id, client_uuid=uuid.uuid4(), project='A',
                   started_at=start.astimezone(UTC),
                   ended_at=(start + timedelta(hours=2)).astimezone(UTC)))
    db.commit()

    rows = {m['month']: m for m in R.monthly_totals(db, honolulu, 2026, now=NOW)}
    assert rows[3]['total_seconds'] == 2 * 3600      # March, where they were
    assert rows[4]['total_seconds'] == 0


def test_another_persons_work_is_not_counted(db, user, password):
    other = create_user(db, 'other@example.com', 'Other', password)
    worked(db, user, 2026, 3, 10, hours=8)
    worked(db, other, 2026, 3, 10, hours=8)
    assert months(db, user)[3]['total_seconds'] == 8 * 3600


# ── Activity across the year ─────────────────────────────────────────────────

def test_activity_is_summarised_per_month(db, user):
    start = datetime(2026, 3, 10, 9, 0, tzinfo=NAIROBI).astimezone(UTC)
    for i, active in enumerate([8, 6]):
        db.add(ActivityWindow(user_id=user.id, client_uuid=uuid.uuid4(),
                              started_at=start + timedelta(minutes=10 * i),
                              ended_at=start + timedelta(minutes=10 * (i + 1)),
                              active_minutes=active, tracked_minutes=10))
    db.commit()
    assert months(db, user)[3]['activity_percent'] == 70


def test_a_month_with_no_measurement_has_no_percentage(db, user):
    """Not 0%. An agent installed later must not make an earlier month look
    like somebody who did nothing."""
    worked(db, user, 2026, 3, 10)
    assert months(db, user)[3]['activity_percent'] is None


# ── The page ─────────────────────────────────────────────────────────────────

def test_an_admin_can_open_anybodys_year(client, db, make_login_user, password):
    from app.services import consent as C

    admin = make_login_user('boss@example.com', role='admin')
    worker = make_login_user('w@example.com')
    C.record(db, admin)
    worked(db, worker, 2026, 3, 10)

    client.post('/login', data={'email': 'boss@example.com', 'password': password})
    page = client.get(f'/year?user={worker.id}')
    assert page.status_code == 200
    assert b'Viewing' in page.data


def test_a_worker_asking_for_someone_else_gets_their_own(client, db,
                                                         make_login_user, password):
    """No 403 — that would confirm the other account exists."""
    from app.services import consent as C

    worker = make_login_user('w@example.com')
    other = make_login_user('other@example.com')
    C.record(db, worker)
    worked(db, other, 2026, 3, 10, hours=8)

    client.post('/login', data={'email': 'w@example.com', 'password': password})
    page = client.get(f'/year?user={other.id}')
    assert page.status_code == 200
    assert b'Viewing' not in page.data


def test_an_absurd_year_is_refused(client, db, make_login_user, password):
    """A bound, so a crafted query cannot ask the database for a million days."""
    from app.services import consent as C

    worker = make_login_user('w@example.com')
    C.record(db, worker)
    client.post('/login', data={'email': 'w@example.com', 'password': password})

    assert client.get('/year?year=999999').status_code == 404
    assert client.get('/year?year=1970').status_code == 404
    assert client.get('/year?year=notayear').status_code == 404
