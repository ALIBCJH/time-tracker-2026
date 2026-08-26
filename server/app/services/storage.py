"""Where screen captures live.

Two backends behind one interface. Local disk for development, S3 in
production — and the abstraction is not architecture for its own sake: it means
the upload path, the retention rules and the gallery are all exercised by the
test suite without an AWS account, which is the only way they get tested at all.

Nothing here ever serves bytes through the application. A capture is fetched by
a short-lived signed URL, so the image is not sitting behind a
long-lived path that could be shared, guessed, or left in someone's history.
"""
import hmac
import logging
import mimetypes
import os
import shutil
import time
from datetime import datetime, timedelta, timezone
from hashlib import sha256

logger = logging.getLogger('storage')

# Full-resolution captures expire in weeks; thumbnails live a year. That
# asymmetry is the whole retention design: the visual timeline survives long
# after the evidence does, so an old month can still be reviewed at a glance
# without the system hoarding readable screenshots of anyone's banking tab.
FULL_RETENTION_DAYS = 30
THUMB_RETENTION_DAYS = 365

SIGNED_URL_SECONDS = 300


def key_for(user_id, captured_at, client_uuid, kind):
    """s3://bucket/<user>/<date>/<time>-<id>-<kind>.webp

    Partitioned by user first and day second, so a person's data can be
    enumerated (and deleted) as a prefix — which is what makes an erasure
    request a bounded operation rather than a scan.
    """
    day = captured_at.astimezone(timezone.utc).strftime('%Y-%m-%d')
    stamp = captured_at.astimezone(timezone.utc).strftime('%H%M%S')
    return f'{user_id}/{day}/{stamp}-{client_uuid}-{kind}.webp'


class LocalStorage:
    """Development backend. Signs URLs with the app secret so the gallery code
    path is identical to production's — an unsigned local mode would leave the
    only interesting part of the flow untested."""

    name = 'local'

    def __init__(self, root, secret):
        self.root = os.path.abspath(root)
        self.secret = secret.encode() if isinstance(secret, str) else secret
        os.makedirs(self.root, exist_ok=True)

    def _path(self, key):
        path = os.path.abspath(os.path.join(self.root, key))
        # A key is attacker-influenced (it carries ids). Confine it to the root
        # so '../' can never walk out of the store.
        if not path.startswith(self.root + os.sep):
            raise ValueError('key escapes the storage root')
        return path

    def put(self, key, data, content_type='image/webp'):
        path = self._path(key)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'wb') as f:
            f.write(data)
        return key

    def get(self, key):
        with open(self._path(key), 'rb') as f:
            return f.read()

    def exists(self, key):
        return os.path.exists(self._path(key))

    def delete(self, key):
        try:
            os.remove(self._path(key))
            return True
        except FileNotFoundError:
            return False

    def signed_url(self, key, seconds=SIGNED_URL_SECONDS):
        expires = int(time.time()) + seconds
        return f'/media/{key}?expires={expires}&sig={self.sign(key, expires)}'

    def sign(self, key, expires):
        return hmac.new(self.secret, f'{key}:{expires}'.encode(), sha256).hexdigest()[:32]

    def verify(self, key, expires, signature):
        try:
            expires = int(expires)
        except (TypeError, ValueError):
            return False
        if expires < time.time():
            return False
        return hmac.compare_digest(self.sign(key, expires), signature or '')

    def purge_user(self, user_id):
        directory = self._path(str(user_id))
        if os.path.isdir(directory):
            shutil.rmtree(directory)


class S3Storage:
    """Production backend. The bucket is private with Block Public Access on;
    every read is a presigned GET."""

    name = 's3'

    def __init__(self, bucket, client=None, prefix=''):
        import boto3                                    # optional dependency
        self.bucket = bucket
        self.prefix = prefix.strip('/')
        self.client = client or boto3.client('s3')

    def _key(self, key):
        return f'{self.prefix}/{key}' if self.prefix else key

    def put(self, key, data, content_type='image/webp'):
        # Tagged on upload so the lifecycle rules can expire full captures and
        # thumbnails on different clocks. Tagging beats separate prefixes: the
        # gallery keeps one predictable key shape.
        kind = 'thumb' if key.endswith('-thumb.webp') else 'full'
        self.client.put_object(Bucket=self.bucket, Key=self._key(key), Body=data,
                               ContentType=content_type, Tagging=f'kind={kind}')
        return key

    def get(self, key):
        return self.client.get_object(Bucket=self.bucket,
                                      Key=self._key(key))['Body'].read()

    def exists(self, key):
        from botocore.exceptions import ClientError
        try:
            self.client.head_object(Bucket=self.bucket, Key=self._key(key))
            return True
        except ClientError:
            return False

    def delete(self, key):
        self.client.delete_object(Bucket=self.bucket, Key=self._key(key))
        return True

    def signed_url(self, key, seconds=SIGNED_URL_SECONDS):
        return self.client.generate_presigned_url(
            'get_object', Params={'Bucket': self.bucket, 'Key': self._key(key)},
            ExpiresIn=seconds)

    def purge_user(self, user_id):
        paginator = self.client.get_paginator('list_objects_v2')
        prefix = self._key(f'{user_id}/')
        for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
            keys = [{'Key': o['Key']} for o in page.get('Contents', [])]
            if keys:
                self.client.delete_objects(Bucket=self.bucket, Delete={'Objects': keys})


def lifecycle_rules(prefix=''):
    """The bucket policy that does the expiring.

    S3 deletes the bytes on its own clock. A cron job doing the same work is one
    more thing to notice has stopped — and a retention policy that silently
    stops running is worse than not having one, because everyone assumes it is.
    """
    return {'Rules': [
        {'ID': 'expire-full-captures', 'Status': 'Enabled',
         'Filter': {'And': {'Prefix': prefix,
                            'Tags': [{'Key': 'kind', 'Value': 'full'}]}},
         'Expiration': {'Days': FULL_RETENTION_DAYS}},
        {'ID': 'expire-thumbnails', 'Status': 'Enabled',
         'Filter': {'And': {'Prefix': prefix,
                            'Tags': [{'Key': 'kind', 'Value': 'thumb'}]}},
         'Expiration': {'Days': THUMB_RETENTION_DAYS}},
    ]}


def build(config=None):
    """The backend this deployment uses, from the environment."""
    config = config or os.environ
    bucket = config.get('S3_BUCKET')
    if bucket:
        return S3Storage(bucket, prefix=config.get('S3_PREFIX', ''))
    root = config.get('MEDIA_ROOT') or os.path.join(os.getcwd(), 'var', 'media')
    return LocalStorage(root, config.get('SECRET_KEY', 'dev-only'))
