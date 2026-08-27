"""Scheduled work, in its own process.

This is the single most important structural difference from the local app.
There, the mailer, the screenshot service and the daily prompt all ran as
threads inside the Flask process — which is fine when there is exactly one
process and fatal the moment there are four. Four Gunicorn workers would mean
four weekly emails, four monthly reports, four of everything.

So none of it runs in the web process. The web process answers requests; this
one does work on a clock.

Every job here is safe to run twice, because "exactly one worker" is not a
property anyone should have to maintain by hand:

  * sending reports is guarded by a unique constraint, so a second worker
    loses the claim and does not send;
  * closing orphaned sessions is idempotent — a session already capped is not
    open, so it is not selected again;
  * refreshing a draft rewrites the same row rather than adding one;
  * an alert about a dead agent is claimed the same way a report is, so the
    second worker to notice the same dead agent does not send a second email.

That means a deploy that briefly runs two workers, or a forgotten container on
another host, degrades to wasted CPU rather than duplicate mail.
"""
import logging
import signal
import time
from datetime import datetime, timedelta, timezone

from app.db import session_scope
from app.models import User
from app.reports import send as reports
from app.services import activity_log as AL
from app.services import alerts as agent_alerts
from app.services import reporting as R
from app.services.mail import NotConfigured
from app.services.sessions import (close_orphaned_sessions,
                                   close_sessions_paused_overnight)

logger = logging.getLogger('worker')
UTC = timezone.utc

TICK_SECONDS = 60

# How often each job runs, in seconds.
INTERVALS = {
    'reports': 60,       # the send window is an hour wide; a minute is plenty
    'orphans': 300,      # a dead agent is not urgent, but should not linger
    'drafts': 900,       # the prompt reads these; they need only be fresh-ish
    # Runs after orphans has had a chance to cap anything, and the conditions
    # it reports on are measured in hours and days — a minute either way in
    # noticing them changes nothing.
    'alerts': 600,
    # A disk fills over days. Hourly is often enough to act on and rare enough
    # to be free.
    'disk': 3600,
}

# How many recent local days of draft to rebuild. More than one, so a day whose
# usage arrived late — an agent uploading a backlog — gets an accurate draft
# rather than the empty one written while its data was still on a laptop.
DRAFT_DAYS = 3


def active_users(db):
    return db.query(User).filter(User.is_active.is_(True)).all()


def run_reports(now=None):
    with session_scope() as db:
        try:
            results = reports.run_due(db, active_users(db), now=now)
        except NotConfigured as e:
            # Nothing to retry until someone configures SMTP. Say so once per
            # tick rather than raising and restarting the process in a loop.
            logger.warning(f'Reports skipped: {e}')
            return []
    for email, kind, period, outcome in results:
        logger.info(f'{kind} {period} for {email}: {outcome}')
    return results


def run_orphans(now=None):
    """Two different endings, both idempotent.

    An orphan is a session whose agent died — capped at its last heartbeat. An
    overnight pause is a session whose agent is alive and whose person is not —
    ended where input stopped, once their day has rolled over. Neither selects
    a session the other has already closed, because a closed session is not
    open.
    """
    with session_scope() as db:
        capped = close_orphaned_sessions(db, now=now)
        overnight = close_sessions_paused_overnight(db, active_users(db), now=now)
        return capped + overnight


def run_alerts(now=None):
    """Tell people when their own tracking stopped working.

    Ordered after orphans in JOBS so a session capped this tick is available to
    be reported on in the same tick rather than ten minutes later. Dict order is
    insertion order, and tick() iterates it, so this is load-bearing rather than
    decorative.
    """
    with session_scope() as db:
        try:
            results = agent_alerts.run_due(db, active_users(db), now=now)
        except NotConfigured as e:
            logger.warning(f'Agent alerts skipped: {e}')
            return []
    for email, kind, key, outcome in results:
        logger.info(f'{kind} for {email} ({key}): {outcome}')
    return results


def run_disk(now=None):
    """Warn the administrators before the disk stops Postgres writing.

    Its own job rather than part of run_alerts, because it reports one fact
    about the machine to whoever can act on it, while that one reports to each
    person about their own tracking.
    """
    with session_scope() as db:
        try:
            results = agent_alerts.run_disk_check(db, now=now)
        except NotConfigured as e:
            logger.warning(f'Disk alert skipped: {e}')
            return []
    for email, kind, key, outcome in results:
        logger.info(f'{kind} for {email} ({key}): {outcome}')
    return results


def run_drafts(now=None):
    now = now or datetime.now(UTC)
    refreshed = 0
    with session_scope() as db:
        for user in active_users(db):
            today = R.logical_today(user, now)
            for offset in range(DRAFT_DAYS):
                AL.refresh_draft(db, user, today - timedelta(days=offset), now=now)
                refreshed += 1
    return refreshed


JOBS = {'reports': run_reports, 'orphans': run_orphans,
        'alerts': run_alerts, 'disk': run_disk, 'drafts': run_drafts}


class Worker:
    def __init__(self, intervals=None, jobs=None, clock=None):
        self.intervals = dict(intervals or INTERVALS)
        self.jobs = dict(jobs or JOBS)
        self._clock = clock or (lambda: datetime.now(UTC))
        self._last = {}
        self._running = True

    def due(self, name, now):
        last = self._last.get(name)
        return last is None or (now - last).total_seconds() >= self.intervals[name]

    def tick(self, now=None):
        """One pass. Returns the jobs that ran, for tests and logs."""
        now = now or self._clock()
        ran = []
        for name, job in self.jobs.items():
            if not self.due(name, now):
                continue
            try:
                job(now=now)
            except Exception as e:
                # One failing job must not stop the others, or a broken SMTP
                # server would also stop orphaned sessions being closed.
                logger.exception(f'Job {name} failed: {e}')
            finally:
                # Stamped even on failure, so a persistently broken job retries
                # on its interval instead of on every single tick.
                self._last[name] = now
            ran.append(name)
        return ran

    def run(self):
        logger.info('Worker started — ' + ', '.join(
            f'{name} every {seconds}s' for name, seconds in self.intervals.items()))
        signal.signal(signal.SIGTERM, self._stop)
        signal.signal(signal.SIGINT, self._stop)
        while self._running:
            self.tick()
            # Sleep in slices so a SIGTERM is answered promptly rather than up
            # to a minute later, which is what makes a rolling deploy quick.
            for _ in range(TICK_SECONDS):
                if not self._running:
                    break
                time.sleep(1)
        logger.info('Worker stopped.')

    def _stop(self, *_):
        self._running = False


def main():
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(name)s] %(levelname)s: %(message)s')
    from app import create_app
    app = create_app()
    with app.app_context():
        Worker().run()


if __name__ == '__main__':
    main()
