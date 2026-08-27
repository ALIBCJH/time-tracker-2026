"""Application factory."""
import os
from datetime import timedelta

from flask import Flask, jsonify, redirect, url_for
from flask_login import LoginManager
from flask_wtf.csrf import CSRFProtect

from app import config
from app.db import db_session
from app.models import User
from app.services import storage as storage_module

login_manager = LoginManager()
csrf = CSRFProtect()


def create_app(**overrides):
    app = Flask(__name__)
    app.config.update(
        SECRET_KEY=config.SECRET_KEY or os.urandom(32),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE='Lax',
        # Only sent over HTTPS in production. Off in development because there
        # is no TLS on localhost and the cookie would never be set at all.
        SESSION_COOKIE_SECURE=os.environ.get('FLASK_ENV') == 'production',
        # Flask checks the signature's age against this when it loads a
        # session, so the limit is enforced here rather than trusted to the
        # browser's cookie expiry. Refreshed on every request, which is what
        # makes it idle time rather than a fixed countdown.
        PERMANENT_SESSION_LIFETIME=timedelta(hours=config.SESSION_IDLE_HOURS),
        SESSION_REFRESH_EACH_REQUEST=True,
        WTF_CSRF_TIME_LIMIT=None,
        # A capture is ~120KB as WebP; the ceiling is generous enough for a
        # 4K screen and tight enough that a broken client cannot post a DVD.
        MAX_CONTENT_LENGTH=12 * 1024 * 1024,
    )
    app.config.update(overrides)

    app.storage = storage_module.build({**os.environ,
                                        'SECRET_KEY': app.config['SECRET_KEY']})

    login_manager.init_app(app)
    login_manager.login_view = 'auth.login'
    csrf.init_app(app)

    from app.auth.routes import bp as auth_bp
    app.register_blueprint(auth_bp)

    # Agent ingest is authenticated by bearer token, not by a session cookie, so
    # CSRF — which protects cookie-authenticated requests — does not apply and
    # would only reject every upload.
    from app.api import bp as api_bp
    app.register_blueprint(api_bp, url_prefix='/api/agent')
    csrf.exempt(api_bp)

    from app.dashboard import bp as dash_bp
    app.register_blueprint(dash_bp)

    from app.cli import register as register_cli
    register_cli(app)

    @app.teardown_appcontext
    def remove_session(exception=None):
        db_session.remove()

    from app.media import bp as media_bp
    app.register_blueprint(media_bp)

    @app.get('/healthz')
    def healthz():
        """Liveness: is this process answering at all.

        Deliberately does NOT touch the database. A liveness check that fails
        when Postgres is briefly down makes the orchestrator kill and restart
        every healthy web container at exactly the wrong moment.
        """
        return jsonify({'status': 'ok'})

    @app.get('/readyz')
    def readyz():
        """Readiness: can this process actually serve a request.

        This one does touch the database, which is the difference — a process
        that cannot reach Postgres should be taken out of rotation, but not
        killed.
        """
        from sqlalchemy import text
        try:
            db_session.execute(text('SELECT 1'))
            return jsonify({'status': 'ready'})
        except Exception as e:
            return jsonify({'status': 'not-ready', 'reason': str(e)[:200]}), 503

    return app


@login_manager.user_loader
def load_user(user_id):
    """The signed-in user, or None if this session no longer stands.

    A session records a fingerprint of the password hash it was created under.
    Changing a password changes the hash, which changes the fingerprint, which
    makes every other session stop resolving to anybody — so "change my
    password" actually ends the sessions somebody else may be holding, which is
    the entire reason a person changes it in a hurry.

    Stateless, so it costs no table and no cleanup: the cookie carries the
    fingerprint and the database carries the truth.
    """
    import uuid

    from flask import session as flask_session

    from app.auth.passwords import session_fingerprint

    try:
        user = db_session.get(User, uuid.UUID(str(user_id)))
    except (ValueError, TypeError):
        return None
    if user is None:
        return None
    # Absent rather than wrong is still refused: a session predating this check
    # cannot be told apart from one being replayed, and the cost of refusing is
    # one sign-in.
    if flask_session.get('pw') != session_fingerprint(user.password_hash):
        return None
    return user


@login_manager.unauthorized_handler
def unauthorized():
    return redirect(url_for('auth.login'))
