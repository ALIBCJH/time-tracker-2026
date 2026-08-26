"""Captures: storage, upload, retention, and who can see them.

The last one matters most. This is the most sensitive data the system holds —
pictures of somebody's screen, taken on a timer — so the tests that count are
the ones about access, expiry and consent rather than about pixels.
"""
import io
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from app.models import Screenshot, Session
from app.services import storage as ST
from app.services.users import create_user, issue_device_token

UTC = timezone.utc
NBO = ZoneInfo('Africa/Nairobi')
WHEN = datetime(2026, 8, 26, 11, 30, tzinfo=UTC)
IMAGE = b'RIFF----WEBPVP8 fake-but-plausible-bytes'


@pytest.fixture
def user(db, password):
    return create_user(db, 'a@example.com', 'A', password,
                       timezone_name='Africa/Nairobi')


@pytest.fixture
def store(flask_app):
    return flask_app.storage


@pytest.fixture
def agent(client, db, user):
    _, token = issue_device_token(db, user, 'laptop')
    auth = {'Authorization': f'Bearer {token}'}

    def upload(client_uuid=None, captured_at=None, thumb=True, session=None,
               data=IMAGE):
        payload = {
            'client_uuid': str(client_uuid or uuid.uuid4()),
            'captured_at': (captured_at or WHEN).isoformat(),
            'full': (io.BytesIO(data), 'full.webp'),
        }
        if thumb:
            payload['thumb'] = (io.BytesIO(b'thumb'), 'thumb.webp')
        if session:
            payload['session_client_uuid'] = str(session)
        return client.post('/api/agent/screenshot', data=payload,
                           content_type='multipart/form-data', headers=auth)
    return upload


# ── Keys and signatures ──────────────────────────────────────────────────────

def test_keys_are_partitioned_by_user_then_day():
    """So one person's captures are a prefix — which is what makes an erasure
    request a bounded delete rather than a scan."""
    key = ST.key_for('user-1', WHEN, 'abc', 'full')
    assert key.startswith('user-1/2026-08-26/')
    assert key.endswith('-abc-full.webp')


def test_a_signed_url_verifies(store):
    key = ST.key_for('u', WHEN, 'abc', 'full')
    expires = int(time.time()) + 300
    assert store.verify(key, expires, store.sign(key, expires))


def test_a_tampered_signature_is_refused(store):
    key = ST.key_for('u', WHEN, 'abc', 'full')
    expires = int(time.time()) + 300
    assert not store.verify(key, expires, 'x' * 32)


def test_an_expired_url_is_refused(store):
    """A link that leaks is worth nothing minutes later."""
    key = ST.key_for('u', WHEN, 'abc', 'full')
    past = int(time.time()) - 10
    assert not store.verify(key, past, store.sign(key, past))


def test_a_signature_does_not_transfer_to_another_key(store):
    """Otherwise one shared link opens the whole store."""
    expires = int(time.time()) + 300
    signature = store.sign('a/b/c-full.webp', expires)
    assert not store.verify('a/b/other-full.webp', expires, signature)


def test_a_key_cannot_escape_the_store(store):
    with pytest.raises(ValueError):
        store.put('../../etc/passwd', b'x')


# ── Retention ────────────────────────────────────────────────────────────────

def test_full_frames_and_thumbnails_expire_on_different_clocks():
    """The timeline outlives the evidence: an old month can still be reviewed
    at a glance without the system hoarding readable screenshots."""
    rules = {r['ID']: r for r in ST.lifecycle_rules()['Rules']}
    assert rules['expire-full-captures']['Expiration']['Days'] == 30
    assert rules['expire-thumbnails']['Expiration']['Days'] == 365
    assert (rules['expire-thumbnails']['Expiration']['Days']
            > rules['expire-full-captures']['Expiration']['Days'])


def test_the_rules_select_by_tag():
    """Tagging beats separate prefixes — the gallery keeps one key shape."""
    for rule in ST.lifecycle_rules()['Rules']:
        tags = rule['Filter']['And']['Tags']
        assert tags and tags[0]['Key'] == 'kind'


# ── Upload ───────────────────────────────────────────────────────────────────

def test_a_capture_is_stored_and_indexed(db, user, agent, store):
    response = agent()
    assert response.status_code == 201
    shot = db.query(Screenshot).one()
    assert store.exists(shot.full_key) and store.exists(shot.thumb_key)
    assert shot.bytes_full == len(IMAGE)


def test_a_retried_upload_does_not_store_twice(db, user, agent, store):
    """A lost response must not leave an orphan object nothing points at."""
    same = uuid.uuid4()
    agent(client_uuid=same)
    second = agent(client_uuid=same)
    assert second.get_json()['duplicate'] is True
    assert db.query(Screenshot).count() == 1


