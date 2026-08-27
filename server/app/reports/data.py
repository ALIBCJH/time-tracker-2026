"""Assembling what a report says, separately from how it looks.

Kept apart from rendering on purpose: the numbers are worth testing and the
table markup is not, and in the local app they were the same 400-line function.
"""
from datetime import date, timedelta

from app.models import ActivityLog
from app.reports.schedule import month_end
from app.services import reporting as R

# Fallbacks for a user who has not defined their own. Ordered — the FIRST
# stream whose pattern matches a label wins, so specific sits above general.
DEFAULT_STREAMS = []
DEFAULT_CATCH_ALL = 'Deep Research'

# How many named activities a report lists before folding the rest away.
TOP_ACTIVITIES = 12


# ── Activities over a range ──────────────────────────────────────────────────

def activities_between(db, user, first_day, last_day):
    """The daily logs' activities, summed across a range of local days.

    Days still in 'draft' are included: the machine half of a draft is already
    accurate, and excluding them would silently drop every day not yet answered
    — which is most of a week, most of the time.
    """
    logs = (db.query(ActivityLog)
            .filter(ActivityLog.user_id == user.id,
                    ActivityLog.log_date >= first_day,
                    ActivityLog.log_date <= last_day)
            .order_by(ActivityLog.log_date).all())

    by_label, by_category, days_logged, total = {}, {}, 0, 0
    for log in logs:
        entries = log.activities or []
        if entries:
            days_logged += 1
        for entry in entries:
            label, category = entry.get('label'), entry.get('category', 'App')
            seconds = int(entry.get('seconds', 0))
            if not label or seconds <= 0:
                continue
            key = (category, label)
            by_label[key] = by_label.get(key, 0) + seconds
            by_category[category] = by_category.get(category, 0) + seconds
            total += seconds

    return {
        'days_logged': days_logged,
        'total_seconds': total,
        'labels': [{'category': c, 'label': l, 'seconds': s}
                   for (c, l), s in sorted(by_label.items(), key=lambda kv: -kv[1])],
        'categories': [{'category': c, 'seconds': s}
                       for c, s in sorted(by_category.items(), key=lambda kv: -kv[1])],
    }


# ── Streams ──────────────────────────────────────────────────────────────────

def _configured_streams(user):
    settings = user.settings
    raw = settings.streams or DEFAULT_STREAMS
    streams = [(name, list(patterns)) for name, patterns in raw]
    return streams, settings.catch_all_stream or DEFAULT_CATCH_ALL


def stream_totals(activities, streams, catch_all):
    """Activity labels folded into named work streams, biggest first.

    This is what the monthly donut is built from, rather than the project a
    session was started under. A project is chosen before the work happens, so
    in practice it is always the default and a donut built from it has one
    slice. The activity log records what was actually in front of you.

    Nothing is dropped: an unmatched label lands in the catch-all, so the slices
    always account for all described time.
    """
    totals = {}
    for entry in activities.get('labels', []):
        label = (entry.get('label') or '').lower()
        name = catch_all
        for stream, patterns in streams:
            if any(p.lower() in label for p in patterns):
                name = stream
                break
        totals[name] = totals.get(name, 0) + entry['seconds']
    return [{'name': n, 'seconds': s}
            for n, s in sorted(totals.items(), key=lambda kv: -kv[1]) if s > 0]


def is_private(label, patterns):
    lowered = (label or '').lower()
    return any(p.lower() in lowered for p in patterns or [])


def split_activities(activities, user, top_n=TOP_ACTIVITIES):
    """(named, folded_seconds, private_seconds) for a report someone else reads.

    Private time is NOT deleted — it still counts toward the total and lands in
    the folded remainder. It simply never gets named in a report that leaves
    your machine.
    """
    settings = user.settings
    private_patterns = settings.private_labels or []
    research_patterns = settings.research_labels or []

    named, private_seconds = [], 0
    for entry in activities.get('labels', []):
        label = entry['label']
        # Research is checked first, so a study site that looks personal at a
        # glance is reported rather than hidden.
        if is_private(label, research_patterns):
            named.append({**entry, 'category': 'Research'})
        elif is_private(label, private_patterns):
            private_seconds += entry['seconds']
        else:
            named.append(entry)

    folded = sum(e['seconds'] for e in named[top_n:])
    return named[:top_n], folded, private_seconds


