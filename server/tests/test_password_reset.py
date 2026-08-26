"""Password reset tickets.

A reset link is a standing key to an account for as long as it works, so the
properties that matter are: it works once, it stops working soon, and what it
tells someone holding a bad one.
"""
from datetime import datetime, timedelta, timezone

import pytest

from app.auth.passwords import verify_password
from app.models import PasswordReset
from app.services.passwords import (LIFETIME, PREFIX, InvalidTicket, issue,
                                    redeem)
from app.services.users import create_user

UTC = timezone.utc
NOW = datetime(2026, 8, 26, 12, tzinfo=UTC)
NEW = 'a-perfectly-good-new-password'


@pytest.fixture
def user(db, password):
    return create_user(db, 'a@example.com', 'A', password)


# ── The ticket ───────────────────────────────────────────────────────────────

def test_a_ticket_sets_the_password(db, user):
    ticket, _ = issue(db, user, now=NOW)
    redeem(db, ticket, NEW, now=NOW)
    db.expire_all()
    assert verify_password(NEW, db.query(type(user)).one().password_hash)


def test_the_raw_ticket_is_never_stored(db, user):
    """A stolen database must not hand anyone a working reset link."""
    ticket, _ = issue(db, user, now=NOW)
    stored = db.query(PasswordReset).order_by(PasswordReset.created_at.desc()).first()
    assert stored.token_hash != ticket and ticket not in stored.token_hash


def test_a_ticket_works_only_once(db, user):
    ticket, _ = issue(db, user, now=NOW)
    redeem(db, ticket, NEW, now=NOW)
    with pytest.raises(InvalidTicket):
        redeem(db, ticket, 'another-password-entirely', now=NOW)


def test_a_ticket_expires(db, user):
    """A link sitting in a mailbox is a standing key to the account."""
    ticket, _ = issue(db, user, now=NOW)
    with pytest.raises(InvalidTicket):
        redeem(db, ticket, NEW, now=NOW + LIFETIME + timedelta(seconds=1))


def test_issuing_a_new_ticket_kills_the_old_one(db, user):
    """What someone does when they think the previous link leaked."""
    first, _ = issue(db, user, now=NOW)
    issue(db, user, now=NOW)
    with pytest.raises(InvalidTicket):
        redeem(db, first, NEW, now=NOW)


def test_an_invented_ticket_is_refused(db, user):
    with pytest.raises(InvalidTicket):
        redeem(db, PREFIX + 'x' * 40, NEW, now=NOW)


def test_a_ticket_without_the_prefix_is_refused(db, user):
    with pytest.raises(InvalidTicket):
        redeem(db, 'not-even-close', NEW, now=NOW)


def test_a_ticket_for_a_disabled_account_is_refused(db, user):
    ticket, _ = issue(db, user, now=NOW)
    user.is_active = False
    db.commit()
    with pytest.raises(InvalidTicket):
        redeem(db, ticket, NEW, now=NOW)


def test_a_weak_password_is_refused_without_burning_the_ticket(db, user):
    """Mistyping a short password should not cost someone their only link."""
    from app.auth.passwords import WeakPassword
    ticket, _ = issue(db, user, now=NOW)
    with pytest.raises(WeakPassword):
        redeem(db, ticket, 'short', now=NOW)
    redeem(db, ticket, NEW, now=NOW)          # still works


def test_used_rows_are_kept_not_deleted(db, user):
    """So "already used" can be answered honestly rather than being
    indistinguishable from "never existed"."""
    ticket, _ = issue(db, user, now=NOW)
    redeem(db, ticket, NEW, now=NOW)
    assert db.query(PasswordReset).one().used_at is not None


# ── Over the wire ────────────────────────────────────────────────────────────

def test_the_page_accepts_a_new_password(client, db, user):
    ticket, _ = issue(db, user)
    r = client.post(f'/reset/{ticket}',
                    data={'password': NEW, 'confirm': NEW})
    assert r.status_code == 302 and '/login' in r.headers['Location']
    assert client.post('/login', data={'email': 'a@example.com',
                                       'password': NEW}).status_code == 302


def test_mismatched_passwords_are_refused(client, db, user):
    ticket, _ = issue(db, user)
    r = client.post(f'/reset/{ticket}', data={'password': NEW, 'confirm': 'other'})
    assert r.status_code == 400 and b'do not match' in r.data


@pytest.mark.parametrize('ticket', ['ttr_nonsense-but-right-shape-aaaaaaaaaaaa',
                                    'garbage'])
def test_a_bad_link_says_the_same_thing_however_it_is_bad(client, db, ticket):
    """Which of unknown, expired or spent it is tells an attacker something and
    the owner nothing."""
    r = client.post(f'/reset/{ticket}', data={'password': NEW, 'confirm': NEW})
    assert r.status_code == 400 and b'not valid' in r.data


def test_a_spent_link_says_the_same_thing(client, db, user):
    ticket, _ = issue(db, user)
    client.post(f'/reset/{ticket}', data={'password': NEW, 'confirm': NEW})
    r = client.post(f'/reset/{ticket}', data={'password': NEW, 'confirm': NEW})
    assert b'not valid' in r.data


def test_the_form_renders_without_a_valid_ticket(client):
    """The link is only checked on submit, so a typo shows a form rather than
    an error page that confirms which links exist."""
    assert client.get('/reset/ttr_anything').status_code == 200


def test_grinding_at_the_endpoint_is_limited(client, db):
    codes = [client.post('/reset/ttr_guessing-away-aaaaaaaaaaaaaaaaaaaa',
                         data={'password': NEW, 'confirm': NEW}).status_code
             for _ in range(25)]
    assert 429 in codes


def test_there_is_no_self_service_request_form(client):
    """An unauthenticated way to make the domain send mail to any address
    someone types buys nothing in a three-person deployment."""
    for path in ('/forgot', '/forgot-password', '/password-reset'):
        assert client.get(path).status_code == 404
