"""Person-facing pages. Session cookie only.

Every view resolves whose data it is showing before it reads anything. A worker
sees themselves and nothing else; an admin may name someone else, and then sees
that person's days in THAT PERSON's timezone — not their own, because when
Benson's Tuesday started is a fact about Benson.
"""
from datetime import date, datetime, timedelta, timezone

from flask import (Blueprint, abort, flash, jsonify, redirect,
                   render_template, request, url_for)
from flask_login import current_user, login_required

from app.auth.decorators import admin_required
from app.db import db_session
from app.models import Session, User
from app.services import reporting as R

bp = Blueprint('dashboard', __name__)

# Pages reachable before the policy has been accepted. Everything else is
# gated: someone must be able to read what is collected, and to leave, without
# first agreeing to it.
CONSENT_EXEMPT = {'dashboard.consent', 'dashboard.accept_consent'}


@bp.before_request
def require_consent():
    """Nothing is shown until the policy is accepted.

    The gate is on the dashboard, not the API, and that is on purpose: a person
    who has not yet agreed should see the policy rather than their colleague's
    week, but an agent already installed must keep uploading rather than
    silently losing a day while someone reads a page.
    """
    from app.services.consent import has_consented

    if not current_user.is_authenticated:
        return None
    if request.endpoint in CONSENT_EXEMPT:
        return None
    if has_consented(db_session, current_user):
        return None
    return redirect(url_for('dashboard.consent'))


def _subject():
    """Whose data this request is about.

    Defaults to the signed-in person. An admin may pass ?user=<id>; anyone else
    trying that gets their own data, not an error — there is nothing to tell
    them, and a 403 would confirm the other account exists.
    """
    wanted = request.args.get('user')
    if wanted and current_user.is_admin:
        subject = db_session.get(User, wanted) if _is_uuid(wanted) else None
        if subject is None:
            abort(404)
        return subject
    return current_user


def _is_uuid(value):
    import uuid
    try:
        uuid.UUID(str(value))
        return True
    except (ValueError, TypeError):
        return False


@bp.get('/')
@login_required
def index():
    user = _subject()
    now = datetime.now(timezone.utc)
    today = R.logical_today(user, now)

    return render_template(
        'index.html',
        subject=user,
        viewing_other=user.id != current_user.id,
        today=today,
        status=R.current_status(db_session, user, now),
        day=R.day_summary(db_session, user, today, now),
        week=R.week_summary(db_session, user, now=now),
        projects=R.project_totals(db_session, user, R.week_start(today), today, now),
        hm=R.format_hm,
    )


@bp.get('/history')
@login_required
def history():
    user = _subject()
    sessions = (db_session.query(Session)
                .filter(Session.user_id == user.id)
                .order_by(Session.started_at.desc())
                .limit(100).all())
    tz = R.user_tz(user)
    now = datetime.now(timezone.utc)
    rows = [{
        'project': s.project, 'task': s.task,
        'started': s.started_at.astimezone(tz),
        'ended': s.ended_at.astimezone(tz) if s.ended_at else None,
        'seconds': int(((s.ended_at or now) - s.started_at).total_seconds()),
    } for s in sessions]
    return render_template('history.html', subject=user, rows=rows,
                           viewing_other=user.id != current_user.id, hm=R.format_hm)


@bp.get('/api/status')
@login_required
def api_status():
    """What the page polls. Deliberately small — it is fetched every few
    seconds and must not re-run the week's aggregation each time."""
    user = _subject()
    now = datetime.now(timezone.utc)
    status = R.current_status(db_session, user, now)
    return jsonify({
        'is_tracking': status['is_tracking'],
        'project': status['project'],
        'elapsed_seconds': status['elapsed_seconds'],
        'today_seconds': R.day_summary(db_session, user, now=now)['total_seconds'],
        'server_time': now.isoformat(),
    })


@bp.get('/admin/team')
@admin_required
def team():
    """Everyone, with today and this week — each in their own timezone."""
    from app.services import alerts as A

    now = datetime.now(timezone.utc)
    people = db_session.query(User).order_by(User.name).all()

    rows = []
    for person in people:
        today = R.logical_today(person, now)
        week = R.week_summary(db_session, person, now=now)
        status = R.current_status(db_session, person, now)
        rows.append({
            'user': person,
            'local_date': today,
            'today_seconds': R.day_summary(db_session, person, today, now)['total_seconds'],
            'week_seconds': week['total_seconds'],
            'status': status,
            'agent': A.device_health(db_session, person, now),
        })
    return render_template('team.html', rows=rows, hm=R.format_hm)


