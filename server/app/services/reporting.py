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

from app.models import ActivityWindow, IdlePeriod, Session

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
    """An open session counts up to now — but never past it.

    A session that is paused right now stops at the moment input stopped, not
    at now. The break it is in has no idle_periods row yet — that is only
    written when somebody comes back — so without this the dashboard would
    quietly count up through every lunch.
    """
    if session.ended_at is not None:
        return session.started_at, min(session.ended_at, now)
    return session.started_at, min(session.idle_since or now, now)


def _idle_intervals(db, user, window_start, window_end):
    """Completed breaks overlapping the window, merged and in order.

    Merged because two overlapping rows would otherwise each be subtracted,
    taking the same minute off twice and under-reporting the day. Overlap
    should not happen, but a total that is wrong when it does is not worth the
    few lines saved.
    """
    rows = (db.query(IdlePeriod)
            .filter(IdlePeriod.user_id == user.id,
                    IdlePeriod.started_at < window_end,
                    IdlePeriod.ended_at > window_start)
            .order_by(IdlePeriod.started_at)
            .all())

    merged = []
    for row in rows:
        if merged and row.started_at <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], row.ended_at))
        else:
            merged.append((row.started_at, row.ended_at))
    return merged


def _minus_idle(start, end, idles):
    """The span with the breaks cut out of it. Yields the worked pieces."""
    cursor = start
    for idle_start, idle_end in idles:
        if idle_end <= cursor:
            continue
        if idle_start >= end:
            break
        if idle_start > cursor:
            yield cursor, min(idle_start, end)
        cursor = max(cursor, idle_end)
        if cursor >= end:
            return
    if cursor < end:
        yield cursor, end


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

    idles = _idle_intervals(db, user, window_start, window_end)

    totals = {}
    for session in _overlapping(db, user, window_start, window_end):
        start, end = _span(session, now)
        start, end = max(start, window_start), min(end, window_end)
        if end <= start:
            continue
        # A session now contains the breaks taken during it, so the gaps come
        # out before anything is counted.
        for worked_start, worked_end in _minus_idle(start, end, idles):
            for day, seconds in _split_by_local_day(worked_start, worked_end, tz):
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

    idles = _idle_intervals(db, user, window_start, window_end)

    totals = {}
    for session in _overlapping(db, user, window_start, window_end):
        start, end = _span(session, now)
        start, end = max(start, window_start), min(end, window_end)
        seconds = sum(int((b - a).total_seconds())
                      for a, b in _minus_idle(start, end, idles))
        if seconds > 0:
            totals[session.project] = totals.get(session.project, 0) + seconds
    return [{'project': p, 'total_seconds': s}
            for p, s in sorted(totals.items(), key=lambda kv: -kv[1])]


def session_tracked_seconds(db, user, session, now=None):
    """What this session has actually counted, breaks removed.

    Not the wall clock since it opened. Once a session can pause, the two
    diverge by the length of every break, and showing the wall clock next to a
    day total that excludes them makes the page contradict itself.
    """
    now = now or datetime.now(UTC)
    start, end = _span(session, now)
    if end <= start:
        return 0
    idles = _idle_intervals(db, user, start, end)
    return sum(int((b - a).total_seconds()) for a, b in _minus_idle(start, end, idles))


def activity_summary(db, user, first_day, last_day, now=None):
    """How much of the tracked time over a range had a person in it.

    Deliberately a ratio of totals rather than an average of percentages: a
    window covering two minutes and one covering ten are not equal evidence,
    and averaging them lets a sliver of a window swing the day.

    percent is None when nothing was tracked. "No data" and "did nothing" are
    different answers, and a page that renders them both as 0% is lying about
    one of them.
    """
    window_start, window_end = range_window(user, first_day, last_day)
    rows = (db.query(ActivityWindow)
            .filter(ActivityWindow.user_id == user.id,
                    ActivityWindow.started_at < window_end,
                    ActivityWindow.ended_at > window_start)
            .all())
    active = sum(r.active_minutes for r in rows)
    tracked = sum(r.tracked_minutes for r in rows)
    return {
        'active_minutes': active,
        'tracked_minutes': tracked,
        'percent': round(100 * active / tracked) if tracked else None,
        'windows': len(rows),
    }


def activity_for_instants(db, user, instants):
    """{instant: percent} for the window each instant falls in.

    One query for a whole gallery page rather than one per thumbnail — a
    screenshot page is twenty or more images and a query each would be the
    slowest page in the app.
    """
    if not instants:
        return {}
    rows = (db.query(ActivityWindow)
            .filter(ActivityWindow.user_id == user.id,
                    ActivityWindow.started_at <= max(instants),
                    ActivityWindow.ended_at > min(instants))
            .all())
    found = {}
    for instant in instants:
        for row in rows:
            if row.started_at <= instant < row.ended_at:
                found[instant] = row.percent
                break
    return found


