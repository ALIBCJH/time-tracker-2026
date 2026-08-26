"""The capture loop.

The behaviours worth pinning are the ones that decide how much time a person is
credited with, and every one of them is about *when* something happened rather
than when it was noticed. The local app's equivalent had no tests at all,
because it read the clock and shelled out to xdotool internally — verifying the
ten-minute idle rule meant sitting still for ten minutes.
"""
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from capture import ActivityMonitor
from spool import Spool

UTC = timezone.utc
T0 = datetime(2026, 8, 26, 9, 0, tzinfo=UTC)


class FakeIdle:
    """A settable idle counter, standing in for the X server's."""
    name = 'fake'

    def __init__(self, seconds=0.0):
        self.seconds = seconds
        self.fail = False

    def idle_seconds(self):
        if self.fail:
            raise RuntimeError('display went away')
        return self.seconds


class FakeWindow:
    name = 'fake'

    def __init__(self, app='code', title='main.py'):
        self.app, self.title = app, title

    def active_window(self):
        return self.app, self.title


@pytest.fixture
def rig(tmp_path):
    spool = Spool(str(tmp_path))
    idle = FakeIdle()
    window = FakeWindow()
    monitor = ActivityMonitor(spool, idle, window, idle_threshold=600,
                              poll_interval=5, min_span=5)
    yield monitor, spool, idle, window
    spool.close()


def usage_rows(spool):
    return [dict(r) for r in spool.conn.execute(
        'SELECT * FROM app_usage ORDER BY started_at')]


def idle_rows(spool):
    return [dict(r) for r in spool.conn.execute(
        'SELECT * FROM idle_periods ORDER BY started_at')]


# ── Foreground spans ─────────────────────────────────────────────────────────

def test_a_window_change_banks_the_previous_span(rig):
    monitor, spool, idle, window = rig
    monitor.tick(T0, mono=0)                                  # code/main.py starts
    window.app, window.title = 'chrome', 'docs'
    monitor.tick(T0 + timedelta(minutes=20), mono=5)

    row = usage_rows(spool)[0]
    assert row['app_name'] == 'code'
    assert row['started_at'] == T0.isoformat()
    assert row['ended_at'] == (T0 + timedelta(minutes=20)).isoformat()


def test_an_unchanged_window_does_not_split_the_span(rig):
    monitor, spool, idle, window = rig
    for i in range(6):
        monitor.tick(T0 + timedelta(seconds=i * 5), mono=i * 5)
    assert usage_rows(spool) == []          # still in the same span, nothing banked


def test_a_flicker_is_not_recorded(rig):
    """A window that had focus for two seconds is alt-tab, not work."""
    monitor, spool, idle, window = rig
    monitor.tick(T0, mono=0)
    window.app = 'chrome'
    monitor.tick(T0 + timedelta(seconds=2), mono=2)
    assert usage_rows(spool) == []


def test_spans_are_attributed_to_the_open_session(rig):
    monitor, spool, idle, window = rig
    cu = spool.start_session('Alpha')
    monitor.tick(T0, mono=0)
    window.app = 'chrome'
    monitor.tick(T0 + timedelta(minutes=10), mono=5)
    assert usage_rows(spool)[0]['session_client_uuid'] == cu


def test_stopping_banks_the_span_in_flight(rig):
    """A clean shutdown must not lose the last span the way a crash would."""
    monitor, spool, idle, window = rig
    monitor.tick(T0, mono=0)
    monitor._app_started = T0            # pin the start, stop() uses the real clock
    monitor.stop()
    assert len(usage_rows(spool)) == 1


# ── Going idle ───────────────────────────────────────────────────────────────

def test_crossing_the_threshold_closes_the_session(rig):
    monitor, spool, idle, window = rig
    spool.start_session('Alpha')
    monitor.tick(T0, mono=0)

    idle.seconds = 660                                        # 11 minutes
    monitor.tick(T0 + timedelta(minutes=11), mono=5)

    assert monitor.is_idle
    assert spool.open_session() is None


def test_the_session_is_cut_back_to_when_input_stopped(rig):
    """Not to the poll that noticed. The eleven minutes of staring at a wall
    are not work, and crediting them would be the difference between an honest
    number and a flattering one."""
    monitor, spool, idle, window = rig
    cu = spool.start_session('Alpha', started_at=T0.isoformat())
    monitor.tick(T0, mono=0)

    noticed_at = T0 + timedelta(minutes=11)
    idle.seconds = 660
    monitor.tick(noticed_at, mono=5)

    row = spool.conn.execute('SELECT ended_at FROM sessions WHERE client_uuid=?',
                             (cu,)).fetchone()
    assert row['ended_at'] == T0.isoformat()          # 11 minutes before it was seen


