"""Person-facing pages. Session cookie only.

Every view resolves whose data it is showing before it reads anything. A worker
sees themselves and nothing else; an admin may name someone else, and then sees
that person's days in THAT PERSON's timezone — not their own, because when
Benson's Tuesday started is a fact about Benson.
"""
from datetime import date, datetime, timedelta, timezone

from flask import Blueprint, abort, jsonify, render_template, request
from flask_login import current_user, login_required

from app.auth.decorators import admin_required
from app.db import db_session
from app.models import Session, User
from app.services import reporting as R

bp = Blueprint('dashboard', __name__)


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
