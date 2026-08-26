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


@pytest.fixture
def db(engine):
    Session = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    s = Session()
    yield s
    s.rollback()
    s.close()
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