def monthly_totals(db, user, year, now=None):
    """One row per month of `year`, in the user's own timezone.

    Built on daily_totals rather than on its own query, so a session running
    past midnight, a break taken at lunch and a person in another timezone are
    all handled the way they are everywhere else. A second implementation of
    "how long was that" is how two pages start disagreeing.

    Future months are present and empty rather than absent: a year with a hole
    in it reads as missing data, and a year that stops in August reads as a
    person who left.
    """
    now = now or datetime.now(UTC)
    today = logical_today(user, now)
    first, last = date(year, 1, 1), date(year, 12, 31)

    daily = daily_totals(db, user, first, last, now=now)
    activity = _activity_by_month(db, user, first, last)

    months = []
    for month in range(1, 13):
        days = {day: seconds for day, seconds in daily.items()
                if day.year == year and day.month == month}
        total = sum(days.values())
        active, tracked = activity.get(month, (0, 0))
        months.append({
            'month': month,
            'label': date(year, month, 1).strftime('%b'),
            'name': date(year, month, 1).strftime('%B'),
            'total_seconds': total,
            # Days actually worked, not days in the month — the honest
            # denominator for "how much on a working day".
            'worked_days': sum(1 for seconds in days.values() if seconds > 0),
            'average_seconds': total // max(sum(1 for s in days.values() if s > 0), 1),
            'activity_percent': round(100 * active / tracked) if tracked else None,
            'is_current': year == today.year and month == today.month,
            'is_future': date(year, month, 1) > today.replace(day=1),
        })
    return months


def _activity_by_month(db, user, first_day, last_day):
    """{month: (active_minutes, tracked_minutes)} across a local-day range.

    One query for the year. A query per month would be twelve round trips for
    a page somebody opens to glance at.
    """
    window_start, window_end = range_window(user, first_day, last_day)
    tz = user_tz(user)
    rows = (db.query(ActivityWindow)
            .filter(ActivityWindow.user_id == user.id,
                    ActivityWindow.started_at < window_end,
                    ActivityWindow.ended_at > window_start)
            .all())

    by_month = {}
    for row in rows:
        # The month it began in, locally. A window straddling a month boundary
        # is ten minutes long; splitting it would be precision nobody can use.
        month = row.started_at.astimezone(tz).month
        active, tracked = by_month.get(month, (0, 0))
        by_month[month] = (active + row.active_minutes,
                           tracked + row.tracked_minutes)
    return by_month


def year_summary(db, user, year, now=None):
    """The twelve months, plus what a person reads first."""
    months = monthly_totals(db, user, year, now=now)
    real = [m for m in months if m['total_seconds'] > 0]
    total = sum(m['total_seconds'] for m in months)
    return {
        'year': year,
        'months': months,
        'total_seconds': total,
        'worked_days': sum(m['worked_days'] for m in months),
        'best_month': max(real, key=lambda m: m['total_seconds']) if real else None,
        'average_seconds': total // len(real) if real else 0,
    }


def current_status(db, user, now=None):
    """What the dashboard shows at the top: are they working right now."""
    now = now or datetime.now(UTC)
    open_session = (db.query(Session)
                    .filter(Session.user_id == user.id, Session.ended_at.is_(None))
                    .one_or_none())
    paused_since = open_session.idle_since if open_session else None
    return {
        'is_tracking': open_session is not None,
        # Still the same session — it is simply not counting at the moment.
        'is_paused': paused_since is not None,
        'paused_since': paused_since,
        'project': open_session.project if open_session else None,
        'task': open_session.task if open_session else None,
        'started_at': open_session.started_at if open_session else None,
        # Wall clock since the session opened — kept because "running since
        # 09:00" is a different and still useful fact.
        'elapsed_seconds': (int((now - open_session.started_at).total_seconds())
                            if open_session else 0),
        # What it has actually counted. This is the one to put on screen next
        # to a day total, because it is measured the same way.
        'tracked_seconds': (session_tracked_seconds(db, user, open_session, now)
                            if open_session else 0),
        'paused_seconds': (int((now - paused_since).total_seconds())
                           if paused_since else 0),
        'last_heartbeat_at': open_session.last_heartbeat_at if open_session else None,
    }


def format_hm(seconds):
    hours, rest = divmod(int(seconds), 3600)
    return f'{hours}h {rest // 60:02d}m' if hours else f'{rest // 60}m'
