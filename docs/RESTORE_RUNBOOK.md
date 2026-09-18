# Restore Runbook

## Backup schedule
`docker-compose.prod.yml`'s `backup` service runs `scripts/backup.sh` on a loop
(default: every `BACKUP_INTERVAL_SECONDS`, 86400s/daily), writing timestamped
`pg_dump -Fc` archives to the `backup_data` volume and verifying each one with
`pg_restore --list`. It is not part of `lb`'s dependency chain, so a plain
`docker compose -f docker-compose.prod.yml up -d` (no target) is required to
start it -- `up -d --build lb` (used by CI's multi-replica test) does not.
Copy the volume off-host on whatever cadence your retention policy requires;
this service only guarantees a backup *exists* on the host, not off it.

## Restore
1. Identify latest valid backup (.dump file).
2. Stop the application (prevent writes during restore).
3. Restore: `pg_restore --clean --if-exists --dbname=<DB_URL> <backup.dump>`
4. Verify: run migrations (`alembic upgrade head`), check schema, run smoke tests.
5. Restart services.