def test_the_span_in_flight_ends_where_the_idle_began(rig):
    """Twenty minutes at the keyboard then eleven idle: the span is banked at
    minute twenty, not at minute thirty-one when the idle was noticed."""
    monitor, spool, idle, window = rig
    spool.start_session('Alpha')
    monitor.tick(T0, mono=0)

    idle.seconds = 660
    monitor.tick(T0 + timedelta(minutes=31), mono=5)

    row = usage_rows(spool)[0]
    assert row['started_at'] == T0.isoformat()
    assert row['ended_at'] == (T0 + timedelta(minutes=20)).isoformat()


def test_going_idle_without_a_session_is_harmless(rig):
    monitor, spool, idle, window = rig
    idle.seconds = 660
    monitor.tick(T0, mono=0)
    assert monitor.is_idle and spool.open_session() is None


# ── Coming back ──────────────────────────────────────────────────────────────

def test_returning_reopens_the_session_that_was_running(rig):
    monitor, spool, idle, window = rig
    spool.start_session('Alpha', 'the task')
    monitor.tick(T0, mono=0)
    idle.seconds = 660
    monitor.tick(T0 + timedelta(minutes=11), mono=5)

    idle.seconds = 0
    monitor.tick(T0 + timedelta(minutes=30), mono=10)

    reopened = spool.open_session()
    assert reopened['project'] == 'Alpha' and reopened['task'] == 'the task'


def test_the_reopened_session_starts_now_not_earlier(rig):
    """The gap is not retroactively counted as work."""
    monitor, spool, idle, window = rig
    spool.start_session('Alpha')
    monitor.tick(T0, mono=0)
    idle.seconds = 660
    monitor.tick(T0 + timedelta(minutes=11), mono=5)

    back = T0 + timedelta(minutes=45)
    idle.seconds = 0
    monitor.tick(back, mono=10)
    assert spool.open_session()['started_at'] == back.isoformat()


def test_the_idle_gap_is_recorded(rig):
    """The local app had an idle_periods table its monitor never wrote to."""
    monitor, spool, idle, window = rig
    spool.start_session('Alpha')
    monitor.tick(T0, mono=0)
    idle.seconds = 660
    monitor.tick(T0 + timedelta(minutes=11), mono=5)
    idle.seconds = 0
    monitor.tick(T0 + timedelta(minutes=40), mono=10)

    row = idle_rows(spool)[0]
    assert row['started_at'] == T0.isoformat()
    assert row['ended_at'] == (T0 + timedelta(minutes=40)).isoformat()


def test_nothing_reopens_if_nothing_was_running(rig):
    monitor, spool, idle, window = rig
    idle.seconds = 660
    monitor.tick(T0, mono=0)
    idle.seconds = 0
    monitor.tick(T0 + timedelta(minutes=20), mono=5)
    assert spool.open_session() is None


# ── Suspend ──────────────────────────────────────────────────────────────────

def test_a_frozen_gap_is_not_credited_as_work(rig):
    """A closed lid. The wall clock jumps two hours but the idle counter reads
    near zero, because the X server was not running either — so the gap has to
    be caught by the loop stalling, not by the idle counter.

    Waking reopens a session, which is right: you are back. What must not
    happen is the two suspended hours landing in the total.
    """
    monitor, spool, idle, window = rig
    original = spool.start_session('Alpha', started_at=T0.isoformat())
    monitor.tick(T0, mono=0)

    woke = T0 + timedelta(hours=2)
    result = monitor.tick(woke, mono=7200)
    assert 'suspend' in result['events']

    closed = spool.conn.execute('SELECT ended_at FROM sessions WHERE client_uuid=?',
                                (original,)).fetchone()
    assert closed['ended_at'] == T0.isoformat()      # cut back to the freeze

    reopened = spool.open_session()
    assert reopened['client_uuid'] != original
    assert reopened['started_at'] == woke.isoformat()

    # The two hours are idle, not work.
    assert idle_rows(spool)[0]['started_at'] == T0.isoformat()
    assert idle_rows(spool)[0]['ended_at'] == woke.isoformat()


def test_an_ordinary_poll_gap_is_not_a_suspend(rig):
    monitor, spool, idle, window = rig
    spool.start_session('Alpha')
    monitor.tick(T0, mono=0)
    result = monitor.tick(T0 + timedelta(seconds=6), mono=6)
    assert 'suspend' not in result['events']
    assert spool.open_session() is not None


def test_the_first_tick_is_never_a_suspend(rig):
    """There is no previous poll to measure a gap against."""
    monitor, spool, idle, window = rig
    spool.start_session('Alpha')
    assert 'suspend' not in monitor.tick(T0, mono=99999)['events']


# ── Robustness ───────────────────────────────────────────────────────────────

