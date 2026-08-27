"""Breaks taken during a session, and the totals that have to exclude them.

Reaching the idle threshold used to CLOSE the running session and open a fresh
one on return, which chopped a day on one project into a dozen fragments. Now
it pauses: the same session continues, and the break is cut out of the total.

That moves an invariant. Time used to be excluded by construction — the session
simply was not open during a break, so nothing could count it. Now a session
contains its breaks and every total has to subtract them explicitly, which is
precisely the kind of change that is correct on the day it is written and wrong
six months later. Hence this file.
"""
import uuid
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from app.models import IdlePeriod, Session
from app.services import reporting as R
from app.services.users import create_user

UTC = timezone.utc
NAIROBI = ZoneInfo('Africa/Nairobi')
DAY = date(2026, 8, 27)


@pytest.fixture
def user(db, password):
    return create_user(db, 'nbo@example.com', 'Nairobi', password,
                       timezone_name='Africa/Nairobi')


def at(hh, mm=0):
    """A local Nairobi wall-clock time on the test day, as UTC."""
    return datetime(2026, 8, 27, hh, mm, tzinfo=NAIROBI).astimezone(UTC)


def session(db, user, start, end=None, project='Alpha', idle_since=None):
    s = Session(user_id=user.id, client_uuid=uuid.uuid4(), project=project,
                started_at=start, ended_at=end, idle_since=idle_since)
    db.add(s)
    db.commit()
    return s


def break_(db, user, start, end):
    row = IdlePeriod(user_id=user.id, client_uuid=uuid.uuid4(),
                     started_at=start, ended_at=end,
                     duration_seconds=int((end - start).total_seconds()))
    db.add(row)
    db.commit()
    return row


# Every total is capped at `now` — an open session counts up to the present and
# never past it. These tests run on the day they describe, so `now` is pinned
# past the end of it; letting it default to the real clock would clip sessions
# mid-afternoon and the numbers would depend on when the suite was run.
END_OF_DAY = datetime(2026, 8, 28, 23, 59, tzinfo=NAIROBI).astimezone(UTC)


def hours(db, user, now=None):
    return R.daily_totals(db, user, DAY, DAY, now=now or END_OF_DAY).get(DAY, 0)


# ── The break comes out of the total ─────────────────────────────────────────

def test_a_session_with_no_break_counts_in_full(db, user):
    session(db, user, at(9), at(12))
    assert hours(db, user) == 3 * 3600


def test_a_break_inside_a_session_is_not_counted(db, user):
    """09:00 to 17:00 with an hour for lunch is seven hours, not eight."""
    session(db, user, at(9), at(17))
    break_(db, user, at(13), at(14))
    assert hours(db, user) == 7 * 3600


def test_several_breaks_all_come_out(db, user):
    session(db, user, at(9), at(17))
    break_(db, user, at(11), at(11, 30))
    break_(db, user, at(13), at(14))
    break_(db, user, at(15, 45), at(16))
    assert hours(db, user) == 8 * 3600 - (30 + 60 + 15) * 60


def test_the_pause_that_replaced_the_split_gives_the_same_total(db, user):
    """The point of the change. One paused session and two closed ones either
    side of the same gap must add up identically — the day was the same day."""
    session(db, user, at(9), at(17))
    break_(db, user, at(12), at(13))
    paused = hours(db, user)

    db.query(Session).delete()
    db.query(IdlePeriod).delete()
    db.commit()
    session(db, user, at(9), at(12))
    session(db, user, at(13), at(17))
    assert hours(db, user) == paused


def test_a_break_outside_any_session_changes_nothing(db, user):
    """Idle before work started is not deducted from work that had not begun."""
    session(db, user, at(14), at(17))
    break_(db, user, at(9), at(13))
    assert hours(db, user) == 3 * 3600


def test_a_break_overhanging_the_end_only_counts_where_it_overlaps(db, user):
    session(db, user, at(9), at(12))
    break_(db, user, at(11), at(15))
    assert hours(db, user) == 2 * 3600


def test_overlapping_breaks_are_not_subtracted_twice(db, user):
    """Two rows covering the same minute would otherwise take it off the total
    twice and under-report the day."""
    session(db, user, at(9), at(17))
    break_(db, user, at(12), at(14))
    break_(db, user, at(13), at(15))
    assert hours(db, user) == 8 * 3600 - 3 * 3600


def test_a_break_covering_the_whole_session_leaves_nothing(db, user):
    session(db, user, at(9), at(10))
    break_(db, user, at(8), at(11))
    assert hours(db, user) == 0


