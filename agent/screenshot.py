"""Taking a screen capture, and shrinking it before it ever leaves the laptop.

Encoded to WebP here rather than uploaded as PNG, and the difference is not
marginal: measured on a real month of captures, WebP q80 is 21% the size of the
PNG the local app stored. That is five times less of someone's mobile data
spent uploading pictures of their own screen, which matters far more than the
storage bill it also cuts.

Two objects per capture, because they have different lifetimes: the full frame
expires in weeks, the thumbnail lives a year. The timeline outlives the
evidence.
"""
import logging
import os
import subprocess
import uuid
from datetime import datetime, timezone

logger = logging.getLogger('agent.screenshot')

QUALITY = 80
THUMB_WIDTH = 400
CAPTURE_TIMEOUT = 10

# Tried in order; the first that works on this desktop wins.
BACKENDS = (
    ['scrot', '-o', '{path}'],
    ['import', '-window', 'root', '{path}'],
    ['gnome-screenshot', '-f', '{path}'],
    ['spectacle', '-b', '-n', '-o', '{path}'],
)

SOUND = '/usr/share/sounds/freedesktop/stereo/message.oga'


class CaptureUnavailable(RuntimeError):
    """No working screenshot tool. Tracking continues without pictures."""


def _run(argv):
    try:
        result = subprocess.run(argv, capture_output=True, timeout=CAPTURE_TIMEOUT)
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def detect_backend(tmp_dir):
    # Created here, not assumed: on a fresh install this directory does not
    # exist yet, every probe fails to write, and the agent concludes the
    # machine has no screenshot tool at all.
    os.makedirs(tmp_dir, exist_ok=True)
    probe = os.path.join(tmp_dir, '.probe.png')
    for template in BACKENDS:
        argv = [part.format(path=probe) for part in template]
        if _run(argv) and os.path.exists(probe) and os.path.getsize(probe) > 0:
            os.remove(probe)
            logger.info(f'Screen capture using {argv[0]}')
            return template
        if os.path.exists(probe):
            os.remove(probe)
    raise CaptureUnavailable('no working screenshot tool found')


def capture(out_dir, backend, captured_at=None, quality=QUALITY,
            thumb_width=THUMB_WIDTH, notify=True):
    """(client_uuid_stub, full_path, thumb_path, captured_at). Raises on failure."""
    from PIL import Image                                  # only needed here

    captured_at = captured_at or datetime.now(timezone.utc)
    os.makedirs(out_dir, exist_ok=True)
    stem = f"{captured_at.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"
    raw = os.path.join(out_dir, f'{stem}.png')

    argv = [part.format(path=raw) for part in backend]
    if not _run(argv) or not os.path.exists(raw):
        raise CaptureUnavailable(f'{argv[0]} produced nothing')

    full = os.path.join(out_dir, f'{stem}-full.webp')
    thumb = os.path.join(out_dir, f'{stem}-thumb.webp')
    try:
        with Image.open(raw) as image:
            image = image.convert('RGB')
            image.save(full, 'WEBP', quality=quality)
            small = image.copy()
            small.thumbnail((thumb_width, thumb_width * 2))
            small.save(thumb, 'WEBP', quality=quality)
    finally:
        # The PNG is the largest thing on disk and is never uploaded.
        if os.path.exists(raw):
            os.remove(raw)

    if notify:
        _chime()
    return full, thumb, captured_at


def _chime():
    """Say out loud that a capture happened. Someone being recorded should be
    able to tell without watching a status light."""
    if not os.path.exists(SOUND):
        return
    try:
        subprocess.Popen(['paplay', SOUND],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        pass


class ScreenshotService:
    """Captures on an interval, but only while there is something to capture.

    Three conditions, all required: a session is running, the person is not
    idle, and captures are switched on. Anything less fills the store with
    pictures of a locked screen, and — worse — with pictures taken while nobody
    agreed to be recorded.
    """

    def __init__(self, spool, out_dir, settings, backend=None, clock=None):
        self.spool = spool
        self.out_dir = out_dir
        self.settings = settings
        self.backend = backend
        self._last = None
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def ensure_backend(self):
        if self.backend is None:
            self.backend = detect_backend(self.out_dir)
        return self.backend

    def due(self, now, is_idle):
        if not self.settings.get('screenshots_enabled', True):
            return False
        if is_idle or self.spool.open_session() is None:
            return False
        if self._last is None:
            return True
        interval = self.settings.get('screenshot_interval_seconds', 600)
        return (now - self._last).total_seconds() >= interval

    def tick(self, is_idle=False, now=None):
        now = now or self._clock()
        if not self.due(now, is_idle):
            return None
        try:
            full, thumb, captured_at = capture(self.out_dir, self.ensure_backend(),
                                               captured_at=now)
        except (CaptureUnavailable, OSError, ImportError) as e:
            # Never fatal: a machine with no capture tool still tracks time.
            logger.warning(f'Capture skipped: {e}')
            self._last = now
            return None

        session = self.spool.open_session()
        client_uuid = self.spool.record_screenshot(
            captured_at.isoformat(), full, thumb,
            session['client_uuid'] if session else None)
        self._last = now
        return client_uuid
