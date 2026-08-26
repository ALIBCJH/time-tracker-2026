"""Who is allowed to call what.

Two entirely separate identities reach this server and they must not be
confused:

  * a PERSON, authenticated by a session cookie, who may read a dashboard;
  * an AGENT, authenticated by a bearer token, which may only upload data for
    the one user it was issued to.

An agent token is therefore never accepted on a dashboard route and a session
cookie is never accepted on an ingest route. Keeping them apart is what stops a
token leaked from a laptop turning into a login.
"""
from functools import wraps

from flask import abort, g, jsonify, request
from flask_login import current_user

from app.auth.tokens import hash_token, looks_like_token
from app.db import db_session
from app.models import Device, User


def admin_required(view):
    """A person with the admin role. Anyone else gets 404, not 403 — a worker
    should not learn that an admin area exists."""
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not current_user.is_authenticated:
            abort(401)
        if current_user.role != 'admin':
            abort(404)
        return view(*args, **kwargs)
    return wrapped


def agent_required(view):
    """Authenticate an installed agent from its bearer token.

    On success g.device and g.agent_user are set. The route reads the user from
    g, NEVER from the request body: a compromised agent must be unable to write
    rows against somebody else by putting their id in the payload.
    """
    @wraps(view)
    def wrapped(*args, **kwargs):
        header = request.headers.get('Authorization', '')
        scheme, _, token = header.partition(' ')
        if scheme.lower() != 'bearer' or not looks_like_token(token):
            return jsonify({'error': 'agent token required'}), 401

        # Lookup is by hash, so the raw token is never compared in SQL and never
        # reaches a query log.
        device = (db_session.query(Device)
                  .filter(Device.token_hash == hash_token(token),
                          Device.revoked_at.is_(None))
                  .one_or_none())
        if device is None:
            return jsonify({'error': 'invalid or revoked token'}), 401

        user = db_session.get(User, device.user_id)
        if user is None or not user.is_active:
            return jsonify({'error': 'account is not active'}), 403

        g.device = device
        g.agent_user = user
        return view(*args, **kwargs)
    return wrapped
