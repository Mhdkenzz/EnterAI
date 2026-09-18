#!/usr/bin/env bash
# Managed PostgreSQL backup strategy
set -euo pipefail
DB_URL="${DATABASE_URL:-postgresql+psycopg://enterai:enterai_dev_password@db:5432/enterai}"
BACKUP_DIR="${BACKUP_DIR:-/var/backups/enterai}"
mkdir -p "$BACKUP_DIR"
TIMESTAMP=$(date -u +%Y%m%d_%H%M%S)
echo "Starting backup at $TIMESTAMP"
pg_dump -Fc --no-owner --clean --if-exists -d "$DB_URL" > "$BACKUP_DIR/enterai_$TIMESTAMP.dump"
echo "Backup complete: $BACKUP_DIR/enterai_$TIMESTAMP.dump"
pg_restore --list "$BACKUP_DIR/enterai_$TIMESTAMP.dump" > /dev/null || echo "WARNING: Dump integrity check failed"
