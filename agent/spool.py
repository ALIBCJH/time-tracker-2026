"""The agent's local queue.

Everything the agent records is written here first and uploaded second. That
ordering is the whole design: the laptop is the source of truth until the server
has acknowledged a record, so a dropped connection, a closed lid, or a server
that is down for a day costs nothing.

Stdlib only, on purpose — the agent runs on other people's machines and every
dependency is something that can fail to install there.

Two lifecycles, because two kinds of record behave differently:

  * app usage and idle periods are FINISHED when recorded. They are uploaded
    once and then pruned.
  * a session CHANGES after it is recorded — it opens, runs, and later closes.
    So it is marked dirty on every change and re-uploaded until the server has
    the latest version. It is never pruned while it is still open.
"""
import json
import os
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone

DEFAULT_DIR = os.path.expanduser('~/.timetracker-agent')

# How long an uploaded record is kept before pruning. Not zero: keeping a few
# days means a server-side restore can be replayed from the laptops.
KEEP_SYNCED_DAYS = 7

# A record the server has refused this many times is not going to be accepted.
# Retrying it for ever would block nothing (the spool uploads around it) but
# would grow without bound and hide the problem.
MAX_ATTEMPTS = 5

SCHEMA = '''
CREATE TABLE IF NOT EXISTS sessions (
    client_uuid       TEXT PRIMARY KEY,
    project           TEXT NOT NULL,
    task              TEXT NOT NULL DEFAULT '',
    started_at        TEXT NOT NULL,
    ended_at          TEXT,
    last_heartbeat_at TEXT,
    idle_since        TEXT,
    dirty             INTEGER NOT NULL DEFAULT 1,
    attempts          INTEGER NOT NULL DEFAULT 0,
    dead              INTEGER NOT NULL DEFAULT 0,
    synced_at         TEXT
);

CREATE TABLE IF NOT EXISTS app_usage (
    client_uuid         TEXT PRIMARY KEY,
    session_client_uuid TEXT,
    app_name            TEXT NOT NULL,
    window_title        TEXT NOT NULL DEFAULT '',
    started_at          TEXT NOT NULL,
    ended_at            TEXT NOT NULL,
    attempts            INTEGER NOT NULL DEFAULT 0,
    dead                INTEGER NOT NULL DEFAULT 0,
    synced_at           TEXT
);

CREATE TABLE IF NOT EXISTS idle_periods (
    client_uuid TEXT PRIMARY KEY,
    started_at  TEXT NOT NULL,
    ended_at    TEXT NOT NULL,
    -- 1 while the break is still happening. Written the moment input stops
    -- rather than when it resumes, so an agent killed mid-break still leaves
    -- the gap on record instead of the server counting it as work.
    open        INTEGER NOT NULL DEFAULT 0,
    attempts    INTEGER NOT NULL DEFAULT 0,
    dead        INTEGER NOT NULL DEFAULT 0,
    synced_at   TEXT
);

CREATE TABLE IF NOT EXISTS screenshots (
    client_uuid         TEXT PRIMARY KEY,
    session_client_uuid TEXT,
    captured_at         TEXT NOT NULL,
    full_path           TEXT NOT NULL,
    thumb_path          TEXT,
    attempts            INTEGER NOT NULL DEFAULT 0,
    dead                INTEGER NOT NULL DEFAULT 0,
    synced_at           TEXT
);

CREATE INDEX IF NOT EXISTS ix_shots_pending    ON screenshots(synced_at, dead);
CREATE INDEX IF NOT EXISTS ix_sessions_pending ON sessions(dirty, dead);
CREATE INDEX IF NOT EXISTS ix_usage_pending    ON app_usage(synced_at, dead);
CREATE INDEX IF NOT EXISTS ix_idle_pending     ON idle_periods(synced_at, dead);
'''

_FINISHED = ('app_usage', 'idle_periods')


def now_iso():
    """Always with an offset. The server refuses a naive timestamp, and it is
    right to — a timestamp without a zone is a guess about which day it was."""
    return datetime.now(timezone.utc).isoformat()


