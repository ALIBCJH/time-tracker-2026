"""Authentication and authorisation.

Two separate identities reach this server — a person with a session cookie and
an agent with a bearer token — and most of what follows exists to pin that they
never substitute for one another.
"""
import pytest

from app.auth.passwords import WeakPassword, hash_password, verify_password
from app.auth.tokens import PREFIX, generate_token, tokens_match
from app.db import db_session
from app.models import Device, User
from app.services.users import EmailTaken, create_user, issue_device_token, revoke_device


# ── Passwords ────────────────────────────────────────────────────────────────

def test_a_password_verifies_only_against_itself(password):
    h = hash_password(password)
    assert verify_password(password, h)
    assert not verify_password(password + 'x', h)


def test_the_same_password_hashes_differently_each_time(password):
    """Salted — two accounts with one password must not share a hash, or the
    database itself tells an attacker who to target first."""
    assert hash_password(password) != hash_password(password)


def test_short_passwords_are_refused():
    with pytest.raises(WeakPassword):
        hash_password('short')


def test_a_broken_hash_is_a_failed_login_not_a_crash():
    """A 500 here would tell an attacker the account is unusual."""
    assert verify_password('anything', 'not-a-real-hash') is False
    assert verify_password('anything', '') is False


# ── Accounts ─────────────────────────────────────────────────────────────────

def test_email_case_cannot_create_a_second_account(db, password):
    create_user(db, 'Person@Example.com', 'A', password)
    with pytest.raises(EmailTaken):
        create_user(db, 'person@example.com', 'B', password)


def test_a_new_user_always_has_settings(db, password):
    """Every read of a setting assumes the row exists — so creation must."""
    u = create_user(db, 'x@example.com', 'X', password)
    assert u.settings is not None
    assert u.settings.timezone == 'Africa/Nairobi'
    assert u.settings.catch_all_stream == 'Deep Research'


def test_an_unknown_role_is_refused(db, password):
    with pytest.raises(ValueError):
        create_user(db, 'x@example.com', 'X', password, role='superuser')


# ── Logging in ───────────────────────────────────────────────────────────────

def test_correct_credentials_sign_in(client, make_login_user, password):
    make_login_user()
    r = client.post('/login', data={'email': 'worker@example.com', 'password': password})
    assert r.status_code == 302
    assert client.get('/').status_code == 200


def test_wrong_password_is_refused(client, make_login_user, password):
    make_login_user()
    r = client.post('/login', data={'email': 'worker@example.com', 'password': 'wrong'})
    assert r.status_code == 401


def test_unknown_email_and_wrong_password_are_indistinguishable(
        client, make_login_user, password):
    """Anything more specific is an account-enumeration oracle: an attacker
    learns which addresses have accounts before guessing a single password."""
    make_login_user()
    wrong_pw = client.post('/login', data={'email': 'worker@example.com',
                                           'password': 'wrong'})
    no_user = client.post('/login', data={'email': 'nobody@example.com',
                                          'password': 'wrong'})
    assert wrong_pw.status_code == no_user.status_code == 401
    assert b'incorrect' in wrong_pw.data and b'incorrect' in no_user.data
    assert wrong_pw.data.count(b'incorrect') == no_user.data.count(b'incorrect')


def test_a_disabled_account_cannot_sign_in(client, make_login_user, password):
    make_login_user(active=False)
    r = client.post('/login', data={'email': 'worker@example.com', 'password': password})
    assert r.status_code == 401


def test_signing_out_ends_the_session(client, make_login_user, password):
    make_login_user()
    client.post('/login', data={'email': 'worker@example.com', 'password': password})
    client.post('/logout')
    assert client.get('/').status_code == 302        # bounced to login


def test_anonymous_visitors_are_sent_to_login(client):
    r = client.get('/')
    assert r.status_code == 302 and '/login' in r.headers['Location']


@pytest.mark.parametrize('target', [
    'https://evil.example.com/',      # absolute
    '//evil.example.com/',            # scheme-relative
    'http://evil.example.com',
])
def test_login_will_not_redirect_off_site(client, make_login_user, password, target):
    """An open redirect on a login page is how a phishing link gets built out of
    a domain the victim already trusts."""
    make_login_user()
    r = client.post(f'/login?next={target}',
                    data={'email': 'worker@example.com', 'password': password})
    assert 'evil.example.com' not in r.headers['Location']


def test_login_honours_a_same_site_next(client, make_login_user, password):
    make_login_user()
    r = client.post('/login?next=/admin/team',
                    data={'email': 'worker@example.com', 'password': password})
    assert r.headers['Location'].endswith('/admin/team')


