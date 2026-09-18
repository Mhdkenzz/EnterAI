# Restore Runbook
1. Identify latest valid backup (.dump file).
2. Stop the application (prevent writes during restore).
3. Restore: `pg_restore --clean --if-exists --dbname=<DB_URL> <backup.dump>`
4. Verify: run migrations (`alembic upgrade head`), check schema, run smoke tests.
5. Restart services.