class Spool:
    def __init__(self, path=None):
        self.dir = path or DEFAULT_DIR
        os.makedirs(self.dir, exist_ok=True)
        self.path = os.path.join(self.dir, 'spool.db')
        self.conn = sqlite3.connect(self.path, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        # WAL so a read while the uploader is writing doesn't block the tracker.
        self.conn.execute('PRAGMA journal_mode=WAL')
        self.conn.executescript(SCHEMA)
        self._add_missing_columns()

    def _add_missing_columns(self):
        """Bring an already-installed spool up to the current shape.

        CREATE TABLE IF NOT EXISTS does nothing to a table that already exists,
        so a laptop that has been running since before pausing existed would
        otherwise fail on the first write to a column it has never had.
        """
        for table, column, ddl in (
                ('sessions', 'idle_since', 'TEXT'),
                ('idle_periods', 'open', 'INTEGER NOT NULL DEFAULT 0')):
            have = {r['name'] for r in self.conn.execute(f'PRAGMA table_info({table})')}
            if column not in have:
                self.conn.execute(f'ALTER TABLE {table} ADD COLUMN {column} {ddl}')

    def close(self):
        self.conn.close()

    # ── Recording ────────────────────────────────────────────────────────────

    def start_session(self, project, task='', started_at=None):
        """Open a session locally. Returns its client_uuid.

        No server round trip: the widget must be able to start tracking on a
        train. Whether the server has heard about it yet is the uploader's
        problem, not the user's.
        """
        cu = str(uuid.uuid4())
        self.conn.execute(
            'INSERT INTO sessions (client_uuid, project, task, started_at, '
            'last_heartbeat_at) VALUES (?,?,?,?,?)',
            (cu, project, task, started_at or now_iso(), now_iso()))
        return cu

    def adopt_session(self, server_session):
        """Take on a session the server already has — one started from the
        dashboard. Inserted CLEAN, because uploading it back would be telling
        the server something it just told us.
        """
        self.conn.execute(
            'INSERT OR IGNORE INTO sessions (client_uuid, project, task, '
            'started_at, last_heartbeat_at, dirty, synced_at) '
            'VALUES (?,?,?,?,?,0,?)',
            (server_session['client_uuid'], server_session.get('project', ''),
             server_session.get('task') or '', server_session['started_at'],
             now_iso(), now_iso()))
        return server_session['client_uuid']

    def stop_session(self, client_uuid, ended_at=None):
        self.conn.execute(
            'UPDATE sessions SET ended_at=?, dirty=1, synced_at=NULL '
            'WHERE client_uuid=? AND ended_at IS NULL',
            (ended_at or now_iso(), client_uuid))

    def heartbeat(self, client_uuid, at=None):
        """Mark the open session alive. This is what the server caps an
        abandoned session at when the agent dies mid-session."""
        self.conn.execute(
            'UPDATE sessions SET last_heartbeat_at=?, dirty=1 '
            'WHERE client_uuid=? AND ended_at IS NULL', (at or now_iso(), client_uuid))

    def open_session(self):
        row = self.conn.execute(
            'SELECT * FROM sessions WHERE ended_at IS NULL ORDER BY started_at DESC'
        ).fetchone()
        return dict(row) if row else None

    def record_app_usage(self, app_name, window_title, started_at, ended_at,
                         session_client_uuid=None):
        cu = str(uuid.uuid4())
        self.conn.execute(
            'INSERT INTO app_usage (client_uuid, session_client_uuid, app_name, '
            'window_title, started_at, ended_at) VALUES (?,?,?,?,?,?)',
            (cu, session_client_uuid, app_name, window_title, started_at, ended_at))
        return cu

    def record_screenshot(self, captured_at, full_path, thumb_path=None,
                          session_client_uuid=None):
        """Queue a capture. The image files stay on disk until the server has
        them — the spool row is an index, not the picture."""
        cu = str(uuid.uuid4())
        self.conn.execute(
            'INSERT INTO screenshots (client_uuid, session_client_uuid, captured_at, '
            'full_path, thumb_path) VALUES (?,?,?,?,?)',
            (cu, session_client_uuid, captured_at, full_path, thumb_path))
        return cu

    def pending_screenshots(self, limit=20):
        """Uploaded one at a time, not in the JSON batch: images are large and a
        failed 8MB request should cost one capture, not a day of them."""
        return [dict(r) for r in self.conn.execute(
            'SELECT * FROM screenshots WHERE synced_at IS NULL AND dead=0 '
            'ORDER BY captured_at LIMIT ?', (limit,))]

    def screenshot_sent(self, client_uuid):
        """Mark uploaded and remove the local copies — the server has them now,
        and a laptop is the one place these should not accumulate."""
        row = self.conn.execute('SELECT * FROM screenshots WHERE client_uuid=?',
                                (client_uuid,)).fetchone()
        if row is None:
            return
        for path in (row['full_path'], row['thumb_path']):
            if path and os.path.exists(path):
                try:
                    os.remove(path)
                except OSError:
                    pass
        self.conn.execute(
            'UPDATE screenshots SET synced_at=?, attempts=0 WHERE client_uuid=?',
            (now_iso(), client_uuid))

    def screenshot_failed(self, client_uuid):
        self.conn.execute(
            'UPDATE screenshots SET attempts = attempts + 1, '
            'dead = CASE WHEN attempts + 1 >= ? THEN 1 ELSE 0 END '
            'WHERE client_uuid = ?', (MAX_ATTEMPTS, client_uuid))

    def record_idle(self, started_at, ended_at):
        """A break that is already over. Uploadable immediately."""
        cu = str(uuid.uuid4())
        self.conn.execute(
            'INSERT INTO idle_periods (client_uuid, started_at, ended_at, open) '
            'VALUES (?,?,?,0)', (cu, started_at, ended_at))
        return cu

    def open_idle(self, started_at):
        """A break that has just begun. Held back from upload until it closes —
        the server takes an idle period as final, and re-sending a longer one
        would be ignored by the conflict clause that makes resends safe."""
        cu = str(uuid.uuid4())
        self.conn.execute(
            'INSERT INTO idle_periods (client_uuid, started_at, ended_at, open) '
            'VALUES (?,?,?,1)', (cu, started_at, started_at))
        return cu

    def extend_idle(self, client_uuid, ended_at):
        self.conn.execute(
            'UPDATE idle_periods SET ended_at=? WHERE client_uuid=? AND open=1',
            (ended_at, client_uuid))

    def close_idle(self, client_uuid, ended_at):
        self.conn.execute(
            'UPDATE idle_periods SET ended_at=?, open=0 WHERE client_uuid=?',
            (ended_at, client_uuid))

    def close_stale_idle(self):
        """Settle any break left open by a crash, at wherever it last reached.

        Called at startup. Without it the gap would sit unsent for ever and the
        session it belongs to would have its break counted as work.
        """
        cursor = self.conn.execute(
            'UPDATE idle_periods SET open=0 WHERE open=1')
        return cursor.rowcount

    def set_idle_since(self, client_uuid, at):
        """Mark the open session as being in a break, or out of one (at=None).
        Dirty, so the server hears about it on the next upload."""
        self.conn.execute(
            'UPDATE sessions SET idle_since=?, dirty=1 '
            'WHERE client_uuid=? AND ended_at IS NULL', (at, client_uuid))

    # ── Uploading ────────────────────────────────────────────────────────────

    def pending_batch(self, limit=500):
        """The next batch to upload, oldest first.

        Sessions come out whenever they are dirty, not only when new: an open
        session is re-sent as it changes, and the server's upsert makes each
        resend a no-op or an update rather than a duplicate.
        """
        batch = {'sessions': [], 'app_usage': [], 'idle_periods': []}

        for r in self.conn.execute(
                'SELECT * FROM sessions WHERE dirty=1 AND dead=0 '
                'ORDER BY started_at LIMIT ?', (limit,)):
            batch['sessions'].append({
                'client_uuid': r['client_uuid'], 'project': r['project'],
                'task': r['task'], 'started_at': r['started_at'],
                'ended_at': r['ended_at'], 'last_heartbeat_at': r['last_heartbeat_at'],
                'idle_since': r['idle_since']})

        for r in self.conn.execute(
                'SELECT * FROM app_usage WHERE synced_at IS NULL AND dead=0 '
                'ORDER BY started_at LIMIT ?', (limit,)):
            batch['app_usage'].append({
                'client_uuid': r['client_uuid'],
                'session_client_uuid': r['session_client_uuid'],
                'app_name': r['app_name'], 'window_title': r['window_title'],
                'started_at': r['started_at'], 'ended_at': r['ended_at']})

        for r in self.conn.execute(
                'SELECT * FROM idle_periods '
                'WHERE synced_at IS NULL AND dead=0 AND open=0 '
                'ORDER BY started_at LIMIT ?', (limit,)):
            batch['idle_periods'].append({
                'client_uuid': r['client_uuid'], 'started_at': r['started_at'],
                'ended_at': r['ended_at']})

        return batch

    def mark_accepted(self, batch, rejected=()):
        """Settle a batch against the server's answer.

        Only records the server did NOT reject are marked synced. A rejected
        record has its attempt counted and is retried, up to MAX_ATTEMPTS —
        after which it is marked dead and stops consuming bandwidth for ever.
        Nothing is deleted here: pruning is a separate, later decision.
        """
        bad = {(r.get('kind'), r.get('index')) for r in rejected}
        stamp = now_iso()

        for kind, records in batch.items():
            for i, record in enumerate(records):
                cu = record['client_uuid']
                if (kind, i) in bad or (kind, None) in bad:
                    self.conn.execute(
                        f'UPDATE {kind} SET attempts = attempts + 1, '
                        f'dead = CASE WHEN attempts + 1 >= ? THEN 1 ELSE 0 END '
                        f'WHERE client_uuid = ?', (MAX_ATTEMPTS, cu))
                elif kind == 'sessions':
                    # Clean only if it still looks the way we sent it: a session
                    # that changed mid-upload must stay dirty, or that change is
                    # lost.
                    self.conn.execute(
                        'UPDATE sessions SET dirty=0, attempts=0, synced_at=? '
                        'WHERE client_uuid=? AND IFNULL(ended_at, "") = IFNULL(?, "") '
                        'AND IFNULL(last_heartbeat_at, "") = IFNULL(?, "")',
                        (stamp, cu, record.get('ended_at'), record.get('last_heartbeat_at')))
                else:
                    self.conn.execute(
                        f'UPDATE {kind} SET synced_at=?, attempts=0 WHERE client_uuid=?',
                        (stamp, cu))

    def prune(self, keep_days=KEEP_SYNCED_DAYS):
        """Drop uploaded records older than the keep window. Open sessions are
        never pruned however old — an open session is still live state."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=keep_days)).isoformat()
        removed = 0
        for table in _FINISHED:
            cur = self.conn.execute(
                f'DELETE FROM {table} WHERE synced_at IS NOT NULL AND synced_at < ?',
                (cutoff,))
            removed += cur.rowcount
        cur = self.conn.execute(
            'DELETE FROM sessions WHERE ended_at IS NOT NULL AND dirty=0 '
            'AND synced_at IS NOT NULL AND synced_at < ?', (cutoff,))
        return removed + cur.rowcount

    def stats(self):
        def count(table, where):
            return self.conn.execute(f'SELECT COUNT(*) c FROM {table} WHERE {where}'
                                     ).fetchone()['c']
        return {
            'pending_sessions': count('sessions', 'dirty=1 AND dead=0'),
            'pending_app_usage': count('app_usage', 'synced_at IS NULL AND dead=0'),
            'pending_idle': count('idle_periods', 'synced_at IS NULL AND dead=0'),
            'pending_screenshots': count('screenshots', 'synced_at IS NULL AND dead=0'),
            'dead': sum(count(t, 'dead=1')
                        for t in ('sessions', 'screenshots') + _FINISHED),
        }
