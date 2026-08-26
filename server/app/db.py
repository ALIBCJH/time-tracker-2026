"""Engine and session factory.

The local app cached one sqlite3 connection per thread in a threading.local().
That works for a single process owning a single file and breaks the moment
there is a pool in front of a real server, so the connection lifecycle here is
explicit: one Session per unit of work, closed when it ends.
"""
from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.orm import scoped_session, sessionmaker

from app.config import DATABASE_URL

engine = create_engine(DATABASE_URL, pool_pre_ping=True, future=True)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, future=True)


@contextmanager
def session_scope():
    """A transactional scope. Commits on success, rolls back on any exception."""
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


# The web app uses a scoped session — one per request, removed on teardown by
# create_app(). Background workers use session_scope() above instead, because
# they have no request to scope to.
db_session = scoped_session(SessionLocal)
