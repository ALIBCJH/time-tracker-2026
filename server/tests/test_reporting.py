"""Days and weeks, in the user's own timezone.

Everything is stored as UTC and nobody thinks in UTC, so every test here is
really asking the same question: does an hour land in the day the person who
worked it would say it landed in.
"""
import uuid
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from app.models import Session
from app.services import reporting as R
from app.services.users import create_user

UTC = timezone.utc
NAIROBI = ZoneInfo('Africa/Nairobi')      # UTC+3, no DST
LONDON = ZoneInfo('Europe/London')        # UTC+0/+1, has DST


@pytest.fixture
def nairobi_user(db, password):
    return create_user(db, 'nbo@example.com', 'Nairobi', password,
                       timezone_name='Africa/Nairobi')


def add(db, user, start, end, project='Alpha'):
    s = Session(user_id=user.id, client_uuid=uuid.uuid4(), project=project,
                started_at=start, ended_at=end)
    db.add(s)
    db.commit()
    return s


def local(tz, y, m, d, hh=0, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=tz)


# ── Which day is it ──────────────────────────────────────────────────────────

def test_the_local_date_is_the_users_not_the_servers(db, nairobi_user):
    """22:30 UTC is already tomorrow in Nairobi."""
    at = datetime(2026, 8, 26, 22, 30, tzinfo=UTC)
    assert R.logical_today(nairobi_user, at) == date(2026, 8, 27)


def test_a_day_window_is_local_midnight_to_local_midnight(db, nairobi_user):
    start, end = R.day_window(nairobi_user, date(2026, 8, 26))
    assert start == datetime(2026, 8, 25, 21, 0, tzinfo=UTC)   # 00:00 EAT
    assert end == datetime(2026, 8, 26, 21, 0, tzinfo=UTC)


def test_the_week_starts_on_monday(db):
    assert R.week_start(date(2026, 8, 26)) == date(2026, 8, 24)   # Wed -> Mon
    assert R.week_start(date(2026, 8, 24)) == date(2026, 8, 24)   # Mon -> itself
    assert R.week_start(date(2026, 8, 30)) == date(2026, 8, 24)   # Sun -> Mon


# ── Attribution ──────────────────────────────────────────────────────────────

def test_an_ordinary_session_lands_in_its_own_day(db, nairobi_user):
    add(db, nairobi_user, local(NAIROBI, 2026, 8, 26, 9), local(NAIROBI, 2026, 8, 26, 12))
    assert R.day_summary(db, nairobi_user, date(2026, 8, 26),
                         now=local(NAIROBI, 2026, 8, 27))['total_seconds'] == 3 * 3600


def test_late_evening_work_stays_in_the_day_it_felt_like(db, nairobi_user):
    """20:00–22:00 Nairobi is 17:00–19:00 UTC — same day either way, but the
    session is stored in UTC and must still be found by the local day."""
    add(db, nairobi_user, local(NAIROBI, 2026, 8, 26, 20), local(NAIROBI, 2026, 8, 26, 22))
    assert R.day_summary(db, nairobi_user, date(2026, 8, 26),
                         now=local(NAIROBI, 2026, 8, 27))['total_seconds'] == 2 * 3600


def test_work_after_local_midnight_belongs_to_the_next_day(db, nairobi_user):
    """01:00 Nairobi is 22:00 UTC the day before. Storing UTC and reading it
    back naively would file this under yesterday."""
    add(db, nairobi_user, local(NAIROBI, 2026, 8, 27, 1), local(NAIROBI, 2026, 8, 27, 3))
    now = local(NAIROBI, 2026, 8, 28)
    assert R.day_summary(db, nairobi_user, date(2026, 8, 26), now=now)['total_seconds'] == 0
    assert R.day_summary(db, nairobi_user, date(2026, 8, 27), now=now)['total_seconds'] == 2 * 3600


def test_a_session_across_midnight_is_split_in_proportion(db, nairobi_user):
    """23:00 to 02:00 is one hour of Wednesday and two of Thursday — not three
    of whichever day it started in."""
    add(db, nairobi_user, local(NAIROBI, 2026, 8, 26, 23), local(NAIROBI, 2026, 8, 27, 2))
    totals = R.daily_totals(db, nairobi_user, date(2026, 8, 26), date(2026, 8, 27),
                            now=local(NAIROBI, 2026, 8, 28))
    assert totals[date(2026, 8, 26)] == 3600
    assert totals[date(2026, 8, 27)] == 2 * 3600


def test_a_session_spanning_several_days_is_split_across_all_of_them(db, nairobi_user):
    add(db, nairobi_user, local(NAIROBI, 2026, 8, 26, 22), local(NAIROBI, 2026, 8, 29, 2))
    totals = R.daily_totals(db, nairobi_user, date(2026, 8, 26), date(2026, 8, 29),
                            now=local(NAIROBI, 2026, 8, 30))
    assert totals[date(2026, 8, 26)] == 2 * 3600
    assert totals[date(2026, 8, 27)] == 24 * 3600
    assert totals[date(2026, 8, 28)] == 24 * 3600
    assert totals[date(2026, 8, 29)] == 2 * 3600


