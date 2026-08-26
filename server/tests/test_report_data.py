"""What a report says.

Kept separate from how it looks: these numbers are worth testing and table
markup is not. In the local app they were the same 400-line function.
"""
import uuid
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from app.models import ActivityLog, Session
from app.reports import data as D
from app.services.users import create_user

UTC = timezone.utc
NBO = ZoneInfo('Africa/Nairobi')
MONDAY = date(2026, 8, 17)
AFTER = datetime(2026, 9, 15, 12, tzinfo=NBO)


@pytest.fixture
def user(db, password):
    return create_user(db, 'a@example.com', 'A', password,
                       timezone_name='Africa/Nairobi')


def work(db, user, day, hours, project='Alpha'):
    start = datetime.combine(day, datetime.min.time(), tzinfo=NBO) + timedelta(hours=9)
    db.add(Session(user_id=user.id, client_uuid=uuid.uuid4(), project=project,
                   started_at=start, ended_at=start + timedelta(hours=hours)))
    db.commit()


def log(db, user, day, entries, status='confirmed'):
    db.add(ActivityLog(user_id=user.id, log_date=day, status=status,
                       activities=entries, headline='x',
                       tracked_seconds=sum(e['seconds'] for e in entries),
                       created_at=datetime.now(UTC)))
    db.commit()


# ── Streams ──────────────────────────────────────────────────────────────────

STREAMS = [('Content Evangelism', ['content-evangelism', 'vercel']),
           ('TimeTracker', ['timetracker'])]


def acts(*pairs):
    return {'labels': [{'label': l, 'seconds': s, 'category': 'Coding'}
                       for l, s in pairs]}


def test_labels_fold_into_their_stream():
    got = D.stream_totals(acts(('content-evangelism', 100), ('Vercel', 50),
                               ('TimeTracker', 30)), STREAMS, 'Deep Research')
    assert [(r['name'], r['seconds']) for r in got] == \
        [('Content Evangelism', 150), ('TimeTracker', 30)]


def test_matching_is_case_insensitive_substring():
    got = D.stream_totals(acts(('TIMETRACKER widget', 60)), STREAMS, 'Deep Research')
    assert got[0]['name'] == 'TimeTracker'


def test_the_first_matching_stream_wins():
    """Order is the tie-breaker, so a specific stream can sit above a broad one."""
    streams = [('Specific', ['timetracker widget']), ('Broad', ['timetracker'])]
    assert D.stream_totals(acts(('TimeTracker widget', 10)), streams, 'X')[0]['name'] \
        == 'Specific'


def test_nothing_is_dropped(self=None):
    """An unmatched label lands in the catch-all, so the slices always account
    for all described time."""
    got = D.stream_totals(acts(('WhatsApp', 40), ('Gmail', 20), ('TimeTracker', 5)),
                          STREAMS, 'Deep Research')
    assert [(r['name'], r['seconds']) for r in got] == \
        [('Deep Research', 60), ('TimeTracker', 5)]


def test_no_activities_means_no_streams():
    assert D.stream_totals({'labels': []}, STREAMS, 'Deep Research') == []


# ── Privacy ──────────────────────────────────────────────────────────────────

def test_private_time_is_counted_but_never_named(db, user):
    """It still belongs in the total — it just does not get a line in a report
    somebody else reads."""
    user.settings.private_labels = ['whatsapp', 'budget']
    db.commit()
    activities = acts(('Real Work', 3600), ('WhatsApp', 600), ('Weekly Budget', 300))
    named, folded, private = D.split_activities(activities, user)
    assert [n['label'] for n in named] == ['Real Work']
    assert private == 900


def test_research_beats_the_private_list(db, user):
    """A study site that looks personal at a glance is reported, not hidden."""
    user.settings.private_labels = ['budget']
    user.settings.research_labels = ['budget']
    db.commit()
    named, _, private = D.split_activities(acts(('Budget course', 600)), user)
    assert private == 0 and named[0]['category'] == 'Research'


def test_activities_beyond_the_top_are_folded(db, user):
    activities = acts(*[(f'thing {i}', 1000 - i) for i in range(20)])
    named, folded, _ = D.split_activities(activities, user, top_n=5)
    assert len(named) == 5 and folded > 0


# ── Activities over a range ──────────────────────────────────────────────────

def test_activities_are_summed_across_days(db, user):
    log(db, user, MONDAY, [{'label': 'ttcloud', 'category': 'Coding', 'seconds': 3600}])
    log(db, user, MONDAY + timedelta(days=1),
        [{'label': 'ttcloud', 'category': 'Coding', 'seconds': 1800}])
    got = D.activities_between(db, user, MONDAY, MONDAY + timedelta(days=6))
    assert got['labels'][0]['seconds'] == 5400
    assert got['days_logged'] == 2


def test_draft_days_are_included(db, user):
    """The machine half of a draft is already accurate, and excluding drafts
    would silently drop most of a week most of the time."""
    log(db, user, MONDAY, [{'label': 'x', 'category': 'Coding', 'seconds': 600}],
        status='draft')
    assert D.activities_between(db, user, MONDAY, MONDAY)['total_seconds'] == 600


