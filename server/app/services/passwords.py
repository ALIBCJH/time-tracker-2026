"""Resetting a forgotten password.

There is no self-service "email me a link" form, and that is a deliberate
choice rather than a missing feature. This is a three-person deployment behind
no public sign-up; a reset form on the internet is an unauthenticated way to
send mail from your domain to any address someone types, and it buys nothing
here. An administrator issues a link instead, and mails it if SMTP is
configured.

The ticket itself follows the agent-token rules: 32 random bytes, stored only
as a SHA-256 hash, compared in constant time. It is single-use and short-lived,
because a reset link sitting in a mailbox is a standing key to the account.
"""
import hashlib
import hmac
import logging
import secrets
from datetime import datetime, timedelta, timezone

from app.auth.passwords import hash_password
from app.models import PasswordReset, User

logger = logging.getLogger('passwords')
UTC = timezone.utc

PREFIX = 'ttr_'
LIFETIME = timedelta(hours=2)


class InvalidTicket(ValueError):
    """Unknown, expired, or already used. Deliberately one exception for all
    three at the boundary — the page must not explain which."""


def issue(db, user, lifetime=LIFETIME, now=None):
    """(ticket, row). The ticket is returned once and never recoverable."""
    now = now or datetime.now(UTC)
    ticket = PREFIX + secrets.token_urlsafe(32)
    row = PasswordReset(user_id=user.id, token_hash=_hash(ticket),
                        expires_at=now + lifetime)
    # Any earlier unused ticket stops working. Issuing a new one is what
    # someone does when they think the old one leaked.
    (db.query(PasswordReset)
     .filter(PasswordReset.user_id == user.id, PasswordReset.used_at.is_(None))
     .update({'used_at': now}))
    db.add(row)
    db.commit()
    logger.info(f'Password reset issued for {user.email}')
    return ticket, row


def redeem(db, ticket, new_password, now=None):
    """Set the password and burn the ticket. Returns the user."""
    now = now or datetime.now(UTC)
    if not ticket or not ticket.startswith(PREFIX):
        raise InvalidTicket('that link is not valid')

    row = (db.query(PasswordReset)
           .filter(PasswordReset.token_hash == _hash(ticket)).one_or_none())
    if row is None or row.used_at is not None or row.expires_at <= now:
        raise InvalidTicket('that link is not valid')

    user = db.get(User, row.user_id)
    if user is None or not user.is_active:
        raise InvalidTicket('that link is not valid')

    # hash_password enforces the minimum length and raises WeakPassword, which
    # the caller shows as a form error rather than as an invalid link.
    user.password_hash = hash_password(new_password)
    row.used_at = now
    db.commit()
    logger.info(f'Password reset completed for {user.email}')
    return user


def _hash(ticket):
    return hashlib.sha256(ticket.encode('utf-8')).hexdigest()


def matches(ticket, expected_hash):
    return hmac.compare_digest(_hash(ticket), expected_hash)
