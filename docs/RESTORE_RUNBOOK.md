# Backup and restore drill

`backup.sh` creates a compressed PostgreSQL archive, verifies it with
`pg_restore --list`, and retains local copies for `BACKUP_RETENTION_DAYS` (14 by
default). It accepts SQLAlchemy `postgresql+psycopg://` URLs but converts only that
driver prefix to libpq `postgresql://` before calling PostgreSQL tools. It never
logs a URL.

The production `database-backup-offsite` worker checks for new archives every five
minutes (configurable with `DATABASE_BACKUP_SYNC_INTERVAL_SECONDS`), copies them to
`DATABASE_BACKUP_BUCKET`, and verifies its size and SHA-256 metadata. This must be a
dedicated bucket, separate from `BACKUP_OBJECTS_BUCKET`; configure off-host
versioning/lifecycle retention there. The local Compose volume is only a short-term
cache. Monitor the worker logs and test restore access, not merely object existence.

## Uploaded files

The database dump does not contain uploaded documents and attachments; those live in
object storage. The `objectstore-backup` service in `docker-compose.prod.yml` runs
`python -m app.backup_objects --loop`, mirroring `STORAGE_BUCKET` to
`BACKUP_OBJECTS_BUCKET` every `BACKUP_INTERVAL_SECONDS`. Objects are immutable (each
upload gets a new key), so the mirror copies new keys and removes keys deleted at the
source -- which is what makes an account or organization deletion reach the backup.
Point-in-time recovery comes from the destination bucket: enable versioning with a
noncurrent-version expiry equal to `BACKUP_RETENTION_DAYS`.

To restore, copy the objects back from the upload backup bucket (or a prior version
of it) into a fresh `STORAGE_BUCKET`, retrieve the matching archive from
`DATABASE_BACKUP_BUCKET`, restore it, then spot-check
that a few `project_documents.path` / `attachments.path` values resolve. Restore the
files and the database from the same point in time, or rows will reference missing files.

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
archive, runs `alembic upgrade head`, compares `alembic current` with `alembic heads`, and issues
a basic SQL smoke query. Record archive identifier, revision, duration, and result.
Destroy the disposable database only after evidence is retained; that deletion is a
separate, explicitly authorized operation.
