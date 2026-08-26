"""What the widget shows.

Every one of these lived inside a QWidget in the local app, which is why none
of them were tested and why verifying the twenty-five minute encouragement
meant sitting still for twenty-five minutes.
"""
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from settings import RemoteSettings
from spool import Spool
from widget.state import CHEER_SECONDS, OFFLINE_AFTER, WidgetState, format_hm, greeting

UTC = timezone.utc
T0 = datetime(2026, 8, 26, 9, 0, tzinfo=UTC)


class Clock:
    def __init__(self, now=T0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, **kwargs):
        self.now += timedelta(**kwargs)
        return self.now


@pytest.fixture
def rig(tmp_path):
    spool = Spool(str(tmp_path))
    clock = Clock()
    settings = RemoteSettings({'day_goal_seconds': 8 * 3600,
                               'week_goal_seconds': 40 * 3600})
    state = WidgetState(spool, settings, clock=clock, name='Douglas')
    yield state, spool, clock, settings
    spool.close()


# ── Goals ────────────────────────────────────────────────────────────────────

def test_progress_is_a_fraction_of_the_goal(rig):
    state, _, _, _ = rig
    state.set_totals(today_seconds=4 * 3600)
    assert state.progress == 0.5


def test_progress_is_not_capped_at_the_goal(rig):
    """A bar pinned at 100% says nothing; finishing a day's work is worth
    noticing."""
    state, _, _, _ = rig
    state.set_totals(today_seconds=10 * 3600)
    assert state.progress > 1.0 and state.overdrive


def test_switching_to_the_week_switches_the_goal(rig):
    state, _, _, _ = rig
    state.set_totals(today_seconds=4 * 3600, week_seconds=20 * 3600)
    assert state.goal_seconds == 8 * 3600
    state.toggle_mode()
    assert state.goal_seconds == 40 * 3600 and state.progress == 0.5


def test_remaining_never_goes_negative(rig):
    state, _, _, _ = rig
    state.set_totals(today_seconds=12 * 3600)
    assert state.remaining_seconds == 0


def test_goals_come_from_the_server(rig):
    state, _, _, settings = rig
    settings._values['day_goal_seconds'] = 6 * 3600
    state.set_totals(today_seconds=3 * 3600)
    assert state.progress == 0.5


# ── Sessions ─────────────────────────────────────────────────────────────────

def test_starting_a_session_greets_you(rig):
    state, _, clock, _ = rig
    toast = state.start_session('Alpha')
    assert 'Good' in toast['title'] or 'Still up' in toast['title']
    assert 'Alpha' in toast['sub'] and state.tracking


def test_elapsed_is_read_from_the_spool_not_the_server(rig):
    """So the face keeps ticking on a train."""
    state, _, clock, _ = rig
    state.start_session('Alpha')
    clock.advance(minutes=42)
    assert state.elapsed == 42 * 60


def test_stopping_reports_the_day(rig):
    state, _, _, _ = rig
    state.set_totals(today_seconds=3 * 3600)
    state.start_session('Alpha')
    toast = state.stop_session()
    assert '3h 00m' in toast['sub'] and not state.tracking


def test_stopping_when_nothing_runs_is_harmless(rig):
    state, _, _, _ = rig
    assert state.stop_session() is None


def test_the_session_survives_a_restart(rig, tmp_path):
    state, spool, clock, settings = rig
    state.start_session('Alpha')
    fresh = WidgetState(spool, settings, clock=clock)
    fresh.refresh_from_spool()
    assert fresh.tracking and fresh.session['project'] == 'Alpha'


# ── The status line ──────────────────────────────────────────────────────────

def test_it_names_the_project_while_working(rig):
    state, _, _, _ = rig
    state.start_session('Digital Transformation')
    assert state.status_line == 'Digital Transformation'


def test_idle_is_distinct_from_stopped(rig):
    state, _, _, _ = rig
    state.start_session('Alpha')
    state.tick(is_idle=True)
    assert state.status_line == 'Idle'


def test_paused_outranks_everything(rig):
    state, _, _, settings = rig
    state.start_session('Alpha')
    settings.paused = True
    assert state.status_line == 'Paused' and not state.tracking


# ── Offline ──────────────────────────────────────────────────────────────────

def test_it_starts_offline_until_something_answers(rig):
    state, _, _, _ = rig
    assert state.offline


def test_contact_clears_it(rig):
    state, _, _, _ = rig
    state.note_contact()
    assert not state.offline


