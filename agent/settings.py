"""Settings the server holds, polled by the agent.

Nothing here is configured on the laptop. Idle threshold, capture interval,
whether captures happen at all, and whether tracking is paused all live on the
server, so a person changing a setting in their browser does not have to touch
the machine — or be talked through touching it.

The pause matters most. The server also refuses paused uploads, so this is not
what enforces it; this is what stops the agent recording and capturing in the
first place, which is the difference between "your screenshots are discarded"
and "your screen is not photographed".
"""
import logging

from client import AuthError, TransientError

logger = logging.getLogger('agent.settings')

DEFAULTS = {
    'timezone': 'UTC',
    'idle_threshold_seconds': 600,
    'screenshot_interval_seconds': 600,
    'screenshots_enabled': True,
    'tracking_enabled': True,
    'day_goal_seconds': 8 * 3600,
    'week_goal_seconds': 40 * 3600,
}


class RemoteSettings:
    """Dict-like, so anything reading settings does not care where they came from.

    Starts from defaults and keeps the LAST KNOWN values when the server cannot
    be reached. Falling back to defaults on a network blip would silently
    un-pause someone, which is the one failure this must not have.
    """

    def __init__(self, values=None):
        self._values = dict(DEFAULTS)
        self._values.update(values or {})
        self.paused = False
        self.paused_until = None
        self.last_error = None

    def get(self, key, default=None):
        return self._values.get(key, default)

    def __getitem__(self, key):
        return self._values[key]

    def refresh(self, client):
        """Poll the server. True if the values were updated."""
        try:
            body = client.me()
        except AuthError as e:
            self.last_error = str(e)
            logger.error(f'Cannot read settings: {e}')
            return False
        except TransientError as e:
            self.last_error = str(e)
            logger.debug(f'Settings not refreshed: {e}')
            return False

        self._values.update(body.get('settings') or {})
        was_paused = self.paused
        self.paused = bool(body.get('paused'))
        self.paused_until = body.get('paused_until')
        self.last_error = None

        if self.paused and not was_paused:
            logger.info(f'Tracking paused by the user until {self.paused_until}')
        elif was_paused and not self.paused:
            logger.info('Tracking resumed by the user')
        return True
