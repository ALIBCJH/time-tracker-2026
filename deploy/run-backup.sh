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
exec $COMPOSE exec -T db sh -c '
  set -eu
  export PGUSER="$POSTGRES_USER"
  export DATABASE_URL_PSQL="postgresql:///$POSTGRES_DB"
  export BACKUP_DIR=/backups
  exec sh /deploy/backup.sh
'
