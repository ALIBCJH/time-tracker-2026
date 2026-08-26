"""Talking to X11. The only part of the agent that is not portable.

Idle time comes from the XScreenSaver extension via ctypes rather than
python-xlib, which removes a dependency from a program that has to install on
other people's machines. It is the same counter either way: seconds since the
last real input, maintained by the X server.

Polling a counter beats keeping an input listener alive. A listener is a
long-lived object that can die quietly — the local app's pynput listeners did,
with broken-pipe crashes, and a dead listener reads as "idle for ever", which
silently stops recording. A counter has no state to lose and survives screen
locks and suspend.
"""
import ctypes
import ctypes.util
import logging
import subprocess

logger = logging.getLogger('agent.x11')


class _ScreenSaverInfo(ctypes.Structure):
    _fields_ = [('window', ctypes.c_ulong), ('state', ctypes.c_int),
                ('kind', ctypes.c_int), ('til_or_since', ctypes.c_ulong),
                ('idle', ctypes.c_ulong), ('event_mask', ctypes.c_ulong)]


class X11IdleSource:
    """Seconds since the last input, from the X server."""
    name = 'x11-screensaver'

    def __init__(self, display_name=None):
        self._x11 = ctypes.CDLL(ctypes.util.find_library('X11') or 'libX11.so.6')
        self._xss = ctypes.CDLL(ctypes.util.find_library('Xss') or 'libXss.so.1')
        self._xss.XScreenSaverAllocInfo.restype = ctypes.POINTER(_ScreenSaverInfo)
        self._x11.XOpenDisplay.restype = ctypes.c_void_p
        self._x11.XDefaultRootWindow.restype = ctypes.c_ulong
        self._display_name = display_name.encode() if display_name else None
        self._info = self._xss.XScreenSaverAllocInfo()
        self._connect()
        self.idle_seconds()          # fail now, not on the first poll

    def _connect(self):
        self._display = self._x11.XOpenDisplay(self._display_name)
        if not self._display:
            raise RuntimeError('cannot open the X display')
        self._root = self._x11.XDefaultRootWindow(ctypes.c_void_p(self._display))

    def idle_seconds(self):
        ok = self._xss.XScreenSaverQueryInfo(
            ctypes.c_void_p(self._display), ctypes.c_ulong(self._root), self._info)
        if not ok:
            # The connection drops across a lock or a suspend. Reconnect once
            # rather than treating it as "the user is gone".
            self._connect()
            ok = self._xss.XScreenSaverQueryInfo(
                ctypes.c_void_p(self._display), ctypes.c_ulong(self._root), self._info)
            if not ok:
                raise RuntimeError('XScreenSaverQueryInfo failed')
        return self._info.contents.idle / 1000.0


class XdotoolWindowSource:
    """(app_name, window_title) for the focused window, or (None, None).

    The app name comes from /proc/<pid>/comm rather than the window title,
    because a title is whatever the application feels like writing and the
    process name is stable.
    """
    name = 'xdotool'

    def __init__(self, timeout=2):
        self.timeout = timeout
        if self._run(['xdotool', 'getactivewindow']) is None:
            raise RuntimeError('xdotool is unavailable or there is no active window')

    def _run(self, argv):
        try:
            r = subprocess.run(argv, capture_output=True, text=True, timeout=self.timeout)
        except (OSError, subprocess.SubprocessError):
            return None
        return r.stdout.strip() if r.returncode == 0 else None

    def active_window(self):
        window_id = self._run(['xdotool', 'getactivewindow'])
        if not window_id:
            return None, None

        title = self._run(['xdotool', 'getwindowname', window_id]) or ''
        app = title
        pid = self._run(['xdotool', 'getwindowpid', window_id])
        if pid:
            try:
                with open(f'/proc/{pid}/comm') as f:
                    app = f.read().strip() or title
            except OSError:
                pass
        return (app or None), title


def detect_sources(display_name=None):
    """(idle_source, window_source). Either may be None on a machine without X —
    the caller decides whether that is fatal."""
    idle = window = None
    try:
        idle = X11IdleSource(display_name)
        logger.info('Idle detection: X11 screensaver counter')
    except Exception as e:
        logger.error(f'No idle source available: {e}')
    try:
        window = XdotoolWindowSource()
        logger.info('Window detection: xdotool')
    except Exception as e:
        logger.warning(f'No window source available: {e} — time will be tracked '
                       f'but not attributed to applications')
    return idle, window
