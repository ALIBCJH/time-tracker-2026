"""Turning window titles into an account of what a day was spent on.

No model and no network. Every title the desktop hands us is already
structured: an editor appends the project after the file, Claude Code writes
the task into the terminal title, a browser tags its own name on the end, a
site appends its name to every page. The "what was he doing" signal has been
sitting in a column all along; parsing it costs nothing, works offline, and
never sends a screenshot anywhere.

This module is pure — it takes rows and returns activities. The local app's
version reached into the database from inside the aggregation, which is why
none of this logic was ever tested despite being the cleverest part of it.
"""
import re

# Rows shorter than this are focus flickers — alt-tabbing through a window on
# the way to another. They pollute the ranking without describing anything.
MIN_ROW_SECONDS = 8

# Decoration that makes one activity look like many.
SPINNER_GLYPHS = '✳◐◑◒◓✢✻✽·*⠂⠄⠆⠈⠐⠠⡀⢀⣀⠁⠉⣾⣽⣻⢿⡿⣟⣯⣷'
_LEADING_SPINNER = re.compile(r'^[' + re.escape(SPINNER_GLYPHS) + r'\s]+')
# "Inbox (3,739)" and "Inbox (3,740)" are the same activity an hour apart.
# Only a purely numeric bracket is a counter — xed's "notes.md (~/Desktop)" is
# real information, so the digits-only anchor matters.
_TRAILING_COUNT = re.compile(r'\s*\(\d[\d,]*\)\s*$')
_LEADING_COUNT = re.compile(r'^\s*\(\d[\d,]*\)\s*')

# Longest first, so " - " never wins over " — " on a title containing both.
SITE_SEPARATORS = (' · ', ' — ', ' | ', ' – ', ' - ')

# Below this many distinct pages, a shared site is a coincidence rather than a
# browsing session worth collapsing into one line.
MIN_PAGES_PER_SITE = 3

# Titles that identify the program but say nothing about the work.
EMPTY_TITLES = {'terminal', 'claude', 'claude code', 'cursor', 'new tab',
                'untitled', 'bash', 'zsh', 'google chrome', 'chrome', '', '-'}

MAX_LABEL = 52
# The headline is tighter because it has to survive being read at a glance.
MAX_HEADLINE_LABEL = 38

TERMINALS = {'gnome-terminal-', 'kitty', 'alacritty', 'xterm', 'konsole', 'terminator'}
EDITORS = {'cursor', 'code', 'codium', 'code-oss'}
BROWSERS = {'chrome', 'chromium', 'firefox', 'brave', 'google-chrome'}
TEXT_EDITORS = {'xed', 'gedit', 'kate', 'mousepad'}
BROWSER_SUFFIXES = (' - Google Chrome', ' - Chromium', ' - Mozilla Firefox', ' - Brave')


def clean(title):
    if not title:
        return ''
    title = _LEADING_SPINNER.sub('', title)
    title = _LEADING_COUNT.sub('', title)
    title = _TRAILING_COUNT.sub('', title)
    return title.strip()


def shorten(text, limit):
    return text if len(text) <= limit else text[:limit - 1].rstrip() + '…'


def site_of(page):
    """The site a page title belongs to, if it names one.

    Sites append their own name to every page ("Prophecy Archive · Repent and
    Prepare the Way"), which is what lets eleven pages be recognised as one
    visit to one place rather than eleven unrelated activities.
    """
    for separator in SITE_SEPARATORS:
        if separator in page:
            site = page.rsplit(separator, 1)[1].strip()
            if 4 <= len(site) <= 40:
                return site
    return None


def _strip_suffix(title, suffixes):
    for suffix in suffixes:
        if title.endswith(suffix):
            return title[:-len(suffix)].strip()
    return None


def classify(app_name, window_title):
    """(category, label) for one title, or None if it describes nothing.

    Category is the kind of work; label is the specific thing. Two rows sharing
    a (category, label) are the same activity and get summed — which is what
    turns forty focus switches into the four lines a person would have written.
    """
    app = (app_name or '').lower()
    title = clean(window_title)

    if title.lower() in EMPTY_TITLES and app not in TERMINALS:
        # A bare program name still says which program had focus.
        return ('App', app_name or 'unknown') if app_name else None

    # Terminals. Claude Code renames the terminal to the task it is working on,
    # so these titles are the closest thing to a human-written log entry we get.
    if app in TERMINALS:
        if title.startswith('!'):
            return 'Shell', title.lstrip('! ').strip()[:80]
        if not title or title.lower() in EMPTY_TITLES:
            return 'Claude Code', 'session (no task set)'
        return 'Claude Code', title

    # Editors: "file - project - Cursor". The project is the second-to-last
    # segment, and it is the useful half — the file changes every two minutes,
    # the project does not.
    if app in EDITORS:
        parts = [p.strip() for p in title.split(' - ')]
        if len(parts) >= 3:
            return 'Coding', parts[-2]
        if len(parts) == 2:
            return 'Coding', parts[0]
        return 'Coding', title or (app_name or 'editor')

    if app in BROWSERS:
        page = _strip_suffix(title, BROWSER_SUFFIXES)
        page = title if page is None else page
        if not page or page.lower() in EMPTY_TITLES:
            return None
        # A run of searches is itself the activity ("costing a car hire"), even
        # though no single query deserves a line.
        if page.endswith(' - Google Search'):
            return 'Search', page[:-len(' - Google Search')].strip()
        if page.endswith(' - Gmail'):
            return 'Email', 'Gmail'
        if 'Outlook' in page or 'Mail —' in page:
            return 'Email', page
        return 'Web', page

    # "notes.md (~/Desktop/project/docs)" — the folder is the project.
    if app in TEXT_EDITORS:
        match = re.match(r'^(.*?)\s*\((~?[^)]*)\)\s*$', title)
        if match:
            return 'Writing', f'{match.group(1)} in {match.group(2)}'
        return 'Writing', title or (app_name or 'editor')

    return 'App', title or (app_name or 'unknown')


