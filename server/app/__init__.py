"""Application factory."""
import os

from flask import Flask, jsonify, redirect, url_for
from flask_login import LoginManager
from flask_wtf.csrf import CSRFProtect

from app import config
from app.db import db_session
from app.models import User

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
        WTF_CSRF_TIME_LIMIT=None,
    )
    app.config.update(overrides)

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

    @app.get('/healthz')
    def healthz():
        return jsonify({'status': 'ok'})

    return app


@login_manager.user_loader
def load_user(user_id):
    import uuid
    try:
        return db_session.get(User, uuid.UUID(str(user_id)))
    except (ValueError, TypeError):
        return None


@login_manager.unauthorized_handler
def unauthorized():
    return redirect(url_for('auth.login'))
