"""Configuration, read from the environment only.

Nothing is read from a file on disk: the local app kept its Gmail app password
in ~/.timetracker/email_config.json, which is exactly the thing that cannot ship
to a server. Secrets arrive as env vars and stay there.
"""
import os
from pathlib import Path

from dotenv import load_dotenv

# .env is a developer convenience; in production the environment is already set
# and this call finds nothing, which is correct.
load_dotenv(Path(__file__).resolve().parent.parent / '.env')


def require(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(
            f"{name} is not set. Copy server/.env.example to server/.env for "
            f"development, or set it in the deployment environment."
        )
    return value


DATABASE_URL = require('DATABASE_URL')
SECRET_KEY = os.environ.get('SECRET_KEY', '')

# How long a signed-in browser may sit idle. Long enough to cover a working day
# without interrupting anybody, short enough that a machine left unlocked
# overnight is signed out by morning. It slides: every request refreshes it, so
# this is idle time and not a countdown from signing in.
SESSION_IDLE_HOURS = int(os.environ.get('SESSION_IDLE_HOURS', 12))

# When to say the disk is filling. Everything shares one volume — Postgres, the
# nightly dumps, Docker's layers and, if S3 is not configured, every screen
# capture — so it fills from several directions at once and the first symptom
# is Postgres refusing to write.
DISK_WARN_PERCENT = int(os.environ.get('DISK_WARN_PERCENT', 80))
DISK_CRITICAL_PERCENT = int(os.environ.get('DISK_CRITICAL_PERCENT', 90))

# Every user gets their own, but a new account has to start somewhere and this
# is a Nairobi-based team.
DEFAULT_TIMEZONE = os.environ.get('DEFAULT_TIMEZONE', 'Africa/Nairobi')
