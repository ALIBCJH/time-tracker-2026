"""Turning stored instants into the days and weeks a person recognises.

Everything is stored as UTC. Nobody thinks in UTC. The whole job of this module
is the translation, and it is done in the user's own timezone — not the
server's, not the viewer's. When Benson's Tuesday started is a fact about
Benson, and an admin in another zone looking at his week must see his Tuesday,
not theirs.

A session that runs past local midnight is split at the boundary and its
minutes land in both days, in the proportion actually worked. Attributing the
whole session to whichever day it started in would be simpler and would quietly
move hours between days for anyone who works late.
"""
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from app.models import Session

UTC = timezone.utc


def user_tz(user):
    return ZoneInfo(user.settings.timezone)


def logical_today(user, now=None):
    """The date it is where the user is."""
    return (now or datetime.now(UTC)).astimezone(user_tz(user)).date()


def week_start(day):
    """The Monday of that day's week. Mon→Sun, as the local app settled on."""
    return day - timedelta(days=day.weekday())


def day_window(user, day):
    """[local midnight, next local midnight) as a UTC pair."""
    tz = user_tz(user)
    start = datetime.combine(day, time.min, tzinfo=tz)
    end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=tz)
    return start.astimezone(UTC), end.astimezone(UTC)


def range_window(user, first_day, last_day):
    return day_window(user, first_day)[0], day_window(user, last_day)[1]


def _span(session, now):
    """An open session counts up to now — but never past it."""
    return session.started_at, min(session.ended_at or now, now)


def _overlapping(db, user, window_start, window_end):
    return (db.query(Session)
            .filter(Session.user_id == user.id,
                    Session.started_at < window_end,
                    # An open session has no end yet, so it may still overlap.
                    (Session.ended_at.is_(None)) | (Session.ended_at > window_start))
            .order_by(Session.started_at)
            .all())


def _split_by_local_day(start, end, tz):
    """Yield (local_date, seconds) for a span, cut at local midnights."""
    cursor = start
    while cursor < end:
        local_day = cursor.astimezone(tz).date()
        next_midnight = datetime.combine(
            local_day + timedelta(days=1), time.min, tzinfo=tz).astimezone(UTC)
        segment_end = min(end, next_midnight)
        seconds = int((segment_end - cursor).total_seconds())
        if seconds > 0:
            yield local_day, seconds
        cursor = segment_end


def daily_totals(db, user, first_day, last_day, now=None):
    """{local_date: seconds} across an inclusive range of local days."""
    now = now or datetime.now(UTC)
    tz = user_tz(user)
    window_start, window_end = range_window(user, first_day, last_day)

    totals = {}
    for session in _overlapping(db, user, window_start, window_end):
        start, end = _span(session, now)
        start, end = max(start, window_start), min(end, window_end)
        if end <= start:
            continue
        for day, seconds in _split_by_local_day(start, end, tz):
            totals[day] = totals.get(day, 0) + seconds
    return totals


def day_summary(db, user, day=None, now=None):
    now = now or datetime.now(UTC)
    day = day or logical_today(user, now)
    totals = daily_totals(db, user, day, day, now=now)
    return {'date': day, 'total_seconds': totals.get(day, 0)}


def week_summary(db, user, monday=None, now=None):
    """Seven days, Monday first, future days present and zero."""
    now = now or datetime.now(UTC)
    today = logical_today(user, now)
    monday = monday or week_start(today)
    sunday = monday + timedelta(days=6)
    totals = daily_totals(db, user, monday, sunday, now=now)

    days = []
    for i in range(7):
        d = monday + timedelta(days=i)
        days.append({'date': d, 'label': d.strftime('%a'),
                     'total_seconds': totals.get(d, 0),
                     'is_today': d == today, 'is_future': d > today})
    return {'week_start': monday, 'week_end': sunday,
            'total_seconds': sum(d['total_seconds'] for d in days), 'days': days}


def project_totals(db, user, first_day, last_day, now=None):
    """Seconds per project over a local-day range, biggest first."""
    now = now or datetime.now(UTC)
    window_start, window_end = range_window(user, first_day, last_day)

    totals = {}
    for session in _overlapping(db, user, window_start, window_end):
        start, end = _span(session, now)
        start, end = max(start, window_start), min(end, window_end)
        seconds = int((end - start).total_seconds())
        if seconds > 0:
            totals[session.project] = totals.get(session.project, 0) + seconds
    return [{'project': p, 'total_seconds': s}
            for p, s in sorted(totals.items(), key=lambda kv: -kv[1])]


def current_status(db, user, now=None):
    """What the dashboard shows at the top: are they working right now."""
    now = now or datetime.now(UTC)
    open_session = (db.query(Session)
                    .filter(Session.user_id == user.id, Session.ended_at.is_(None))
                    .one_or_none())
    return {
        'is_tracking': open_session is not None,
        'project': open_session.project if open_session else None,
        'task': open_session.task if open_session else None,
        'started_at': open_session.started_at if open_session else None,
        'elapsed_seconds': (int((now - open_session.started_at).total_seconds())
                            if open_session else 0),
        'last_heartbeat_at': open_session.last_heartbeat_at if open_session else None,
    }


def format_hm(seconds):
    hours, rest = divmod(int(seconds), 3600)
    return f'{hours}h {rest // 60:02d}m' if hours else f'{rest // 60}m'
