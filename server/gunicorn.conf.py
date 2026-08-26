"""Gunicorn settings.

Sync workers, not async. Every request here is a short database query; gevent
would add a failure mode to debug in exchange for concurrency this workload
does not need.

Nothing scheduled runs in these processes — see app/worker.py. That separation
is the reason worker count can be changed freely without changing how many
emails anyone receives.
"""
import multiprocessing
import os

bind = f"0.0.0.0:{os.environ.get('PORT', 8000)}"
workers = int(os.environ.get('WEB_CONCURRENCY', min(4, multiprocessing.cpu_count() * 2 + 1)))
worker_class = 'sync'

# Long enough for a 12MB screenshot upload on a poor connection, short enough
# that a genuinely stuck worker is recycled rather than held for ever.
timeout = 120
graceful_timeout = 30
# Slightly above a typical 60s proxy keep-alive, so the connection is closed by
# the proxy rather than by us mid-response.
keepalive = 65

# Recycled periodically with jitter, so a slow leak cannot accumulate and the
# whole pool does not restart in lockstep.
max_requests = 1000
max_requests_jitter = 100

accesslog = '-'
errorlog = '-'
loglevel = os.environ.get('LOG_LEVEL', 'info')
# Log the forwarded address, not the proxy's.
access_log_format = '%({X-Forwarded-For}i)s %(m)s %(U)s %(s)s %(L)ss'

preload_app = True
