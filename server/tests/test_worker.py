"""The background worker.

Its whole reason to exist is that the local app ran this work as threads inside
the web process. Four Gunicorn workers there would mean four weekly emails. So
the tests that matter are: does each job run on its own clock, does one failure
spare the others, and is running two workers at once merely wasteful rather
than wrong.
"""
from datetime import datetime, timedelta, timezone

import pytest

from app.worker import INTERVALS, JOBS, Worker

UTC = timezone.utc
T0 = datetime(2026, 8, 26, 9, 0, tzinfo=UTC)


class Counter:
    def __init__(self, explode=False):
        self.calls, self.explode = 0, explode

    def __call__(self, now=None):
        self.calls += 1
        if self.explode:
            raise RuntimeError('this job is broken')
        return self.calls


@pytest.fixture
def jobs():
    return {'reports': Counter(), 'orphans': Counter(), 'drafts': Counter(),
            'disk': Counter()}


def make(jobs, **intervals):
    # Every job in the fixture needs an interval, or due() raises KeyError on
    # the first tick — which is exactly what the real worker would do too.
    return Worker(intervals={'reports': 60, 'orphans': 300, 'drafts': 900,
                             'disk': 3600, **intervals}, jobs=jobs)


def test_every_job_runs_on_the_first_tick(jobs):
    """Asserted against the fixture rather than a written-out list, so adding a
    job cannot leave this passing while ignoring it."""
    assert set(make(jobs).tick(T0)) == set(jobs)
    assert all(job.calls == 1 for job in jobs.values())


def test_each_job_keeps_its_own_clock(jobs):
    worker = make(jobs)
    worker.tick(T0)
    worker.tick(T0 + timedelta(seconds=90))
    assert jobs['reports'].calls == 2        # every 60s
    assert jobs['orphans'].calls == 1        # every 300s
    assert jobs['drafts'].calls == 1         # every 900s


def test_a_job_does_not_run_before_its_interval(jobs):
    worker = make(jobs)
    worker.tick(T0)
    worker.tick(T0 + timedelta(seconds=30))
    assert jobs['reports'].calls == 1


def test_a_failing_job_does_not_stop_the_others(jobs):
    """A broken SMTP server must not also stop orphaned sessions being closed."""
    jobs['reports'] = Counter(explode=True)
    worker = make(jobs)
    worker.tick(T0)
    assert jobs['orphans'].calls == 1 and jobs['drafts'].calls == 1


def test_a_failing_job_backs_off_to_its_interval(jobs):
    """Otherwise a persistently broken job retries on every single tick."""
    jobs['reports'] = Counter(explode=True)
    worker = make(jobs)
    worker.tick(T0)
    worker.tick(T0 + timedelta(seconds=10))
    assert jobs['reports'].calls == 1


def test_two_workers_running_at_once_is_wasteful_not_wrong(jobs):
    """'Exactly one worker' is not a property anyone should maintain by hand.
    A forgotten container on another host must degrade to spent CPU, not to
    duplicate mail — which the send-once constraint is what guarantees."""
    first, second = make(jobs), make(dict(jobs))
    first.tick(T0)
    second.tick(T0)
    assert jobs['reports'].calls == 2        # both ran...
    # ...and reports.run_due is guarded by the unique constraint, proven in
    # test_report_send.py::test_a_report_is_sent_once.


def test_stopping_ends_the_loop(jobs):
    worker = make(jobs)
    worker._stop()
    assert worker._running is False


def test_alerts_run_after_orphans(jobs):
    """A session capped this tick should be reportable in the same tick rather
    than ten minutes later. tick() iterates the jobs dict, and dict order is
    insertion order, so this ordering is load-bearing rather than cosmetic."""
    names = list(JOBS)
    assert names.index('alerts') > names.index('orphans')


def test_every_job_has_an_interval():
    """A job present in JOBS but missing from INTERVALS raises KeyError inside
    due() on the first tick, taking the whole worker down at startup."""
    assert set(JOBS) == set(INTERVALS)
