"""Rendering a report payload into an email.

Charts reach the reader as raster images, but HOW an image is referenced
depends on where the HTML is going: a mail client needs a cid: part, a browser
preview needs a data: URI. The caller supplies an `embed` function and the
templates just ask for a src — which is what lets the preview page and the real
email be the same rendering rather than two that drift apart.
"""
import base64
from datetime import datetime, timezone

from flask import render_template

from app.reports import charts
from app.reports.data import percent_change
from app.services.reporting import format_hm

UTC = timezone.utc

COLOURS = {
    'font': "system-ui,-apple-system,'Segoe UI',Arial,sans-serif",
    'ink': '#0b0b0b', 'ink2': '#52514e', 'muted': '#898781',
    'line': '#e1e0d9', 'track': '#f2f1ee', 'blue': '#2a78d6',
    'good': '#006300',
}
WEEKLY_ACCENT = '#2a78d6'
# A different accent so a monthly report never reads as a weekly one arriving
# early. They are different documents and should look it.
MONTHLY_ACCENT = '#7a5cd6'


def cid_embedder(images):
    def embed(name, png):
        images[name] = png
        return f'cid:{name}'
    return embed


def data_uri_embedder():
    def embed(name, png):
        return 'data:image/png;base64,' + base64.b64encode(png).decode()
    return embed


def _base(user, now):
    return {
        'C': COLOURS, 'hm': format_hm, 'colour_of': charts.palette_hex,
        'sent_at': now.astimezone(_tz(user)).strftime('%A, %B %d, %Y at %I:%M %p %Z'),
    }


def _tz(user):
    from app.services.reporting import user_tz
    return user_tz(user)


def render_weekly(report, now=None, embed=None):
    now = now or datetime.now(UTC)
    user = report['user']

    delta = None
    difference = report['total_seconds'] - report['previous_seconds']
    if report['previous_seconds'] > 0 and abs(difference) >= 60:
        up = difference > 0
        delta = {'seconds': abs(difference), 'word': 'more' if up else 'less',
                 'arrow': '&#9650;' if up else '&#9660;',
                 'colour': COLOURS['good'] if up else COLOURS['ink2']}

    chart_daily = chart_mix = None
    if embed:
        past = [{'label': d['label'], 'seconds': d['total_seconds']}
                for d in report['days'] if not d['is_future']]
        peak_index = max(range(len(past)), key=lambda i: past[i]['seconds']) if past else None
        png = charts.bars(past, highlight=peak_index)
        if png:
            chart_daily = embed('daily', png)
        categories = [{'label': c['category'], 'seconds': c['seconds']}
                      for c in report['activities']['categories']]
        png = charts.donut(categories)
        if png:
            chart_mix = embed('mix', png)

    html = render_template(
        'email/weekly.html', r=report, delta=delta,
        chart_daily=chart_daily, chart_mix=chart_mix,
        project_rows=[{'label': p['project'], 'seconds': p['total_seconds']}
                      for p in report['projects']],
        kicker='Weekly Time Report', accent=WEEKLY_ACCENT, title_size=24,
        title=_pretty_range(report['week_start'], report['week_end']),
        **_base(user, now))

    subject = f"Weekly Time Report — Week of {report['week_start'].strftime('%B %d, %Y')}"
    return subject, html


def render_monthly(report, now=None, embed=None):
    now = now or datetime.now(UTC)
    user = report['user']

    badge = None
    if report['percent_change'] is not None:
        up = report['percent_change'] >= 0
        badge = {'percent': f"{abs(report['percent_change']):.1f}",
                 'arrow': '&#9650;' if up else '&#9660;',
                 'colour': COLOURS['good'] if up else COLOURS['ink2'],
                 'background': '#eaf3ea' if up else COLOURS['track']}

    chart_streams = chart_weeks = None
    if embed:
        png = charts.donut([{'label': s['name'], 'seconds': s['seconds']}
                            for s in report['streams']])
        if png:
            chart_streams = embed('streams', png)
        png = charts.bars([{'label': w['label'], 'seconds': w['seconds']}
                           for w in report['weeks']])
        if png:
            chart_weeks = embed('weeks', png)

    best = max(report['weeks'], key=lambda w: w['seconds'], default=None)
    best = best if best and best['seconds'] else None

    # The donut is drawn from described time, which is only ever a subset of
    # tracked time. Say so under the heading rather than letting the wedges
    # imply they add up to the number in the hero.
    coverage = ''
    if report['streams'] and report['described_seconds'] < report['total_seconds']:
        days = report['described_days']
        coverage = (f"From {days} described {'day' if days == 1 else 'days'} — "
                    f"{format_hm(report['described_seconds'])} of the "
                    f"{format_hm(report['total_seconds'])} tracked")
    elif not report['streams']:
        coverage = 'By project — no daily logs described for this month'

    # Sent on the last day, the month is not strictly over. State the window
    # actually measured rather than implying a whole calendar month.
    local_now = now.astimezone(_tz(user))
    closed = local_now.date() > report['month_end']
    window = (f"{report['month_start'].strftime('%B %d')} 00:00 – "
              f"{report['month_end'].strftime('%B %d')} "
              f"{'23:59' if closed else local_now.strftime('%H:%M')}")
    if not closed:
        window += ' · sent before the final hours of the month closed'

    html = render_template(
        'email/monthly.html', r=report, badge=badge, best=best, coverage=coverage,
        window=window, chart_streams=chart_streams, chart_weeks=chart_weeks,
        stream_rows=([{'label': s['name'], 'seconds': s['seconds']} for s in report['streams']]
                     or [{'label': p['project'], 'seconds': p['total_seconds']}
                         for p in report['projects']]),
        kicker='Monthly Report', accent=MONTHLY_ACCENT, title_size=30,
        title=report['label'], **_base(user, now))

    subject = f"Monthly Time Report — {report['label']}"
    return subject, html


def _pretty_range(start, end):
    """'July 18 – 24, 2026', handling month and year seams."""
    if start.year != end.year:
        return f"{start.strftime('%B %d, %Y')} – {end.strftime('%B %d, %Y')}"
    if start.month != end.month:
        return f"{start.strftime('%B %d')} – {end.strftime('%B %d')}, {end.year}"
    return f"{start.strftime('%B %d')} – {end.day}, {end.year}"
