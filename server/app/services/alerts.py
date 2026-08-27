"""Telling someone their tracking stopped working.

Everything else in this system guards against recording time that was not
worked. This module guards the opposite failure, which is quieter and worse: an
agent dies, nothing is recorded, and the silence is indistinguishable from a
day off. The weekly report goes out on Monday at 17:00 with a number that is
simply wrong, and the first anyone knows of it is an argument about hours.

The whole difficulty is that silence is normal. Evenings, weekends, leave,
public holidays — an agent that is not reporting is usually a person who is not
working, and a system that mails somebody every Saturday morning to say their
tracker is quiet gets filtered into a folder within a fortnight. An alert
nobody reads is worse than no alert, because it also buries the next real one.

So nothing here fires on silence alone. Each alert needs silence that
contradicts something the system already believes:

  * SESSION DROPPED — the agent said it was working, then vanished. Its own
    claim is the expectation, so the contradiction is total. This is the case
    the desktop widget cannot catch: the widget goes dark because the agent
    died, and a dark widget is exactly what someone at a shut laptop expects
    to see. Only the server knows the session was still open.

  * DEVICE DORMANT — a device that used to report has said nothing for days.
    Ambiguous over an evening, unambiguous over a working week. Judged in days
    rather than hours precisely so that going home, sleeping, and a long
    weekend are never mistaken for a fault.

What is deliberately NOT alerted on: a device that went quiet earlier today.
Distinguishing "the agent crashed at 11am" from "she finished at 4pm and shut
the laptop" needs working hours the system does not ask anyone for, and
guessing wrong is the false positive that ruins the whole channel. The dormant
alert catches the same fault a few days later, when it is no longer a guess.

Alerts go to the tracked person and nobody else. An administrator does not need
mail about somebody else's laptop: they already have the team page, which now
shows the same staleness by looking rather than by pushing. "Your tool is
broken" and "your colleague's tool is broken, here is a notification about it"
are different messages, and only the first one helps.
"""
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy.exc import IntegrityError

from app.models import AgentAlert, Device, Session
from app.services import mail
from app.services.consent import is_paused

logger = logging.getLogger('alerts')
UTC = timezone.utc

# How long after the server caps a session it is still worth mentioning. The
# guard matters on a restored database or a first deploy, where every orphan
# ever recorded is suddenly "new" — without a horizon, coming up would mail
# everybody a history lesson. Comfortably longer than the orphans job interval,
# so nothing that happens while the worker is briefly down is missed.
RECENT_DROP = timedelta(hours=6)

# Silence that stops being ambiguous. Three days spans a normal weekend, so a
# Friday-evening shutdown cannot trigger a Monday-morning alert.
DORMANT_AFTER = timedelta(days=3)

# Past this the device is not news, it is abandoned — an old laptop, someone
# who has left. Alerting on it is noise, and on a restored database it would be
# a burst of it.
DORMANT_MAX = timedelta(days=30)


# ── Deciding ─────────────────────────────────────────────────────────────────