def test_days_outside_the_range_are_excluded(db, user):
    log(db, user, MONDAY - timedelta(days=1),
        [{'label': 'x', 'category': 'Coding', 'seconds': 600}])
    assert D.activities_between(db, user, MONDAY, MONDAY)['total_seconds'] == 0


def test_one_users_activities_never_reach_another(db, user, password):
    other = create_user(db, 'b@example.com', 'B', password)
    log(db, other, MONDAY, [{'label': 'theirs', 'category': 'Coding', 'seconds': 600}])
    assert D.activities_between(db, user, MONDAY, MONDAY)['total_seconds'] == 0


# ── Weekly payload ───────────────────────────────────────────────────────────

def test_the_weekly_total_is_the_week_worked(db, user):
    work(db, user, MONDAY, 4)
    work(db, user, MONDAY + timedelta(days=1), 5)
    report = D.weekly(db, user, MONDAY, now=AFTER)
    assert report['total_seconds'] == 9 * 3600
    assert report['active_days'] == 2 and report['total_days'] == 7


def test_the_weekly_report_knows_the_week_before(db, user):
    work(db, user, MONDAY - timedelta(days=7), 10)
    work(db, user, MONDAY, 4)
    report = D.weekly(db, user, MONDAY, now=AFTER)
    assert report['previous_seconds'] == 10 * 3600


def test_the_peak_day_is_the_busiest(db, user):
    work(db, user, MONDAY, 2)
    work(db, user, MONDAY + timedelta(days=2), 7)
    report = D.weekly(db, user, MONDAY, now=AFTER)
    assert report['peak_day']['label'] == 'Wed'


def test_the_average_ignores_days_not_worked(db, user):
    """Dividing by seven would make every part-time week look worse than it was."""
    work(db, user, MONDAY, 4)
    work(db, user, MONDAY + timedelta(days=1), 6)
    assert D.weekly(db, user, MONDAY, now=AFTER)['average_seconds'] == 5 * 3600


# ── Monthly payload ──────────────────────────────────────────────────────────

def test_the_monthly_total_covers_the_whole_month(db, user):
    for day in (3, 10, 20, 31):
        work(db, user, date(2026, 8, day), 3)
    report = D.monthly(db, user, 2026, 8, now=AFTER)
    assert report['total_seconds'] == 12 * 3600
    assert report['total_days'] == 31


def test_the_percentage_is_against_the_month_before(db, user):
    work(db, user, date(2026, 7, 10), 10)
    work(db, user, date(2026, 8, 10), 11)
    report = D.monthly(db, user, 2026, 8, now=AFTER)
    assert round(report['percent_change'], 1) == 10.0
    assert report['previous_label'] == 'July'


def test_there_is_no_percentage_without_a_previous_month(db, user):
    """'Up 100%' from nothing is meaningless; saying so is more honest than
    inventing a number."""
    work(db, user, date(2026, 8, 10), 5)
    assert D.monthly(db, user, 2026, 8, now=AFTER)['percent_change'] is None


def test_week_buckets_are_clipped_to_the_month(db, user):
    """August 2026 opens on a Saturday and closes on a Monday, so the first
    bucket is two days and the last is one."""
    report = D.monthly(db, user, 2026, 8, now=AFTER)
    assert [b['days'] for b in report['weeks']] == [2, 7, 7, 7, 7, 1]
    assert report['weeks'][0]['label'] == '1–2'
    assert report['weeks'][-1]['label'] == '31'


def test_week_buckets_sum_to_the_month(db, user):
    for day in (1, 5, 12, 19, 26, 31):
        work(db, user, date(2026, 8, day), 2)
    report = D.monthly(db, user, 2026, 8, now=AFTER)
    assert sum(b['seconds'] for b in report['weeks']) == report['total_seconds']


def test_the_monthly_report_carries_its_streams(db, user):
    user.settings.streams = [['Content Evangelism', ['content-evangelism']]]
    db.commit()
    log(db, user, date(2026, 8, 10),
        [{'label': 'content-evangelism', 'category': 'Coding', 'seconds': 7200},
         {'label': 'WhatsApp', 'category': 'Web', 'seconds': 1800}])
    report = D.monthly(db, user, 2026, 8, now=AFTER)
    assert [(s['name'], s['seconds']) for s in report['streams']] == \
        [('Content Evangelism', 7200), ('Deep Research', 1800)]


def test_described_time_is_reported_separately_from_tracked(db, user):
    """The donut is drawn from described time, which is only ever a subset. A
    report has to be able to say so rather than implying they are the same."""
    work(db, user, date(2026, 8, 10), 8)
    log(db, user, date(2026, 8, 10),
        [{'label': 'x', 'category': 'Coding', 'seconds': 3600}])
    report = D.monthly(db, user, 2026, 8, now=AFTER)
    assert report['total_seconds'] == 8 * 3600
    assert report['described_seconds'] == 3600
    assert report['described_days'] == 1
