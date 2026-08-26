"""What people are told, and what they can switch off.

This module is short and it is the reason the rest of the system is defensible.
An admin watching two colleagues' screens on a ten-minute timer is workplace
monitoring, and the difference between a tool a team accepts and one they
resent is almost entirely here: they were told what is collected, they agreed
to it, and they can stop it themselves without asking anyone.

The pause is enforced on the SERVER, not by asking the agent nicely. An agent
that keeps uploading while paused is refused. That matters because the person
being recorded should not have to trust that a program on their machine is
honouring a switch they cannot see.
"""
import logging
from datetime import datetime, timedelta, timezone

from app.models import Consent

logger = logging.getLogger('consent')
UTC = timezone.utc

# Bumped whenever what is collected changes. A new version means everyone is
# asked again rather than silently inheriting an agreement to something else.
POLICY_VERSION = '2026-08-1'

# What the policy version above actually covers. Kept next to the version so
# the two cannot drift, and rendered on the consent page verbatim.
COLLECTED = [
    ('Which application has focus, and its window title',
     'Recorded continuously while a session is running. Window titles often '
     'contain file names, page titles and subject lines.'),
    ('Screen captures',
     'A picture of your whole screen, roughly every ten minutes, only while a '
     'session is running and you are not idle. A sound plays each time. Full '
     'images are deleted after 30 days; thumbnails after a year.'),
    ('When you are idle',
     'Time with no keyboard or mouse input. Used to stop sessions, not to '
     'measure how long you were away from your desk.'),
    ('A daily note you write yourself',
     'Optional, and yours. Nothing writes it for you.'),
]

WHO_CAN_SEE = [
    'You can see everything recorded about you.',
    'An administrator can see your hours, projects, daily notes and screen '
    'captures.',
    'Nobody else has access, and nothing is shared outside this system.',
]

# An indefinite pause is stored as a real timestamp rather than NULL, so every
# code path reads the same column and there is no "off means on" special case.
INDEFINITE = timedelta(days=365 * 10)

PAUSE_PRESETS = [('15 minutes', 15), ('1 hour', 60), ('Rest of the day', None)]


def has_consented(db, user, version=POLICY_VERSION):
    return (db.query(Consent)
            .filter(Consent.user_id == user.id, Consent.policy_version == version)
            .first() is not None)


def record(db, user, version=POLICY_VERSION, source_ip=None, now=None):
    if has_consented(db, user, version):
        return None
    entry = Consent(user_id=user.id, policy_version=version,
                    source_ip=(source_ip or '')[:64] or None,
                    accepted_at=now or datetime.now(UTC))
    db.add(entry)
    db.commit()
    logger.info(f'{user.email} accepted policy {version}')
    return entry


def history(db, user):
    return (db.query(Consent).filter(Consent.user_id == user.id)
            .order_by(Consent.accepted_at.desc()).all())


# ── Pause ────────────────────────────────────────────────────────────────────

def is_paused(user, now=None):
    until = user.settings.tracking_paused_until
    return until is not None and until > (now or datetime.now(UTC))


def pause(db, user, minutes=None, reason=None, now=None, tz=None):
    """Stop tracking. minutes=None means the rest of the local day.

    'Rest of the day' rather than 'for ever' as the longest preset: a pause you
    have to remember to undo becomes a tracker that quietly stopped weeks ago,
    which helps nobody. Indefinite is still available, deliberately as its own
    choice.
    """
    now = now or datetime.now(UTC)
    if minutes is None:
        from app.services.reporting import user_tz
        zone = tz or user_tz(user)
        local = now.astimezone(zone)
        midnight = (local + timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0)
        until = midnight.astimezone(UTC)
    else:
        until = now + timedelta(minutes=int(minutes))

    user.settings.tracking_paused_until = until
    user.settings.pause_reason = (reason or '').strip()[:200] or None
    db.commit()
    logger.info(f'{user.email} paused tracking until {until.isoformat()}')
    return until


def pause_indefinitely(db, user, reason=None, now=None):
    now = now or datetime.now(UTC)
    user.settings.tracking_paused_until = now + INDEFINITE
    user.settings.pause_reason = (reason or '').strip()[:200] or None
    db.commit()
    return user.settings.tracking_paused_until


def resume(db, user):
    user.settings.tracking_paused_until = None
    user.settings.pause_reason = None
    db.commit()