# ── While the break is still happening ───────────────────────────────────────

def test_an_open_session_stops_counting_while_paused(db, user):
    """The break in progress has no idle_periods row yet — that is written when
    somebody comes back. Without idle_since the dashboard would count up
    through every lunch."""
    session(db, user, at(9), None, idle_since=at(12))
    assert hours(db, user, now=at(15)) == 3 * 3600


def test_an_open_session_that_is_not_paused_counts_up_to_now(db, user):
    session(db, user, at(9), None)
    assert hours(db, user, now=at(12)) == 3 * 3600


def test_resuming_starts_the_clock_again(db, user):
    """idle_since cleared, and the finished break now on record."""
    session(db, user, at(9), None)
    break_(db, user, at(12), at(13))
    assert hours(db, user, now=at(15)) == 5 * 3600


def test_status_says_paused_rather_than_stopped(db, user):
    session(db, user, at(9), None, idle_since=at(12))
    status = R.current_status(db, user, now=at(13))
    assert status['is_tracking'] is True
    assert status['is_paused'] is True
    assert status['paused_since'] == at(12)
    assert status['project'] == 'Alpha'


def test_status_is_not_paused_when_input_is_flowing(db, user):
    session(db, user, at(9), None)
    status = R.current_status(db, user, now=at(10))
    assert status['is_tracking'] is True and status['is_paused'] is False


# ── Across boundaries ────────────────────────────────────────────────────────

def test_a_break_across_local_midnight_lands_in_both_days(db, user):
    """A session running past midnight is split at the boundary, so a break
    straddling it has to be too — or the deduction lands entirely in one day."""
    start = datetime(2026, 8, 27, 22, 0, tzinfo=NAIROBI).astimezone(UTC)
    end = datetime(2026, 8, 28, 3, 0, tzinfo=NAIROBI).astimezone(UTC)
    session(db, user, start, end)
    break_(db, user,
           datetime(2026, 8, 27, 23, 30, tzinfo=NAIROBI).astimezone(UTC),
           datetime(2026, 8, 28, 0, 30, tzinfo=NAIROBI).astimezone(UTC))

    totals = R.daily_totals(db, user, date(2026, 8, 27), date(2026, 8, 28),
                            now=END_OF_DAY)
    assert totals[date(2026, 8, 27)] == 90 * 60      # 22:00–23:30
    assert totals[date(2026, 8, 28)] == 150 * 60     # 00:30–03:00


def test_project_totals_subtract_breaks_too(db, user):
    """Otherwise the day total and the project rows in the same report
    disagree, and a report that disagrees with itself is worse than no report."""
    session(db, user, at(9), at(13), project='Alpha')
    session(db, user, at(13), at(17), project='Beta')
    break_(db, user, at(11), at(12))

    rows = {r['project']: r['total_seconds']
            for r in R.project_totals(db, user, DAY, DAY, now=END_OF_DAY)}
    assert rows['Alpha'] == 3 * 3600
    assert rows['Beta'] == 4 * 3600
    assert sum(rows.values()) == hours(db, user)


# ── What the page shows ─────────────────────────────────────────────────────

def test_the_session_figure_agrees_with_the_day_total(db, user):
    """The pill used to show wall clock since the session opened. Once a
    session can pause, that overstates by the length of every break, and a page
    whose two numbers disagree is worse than one that shows fewer."""
    s = session(db, user, at(9), None)
    break_(db, user, at(12), at(13))
    status = R.current_status(db, user, now=at(15))

    assert status['tracked_seconds'] == 5 * 3600      # nine to three, less lunch
    assert status['elapsed_seconds'] == 6 * 3600      # wall clock, still available
    assert status['tracked_seconds'] == hours(db, user, now=at(15))


def test_a_paused_session_reports_how_long_it_has_been_idle(db, user):
    session(db, user, at(9), None, idle_since=at(12))
    status = R.current_status(db, user, now=at(13, 30))
    assert status['is_paused'] is True
    assert status['paused_seconds'] == 90 * 60


def test_nothing_is_paused_when_nothing_is_running(db, user):
    status = R.current_status(db, user, now=at(13))
    assert status['is_tracking'] is False
    assert status['is_paused'] is False
    assert status['tracked_seconds'] == 0
    assert status['paused_seconds'] == 0


# ── The day rolling over ────────────────────────────────────────────────────

def yesterday(hh, mm=0):
    return datetime(2026, 8, 26, hh, mm, tzinfo=NAIROBI).astimezone(UTC)


