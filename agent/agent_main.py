#!/usr/bin/env python3
"""The agent: capture, spool, upload, and the widget.

One process with a Qt event loop driving everything on timers, rather than the
local app's several daemon threads. The reason is not elegance — it is that a
thread that dies takes its job with it silently, and the local app lost its
input listeners that way more than once. A timer that stops is visible, because
the face stops moving.

The web dashboard is the source of truth for settings; the laptop is the source
of truth for what happened on it. Neither waits for the other.
"""
import logging
import os
import sys
import webbrowser

logging.basicConfig(level=os.environ.get('LOG_LEVEL', 'INFO'),
                    format='%(asctime)s [%(name)s] %(levelname)s: %(message)s')
logger = logging.getLogger('agent')

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config
from client import Client
from capture import ActivityMonitor
from platform_x11 import detect_sources
from screenshot import ScreenshotService, detect_backend
from settings import RemoteSettings
from spool import Spool
from sync import flush_all, flush_screenshots
from widget.state import WidgetState

POLL_MS = 5_000            # the capture loop
SYNC_MS = 60_000           # uploading
SETTINGS_MS = 120_000      # settings and the pause
PROMPT_MS = 15_000         # the daily card


class Controller:
    """Wires the pieces together and owns the timers."""

    def __init__(self):
        conf = config.load()
        self.server = conf['server']
        self.client = Client(self.server, conf['token'])
        self.spool = Spool()
        self.settings = RemoteSettings()
        self.state = WidgetState(self.spool, self.settings,
                                 name=os.environ.get('TIMETRACKER_USER_NAME'))
        self.state.refresh_from_spool()

        # A pause left mid-flight by a crash is settled before anything else
        # runs: the gap is closed so it uploads, and the session is un-marked so
        # the server does not hold it at the moment of the pause all day.
        gaps, sessions = self.spool.settle_interrupted_pause()
        if gaps or sessions:
            logger.info(f'Settled an interrupted pause — {gaps} idle period(s), '
                        f'{sessions} session(s) resumed')

        idle_source, window_source = detect_sources()
        self.monitor = ActivityMonitor(
            self.spool, idle_source, window_source,
            idle_threshold=self.settings.get('idle_threshold_seconds'),
            settings=self.settings) if idle_source else None
        if self.monitor is None:
            logger.error('No idle source — time cannot be tracked on this display.')

        shots_dir = os.path.join(self.spool.dir, 'shots')
        try:
            backend = detect_backend(shots_dir)
        except Exception as e:
            backend = None
            logger.warning(f'Screen capture unavailable: {e}')
        self.shots = ScreenshotService(self.spool, shots_dir, self.settings,
                                       backend=backend)
        self.widget = None
        self.card = None

    # ── Timers ───────────────────────────────────────────────────────────────

    def poll(self):
        result = self.monitor.tick() if self.monitor else {'is_idle': False}
        if not result.get('paused'):
            self.shots.tick(is_idle=result.get('is_idle', False))
        cheer = self.state.tick(is_idle=result.get('is_idle', False))
        self.state.refresh_from_spool()
        if self.widget:
            self.widget.render_state()
            if cheer:
                self.widget.window().setToolTip(cheer['title'])
                logger.info(f"{cheer['glyph']} {cheer['title']} — {cheer['sub']}")

    def sync(self):
        result = flush_all(self.spool, self.client)
        if result['status'] in ('ok', 'idle'):
            self.state.note_contact()
        flush_screenshots(self.spool, self.client)

    def refresh_settings(self):
        from settings import reconcile_session
        if not self.settings.refresh(self.client):
            return
        self.state.note_contact()
        if self.monitor:
            self.monitor.idle_threshold = self.settings.get('idle_threshold_seconds')

        outcome = reconcile_session(self.spool, self.settings.server_session)
        if outcome not in ('agreed', 'pending-upload'):
            logger.info(f'Session reconciled with the server: {outcome}')
            self.state.refresh_from_spool()
            if self.widget:
                self.widget.render_state()

    def refresh_prompts(self):
        from client import AuthError, TransientError
        try:
            idle = self.monitor.idle_source.idle_seconds() if self.monitor else None
            self.state.set_prompts(self.client.activity_log_pending(idle))
            self.state.note_contact()
        except (AuthError, TransientError) as e:
            logger.debug(f'Prompts not refreshed: {e}')
            return
        self.show_prompt()

    def show_prompt(self):
        """Raise the evening card, if one is due and none is already up.

        Only ever one at a time, and never replaced while it is open: replacing
        a card someone is halfway through typing into loses what they wrote.
        """
        if self.card is not None and self.card.isVisible():
            return
        prompt = self.state.pending_prompt
        if prompt is None:
            return

        from widget.prompt import PromptCard
        self.card = PromptCard(prompt, self.answer_prompt)
        if self.widget:
            geometry = self.widget.frameGeometry()
            self.card.move(geometry.left(), geometry.bottom() + 10)
        self.card.show()

    def answer_prompt(self, date, note, status):
        from client import AuthError, TransientError
        try:
            self.client.answer_activity_log(date, note, status)
            self.state.set_prompts(
                [p for p in self.state.prompts if p['date'] != date])
        except (AuthError, TransientError) as e:
            # The card is already closing. Leaving the day in the queue means
            # it comes back rather than the answer being silently lost.
            logger.warning(f'Could not save that day: {e}')

    # ── Tray actions ─────────────────────────────────────────────────────────

    def pause(self):
        """Opens the settings page rather than pausing from here.

        The pause is the user's own control and lives on the server; a local
        button that only stopped this process would leave someone believing
        they had stopped something they had not.
        """
        webbrowser.open(f'{self.server}/settings')

    def open_dashboard(self):
        webbrowser.open(self.server)


def main():
    from PyQt6.QtCore import QTimer
    from PyQt6.QtWidgets import QApplication

    from widget.app import Widget, tray_icon

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)      # closing the card is not quitting

    controller = Controller()
    controller.widget = Widget(controller.state, on_sync=controller.sync)
    controller.widget.show()
    icon = tray_icon(app, controller.widget, controller.state, controller)

    for interval, job in ((POLL_MS, controller.poll),
                          (SYNC_MS, controller.sync),
                          (SETTINGS_MS, controller.refresh_settings),
                          (PROMPT_MS, controller.refresh_prompts)):
        timer = QTimer(app)
        timer.timeout.connect(job)
        timer.start(interval)

    controller.refresh_settings()
    controller.poll()
    logger.info(f'Agent running against {controller.server}')
    sys.exit(app.exec())


if __name__ == '__main__':
    main()
