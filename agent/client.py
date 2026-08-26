"""HTTP to the server. Stdlib only.

Errors are split into two kinds, because the spool must treat them differently:

  * Transient — no network, DNS failure, 5xx, timeout. The batch is untouched
    and retried later. This is the normal state on a laptop.
  * Permanent — 401/403. The token is wrong or revoked; retrying will never
    help and the agent should say so rather than hammer the server.
"""
import json
import urllib.error
import urllib.request

TIMEOUT = 20


class TransientError(Exception):
    """Try again later — the work is still in the spool."""


class AuthError(Exception):
    """The token is not valid. Retrying will not fix it."""


class CapturesDisabled(Exception):
    """The account has screenshots switched off. Stop taking them."""


class Client:
    def __init__(self, server, token, timeout=TIMEOUT):
        self.server = server.rstrip('/')
        self.token = token
        self.timeout = timeout

    def _request(self, method, path, payload=None):
        url = f'{self.server}{path}'
        body = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(url, data=body, method=method, headers={
            'Authorization': f'Bearer {self.token}',
            'Content-Type': 'application/json',
            'Accept': 'application/json',
        })
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return json.loads(r.read() or b'{}')
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                raise AuthError(f'server rejected the agent token ({e.code})')
            if e.code >= 500:
                raise TransientError(f'server error {e.code}')
            # A 4xx that is not auth means this request is malformed. Surface it
            # rather than retrying a request that will always be refused.
            raise TransientError(f'request refused ({e.code})')
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
            raise TransientError(f'cannot reach {self.server}: {e}')
        except json.JSONDecodeError:
            raise TransientError('server sent a response that was not JSON')

    def heartbeat(self):
        return self._request('POST', '/api/agent/heartbeat', {})

    def me(self):
        return self._request('GET', '/api/agent/me')

    def activity_log_pending(self, idle_seconds=None, tracking=True):
        query = f'?idle_seconds={idle_seconds if idle_seconds is not None else ""}'
        query += f'&tracking={"true" if tracking else "false"}'
        return self._request('GET', '/api/agent/activity-log/pending' + query)

    def answer_activity_log(self, date, note='', status='confirmed'):
        return self._request('POST', '/api/agent/activity-log/answer',
                             {'date': date, 'note': note, 'status': status})

    def sync(self, batch):
        return self._request('POST', '/api/agent/sync', batch)

    def upload_screenshot(self, client_uuid, captured_at, full_path,
                          thumb_path=None, session_client_uuid=None):
        """One capture, as multipart. Hand-rolled rather than pulled from a
        library: the agent installs on other people's machines and every
        dependency is one more thing that can fail to install there."""
        import mimetypes
        import uuid as _uuid

        fields = {'client_uuid': client_uuid, 'captured_at': captured_at}
        if session_client_uuid:
            fields['session_client_uuid'] = session_client_uuid

        files = [('full', full_path)]
        if thumb_path:
            files.append(('thumb', thumb_path))

        boundary = '----ttcloud' + _uuid.uuid4().hex
        body = bytearray()
        for name, value in fields.items():
            body += (f'--{boundary}\r\nContent-Disposition: form-data; '
                     f'name="{name}"\r\n\r\n{value}\r\n').encode()
        for name, path in files:
            with open(path, 'rb') as f:
                data = f.read()
            filename = path.rsplit('/', 1)[-1]
            content_type = mimetypes.guess_type(path)[0] or 'image/webp'
            body += (f'--{boundary}\r\nContent-Disposition: form-data; '
                     f'name="{name}"; filename="{filename}"\r\n'
                     f'Content-Type: {content_type}\r\n\r\n').encode()
            body += data + b'\r\n'
        body += f'--{boundary}--\r\n'.encode()

        req = urllib.request.Request(
            f'{self.server}/api/agent/screenshot', data=bytes(body), method='POST',
            headers={'Authorization': f'Bearer {self.token}',
                     'Content-Type': f'multipart/form-data; boundary={boundary}'})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout * 3) as r:
                return json.loads(r.read() or b'{}')
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                raise AuthError(f'server rejected the agent token ({e.code})')
            if e.code == 409:
                # Captures are switched off for this account. Not an error to
                # retry — the agent should stop taking them.
                raise CapturesDisabled('screenshots are disabled for this account')
            if e.code >= 500:
                raise TransientError(f'server error {e.code}')
            raise TransientError(f'upload refused ({e.code})')
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
            raise TransientError(f'cannot reach {self.server}: {e}')
