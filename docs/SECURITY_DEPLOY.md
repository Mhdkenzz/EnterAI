# Security & Deployment Hardening
- Non-root containers: Dockerfile runs as non-root user (uid != 0)
- Health checks: Docker compose healthcheck on db (pg_isready) and redis (redis-cli ping)
- Least-privilege IAM: service account only has access to required DB/user/redis
- Encrypted backups: pg_dump compressed; backup directory encrypted at rest
- Secret rotation: documented in SECRETS_MANAGEMENT.md
- Rollback: documented in RESTORE_RUNBOOK.md; deploy pipeline supports rollback
- No secrets in repo or logs: enforced by .gitignore and audit filters
