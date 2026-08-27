"""When a capture is taken, and what happens to it afterwards.

The gating tests are the important ones. Every condition exists so that the
store does not fill with pictures taken while nobody agreed to be recorded —
of a locked screen, or of an evening after someone stopped working.
"""
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from screenshot import ScreenshotService
from spool import Spool

UTC = timezone.utc
T0 = datetime(2026, 8, 26, 9, 0, tzinfo=UTC)
ON = {'screenshots_enabled': True, 'screenshot_interval_seconds': 600}


class FakeCapture:
    """Stands in for scrot + Pillow, so the gating can be tested with no display."""

    def __init__(self, out_dir):
        self.out_dir = out_dir
        self.calls = 0

    def __call__(self, out_dir, backend, captured_at=None, **kwargs):
        self.calls += 1
        os.makedirs(out_dir, exist_ok=True)
        full = os.path.join(out_dir, f'{self.calls}-full.webp')
        thumb = os.path.join(out_dir, f'{self.calls}-thumb.webp')
        for path in (full, thumb):
            with open(path, 'wb') as f:
                f.write(b'RIFF----WEBP')
        return full, thumb, captured_at


@pytest.fixture
def rig(tmp_path, monkeypatch):
    spool = Spool(str(tmp_path))
    shots = str(tmp_path / 'shots')
    fake = FakeCapture(shots)
    monkeypatch.setattr('screenshot.capture', fake)
    service = ScreenshotService(spool, shots, dict(ON), backend=['true'])
    yield service, spool, fake
    spool.close()


# ── Gating ───────────────────────────────────────────────────────────────────

def test_nothing_is_captured_without_a_session(rig):
    """Time nobody asked to have tracked is time nobody agreed to be recorded."""
    service, spool, fake = rig
    assert service.tick(is_idle=False, now=T0) is None
    assert fake.calls == 0


def test_nothing_is_captured_while_idle(rig):
    """Otherwise the store fills with pictures of a locked screen."""
    service, spool, fake = rig
    spool.start_session('Alpha')
    assert service.tick(is_idle=True, now=T0) is None
    assert fake.calls == 0


def test_nothing_is_captured_when_the_user_has_them_off(rig):
    service, spool, fake = rig
    service.settings['screenshots_enabled'] = False
    spool.start_session('Alpha')
    assert service.tick(is_idle=False, now=T0) is None


def test_a_capture_is_taken_while_actively_working(rig):
    service, spool, fake = rig
    spool.start_session('Alpha')
    assert service.tick(is_idle=False, now=T0) is not None
    assert spool.stats()['pending_screenshots'] == 1


def test_captures_respect_the_interval(rig):
    """The interval is now a range rather than a point, so the contract is a
    floor and a ceiling: never sooner than the shortest possible gap, and
    always by the longest. Asserting an exact ten minutes would be asserting
    the predictability the jitter exists to remove."""
    from screenshot import JITTER

    service, spool, fake = rig
    spool.start_session('Alpha')
    service.tick(is_idle=False, now=T0)

    too_soon = T0 + timedelta(seconds=600 * (1 - JITTER) - 1)
    service.tick(is_idle=False, now=too_soon)
    assert fake.calls == 1

    certainly_due = T0 + timedelta(seconds=600 * (1 + JITTER) + 1)
    service.tick(is_idle=False, now=certainly_due)
    assert fake.calls == 2


def test_the_interval_advances_even_when_a_capture_fails(rig, monkeypatch):
    """A machine with no capture tool must not retry every single poll."""
    service, spool, fake = rig
    from screenshot import CaptureUnavailable

    def boom(*args, **kwargs):
        raise CaptureUnavailable('no tool')
    monkeypatch.setattr('screenshot.capture', boom)

    spool.start_session('Alpha')
    assert service.tick(is_idle=False, now=T0) is None
    assert service.due(T0 + timedelta(minutes=1), is_idle=False) is False


