"""Reading window titles.

This is the cleverest logic in the system and, in the local app, the only part
with no tests at all — because its aggregation reached into the database from
inside itself. Ported as a pure function, every rule can finally be stated.
"""
from datetime import datetime, timedelta, timezone

import pytest

from app.services.summarize import (MIN_ROW_SECONDS, classify, headline,
                                    site_of, summarise)

UTC = timezone.utc
T0 = datetime(2026, 8, 26, 9, 0, tzinfo=UTC)
WINDOW = (T0, T0 + timedelta(hours=12))


def row(app, title, at=0, minutes=10):
    start = T0 + timedelta(minutes=at)
    return {'app_name': app, 'window_title': title, 'started_at': start,
            'ended_at': start + timedelta(minutes=minutes)}


def labels(activities):
    return [(a['category'], a['label']) for a in activities]


# ── Editors ──────────────────────────────────────────────────────────────────

def test_an_editor_reports_the_project_not_the_file(self=None):
    """The file changes every two minutes; the project does not. Keeping the
    file would make one afternoon look like forty activities."""
    assert classify('cursor', 'models.py - ttcloud - Cursor') == ('Coding', 'ttcloud')
    assert classify('cursor', 'api.py - ttcloud - Cursor') == ('Coding', 'ttcloud')


def test_a_two_part_editor_title_falls_back_to_the_first_part():
    assert classify('code', 'notes.md - Code') == ('Coding', 'notes.md')


# ── Terminals ────────────────────────────────────────────────────────────────

def test_a_terminal_title_is_treated_as_a_written_log_entry():
    """Claude Code renames the terminal to the task it is on, which is the
    closest thing to a human-written entry the system ever receives."""
    assert classify('kitty', 'Building the ingest endpoint') == \
        ('Claude Code', 'Building the ingest endpoint')


def test_a_bang_prefix_is_a_shell_command():
    assert classify('kitty', '! pytest -q tests') == ('Shell', 'pytest -q tests')


def test_an_idle_terminal_says_so_rather_than_naming_the_program():
    assert classify('kitty', 'Claude Code') == ('Claude Code', 'session (no task set)')


def test_a_spinner_does_not_split_one_task_into_many():
    assert classify('kitty', '◐ Building') == classify('kitty', '⣾ Building')


# ── Browsers ─────────────────────────────────────────────────────────────────

def test_the_browsers_own_name_is_stripped():
    assert classify('chrome', 'Some Page - Google Chrome') == ('Web', 'Some Page')


def test_a_search_is_its_own_category():
    assert classify('chrome', 'postgres upsert - Google Search - Google Chrome') == \
        ('Search', 'postgres upsert')


def test_every_mail_message_collapses_to_one_activity():
    """Otherwise reading twenty messages reads as twenty activities."""
    a = classify('chrome', 'Re: invoice - a@x.com - Gmail - Google Chrome')
    b = classify('chrome', 'Fwd: notes - b@x.com - Gmail - Google Chrome')
    assert a == b == ('Email', 'Gmail')


def test_an_empty_tab_describes_nothing():
    assert classify('chrome', 'New Tab - Google Chrome') is None


def test_an_unread_count_does_not_create_a_new_activity():
    assert classify('chrome', 'Inbox (3,739) - Gmail - Google Chrome') == \
        classify('chrome', 'Inbox (3,740) - Gmail - Google Chrome')


# ── Text editors ─────────────────────────────────────────────────────────────

def test_a_text_editor_keeps_the_folder_because_it_is_the_project():
    assert classify('xed', 'notes.md (~/Desktop/rj)') == \
        ('Writing', 'notes.md in ~/Desktop/rj')


def test_a_bracketed_folder_is_not_mistaken_for_a_counter():
    """Only a purely numeric bracket is a counter — this is real information."""
    category, label = classify('xed', 'notes.md (~/Desktop)')
    assert '~/Desktop' in label


# ── Sites ────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize('title, expected', [
    ('Prophecy Archive · Repent and Prepare the Way', 'Repent and Prepare the Way'),
    ('Some Page — The Guardian', 'The Guardian'),
    ('A page | Wikipedia', 'Wikipedia'),
    ('No separator here', None),
    ('Page - ab', None),                       # too short to be a site name
])
def test_site_detection(title, expected):
    assert site_of(title) == expected


def test_many_pages_of_one_site_become_one_line():
    """Reading eleven pages of a site is one activity; listing every page
    buries the things that only got one line each."""
    rows = [row('chrome', f'Article {i} · The Ministry - Google Chrome', at=i * 10)
            for i in range(4)]
    activities, _ = summarise(rows, *WINDOW)
    assert labels(activities) == [('Web', 'The Ministry (4 pages)')]
    assert activities[0]['seconds'] == 4 * 600


