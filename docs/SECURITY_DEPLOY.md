# Security & Deployment Hardening
- Non-root containers: Dockerfile runs as non-root user (uid != 0)
- Health checks: `/health` verifies DB + Redis reachability (backend/app/main.py); Docker compose healthcheck on db (pg_isready) and redis (redis-cli ping)
- Least-privilege IAM: service account only has access to required DB/user/redis
- Backups: `scripts/backup.sh` runs `pg_dump -Fc` (compressed, not encrypted -- there is
  no disk-level or GPG encryption of the dump file or `backup_data` volume in this repo).
  If the backup destination is not itself encrypted at rest (e.g. an encrypted host
  volume or an encrypting off-host copy target), treat that as an open gap, not a
  solved one.
- Secret rotation: documented in SECRETS_MANAGEMENT.md; real, tested rotation exists
  for at-rest-encrypted application secrets (backend/app/secrets_store.py) -- IdP
  client secrets today. JWT_SECRET/DATABASE_URL/OPENAI_API_KEY rotation is still a
  manual redeploy-with-new-env-var procedure, not automated.
- Deployment: see docs/DEPLOY.md. Real image builds run in CI on every push;
  automated deploy-to-target only runs if DEPLOY_HOST/DEPLOY_SSH_USER/DEPLOY_SSH_KEY
  are configured as repo secrets (they are not, as of this writing -- deployment is
  manual until they are).
- Rollback: documented in RESTORE_RUNBOOK.md (data) and DEPLOY.md (application code)
- No secrets in repo or logs: enforced by .gitignore and audit filters