def test_a_capture_is_attached_to_the_open_session(rig):
    service, spool, fake = rig
    cu = spool.start_session('Alpha')
    service.tick(is_idle=False, now=T0)
    assert spool.pending_screenshots()[0]['session_client_uuid'] == cu


# ── After upload ─────────────────────────────────────────────────────────────

def test_the_local_copies_are_removed_once_uploaded(rig):
    """A laptop is the one place these should not accumulate."""
    service, spool, fake = rig
    spool.start_session('Alpha')
    client_uuid = service.tick(is_idle=False, now=T0)
    row = spool.pending_screenshots()[0]

    spool.screenshot_sent(client_uuid)
    assert not os.path.exists(row['full_path'])
    assert not os.path.exists(row['thumb_path'])
    assert spool.stats()['pending_screenshots'] == 0


def test_an_un_uploaded_capture_keeps_its_files(rig):
    service, spool, fake = rig
    spool.start_session('Alpha')
    service.tick(is_idle=False, now=T0)
    row = spool.pending_screenshots()[0]
    assert os.path.exists(row['full_path'])


def test_a_capture_the_server_keeps_refusing_is_retired(rig):
    """Otherwise it retries for ever, hiding the problem."""
    service, spool, fake = rig
    spool.start_session('Alpha')
    client_uuid = service.tick(is_idle=False, now=T0)
    for _ in range(5):
        spool.screenshot_failed(client_uuid)
    stats = spool.stats()
    assert stats['pending_screenshots'] == 0 and stats['dead'] == 1


def test_captures_survive_a_restart(rig, tmp_path):
    service, spool, fake = rig
    spool.start_session('Alpha')
    service.tick(is_idle=False, now=T0)
    spool.close()

    reopened = Spool(str(tmp_path))
    assert reopened.stats()['pending_screenshots'] == 1
    reopened.close()


def test_backend_detection_creates_its_own_probe_directory(tmp_path, monkeypatch):
    """On a fresh install the directory does not exist yet. Without this, every
    probe fails to write and the agent concludes the machine has no screenshot
    tool at all — which is what happened the first time it was run enrolled."""
    import screenshot as S

    calls = []

    def fake_run(argv):
        calls.append(argv)
        path = argv[-1]
        with open(path, 'wb') as f:            # only succeeds if the dir exists
            f.write(b'x')
        return True
    monkeypatch.setattr(S, '_run', fake_run)

    missing = tmp_path / 'does' / 'not' / 'exist'
    assert S.detect_backend(str(missing)) is not None
    assert missing.is_dir()


# ── Unpredictable timing ─────────────────────────────────────────────────────

def test_captures_do_not_land_on_a_fixed_schedule(tmp_path):
    """A fixed interval is a fixed schedule, and a fixed schedule can be worked
    around: captures exactly ten minutes apart tell somebody precisely when not
    to be looking at something else."""
    import random

    from screenshot import JITTER

    spool = Spool(str(tmp_path))
    gaps = set()
    for seed in range(20):
        service = ScreenshotService(spool, str(tmp_path / 'shots'), dict(ON),
                                    backend=['true'], rng=random.Random(seed))
        gaps.add(round(service._next_gap()))
    spool.close()

    assert len(gaps) > 10                       # varied, not a constant
    assert min(gaps) >= round(600 * (1 - JITTER)) - 1
    assert max(gaps) <= round(600 * (1 + JITTER)) + 1


def test_the_gap_is_held_until_the_capture_happens(rig):
    """Drawn once per capture, not per poll. Re-drawing every poll would make
    the expected wait drift toward the shortest draw, so captures would creep
    steadily earlier."""
    service, spool, fake = rig
    assert service._next_gap() == service._next_gap() == service._next_gap()


def test_a_new_gap_is_drawn_after_each_capture(rig):
    """Otherwise the first draw would set the cadence for the whole day."""
    service, spool, fake = rig
    spool.start_session('Alpha')
    service.tick(is_idle=False, now=T0)
    assert service._gap is None                 # cleared, so the next is fresh
    service._next_gap()
    assert service._gap is not None
