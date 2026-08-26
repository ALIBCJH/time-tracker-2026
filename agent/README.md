# TimeTracker agent

Runs on each tracked machine. Records what had focus, how long, and (if enabled)
a periodic screen capture — into a local spool first, uploaded second, so a lost
connection costs nothing.

## Install

    python3 -m venv .venv
    .venv/bin/pip install PyQt6 Pillow

Nothing else. Everything but the widget and the image encoder is standard
library, because every dependency is one more thing that can fail to install on
somebody else's machine.

Needs `xdotool` for window titles and one of `scrot`, `import`,
`gnome-screenshot` or `spectacle` for captures. Time is still tracked without
either — it is just not attributed to applications, and no pictures are taken.

## Enrol

An administrator issues a token on the server:

    flask --app app issue-token --email you@example.com --device "your-laptop"

It is shown once. Then, on the laptop:

    TIMETRACKER_SERVER=https://time.example.com \
    TIMETRACKER_TOKEN=ttc_... \
    .venv/bin/python agent_main.py

Or write `~/.timetracker-agent/config.json` (created 0600) with `server` and
`token`, and just run `agent_main.py`.

## What it does not do

It has no settings of its own. Idle threshold, capture interval, whether
captures happen at all and whether tracking is paused all come from the server,
so changing them is a browser tab rather than a phone call. If the server is
unreachable it keeps the last values it was given — including a pause, which is
never assumed to have been lifted.

## Stopping it

Pause from the dashboard (tray menu → Pause tracking). While paused the agent
records nothing and the server refuses anything that arrives anyway.
