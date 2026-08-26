"""The local spool.

These pin the behaviours that make the agent survivable offline: a record is
never considered delivered until the server said so, a session that changes
after upload is re-sent, and a permanently bad record eventually stops costing
bandwidth.
"""
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import spool as spool_mod
from spool import MAX_ATTEMPTS, Spool

UTC = timezone.utc


def iso(dt):
    return dt.isoformat()


@pytest.fixture
def s(tmp_path):
    sp = Spool(str(tmp_path))
    yield sp
    sp.close()


def usage(s, session=None, minutes=30):
    start = datetime.now(UTC) - timedelta(minutes=minutes)
    return s.record_app_usage('code', 'main.py', iso(start), iso(datetime.now(UTC)), session)


# ── Recording works without a server ─────────────────────────────────────────

def test_a_session_starts_with_no_network(s):
    """The widget must be able to start tracking on a train."""
    cu = s.start_session('Alpha')
    assert s.open_session()['client_uuid'] == cu
    assert s.stats()['pending_sessions'] == 1


def test_timestamps_always_carry_an_offset(s):
    """The server refuses naive timestamps, and is right to."""
    s.start_session('Alpha')
    started = s.open_session()['started_at']
    assert datetime.fromisoformat(started).tzinfo is not None


def test_stopping_a_session_closes_it(s):
    cu = s.start_session('Alpha')
    s.stop_session(cu)
    assert s.open_session() is None


def test_stopping_an_already_closed_session_is_harmless(s):
    cu = s.start_session('Alpha')
    s.stop_session(cu, iso(datetime.now(UTC) - timedelta(hours=1)))
    first = s.pending_batch()['sessions'][0]['ended_at']
    s.stop_session(cu)
    assert s.pending_batch()['sessions'][0]['ended_at'] == first


# ── Nothing is delivered until the server says so ────────────────────────────

def test_records_stay_pending_until_accepted(s):
    s.start_session('Alpha')
    usage(s)
    before = s.stats()
    assert before['pending_sessions'] == 1 and before['pending_app_usage'] == 1

    s.mark_accepted(s.pending_batch())
    after = s.stats()
    assert after['pending_sessions'] == 0 and after['pending_app_usage'] == 0


def test_an_unsent_batch_is_offered_again(s):
    """A lost response must not look like a delivery."""
    usage(s)
    first = s.pending_batch()
    second = s.pending_batch()          # never marked accepted
    assert first['app_usage'] == second['app_usage']


def test_a_closed_session_is_re_uploaded(s):
    """It was already uploaded while open; closing it changes it."""
    cu = s.start_session('Alpha')
    s.mark_accepted(s.pending_batch())
    assert s.stats()['pending_sessions'] == 0

    s.stop_session(cu)
    assert s.stats()['pending_sessions'] == 1
    assert s.pending_batch()['sessions'][0]['ended_at'] is not None


def test_a_session_changed_mid_upload_stays_pending(s):
    """The change happened after the batch was built, so acknowledging that
    batch must not mark the newer state delivered."""
    cu = s.start_session('Alpha')
    batch = s.pending_batch()
    s.stop_session(cu)                  # changes after the batch was taken
    s.mark_accepted(batch)
    assert s.stats()['pending_sessions'] == 1


def test_a_heartbeat_requeues_the_open_session(s):
    cu = s.start_session('Alpha')
    s.mark_accepted(s.pending_batch())
    s.heartbeat(cu)
    assert s.stats()['pending_sessions'] == 1


# ── Rejections ───────────────────────────────────────────────────────────────

def test_a_rejected_record_is_retried(s):
    usage(s)
    batch = s.pending_batch()
    s.mark_accepted(batch, [{'kind': 'app_usage', 'index': 0, 'error': 'nope'}])
    assert s.stats()['pending_app_usage'] == 1


def test_a_permanently_rejected_record_eventually_gives_up(s):
    """Otherwise it retries for ever, hiding the problem and wasting bandwidth."""
    usage(s)
    for _ in range(MAX_ATTEMPTS):
        batch = s.pending_batch()
        if not batch['app_usage']:
            break
        s.mark_accepted(batch, [{'kind': 'app_usage', 'index': 0, 'error': 'nope'}])
    stats = s.stats()
    assert stats['pending_app_usage'] == 0 and stats['dead'] == 1


def test_one_rejection_does_not_hold_up_its_neighbours(s):
    usage(s, minutes=60)
    usage(s, minutes=30)
    batch = s.pending_batch()
    s.mark_accepted(batch, [{'kind': 'app_usage', 'index': 0, 'error': 'nope'}])
    assert s.stats()['pending_app_usage'] == 1


def test_a_whole_kind_rejected_counts_against_every_record_in_it(s):
    """The server can refuse a kind outright — an oversized batch, say."""
    usage(s)
    usage(s, minutes=10)
    s.mark_accepted(s.pending_batch(),
                    [{'kind': 'app_usage', 'index': None, 'error': 'at most 1000'}])
    assert s.stats()['pending_app_usage'] == 2      # both retried, both counted


# ── Pruning ──────────────────────────────────────────────────────────────────

def test_pruning_keeps_unsent_work(s):
    usage(s)
    assert s.prune(keep_days=0) == 0
    assert s.stats()['pending_app_usage'] == 1


def test_pruning_removes_delivered_work(s):
    usage(s)
    s.mark_accepted(s.pending_batch())
    assert s.prune(keep_days=-1) == 1


def test_pruning_never_removes_an_open_session(s):
    """An open session is live state, however old."""
    s.start_session('Alpha')
    s.mark_accepted(s.pending_batch())
    s.prune(keep_days=-1)
    assert s.open_session() is not None


def test_the_spool_survives_being_reopened(tmp_path):
    """A crash, a reboot, a closed lid — the queue is on disk, not in memory."""
    first = Spool(str(tmp_path))
    cu = first.start_session('Alpha')
    usage(first)
    first.close()

    second = Spool(str(tmp_path))
    assert second.open_session()['client_uuid'] == cu
    assert second.stats()['pending_app_usage'] == 1
    second.close()
