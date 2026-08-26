"""Deciding, claiming, rendering, and sending. The whole loop in one place.

Ordering matters and is deliberate: the claim is taken BEFORE the mail goes
out. Two workers reaching the same report at the same instant cannot both send,
because the loser's INSERT violates a unique constraint. The cost is that a
crash between claiming and sending loses one report; the other ordering costs a
duplicate, and a duplicate report is worse than a missing one you can trigger
by hand.
"""
import logging

from datetime import datetime, timezone

from app.reports import data, render, schedule
from app.services import mail

logger = logging.getLogger('reports.send')
UTC = timezone.utc


def recipients_for(user):
    cc = [address for address in (user.settings.report_cc or []) if address]
    return user.email, cc


def build(db, user, kind, period, now=None, embed=None):
    """(subject, html) for one report. `period` is (monday,) or (year, month)."""
    now = now or datetime.now(UTC)
    if kind == 'weekly':
        payload = data.weekly(db, user, period[0], now=now)
        return render.render_weekly(payload, now=now, embed=embed)
    payload = data.monthly(db, user, period[0], period[1], now=now)
    return render.render_monthly(payload, now=now, embed=embed)


def send_report(db, user, kind, period_key, period, now=None, settings=None):
    """Claim, render, send. Returns 'sent', 'already-sent', or 'failed'."""
    now = now or datetime.now(UTC)
    to, cc = recipients_for(user)

    if not schedule.claim(db, user, kind, period_key, to, sent_at=now):
        return 'already-sent'

    images = {}
    try:
        subject, html = build(db, user, kind, period, now=now,
                              embed=render.cid_embedder(images))
        mail.send(to, subject, html, images=images, cc=cc, settings=settings)
    except Exception as e:
        # Give the claim back so the next run may retry. Keeping it would turn
        # one bad SMTP minute into a permanently missing report.
        schedule.release(db, user, kind, period_key)
        logger.error(f'{kind} report for {user.email} ({period_key}) failed: {e}')
        return 'failed'
    return 'sent'


def run_due(db, users, now=None, settings=None):
    """One pass over everyone. Safe to run every minute.

    A user with no send history is SEEDED rather than sent to: a new account, or
    a restored database, must not read "I have never sent anything" as "I owe
    you everything". That is exactly how the local app mailed a real person a
    backdated report the first time it restarted without its state file.
    """
    now = now or datetime.now(UTC)
    results = []

    for user in users:
        if not user.is_active or not user.settings.reports_enabled:
            continue

        for kind, due in (('weekly', schedule.weekly_due(user, now)),
                          ('monthly', schedule.monthly_due(user, now))):
            if due is None:
                continue
            period_key, period = due[0], due[1:]

            if not schedule.has_history(db, user, kind):
                schedule.seed(db, user, kind, period_key)
                logger.info(f'{kind} reports armed for {user.email} — first one is '
                            f'the next period, not a backdated {period_key}')
                results.append((user.email, kind, period_key, 'seeded'))
                continue

            if schedule.already_sent(db, user, kind, period_key):
                continue

            outcome = send_report(db, user, kind, period_key, period,
                                  now=now, settings=settings)
            results.append((user.email, kind, period_key, outcome))
    return results
