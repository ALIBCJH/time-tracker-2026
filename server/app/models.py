"""The schema.

Three rules run through all of it, and they are the three things the local app
got wrong in ways that only matter once it is hosted:

1. Every row belongs to a user.  `user_id` is NOT NULL on every table that holds
   tracked data, and every query filters on it.  The local schema had no notion
   of a user at all, so "show me my week" and "show me everyone's week" were the
   same query.

2. Every instant is timezone-aware.  Columns are TIMESTAMPTZ and store UTC; the
   user's IANA timezone turns those into the days and weeks a report talks about.
   The local app used naive datetime.now() throughout, which silently means "the
   timezone the process happens to run in" — fine on one laptop in Nairobi,
   wrong the moment a server runs in UTC.

3. Invariants live in the database.  A user can have only one open session, and
   a report can be sent only once per period.  The local app enforced both in
   Python — a BEGIN IMMEDIATE dance and a JSON state file — and both leaked
   (duplicate active sessions in July, a duplicate report send today).  Partial
   unique indexes do it properly and survive two web workers racing.
"""
import uuid
from datetime import datetime, date

from sqlalchemy import (BigInteger, Boolean, CheckConstraint, Date, DateTime,
                        ForeignKey, Index, Integer, String, Text,
                        UniqueConstraint, func, text)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


def _uuid_pk():
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


def _created_at():
    return mapped_column(DateTime(timezone=True), nullable=False,
                         server_default=func.now())


# ── People ───────────────────────────────────────────────────────────────────

class User(Base):
    """A person. Flat list, no teams: this is a three-person deployment and an
    admin simply sees everyone. Adding a team_id later is an additive migration,
    which is cheaper than carrying an org hierarchy nobody uses."""
    __tablename__ = 'users'

    id: Mapped[uuid.UUID] = _uuid_pk()
    # Stored lowercased by the application so a plain unique index is enough —
    # cheaper than requiring the citext extension for one column.
    email: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False, default='worker')
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = _created_at()

    settings: Mapped['UserSettings'] = relationship(
        back_populates='user', uselist=False, cascade='all, delete-orphan')

    __table_args__ = (
        CheckConstraint("role IN ('worker', 'admin')", name='ck_users_role'),
    )

    # ── Flask-Login contract ──
    @property
    def is_authenticated(self):
        return True

    @property
    def is_anonymous(self):
        return False

    def get_id(self):
        return str(self.id)

    @property
    def is_admin(self):
        return self.role == 'admin'

    def __repr__(self):
        return f'<User {self.email} ({self.role})>'


