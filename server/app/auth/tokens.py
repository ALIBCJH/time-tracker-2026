"""Agent tokens — how an installed agent proves which user it uploads for.

Issued once, shown once, stored only as a hash. A leaked database must not
hand anyone a working token.

The hash here is SHA-256, deliberately, where passwords use slow scrypt. A
token is 32 bytes from os.urandom: there is nothing to guess, so a slow KDF
would buy no security while making every single upload request pay for it.
Slow hashing protects low-entropy secrets; this is not one.

Constant-time comparison is still required — a lookup by hash must not leak
where two hashes first differ.
"""
import hashlib
import hmac
import secrets

# Prefixed so a leaked string is recognisable in a log or a paste, and so
# secret-scanners can be taught the shape.
PREFIX = 'ttc_'
_ENTROPY_BYTES = 32


def generate_token() -> tuple[str, str]:
    """(token, token_hash). The token is returned exactly once — after this it
    exists only as a hash, and a lost token is replaced rather than recovered."""
    token = PREFIX + secrets.token_urlsafe(_ENTROPY_BYTES)
    return token, hash_token(token)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode('utf-8')).hexdigest()


def tokens_match(token: str, expected_hash: str) -> bool:
    if not token or not expected_hash:
        return False
    return hmac.compare_digest(hash_token(token), expected_hash)


def looks_like_token(value: str) -> bool:
    """Cheap shape check so an obviously-wrong header is rejected before it
    touches the database."""
    return bool(value) and value.startswith(PREFIX) and len(value) > len(PREFIX) + 20
