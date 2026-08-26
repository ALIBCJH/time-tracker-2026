"""Login and logout for people."""
from flask import (Blueprint, flash, redirect, render_template, request,
                   session, url_for)
from flask_login import current_user, login_required, login_user, logout_user

from app.auth.passwords import verify_password
from app.db import db_session
from app.models import User
from app.ratelimit import LOGIN_ATTEMPTS, clear, hit
from app.services.users import normalise_email

bp = Blueprint('auth', __name__)

GENERIC_FAILURE = 'Email or password is incorrect.'


@bp.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard.index'))

    if request.method == 'POST':
        email = normalise_email(request.form.get('email'))
        password = request.form.get('password') or ''

        # Counted per address AND per source, so one attacker cannot lock every
        # account out by guessing at each in turn, and cannot spread an attack
        # across addresses to stay under a single limit either.
        source = (request.headers.get('X-Forwarded-For', request.remote_addr or '')
                  .split(',')[0].strip())
        allowed_email, _ = hit(db_session, 'login-email', email, LOGIN_ATTEMPTS)
        allowed_source, _ = hit(db_session, 'login-ip', source, LOGIN_ATTEMPTS * 5)
        if not (allowed_email and allowed_source):
            flash('Too many attempts. Try again in a few minutes.', 'error')
            return render_template('login.html', email=email), 429

        user = db_session.query(User).filter(User.email == email).one_or_none()

        # One message for every failure — unknown email, wrong password and
        # deactivated account are indistinguishable to the caller. Anything more
        # specific is an account-enumeration oracle.
        if user is None or not user.is_active or not verify_password(password, user.password_hash):
            flash(GENERIC_FAILURE, 'error')
            return render_template('login.html', email=email), 401

        # New session id on privilege change, so a cookie captured before login
        # is worthless afterwards.
        # Someone who mistypes twice and then gets it right should not be left
        # sitting next to a lockout.
        clear(db_session, 'login-email', email)
        session.clear()
        login_user(user, remember=False)
        return redirect(_safe_next() or url_for('dashboard.index'))

    return render_template('login.html', email='')


@bp.post('/logout')
@login_required
def logout():
    logout_user()
    session.clear()
    return redirect(url_for('auth.login'))


def _safe_next():
    """Only same-site relative paths. An absolute or scheme-relative URL here is
    an open redirect, which is how a convincing phishing link gets built out of
    a legitimate domain."""
    target = request.args.get('next') or ''
    if target.startswith('/') and not target.startswith('//'):
        return target
    return None
