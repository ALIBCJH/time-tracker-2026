"""Agent configuration: where the server is, and the token to reach it with.

Read from ~/.timetracker-agent/config.json, overridable by environment for
testing. The token is the one secret on the machine, so the file is written
0600 and never logged.
"""
import json
import os
import stat

DEFAULT_DIR = os.path.expanduser('~/.timetracker-agent')
CONFIG_NAME = 'config.json'


def config_path(directory=None):
    return os.path.join(directory or DEFAULT_DIR, CONFIG_NAME)


def load(directory=None):
    path = config_path(directory)
    data = {}
    if os.path.exists(path):
        with open(path) as f:
            data = json.load(f)
    # Environment wins, so a test or a one-off run needs no file at all.
    server = os.environ.get('TIMETRACKER_SERVER', data.get('server'))
    token = os.environ.get('TIMETRACKER_TOKEN', data.get('token'))
    if not server or not token:
        raise RuntimeError(
            f'Agent is not enrolled. Write {path} with {{"server": ..., "token": ...}} '
            f'or set TIMETRACKER_SERVER and TIMETRACKER_TOKEN.')
    return {'server': server.rstrip('/'), 'token': token}


def save(server, token, directory=None):
    directory = directory or DEFAULT_DIR
    os.makedirs(directory, exist_ok=True)
    path = config_path(directory)
    # Create with the right mode from the start — writing then chmod leaves a
    # window where the token is world-readable.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, stat.S_IRUSR | stat.S_IWUSR)
    with os.fdopen(fd, 'w') as f:
        json.dump({'server': server.rstrip('/'), 'token': token}, f, indent=2)
    return path
