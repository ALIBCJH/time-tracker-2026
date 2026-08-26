#!/usr/bin/env bash
# Nightly database backup, with a RESTORE VERIFICATION built in.
#
# A backup nobody has restored is a hope, not a backup. This one restores each
# dump into a scratch database and counts the rows before it is kept, so a
# silently corrupt or empty dump is caught the night it happens rather than on
# the day it is needed.
set -euo pipefail

BACKUP_DIR="${BACKUP_DIR:-/var/backups/ttcloud}"
KEEP_DAYS="${KEEP_DAYS:-14}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
DUMP="$BACKUP_DIR/ttcloud-$STAMP.dump"

mkdir -p "$BACKUP_DIR"

echo "Dumping..."
pg_dump --format=custom --no-owner --file="$DUMP" "$DATABASE_URL_PSQL"

echo "Verifying by restoring into a scratch database..."
SCRATCH="ttcloud_verify_$$"
createdb "$SCRATCH"
trap 'dropdb --if-exists "$SCRATCH" >/dev/null 2>&1 || true' EXIT

pg_restore --no-owner --dbname="$SCRATCH" "$DUMP"
USERS=$(psql -tAc 'SELECT COUNT(*) FROM users' "$SCRATCH")
SESSIONS=$(psql -tAc 'SELECT COUNT(*) FROM sessions' "$SCRATCH")

if [ "$USERS" -lt 1 ]; then
    echo "FAILED: restored backup has no users — not keeping it." >&2
    rm -f "$DUMP"
    exit 1
fi
echo "Verified: $USERS user(s), $SESSIONS session(s)."

if [ -n "${BACKUP_S3_URI:-}" ]; then
    aws s3 cp "$DUMP" "$BACKUP_S3_URI/$(basename "$DUMP")"
    echo "Uploaded to $BACKUP_S3_URI"
fi

find "$BACKUP_DIR" -name 'ttcloud-*.dump' -mtime "+$KEEP_DAYS" -delete
echo "Done. Kept $(find "$BACKUP_DIR" -name 'ttcloud-*.dump' | wc -l) local backup(s)."