class UserSettings(Base):
    """Everything the local app hardcoded as a module constant or kept in one
    shared email_config.json. All of it is per-person here: your idle threshold,
    prompt hours and stream map are not your colleague's."""
    __tablename__ = 'user_settings'

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey('users.id', ondelete='CASCADE'), primary_key=True)

    # IANA name. This is what turns a TIMESTAMPTZ into "which day was that".
    timezone: Mapped[str] = mapped_column(String(64), nullable=False,
                                          default='Africa/Nairobi')

    day_goal_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=8 * 3600)
    week_goal_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=40 * 3600)

    # Seconds of no input before the running session PAUSES. It is not closed:
    # the idle time is excluded from the total and the same session continues
    # when you come back, so a day's work on one project is one session rather
    # than a dozen fragments. A pause waits indefinitely — somebody who wants
    # to stop has the pause control in tracking_paused_until for that.
    idle_threshold_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=900)
    screenshot_interval_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=600)
    screenshots_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    # The evening window in which the daily prompt may appear (local hours).
    prompt_start_hour: Mapped[int] = mapped_column(Integer, nullable=False, default=21)
    prompt_end_hour: Mapped[int] = mapped_column(Integer, nullable=False, default=24)

    # Weekly report: 0=Monday. Monthly goes out on the last day of the month.
    weekly_send_weekday: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    weekly_send_hour: Mapped[int] = mapped_column(Integer, nullable=False, default=17)
    monthly_send_hour: Mapped[int] = mapped_column(Integer, nullable=False, default=21)
    reports_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # Mail about the agent itself rather than about the work: a session the
    # server had to cap, a device that stopped reporting. Separate from
    # reports_enabled because "do not send me a weekly summary" and "do not
    # tell me my tracker is broken" are not the same request.
    offline_alerts_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False,
                                                         default=True)

    # Ordered [[name, [patterns]], ...] for the monthly work-stream donut, the
    # catch-all every unmatched label falls into, and the labels that are never
    # named in a report someone else reads. Shapes that are lists of strings and
    # will keep changing — JSONB rather than five more tables.
    streams: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    catch_all_stream: Mapped[str] = mapped_column(String(64), nullable=False,
                                                  default='Deep Research')
    private_labels: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    # Set by the tracked person to stop everything for a while. NULL means
    # not paused. A far-future value is an indefinite pause. The agent reads
    # this and stops; nothing here relies on the agent being honest, because
    # anything it does upload while paused is refused.
    tracking_paused_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    pause_reason: Mapped[str | None] = mapped_column(String(200))

    # Extra addresses that receive this person's reports — how an admin gets a
    # copy of a worker's week. A list rather than one field because "the boss
    # and the accountant" is the normal case, not an exception.
    report_cc: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    research_labels: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))

    user: Mapped[User] = relationship(back_populates='settings')

    __table_args__ = (
        CheckConstraint('prompt_start_hour BETWEEN 0 AND 23', name='ck_settings_prompt_start'),
        CheckConstraint('prompt_end_hour BETWEEN 1 AND 24', name='ck_settings_prompt_end'),
        CheckConstraint('weekly_send_weekday BETWEEN 0 AND 6', name='ck_settings_weekday'),
        CheckConstraint('idle_threshold_seconds > 0', name='ck_settings_idle'),
    )


class Consent(Base):
    """A record that someone was told what is collected, and agreed.

    Kept as an append-only log rather than a flag on the user, because the
    question that gets asked later is "what were they told, and when" — and a
    boolean cannot answer it. A new policy version means a new row, so changing
    what is collected requires asking again rather than silently inheriting an
    agreement to something else.
    """
    __tablename__ = 'consents'

    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey('users.id', ondelete='CASCADE'), nullable=False)
    policy_version: Mapped[str] = mapped_column(String(32), nullable=False)
    accepted_at: Mapped[datetime] = _created_at()
    # Evidence of where the agreement came from, kept short and non-identifying
    # beyond what a web server logs anyway.
    source_ip: Mapped[str | None] = mapped_column(String(64))

    __table_args__ = (
        UniqueConstraint('user_id', 'policy_version', name='uq_consent_user_version'),
    )


class Device(Base):
    """One installed agent. The token is what an agent authenticates with, and
    only its hash is stored — a leaked database must not yield working tokens."""
    __tablename__ = 'devices'

    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey('users.id', ondelete='CASCADE'), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _created_at()

    __table_args__ = (Index('ix_devices_user', 'user_id'),)


# ── Tracked data ─────────────────────────────────────────────────────────────

class Session(Base):
    """A stretch of tracked work.

    `ended_at IS NULL` means open — there is no separate is_active flag to
    disagree with it. `last_heartbeat_at` is what an open session is capped at
    when the agent dies: the local app had to infer that moment from other
    tables, and an agent that simply says so is both simpler and more accurate.
    """
    __tablename__ = 'sessions'

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey('users.id', ondelete='CASCADE'), nullable=False)
    # Minted by the agent before the row ever leaves the laptop. Uploads are
    # idempotent on it, which is the whole reason an agent can retry a batch
    # after a dropped connection without inventing duplicate work.
    client_uuid: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, default=uuid.uuid4)

    project: Mapped[str] = mapped_column(String(120), nullable=False)
    task: Mapped[str] = mapped_column(Text, nullable=False, default='')

    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # When the current pause began, or NULL if input is flowing. A completed
    # break becomes an idle_periods row; this covers the one still happening,
    # which has no row yet — without it the dashboard would keep counting up
    # while somebody is at lunch.
    idle_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Set when the server capped this session itself because the agent went
    # silent. It is the difference between an end time somebody asserted and
    # one we inferred, and only the inferred kind is worth telling anyone about
    # — a number that came from a guess should be visible as a guess.
    orphaned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _created_at()

    __table_args__ = (
        # The invariant the local app tried to hold with BEGIN IMMEDIATE and
        # lost anyway: one open session per person, enforced by the database so
        # two racing writers cannot both win.
        Index('uq_sessions_one_open_per_user', 'user_id',
              unique=True, postgresql_where=text('ended_at IS NULL')),
        Index('ix_sessions_user_started', 'user_id', 'started_at'),
        UniqueConstraint('user_id', 'client_uuid', name='uq_sessions_client_uuid'),
        CheckConstraint('ended_at IS NULL OR ended_at >= started_at',
                        name='ck_sessions_order'),
    )


