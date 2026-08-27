#!/usr/bin/env bash
# Nightly backup, run from the host by cron.
#
# backup.sh itself runs INSIDE the database container, which is the only place
# with pg_dump, a local socket and the privileges to create the scratch
# database it restores into to prove the dump is good. This wrapper exists so
# the crontab line is one readable command rather than a paragraph of nested
# quoting.
set -euo pipefail

DEPLOY_PATH="${DEPLOY_PATH:-/opt/timetracker}"
BACKUP_DIR="${BACKUP_DIR:-/var/backups/ttcloud}"
COMPOSE="docker compose -f $DEPLOY_PATH/docker-compose.prod.yml"

cd "$DEPLOY_PATH"

# POSTGRES_USER and POSTGRES_DB are already in the container's environment, so
# the credentials are read there rather than passed in from here — nothing
# secret appears in the crontab, in ps output, or in this script.
#
# The connection is over the container's local socket, which the image trusts,
# so no password crosses anything. PGUSER matters because createdb and psql
# would otherwise use the container's OS user, and there is no database role
# by that name when POSTGRES_USER is anything other than 'postgres'.
$COMPOSE exec -T db sh -c '
  set -eu
  export PGUSER="$POSTGRES_USER"
  export DATABASE_URL_PSQL="postgresql:///$POSTGRES_DB"
  export BACKUP_DIR=/backups
  exec sh /deploy/backup.sh
'

# ── Off this machine ─────────────────────────────────────────────────────────
#
# A backup on the same disk as the database survives a dropped table and a bad
# migration. It does not survive the instance — and losing the instance is the
# failure a backup exists for. So the verified dump is copied to S3 from here,
# where the aws CLI and the instance's credentials live; the database container
# has neither, on purpose.
#
# Deliberately after the verification. A dump that failed to restore is deleted
# by backup.sh and never reaches this line, so nothing useless is stored.
if [ -z "${BACKUP_S3_URI:-}" ]; then
  echo "No BACKUP_S3_URI set — backups exist only on this instance."
  exit 0
fi

command -v aws >/dev/null || {
  echo "BACKUP_S3_URI is set but the aws CLI is missing. Backups are NOT leaving this machine." >&2
  exit 1
}

NEWEST="$(ls -1t "$BACKUP_DIR"/ttcloud-*.dump 2>/dev/null | head -1 || true)"
[ -n "$NEWEST" ] || { echo "No dump to upload." >&2; exit 1; }

aws s3 cp "$NEWEST" "$BACKUP_S3_URI/$(basename "$NEWEST")"
echo "Copied $(basename "$NEWEST") to $BACKUP_S3_URI"
