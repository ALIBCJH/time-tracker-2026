"""Rendering and sending.

The send loop is where the local app's worst bug lived — a missing state file
turned "I have never sent anything" into "I owe you everything" and mailed a
real person a backdated report. Most of this file is that scenario.
"""
import uuid
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from app.models import ActivityLog, ReportSend, Session
from app.reports import data as D
from app.reports import render as RR
from app.reports import schedule as S
from app.reports import send as SEND
from app.services.users import create_user

UTC = timezone.utc
NBO = ZoneInfo('Africa/Nairobi')
MONDAY = date(2026, 8, 17)
SEND_TIME = datetime(2026, 8, 24, 17, 0, tzinfo=NBO)


class Outbox:
    """A stand-in for SMTP that records instead of sending."""

    def __init__(self, fail=False):
        self.sent, self.fail = [], fail

    def __call__(self, to, subject, html, images=None, cc=(), settings=None):
        if self.fail:
            raise RuntimeError('smtp is down')
        self.sent.append({'to': to, 'cc': list(cc), 'subject': subject,
                          'html': html, 'images': images or {}})
        return [to] + list(cc)


@pytest.fixture
def user(db, password):
    return create_user(db, 'a@example.com', 'A', password,
                       timezone_name='Africa/Nairobi')


@pytest.fixture
def outbox(monkeypatch):
    box = Outbox()
    monkeypatch.setattr('app.services.mail.send', box)
    return box


def work(db, user, day, hours, project='Alpha'):
    start = datetime.combine(day, datetime.min.time(), tzinfo=NBO) + timedelta(hours=9)
    db.add(Session(user_id=user.id, client_uuid=uuid.uuid4(), project=project,
                   started_at=start, ended_at=start + timedelta(hours=hours)))
    db.commit()


def full_week(db, user):
    for offset in range(5):
        work(db, user, MONDAY + timedelta(days=offset), 6)


# ── Rendering ────────────────────────────────────────────────────────────────

def test_a_weekly_report_renders(flask_app, db, user):
    full_week(db, user)
    payload = D.weekly(db, user, MONDAY, now=SEND_TIME)
    subject, html = RR.render_weekly(payload, now=SEND_TIME)
    assert subject == 'Weekly Time Report — Week of August 17, 2026'
    assert '30h 00m' in html and 'Africa/Nairobi' in html


def test_charts_are_embedded_as_parts_for_mail(flask_app, db, user):
    """Gmail strips <svg> and runs no JS; a chart that does not survive the
    mail client is not a chart."""
    full_week(db, user)
    images = {}
    payload = D.weekly(db, user, MONDAY, now=SEND_TIME)
    _, html = RR.render_weekly(payload, now=SEND_TIME,
                               embed=RR.cid_embedder(images))
    assert 'cid:daily' in html and images['daily'][:4] == b'\x89PNG'


def test_a_preview_embeds_the_same_charts_as_data_uris(flask_app, db, user):
    """Same rendering, different reference — so the preview cannot drift away
    from what actually arrives."""
    full_week(db, user)
    payload = D.weekly(db, user, MONDAY, now=SEND_TIME)
    _, html = RR.render_weekly(payload, now=SEND_TIME, embed=RR.data_uri_embedder())
    assert 'data:image/png;base64,' in html and 'cid:' not in html


def test_a_report_renders_without_charts(flask_app, db, user):
    """A missing font must cost a picture, not the report."""
    full_week(db, user)
    payload = D.weekly(db, user, MONDAY, now=SEND_TIME)
    _, html = RR.render_weekly(payload, now=SEND_TIME, embed=None)
    assert '30h 00m' in html and '<img' not in html


