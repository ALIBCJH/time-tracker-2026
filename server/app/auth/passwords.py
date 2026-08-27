"""Password hashing.

Werkzeug's scrypt, which is a deliberately slow memory-hard KDF — the right
tool for a human-chosen secret, because the whole point is to make an offline
guessing attack against a stolen database expensive.

Contrast app/auth/tokens.py, which uses a *fast* hash for exactly the opposite
reason. Both are correct; the difference is where the entropy comes from.
"""
import functools
import hashlib

from werkzeug.security import check_password_hash, generate_password_hash

MIN_LENGTH = 12


class WeakPassword(ValueError):
    pass


def hash_password(password: str) -> str:
    if len(password or '') < MIN_LENGTH:
        raise WeakPassword(f'Password must be at least {MIN_LENGTH} characters.')
    return generate_password_hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    """False for a bad password AND for a user with no usable hash.

    check_password_hash raises on a malformed hash; a login route must treat
    that as "no", not as a 500 that tells an attacker the account is unusual.
    """
    if not password or not password_hash:
        return False
    try:
        return check_password_hash(password_hash, password)
    except (ValueError, TypeError):
        return False


@functools.lru_cache(maxsize=1)
def _dummy_hash() -> str:
    """A hash of a password nobody has, with the same parameters as a real one.

    Computed once, on first use rather than at import, so a CLI command that
    never authenticates anybody does not pay for it.
    """
    return generate_password_hash('there-is-no-account-with-this-password')


def verify_in_constant_work(password: str, password_hash: str | None) -> bool:
    """Verify, and spend the same time when there is nothing to verify against.

    scrypt is deliberately slow — around 170ms here — which is exactly right
    against a stolen database and exactly wrong if it only runs for addresses
    that exist. Returning early for an unknown address makes the response
    around 170ms faster, and that gap is an account-enumeration oracle as
    surely as a different error message would be. The login route is careful to
    say the same thing in every failure case; this makes it take the same time
    as well.

    So an absent hash is compared against a dummy one instead of skipped. The
    answer is still False; it just costs what a real answer costs.
    """
    if not password_hash:
        check_password_hash(_dummy_hash(), password or '')
        return False
    return verify_password(password, password_hash)


def session_fingerprint(password_hash: str) -> str:
    """A short, non-reversible marker of which password a session belongs to.

    The hash itself never goes into a cookie — a scrypt hash in a signed but
    readable session is a hash handed to anybody who looks. This is a truncated
    digest of it: enough to notice the password changed, useless for anything
    else.
    """
    return hashlib.sha256((password_hash or '').encode()).hexdigest()[:16]
