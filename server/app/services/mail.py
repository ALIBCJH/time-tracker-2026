"""Sending mail.

Credentials come from the environment and nowhere else. The local app kept a
Gmail app password in ~/.timetracker/email_config.json, which is precisely the
thing that cannot ship to a server holding other people's data.

The message shape — multipart/related wrapping multipart/alternative — is what
Gmail, Outlook and Apple Mail all expect for inline images. Putting the image
parts as siblings of the HTML instead of children of the related part is the
classic way to get charts shown as downloadable attachments instead.
"""
import logging
import os
import smtplib
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

logger = logging.getLogger('mail')


class NotConfigured(RuntimeError):
    """No SMTP settings. Reports are skipped, not retried — there is nothing to
    retry until someone configures it."""


def config():
    host = os.environ.get('SMTP_HOST')
    sender = os.environ.get('MAIL_FROM')
    if not host or not sender:
        raise NotConfigured('SMTP_HOST and MAIL_FROM must be set to send mail.')
    return {
        'host': host,
        'port': int(os.environ.get('SMTP_PORT', 465)),
        'user': os.environ.get('SMTP_USER', sender),
        'password': os.environ.get('SMTP_PASSWORD', ''),
        'sender': sender,
        'use_ssl': os.environ.get('SMTP_SSL', 'true').lower() != 'false',
    }


def build_message(sender, to, cc, subject, html, images=None):
    body = MIMEMultipart('alternative')
    body.attach(MIMEText(html, 'html', 'utf-8'))

    if images:
        message = MIMEMultipart('related')
        message.attach(body)
        for name, data in images.items():
            part = MIMEImage(data, 'png')
            part.add_header('Content-ID', f'<{name}>')
            # inline, not attachment — otherwise clients show paperclips.
            part.add_header('Content-Disposition', 'inline', filename=f'{name}.png')
            message.attach(part)
    else:
        message = body

    message['Subject'] = subject
    message['From'] = sender
    message['To'] = to
    if cc:
        message['Cc'] = ', '.join(cc)
    return message


def send(to, subject, html, images=None, cc=(), settings=None):
    """Deliver one message. Raises on failure so the caller can release its claim."""
    settings = settings or config()
    cc = [address for address in (cc or []) if address and address != to]
    message = build_message(settings['sender'], to, cc, subject, html, images)
    recipients = [to] + cc

    opener = smtplib.SMTP_SSL if settings['use_ssl'] else smtplib.SMTP
    with opener(settings['host'], settings['port']) as server:
        if not settings['use_ssl']:
            server.starttls()
        if settings['password']:
            server.login(settings['user'], settings['password'])
        server.sendmail(settings['sender'], recipients, message.as_string())
    logger.info(f'Sent "{subject}" to {to}' + (f' (cc {", ".join(cc)})' if cc else ''))
    return recipients
