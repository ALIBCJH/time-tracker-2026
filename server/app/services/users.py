"""Creating people and devices — the operations that must not be done ad hoc."""
import uuid
from datetime import datetime, timezone

from sqlalchemy.exc import IntegrityError

from app.auth.passwords import hash_password
from app.auth.tokens import generate_token
from app.config import DEFAULT_TIMEZONE
from app.models import Device, User, UserSettings


class EmailTaken(ValueError):
    pass


def normalise_email(email: str) -> str:
    """Lowercased and stripped. Done in one place so the unique index actually
    means what it looks like it means — 'A@x.com' must not create a second
    account alongside 'a@x.com'."""
    return (email or '').strip().lower()


def create_user(db, email, name, password, role='worker', timezone_name=None):
    if role not in ('worker', 'admin'):
        raise ValueError(f'unknown role: {role!r}')

    user = User(email=normalise_email(email), name=name.strip(),
                password_hash=hash_password(password), role=role)
    # Settings are created with the user, never lazily: every code path that
    # reads a setting can then assume the row exists.
    user.settings = UserSettings(timezone=timezone_name or DEFAULT_TIMEZONE)
    db.add(user)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise EmailTaken(f'{email} already has an account.')
    return user


def issue_device_token(db, user, device_name):
    """(device, token). The token is returned once and never recoverable."""
    token, token_hash = generate_token()
    device = Device(user_id=user.id, name=device_name.strip(), token_hash=token_hash)
    db.add(device)
    db.commit()
    return device, token


def revoke_device(db, device):
    device.revoked_at = datetime.now(timezone.utc)
    db.commit()
    return device