def test_a_failed_idle_query_does_not_close_the_session(rig):
    """A transient X hiccup must not read as 'the user left'."""
    monitor, spool, idle, window = rig
    spool.start_session('Alpha')
    monitor.tick(T0, mono=0)

    idle.fail = True
    result = monitor.tick(T0 + timedelta(seconds=5), mono=5)

    assert 'idle-query-failed' in result['events']
    assert not monitor.is_idle and spool.open_session() is not None


def test_time_is_still_tracked_without_a_window_source(rig, tmp_path):
    """A machine where xdotool is missing still records sessions and idle."""
    _, spool, idle, _ = rig
    monitor = ActivityMonitor(spool, idle, window_source=None, idle_threshold=600)
    spool.start_session('Alpha')
    monitor.tick(T0, mono=0)
    idle.seconds = 660
    monitor.tick(T0 + timedelta(minutes=11), mono=5)
    assert spool.open_session() is None and usage_rows(spool) == []


def test_heartbeats_are_throttled(rig):
    """A heartbeat dirties the session row; doing it every poll would have the
    agent re-uploading the same session all day."""
    monitor, spool, idle, window = rig
    cu = spool.start_session('Alpha')
    spool.mark_accepted(spool.pending_batch())

    monitor.tick(T0, mono=0)
    assert spool.stats()['pending_sessions'] == 1        # first one beats
    spool.mark_accepted(spool.pending_batch())

    # Poll-sized steps, as the real loop runs. A jump here would read as a
    # suspend, which is a different behaviour entirely.
    for i in range(1, 12):                               # up to +55s
        monitor.tick(T0 + timedelta(seconds=i * 5), mono=i * 5)
    assert spool.stats()['pending_sessions'] == 0        # still too soon

    monitor.tick(T0 + timedelta(seconds=60), mono=60)
    assert spool.stats()['pending_sessions'] == 1


# ── Titles that change while the window does not ─────────────────────────────

def test_a_spinner_does_not_fragment_a_span(rig):
    """A terminal animating its title is one activity, not one per frame. The
    local app fragmented an hour into hundreds of one-second rows this way."""
    monitor, spool, idle, window = rig
    for i, glyph in enumerate('◐◑◒◓◐◑'):
        window.title = f'{glyph} Building the thing'
        monitor.tick(T0 + timedelta(seconds=i * 5), mono=i * 5)
    assert usage_rows(spool) == []


def test_an_unread_count_does_not_fragment_a_span(rig):
    monitor, spool, idle, window = rig
    window.app = 'chrome'
    for i, n in enumerate([3, 4, 5, 6]):
        window.title = f'({n}) Inbox — Gmail'
        monitor.tick(T0 + timedelta(seconds=i * 5), mono=i * 5)
    assert usage_rows(spool) == []


def test_a_real_change_still_banks_the_span(rig):
    """Normalising must not blind the loop to actual switches."""
    monitor, spool, idle, window = rig
    window.title = '◐ Building the thing'
    monitor.tick(T0, mono=0)
    window.title = '◑ Reading the docs'
    monitor.tick(T0 + timedelta(minutes=10), mono=5)
    assert len(usage_rows(spool)) == 1


def test_the_stored_title_is_the_clean_one(rig):
    monitor, spool, idle, window = rig
    window.title = '◐ Building the thing'
    monitor.tick(T0, mono=0)
    window.app = 'chrome'
    monitor.tick(T0 + timedelta(minutes=10), mono=5)
    assert usage_rows(spool)[0]['window_title'] == 'Building the thing'


# ── The pause ────────────────────────────────────────────────────────────────

class Paused:
    """A settings object that says tracking is paused."""
    paused = True

    def get(self, key, default=None):
        return {'screenshots_enabled': False, 'tracking_enabled': False}.get(key, default)


def test_a_paused_agent_records_nothing(rig):
    """The server would refuse the uploads anyway. Not recording is the
    difference between "your data is discarded" and "your data is not
    collected"."""
    monitor, spool, idle, window = rig
    monitor.settings = Paused()
    monitor.tick(T0, mono=0)
    assert usage_rows(spool) == []
    assert spool.stats()['pending_app_usage'] == 0


def test_pausing_closes_an_open_session(rig):
    monitor, spool, idle, window = rig
    spool.start_session('Alpha')
    monitor.settings = Paused()
    result = monitor.tick(T0, mono=0)
    assert 'paused' in result['events']
    assert spool.open_session() is None


def test_a_paused_tick_reports_itself(rig):
    monitor, spool, idle, window = rig
    monitor.settings = Paused()
    assert monitor.tick(T0, mono=0)['paused'] is True


def test_pausing_repeatedly_is_harmless(rig):
    monitor, spool, idle, window = rig
    spool.start_session('Alpha')
    monitor.settings = Paused()
    for i in range(3):
        monitor.tick(T0 + timedelta(seconds=i * 5), mono=i * 5)
    assert spool.open_session() is None
