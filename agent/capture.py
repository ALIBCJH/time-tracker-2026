"""The capture loop: idle, suspend, and what was in the foreground.

The whole loop is one method, tick(), which is handed the time rather than
reading the clock itself. That is deliberate: the equivalent in the local app
called datetime.now() and time.monotonic() internally and shelled out to
xdotool, which made it impossible to test — so it was never tested, and its two
subtlest behaviours (crossing the idle threshold, and waking from suspend) could
only ever be verified by sitting in front of the machine and waiting ten
minutes. Here a whole day runs in milliseconds.

Time is credited to when it happened, not when it was noticed. A poll every few
seconds sees an idle counter that is already at eleven minutes; the session must
be cut back to where input actually stopped, not to the poll that spotted it.
"""
import logging
import time
from datetime import datetime, timedelta, timezone

from titles import normalise

logger = logging.getLogger('agent.capture')

POLL_INTERVAL = 5
SUSPEND_GAP_FACTOR = 3       # a loop gap this many polls long means the box froze
MIN_SPAN_SECONDS = 5         # shorter than this is a window flicker, not usage
HEARTBEAT_INTERVAL = 60      # how often an open session is re-marked alive


def _now():
    return datetime.now(timezone.utc)


class ActivityMonitor:
    def __init__(self, spool, idle_source, window_source=None,
                 idle_threshold=600, poll_interval=POLL_INTERVAL,
                 suspend_factor=SUSPEND_GAP_FACTOR,
                 min_span=MIN_SPAN_SECONDS,
                 heartbeat_interval=HEARTBEAT_INTERVAL):
        self.spool = spool
        self.idle_source = idle_source
        self.window_source = window_source
        self.idle_threshold = idle_threshold
        self.poll_interval = poll_interval
        self.suspend_factor = suspend_factor
        self.min_span = min_span
        self.heartbeat_interval = heartbeat_interval

        self.is_idle = False
        self._running = True
        self._app = self._title = self._app_started = None
        self._idle_started = None
        self._resume = None                 # (project, task) to reopen on return
        self._last_mono = None
        self._last_heartbeat = None

    # ── Spans ────────────────────────────────────────────────────────────────

    def _flush_span(self, ended_at):
        """Bank the foreground span that just finished."""
        if not (self._app and self._app_started):
            self._app = self._title = self._app_started = None
            return
        if (ended_at - self._app_started).total_seconds() >= self.min_span:
            session = self.spool.open_session()
            self.spool.record_app_usage(
                self._app, self._title or '', self._app_started.isoformat(),
                ended_at.isoformat(),
                session['client_uuid'] if session else None)
        self._app = self._title = self._app_started = None

    # ── Idle ─────────────────────────────────────────────────────────────────

    def _go_idle(self, idle_for, now, reason='idle'):
        """Input stopped `idle_for` seconds ago. Roll everything back to then."""
        started = now - timedelta(seconds=idle_for)
        self._idle_started = started
        self._flush_span(started)

        session = self.spool.open_session()
        if session:
            self.spool.stop_session(session['client_uuid'], started.isoformat())
            self._resume = (session['project'], session['task'])
            logger.info(
                f"Session '{session['project']}' closed at "
                f"{started.isoformat(timespec='seconds')} ({reason}, "
                f"{int(idle_for) // 60}m {int(idle_for) % 60}s)")
        else:
            # Nothing was running, so there is nothing to reopen later.
            self._resume = None
        self.is_idle = True

    def _return_from_idle(self, now):
        if self._idle_started:
            self.spool.record_idle(self._idle_started.isoformat(), now.isoformat())
        self._idle_started = None

        if self._resume:
            project, task = self._resume
            self.spool.start_session(project, task, now.isoformat())
            logger.info(f"Resumed '{project}' after idle")
            self._resume = None

        self.is_idle = False
        self._app_started = now

    # ── One step ─────────────────────────────────────────────────────────────

    def tick(self, now=None, mono=None):
        """Advance the loop by one poll. Returns what it did, for tests and logs."""
        now = now or _now()
        mono = time.monotonic() if mono is None else mono
        events = []

        try:
            idle_for = float(self.idle_source.idle_seconds())
        except Exception as e:
            # A failed poll must not read as "the user left" — that would close
            # a live session over a transient X hiccup.
            logger.warning(f'Idle query failed: {e}')
            idle_for = 0.0
            events.append('idle-query-failed')

        # A gap far longer than the poll interval means the machine was frozen
        # — suspended lid, or a stall. Nothing happened during it, so any open
        # session is rolled back to where the machine went away.
        gap = None if self._last_mono is None else mono - self._last_mono
        self._last_mono = mono
        if (gap is not None and gap > self.poll_interval * self.suspend_factor
                and not self.is_idle and self.spool.open_session()):
            logger.info(f'{int(gap)}s gap — treating as suspend')
            self._go_idle(gap, now, reason='suspend')
            events.append('suspend')

        if idle_for > self.idle_threshold and not self.is_idle:
            self._go_idle(idle_for, now)
            events.append('went-idle')
        elif idle_for <= self.idle_threshold and self.is_idle:
            self._return_from_idle(now)
            events.append('returned')

        if not self.is_idle:
            events += self._sample_window(now)
            self._maybe_heartbeat(now)

        return {'idle_seconds': idle_for, 'is_idle': self.is_idle, 'events': events}

    def _sample_window(self, now):
        if not self.window_source:
            return []
        try:
            app, title = self.window_source.active_window()
        except Exception as e:
            logger.debug(f'Window query failed: {e}')
            return []
        if not app:
            return []

        # Compare — and store — the normalised title. A spinner or an unread
        # count changing is the same window, not a new activity.
        title = normalise(title)
        if app == self._app and title == self._title:
            return []

        self._flush_span(now)
        self._app, self._title, self._app_started = app, title, now
        return ['window-changed']

    def _maybe_heartbeat(self, now):
        """Re-mark the open session alive, but not on every poll: a heartbeat
        makes the row dirty, and dirtying it every few seconds would have the
        agent re-uploading the same session all day."""
        session = self.spool.open_session()
        if not session:
            return
        if (self._last_heartbeat is None
                or (now - self._last_heartbeat).total_seconds() >= self.heartbeat_interval):
            self.spool.heartbeat(session['client_uuid'], now.isoformat())
            self._last_heartbeat = now

    # ── The loop ─────────────────────────────────────────────────────────────

    def run(self):
        logger.info(f'Capture started (idle threshold {self.idle_threshold // 60}m, '
                    f'poll {self.poll_interval}s)')
        while self._running:
            try:
                self.tick()
            except Exception as e:
                # One bad poll must not end the day's tracking.
                logger.exception(f'Capture tick failed: {e}')
            time.sleep(self.poll_interval)

    def stop(self):
        self._running = False
        # Bank whatever is in flight so a clean shutdown does not lose the last
        # span the way a crash would.
        self._flush_span(_now())
