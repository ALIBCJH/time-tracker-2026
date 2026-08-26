"""Agent-facing API. Bearer token only — no session cookies reach here.

Every route reads the user from g.agent_user, set by @agent_required from the
token. Nothing takes a user id from the request body: a token identifies
exactly one account, and that is the only account it can ever write to.
"""
from datetime import datetime, timezone

from flask import Blueprint, g, jsonify, request

from app.auth.decorators import agent_required
from app.db import db_session
from app.services.ingest import ingest_batch

bp = Blueprint('api', __name__)


@bp.post('/heartbeat')
@agent_required
def heartbeat():
    """Proof of life. Also how the server learns an agent is still running,
    which is what an abandoned open session gets capped at."""
    g.device.last_seen_at = datetime.now(timezone.utc)
    db_session.commit()
    return jsonify({
        'ok': True,
        'user': g.agent_user.email,
        'device': g.device.name,
        'server_time': datetime.now(timezone.utc).isoformat(),
    })


@bp.get('/me')
@agent_required
def me():
    """What the agent needs to configure itself — all of it per-user, none of
    it baked into the agent build."""
    from app.services.consent import is_paused

    s = g.agent_user.settings
    paused = is_paused(g.agent_user)
    return jsonify({
        'user': {'email': g.agent_user.email, 'name': g.agent_user.name},
        'paused': paused,
        'paused_until': s.tracking_paused_until.isoformat() if paused else None,
        'settings': {
            # Reported as off while paused, so an agent that only reads this
            # much still stops capturing.
            'screenshots_enabled': s.screenshots_enabled and not paused,
            'tracking_enabled': not paused,
            'timezone': s.timezone,
            'idle_threshold_seconds': s.idle_threshold_seconds,
            'screenshot_interval_seconds': s.screenshot_interval_seconds,
            'day_goal_seconds': s.day_goal_seconds,
            'week_goal_seconds': s.week_goal_seconds,
        },
    })


@bp.post('/sync')
@agent_required
def sync():
    """Upload a batch of tracked data.

    Idempotent: every record carries a client_uuid, so an agent that loses the
    response to a batch simply sends it again. Partial failures are reported per
    record and the rest of the batch is still committed — an agent coming back
    from a week offline must not lose the week to one bad row.
    """
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({'error': 'body must be a JSON object'}), 400

    result = ingest_batch(db_session, g.agent_user, payload)
    g.device.last_seen_at = datetime.now(timezone.utc)
    db_session.commit()

    # 200 even with rejections: the batch was processed, and the agent needs the
    # per-record detail to decide what to drop from its spool rather than a
    # blanket failure that would make it retry good records for ever.
    return jsonify(result), 200


@bp.get('/activity-log/pending')
@agent_required
def activity_log_pending():
    """What the widget's daily card should show right now — empty most of the day.

    Presence is decided here rather than in each client so every client gets the
    same answer, but the idle counter lives on the laptop, so the agent passes
    it in.
    """
    from app.services import activity_log as AL

    raw = request.args.get('idle_seconds')
    try:
        idle_seconds = float(raw) if raw is not None else None
    except ValueError:
        idle_seconds = None
    tracking = request.args.get('tracking', 'true').lower() != 'false'

    return jsonify(AL.pending(db_session, g.agent_user,
                              idle_seconds=idle_seconds, tracking_enabled=tracking))


@bp.post('/activity-log/answer')
@agent_required
def activity_log_answer():
    """Confirm, skip, or leave a day as it was."""
    from datetime import date as _date

    from app.services import activity_log as AL

    data = request.get_json(silent=True) or {}
    try:
        day = _date.fromisoformat(data['date'])
    except (KeyError, ValueError, TypeError):
        return jsonify({'error': 'date must be YYYY-MM-DD'}), 400

    status = data.get('status', 'confirmed')
    if status not in ('confirmed', 'skipped', 'unchanged'):
        return jsonify({'error': 'unknown status'}), 400

    # 'unchanged' is "leave as is" on a top-up. It cannot go through answer() —
    # that would overwrite the note with the empty string the card sends
    # alongside it.
    if status == 'unchanged':
        if AL.rebaseline(db_session, g.agent_user, day) is None:
            return jsonify({'error': 'no answered log to settle'}), 400
        return jsonify({'ok': True})

    try:
        AL.answer(db_session, g.agent_user, day, data.get('note'), status)
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except LookupError:
        return jsonify({'error': f'no log for {day}'}), 404
    return jsonify({'ok': True})


@bp.post('/screenshot')
@agent_required
def upload_screenshot():
    """One capture: the full frame and its thumbnail, in one request.

    Uploaded separately from the JSON batch because images are large — a failed
    8MB request should cost one capture, not a day of tracked time riding along
    with it.
    """
    import uuid as _uuid
    from datetime import datetime as _dt

    from flask import current_app

    from app.models import Screenshot, Session
    from app.services import storage as storage_module
    from app.services.ingest import RecordError, parse_instant, parse_uuid

    full = request.files.get('full')
    if full is None:
        return jsonify({'error': 'a "full" image part is required'}), 400
    thumb = request.files.get('thumb')

    try:
        client_uuid = parse_uuid(request.form.get('client_uuid'), 'client_uuid')
        captured_at = parse_instant(request.form.get('captured_at'), 'captured_at')
    except RecordError as e:
        return jsonify({'error': str(e)}), 400

    from app.services.consent import is_paused

    user = g.agent_user
    if is_paused(user):
        # Enforced here, not by asking the agent nicely. Someone who pauses
        # should not have to trust a program on their machine to honour it.
        return jsonify({'error': 'tracking is paused'}), 409
    if not user.settings.screenshots_enabled:
        # Refused rather than silently dropped: an agent whose captures are
        # being discarded should know, and stop taking them.
        return jsonify({'error': 'screenshots are disabled for this account'}), 409

    existing = (db_session.query(Screenshot)
                .filter(Screenshot.user_id == user.id,
                        Screenshot.client_uuid == client_uuid).one_or_none())
    if existing is not None:
        # A retry after a lost response. Storing it again would leave an orphan
        # object nothing points at.
        return jsonify({'ok': True, 'duplicate': True}), 200

    session_id = None
    reference = request.form.get('session_client_uuid')
    if reference:
        try:
            session = (db_session.query(Session)
                       .filter(Session.user_id == user.id,
                               Session.client_uuid == _uuid.UUID(reference))
                       .one_or_none())
            session_id = session.id if session else None
        except (ValueError, TypeError):
            session_id = None

    store = current_app.storage
    full_bytes = full.read()
    full_key = storage_module.key_for(user.id, captured_at, client_uuid, 'full')
    store.put(full_key, full_bytes)

    thumb_key = None
    if thumb is not None:
        thumb_key = storage_module.key_for(user.id, captured_at, client_uuid, 'thumb')
        store.put(thumb_key, thumb.read())

    db_session.add(Screenshot(user_id=user.id, client_uuid=client_uuid,
                              session_id=session_id, captured_at=captured_at,
                              full_key=full_key, thumb_key=thumb_key,
                              bytes_full=len(full_bytes)))
    g.device.last_seen_at = _dt.now(timezone.utc)
    db_session.commit()
    return jsonify({'ok': True, 'key': full_key}), 201