@bp.get('/log')
@login_required
def activity_log():
    """The narrative: what was said about each day, next to what was observed."""
    from app.services import activity_log as AL

    user = _subject()
    return render_template('log.html', subject=user,
                           entries=AL.history(db_session, user, limit=60),
                           viewing_other=user.id != current_user.id,
                           hm=R.format_hm)


@bp.get('/reports/preview/<kind>')
@login_required
def report_preview(kind):
    """Render a report exactly as it will be sent, in the browser.

    Same rendering as the real email — only the way images are referenced
    differs — so the preview cannot drift away from what actually arrives.
    """
    from datetime import date as _date

    from app.reports import data as RD
    from app.reports import render as RR
    from app.reports import schedule as RS

    if kind not in ('weekly', 'monthly'):
        abort(404)

    user = _subject()
    now = datetime.now(timezone.utc)
    embed = RR.data_uri_embedder()

    if kind == 'weekly':
        raw = request.args.get('week')
        monday = (_date.fromisoformat(raw) if raw
                  else R.week_start(R.logical_today(user, now)) - timedelta(days=7))
        payload = RD.weekly(db_session, user, R.week_start(monday), now=now)
        _, html = RR.render_weekly(payload, now=now, embed=embed)
        return html

    raw = request.args.get('month')
    if raw:
        year, month = (int(part) for part in raw.split('-')[:2])
    else:
        year, month = RS.previous_month(R.logical_today(user, now))
    payload = RD.monthly(db_session, user, year, month, now=now)
    _, html = RR.render_monthly(payload, now=now, embed=embed)
    return html


@bp.get('/screenshots')
@login_required
def screenshots():
    """The capture gallery for one local day.

    Thumbnails are shown; the full frame is a click away and only exists for a
    few weeks. A day past that window still has its timeline — the thumbnails
    outlive the evidence by design.
    """
    from datetime import date as _date

    from flask import current_app

    from app.models import Screenshot

    user = _subject()
    now = datetime.now(timezone.utc)
    raw = request.args.get('date')
    try:
        day = _date.fromisoformat(raw) if raw else R.logical_today(user, now)
    except ValueError:
        abort(404)

    start, end = R.day_window(user, day)
    shots = (db_session.query(Screenshot)
             .filter(Screenshot.user_id == user.id,
                     Screenshot.captured_at >= start, Screenshot.captured_at < end)
             .order_by(Screenshot.captured_at).all())

    store = current_app.storage
    tz = R.user_tz(user)
    items = []
    for shot in shots:
        # An expired full frame is reported as expired rather than rendered as
        # a broken image — the difference between "gone on purpose" and "lost".
        expired = shot.full_deleted_at is not None or not store.exists(shot.full_key)
        items.append({
            'at': shot.captured_at.astimezone(tz),
            'thumb': store.signed_url(shot.thumb_key) if shot.thumb_key else None,
            'full': None if expired else store.signed_url(shot.full_key),
            'expired': expired,
        })

    return render_template('screenshots.html', subject=user, day=day, items=items,
                           viewing_other=user.id != current_user.id,
                           prev_day=day - timedelta(days=1),
                           next_day=day + timedelta(days=1))


# ── Consent, pause, settings ─────────────────────────────────────────────────

@bp.get('/consent')
@login_required
def consent():
    from app.services import consent as C
    return render_template('consent.html', collected=C.COLLECTED,
                           who_can_see=C.WHO_CAN_SEE, version=C.POLICY_VERSION,
                           already=C.has_consented(db_session, current_user),
                           history=C.history(db_session, current_user))


@bp.post('/consent')
@login_required
def accept_consent():
    from app.services import consent as C
    source = (request.headers.get('X-Forwarded-For', request.remote_addr or '')
              .split(',')[0].strip())
    C.record(db_session, current_user, source_ip=source)
    return redirect(url_for('dashboard.index'))


@bp.post('/pause')
@login_required
def pause():
    """Only ever the signed-in person's own tracking.

    An admin cannot pause or resume somebody else — the control belongs to the
    person being recorded, and a switch someone else can flip is not a control.
    """
    from app.services import consent as C

    action = request.form.get('action')
    reason = request.form.get('reason')
    if action == 'resume':
        C.resume(db_session, current_user)
        flash('Tracking resumed.', 'ok')
    elif action == 'indefinite':
        C.pause_indefinitely(db_session, current_user, reason)
        flash('Tracking paused until you resume it.', 'ok')
    else:
        raw = request.form.get('minutes')
        minutes = int(raw) if raw and raw.isdigit() else None
        C.pause(db_session, current_user, minutes, reason)
        flash('Tracking paused.', 'ok')
    return redirect(url_for('dashboard.settings'))