def test_a_session_paused_since_yesterday_is_closed(db, user):
    """A laptop left running. Without this, Monday's session is still open on
    Wednesday and the history is one item three days long."""
    from app.services.sessions import close_sessions_paused_overnight

    s = session(db, user, yesterday(9), None, idle_since=yesterday(17))
    closed = close_sessions_paused_overnight(db, [user], now=at(10))

    assert len(closed) == 1
    db.expire_all()
    assert db.get(Session, s.id).ended_at == yesterday(17)


def test_it_ends_where_input_stopped_not_at_the_boundary(db, user):
    """The hours in between were already excluded as idle. Moving the end
    forward would either credit them or leave a gap nothing accounts for."""
    from app.services.sessions import close_sessions_paused_overnight

    session(db, user, yesterday(9), None, idle_since=yesterday(17))
    closed = close_sessions_paused_overnight(db, [user], now=at(10))
    assert closed[0]['ended_at'] == yesterday(17)


def test_a_session_paused_earlier_today_is_left_alone(db, user):
    """They may still walk back in. The pause waits, as it is meant to."""
    from app.services.sessions import close_sessions_paused_overnight

    session(db, user, at(9), None, idle_since=at(12))
    assert close_sessions_paused_overnight(db, [user], now=at(14)) == []


def test_somebody_working_past_midnight_is_not_touched(db, user):
    """Not paused, so not a candidate — whatever the clock says. This is why
    the test is the pause mark and not the hour."""
    from app.services.sessions import close_sessions_paused_overnight

    s = session(db, user, yesterday(22), None)
    assert close_sessions_paused_overnight(db, [user], now=at(0, 30)) == []
    db.expire_all()
    assert db.get(Session, s.id).ended_at is None


def test_closing_it_does_not_change_the_totals(db, user):
    """The idle was already subtracted. Ending the session where the pause
    began must move no minutes between days."""
    from app.services.sessions import close_sessions_paused_overnight

    session(db, user, yesterday(9), None, idle_since=yesterday(17))
    break_(db, user, yesterday(17), yesterday(23))
    before = R.daily_totals(db, user, date(2026, 8, 26), date(2026, 8, 27),
                            now=at(10))
    close_sessions_paused_overnight(db, [user], now=at(10))
    after = R.daily_totals(db, user, date(2026, 8, 26), date(2026, 8, 27),
                           now=at(10))
    assert before == after
    assert after[date(2026, 8, 26)] == 8 * 3600


def test_running_it_twice_closes_nothing_the_second_time(db, user):
    from app.services.sessions import close_sessions_paused_overnight

    session(db, user, yesterday(9), None, idle_since=yesterday(17))
    assert len(close_sessions_paused_overnight(db, [user], now=at(10))) == 1
    assert close_sessions_paused_overnight(db, [user], now=at(10)) == []


def test_each_person_gets_their_own_midnight(db, user, password):
    """An admin in another zone must not close somebody's session because it
    is tomorrow where the server is."""
    from app.services.sessions import close_sessions_paused_overnight
    from app.services.users import create_user

    honolulu = create_user(db, 'hi@example.com', 'Honolulu', password,
                           timezone_name='Pacific/Honolulu')
    # 01:00 in Nairobi on the 27th is still 12:00 on the 26th in Honolulu.
    paused_at = datetime(2026, 8, 26, 9, 0, tzinfo=ZoneInfo('Pacific/Honolulu'))
    s = session(db, honolulu, paused_at - timedelta(hours=2), None,
                idle_since=paused_at)

    now = datetime(2026, 8, 27, 1, 0, tzinfo=NAIROBI).astimezone(UTC)
    assert close_sessions_paused_overnight(db, [honolulu], now=now) == []
    db.expire_all()
    assert db.get(Session, s.id).ended_at is None


# ── The settings that drive it ───────────────────────────────────────────────

def test_the_pause_threshold_defaults_to_fifteen_minutes(db, user):
    assert user.settings.idle_threshold_seconds == 15 * 60


def test_a_pause_waits_indefinitely(db, user):
    """No maximum. Deciding on somebody's behalf that they have been away long
    enough to have gone home needs working hours nobody is asked for; stopping
    deliberately is what the pause control is already for."""
    session(db, user, at(9), None, idle_since=at(10))
    much_later = datetime(2026, 8, 29, 12, 0, tzinfo=NAIROBI).astimezone(UTC)
    assert R.current_status(db, user, now=much_later)['is_paused'] is True
    assert hours(db, user, now=much_later) == 1 * 3600
