"""Accepting a batch of tracked data from an agent.

The agent is offline-first: it records to a local spool and uploads when it can,
which means this endpoint must tolerate three things a live API would not.

  * Retries. A batch whose response was lost gets sent again, so every record
    carries a client_uuid minted on the laptop and the upsert is idempotent.
    Sending the same batch twice changes nothing the second time.
  * Lateness. Records can arrive hours after the work happened, so nothing here
    reads the server clock to decide when something occurred.
  * Partial garbage. One malformed record must not reject a day's backlog, so
    each is applied in its own savepoint and failures are reported per record
    while the rest of the batch commits.

Nothing in the payload says who the user is. That comes from the token.
"""
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError

from app.models import AppUsage, IdlePeriod, Session

# A cap so a broken or hostile agent cannot ask the server to chew through an
# unbounded batch in one transaction.
MAX_RECORDS_PER_KIND = 1000

# How far ahead of the server an agent's clock may be before its records are
# refused. Laptop clocks drift and resync; a few minutes is drift, an hour is a
# wrong clock that would file work under the wrong day.
MAX_CLOCK_SKEW = timedelta(minutes=5)

MAX_PROJECT = 120
MAX_APP_NAME = 120
MAX_TEXT = 2000


class RecordError(ValueError):
    """One record is unusable. The rest of the batch is unaffected."""


# ── Parsing ──────────────────────────────────────────────────────────────────

def parse_instant(value, field, *, now=None):
    """An ISO-8601 string WITH an offset, as an aware datetime.

    A naive timestamp is rejected rather than assumed to be UTC. 'Assume UTC' is
    how the local app ended up meaning 'whatever zone the process runs in', and
    a guess here files someone's evening under the wrong day.
    """
    if not isinstance(value, str):
        raise RecordError(f'{field} must be an ISO-8601 string')
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise RecordError(f'{field} is not a valid ISO-8601 timestamp')
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise RecordError(f'{field} must carry a UTC offset')

    now = now or datetime.now(timezone.utc)
    if parsed > now + MAX_CLOCK_SKEW:
        raise RecordError(f'{field} is in the future — check the device clock')
    return parsed


def parse_uuid(value, field):
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        raise RecordError(f'{field} must be a UUID')


def text_field(value, field, limit, *, required=False):
    value = (value or '').strip() if isinstance(value, (str, type(None))) else None
    if value is None:
        raise RecordError(f'{field} must be a string')
    if required and not value:
        raise RecordError(f'{field} is required')
    return value[:limit]


def _span(record, now):
    """(started_at, ended_at, duration_seconds), duration computed here.

    The agent sends both ends and the server derives the duration — a client
    that miscounts, or lies, cannot inflate a total.
    """
    started = parse_instant(record.get('started_at'), 'started_at', now=now)
    ended = parse_instant(record.get('ended_at'), 'ended_at', now=now)
    if ended < started:
        raise RecordError('ended_at is before started_at')
    return started, ended, int((ended - started).total_seconds())


# ── Upserts ──────────────────────────────────────────────────────────────────

def _upsert_session(db, user, record, now):
    started = parse_instant(record.get('started_at'), 'started_at', now=now)
    ended = record.get('ended_at')
    ended = parse_instant(ended, 'ended_at', now=now) if ended else None
    if ended is not None and ended < started:
        raise RecordError('ended_at is before started_at')

    beat = record.get('last_heartbeat_at')
    beat = parse_instant(beat, 'last_heartbeat_at', now=now) if beat else None

    # When the break the session is currently in began, if it is in one. Cleared
    # by the agent on return, so it goes back to NULL the same way it was set.
    idle_since = record.get('idle_since')
    idle_since = parse_instant(idle_since, 'idle_since', now=now) if idle_since else None

    values = {
        'user_id': user.id,
        'client_uuid': parse_uuid(record.get('client_uuid'), 'client_uuid'),
        'project': text_field(record.get('project'), 'project', MAX_PROJECT, required=True),
        'task': text_field(record.get('task'), 'task', MAX_TEXT),
        'started_at': started,
        'ended_at': ended,
        'last_heartbeat_at': beat,
        'idle_since': idle_since,
    }
    stmt = insert(Session).values(**values)
    # A session is uploaded once when it opens and again when it closes, so the
    # update path is the normal case, not an edge case. started_at is not
    # updated: when a session began is settled by the first upload.
    stmt = stmt.on_conflict_do_update(
        constraint='uq_sessions_client_uuid',
        set_={'project': stmt.excluded.project,
              'task': stmt.excluded.task,
              'ended_at': stmt.excluded.ended_at,
              'last_heartbeat_at': stmt.excluded.last_heartbeat_at,
              'idle_since': stmt.excluded.idle_since},
    )
    db.execute(stmt)


