"""Making the laptop and the server agree about what is running.

Someone can start or stop a session from the dashboard — from another room, or
after leaving the laptop running at the office. So the two views have to be
reconciled, and the interesting cases are all about which one is right.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from settings import reconcile_session
from spool import Spool


@pytest.fixture
def spool(tmp_path):
    s = Spool(str(tmp_path))
    yield s
    s.close()


def remote(client_uuid, project='FromBrowser'):
    return {'client_uuid': client_uuid, 'project': project, 'task': '',
            'started_at': '2026-08-26T09:00:00+00:00'}


def sync(spool):
    """Pretend the server acknowledged everything currently queued."""
    spool.mark_accepted(spool.pending_batch())


# ── Nothing to do ────────────────────────────────────────────────────────────

def test_both_idle_is_agreement(spool):
    assert reconcile_session(spool, None) == 'agreed'


def test_the_same_session_on_both_sides_is_agreement(spool):
    cu = spool.start_session('Alpha')
    sync(spool)
    assert reconcile_session(spool, remote(cu)) == 'agreed'


# ── Started from the browser ─────────────────────────────────────────────────

def test_a_session_started_in_the_browser_is_adopted(spool):
    """So what happens on screen from now on is attributed to it."""
    assert reconcile_session(spool, remote('abc-123')) == 'adopted'
    local = spool.open_session()
    assert local['client_uuid'] == 'abc-123' and local['project'] == 'FromBrowser'


def test_an_adopted_session_is_not_uploaded_back(spool):
    """Telling the server something it just told us is pure noise."""
    reconcile_session(spool, remote('abc-123'))
    assert spool.stats()['pending_sessions'] == 0


def test_an_adopted_session_collects_activity(spool):
    reconcile_session(spool, remote('abc-123'))
    spool.record_app_usage('code', 'main.py', '2026-08-26T10:00:00+00:00',
                           '2026-08-26T10:30:00+00:00',
                           spool.open_session()['client_uuid'])
    row = spool.pending_batch()['app_usage'][0]
    assert row['session_client_uuid'] == 'abc-123'


def test_adopting_twice_is_harmless(spool):
    reconcile_session(spool, remote('abc-123'))
    assert reconcile_session(spool, remote('abc-123')) == 'agreed'


# ── Stopped from the browser ─────────────────────────────────────────────────

def test_a_session_stopped_in_the_browser_is_closed_locally(spool):
    """The laptop left running at the office."""
    spool.start_session('Alpha')
    sync(spool)
    assert reconcile_session(spool, None) == 'stopped-remotely'
    assert spool.open_session() is None


def test_a_different_session_on_the_server_replaces_the_local_one(spool):
    spool.start_session('Alpha')
    sync(spool)
    assert reconcile_session(spool, remote('other-1')) == 'replaced'
    assert spool.open_session()['client_uuid'] == 'other-1'


# ── The one case the server does not win ─────────────────────────────────────

def test_a_session_the_server_has_never_seen_is_left_alone(spool):
    """Started on a train. The server's silence about it means "not uploaded
    yet", not "stopped" — and treating it as stopped would delete work the
    moment it was started offline."""
    spool.start_session('Alpha')                    # never synced
    assert reconcile_session(spool, None) == 'pending-upload'
    assert spool.open_session() is not None


def test_a_session_changed_since_its_upload_is_also_left_alone(spool):
    """A heartbeat makes it dirty again; that is not the same as unknown, but
    the safe reading is still "wait until the server has the latest"."""
    cu = spool.start_session('Alpha')
    sync(spool)
    spool.heartbeat(cu)
    assert reconcile_session(spool, None) == 'pending-upload'
    assert spool.open_session() is not None


def test_once_uploaded_the_same_session_can_then_be_stopped_remotely(spool):
    cu = spool.start_session('Alpha')
    assert reconcile_session(spool, None) == 'pending-upload'
    sync(spool)
    assert reconcile_session(spool, None) == 'stopped-remotely'