def test_a_couple_of_pages_are_left_alone():
    """Two pages sharing a site is a coincidence, not a browsing session."""
    rows = [row('chrome', f'Article {i} · The Ministry - Google Chrome', at=i * 10)
            for i in range(2)]
    activities, _ = summarise(rows, *WINDOW)
    assert len(activities) == 2


def test_the_sites_landing_page_folds_into_its_own_group():
    """It has no page name to strip, so it would otherwise print as a second,
    nearly identical line right beside the group."""
    rows = [row('chrome', f'Article {i} · The Ministry - Google Chrome', at=i * 10)
            for i in range(3)]
    rows.append(row('chrome', 'The Ministry - Google Chrome', at=40))
    activities, _ = summarise(rows, *WINDOW)
    assert len(activities) == 1 and '4 pages' in activities[0]['label']


# ── Searches ─────────────────────────────────────────────────────────────────

def test_many_searches_become_one_activity_carrying_evidence():
    rows = [row('chrome', f'query {i} - Google Search - Google Chrome', at=i * 5, minutes=4)
            for i in range(5)]
    activities, _ = summarise(rows, *WINDOW)
    assert len(activities) == 1
    label = activities[0]['label']
    assert label.startswith('5 queries — ') and '+2 more' in label
    assert activities[0]['short'] == '5 web searches'


def test_a_single_search_is_left_as_itself():
    activities, _ = summarise([row('chrome', 'one thing - Google Search - Google Chrome')],
                              *WINDOW)
    assert labels(activities) == [('Search', 'one thing')]


# ── Aggregation ──────────────────────────────────────────────────────────────

def test_the_same_activity_is_summed_not_repeated():
    """Forty focus switches become the four lines a person would have written."""
    rows = [row('cursor', 'a.py - ttcloud - Cursor', at=0),
            row('cursor', 'b.py - ttcloud - Cursor', at=20),
            row('cursor', 'c.py - ttcloud - Cursor', at=40)]
    activities, tracked = summarise(rows, *WINDOW)
    assert labels(activities) == [('Coding', 'ttcloud')]
    assert activities[0]['seconds'] == tracked == 3 * 600


def test_activities_are_ranked_by_time():
    rows = [row('cursor', 'a.py - small - Cursor', minutes=5),
            row('kitty', 'A long task', at=30, minutes=90)]
    activities, _ = summarise(rows, *WINDOW)
    assert [a['category'] for a in activities] == ['Claude Code', 'Coding']


def test_a_focus_flicker_is_ignored():
    """Alt-tabbing through a window on the way to another describes nothing."""
    flicker = row('chrome', 'Passing By - Google Chrome')
    flicker['ended_at'] = flicker['started_at'] + timedelta(seconds=MIN_ROW_SECONDS - 1)
    activities, tracked = summarise([flicker], *WINDOW)
    assert activities == [] and tracked == 0


def test_only_the_part_inside_the_window_counts():
    """A session running past the boundary is clipped, not counted whole."""
    late = row('cursor', 'a.py - ttcloud - Cursor', at=11 * 60 + 30, minutes=60)
    activities, tracked = summarise([late], *WINDOW)
    assert tracked == 30 * 60


def test_nothing_in_nothing_out():
    assert summarise([], *WINDOW) == ([], 0)


# ── Headline ─────────────────────────────────────────────────────────────────

def test_the_headline_reads_like_a_sentence():
    rows = [row('cursor', 'a.py - ttcloud - Cursor', minutes=120),
            row('kitty', 'Writing the report', at=130, minutes=60),
            row('chrome', 'Docs · MDN - Google Chrome', at=200, minutes=30)]
    activities, _ = summarise(rows, *WINDOW)
    assert headline(activities) == 'ttcloud (2h 00m), Writing the report (1h 00m) ' \
                                   'and Docs · MDN (30m)'


def test_a_single_activity_needs_no_conjunction():
    activities, _ = summarise([row('cursor', 'a.py - ttcloud - Cursor')], *WINDOW)
    assert headline(activities) == 'ttcloud (10m)'


def test_an_empty_day_says_so():
    assert headline([]) == 'Nothing recorded.'


def test_the_headline_uses_the_short_form_of_a_folded_group():
    """The evidence that makes a search line useful is far too long to read at
    a glance."""
    rows = [row('chrome', f'a long query number {i} - Google Search - Google Chrome',
                at=i * 5, minutes=4) for i in range(4)]
    activities, _ = summarise(rows, *WINDOW)
    assert headline(activities) == '4 web searches (16m)'
