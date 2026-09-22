#!/usr/bin/env bash
# Managed PostgreSQL backup strategy
set -euo pipefail
DB_URL="${DATABASE_URL:?DATABASE_URL must be set}"
BACKUP_DIR="${BACKUP_DIR:-/var/backups/enterai}"
BACKUP_RETENTION_DAYS="${BACKUP_RETENTION_DAYS:-14}"
BACKUP_OFFSITE_HOOK="${BACKUP_OFFSITE_HOOK:-}"
mkdir -p "$BACKUP_DIR"
TIMESTAMP=$(date -u +%Y%m%d_%H%M%S)
ARCHIVE="$BACKUP_DIR/enterai_$TIMESTAMP.dump"
# SQLAlchemy dialect URLs are not libpq connection strings.  Keep passwords out
# of logs and convert only the driver prefix pg_dump cannot understand.
LIBPQ_URL="${DB_URL/postgresql+psycopg:/postgresql}"
echo "Starting backup at $TIMESTAMP"
pg_dump -Fc --no-owner --clean --if-exists -d "$LIBPQ_URL" > "$ARCHIVE"
test -s "$ARCHIVE"
pg_restore --list "$ARCHIVE" > /dev/null
if [ -n "$BACKUP_OFFSITE_HOOK" ]; then
  "$BACKUP_OFFSITE_HOOK" "$ARCHIVE"
fi
find "$BACKUP_DIR" -maxdepth 1 -type f -name 'enterai_*.dump' -mtime +"$BACKUP_RETENTION_DAYS" -delete
echo "Backup verified: $(basename "$ARCHIVE")"
