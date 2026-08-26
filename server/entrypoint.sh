#!/bin/sh
# Migrations run once, here, before anything serves traffic — not from an
# application worker. Four workers racing `alembic upgrade` is how a schema
# ends up half-applied.
set -e

echo "Waiting for the database..."
until pg_isready -d "$DATABASE_URL_PSQL" >/dev/null 2>&1; do sleep 1; done

echo "Applying migrations..."
alembic upgrade head

exec "$@"
