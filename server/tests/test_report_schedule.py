"""When reports fire, and the promise that each fires once.

The local app kept this in a JSON file and the first restart without that file
mailed a real person a backdated report. Every test here is a way that could
happen again.
"""
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from app.models import ReportSend
from app.reports import schedule as S
from app.services.users import create_user

UTC = timezone.utc
NBO = ZoneInfo('Africa/Nairobi')


@pytest.fixture
def user(db, password):
    return create_user(db, 'a@example.com', 'A', password,
                       timezone_name='Africa/Nairobi')


def at(y, m, d, hh=0, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=NBO)


# ── Period keys ──────────────────────────────────────────────────────────────

def test_a_week_straddling_new_year_keys_on_its_iso_year():
    """Keying on the calendar year of the Monday would let one week be
    reported twice — once as 2025's and once as 2026's."""
    assert S.weekly_key(date(2025, 12, 29)) == '2026-W01'


def test_month_end_handles_leap_years():
    assert S.month_end(2026, 2) == date(2026, 2, 28)
    assert S.month_end(2028, 2) == date(2028, 2, 29)


def test_previous_month_crosses_the_year_boundary():
    assert S.previous_month(date(2026, 1, 4)) == (2025, 12)


# ── Weekly timing ────────────────────────────────────────────────────────────

def test_the_weekly_report_fires_on_monday_at_the_send_hour(db, user):
    assert S.weekly_due(user, at(2026, 8, 24, 17)) is not None


def test_it_does_not_fire_before_the_send_hour(db, user):
    assert S.weekly_due(user, at(2026, 8, 24, 16)) is None


def test_it_does_not_fire_on_other_days(db, user):
    for day in range(25, 31):
        assert S.weekly_due(user, at(2026, 8, day, 17)) is None


def test_it_reports_the_previous_week_not_the_current_one(db, user):
    """Send day is the FIRST day of a new week, so the week to report is always
    the one that just closed — including on send day itself."""
    key, monday = S.weekly_due(user, at(2026, 8, 24, 17))
    assert monday == date(2026, 8, 17)
    assert key == S.weekly_key(date(2026, 8, 17))


def test_the_hour_is_the_users_local_hour(db, password):
    """17:00 Nairobi is 14:00 UTC. A server in UTC must not send three hours
    early, or on the wrong day for anyone near a date line."""
    nairobi = create_user(db, 'nbo@example.com', 'N', password,
                          timezone_name='Africa/Nairobi')
    moment = datetime(2026, 8, 24, 14, 0, tzinfo=UTC)      # 17:00 EAT
    assert S.weekly_due(nairobi, moment) is not None
    assert S.weekly_due(nairobi, moment - timedelta(hours=1)) is None


def test_each_user_can_have_their_own_send_time(db, user, password):
    other = create_user(db, 'b@example.com', 'B', password)
    other.settings.weekly_send_weekday = 4          # Friday
    other.settings.weekly_send_hour = 9
    db.commit()
    friday_morning = at(2026, 8, 28, 9)
    assert S.weekly_due(other, friday_morning) is not None
    assert S.weekly_due(user, friday_morning) is None


# ── Monthly timing ───────────────────────────────────────────────────────────

def test_the_monthly_report_fires_on_the_last_day(db, user):
    key, year, month = S.monthly_due(user, at(2026, 8, 31, 21))
    assert (key, year, month) == ('2026-08', 2026, 8)


def test_before_the_send_hour_the_last_day_still_owes_the_previous_month(db, user):
    """At 20:00 on the 31st, August is not due yet — but July might still be."""
    key, year, month = S.monthly_due(user, at(2026, 8, 31, 20))
    assert (key, year, month) == ('2026-07', 2026, 7)


def test_mid_month_only_the_closed_month_is_owed(db, user):
    """A 'month so far' total would silently omit the days still to come."""
    key, _, _ = S.monthly_due(user, at(2026, 8, 15, 21))
    assert key == '2026-07'


def test_a_month_missed_at_its_end_is_still_owed_afterwards(db, user):
    """The machine may have been off on the 31st, and a month with no report at
    all is the worse failure."""
    key, _, _ = S.monthly_due(user, at(2026, 9, 3, 10))
    assert key == '2026-08'


# ── Sending exactly once ─────────────────────────────────────────────────────

def test_a_claim_can_only_be_won_once(db, user):
    assert S.claim(db, user, 'monthly', '2026-08', 'a@example.com') is True
    assert S.claim(db, user, 'monthly', '2026-08', 'a@example.com') is False


def test_a_lost_claim_leaves_exactly_one_record(db, user):
    S.claim(db, user, 'monthly', '2026-08', 'a@example.com')
    S.claim(db, user, 'monthly', '2026-08', 'a@example.com')
    assert db.query(ReportSend).count() == 1


def test_two_workers_racing_cannot_both_send(db, engine, user):
    """The whole reason this is a constraint and not an if-statement."""
    from sqlalchemy.orm import sessionmaker
    Other = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    second = Other()
    try:
        first_won = S.claim(db, user, 'weekly', '2026-W35', 'a@example.com')
        second_won = S.claim(second, user, 'weekly', '2026-W35', 'a@example.com')
        assert [first_won, second_won] == [True, False]
    finally:
        second.close()


def test_weekly_and_monthly_do_not_block_each_other(db, user):
    assert S.claim(db, user, 'weekly', '2026-W35', 'a@example.com')
    assert S.claim(db, user, 'monthly', '2026-08', 'a@example.com')


def test_each_user_claims_independently(db, user, password):
    other = create_user(db, 'b@example.com', 'B', password)
    assert S.claim(db, user, 'monthly', '2026-08', 'a@example.com')
    assert S.claim(db, other, 'monthly', '2026-08', 'b@example.com')


def test_a_released_claim_can_be_retried(db, user):
    """Sending failed; the next run should be allowed to try again."""
    S.claim(db, user, 'monthly', '2026-08', 'a@example.com')
    S.release(db, user, 'monthly', '2026-08')
    assert S.claim(db, user, 'monthly', '2026-08', 'a@example.com') is True


def test_already_sent_reflects_the_record(db, user):
    assert not S.already_sent(db, user, 'monthly', '2026-08')
    S.claim(db, user, 'monthly', '2026-08', 'a@example.com')
    assert S.already_sent(db, user, 'monthly', '2026-08')


# ── No history is not a missed send ──────────────────────────────────────────

def test_a_brand_new_account_is_owed_nothing(db, user):
    """The exact bug: a fresh install must not read 'I have never sent
    anything' as 'I owe you everything' and mail a backdated report."""
    assert S.has_history(db, user, 'monthly') is False


def test_seeding_marks_a_period_handled_without_sending(db, user):
    S.seed(db, user, 'monthly', '2026-07')
    assert S.has_history(db, user, 'monthly')
    assert S.already_sent(db, user, 'monthly', '2026-07')
    assert S.claim(db, user, 'monthly', '2026-07', 'a@example.com') is False


def test_seeding_does_not_block_the_next_period(db, user):
    S.seed(db, user, 'monthly', '2026-07')
    assert S.claim(db, user, 'monthly', '2026-08', 'a@example.com') is True