def _overlap(row_start, row_end, window_start, window_end):
    start = max(row_start, window_start)
    end = min(row_end, window_end)
    return max(0, int((end - start).total_seconds()))


def summarise(rows, window_start, window_end):
    """(activities, tracked_seconds) for usage rows overlapping a window.

    `rows` need only expose app_name, window_title, started_at and ended_at, so
    this works equally on ORM objects and on plain dicts in a test.
    """
    totals, seen, tracked = {}, {}, 0

    for row in rows:
        started = getattr(row, 'started_at', None) or row['started_at']
        ended = getattr(row, 'ended_at', None) or row['ended_at']
        seconds = _overlap(started, ended, window_start, window_end)
        if seconds < MIN_ROW_SECONDS:
            continue

        entry = classify(getattr(row, 'app_name', None) or row.get('app_name'),
                         getattr(row, 'window_title', None) if not isinstance(row, dict)
                         else row.get('window_title'))
        if entry is None:
            continue

        totals[entry] = totals.get(entry, 0) + seconds
        tracked += seconds
        seen.setdefault(entry[0], [])
        if entry[1] not in seen[entry[0]]:
            seen[entry[0]].append(entry[1])

    shorts = {}
    _fold_sites(totals, shorts)
    _fold_searches(totals, shorts, seen)

    activities = [
        {'category': category, 'label': label,
         'short': shorts.get((category, label)) or shorten(label, MAX_HEADLINE_LABEL),
         'seconds': seconds}
        for (category, label), seconds in sorted(totals.items(), key=lambda kv: -kv[1])
    ]
    return activities, tracked


def _fold_sites(totals, shorts):
    """Reading eleven pages of one site is a single activity. Listing every page
    buries the things that only got one line each."""
    by_site = {}
    for key in [k for k in totals if k[0] == 'Web']:
        site = site_of(key[1])
        if site:
            by_site.setdefault(site, []).append(key)

    for site, keys in by_site.items():
        if len(keys) < MIN_PAGES_PER_SITE:
            continue
        seconds = sum(totals.pop(k) for k in keys)
        # A site's own landing page has no page name to strip, so it sits under
        # the bare site name and would otherwise print as a second, nearly
        # identical line right beside the group.
        if ('Web', site) in totals:
            seconds += totals.pop(('Web', site))
            keys.append(('Web', site))
        label = f'{site} ({len(keys)} pages)'
        totals[('Web', label)] = totals.get(('Web', label), 0) + seconds
        shorts[('Web', label)] = shorten(site, MAX_HEADLINE_LABEL)


def _fold_searches(totals, shorts, seen):
    """A dozen one-off queries are one activity, not a dozen. The evidence is
    what makes the line useful, and also what makes it far too long for a
    headline — so the fold records a short form alongside."""
    keys = [k for k in totals if k[0] == 'Search']
    if len(keys) <= 1:
        return
    seconds = sum(totals.pop(k) for k in keys)
    queries = seen.get('Search', [])
    label = f'{len(keys)} queries — ' + '; '.join(f'"{q}"' for q in queries[:3])
    if len(queries) > 3:
        label += f'; +{len(queries) - 3} more'
    totals[('Search', label)] = seconds
    shorts[('Search', label)] = f'{len(keys)} web searches'


def format_hm(seconds):
    hours, minutes = divmod(int(seconds) // 60, 60)
    return f'{hours}h {minutes:02d}m' if hours else f'{minutes}m'


def headline(activities, limit=3):
    """One sentence, the way you would answer "what were you doing?" in passing."""
    if not activities:
        return 'Nothing recorded.'
    parts = [f'{a["short"]} ({format_hm(a["seconds"])})' for a in activities[:limit]]
    if len(parts) == 1:
        return parts[0]
    return ', '.join(parts[:-1]) + ' and ' + parts[-1]
