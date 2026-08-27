"""Operator commands. Run with:  ./.venv/bin/flask --app app <command>

Account creation is deliberately CLI-only for now. There is no public sign-up:
this is a three-person deployment, and an open registration form on a server
holding screen captures is a liability nobody asked for.
"""
import click
from flask.cli import with_appcontext

from app.auth.passwords import WeakPassword
from app.db import db_session
from app.models import Device, User
from app.services.sessions import close_orphaned_sessions
from app.services.users import (EmailTaken, create_user, issue_device_token,
                                normalise_email, revoke_device)


def register(app):
    app.cli.add_command(create_user_cmd)
    app.cli.add_command(close_orphans_cmd)
    app.cli.add_command(refresh_drafts_cmd)
    app.cli.add_command(send_reports_cmd)
    app.cli.add_command(reset_password_cmd)
    app.cli.add_command(list_users_cmd)
    app.cli.add_command(issue_token_cmd)
    app.cli.add_command(revoke_token_cmd)
    app.cli.add_command(check_agents_cmd)
    app.cli.add_command(check_disk_cmd)


@click.command('create-user')
@click.option('--email', prompt=True)
@click.option('--name', prompt=True)
@click.option('--role', type=click.Choice(['worker', 'admin']), default='worker')
@click.option('--timezone', 'tz', default=None, help='IANA name, e.g. Africa/Nairobi')
@click.password_option('--password', confirmation_prompt=True)
@with_appcontext
def create_user_cmd(email, name, role, tz, password):
    try:
        user = create_user(db_session, email, name, password, role=role, timezone_name=tz)
    except (EmailTaken, WeakPassword, ValueError) as e:
        raise click.ClickException(str(e))
    click.echo(f'Created {user.email} ({user.role}) in {user.settings.timezone}')


@click.command('list-users')
@with_appcontext
def list_users_cmd():
    users = db_session.query(User).order_by(User.created_at).all()
    if not users:
        click.echo('No accounts yet. Start with: flask --app app create-user --role admin')
        return
    for u in users:
        devices = (db_session.query(Device)
                   .filter(Device.user_id == u.id, Device.revoked_at.is_(None)).count())
        state = '' if u.is_active else '  [disabled]'
        click.echo(f'{u.email:<34} {u.role:<7} {devices} device(s){state}')


@click.command('issue-token')
@click.option('--email', prompt=True)
@click.option('--device', prompt='Device name', help='e.g. "benson-thinkpad"')
@with_appcontext
def issue_token_cmd(email, device):
    user = (db_session.query(User)
            .filter(User.email == normalise_email(email)).one_or_none())
    if user is None:
        raise click.ClickException(f'No account for {email}')

    _, token = issue_device_token(db_session, user, device)
    click.echo(f'\nAgent token for {user.email} / {device}:\n')
    click.echo(f'  {token}\n')
    click.echo('This is shown once and is not recoverable — only its hash is stored.')
    click.echo('If it is lost, revoke it and issue another.')


@click.command('revoke-token')
@click.option('--device-id', prompt=True)
@with_appcontext
def revoke_token_cmd(device_id):
    device = db_session.get(Device, device_id)
    if device is None:
        raise click.ClickException('No such device')
    revoke_device(db_session, device)
    click.echo(f'Revoked {device.name} — its agent can no longer upload.')


@click.command('close-orphans')
@click.option('--dry-run', is_flag=True, help='Report what would be closed.')
@with_appcontext
def close_orphans_cmd(dry_run):
    """Cap sessions whose agent has gone silent. Runs on a schedule in
    production; here so it can be run and inspected by hand."""
    closed = close_orphaned_sessions(db_session)
    if dry_run:
        db_session.rollback()
    if not closed:
        click.echo('No orphaned sessions.')
        return
    for c in closed:
        click.echo(f"{'would close' if dry_run else 'closed'} #{c['id']} "
                   f"'{c['project']}' at {c['ended_at'].isoformat(timespec='seconds')} "
                   f"(silent {c['silent_for'] // 60}m)")


@click.command('check-agents')
@click.option('--dry-run', is_flag=True,
              help='List what would be alerted on without sending or claiming.')
@with_appcontext
def check_agents_cmd(dry_run):
    """Alert people whose own tracking stopped working.

    Runs on a schedule in production; here so the condition can be inspected
    without waiting for the worker, and so --dry-run can answer "would this
    have mailed anybody?" before a deploy rather than after.
    """
    from datetime import datetime, timezone

    from app.services import alerts as A

    now = datetime.now(timezone.utc)
    users = db_session.query(User).filter(User.is_active.is_(True)).all()

    if dry_run:
        pending = 0
        for user in users:
            for kind, rows in (('session_dropped', A.dropped_sessions(db_session, user, now=now)),
                               ('device_dormant', A.dormant_devices(db_session, user, now=now))):
                for row in rows:
                    key = A.dedupe_key(kind, row)
                    if A.already_sent(db_session, user, kind, key):
                        continue
                    eligible = '' if A._eligible(user, now) else '  [suppressed: paused or opted out]'
                    click.echo(f'would alert {user.email}: {kind} {key}{eligible}')
                    pending += 1
        if not pending:
            click.echo('Nothing to alert on.')
        return

    results = A.run_due(db_session, users, now=now)
    if not results:
        click.echo('Nothing to alert on.')
        return
    for email, kind, key, outcome in results:
        click.echo(f'{kind} for {email} ({key}): {outcome}')