def format_days(delta):
    """Duration at the scale this alert lives on.

    format_hm renders four days as "103h 00m", which is accurate and useless —
    nobody counts in three-digit hours. Dormancy is measured in days, so it
    should be read in days.
    """
    def plural(n, unit):
        return f'{n} {unit}' if n == 1 else f'{n} {unit}s'

    hours = int(delta.total_seconds() // 3600)
    if hours < 48:
        return plural(hours, 'hour')
    days, rest = divmod(hours, 24)
    if rest == 0:
        return plural(days, 'day')
    return f"{plural(days, 'day')}, {plural(rest, 'hour')}"


def _eligible(user, now):
    """Whether this person should be told anything at all right now.

    A pause is the important one. Somebody who switched tracking off did so on
    purpose, and mailing them to point out that it is off turns a control they
    hold into a nag — which is precisely what the pause was designed not to be.
    """
    if not user.is_active:
        return False
    if not user.settings.offline_alerts_enabled:
        return False
    return not is_paused(user, now)


def dropped_sessions(db, user, now=None, horizon=RECENT_DROP):
    """Sessions the server had to cap for this person, recently.

    Ordered oldest first so that if several are pending, the mail goes out in
    the order the failures happened.
    """
    now = now or datetime.now(UTC)
    return (db.query(Session)
            .filter(Session.user_id == user.id,
                    Session.orphaned_at.isnot(None),
                    Session.orphaned_at > now - horizon)
            .order_by(Session.orphaned_at.asc())
            .all())


def dormant_devices(db, user, now=None, after=DORMANT_AFTER, maximum=DORMANT_MAX):
    """Live devices of this person that have gone quiet for days.

    A device that has never reported at all is skipped: it was issued a token
    and not installed yet, which is a setup step rather than a failure. So is a
    revoked one — that silence was ordered.
    """
    now = now or datetime.now(UTC)
    devices = (db.query(Device)
               .filter(Device.user_id == user.id, Device.revoked_at.is_(None))
               .order_by(Device.name.asc())
               .all())
    return [d for d in devices
            if d.last_seen_at is not None
            and after < (now - d.last_seen_at) <= maximum]


def dedupe_key(kind, subject):
    """What makes two alerts 'the same one'.

    For a dropped session the identity is the session, and it never recurs. For
    a dormant device the identity includes the moment it fell silent, so a
    device that comes back and dies again is a new episode rather than a
    permanently suppressed one. Nothing has to remember to reset a flag; the
    agent reporting again is what re-arms it.
    """
    if kind == 'session_dropped':
        return str(subject.id)
    return f'{subject.id}:{subject.last_seen_at.isoformat()}'


def device_health(db, user, now=None):
    """The freshest signal from any of this person's live devices.

    For the team page, which is how an administrator learns that somebody's
    agent is dead without being mailed about it. Deliberately the FRESHEST
    device rather than the stalest: someone with a desktop and a laptop is
    reporting fine as long as one of them is, and flagging the laptop that has
    been in a drawer since Tuesday would be a false alarm every week.

    Returns None when no device has ever reported — nothing to be stale yet.
    """
    now = now or datetime.now(UTC)
    seen = [d for d in db.query(Device)
            .filter(Device.user_id == user.id, Device.revoked_at.is_(None)).all()
            if d.last_seen_at is not None]
    if not seen:
        return None

    freshest = max(seen, key=lambda d: d.last_seen_at)
    silent_for = now - freshest.last_seen_at
    return {
        'device': freshest,
        'last_seen_at': freshest.last_seen_at,
        'silent_seconds': int(silent_for.total_seconds()),
        # Named the same way the alert decides, so what an admin sees on the
        # page and what the person was mailed about cannot disagree.
        'dormant': silent_for > DORMANT_AFTER,
    }


# ── Claiming ─────────────────────────────────────────────────────────────────

def claim(db, user, kind, key, recipient, sent_at=None):
    """Reserve the right to send. True if this caller won.

    Taken before the mail goes out, the same ordering as reports and for the
    same reason: two workers reaching the same alert at the same instant cannot
    both send, because the loser's INSERT violates the unique constraint. A
    crash between claiming and sending costs one alert; the other ordering
    costs a duplicate, and duplicates are what teach people to ignore a channel.
    """
    row = AgentAlert(user_id=user.id, kind=kind, dedupe_key=key,
                     recipient=recipient, sent_at=sent_at or datetime.now(UTC))
    db.add(row)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return False
    return True


def release(db, user, kind, key):
    """Give a claim back when sending failed, so the next tick may retry."""
    (db.query(AgentAlert)
     .filter(AgentAlert.user_id == user.id, AgentAlert.kind == kind,
             AgentAlert.dedupe_key == key)
     .delete())
    db.commit()


def already_sent(db, user, kind, key):
    return (db.query(AgentAlert)
            .filter(AgentAlert.user_id == user.id, AgentAlert.kind == kind,
                    AgentAlert.dedupe_key == key)
            .first() is not None)


# ── Sending ──────────────────────────────────────────────────────────────────

def render(user, kind, subject_row, now=None):
    """(subject, html) for one alert."""
    from app.reports.render import COLOURS, _tz
    from flask import render_template
    from app.services.reporting import format_hm

    now = now or datetime.now(UTC)
    local = _tz(user)

    if kind == 'session_dropped':
        ended = subject_row.ended_at
        lost = int((subject_row.orphaned_at - ended).total_seconds())
        context = {
            'kicker': 'Tracking interrupted',
            'title': 'A session was closed for you',
            'lead': (f"Your agent stopped reporting while a session was still "
                     f"running, so the server ended it at the last moment it "
                     f"had proof your machine was awake."),
            'facts': [
                ('Project', subject_row.project or '—'),
                ('Started', subject_row.started_at.astimezone(local)
                 .strftime('%a %d %b, %I:%M %p')),
                ('Ended at', ended.astimezone(local).strftime('%a %d %b, %I:%M %p')),
                ('Unrecorded after that', format_hm(lost)),
            ],
            'meaning': (
                "Any work after that time was not recorded. If you were still "
                "working, the tracked total for that day is short by roughly "
                "the amount above."),
            'action': 'Check that the agent is running, then start a new session.',
        }
        email_subject = f'TimeTracker: session on {subject_row.project} was cut short'
    else:
        silent = now - subject_row.last_seen_at
        context = {
            'kicker': 'Agent not reporting',
            'title': 'A device has stopped checking in',
            'lead': (f"“{subject_row.name}” has not contacted the server in "
                     f"several days. Nothing from it is being recorded."),
            'facts': [
                ('Device', subject_row.name),
                ('Last heard from', subject_row.last_seen_at.astimezone(local)
                 .strftime('%a %d %b, %I:%M %p')),
                ('Silent for', format_days(silent)),
            ],
            'meaning': (
                "If you have been working on this machine, that time is "
                "missing. If you have not, you can ignore this — the alert "
                "stops on its own once the agent reports again."),
            'action': 'Check the agent is running and that its token is still valid.',
        }
        email_subject = f'TimeTracker: {subject_row.name} has stopped reporting'

    context.update({
        'C': COLOURS,
        'accent': '#c2410c',
        'title_size': 26,
        'sent_at': now.astimezone(local).strftime('%A, %B %d, %Y at %I:%M %p %Z'),
    })
    return email_subject, render_template('email/agent_alert.html', **context)


def send_one(db, user, kind, subject_row, now=None, settings=None):
    """Claim, render, send. Returns 'sent', 'already-sent' or 'failed'."""
    now = now or datetime.now(UTC)
    key = dedupe_key(kind, subject_row)

    if not claim(db, user, kind, key, user.email, sent_at=now):
        return 'already-sent'
    try:
        subject, html = render(user, kind, subject_row, now=now)
        mail.send(user.email, subject, html, settings=settings)
    except Exception as e:
        release(db, user, kind, key)
        logger.error(f'{kind} alert for {user.email} ({key}) failed: {e}')
        return 'failed'
    logger.info(f'{kind} alert sent to {user.email} ({key})')
    return 'sent'


def run_due(db, users, now=None, settings=None):
    """One pass over everyone. Safe to run repeatedly and safe to run twice."""
    now = now or datetime.now(UTC)
    results = []

    for user in users:
        if not _eligible(user, now):
            continue

        for session in dropped_sessions(db, user, now=now):
            key = dedupe_key('session_dropped', session)
            if already_sent(db, user, 'session_dropped', key):
                continue
            results.append((user.email, 'session_dropped', key,
                            send_one(db, user, 'session_dropped', session,
                                     now=now, settings=settings)))

        for device in dormant_devices(db, user, now=now):
            key = dedupe_key('device_dormant', device)
            if already_sent(db, user, 'device_dormant', key):
                continue
            results.append((user.email, 'device_dormant', key,
                            send_one(db, user, 'device_dormant', device,
                                     now=now, settings=settings)))
    return results