# ── Report payloads ──────────────────────────────────────────────────────────

def weekly(db, user, monday, now=None):
    sunday = monday + timedelta(days=6)
    previous = monday - timedelta(days=7)
    week = R.week_summary(db, user, monday, now=now)
    activities = activities_between(db, user, monday, sunday)
    named, folded, private_seconds = split_activities(activities, user)

    days = [d for d in week['days'] if not d['is_future']]
    active_days = sum(1 for d in days if d['total_seconds'] > 0)
    peak = max(days, key=lambda d: d['total_seconds'], default=None)

    return {
        'kind': 'weekly',
        'user': user,
        'week_start': monday, 'week_end': sunday,
        'total_seconds': week['total_seconds'],
        'previous_seconds': R.week_summary(db, user, previous, now=now)['total_seconds'],
        'days': week['days'],
        'projects': R.project_totals(db, user, monday, sunday, now=now),
        'activities': activities,
        'named_activities': named,
        'folded_seconds': folded,
        'private_seconds': private_seconds,
        'active_days': active_days,
        'total_days': len(days),
        'peak_day': peak,
        'average_seconds': week['total_seconds'] // active_days if active_days else 0,
        # Presence is what the hours measure; this is what was in them.
        'activity': R.activity_summary(db, user, monday, sunday, now=now),
    }


def monthly(db, user, year, month, now=None):
    first = date(year, month, 1)
    last = month_end(year, month)
    previous_year, previous_month_no = (year, month - 1) if month > 1 else (year - 1, 12)

    totals = R.daily_totals(db, user, first, last, now=now)
    days = [{'date': first + timedelta(days=i),
             'seconds': totals.get(first + timedelta(days=i), 0)}
            for i in range((last - first).days + 1)]
    total = sum(d['seconds'] for d in days)

    previous_first = date(previous_year, previous_month_no, 1)
    previous_total = sum(R.daily_totals(db, user, previous_first,
                                        month_end(previous_year, previous_month_no),
                                        now=now).values())

    activities = activities_between(db, user, first, last)
    streams, catch_all = _configured_streams(user)

    active_days = sum(1 for d in days if d['seconds'] > 0)
    return {
        'kind': 'monthly',
        'user': user,
        'year': year, 'month': month,
        'label': first.strftime('%B %Y'),
        'month_start': first, 'month_end': last,
        'total_seconds': total,
        'previous_seconds': previous_total,
        'percent_change': percent_change(total, previous_total),
        'previous_label': previous_first.strftime('%B'),
        'days': days,
        'weeks': week_buckets(days, first, last),
        'projects': R.project_totals(db, user, first, last, now=now),
        'activities': activities,
        'streams': stream_totals(activities, streams, catch_all),
        'active_days': active_days,
        'total_days': len(days),
        'average_seconds': total // active_days if active_days else 0,
        'described_days': activities['days_logged'],
        'described_seconds': activities['total_seconds'],
    }


def percent_change(total, previous):
    """None when there is no base. 'Up 100%' from nothing is meaningless, and a
    report that says so is more honest than one that invents a number."""
    if previous <= 0:
        return None
    return (total - previous) / previous * 100.0


def week_buckets(days, first, last):
    """The month's days grouped into its Mon→Sun weeks, clipped to the month.

    A 31-bar chart labelled Mon..Sun four and a half times says nothing; five
    bars labelled 3–9, 10–16 say how the month went. The edge buckets cover only
    the part of their week inside the month — a partial week is shown short
    rather than borrowed from a neighbour.
    """
    by_date = {d['date']: d['seconds'] for d in days}
    buckets, cursor = [], R.week_start(first)
    while cursor <= last:
        low, high = max(cursor, first), min(cursor + timedelta(days=6), last)
        seconds = sum(by_date.get(low + timedelta(days=i), 0)
                      for i in range((high - low).days + 1))
        buckets.append({'start': low, 'end': high, 'days': (high - low).days + 1,
                        'label': f'{low.day}' if low == high else f'{low.day}–{high.day}',
                        'seconds': seconds})
        cursor += timedelta(days=7)
    return buckets
