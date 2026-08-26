import os
import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.config import DATABASE_URL
from app.models import Base, User, UserSettings


@pytest.fixture(scope='session')
def engine():
    """Tests run against a scratch database, created and dropped around the run.

    Never the development database: a test that truncates tables must not be one
    typo away from wiping real data.
    """
    admin_url = DATABASE_URL.rsplit('/', 1)[0] + '/postgres'
    name = 'ttcloud_test'
    admin = create_engine(admin_url, isolation_level='AUTOCOMMIT', future=True)
    with admin.connect() as c:
        c.execute(text(f'DROP DATABASE IF EXISTS {name} WITH (FORCE)'))
        c.execute(text(f'CREATE DATABASE {name}'))
    test_url = DATABASE_URL.rsplit('/', 1)[0] + f'/{name}'
    eng = create_engine(test_url, future=True)
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()
    with admin.connect() as c:
        c.execute(text(f'DROP DATABASE IF EXISTS {name} WITH (FORCE)'))
    admin.dispose()


@pytest.fixture(scope='session', autouse=True)
def _bind_app_session(engine):
    """Point the application's scoped session at the scratch database.

    Without this the Flask app would keep talking to the development database
    while the fixtures talk to the test one — tests would pass against real data
    and truncate nothing they expected to.
    """
    import app.db as appdb
    appdb.db_session.remove()
    appdb.db_session.configure(bind=engine)
    yield
    appdb.db_session.remove()


@pytest.fixture
def db(engine):
    Session = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    s = Session()
    yield s
    s.rollback()
    s.close()
    # The app's session may still hold a transaction open; TRUNCATE would block
    # on it.
    import app.db as appdb
    appdb.db_session.remove()
    # Each test starts from an empty schema so ordering never matters.
    with engine.begin() as c:
        for table in reversed(Base.metadata.sorted_tables):
            c.execute(text(f'TRUNCATE TABLE {table.name} CASCADE'))


@pytest.fixture
def make_user(db):
    def _make(email=None, role='worker', tz='Africa/Nairobi'):
        u = User(email=email or f'{uuid.uuid4().hex[:8]}@example.com',
                 name='Test Person', password_hash='x', role=role)
        u.settings = UserSettings(timezone=tz)
        db.add(u)
        db.commit()
        return u
    return _make


@pytest.fixture
def flask_app(tmp_path, monkeypatch):
    """A test app with its OWN media root.

    Without this the suite writes fake images into the development store — they
    turn up in the real gallery and are indistinguishable from captures until
    something tries to open one.
    """
    monkeypatch.setenv('MEDIA_ROOT', str(tmp_path / 'media'))
    monkeypatch.delenv('S3_BUCKET', raising=False)

    from app import create_app
    application = create_app(TESTING=True, WTF_CSRF_ENABLED=False,
                             SECRET_KEY='test-key-not-a-secret')
    with application.app_context():
        yield application


@pytest.fixture
def client(flask_app):
    return flask_app.test_client()


@pytest.fixture
def password():
    return 'a-perfectly-fine-password'


@pytest.fixture
def make_login_user(db, password):
    """A user created through the real service, so the password is hashed the
    way production hashes it rather than stubbed."""
    from app.services.users import create_user

    def _make(email='worker@example.com', role='worker', active=True):
        u = create_user(db, email, 'Test Person', password, role=role)
        if not active:
            u.is_active = False
            db.commit()
        return u
    return _make
