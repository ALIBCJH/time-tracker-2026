"""How long a sign-in lasts, and what ends it.

Two properties, and both are about somebody who is not at the keyboard. A
browser left open on an unlocked desk should stop being signed in. And a person
who changes their password because they think somebody else has it should
actually end that somebody else's session, rather than only their own.
"""
import time
from datetime import timedelta

import pytest

from app.services import consent as C
from app.services.users import create_user


@pytest.fixture
def make_app(tmp_path, monkeypatch):
    """An app whose context is NOT held open across requests.

    The shared fixture wraps a whole test in one app context, which is fine for
    most things and wrong for these: flask_login caches the signed-in user on
    `g`, `g` belongs to the app context, and a context that outlives a request
    therefore remembers a user that a later request should have had to re-load.
    Production never does that — every request gets its own context — so a test
    that holds one cannot see a session expire or a session be invalidated.
    """
    monkeypatch.setenv('MEDIA_ROOT', str(tmp_path / 'media'))
    monkeypatch.delenv('S3_BUCKET', raising=False)

    from app import create_app

    def _make(**overrides):
        return create_app(TESTING=True, WTF_CSRF_ENABLED=False,
                          SECRET_KEY='test-key-not-a-secret', **overrides)
    return _make


@pytest.fixture
def app(make_app):
    return make_app()


@pytest.fixture
def signed_in(app, db, password):
    user = create_user(db, 'worker@example.com', 'Worker', password)
    C.record(db, user)
    return user


def sign_in(app, email='worker@example.com', password='a-perfectly-fine-password'):
    c = app.test_client()
    c.post('/login', data={'email': email, 'password': password})
    return c


def signed_in_now(client):
    """A signed-out browser is redirected to the login page."""
    return client.get('/settings').status_code == 200


# ── Idle timeout ─────────────────────────────────────────────────────────────

def test_a_session_survives_ordinary_use(app, signed_in):
    c = sign_in(app)
    for _ in range(3):
        assert signed_in_now(c)


def test_an_idle_session_expires(make_app, db, password):
    """Enforced by the age of the signature, not by the cookie's expiry — a
    browser that keeps sending an old cookie gains nothing by it."""
    app = make_app(PERMANENT_SESSION_LIFETIME=timedelta(seconds=1))
    user = create_user(db, 'idle@example.com', 'Idle', password)
    C.record(db, user)

    c = sign_in(app, 'idle@example.com', password)
    assert signed_in_now(c)

    time.sleep(2)
    # Same cookie, still sent — and no longer accepted.
    assert not signed_in_now(c)


def test_the_clock_restarts_on_every_request(make_app, db, password):
    """Idle time, not a countdown from signing in. Somebody working through the
    afternoon must not be thrown out mid-sentence."""
    app = make_app(PERMANENT_SESSION_LIFETIME=timedelta(seconds=2))
    user = create_user(db, 'busy@example.com', 'Busy', password)
    C.record(db, user)
    c = sign_in(app, 'busy@example.com', password)

    # Four seconds of use in one-second steps: past the limit in total, never
    # idle for it.
    for _ in range(4):
        time.sleep(1)
        assert signed_in_now(c)


# ── Changing a password ends other sessions ──────────────────────────────────

NEW = 'a-brand-new-password'


def change(client, current, new=NEW, confirm=None):
    return client.post('/settings/password', data={
        'current_password': current, 'new_password': new,
        'confirm_password': NEW if confirm is None else confirm},
        follow_redirects=True)


def test_changing_the_password_signs_out_the_other_browser(app, signed_in, password):
    """The reason somebody changes a password in a hurry. Ending only your own
    session would leave whoever you are worried about still signed in."""
    mine, theirs = sign_in(app), sign_in(app)
    assert signed_in_now(theirs)

    change(mine, password)

    assert not signed_in_now(theirs)     # the other browser is out
    assert signed_in_now(mine)           # the one that did it stays in


def test_the_new_password_works_and_the_old_one_does_not(app, signed_in, password):
    change(sign_in(app), password)

    fresh = app.test_client()
    assert fresh.post('/login', data={'email': 'worker@example.com',
                                      'password': password}).status_code == 401
    assert fresh.post('/login', data={'email': 'worker@example.com',
                                      'password': NEW}).status_code == 302


def test_the_current_password_is_required(app, signed_in, password):
    change(sign_in(app), 'not-my-password')
    # Unchanged: the old one still signs in.
    fresh = app.test_client()
    assert fresh.post('/login', data={'email': 'worker@example.com',
                                      'password': password}).status_code == 302


def test_the_two_new_passwords_must_match(app, signed_in, password):
    assert b'do not match' in change(sign_in(app), password,
                                     confirm='a-different-password').data


def test_a_short_password_is_refused(app, signed_in, password):
    assert b'at least 12' in change(sign_in(app), password, new='short',
                                    confirm='short').data.lower()


def test_you_cannot_set_it_to_what_it_already_is(app, signed_in, password):
    assert b'already have' in change(sign_in(app), password, new=password,
                                     confirm=password).data


def test_guessing_the_current_password_is_rate_limited(app, signed_in, password):
    """Otherwise a stolen session is an unlimited oracle for the password
    itself, which is worth rather more than the session."""
    c = sign_in(app)
    seen = None
    for _ in range(12):
        seen = change(c, 'wrong-every-time')
    assert b'Too many attempts' in seen.data


def test_signing_out_is_still_possible_afterwards(app, signed_in, password):
    c = sign_in(app)
    change(c, password)
    c.post('/logout')
    assert not signed_in_now(c)
