"""When a report is due, and the guarantee that it goes out exactly once.

The local app kept "have I sent this yet" in a JSON file on disk. That failed
in the most instructive way possible: the first time the service restarted
without the file, the catch-up branch fired and mailed a backdated report to a
real recipient. A file can be missing, wiped with a volume, or raced by two
workers starting at the same instant.

Here the record IS the guarantee. A row in report_sends with a unique
constraint on (user_id, kind, period_key) means the second attempt fails at the
database rather than in an if-statement, whatever the filesystem looks like and
however many workers are running.

Two rules carry over from the local app, both learned painfully:

  * a period is only reported once it has CLOSED. A weekly report goes out the
    Monday after its week, a monthly one on the last day of its month. A "month
    so far" sent on the last Monday would silently omit the days still to come.
  * no history is NOT a missed send. A fresh install, or a wiped database, must
    not interpret "I have never sent anything" as "I owe you everything". That
    is the exact bug above.
"""
from datetime import date, datetime, time, timedelta, timezone

from sqlalchemy.exc import IntegrityError

from app.models import ReportSend
from app.services import reporting as R

UTC = timezone.utc


# ── Period keys ──────────────────────────────────────────────────────────────

def weekly_key(monday: date) -> str:
    """ISO year and week of the Monday — '2026-W35'.

    Not the calendar year of the Monday: an ISO week can straddle New Year, and
    keying on the wrong year would let one week be reported twice.
    """
    iso_year, iso_week, _ = monday.isocalendar()
    return f'{iso_year}-W{iso_week:02d}'


def monthly_key(year: int, month: int) -> str:
    return f'{year:04d}-{month:02d}'


def month_end(year: int, month: int) -> date:
    first_of_next = date(year + (month == 12), (month % 12) + 1, 1)
    return first_of_next - timedelta(days=1)


def previous_month(day: date):
    last_of_previous = day.replace(day=1) - timedelta(days=1)
    return last_of_previous.year, last_of_previous.month


# ── Due? ─────────────────────────────────────────────────────────────────────

def weekly_due(user, now):
    """(period_key, monday) for the week a report should cover, or None.

    The report fires on the configured weekday and hour in the USER's timezone,
    and always covers the PREVIOUS week — even on send day, because send day is
    the first day of a new week, not the last day of the old one.
    """
    settings = user.settings
    local = now.astimezone(R.user_tz(user))
    if local.weekday() != settings.weekly_send_weekday:
        return None
    if local.hour < settings.weekly_send_hour:
        return None

    this_monday = R.week_start(local.date())
    monday = this_monday - timedelta(days=7)
    return weekly_key(monday), monday


def monthly_due(user, now):
    """(period_key, year, month) for the month to report, or None.

    Fires on the LAST DAY of the month at the configured hour. The month is not
    strictly over at that point, so whoever renders it must say what window was
    actually measured rather than implying a whole month.
    """
    settings = user.settings
    local = now.astimezone(R.user_tz(user))
    today = local.date()

    if today == month_end(today.year, today.month) and local.hour >= settings.monthly_send_hour:
        year, month = today.year, today.month
    else:
        # Catch-up: the machine may have been down at month end, and a month
        # with no report at all is the worse failure. The send-once record is
        # what stops this firing every day for the rest of the month.
        year, month = previous_month(today)

    return monthly_key(year, month), year, month


# ── Sent? ────────────────────────────────────────────────────────────────────

def already_sent(db, user, kind, period_key):
    return (db.query(ReportSend)
            .filter(ReportSend.user_id == user.id, ReportSend.kind == kind,
                    ReportSend.period_key == period_key)
            .first() is not None)


def has_history(db, user, kind):
    """Whether this user has ever had a report of this kind sent.

    No history is not a missed send — it is a new account, or a restored
    database. Seeding rather than sending is what stops a deployment mailing
    everyone a backdated report the moment it comes up.
    """
    return (db.query(ReportSend)
            .filter(ReportSend.user_id == user.id, ReportSend.kind == kind)
            .first() is not None)


def claim(db, user, kind, period_key, recipient, sent_at=None):
    """Reserve the right to send, atomically. True if this caller won.

    Claimed BEFORE the mail goes out, not after. Two workers reaching this at
    the same instant cannot both win: the loser's INSERT violates the unique
    constraint and it does not send. The cost of that ordering is that a crash
    between claiming and sending loses one report; the cost of the other
    ordering is sending two, and a duplicate report is worse than a missing one
    you can trigger by hand.
    """
    row = ReportSend(user_id=user.id, kind=kind, period_key=period_key,
                     recipient=recipient, sent_at=sent_at or datetime.now(UTC))
    db.add(row)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return False
    return True


def release(db, user, kind, period_key):
    """Give a claim back when sending failed, so the next run may retry."""
    (db.query(ReportSend)
     .filter(ReportSend.user_id == user.id, ReportSend.kind == kind,
             ReportSend.period_key == period_key)
     .delete())
    db.commit()


def seed(db, user, kind, period_key, recipient='(seeded)'):
    """Record a period as handled without sending anything — for a new account,
    so its first real report is the next one rather than a backdated surprise."""
    return claim(db, user, kind, period_key, recipient)
