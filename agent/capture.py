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
IDLE_THRESHOLD = 900         # 15 minutes of no input and the session pauses
ACTIVITY_WINDOW = 600        # the slice an activity percentage describes


def _now():
    return datetime.now(timezone.utc)


class ActivityMonitor:
    def __init__(self, spool, idle_source, window_source=None,
                 idle_threshold=IDLE_THRESHOLD, poll_interval=POLL_INTERVAL,
                 activity_window=ACTIVITY_WINDOW,
                 suspend_factor=SUSPEND_GAP_FACTOR,
                 min_span=MIN_SPAN_SECONDS,
                 heartbeat_interval=HEARTBEAT_INTERVAL,
                 settings=None):
        self.spool = spool
        # Server-held settings, if the agent is polling them. None means the
        # loop runs on its constructor arguments alone, which is what the tests
        # and a first run before the first poll do.
        self.settings = settings
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
        self._idle_uuid = None              # the break currently on record
        self.activity_window = activity_window
        # Minutes, not polls. A minute counts as active if ANY poll inside it
        # saw input, which is the difference between measuring work and
        # measuring typing speed: reading a paragraph between keystrokes is
        # work, and a per-poll count would score it as absence.
        self._window_start = None
        self._active_minutes = set()
        self._tracked_minutes = set()
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
        """Input stopped `idle_for` seconds ago. Pause, do not close.

        The session stays open and keeps its project, its task and everything
        recorded against it. Only the clock stops. Closing here — which is what
        this used to do — chopped a day on one project into a fresh session
        after every coffee, and made "carry on where I left off" impossible.

        The break is written to the spool immediately rather than on return, so
        an agent killed during it still leaves the gap on record. Otherwise the
        session would come back from the dead counting the whole absence.
        """
        started = now - timedelta(seconds=idle_for)
        self._idle_started = started
        self._flush_span(started)

        session = self.spool.open_session()
        if session:
            self._idle_uuid = self.spool.open_idle(started.isoformat())
            self.spool.set_idle_since(session['client_uuid'], started.isoformat())
            logger.info(
                f"Session '{session['project']}' paused at "
                f"{started.isoformat(timespec='seconds')} ({reason}, "
                f"{int(idle_for) // 60}m {int(idle_for) % 60}s)")
        else:
            # Nothing was running, so there is no session to pause and no break
            # worth recording against one.
            self._idle_uuid = None
        self.is_idle = True

    def _hold_pause(self, now):
        """Keep a running pause current while nobody is at the keyboard.

        Two things happen on every poll. The gap on record is extended, so a
        crash loses at most one poll rather than the whole thing. And the
        session is heartbeated — the agent IS alive, it is the person who is
        away — because going silent would have the server cap the session as
        abandoned after fifteen minutes and mail an alert about it.

        Nothing here decides the pause has gone on too long. A pause simply
        stops the clock and waits. Somebody who wants to stop for the day has a
        pause control of their own, and guessing on their behalf is what made
        this complicated the first time.
        """
        session = self.spool.open_session()
        if not session or not self._idle_started:
            return []

        if self._idle_uuid:
            self.spool.extend_idle(self._idle_uuid, now.isoformat())
        self.spool.heartbeat(session['client_uuid'], now.isoformat())
        return []

    def _return_from_idle(self, now):
        """Input is back. Close the gap and let the session count again."""
        if self._idle_uuid:
            self.spool.close_idle(self._idle_uuid, now.isoformat())
        elif self._idle_started:
            # Nothing was running when the idle began, so no record was opened
            # then. The gap is still worth logging.
            self.spool.record_idle(self._idle_started.isoformat(), now.isoformat())
        self._idle_started = self._idle_uuid = None

        session = self.spool.open_session()
        if session:
            self.spool.set_idle_since(session['client_uuid'], None)
            logger.info(f"Resumed '{session['project']}' — same session")

        self.is_idle = False
        self._app_started = now

    # ── Activity ─────────────────────────────────────────────────────────────

    def _window_of(self, now):
        """The clock-aligned slice `now` falls in.

        Aligned to the clock rather than to when tracking started, so two
        people's windows line up and a screenshot can be matched to one by its
        timestamp alone.
        """
        epoch = now.replace(hour=0, minute=0, second=0, microsecond=0)
        elapsed = int((now - epoch).total_seconds())
        return epoch + timedelta(seconds=elapsed - elapsed % self.activity_window)

    def _note_activity(self, now, saw_input, tracking):
        """Fold one poll into the window it belongs to, closing the last one
        if the clock has moved past it."""
        window = self._window_of(now)
        if self._window_start is not None and window != self._window_start:
            self._close_window(now)
        self._window_start = window

        if tracking:
            minute = now.replace(second=0, microsecond=0)
            self._tracked_minutes.add(minute)
            if saw_input:
                self._active_minutes.add(minute)

    def _close_window(self, now):
        """Bank the finished window. Silent if nothing was tracked in it —
        a window with no session behind it is not evidence of anything."""
        if self._window_start is None or not self._tracked_minutes:
            self._reset_window()
            return None

        session = self.spool.open_session()
        client_uuid = self.spool.record_activity_window(
            self._window_start.isoformat(),
            (self._window_start + timedelta(seconds=self.activity_window)).isoformat(),
            len(self._active_minutes), len(self._tracked_minutes),
            session['client_uuid'] if session else None)
        logger.debug(
            f'Activity {len(self._active_minutes)}/{len(self._tracked_minutes)} '
            f'min from {self._window_start.isoformat(timespec="minutes")}')
        self._reset_window()
        return client_uuid

    def _reset_window(self):
        self._window_start = None
        self._active_minutes = set()
        self._tracked_minutes = set()

    # ── One step ─────────────────────────────────────────────────────────────

    def tick(self, now=None, mono=None):
        """Advance the loop by one poll. Returns what it did, for tests and logs."""
        now = now or _now()
        mono = time.monotonic() if mono is None else mono
        events = []

        if self.settings is not None and self.settings.paused:
            # Close whatever is open, once, and record nothing further. The
            # server would refuse the uploads anyway; not recording is the
            # difference between "your data is discarded" and "your data is
            # not collected".
            if self.spool.open_session() is not None:
                self._flush_span(now)
                session = self.spool.open_session()
                self.spool.stop_session(session['client_uuid'], now.isoformat())
                events.append('paused')
                logger.info('Tracking paused — session closed')
            self._last_mono = mono
            # Tracking is off, so nothing is being measured — close whatever
            # window was open rather than letting it span the gap.
            self._close_window(now)
            return {'idle_seconds': 0.0, 'is_idle': self.is_idle,
                    'paused': True, 'events': events}

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

        if self.is_idle:
            # A pause is not nothing happening: the break on record has to keep
            # up with the clock, and the session has to be held alive.
            events += self._hold_pause(now)
        else:
            events += self._sample_window(now)
            self._maybe_heartbeat(now)

        # How much of this window had a person in it. `idle_for` is seconds
        # since the last input, so anything smaller than the gap since the last
        # poll means input landed inside it — no new permission, no record of
        # WHAT was pressed, just that something was.
        gap_seconds = self.poll_interval if gap is None else max(gap, 0.0)
        saw_input = idle_for < max(gap_seconds, 1.0)
        self._note_activity(now, saw_input,
                            tracking=not self.is_idle and self.spool.open_session()
                            is not None)

        return {'idle_seconds': idle_for, 'is_idle': self.is_idle,
                'paused': False, 'events': events}

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
        logger.info(f'Capture started (pause after {self.idle_threshold // 60}m idle, '
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
        # span — or the last activity window — the way a crash would.
        now = _now()
        self._flush_span(now)
        self._close_window(now)
