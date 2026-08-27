# TimeTracker agent

Runs on each tracked machine. Records what had focus, how long, and (if enabled)
a periodic screen capture — into a local spool first, uploaded second, so a lost
connection costs nothing.

## Install

    ./install.sh

Run it as the person being tracked, from inside a desktop session, without
sudo. It checks the machine, installs the dependencies, asks for the server and
device token, and installs a systemd user service so tracking survives a reboot
without anybody remembering to start it.

### It needs X11, not Wayland

This is the one thing that stops the agent dead. The idle counter and the
focused window both come from the X display; under Wayland neither is readable,
so the agent starts, finds no idle source, and records **nothing at all**.

    echo $XDG_SESSION_TYPE

If that says `wayland`, log out and choose "Ubuntu on Xorg" from the gear icon
at the login screen. `install.sh` refuses to install rather than leaving you
with an agent that appears to run and tracks nothing.

### Optional, but you want both

`xdotool` for window titles, and one of `scrot`, `import`, `gnome-screenshot`
or `spectacle` for captures. Time is still tracked without either — it is just
not attributed to applications, and no pictures are taken, which quietly makes
the consent page wrong. The installer says so if they are missing.

Everything else is standard library, because every dependency is one more thing
that can fail to install on somebody else's machine.

## Running

    systemctl --user status timetracker-agent
    journalctl --user -u timetracker-agent -f

The service is tied to the desktop session: it starts at login and stops at
logout, because it can only watch a session that exists. It restarts itself if
it crashes.

To stop being tracked for a while, use **Pause** — in the tray menu or on the
dashboard. A pause is recorded and enforced by the server. Stopping the service
instead just looks like a broken agent, and after three days it emails you
saying so.

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