@click.command('check-disk')
@click.option('--dry-run', is_flag=True, help='Report the state without sending.')
@with_appcontext
def check_disk_cmd(dry_run):
    """How full the server's disk is, and warn the administrators if it matters."""
    from app.services import alerts as A

    state = A.disk_state()
    click.echo(f"{state['percent']}% used, "
               f"{state['free_bytes'] / 2**30:.1f} GiB free of "
               f"{state['total_bytes'] / 2**30:.0f} GiB "
               f"(warn {state['warn_at']}%, critical {state['critical_at']}%)")
    if state['level'] is None:
        click.echo('Nothing to report.')
        return
    click.echo(f"Level: {state['level']}")
    if dry_run:
        click.echo('Would notify: ' + (', '.join(u.email for u in A.operators(db_session))
                                       or 'nobody — no admin has alerts enabled'))
        return
    for email, _, key, outcome in A.run_disk_check(db_session):
        click.echo(f'{email} ({key}): {outcome}')


@click.command('refresh-drafts')
@click.option('--days', default=2, help='How many recent local days to rebuild.')
@with_appcontext
def refresh_drafts_cmd(days):
    """Rebuild the machine's account of recent days for everyone.

    Runs on a schedule in production. Rebuilding a couple of days rather than
    only today is what lets a day whose usage arrived late — an agent uploading
    a backlog — get an accurate draft rather than the empty one written while
    its data was still on a laptop.
    """
    from datetime import timedelta

    from app.services import activity_log as AL
    from app.services import reporting as R

    total = 0
    for user in db_session.query(User).filter(User.is_active.is_(True)).all():
        today = R.logical_today(user)
        for offset in range(days):
            AL.refresh_draft(db_session, user, today - timedelta(days=offset))
            total += 1
    click.echo(f'Refreshed {total} draft(s).')


@click.command('send-reports')
@click.option('--email', default=None, help='Only this person.')
@click.option('--force', default=None,
              type=click.Choice(['weekly', 'monthly']),
              help='Send this kind now regardless of schedule — for samples.')
@click.option('--to', default=None,
              help='Override the recipient. Use with --force so a sample never '
                   'reaches whoever the real reports go to.')
@with_appcontext
def send_reports_cmd(email, force, to):
    """Send every report that is due. Runs on a schedule in production."""
    from datetime import datetime, timezone

    from app.reports import schedule as RS
    from app.reports import send as RSend
    from app.services import reporting as R

    query = db_session.query(User).filter(User.is_active.is_(True))
    if email:
        query = query.filter(User.email == normalise_email(email))
    users = query.all()
    if not users:
        raise click.ClickException('No matching accounts.')

    now = datetime.now(timezone.utc)

    if force:
        for user in users:
            if force == 'weekly':
                monday = R.week_start(R.logical_today(user, now)) - timedelta_days(7)
                period, key = (monday,), RS.weekly_key(monday)
            else:
                year, month = RS.previous_month(R.logical_today(user, now))
                period, key = (year, month), RS.monthly_key(year, month)

            images = {}
            from app.reports import render as RR
            subject, html = RSend.build(db_session, user, force, period, now=now,
                                        embed=RR.cid_embedder(images))
            recipient = to or user.email
            # A forced send does NOT claim the period: a sample must not consume
            # the real report that is still due later.
            from app.services import mail
            mail.send(recipient, '[Sample] ' + subject, html, images=images,
                      cc=() if to else user.settings.report_cc)
            click.echo(f'Sent sample to {recipient}: {subject}')
        return

    for line in RSend.run_due(db_session, users, now=now):
        click.echo(' '.join(str(part) for part in line))


def timedelta_days(n):
    from datetime import timedelta
    return timedelta(days=n)


@click.command('reset-password')
@click.option('--email', prompt=True)
@click.option('--send/--no-send', default=False,
              help='Mail the link as well as printing it.')
@with_appcontext
def reset_password_cmd(email, send):
    """Issue a single-use password-reset link."""
    import os

    from app.services.passwords import issue

    user = (db_session.query(User)
            .filter(User.email == normalise_email(email)).one_or_none())
    if user is None:
        raise click.ClickException(f'No account for {email}')

    ticket, row = issue(db_session, user)
    base = os.environ.get('PUBLIC_URL', 'http://localhost:8000').rstrip('/')
    link = f'{base}/reset/{ticket}'

    click.echo(f'\nSingle-use link for {user.email}, valid until '
               f'{row.expires_at.strftime("%H:%M UTC on %d %b")}:\n')
    click.echo(f'  {link}\n')
    click.echo('Any earlier unused link for this account has stopped working.')

    if send:
        from app.services import mail
        mail.send(user.email, 'Set your TimeTracker password',
                  f'<p>Hello {user.name},</p>'
                  f'<p>Use this link to set a new password. It works once and '
                  f'expires in two hours.</p><p><a href="{link}">{link}</a></p>'
                  f'<p>If you did not ask for this, tell whoever administers '
                  f'TimeTracker — the link is live until it expires.</p>')
        click.echo(f'Also mailed to {user.email}.')