@bp.get('/settings')
@login_required
def settings():
    from app.services import consent as C
    return render_template('settings.html', s=current_user.settings,
                           paused=C.is_paused(current_user),
                           presets=C.PAUSE_PRESETS, hm=R.format_hm)


@bp.post('/settings')
@login_required
def save_settings():
    """Your own settings only — there is no ?user= here at all."""
    from zoneinfo import ZoneInfo, available_timezones

    s = current_user.settings
    form = request.form

    zone = (form.get('timezone') or '').strip()
    if zone and zone in available_timezones():
        s.timezone = zone

    for field, low, high in (('day_goal_hours', 1, 24), ('week_goal_hours', 1, 168)):
        raw = form.get(field)
        if raw and raw.replace('.', '', 1).isdigit() and low <= float(raw) <= high:
            setattr(s, field.replace('_hours', '_seconds'), int(float(raw) * 3600))

    raw = form.get('idle_threshold_minutes')
    if raw and raw.isdigit() and 1 <= int(raw) <= 120:
        s.idle_threshold_seconds = int(raw) * 60

    raw = form.get('screenshot_interval_minutes')
    if raw and raw.isdigit() and 1 <= int(raw) <= 120:
        s.screenshot_interval_seconds = int(raw) * 60

    s.screenshots_enabled = form.get('screenshots_enabled') == 'on'
    s.reports_enabled = form.get('reports_enabled') == 'on'
    s.offline_alerts_enabled = form.get('offline_alerts_enabled') == 'on'
    s.private_labels = _lines(form.get('private_labels'))
    s.research_labels = _lines(form.get('research_labels'))
    s.streams = _streams(form.get('streams'))
    s.catch_all_stream = (form.get('catch_all_stream') or 'Deep Research').strip()[:64]

    db_session.commit()
    flash('Settings saved.', 'ok')
    return redirect(url_for('dashboard.settings'))


def _lines(text):
    return [line.strip() for line in (text or '').splitlines() if line.strip()]


def _streams(text):
    """One stream per line: "Name: pattern, pattern, pattern".

    A textarea rather than a form builder — this is edited a few times a year by
    three people, and the shape is easier to read than to click through.
    """
    streams = []
    for line in _lines(text):
        name, _, patterns = line.partition(':')
        parts = [p.strip() for p in patterns.split(',') if p.strip()]
        if name.strip() and parts:
            streams.append([name.strip()[:64], parts])
    return streams


@bp.post('/session')
@login_required
def session_control():
    """Start or stop your own session from the browser.

    Your own only — there is no ?user= here. An admin starting or stopping
    somebody else's tracking would be recording time on their behalf, which is
    a different thing entirely from watching what they recorded.

    The agent reconciles on its next poll: it adopts a session started here and
    closes one stopped here. That is the whole reason this can exist at all —
    without it a browser button would create a session no agent is feeding,
    which records time with nothing to attribute it to.
    """
    import uuid as _uuid

    from sqlalchemy.exc import IntegrityError

    from app.models import Session
    from app.services.consent import is_paused

    if is_paused(current_user):
        flash('Tracking is paused. Resume it first.', 'error')
        return redirect(url_for('dashboard.index'))

    now = datetime.now(timezone.utc)
    open_session = (db_session.query(Session)
                    .filter(Session.user_id == current_user.id,
                            Session.ended_at.is_(None)).one_or_none())

    if request.form.get('action') == 'stop':
        if open_session is not None:
            open_session.ended_at = now
            db_session.commit()
            flash('Session stopped.', 'ok')
        return redirect(url_for('dashboard.index'))

    if open_session is not None:
        flash('A session is already running.', 'error')
        return redirect(url_for('dashboard.index'))

    project = (request.form.get('project') or '').strip()[:120]
    if not project:
        flash('Name the project first.', 'error')
        return redirect(url_for('dashboard.index'))

    db_session.add(Session(user_id=current_user.id, client_uuid=_uuid.uuid4(),
                           project=project,
                           task=(request.form.get('task') or '').strip()[:2000],
                           started_at=now, last_heartbeat_at=now))
    try:
        db_session.commit()
    except IntegrityError:
        # The agent opened one in the same instant. Its session is the real
        # one — it is the thing actually watching the screen.
        db_session.rollback()
        flash('Your agent just started one.', 'ok')
    return redirect(url_for('dashboard.index'))
