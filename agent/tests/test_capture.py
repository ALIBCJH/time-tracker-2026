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
    monitor = ActivityMonitor(spool, idle, window, idle_threshold=900,
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

def test_crossing_the_threshold_pauses_rather_than_closing(rig):
    """The change this behaviour exists for. Closing here chopped a day on one
    project into a fresh session after every coffee; the work is the same work,
    so it stays the same session."""
    monitor, spool, idle, window = rig
    cu = spool.start_session('Alpha', 'the task')
    monitor.tick(T0, mono=0)

    idle.seconds = 960                                        # 16 minutes
    monitor.tick(T0 + timedelta(minutes=16), mono=5)

    assert monitor.is_idle
    session = spool.open_session()
    assert session is not None
    assert session['client_uuid'] == cu
    assert session['project'] == 'Alpha' and session['task'] == 'the task'


def test_the_pause_is_dated_to_when_input_stopped(rig):
    """Not to the poll that noticed. The sixteen minutes of staring at a wall
    are not work, and crediting them would be the difference between an honest
    number and a flattering one."""
    monitor, spool, idle, window = rig
    cu = spool.start_session('Alpha', started_at=T0.isoformat())
    monitor.tick(T0, mono=0)

    noticed_at = T0 + timedelta(minutes=16)
    idle.seconds = 960
    monitor.tick(noticed_at, mono=5)

    row = spool.conn.execute(
        'SELECT ended_at, idle_since FROM sessions WHERE client_uuid=?',
        (cu,)).fetchone()
    assert row['ended_at'] is None                    # paused, not finished
    assert row['idle_since'] == T0.isoformat()        # 16 minutes before it was seen


def test_the_break_is_on_record_the_moment_it_starts(rig):
    """Written on the way in, not on the way out. An agent killed mid-break
    would otherwise leave no gap at all, and the server would count the whole
    absence as work."""
    monitor, spool, idle, window = rig
    spool.start_session('Alpha')
    monitor.tick(T0, mono=0)
    idle.seconds = 960
    monitor.tick(T0 + timedelta(minutes=16), mono=5)

    rows = idle_rows(spool)
    assert len(rows) == 1
    assert rows[0]['started_at'] == T0.isoformat()
    assert rows[0]['open'] == 1


def test_an_open_break_is_not_uploaded_until_it_closes(rig):
    """The server treats an idle period as final — a resend of a longer one is
    ignored by the conflict clause that makes resends safe. So a break in
    progress is held back rather than sent and corrected."""
    monitor, spool, idle, window = rig
    spool.start_session('Alpha')
    monitor.tick(T0, mono=0)
    idle.seconds = 960
    monitor.tick(T0 + timedelta(minutes=16), mono=5)
    assert spool.pending_batch()['idle_periods'] == []

    idle.seconds = 0
    monitor.tick(T0 + timedelta(minutes=30), mono=10)
    assert len(spool.pending_batch()['idle_periods']) == 1


def test_the_session_stays_alive_while_paused(rig):
    """The agent is alive; it is the person who is away. Without a heartbeat
    the server would cap the session as abandoned after fifteen minutes and
    mail an alert about a lunch break."""
    monitor, spool, idle, window = rig
    cu = spool.start_session('Alpha')
    monitor.tick(T0, mono=0)
    idle.seconds = 960
    monitor.tick(T0 + timedelta(minutes=16), mono=5)

    later = T0 + timedelta(minutes=40)
    idle.seconds = 960 + 24 * 60
    monitor.tick(later, mono=10)

    row = spool.conn.execute(
        'SELECT last_heartbeat_at FROM sessions WHERE client_uuid=?', (cu,)).fetchone()
    assert row['last_heartbeat_at'] == later.isoformat()


def test_the_span_in_flight_ends_where_the_idle_began(rig):
    """Twenty minutes at the keyboard then sixteen idle: the span is banked at
    minute twenty, not at minute thirty-six when the idle was noticed."""
    monitor, spool, idle, window = rig
    spool.start_session('Alpha')
    monitor.tick(T0, mono=0)

    idle.seconds = 960
    monitor.tick(T0 + timedelta(minutes=36), mono=5)

    row = usage_rows(spool)[0]
    assert row['started_at'] == T0.isoformat()
    assert row['ended_at'] == (T0 + timedelta(minutes=20)).isoformat()


def test_going_idle_without_a_session_is_harmless(rig):
    monitor, spool, idle, window = rig
    idle.seconds = 960
    monitor.tick(T0, mono=0)
    assert monitor.is_idle and spool.open_session() is None


# ── Coming back ──────────────────────────────────────────────────────────────

def test_returning_reopens_the_session_that_was_running(rig):
    monitor, spool, idle, window = rig
    spool.start_session('Alpha', 'the task')
    monitor.tick(T0, mono=0)
    idle.seconds = 960
    monitor.tick(T0 + timedelta(minutes=16), mono=5)

    idle.seconds = 0
    monitor.tick(T0 + timedelta(minutes=30), mono=10)

    reopened = spool.open_session()
    assert reopened['project'] == 'Alpha' and reopened['task'] == 'the task'


def test_coming_back_resumes_the_same_session(rig):
    """Not a new one. This is what "pause, do not scrap" means in the data:
    one session for the morning's work, with the coffee cut out of it."""
    monitor, spool, idle, window = rig
    cu = spool.start_session('Alpha', started_at=T0.isoformat())
    monitor.tick(T0, mono=0)
    idle.seconds = 960
    monitor.tick(T0 + timedelta(minutes=16), mono=5)

    back = T0 + timedelta(minutes=45)
    idle.seconds = 0
    monitor.tick(back, mono=10)

    session = spool.open_session()
    assert session['client_uuid'] == cu
    assert session['started_at'] == T0.isoformat()   # still the morning's session
    assert session['idle_since'] is None             # and counting again


def test_the_break_is_cut_out_of_the_resumed_session(rig):
    """The session spans the gap, so the gap has to be on record — it is what
    the server subtracts to keep the total honest."""
    monitor, spool, idle, window = rig
    spool.start_session('Alpha', started_at=T0.isoformat())
    monitor.tick(T0, mono=0)
    idle.seconds = 960
    monitor.tick(T0 + timedelta(minutes=16), mono=5)

    back = T0 + timedelta(minutes=45)
    idle.seconds = 0
    monitor.tick(back, mono=10)

    row = idle_rows(spool)[0]
    assert row['started_at'] == T0.isoformat()
    assert row['ended_at'] == back.isoformat()
    assert row['open'] == 0


def test_a_pause_waits_however_long_it_takes(rig):
    """No maximum, and no guessing that somebody has gone home. Four hours
    later it is still the same session, still paused, still theirs to resume.
    Stopping on purpose is what the pause control is for."""
    monitor, spool, idle, window = rig
    cu = spool.start_session('Alpha', started_at=T0.isoformat())
    monitor.tick(T0, mono=0)
    idle.seconds = 960
    monitor.tick(T0 + timedelta(minutes=16), mono=5)

    idle.seconds = 4 * 3600
    result = monitor.tick(T0 + timedelta(hours=4), mono=10)
    assert result['events'] == []
    session = spool.open_session()
    assert session['client_uuid'] == cu and session['ended_at'] is None

    back = T0 + timedelta(hours=5)
    idle.seconds = 0
    monitor.tick(back, mono=15)
    resumed = spool.open_session()
    assert resumed['client_uuid'] == cu          # the same session, all along
    assert idle_rows(spool)[0]['ended_at'] == back.isoformat()


# ── Suspend ──────────────────────────────────────────────────────────────────

def test_a_frozen_gap_is_not_credited_as_work(rig):
    """A closed lid. The wall clock jumps but the idle counter reads near zero,
    because the X server was not running either — so the gap has to be caught
    by the loop stalling, not by the idle counter.

    Like any other idle, it pauses: same session, minus the frozen hours. What
    must not happen is those hours landing in the total.
    """
    monitor, spool, idle, window = rig
    original = spool.start_session('Alpha', started_at=T0.isoformat())
    monitor.tick(T0, mono=0)

    woke = T0 + timedelta(hours=4)
    result = monitor.tick(woke, mono=4 * 3600)
    assert 'suspend' in result['events']

    session = spool.open_session()
    assert session['client_uuid'] == original    # paused, not closed
    assert session['ended_at'] is None
    assert session['idle_since'] is None         # and already back, counting

    # The four hours are idle, not work.
    row = idle_rows(spool)[-1]
    assert row['started_at'] == T0.isoformat()
    assert row['ended_at'] == woke.isoformat()


def test_a_short_freeze_behaves_the_same_way(rig):
    """Twenty minutes with the lid shut is no different in kind from four
    hours — there is one rule now, not a threshold between two."""
    monitor, spool, idle, window = rig
    original = spool.start_session('Alpha', started_at=T0.isoformat())
    monitor.tick(T0, mono=0)

    woke = T0 + timedelta(minutes=20)
    assert 'suspend' in monitor.tick(woke, mono=20 * 60)['events']

    session = spool.open_session()
    assert session['client_uuid'] == original and session['ended_at'] is None
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
    monitor = ActivityMonitor(spool, idle, window_source=None, idle_threshold=900)
    spool.start_session('Alpha')
    monitor.tick(T0, mono=0)
    idle.seconds = 960
    monitor.tick(T0 + timedelta(minutes=16), mono=5)
    assert monitor.is_idle and usage_rows(spool) == []
    assert spool.open_session()['idle_since'] == T0.isoformat()


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


# ── Surviving a restart mid-pause ────────────────────────────────────────────

def test_a_restart_during_a_pause_does_not_freeze_the_session(tmp_path):
    """The agent is killed while somebody is away, and restarts after they are
    back. The monitor comes up believing nobody is idle, so a pause mark left
    on the session would never be cleared by anything — and the server holds an
    open session at idle_since, which would freeze that person's total for the
    rest of the day while they worked.
    """
    idle = FakeIdle()
    spool = Spool(str(tmp_path))
    monitor = ActivityMonitor(spool, idle, None, idle_threshold=900, poll_interval=5)

    cu = spool.start_session('Alpha', started_at=T0.isoformat())
    monitor.tick(T0, mono=0)
    idle.seconds = 960
    monitor.tick(T0 + timedelta(minutes=16), mono=5)
    assert spool.open_session()['idle_since'] == T0.isoformat()
    spool.close()

    # Restart, exactly as agent_main does on boot.
    restarted = Spool(str(tmp_path))
    gaps, sessions = restarted.settle_interrupted_pause()
    assert (gaps, sessions) == (1, 1)

    fresh = ActivityMonitor(restarted, idle, None, idle_threshold=900, poll_interval=5)
    idle.seconds = 0
    fresh.tick(T0 + timedelta(minutes=30), mono=100)

    row = restarted.open_session()
    assert row['client_uuid'] == cu
    assert row['idle_since'] is None          # counting again
    assert row['dirty'] == 1                  # and the server will be told
    restarted.close()


def test_the_gap_left_by_the_crash_still_uploads(tmp_path):
    """Closed at wherever it reached, rather than sitting unsent for ever with
    the server counting the absence as work."""
    idle = FakeIdle()
    spool = Spool(str(tmp_path))
    monitor = ActivityMonitor(spool, idle, None, idle_threshold=900, poll_interval=5)
    spool.start_session('Alpha', started_at=T0.isoformat())
    monitor.tick(T0, mono=0)
    idle.seconds = 960
    monitor.tick(T0 + timedelta(minutes=16), mono=5)
    monitor.tick(T0 + timedelta(minutes=21), mono=10)      # pause held once
    assert spool.pending_batch()['idle_periods'] == []     # still open, held back
    spool.close()

    restarted = Spool(str(tmp_path))
    restarted.settle_interrupted_pause()
    pending = restarted.pending_batch()['idle_periods']
    assert len(pending) == 1
    assert pending[0]['started_at'] == T0.isoformat()
    assert pending[0]['ended_at'] == (T0 + timedelta(minutes=21)).isoformat()
    restarted.close()


def test_someone_still_away_after_a_restart_simply_pauses_again(tmp_path):
    """Clearing the mark is safe: the next poll reads the idle counter, sees
    the absence and pauses again from where input actually stopped. State is
    rebuilt from the machine rather than trusted from before the crash."""
    idle = FakeIdle()
    spool = Spool(str(tmp_path))
    monitor = ActivityMonitor(spool, idle, None, idle_threshold=900, poll_interval=5)
    spool.start_session('Alpha', started_at=T0.isoformat())
    monitor.tick(T0, mono=0)
    idle.seconds = 960
    monitor.tick(T0 + timedelta(minutes=16), mono=5)
    spool.close()

    restarted = Spool(str(tmp_path))
    restarted.settle_interrupted_pause()
    fresh = ActivityMonitor(restarted, idle, None, idle_threshold=900, poll_interval=5)

    # Away since T0, which by minute 61 the counter reports as 61 minutes.
    idle.seconds = 61 * 60
    fresh.tick(T0 + timedelta(minutes=61), mono=100)

    assert fresh.is_idle
    assert restarted.open_session()['idle_since'] == T0.isoformat()
    restarted.close()