def test_it_goes_offline_again_after_silence(rig):
    state, _, clock, _ = rig
    state.note_contact()
    clock.advance(seconds=OFFLINE_AFTER.total_seconds() + 10)
    assert state.offline


def test_one_slow_poll_does_not_flicker_it_offline(rig):
    state, _, clock, _ = rig
    state.note_contact()
    clock.advance(seconds=20)
    assert not state.offline


def test_offline_is_not_the_same_as_not_working(rig):
    """The widget must not imply someone stopped when it is the network that
    stopped."""
    state, _, clock, _ = rig
    state.start_session('Alpha')
    clock.advance(minutes=10)
    assert state.offline and state.tracking


# ── Encouragement ────────────────────────────────────────────────────────────

def test_a_cheer_arrives_after_the_streak(rig):
    state, _, clock, _ = rig
    state.start_session('Alpha')
    state.tick()
    clock.advance(seconds=CHEER_SECONDS)
    cheer = state.tick()
    assert cheer and '25' in cheer['title'] or cheer


def test_nothing_arrives_before_it_is_earned(rig):
    state, _, clock, _ = rig
    state.start_session('Alpha')
    state.tick()
    clock.advance(seconds=CHEER_SECONDS - 60)
    assert state.tick() is None


def test_going_idle_resets_the_streak(rig):
    """A streak measures unbroken focus."""
    state, _, clock, _ = rig
    state.start_session('Alpha')
    state.tick()
    clock.advance(seconds=CHEER_SECONDS - 60)
    state.tick(is_idle=True)
    clock.advance(seconds=120)
    assert state.tick() is None


def test_nothing_arrives_while_stopped(rig):
    state, _, clock, _ = rig
    state.tick()
    clock.advance(seconds=CHEER_SECONDS * 2)
    assert state.tick() is None


def test_the_streak_measures_real_time_not_ticks(rig):
    """A slow or skipped poll must not inflate or reset it."""
    state, _, clock, _ = rig
    state.start_session('Alpha')
    state.tick()
    for _ in range(3):
        clock.advance(seconds=CHEER_SECONDS / 3 + 1)
        cheer = state.tick()
    assert cheer is not None


def test_a_cheer_never_repeats_itself_immediately(rig):
    """A repeat reads as a bug rather than as encouragement."""
    state, _, clock, _ = rig
    state.start_session('Alpha')
    state.tick()
    seen = []
    for _ in range(6):
        clock.advance(seconds=CHEER_SECONDS)
        seen.append(state.tick()['title'])
    assert all(a != b for a, b in zip(seen, seen[1:]))


def test_a_cheer_past_the_goal_does_not_promise_more_to_do(rig):
    state, _, clock, _ = rig
    state.set_totals(today_seconds=10 * 3600)
    state.start_session('Alpha')
    state.tick()
    clock.advance(seconds=CHEER_SECONDS)
    assert 'past your goal' in state.tick()['sub']


# ── The daily card ───────────────────────────────────────────────────────────

def test_only_an_active_prompt_interrupts(rig):
    """The server decides presence so every client agrees."""
    state, _, _, _ = rig
    state.set_prompts([{'date': '2026-08-25', 'active': False}])
    assert state.pending_prompt is None and state.badge_count == 1


def test_an_active_prompt_is_shown(rig):
    state, _, _, _ = rig
    state.set_prompts([{'date': '2026-08-25', 'active': True}])
    assert state.pending_prompt['date'] == '2026-08-25'


def test_the_badge_counts_everything_waiting(rig):
    state, _, _, _ = rig
    state.set_prompts([{'date': '2026-08-24', 'active': False},
                       {'date': '2026-08-25', 'active': True}])
    assert state.badge_count == 2


# ── Small things ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize('seconds, text', [
    (0, '0m'), (59, '0m'), (60, '1m'), (3600, '1h 00m'), (3661, '1h 01m'),
    (-5, '0m'),
])
def test_duration_formatting(seconds, text):
    assert format_hm(seconds) == text


def test_the_greeting_follows_the_clock():
    make = lambda h: datetime(2026, 8, 26, h).astimezone()
    assert 'morning' in greeting(make(8))
    assert 'afternoon' in greeting(make(14))
    assert 'evening' in greeting(make(19))
    assert 'Still up' in greeting(make(23))


def test_the_greeting_uses_your_name_if_it_has_one():
    assert greeting(datetime(2026, 8, 26, 9).astimezone(), 'Douglas').endswith('Douglas')
    assert ',' not in greeting(datetime(2026, 8, 26, 9).astimezone())
