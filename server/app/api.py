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
    s = g.agent_user.settings
    return jsonify({
        'user': {'email': g.agent_user.email, 'name': g.agent_user.name},
        'settings': {
            'timezone': s.timezone,
            'idle_threshold_seconds': s.idle_threshold_seconds,
            'screenshot_interval_seconds': s.screenshot_interval_seconds,
            'screenshots_enabled': s.screenshots_enabled,
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
