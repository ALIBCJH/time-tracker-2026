"""The evening card."""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

pytest.importorskip('PyQt6', reason='the card needs Qt; the logic it drives does not')

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PyQt6.QtWidgets import QApplication

from widget.prompt import PromptCard, format_hm


@pytest.fixture(scope='module')
def qt():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def answers():
    return []


def make(answers, **overrides):
    prompt = {'date': '2026-08-25', 'kind': 'new', 'tracked_seconds': 7200,
              'headline': 'ttcloud (2h 00m)',
              'activities': [{'label': 'ttcloud', 'seconds': 7200}]}
    prompt.update(overrides)
    card = PromptCard(prompt, lambda *a: answers.append(a))
    # Shown, as it is in practice: a child of a never-shown parent reports
    # isVisible() False however its own flag is set.
    card.show()
    return card


def test_the_draft_is_a_placeholder_not_a_prefilled_answer(qt, answers):
    """A pre-filled note gets accepted unread, and an accepted-unread note
    looks like a record of the day while really being a record of the
    summarizer."""
    card = make(answers)
    assert card.note.toPlainText() == ''
    assert card.note.placeholderText() == 'ttcloud (2h 00m)'


def test_an_empty_confirm_is_refused(qt, answers):
    """It would record the day as answered while saying nothing about it."""
    card = make(answers)
    card._confirm()
    assert answers == []
    assert card.error.isVisible() and 'Write a line' in card.error.text()


def test_whitespace_alone_is_still_empty(qt, answers):
    card = make(answers)
    card.note.setPlainText('   \n  ')
    card._confirm()
    assert answers == []


def test_a_written_note_is_saved(qt, answers):
    card = make(answers)
    card.note.setPlainText('Shipped the ingest endpoint.')
    card._confirm()
    assert answers == [('2026-08-25', 'Shipped the ingest endpoint.', 'confirmed')]


def test_a_day_can_be_skipped_without_writing_anything(qt, answers):
    card = make(answers)
    card._answer('skipped')
    assert answers[0][2] == 'skipped'


def test_a_topup_sends_unchanged_with_an_empty_note(qt, answers):
    """Which is exactly why it cannot go through the confirm path — that would
    overwrite the note already written with the empty box beside it."""
    card = make(answers, kind='topup')
    card._answer('unchanged')
    assert answers == [('2026-08-25', '', 'unchanged')]


def test_a_day_with_no_activities_still_renders(qt, answers):
    card = make(answers, activities=[])
    assert card is not None


def test_only_a_handful_of_activities_are_listed(qt, answers):
    """A card that scrolls is a card that gets dismissed."""
    from widget.prompt import MAX_ROWS
    many = [{'label': f'thing {i}', 'seconds': 600} for i in range(20)]
    card = make(answers, activities=many)
    labels = [w.text() for w in card.findChildren(type(card.error))
              if w.text().startswith('thing ')]
    assert len(labels) == MAX_ROWS


@pytest.mark.parametrize('seconds, text', [(0, '0m'), (600, '10m'), (7200, '2h 00m')])
def test_durations_read_naturally(seconds, text):
    assert format_hm(seconds) == text
