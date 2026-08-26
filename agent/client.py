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

    def sync(self, batch):
        return self._request('POST', '/api/agent/sync', batch)
