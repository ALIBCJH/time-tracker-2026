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
from app.services.users import (EmailTaken, create_user, issue_device_token,
                                normalise_email, revoke_device)


def register(app):
    app.cli.add_command(create_user_cmd)
    app.cli.add_command(list_users_cmd)
    app.cli.add_command(issue_token_cmd)
    app.cli.add_command(revoke_token_cmd)


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