class AppUsage(Base):
    """What was in the foreground, and for how long. The raw material the daily
    draft is summarised from — the richest signal in the system."""
    __tablename__ = 'app_usage'

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey('users.id', ondelete='CASCADE'), nullable=False)
    client_uuid: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, default=uuid.uuid4)
    session_id: Mapped[int | None] = mapped_column(
        ForeignKey('sessions.id', ondelete='SET NULL'))
    app_name: Mapped[str] = mapped_column(String(120), nullable=False)
    window_title: Mapped[str] = mapped_column(Text, nullable=False, default='')
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ended_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    duration_seconds: Mapped[int] = mapped_column(Integer, nullable=False)

    __table_args__ = (
        Index('ix_app_usage_user_started', 'user_id', 'started_at'),
        UniqueConstraint('user_id', 'client_uuid', name='uq_app_usage_client_uuid'),
    )


class IdlePeriod(Base):
    __tablename__ = 'idle_periods'

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey('users.id', ondelete='CASCADE'), nullable=False)
    client_uuid: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, default=uuid.uuid4)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ended_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    duration_seconds: Mapped[int] = mapped_column(Integer, nullable=False)

    __table_args__ = (
        Index('ix_idle_user_started', 'user_id', 'started_at'),
        UniqueConstraint('user_id', 'client_uuid', name='uq_idle_client_uuid'),
    )


class Screenshot(Base):
    """A capture. The image lives in object storage; this row is the index.

    Full-resolution and thumbnail are separate objects with separate lifetimes —
    the full one expires in weeks, the thumbnail survives a year — so the visual
    timeline outlives the evidence. `full_deleted_at` records when the gap
    opened, so the UI can say "expired" instead of showing a broken image.
    """
    __tablename__ = 'screenshots'

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey('users.id', ondelete='CASCADE'), nullable=False)
    client_uuid: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, default=uuid.uuid4)
    session_id: Mapped[int | None] = mapped_column(
        ForeignKey('sessions.id', ondelete='SET NULL'))
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    full_key: Mapped[str] = mapped_column(String(512), nullable=False)
    thumb_key: Mapped[str | None] = mapped_column(String(512))
    bytes_full: Mapped[int | None] = mapped_column(Integer)
    full_deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        Index('ix_screenshots_user_captured', 'user_id', 'captured_at'),
        UniqueConstraint('user_id', 'client_uuid', name='uq_screenshots_client_uuid'),
    )


class ActivityLog(Base):
    """One row per reporting day: the machine's account of it, and yours.

    UNIQUE is on (user_id, log_date), not log_date alone. The local app's
    single-column constraint would have stopped two people logging the same day.
    """
    __tablename__ = 'activity_logs'

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey('users.id', ondelete='CASCADE'), nullable=False)
    # The calendar date in the USER's timezone, not the server's.
    log_date: Mapped[date] = mapped_column(Date, nullable=False)

    headline: Mapped[str | None] = mapped_column(Text)
    draft: Mapped[str | None] = mapped_column(Text)
    note: Mapped[str | None] = mapped_column(Text)
    tracked_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    activities: Mapped[dict] = mapped_column(JSONB, nullable=False,
                                             server_default=text("'[]'::jsonb"))
    status: Mapped[str] = mapped_column(String(16), nullable=False, default='draft')

    created_at: Mapped[datetime] = _created_at()
    answered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    answered_seconds: Mapped[int | None] = mapped_column(Integer)

    __table_args__ = (
        UniqueConstraint('user_id', 'log_date', name='uq_activity_logs_user_date'),
        CheckConstraint("status IN ('draft', 'confirmed', 'skipped')",
                        name='ck_activity_logs_status'),
    )


