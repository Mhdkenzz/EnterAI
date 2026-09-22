# Backup and restore drill

`backup.sh` creates a compressed PostgreSQL archive, verifies it with
`pg_restore --list`, retains archives for `BACKUP_RETENTION_DAYS` (14 by default),
and exits nonzero on dump, verification, or optional off-host-copy failure. It
accepts SQLAlchemy `postgresql+psycopg://` URLs but converts only that driver prefix
to libpq `postgresql://` before calling PostgreSQL tools. It never logs a URL.

The local Compose volume is not an off-host backup. Configure `BACKUP_OFFSITE_HOOK`
as a root-owned executable that receives the verified archive path and uploads it
to an approved S3-compatible or other off-host destination. Alert on nonzero exit
and test restore access, not merely object existence.

## Required disposable restore drill

Never restore automatically over the active staging database. Create a separately
named disposable database with credentials different from the active service, then:

```bash
RESTORE_DATABASE_URL='postgresql+psycopg://…/enterai_restore_drill' \
RESTORE_ARCHIVE=/secure/backups/enterai_YYYYMMDD_HHMMSS.dump \
RESTORE_CONFIRM=RESTORE_TO_DISPOSABLE_DATABASE \
./scripts/verify_restore.sh
```

The script refuses to run without the exact confirmation value. It restores the
archive, runs `alembic upgrade head`, verifies `alembic current --check`, and issues
a basic SQL smoke query. Record archive identifier, revision, duration, and result.
Destroy the disposable database only after evidence is retained; that deletion is a
separate, explicitly authorized operation.
