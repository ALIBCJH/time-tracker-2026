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

# Every user gets their own, but a new account has to start somewhere and this
# is a Nairobi-based team.
DEFAULT_TIMEZONE = os.environ.get('DEFAULT_TIMEZONE', 'Africa/Nairobi')
