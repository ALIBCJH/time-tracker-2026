"""Password hashing.

Werkzeug's scrypt, which is a deliberately slow memory-hard KDF — the right
tool for a human-chosen secret, because the whole point is to make an offline
guessing attack against a stolen database expensive.

Contrast app/auth/tokens.py, which uses a *fast* hash for exactly the opposite
reason. Both are correct; the difference is where the entropy comes from.
"""
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
