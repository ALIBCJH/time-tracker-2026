"""What the widget should be showing, decided without any Qt at all.

The local widget put every one of these decisions inside a QWidget — goal
progress, overdrive, when to congratulate someone, whether the backend is up —
which meant none of them could be tested and all of them had to be checked by
sitting in front of the machine for twenty-five minutes. Here the widget draws
what this object says, and this object is a plain class.

Everything is computed from the local spool rather than from the server, so the
face keeps ticking on a train. The server is consulted for settings and for the
daily prompt; it is never needed to answer "how long have I been working".
"""
from datetime import datetime, timedelta, timezone

UTC = timezone.utc

# Non-idle tracked time between encouragements. Long enough to be earned,
# short enough to land inside one sitting.
CHEER_SECONDS = 25 * 60

# How stale the last successful poll may be before the widget says so. Several
# missed cycles, so one slow request does not flicker the face to "offline".
OFFLINE_AFTER = timedelta(seconds=90)

CHEERS = [
    ('✦', '{mins} minutes deep'),
    ('◆', 'Still going'),
    ('✧', 'Another {mins} in'),
    ('❖', 'Focus holding'),
    ('✶', '{mins} more minutes down'),
]


def greeting(now, name=None):
    """Time-of-day greeting. Uses local time — the widget sits on a desk."""
    hour = now.astimezone().hour
    if hour < 12:
        part = 'Good morning'
    elif hour < 17:
        part = 'Good afternoon'
    elif hour < 22:
        part = 'Good evening'
    else:
        part = 'Still up'
    return f'{part}, {name}' if name else part


def format_hm(seconds):
    hours, minutes = divmod(max(0, int(seconds)) // 60, 60)
    return f'{hours}h {minutes:02d}m' if hours else f'{minutes}m'


class WidgetState:
    """The face's model. Fed by poll(); read by whatever draws it."""

    MODES = ('today', 'week')

    def __init__(self, spool, settings, clock=None, name=None):
        self.spool = spool
        self.settings = settings
        self.name = name
        self._clock = clock or (lambda: datetime.now(UTC))

        self.mode = 'today'
        self.is_idle = False
        self.today_seconds = 0
        self.week_seconds = 0
        self.prompts = []
        self.last_contact = None
        self.session = None

        self._streak = 0
        self._last_tick = None
        self._cheer_index = None

    # ── Reading ──────────────────────────────────────────────────────────────

    @property
    def paused(self):
        return bool(getattr(self.settings, 'paused', False))

    @property
    def tracking(self):
        return self.session is not None and not self.paused

    @property
    def elapsed(self):
        """How long the current session has run, from the spool's own record."""
        if not self.session:
            return 0
        started = datetime.fromisoformat(self.session['started_at'])
        return max(0, int((self._clock() - started).total_seconds()))

    @property
    def goal_seconds(self):
        key = 'day_goal_seconds' if self.mode == 'today' else 'week_goal_seconds'
        return max(1, int(self.settings.get(key, 8 * 3600)))

    @property
    def done_seconds(self):
        return self.today_seconds if self.mode == 'today' else self.week_seconds

    @property
    def progress(self):
        """Fraction of the goal, uncapped — 1.4 means forty percent past it."""
        return self.done_seconds / self.goal_seconds

    @property
    def overdrive(self):
        """Past the goal. Drawn differently, because finishing a day's work is
        worth noticing and a bar pinned at 100% says nothing."""
        return self.progress > 1.0

    @property
    def remaining_seconds(self):
        return max(0, self.goal_seconds - self.done_seconds)

    @property
    def offline(self):
        """No successful contact recently. Distinct from paused, and from
        idle — the widget must not imply someone stopped working when it is the
        network that stopped."""
        if self.last_contact is None:
            return True
        return (self._clock() - self.last_contact) > OFFLINE_AFTER

    @property
    def status_line(self):
        if self.paused:
            return 'Paused'
        if not self.session:
            return 'Not tracking'
        if self.is_idle:
            return 'Idle'
        return self.session['project']

    @property
    def pending_prompt(self):
        """The daily card to show, or None. Only ones the server marked active:
        it decides presence so every client agrees."""
        for prompt in self.prompts:
            if prompt.get('active'):
                return prompt
        return None

    @property
    def badge_count(self):
        """Unanswered days, shown quietly even when none is worth interrupting for."""
        return len(self.prompts)

    # ── Updating ─────────────────────────────────────────────────────────────

    def toggle_mode(self):
        self.mode = 'week' if self.mode == 'today' else 'today'
        return self.mode

    def tick(self, is_idle=False, now=None):
        """Advance the streak clock. Returns a cheer when one is earned.

        Counted from real elapsed time between ticks rather than by counting
        ticks, so a slow or skipped poll does not quietly inflate or reset it.
        """
        now = now or self._clock()
        previous, self._last_tick = self._last_tick, now
        self.is_idle = is_idle

        if not self.tracking or is_idle:
            # A streak measures unbroken focus. Idling ends it.
            self._streak = 0
            return None
        if previous is None:
            return None

        self._streak += max(0, (now - previous).total_seconds())
        if self._streak < CHEER_SECONDS:
            return None

        self._streak = 0
        return self._cheer()

    def _cheer(self):
        # Never the same one twice in a row: a repeat reads as a bug rather
        # than as encouragement.
        import random
        choices = [i for i in range(len(CHEERS)) if i != self._cheer_index]
        self._cheer_index = random.choice(choices)
        glyph, template = CHEERS[self._cheer_index]
        title = template.format(mins=CHEER_SECONDS // 60)
        sub = (f'{format_hm(self.done_seconds)} today · '
               f'{format_hm(self.remaining_seconds)} to your goal')
        if self.overdrive:
            sub = f'{format_hm(self.done_seconds)} today · past your goal'
        return {'glyph': glyph, 'title': title, 'sub': sub}

    def start_session(self, project, task=''):
        # The widget's own clock is passed through, so what it displays as
        # elapsed and what the spool recorded as the start cannot disagree.
        now = self._clock()
        self.spool.start_session(project, task, now.isoformat())
        self.session = self.spool.open_session()
        self._streak = 0
        return {'glyph': '▶', 'title': greeting(self._clock(), self.name),
                'sub': f'Tracking "{project}"'}

    def stop_session(self):
        if not self.session:
            return None
        self.spool.stop_session(self.session['client_uuid'],
                                self._clock().isoformat())
        project = self.session['project']
        self.session = None
        self._streak = 0
        return {'glyph': '■', 'title': 'Session stopped',
                'sub': f'{format_hm(self.done_seconds)} tracked today'}

    def refresh_from_spool(self):
        """Session state comes from the laptop, always — so the face is right
        even when the server is unreachable."""
        self.session = self.spool.open_session()

    def note_contact(self, now=None):
        self.last_contact = now or self._clock()

    def set_totals(self, today_seconds=None, week_seconds=None):
        if today_seconds is not None:
            self.today_seconds = int(today_seconds)
        if week_seconds is not None:
            self.week_seconds = int(week_seconds)

    def set_prompts(self, prompts):
        self.prompts = list(prompts or [])
