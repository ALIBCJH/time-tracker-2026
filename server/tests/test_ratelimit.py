"""Rate limiting.

In the database rather than in memory: an in-memory counter is per-process, so
four Gunicorn workers would quietly give an attacker four times the allowance,
and every deploy would reset it — which is exactly when someone hammering a
login form would most like it not to.
"""
from datetime import datetime, timedelta, timezone

import pytest

from app.ratelimit import LOGIN_ATTEMPTS, LOGIN_WINDOW, clear, hit, prune
from app.services.users import create_user

UTC = timezone.utc
T0 = datetime(2026, 8, 26, 9, 0, tzinfo=UTC)


def test_attempts_are_allowed_up_to_the_limit(db):
    for _ in range(5):
        allowed, _ = hit(db, 'test', 'someone', 5, now=T0)
        assert allowed


def test_the_next_attempt_is_refused(db):
    for _ in range(5):
        hit(db, 'test', 'someone', 5, now=T0)
    allowed, remaining = hit(db, 'test', 'someone', 5, now=T0)
    assert not allowed and remaining == 0


def test_keys_are_counted_separately(db):
    for _ in range(5):
        hit(db, 'test', 'a', 5, now=T0)
    allowed, _ = hit(db, 'test', 'b', 5, now=T0)
    assert allowed


def test_scopes_are_counted_separately(db):
    for _ in range(5):
        hit(db, 'login-email', 'a', 5, now=T0)
    allowed, _ = hit(db, 'login-ip', 'a', 5, now=T0)
    assert allowed


def test_a_later_window_starts_fresh(db):
    for _ in range(5):
        hit(db, 'test', 'someone', 5, now=T0)
    allowed, _ = hit(db, 'test', 'someone', 5, now=T0 + LOGIN_WINDOW * 2)
    assert allowed


def test_clearing_forgets_the_counter(db):
    """Someone who mistypes twice then gets it right should not be left sitting
    next to a lockout."""
    for _ in range(4):
        hit(db, 'test', 'someone', 5, now=T0)
    clear(db, 'test', 'someone', now=T0)
    _, remaining = hit(db, 'test', 'someone', 5, now=T0)
    assert remaining == 4


def test_the_count_is_shared_between_processes(db, engine):
    """The whole reason this is in Postgres and not in a dict."""
    from sqlalchemy.orm import sessionmaker
    Other = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    second = Other()
    try:
        for _ in range(3):
            hit(db, 'test', 'shared', 5, now=T0)
        for _ in range(2):
            hit(second, 'test', 'shared', 5, now=T0)
        allowed, _ = hit(db, 'test', 'shared', 5, now=T0)
        assert not allowed          # six attempts across two connections
    finally:
        second.close()


def test_old_windows_are_pruned(db):
    hit(db, 'test', 'someone', 5, now=T0 - timedelta(days=3))
    prune(db, older_than=timedelta(days=1), now=T0)
    _, remaining = hit(db, 'test', 'someone', 5, now=T0)
    assert remaining == 4


# ── Over the login form ──────────────────────────────────────────────────────

def test_repeated_bad_logins_are_eventually_refused(client, db, password):
    create_user(db, 'a@example.com', 'A', password)
    codes = [client.post('/login', data={'email': 'a@example.com',
                                         'password': 'wrong'}).status_code
             for _ in range(LOGIN_ATTEMPTS + 2)]
    assert 429 in codes


def test_a_locked_out_address_is_refused_even_with_the_right_password(
        client, db, password):
    """Otherwise the limit is decoration — an attacker who guesses correctly on
    attempt fifty is in."""
    create_user(db, 'a@example.com', 'A', password)
    for _ in range(LOGIN_ATTEMPTS + 1):
        client.post('/login', data={'email': 'a@example.com', 'password': 'wrong'})
    r = client.post('/login', data={'email': 'a@example.com', 'password': password})
    assert r.status_code == 429


def test_one_address_being_attacked_does_not_lock_out_everyone(client, db, password):
    """Per-address counting, so an attacker cannot deny service to a whole team
    by guessing at each account in turn."""
    create_user(db, 'victim@example.com', 'V', password)
    create_user(db, 'other@example.com', 'O', password)
    for _ in range(LOGIN_ATTEMPTS + 1):
        client.post('/login', data={'email': 'victim@example.com', 'password': 'x'})
    r = client.post('/login', data={'email': 'other@example.com', 'password': password})
    assert r.status_code == 302


def test_a_successful_login_resets_the_count(client, db, password):
    create_user(db, 'a@example.com', 'A', password)
    for _ in range(3):
        client.post('/login', data={'email': 'a@example.com', 'password': 'wrong'})
    client.post('/login', data={'email': 'a@example.com', 'password': password})
    client.post('/logout')               # a signed-in client is redirected, not re-checked

    for _ in range(LOGIN_ATTEMPTS - 1):
        r = client.post('/login', data={'email': 'a@example.com', 'password': 'wrong'})
    assert r.status_code == 401          # still counting from zero, not locked


def test_the_table_exists_without_importing_the_limiter(db):
    """It used to live in app/ratelimit.py, so whether the table was created at
    all depended on whether anything had imported that module first — which
    made Alembic generate an empty migration and made the test schema
    order-dependent."""
    from app.models import Base
    assert 'rate_buckets' in Base.metadata.tables
