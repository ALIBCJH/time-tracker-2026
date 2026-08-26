"""The daily prompt: a draft the machine writes, waiting for a sentence from you.

The machine can say what was on screen. It cannot say what any of it was for.
So each day gets one row holding both halves — the observed account, refreshed
while the day is still open, and the note you write once, which is the only
thing in the system that knows *why*.

The rules below are all about not being annoying, and each exists because the
obvious alternative is worse:

  * a day you never answered stays queued indefinitely rather than expiring, so
    a day spent away from the machine is waiting when you get back rather than
    silently lost;
  * a day is only *asked about* in the evening window and only while you are
    actually at the keyboard — otherwise the card sits on an empty desktop
    since 21:00 and gets answered at midnight about an evening spent elsewhere;
  * a day you answered early comes back only if the work you did afterwards is
    worth a sentence, and only once the day has closed. Asking the same evening
    would interrupt the very work it is asking about, and the gap it measures
    would still be growing while you answered;
  * a day you waved away never returns. Skipping is meant to be final.
"""
import json
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, or_

from app.models import ActivityLog, AppUsage
from app.services import reporting as R
from app.services.summarize import headline, summarise

logger = logging.getLogger('activity_log')

# Seconds of undescribed work before an answered day is worth reopening. Below
# this the tail is a browser tab left open, not an evening.
MIN_TOPUP_SECONDS = 900

# A day with less tracked time than this has nothing to say. Without the floor,
# a weekend where the laptop was opened to check one thing queues a prompt.
MIN_DAY_SECONDS = 300

# Idle seconds below which someone counts as being at the machine. The same
# ground truth the capture loop uses: "a person is here", rather than "a session
# happens to be open", which the idle cutoff closes while they read something.
PRESENT_IDLE_SECONDS = 300

STATUSES = ('draft', 'confirmed', 'skipped')


def refresh_draft(db, user, day=None, now=None):
    """Write (or rewrite) the machine's half of a day's log.

    Idempotent by design: this runs repeatedly all evening and rewrites the same
    row rather than accumulating one per attempt. `note` is deliberately not
    touched — it is the only thing here you wrote.
    """
    now = now or datetime.now(timezone.utc)
    day = day or R.logical_today(user, now)
    window_start, window_end = R.day_window(user, day)

    rows = (db.query(AppUsage)
            .filter(AppUsage.user_id == user.id,
                    AppUsage.started_at < window_end,
                    AppUsage.ended_at > window_start)
            .order_by(AppUsage.started_at).all())
    activities, tracked = summarise(rows, window_start, window_end)

    log = (db.query(ActivityLog)
           .filter(ActivityLog.user_id == user.id, ActivityLog.log_date == day)
           .one_or_none())
    if log is None:
        log = ActivityLog(user_id=user.id, log_date=day)
        db.add(log)

    log.headline = headline(activities)
    log.draft = log.headline
    log.activities = activities
    log.tracked_seconds = tracked
    db.commit()
    return log


def pending(db, user, now=None, idle_seconds=None, tracking_enabled=True):
    """The prompts waiting right now, oldest first.

    Every row carries a `kind`: 'new' for a day never described, 'topup' for one
    answered before it was over. And an `active` flag — the ones worth
    interrupting for. `active` needs three things at once: the day is askable,
    tracking is on, and the person is at the machine. Everything else is a badge,
    not a pop-up.

    idle_seconds of None means "no idea" (a CLI, a test) and reads as present
    rather than silently suppressing everything.
    """
    now = now or datetime.now(timezone.utc)
    today = R.logical_today(user, now)
    local_hour = now.astimezone(R.user_tz(user)).hour
    settings = user.settings

    in_window = settings.prompt_start_hour <= local_hour < settings.prompt_end_hour
    present = bool(tracking_enabled) and (
        idle_seconds is None or idle_seconds < PRESENT_IDLE_SECONDS)

    due = []
    for log in _queue(db, user, today):
        is_today = log.log_date == today
        # Today is not askable before the window opens. It stays askable to
        # midnight, at which point it becomes a past day anyway — so there is no
        # moment where the prompt silently gives up on a day still being lived.
        if is_today and local_hour < settings.prompt_start_hour:
            continue
        if log.tracked_seconds < MIN_DAY_SECONDS:
            continue

        row = {
            'date': log.log_date.isoformat(),
            'headline': log.headline,
            'draft': log.draft,
            'note': log.note,
            'tracked_seconds': log.tracked_seconds,
            'activities': log.activities or [],
            'status': log.status,
            'kind': 'new' if log.status == 'draft' else 'topup',
            'is_today': is_today,
            'active': present and ((not is_today) or in_window),
        }
        if row['kind'] == 'topup':
            row['undescribed_seconds'] = (
                log.tracked_seconds - (log.answered_seconds or log.tracked_seconds))
        due.append(row)
    return due


def _queue(db, user, today):
    """Days still owed an answer: never described, or described early and since
    grown by enough to be worth a sentence.

    A NULL answered_seconds — a day settled before that column existed — reads
    as finished, so a deployment does not reopen history.
    """
    grown = (ActivityLog.tracked_seconds
             - func.coalesce(ActivityLog.answered_seconds, ActivityLog.tracked_seconds))
    return (db.query(ActivityLog)
            .filter(ActivityLog.user_id == user.id,
                    or_(ActivityLog.status == 'draft',
                        # A top-up is only offered once the day has closed.
                        (ActivityLog.status == 'confirmed')
                        & (ActivityLog.log_date < today)
                        & (grown >= MIN_TOPUP_SECONDS)))
            .order_by(ActivityLog.log_date)
            .limit(14).all())


def answer(db, user, day, note, status='confirmed', now=None):
    """Record what you said about a day.

    answered_seconds is stamped with the total AT THE MOMENT OF ANSWERING, which
    is what makes a top-up measurable later: everything past it is work the note
    does not mention.
    """
    if status not in ('confirmed', 'skipped'):
        raise ValueError(f'unknown status: {status!r}')
    note = (note or '').strip()
    if status == 'confirmed' and not note:
        # An empty confirm would record the day as answered while saying nothing
        # about it — worse than leaving it in the queue.
        raise ValueError('note is empty')

    now = now or datetime.now(timezone.utc)
    log = (db.query(ActivityLog)
           .filter(ActivityLog.user_id == user.id, ActivityLog.log_date == day)
           .one_or_none())
    if log is None:
        raise LookupError(f'no log for {day}')

    log.note = note
    log.status = status
    log.answered_at = now
    log.answered_seconds = log.tracked_seconds
    db.commit()
    return log


def rebaseline(db, user, day):
    """"Leave as is" on a top-up: the undescribed tail is accepted as not worth
    describing, and the note already written stands.

    This cannot go through answer() — that would overwrite the note with the
    empty string the card sends alongside it.
    """
    log = (db.query(ActivityLog)
           .filter(ActivityLog.user_id == user.id, ActivityLog.log_date == day,
                   ActivityLog.status == 'confirmed')
           .one_or_none())
    if log is None:
        return None
    log.answered_seconds = log.tracked_seconds
    db.commit()
    return log


def history(db, user, limit=30):
    """Answered days, newest first — the narrative."""
    logs = (db.query(ActivityLog)
            .filter(ActivityLog.user_id == user.id, ActivityLog.status != 'draft')
            .order_by(ActivityLog.log_date.desc()).limit(limit).all())
    return [{'date': l.log_date.isoformat(), 'note': l.note, 'status': l.status,
             'headline': l.headline, 'tracked_seconds': l.tracked_seconds,
             'activities': l.activities or []} for l in logs]