def test_two_users_in_different_zones_see_different_days(db, nairobi_user, password):
    """The same instant, filed under different local dates. An admin must see
    the worker's day, not their own."""
    londoner = create_user(db, 'ldn@example.com', 'London', password,
                           timezone_name='Europe/London')
    moment = datetime(2026, 8, 26, 22, 30, tzinfo=UTC)
    assert R.logical_today(nairobi_user, moment) == date(2026, 8, 27)
    assert R.logical_today(londoner, moment) == date(2026, 8, 26)


def test_a_dst_day_is_still_one_local_day(db, password):
    """Europe/London springs forward on 2026-03-29: that local day is 23 hours
    long. The window must still run local-midnight to local-midnight."""
    user = create_user(db, 'ldn@example.com', 'L', password,
                       timezone_name='Europe/London')
    start, end = R.day_window(user, date(2026, 3, 29))
    assert (end - start) == timedelta(hours=23)


# ── Open sessions ────────────────────────────────────────────────────────────

def test_an_open_session_counts_up_to_now(db, nairobi_user):
    add(db, nairobi_user, local(NAIROBI, 2026, 8, 26, 9), None)
    got = R.day_summary(db, nairobi_user, date(2026, 8, 26),
                        now=local(NAIROBI, 2026, 8, 26, 11))
    assert got['total_seconds'] == 2 * 3600


def test_an_open_session_never_counts_past_now(db, nairobi_user):
    add(db, nairobi_user, local(NAIROBI, 2026, 8, 26, 9), None)
    totals = R.daily_totals(db, nairobi_user, date(2026, 8, 26), date(2026, 8, 31),
                            now=local(NAIROBI, 2026, 8, 26, 11))
    assert sum(totals.values()) == 2 * 3600


# ── Weeks ────────────────────────────────────────────────────────────────────

def test_a_week_has_seven_days_starting_monday(db, nairobi_user):
    week = R.week_summary(db, nairobi_user, now=local(NAIROBI, 2026, 8, 26, 12))
    assert len(week['days']) == 7
    assert week['week_start'] == date(2026, 8, 24)
    assert [d['label'] for d in week['days']] == ['Mon','Tue','Wed','Thu','Fri','Sat','Sun']


def test_future_days_are_present_and_empty(db, nairobi_user):
    week = R.week_summary(db, nairobi_user, now=local(NAIROBI, 2026, 8, 26, 12))
    future = [d for d in week['days'] if d['is_future']]
    assert len(future) == 4 and all(d['total_seconds'] == 0 for d in future)


def test_the_week_total_is_the_sum_of_its_days(db, nairobi_user):
    for day in (24, 25, 26):
        add(db, nairobi_user, local(NAIROBI, 2026, 8, day, 9),
            local(NAIROBI, 2026, 8, day, 12))
    week = R.week_summary(db, nairobi_user, now=local(NAIROBI, 2026, 8, 26, 23))
    assert week['total_seconds'] == 9 * 3600
    assert sum(d['total_seconds'] for d in week['days']) == week['total_seconds']


def test_last_weeks_work_is_not_in_this_week(db, nairobi_user):
    add(db, nairobi_user, local(NAIROBI, 2026, 8, 23, 9), local(NAIROBI, 2026, 8, 23, 17))
    week = R.week_summary(db, nairobi_user, now=local(NAIROBI, 2026, 8, 26, 12))
    assert week['total_seconds'] == 0


# ── Isolation ────────────────────────────────────────────────────────────────

def test_one_users_work_never_appears_in_anothers(db, nairobi_user, password):
    other = create_user(db, 'b@example.com', 'B', password)
    add(db, other, local(NAIROBI, 2026, 8, 26, 9), local(NAIROBI, 2026, 8, 26, 17))
    assert R.day_summary(db, nairobi_user, date(2026, 8, 26),
                         now=local(NAIROBI, 2026, 8, 27))['total_seconds'] == 0


# ── Projects and status ──────────────────────────────────────────────────────

def test_projects_are_ranked_by_time(db, nairobi_user):
    add(db, nairobi_user, local(NAIROBI, 2026, 8, 26, 9),
        local(NAIROBI, 2026, 8, 26, 10), project='Small')
    add(db, nairobi_user, local(NAIROBI, 2026, 8, 26, 10),
        local(NAIROBI, 2026, 8, 26, 15), project='Big')
    rows = R.project_totals(db, nairobi_user, date(2026, 8, 26), date(2026, 8, 26),
                            now=local(NAIROBI, 2026, 8, 27))
    assert [r['project'] for r in rows] == ['Big', 'Small']


def test_status_reports_an_open_session(db, nairobi_user):
    add(db, nairobi_user, local(NAIROBI, 2026, 8, 26, 9), None, project='Alpha')
    status = R.current_status(db, nairobi_user, now=local(NAIROBI, 2026, 8, 26, 11))
    assert status['is_tracking'] and status['project'] == 'Alpha'
    assert status['elapsed_seconds'] == 2 * 3600


def test_status_is_quiet_when_nothing_is_running(db, nairobi_user):
    status = R.current_status(db, nairobi_user, now=local(NAIROBI, 2026, 8, 26, 11))
    assert not status['is_tracking'] and status['elapsed_seconds'] == 0


@pytest.mark.parametrize('seconds, text', [
    (0, '0m'), (59, '0m'), (60, '1m'), (3600, '1h 00m'),
    (3661, '1h 01m'), (36000, '10h 00m'),
])
def test_duration_formatting(seconds, text):
    assert R.format_hm(seconds) == text
