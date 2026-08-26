"""Normalising window titles before they are compared.

A window title is not stable. Terminals animate a spinner into it, mail clients
count unread messages in it, players tick a timestamp through it. The title
changes while the window does not, and a capture loop that compares raw titles
sees a new activity every poll.

The local app hit this and fixed it in the summarizer, downstream — by which
point one continuous hour is already a hundred one-second rows in the database
and the duration of any single one is meaningless. Doing it here means the span
never fragments in the first place: fewer rows, honest durations, and the
summarizer gets clean input instead of having to reconstruct it.

The spinner glyphs are the same set the local summarizer learned the hard way.
"""
import re

SPINNER_GLYPHS = '✳◐◑◒◓✢✻✽·*⠂⠄⠆⠈⠐⠠⡀⢀⣀⠁⠉⣾⣽⣻⢿⡿⣟⣯⣷▁▂▃▄▅▆▇█⣷⣯⣟'

_LEADING_SPINNER = re.compile(r'^[' + re.escape(SPINNER_GLYPHS) + r'\s]+')
# "(3) Inbox" — an unread count that changes as mail arrives.
_LEADING_COUNT = re.compile(r'^\s*\(\d[\d,]*\)\s*')
# "Inbox (3)" — the same idea at the other end.
_TRAILING_COUNT = re.compile(r'\s*\(\d[\d,]*\)\s*$')
# "01:23 / 45:00" or "1:23:45" — a media position ticking forward.
_TIMECODE = re.compile(r'\b\d{1,2}:\d{2}(?::\d{2})?\s*(?:/\s*\d{1,2}:\d{2}(?::\d{2})?)?\b')
_WHITESPACE = re.compile(r'\s+')


def normalise(title: str) -> str:
    """The stable part of a title — what two samples of the same window share."""
    if not title:
        return ''
    cleaned = _LEADING_SPINNER.sub('', title)
    cleaned = _LEADING_COUNT.sub('', cleaned)
    cleaned = _TRAILING_COUNT.sub('', cleaned)
    cleaned = _TIMECODE.sub('', cleaned)
    return _WHITESPACE.sub(' ', cleaned).strip()