def test_a_monthly_report_leads_with_the_percentage(flask_app, db, user):
    work(db, user, date(2026, 7, 10), 10)
    work(db, user, date(2026, 8, 10), 11)
    payload = D.monthly(db, user, 2026, 8, now=datetime(2026, 9, 1, 12, tzinfo=NBO))
    subject, html = RR.render_monthly(payload, now=datetime(2026, 9, 1, 12, tzinfo=NBO))
    assert subject == 'Monthly Time Report — August 2026'
    assert '10.0%' in html and 'vs July' in html


def test_a_first_month_says_so_rather_than_inventing_a_number(flask_app, db, user):
    work(db, user, date(2026, 8, 10), 5)
    payload = D.monthly(db, user, 2026, 8, now=datetime(2026, 9, 1, 12, tzinfo=NBO))
    _, html = RR.render_monthly(payload, now=datetime(2026, 9, 1, 12, tzinfo=NBO))
    assert 'First month tracked' in html


def test_a_month_sent_on_its_last_day_says_what_it_measured(flask_app, db, user):
    """It is not strictly over, and implying a whole month would be a lie."""
    work(db, user, date(2026, 8, 10), 5)
    at_send = datetime(2026, 8, 31, 21, 0, tzinfo=NBO)
    payload = D.monthly(db, user, 2026, 8, now=at_send)
    _, html = RR.render_monthly(payload, now=at_send)
    assert 'sent before the final hours of the month closed' in html


def test_a_month_sent_late_reports_the_full_window(flask_app, db, user):
    work(db, user, date(2026, 8, 10), 5)
    later = datetime(2026, 9, 3, 10, tzinfo=NBO)
    payload = D.monthly(db, user, 2026, 8, now=later)
    _, html = RR.render_monthly(payload, now=later)
    assert '23:59' in html and 'final hours' not in html


def test_private_labels_never_appear_in_the_email(flask_app, db, user):
    """The time still counts; it just does not get named in a report someone
    else reads."""
    user.settings.private_labels = ['whatsapp']
    db.commit()
    full_week(db, user)
    db.add(ActivityLog(user_id=user.id, log_date=MONDAY, status='confirmed',
                       headline='x', tracked_seconds=3600, created_at=datetime.now(UTC),
                       activities=[{'label': 'WhatsApp', 'category': 'Web', 'seconds': 1800},
                                   {'label': 'Real Work', 'category': 'Coding', 'seconds': 1800}]))
    db.commit()
    payload = D.weekly(db, user, MONDAY, now=SEND_TIME)
    _, html = RR.render_weekly(payload, now=SEND_TIME)
    assert 'WhatsApp' not in html
    assert 'Real Work' in html and 'Personal &amp; breaks' in html


# ── Sending ──────────────────────────────────────────────────────────────────

def test_a_report_is_sent_once(flask_app, db, user, outbox):
    full_week(db, user)
    S.seed(db, user, 'weekly', 'earlier')
    assert SEND.send_report(db, user, 'weekly', '2026-W34', (MONDAY,),
                            now=SEND_TIME) == 'sent'
    assert SEND.send_report(db, user, 'weekly', '2026-W34', (MONDAY,),
                            now=SEND_TIME) == 'already-sent'
    assert len(outbox.sent) == 1


def test_the_cc_list_receives_a_copy(flask_app, db, user, outbox):
    """How an admin gets a copy of a worker's week."""
    user.settings.report_cc = ['boss@example.com']
    db.commit()
    full_week(db, user)
    SEND.send_report(db, user, 'weekly', '2026-W34', (MONDAY,), now=SEND_TIME)
    assert outbox.sent[0]['to'] == 'a@example.com'
    assert outbox.sent[0]['cc'] == ['boss@example.com']


def test_a_failed_send_gives_the_claim_back(flask_app, db, user, monkeypatch):
    """Otherwise one bad SMTP minute becomes a permanently missing report."""
    monkeypatch.setattr('app.services.mail.send', Outbox(fail=True))
    full_week(db, user)
    assert SEND.send_report(db, user, 'weekly', '2026-W34', (MONDAY,),
                            now=SEND_TIME) == 'failed'
    assert db.query(ReportSend).count() == 0


