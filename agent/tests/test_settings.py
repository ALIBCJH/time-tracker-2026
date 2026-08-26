"""Server-held settings, as the agent sees them."""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from client import AuthError, TransientError
from settings import DEFAULTS, RemoteSettings


class FakeClient:
    def __init__(self, body=None, error=None):
        self.body, self.error = body, error

    def me(self):
        if self.error:
            raise self.error
        return self.body


def test_it_starts_from_defaults():
    assert RemoteSettings().get('idle_threshold_seconds') == 600


def test_a_refresh_applies_the_servers_values():
    s = RemoteSettings()
    s.refresh(FakeClient({'settings': {'idle_threshold_seconds': 60},
                          'paused': False}))
    assert s.get('idle_threshold_seconds') == 60


def test_a_pause_is_picked_up():
    s = RemoteSettings()
    s.refresh(FakeClient({'settings': {}, 'paused': True,
                          'paused_until': '2026-08-26T21:00:00+00:00'}))
    assert s.paused and s.paused_until.startswith('2026-08-26')


def test_a_resume_is_picked_up():
    s = RemoteSettings()
    s.refresh(FakeClient({'settings': {}, 'paused': True}))
    s.refresh(FakeClient({'settings': {}, 'paused': False}))
    assert not s.paused


def test_an_unreachable_server_keeps_the_last_known_values():
    """Falling back to defaults on a network blip would silently un-pause
    someone — the one failure this must not have."""
    s = RemoteSettings()
    s.refresh(FakeClient({'settings': {'idle_threshold_seconds': 60},
                          'paused': True}))
    assert s.refresh(FakeClient(error=TransientError('offline'))) is False
    assert s.paused is True and s.get('idle_threshold_seconds') == 60


def test_a_rejected_token_also_keeps_the_pause():
    s = RemoteSettings()
    s.refresh(FakeClient({'settings': {}, 'paused': True}))
    s.refresh(FakeClient(error=AuthError('revoked')))
    assert s.paused is True


def test_unknown_keys_do_not_erase_known_ones():
    s = RemoteSettings()
    s.refresh(FakeClient({'settings': {'something_new': 1}, 'paused': False}))
    assert s.get('idle_threshold_seconds') == DEFAULTS['idle_threshold_seconds']
