"""How much of the tracked time had a person in it.

Credited time measures presence: a session open and not idle. That is
manufacturable — one keystroke every fourteen minutes holds the idle counter
below its threshold all day, so roughly thirty keystrokes buys eight hours.

This is the second number, and the tests below are mostly about the ways it
could quietly lie: a percentage above 100, a partly-covered window weighing the
same as a full one, and "not measured" rendering as "did nothing".
"""
import uuid
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from app.models import ActivityWindow, Session
from app.services import reporting as R
from app.services.users import create_user

UTC = timezone.utc
NAIROBI = ZoneInfo('Africa/Nairobi')
DAY = date(2026, 8, 27)
END_OF_DAY = datetime(2026, 8, 28, 23, 59, tzinfo=NAIROBI).astimezone(UTC)


@pytest.fixture
def user(db, password):
    return create_user(db, 'nbo@example.com', 'Nairobi', password,
                       timezone_name='Africa/Nairobi')


def at(hh, mm=0):
    return datetime(2026, 8, 27, hh, mm, tzinfo=NAIROBI).astimezone(UTC)


def window(db, user, start, active, tracked=10):
    row = ActivityWindow(user_id=user.id, client_uuid=uuid.uuid4(),
                         started_at=start, ended_at=start + timedelta(minutes=10),
                         active_minutes=active, tracked_minutes=tracked)
    db.add(row)
    db.commit()
    return row


def summary(db, user):
    return R.activity_summary(db, user, DAY, DAY, now=END_OF_DAY)


# ── The number itself ────────────────────────────────────────────────────────

def test_a_full_window_reads_as_written(db, user):
    window(db, user, at(9), active=7)
    assert summary(db, user)['percent'] == 70


def test_a_real_morning_and_a_tapped_keyboard_are_not_close(db, user):
    """The whole point. Both hold a session open for the same wall-clock time;
    only one of them was doing anything."""
    for i in range(6):
        window(db, user, at(9) + timedelta(minutes=10 * i), active=7)
    real = summary(db, user)['percent']

    db.query(ActivityWindow).delete()
    db.commit()
    for i in range(6):
        window(db, user, at(9) + timedelta(minutes=10 * i), active=1)
    tapped = summary(db, user)['percent']

    assert real == 70 and tapped == 10


def test_windows_are_weighted_by_how_much_they_covered(db, user):
    """A ratio of totals, not an average of percentages. A window covering two
    minutes is not equal evidence to one covering ten, and averaging lets a
    sliver swing the whole day."""
    window(db, user, at(9), active=2, tracked=2)      # 100%, but two minutes
    window(db, user, at(9, 10), active=2, tracked=10)  # 20%, over ten

    # Averaging the percentages would say 60. The honest answer is 4 of 12.
    assert summary(db, user)['percent'] == 33


def test_nothing_measured_is_not_zero_percent(db, user):
    """"No data" and "did nothing" are different answers. A page rendering both
    as 0% is lying about one of them."""
    assert summary(db, user)['percent'] is None
    assert summary(db, user)['tracked_minutes'] == 0


def test_a_window_with_no_tracked_minutes_has_no_percentage(db, user):
    row = window(db, user, at(9), active=0, tracked=0)
    assert row.percent is None


def test_only_this_persons_windows_count(db, user, password):
    other = create_user(db, 'other@example.com', 'Other', password)
    window(db, user, at(9), active=8)
    window(db, other, at(9), active=1)
    assert summary(db, user)['percent'] == 80


def test_windows_outside_the_day_are_not_counted(db, user):
    window(db, user, at(9), active=8)
    window(db, user, at(9) - timedelta(days=1), active=0)
    assert summary(db, user)['percent'] == 80


# ── Matching a percentage to a screenshot ────────────────────────────────────

def test_a_capture_is_matched_to_the_window_it_falls_in(db, user):
    window(db, user, at(9), active=7)
    window(db, user, at(9, 10), active=2)

    found = R.activity_for_instants(db, user, [at(9, 3), at(9, 14)])
    assert found[at(9, 3)] == 70
    assert found[at(9, 14)] == 20


def test_a_capture_with_no_window_gets_nothing_rather_than_zero(db, user):
    """An agent that predates activity tracking. The gallery shows a dash."""
    window(db, user, at(9), active=7)
    found = R.activity_for_instants(db, user, [at(15)])
    assert found.get(at(15)) is None


def test_the_window_boundary_belongs_to_the_later_window(db, user):
    """Half-open, like every other range in the system. A capture at exactly
    09:10 belongs to 09:10–09:20, and is counted once."""
    window(db, user, at(9), active=7)
    window(db, user, at(9, 10), active=2)
    assert R.activity_for_instants(db, user, [at(9, 10)])[at(9, 10)] == 20


def test_no_captures_asks_nothing(db, user):
    assert R.activity_for_instants(db, user, []) == {}


# ── What the agent is allowed to claim ───────────────────────────────────────

def test_more_active_than_tracked_is_refused(db, user):
    """The agent computes these on somebody's own machine. A window claiming
    more active minutes than tracked ones would render as over 100%."""
    from sqlalchemy.exc import IntegrityError

    db.add(ActivityWindow(user_id=user.id, client_uuid=uuid.uuid4(),
                          started_at=at(9), ended_at=at(9, 10),
                          active_minutes=11, tracked_minutes=10))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_negative_minutes_are_refused(db, user):
    from sqlalchemy.exc import IntegrityError

    db.add(ActivityWindow(user_id=user.id, client_uuid=uuid.uuid4(),
                          started_at=at(9), ended_at=at(9, 10),
                          active_minutes=-1, tracked_minutes=10))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()