def test_a_capture_is_linked_to_its_session(db, user, agent):
    session = Session(user_id=user.id, client_uuid=uuid.uuid4(), project='Alpha',
                      started_at=WHEN - timedelta(hours=1))
    db.add(session)
    db.commit()
    agent(session=session.client_uuid)
    assert db.query(Screenshot).one().session_id == session.id


def test_a_session_reference_cannot_reach_another_users(db, user, agent, password):
    other = create_user(db, 'b@example.com', 'B', password)
    theirs = Session(user_id=other.id, client_uuid=uuid.uuid4(), project='Theirs',
                     started_at=WHEN, ended_at=WHEN)
    db.add(theirs)
    db.commit()
    agent(session=theirs.client_uuid)
    assert db.query(Screenshot).one().session_id is None


def test_an_upload_without_an_image_is_refused(client, db, user):
    _, token = issue_device_token(db, user, 'laptop')
    r = client.post('/api/agent/screenshot',
                    data={'client_uuid': str(uuid.uuid4()),
                          'captured_at': WHEN.isoformat()},
                    content_type='multipart/form-data',
                    headers={'Authorization': f'Bearer {token}'})
    assert r.status_code == 400


def test_a_naive_timestamp_is_refused(db, user, client):
    _, token = issue_device_token(db, user, 'laptop')
    r = client.post('/api/agent/screenshot',
                    data={'client_uuid': str(uuid.uuid4()),
                          'captured_at': '2026-08-26T11:30:00',
                          'full': (io.BytesIO(IMAGE), 'f.webp')},
                    content_type='multipart/form-data',
                    headers={'Authorization': f'Bearer {token}'})
    assert r.status_code == 400


def test_uploading_needs_a_token(client):
    assert client.post('/api/agent/screenshot').status_code == 401


# ── Consent ──────────────────────────────────────────────────────────────────

def test_captures_are_refused_when_the_user_has_them_off(db, user, agent):
    """Refused, not silently dropped: an agent whose captures are being
    discarded should know, and stop taking them."""
    user.settings.screenshots_enabled = False
    db.commit()
    assert agent().status_code == 409
    assert db.query(Screenshot).count() == 0


# ── Seeing them ──────────────────────────────────────────────────────────────

def sign_in(client, email, password):
    client.post('/login', data={'email': email, 'password': password})


def test_the_gallery_shows_the_days_captures(client, db, user, agent, password):
    agent()
    sign_in(client, 'a@example.com', password)
    body = client.get('/screenshots?date=2026-08-26').data
    assert b'/media/' in body and b'14:30' in body      # 11:30 UTC in Nairobi


def test_a_capture_can_be_fetched_with_its_signed_url(client, db, user, agent, password):
    agent()
    sign_in(client, 'a@example.com', password)
    import re
    page = client.get('/screenshots?date=2026-08-26').data.decode()
    url = re.search(r'src="(/media/[^"]+)"', page).group(1).replace('&amp;', '&')
    assert client.get(url).status_code == 200


def test_an_unsigned_media_url_is_refused(client, db, user, agent):
    agent()
    key = db.query(Screenshot).one().full_key
    assert client.get(f'/media/{key}').status_code == 403


def test_a_worker_cannot_open_anothers_gallery(client, db, user, agent, password):
    """The most sensitive data in the system; the scoping rule is the same as
    everywhere else, and it is tested here because here it matters most."""
    other = create_user(db, 'b@example.com', 'B', password)
    agent()
    sign_in(client, 'b@example.com', password)
    body = client.get(f'/screenshots?date=2026-08-26&user={user.id}').data
    assert b'/media/' not in body


def test_an_admin_can(client, db, user, agent, password):
    create_user(db, 'boss@example.com', 'Boss', password, role='admin')
    agent()
    sign_in(client, 'boss@example.com', password)
    body = client.get(f'/screenshots?date=2026-08-26&user={user.id}').data
    assert b'/media/' in body


def test_an_expired_full_frame_is_labelled_not_broken(client, db, user, agent,
                                                      password, store):
    """The difference between "gone on purpose" and "lost"."""
    agent()
    shot = db.query(Screenshot).one()
    store.delete(shot.full_key)                # as the lifecycle rule would
    sign_in(client, 'a@example.com', password)
    body = client.get('/screenshots?date=2026-08-26').data
    assert b'full frame expired' in body


def test_deleting_a_person_removes_their_images(db, user, agent, store):
    """Erasure has to reach the object store, not just the rows."""
    agent()
    key = db.query(Screenshot).one().full_key
    store.purge_user(user.id)
    assert not store.exists(key)
