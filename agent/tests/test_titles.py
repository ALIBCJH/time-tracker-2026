"""Title normalisation."""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from titles import normalise


@pytest.mark.parametrize('raw, expected', [
    ('◐ Building the thing', 'Building the thing'),
    ('⣾ pytest running', 'pytest running'),
    ('(3) Inbox — Gmail', 'Inbox — Gmail'),
    ('Pull requests (12)', 'Pull requests'),
    ('main.py — timetracker', 'main.py — timetracker'),
    ('  spaced   out  ', 'spaced out'),
    ('', ''),
    (None, ''),
])
def test_normalisation(raw, expected):
    assert normalise(raw) == expected


def test_a_moving_timecode_is_stripped():
    """A player ticking through a track is one activity, not one per second."""
    assert normalise('01:23 / 45:00 Some Track') == normalise('02:41 / 45:00 Some Track')


def test_two_different_titles_stay_different():
    """Normalising must not collapse genuinely different windows together."""
    assert normalise('◐ Building') != normalise('◐ Reading')