class PasswordReset(Base):
    """A single-use ticket to set a new password.

    Stored as a hash, like an agent token and for the same reason: the value is
    high-entropy and random, so a fast hash is right, and a stolen database must
    not hand anyone a working reset link.

    Rows are kept after use rather than deleted, so "this link was already
    used" can be answered honestly instead of being indistinguishable from
    "this link never existed".
    """
    __tablename__ = 'password_resets'

    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey('users.id', ondelete='CASCADE'), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _created_at()

    __table_args__ = (Index('ix_resets_user', 'user_id'),)


class RateBucket(Base):
    """One rate-limit counter per (scope, key, window).

    Defined here with every other model rather than beside the limiter logic:
    when it lived in app/ratelimit.py, whether the table got created at all
    depended on whether something had imported that module first. Alembic
    generated an empty migration, and the test schema included the table or not
    depending on test order.
    """
    __tablename__ = 'rate_buckets'

    scope: Mapped[str] = mapped_column(String(32), primary_key=True)
    key: Mapped[str] = mapped_column(String(255), primary_key=True)
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                   primary_key=True)
    count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class AgentAlert(Base):
    """One row per alert about the tracking itself actually sent.

    The same shape as ReportSend, and for the same reason: the record IS the
    send-once guard. An alert that fires every time the worker ticks is an
    alert everybody filters into a folder within a week, and an alert nobody
    reads is worse than none — it converts a real signal into noise that also
    hides the next real signal.

    `dedupe_key` is what decides when the same condition may be reported again,
    and it differs by kind on purpose:

      * session_dropped — the session id. One session, one alert, for ever.
      * device_dormant  — the device id plus the last_seen_at it went quiet
        after. When the agent reports again that timestamp moves, so the next
        silence is a genuinely new episode and is allowed to alert. Nothing has
        to remember to clear a flag.
    """
    __tablename__ = 'agent_alerts'

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey('users.id', ondelete='CASCADE'), nullable=False)
    kind: Mapped[str] = mapped_column(String(24), nullable=False)
    dedupe_key: Mapped[str] = mapped_column(String(160), nullable=False)
    sent_at: Mapped[datetime] = _created_at()
    recipient: Mapped[str] = mapped_column(String(255), nullable=False)

    __table_args__ = (
        UniqueConstraint('user_id', 'kind', 'dedupe_key', name='uq_agent_alerts_dedupe'),
        CheckConstraint("kind IN ('session_dropped', 'device_dormant')",
                        name='ck_agent_alerts_kind'),
        Index('ix_agent_alerts_user_sent', 'user_id', 'sent_at'),
    )


class ReportSend(Base):
    """One row per report actually sent. This replaces mailer_state.json.

    The unique constraint IS the send-once guard, and that matters more than it
    sounds: the file-based version fired a duplicate backdated report the first
    time the service restarted without state. A constraint cannot be defeated by
    a missing file, a wiped volume, or a second worker starting at the same
    instant — the second INSERT simply fails and that send does not happen.

    period_key is the human-readable period: '2026-W35' weekly, '2026-08' monthly.
    """
    __tablename__ = 'report_sends'

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey('users.id', ondelete='CASCADE'), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    period_key: Mapped[str] = mapped_column(String(16), nullable=False)
    sent_at: Mapped[datetime] = _created_at()
    recipient: Mapped[str] = mapped_column(String(255), nullable=False)

    __table_args__ = (
        UniqueConstraint('user_id', 'kind', 'period_key', name='uq_report_sends_period'),
        CheckConstraint("kind IN ('weekly', 'monthly')", name='ck_report_sends_kind'),
    )
