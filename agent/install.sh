#!/usr/bin/env bash
# Install the agent on one machine and keep it running.
#
# Run it as the person being tracked, from inside a desktop session — not with
# sudo. Everything it creates belongs to that user: the virtualenv, the spool,
# the config, and a systemd USER service tied to the graphical session.
set -euo pipefail

AGENT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UNIT_DIR="$HOME/.config/systemd/user"
UNIT="timetracker-agent.service"

say()  { printf '\n\033[1m%s\033[0m\n' "$*"; }
warn() { printf '\033[33m! %s\033[0m\n' "$*"; }
die()  { printf '\033[31mx %s\033[0m\n' "$*" >&2; exit 1; }

[ "$(id -u)" -ne 0 ] || die "Run this as yourself, not with sudo — the agent watches your session."

# ── Will this machine work at all? ──────────────────────────────────────────

say "Checking this machine"

# The single most likely reason tracking silently records nothing. The idle
# counter and the focused window both come from X11; under Wayland neither is
# readable and the agent starts, finds no idle source, and tracks nothing.
case "${XDG_SESSION_TYPE:-unknown}" in
  x11)
    echo "  session type: x11" ;;
  wayland)
    die "This is a Wayland session, and the agent needs X11 — it would run and
   record nothing at all. Log out, and at the login screen choose the gear
   icon and 'Ubuntu on Xorg', then run this again.
   (Check afterwards with: echo \$XDG_SESSION_TYPE)" ;;
  *)
    warn "Session type is '${XDG_SESSION_TYPE:-unset}'. If tracking records
    nothing, this is the first thing to suspect." ;;
esac

command -v xdotool >/dev/null \
  && echo "  xdotool: found" \
  || warn "xdotool is missing — time is still tracked, but not attributed to
    applications, and the daily draft will be empty. sudo apt install xdotool"

if command -v scrot >/dev/null || command -v import >/dev/null \
   || command -v gnome-screenshot >/dev/null || command -v spectacle >/dev/null; then
  echo "  screenshot tool: found"
else
  warn "No screen capture tool — captures will be skipped silently while the
    consent page says they are taken. sudo apt install scrot"
fi

python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' \
  || die "Python 3.10 or newer is required."

# ── Dependencies ────────────────────────────────────────────────────────────

say "Installing into $AGENT_DIR/.venv"
[ -d "$AGENT_DIR/.venv" ] || python3 -m venv "$AGENT_DIR/.venv"
"$AGENT_DIR/.venv/bin/pip" install --quiet --upgrade pip
"$AGENT_DIR/.venv/bin/pip" install --quiet -r "$AGENT_DIR/requirements.txt"
echo "  done"

# ── Enrolment ───────────────────────────────────────────────────────────────

CONFIG="$HOME/.timetracker-agent/config.json"
say "Server and token"
if [ -f "$CONFIG" ]; then
  echo "  $CONFIG already exists — left alone."
else
  mkdir -p "$(dirname "$CONFIG")"
  read -rp "  Server URL (e.g. https://time.example.com): " SERVER
  # Not echoed: the token is the credential for this device.
  read -rsp "  Device token (from 'flask --app app issue-token'): " TOKEN; echo
  [ -n "$SERVER" ] && [ -n "$TOKEN" ] || die "Both are required."
  umask 077
  printf '{\n  "server": "%s",\n  "token": "%s"\n}\n' "$SERVER" "$TOKEN" > "$CONFIG"
  chmod 600 "$CONFIG"
  echo "  written to $CONFIG (readable only by you)"
fi

# ── Keep it running ─────────────────────────────────────────────────────────

say "Installing the service"
mkdir -p "$UNIT_DIR"
sed "s|__AGENT_DIR__|$AGENT_DIR|g" "$AGENT_DIR/$UNIT" > "$UNIT_DIR/$UNIT"

systemctl --user daemon-reload
systemctl --user enable "$UNIT" >/dev/null
systemctl --user restart "$UNIT"

sleep 2
if systemctl --user is-active --quiet "$UNIT"; then
  echo "  running, and will start again at every login"
else
  warn "It did not stay running. What it said:"
  journalctl --user -u "$UNIT" -n 20 --no-pager || true
  exit 1
fi

say "Done."
cat <<NEXT
  Status:   systemctl --user status timetracker-agent
  Logs:     journalctl --user -u timetracker-agent -f
  Stop:     systemctl --user stop timetracker-agent

  The service is tied to your desktop session: it starts when you log in and
  stops when you log out. That is deliberate — it can only watch a session
  that exists.

  To stop being tracked for a while, use Pause in the tray menu or on the
  dashboard rather than stopping the service. A pause is recorded and
  enforced by the server; a stopped service just looks like a broken agent,
  and after three days it will email you saying so.
NEXT