def test_a_retry_after_a_failure_succeeds(flask_app, db, user, monkeypatch):
    monkeypatch.setattr('app.services.mail.send', Outbox(fail=True))
    full_week(db, user)
    SEND.send_report(db, user, 'weekly', '2026-W34', (MONDAY,), now=SEND_TIME)

    box = Outbox()
    monkeypatch.setattr('app.services.mail.send', box)
    assert SEND.send_report(db, user, 'weekly', '2026-W34', (MONDAY,),
                            now=SEND_TIME) == 'sent'
    assert len(box.sent) == 1


# ── The loop ─────────────────────────────────────────────────────────────────

def test_a_new_account_is_seeded_not_mailed(flask_app, db, user, outbox):
    """THE bug. A fresh install, or a restored database, must not read "I have
    never sent anything" as "I owe you everything"."""
    full_week(db, user)
    results = SEND.run_due(db, [user], now=SEND_TIME)
    assert ('a@example.com', 'weekly', '2026-W34', 'seeded') in results
    assert outbox.sent == []


def test_the_next_period_after_seeding_is_really_sent(flask_app, db, user, outbox):
    full_week(db, user)
    SEND.run_due(db, [user], now=SEND_TIME)              # seeds
    next_monday = SEND_TIME + timedelta(days=7)
    results = SEND.run_due(db, [user], now=next_monday)
    assert any(r[3] == 'sent' for r in results)
    assert len(outbox.sent) >= 1


def test_running_the_loop_repeatedly_sends_nothing_extra(flask_app, db, user, outbox):
    """It is safe to run every minute."""
    full_week(db, user)
    SEND.run_due(db, [user], now=SEND_TIME)
    next_monday = SEND_TIME + timedelta(days=7)
    for _ in range(5):
        SEND.run_due(db, [user], now=next_monday)
    weekly = [s for s in outbox.sent if s['subject'].startswith('Weekly')]
    assert len(weekly) == 1


def test_a_disabled_account_gets_nothing(flask_app, db, user, outbox):
    user.is_active = False
    db.commit()
    assert SEND.run_due(db, [user], now=SEND_TIME) == []


def test_reports_can_be_switched_off_per_person(flask_app, db, user, outbox):
    user.settings.reports_enabled = False
    db.commit()
    assert SEND.run_due(db, [user], now=SEND_TIME) == []


def test_nothing_fires_outside_the_send_window(flask_app, db, user, outbox):
    tuesday = datetime(2026, 8, 25, 17, tzinfo=NBO)
    # Seed the periods actually owed on that day, so nothing is outstanding.
    S.seed(db, user, 'weekly', S.weekly_key(date(2026, 8, 17)))
    S.seed(db, user, 'monthly', '2026-07')
    assert SEND.run_due(db, [user], now=tuesday) == []


def test_a_month_missed_at_its_end_goes_out_on_a_later_day(flask_app, db, user, outbox):
    """The catch-up branch, which is deliberate: the machine may have been off
    on the 31st, and a month with no report at all is the worse failure."""
    S.seed(db, user, 'monthly', '2026-06')
    work(db, user, date(2026, 7, 10), 5)
    results = SEND.run_due(db, [user], now=datetime(2026, 8, 25, 17, tzinfo=NBO))
    assert ('a@example.com', 'monthly', '2026-07', 'sent') in results


def test_each_person_is_handled_separately(flask_app, db, user, password, outbox):
    other = create_user(db, 'b@example.com', 'B', password,
                        timezone_name='Africa/Nairobi')
    S.seed(db, user, 'weekly', 'earlier')
    S.seed(db, other, 'weekly', 'earlier')
    full_week(db, user)
    full_week(db, other)
    SEND.run_due(db, [user, other], now=SEND_TIME)
    assert sorted(s['to'] for s in outbox.sent) == ['a@example.com', 'b@example.com']