# ── Roles ────────────────────────────────────────────────────────────────────

def test_a_worker_cannot_see_the_admin_area(client, make_login_user, password):
    """404, not 403 — a worker should not learn the page exists."""
    make_login_user(role='worker')
    client.post('/login', data={'email': 'worker@example.com', 'password': password})
    assert client.get('/admin/team').status_code == 404


def test_an_admin_can(client, make_login_user, password):
    make_login_user(email='boss@example.com', role='admin')
    client.post('/login', data={'email': 'boss@example.com', 'password': password})
    r = client.get('/admin/team')
    assert r.status_code == 200 and b'boss@example.com' in r.data


# ── Agent tokens ─────────────────────────────────────────────────────────────

def test_a_token_verifies_only_against_its_own_hash():
    token, digest = generate_token()
    assert token.startswith(PREFIX)
    assert tokens_match(token, digest)
    assert not tokens_match(token + 'x', digest)


def test_the_raw_token_is_never_stored(db, password):
    """A stolen database must not yield working tokens."""
    u = create_user(db, 'a@example.com', 'A', password)
    _, token = issue_device_token(db, u, 'laptop')
    stored = db.query(Device).one()
    assert stored.token_hash != token
    assert token not in stored.token_hash


def test_an_agent_can_authenticate(client, db, password):
    u = create_user(db, 'a@example.com', 'A', password)
    _, token = issue_device_token(db, u, 'laptop')
    r = client.post('/api/agent/heartbeat', headers={'Authorization': f'Bearer {token}'})
    assert r.status_code == 200
    assert r.get_json()['user'] == 'a@example.com'


def test_a_heartbeat_records_that_the_agent_is_alive(client, db, password):
    """This is what an abandoned open session gets capped at."""
    u = create_user(db, 'a@example.com', 'A', password)
    device, token = issue_device_token(db, u, 'laptop')
    assert device.last_seen_at is None
    client.post('/api/agent/heartbeat', headers={'Authorization': f'Bearer {token}'})
    db_session.remove()
    assert db.get(Device, device.id) and db.query(Device).one().last_seen_at is not None


@pytest.mark.parametrize('header', [
    None,
    'Bearer',
    'Bearer not-a-token',
    'Bearer ttc_obviously-wrong-but-right-shape-aaaaaaaa',
    'Basic ttc_something',
])
def test_bad_agent_credentials_are_refused(client, header):
    headers = {'Authorization': header} if header else {}
    assert client.post('/api/agent/heartbeat', headers=headers).status_code == 401


def test_a_revoked_token_stops_working(client, db, password):
    u = create_user(db, 'a@example.com', 'A', password)
    device, token = issue_device_token(db, u, 'laptop')
    auth = {'Authorization': f'Bearer {token}'}
    assert client.post('/api/agent/heartbeat', headers=auth).status_code == 200
    revoke_device(db, device)
    db_session.remove()
    assert client.post('/api/agent/heartbeat', headers=auth).status_code == 401


def test_a_token_for_a_disabled_account_is_refused(client, db, password):
    u = create_user(db, 'a@example.com', 'A', password)
    _, token = issue_device_token(db, u, 'laptop')
    u.is_active = False
    db.commit()
    db_session.remove()
    r = client.post('/api/agent/heartbeat', headers={'Authorization': f'Bearer {token}'})
    assert r.status_code == 403


def test_a_token_reaches_only_its_own_user(client, db, password):
    """The identity comes from the token, never from the request — a compromised
    agent must not be able to write rows against somebody else."""
    a = create_user(db, 'a@example.com', 'A', password)
    create_user(db, 'b@example.com', 'B', password)
    _, token_a = issue_device_token(db, a, 'laptop')
    r = client.get('/api/agent/me', headers={'Authorization': f'Bearer {token_a}'})
    assert r.get_json()['user']['email'] == 'a@example.com'


# ── The two identities do not substitute for one another ─────────────────────

def test_a_signed_in_person_is_not_an_agent(client, make_login_user, password):
    """A session cookie must not reach the ingest API."""
    make_login_user()
    client.post('/login', data={'email': 'worker@example.com', 'password': password})
    assert client.post('/api/agent/heartbeat').status_code == 401


def test_an_agent_token_is_not_a_login(client, db, password):
    """A token leaked off a laptop must not become a dashboard session."""
    u = create_user(db, 'a@example.com', 'A', password)
    _, token = issue_device_token(db, u, 'laptop')
    r = client.get('/', headers={'Authorization': f'Bearer {token}'})
    assert r.status_code == 302 and '/login' in r.headers['Location']


def test_health_check_needs_no_credentials(client):
    assert client.get('/healthz').status_code == 200
