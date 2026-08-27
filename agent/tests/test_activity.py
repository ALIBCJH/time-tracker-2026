"""Measuring how much of a session had a person in it.

The tracker credits time for a session being open and not idle, which measures
presence rather than work: one keystroke every fourteen minutes holds the idle
counter below its threshold all day. These tests are mostly about the shape of
the measurement — that a minute of reading between keystrokes still counts,
that a window only half covered says so, and that the tapping trick produces a
number nobody has to interpret.
"""
import tempfile
from datetime import datetime, timedelta, timezone

import pytest

from capture import ActivityMonitor
from spool import Spool

UTC = timezone.utc
T0 = datetime(2026, 8, 27, 9, 0, tzinfo=UTC)      # a clean window boundary
POLL = 5


class FakeIdle:
    def __init__(self):
        self.seconds = 0.0

    def idle_seconds(self):
        return self.seconds


@pytest.fixture
def rig(tmp_path):
    spool = Spool(str(tmp_path))
    idle = FakeIdle()
    monitor = ActivityMonitor(spool, idle, None, idle_threshold=900,
                              poll_interval=POLL, activity_window=600)
    yield monitor, spool, idle
    spool.close()


def windows(spool):
    return [dict(r) for r in spool.conn.execute(
        'SELECT * FROM activity_windows ORDER BY started_at')]


def drive(monitor, idle, minutes, last_input, start=T0):
    """Run the real loop for `minutes`, with `last_input(t)` deciding when input
    most recently happened."""
    t, mono = start, 0.0
    for _ in range(int(minutes * 60 / POLL)):
        idle.seconds = (t - last_input(t)).total_seconds()
        monitor.tick(t, mono=mono)
        t += timedelta(seconds=POLL)
        mono += POLL
    return t


# ── The measurement ──────────────────────────────────────────────────────────

def test_constant_typing_is_a_full_window(rig):
    monitor, spool, idle = rig
    spool.start_session('Alpha', started_at=T0.isoformat())
    drive(monitor, idle, 10, lambda t: t)
    monitor._close_window(T0 + timedelta(minutes=10))

    row = windows(spool)[0]
    assert row['active_minutes'] == 10 and row['tracked_minutes'] == 10


def test_reading_between_keystrokes_still_counts_the_minute(rig):
    """A minute is active if ANY poll in it saw input. Counting per poll would
    score somebody reading a paragraph between keystrokes as absent, which is
    the difference between measuring work and measuring typing speed."""
    monitor, spool, idle = rig
    spool.start_session('Alpha', started_at=T0.isoformat())
    # One keystroke at the top of each minute, nothing else.
    drive(monitor, idle, 10,
          lambda t: t.replace(second=0, microsecond=0))
    monitor._close_window(T0 + timedelta(minutes=10))

    row = windows(spool)[0]
    assert row['active_minutes'] == 10


def test_the_fourteen_minute_trick_scores_almost_nothing(rig):
    """Three hours of an open session and a keystroke every fourteen minutes.
    The idle threshold is never crossed, so the time is all credited — and the
    activity figure is what makes that visible."""
    monitor, spool, idle = rig
    spool.start_session('Alpha', started_at=T0.isoformat())

    def tap_every_14(t):
        elapsed = (t - T0).total_seconds()
        return T0 + timedelta(seconds=int(elapsed // 840) * 840)

    end = drive(monitor, idle, 180, tap_every_14)
    monitor._close_window(end)

    rows = windows(spool)
    active = sum(r['active_minutes'] for r in rows)
    tracked = sum(r['tracked_minutes'] for r in rows)
    assert tracked >= 175                     # the time was all credited...
    assert round(100 * active / tracked) < 15  # ...and it was nearly all empty
    assert not monitor.is_idle                # never paused, as designed


def test_a_real_session_scores_far_higher_than_the_trick(rig, tmp_path):
    """The two have to be far enough apart that nobody needs a threshold to
    tell them apart."""
    monitor, spool, idle = rig
    spool.start_session('Alpha', started_at=T0.isoformat())
    # Input in two minutes out of every three.
    end = drive(monitor, idle, 60,
                lambda t: t if (int((t - T0).total_seconds()) // 60) % 3 != 2
                else t - timedelta(minutes=1))
    monitor._close_window(end)

    rows = windows(spool)
    real = 100 * sum(r['active_minutes'] for r in rows) / sum(r['tracked_minutes'] for r in rows)
    assert real > 50


# ── What is and is not counted ───────────────────────────────────────────────

def test_nothing_is_recorded_without_a_session(rig):
    """A window with no session behind it is not evidence of anything."""
    monitor, spool, idle = rig
    end = drive(monitor, idle, 20, lambda t: t)
    monitor._close_window(end)
    assert windows(spool) == []


def test_a_window_the_session_only_half_covered_says_so(rig):
    """Starting at 09:05 must not read as a half-empty window — tracked
    minutes, not wall-clock minutes, are the denominator."""
    monitor, spool, idle = rig
    start = T0 + timedelta(minutes=5)
    spool.start_session('Alpha', started_at=start.isoformat())
    drive(monitor, idle, 5, lambda t: t, start=start)
    monitor._close_window(T0 + timedelta(minutes=10))

    row = windows(spool)[0]
    assert row['tracked_minutes'] == 5
    assert row['active_minutes'] == 5


def test_paused_minutes_are_not_tracked_minutes(rig):
    """Idle time is already excluded from the hours; counting it here too would
    drag the percentage down for somebody who simply stepped away."""
    monitor, spool, idle = rig
    spool.start_session('Alpha', started_at=T0.isoformat())
    drive(monitor, idle, 5, lambda t: t)
    # Away long enough to pause, for the rest of the window.
    idle.seconds = 16 * 60
    monitor.tick(T0 + timedelta(minutes=16), mono=1000)
    monitor._close_window(T0 + timedelta(minutes=20))

    assert monitor.is_idle
    assert all(r['tracked_minutes'] <= 6 for r in windows(spool))


def test_windows_are_aligned_to_the_clock(rig):
    """So that two people's windows line up, and a capture can be matched to
    one by its timestamp alone."""
    monitor, spool, idle = rig
    start = T0 + timedelta(minutes=3)
    spool.start_session('Alpha', started_at=start.isoformat())
    end = drive(monitor, idle, 20, lambda t: t, start=start)
    monitor._close_window(end)

    for row in windows(spool):
        began = datetime.fromisoformat(row['started_at'])
        assert began.minute % 10 == 0 and began.second == 0


def test_a_finished_window_is_queued_for_upload(rig):
    monitor, spool, idle = rig
    spool.start_session('Alpha', started_at=T0.isoformat())
    end = drive(monitor, idle, 12, lambda t: t)
    monitor._close_window(end)

    pending = spool.pending_batch()['activity_windows']
    assert pending and pending[0]['tracked_minutes'] > 0
    assert pending[0]['session_client_uuid'] is not None


def test_stopping_banks_the_window_in_flight(rig):
    """A clean shutdown must not lose the last window the way a crash would."""
    monitor, spool, idle = rig
    spool.start_session('Alpha', started_at=T0.isoformat())
    drive(monitor, idle, 4, lambda t: t)
    assert windows(spool) == []
    monitor.stop()
    assert len(windows(spool)) == 1
