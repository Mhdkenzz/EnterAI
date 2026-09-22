#!/usr/bin/env bash
# A restore drill only targets an explicitly named disposable database.
set -euo pipefail
: "${RESTORE_DATABASE_URL:?set a disposable RESTORE_DATABASE_URL}"
: "${RESTORE_ARCHIVE:?set RESTORE_ARCHIVE}"
test "${RESTORE_CONFIRM:-}" = "RESTORE_TO_DISPOSABLE_DATABASE" || { echo "Set RESTORE_CONFIRM=RESTORE_TO_DISPOSABLE_DATABASE" >&2; exit 2; }
test -s "$RESTORE_ARCHIVE"
LIBPQ_URL="${RESTORE_DATABASE_URL/postgresql+psycopg:/postgresql}"
pg_restore --clean --if-exists --no-owner --dbname="$LIBPQ_URL" "$RESTORE_ARCHIVE"
repo_root=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
(cd "$repo_root/backend" && DATABASE_URL="$RESTORE_DATABASE_URL" alembic upgrade head && current=$(DATABASE_URL="$RESTORE_DATABASE_URL" alembic current) && heads=$(DATABASE_URL="$RESTORE_DATABASE_URL" alembic heads) && test "$current" = "$heads")
psql "$LIBPQ_URL" -v ON_ERROR_STOP=1 -c 'SELECT 1 AS restore_smoke;'
echo "Disposable restore verification passed"