def _upsert_app_usage(db, user, record, now, session_ids):
    started, ended, duration = _span(record, now)
    ref = record.get('session_client_uuid')
    values = {
        'user_id': user.id,
        'client_uuid': parse_uuid(record.get('client_uuid'), 'client_uuid'),
        'session_id': session_ids.get(str(ref)) if ref else None,
        'app_name': text_field(record.get('app_name'), 'app_name', MAX_APP_NAME, required=True),
        'window_title': text_field(record.get('window_title'), 'window_title', MAX_TEXT),
        'started_at': started,
        'ended_at': ended,
        'duration_seconds': duration,
    }
    stmt = insert(AppUsage).values(**values)
    # Already-recorded usage never changes. Ignoring the conflict is what makes
    # a resend a no-op instead of a duplicated hour.
    db.execute(stmt.on_conflict_do_nothing(constraint='uq_app_usage_client_uuid'))


def _upsert_idle(db, user, record, now):
    started, ended, duration = _span(record, now)
    stmt = insert(IdlePeriod).values(
        user_id=user.id,
        client_uuid=parse_uuid(record.get('client_uuid'), 'client_uuid'),
        started_at=started, ended_at=ended, duration_seconds=duration)
    db.execute(stmt.on_conflict_do_nothing(constraint='uq_idle_client_uuid'))


# ── The batch ────────────────────────────────────────────────────────────────

def ingest_batch(db, user, payload, now=None):
    """Apply a batch. Returns {'accepted': {...}, 'rejected': [...]}.

    Sessions go first so app-usage rows in the same batch can resolve the
    session they belong to; a reference to a session not in this batch and not
    already stored is left null rather than rejecting the row, because the work
    happened either way and unattributed time still counts.
    """
    now = now or datetime.now(timezone.utc)
    accepted = {'sessions': 0, 'app_usage': 0, 'idle_periods': 0}
    rejected = []

    def run(kind, records, apply):
        if not isinstance(records, list):
            rejected.append({'kind': kind, 'index': None,
                             'error': f'{kind} must be a list'})
            return
        if len(records) > MAX_RECORDS_PER_KIND:
            rejected.append({'kind': kind, 'index': None,
                             'error': f'at most {MAX_RECORDS_PER_KIND} {kind} per batch'})
            return
        for i, record in enumerate(records):
            if not isinstance(record, dict):
                rejected.append({'kind': kind, 'index': i, 'error': 'record must be an object'})
                continue
            try:
                # A savepoint per record: one violation rolls back only itself,
                # so a single bad row cannot cost an agent a day of backlog.
                with db.begin_nested():
                    apply(record)
            except RecordError as e:
                rejected.append({'kind': kind, 'index': i, 'error': str(e)})
            except SQLAlchemyError as e:
                rejected.append({'kind': kind, 'index': i,
                                 'error': _explain(e)})
            else:
                accepted[kind] += 1

    run('sessions', payload.get('sessions', []),
        lambda r: _upsert_session(db, user, r, now))
    db.flush()

    # Map the client's session ids to ours, for this user only.
    session_ids = {}
    refs = {str(r.get('session_client_uuid')) for r in payload.get('app_usage', []) or []
            if isinstance(r, dict) and r.get('session_client_uuid')}
    if refs:
        valid = []
        for ref in refs:
            try:
                valid.append(uuid.UUID(ref))
            except (ValueError, TypeError):
                continue
        if valid:
            rows = (db.query(Session.client_uuid, Session.id)
                    .filter(Session.user_id == user.id, Session.client_uuid.in_(valid)).all())
            session_ids = {str(cu): sid for cu, sid in rows}

    run('app_usage', payload.get('app_usage', []),
        lambda r: _upsert_app_usage(db, user, r, now, session_ids))
    run('idle_periods', payload.get('idle_periods', []),
        lambda r: _upsert_idle(db, user, r, now))

    db.commit()
    return {'accepted': accepted, 'rejected': rejected}


def _explain(error):
    """Turn a database error into something an agent author can act on, without
    echoing SQL or table structure back over the network."""
    text = str(getattr(error, 'orig', error))
    if 'uq_sessions_one_open_per_user' in text:
        return 'this user already has an open session — close it before opening another'
    return 'record rejected by the database'
