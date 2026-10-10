# Guarded staging deployment

This repository is deployment-ready code, not evidence of a live staging or
production deployment. `Guarded Staging Deploy` is manual-only and serializes
releases; it builds the selected full SHA, publishes backend and frontend images,
then deploys their registry digests. The host receives a reviewed Compose bundle,
not a Git checkout, and never runs `docker compose --build`.

## Required GitHub staging secrets

- `IMAGE_REGISTRY`, `IMAGE_REPOSITORY`, `REGISTRY_USERNAME`, `REGISTRY_PASSWORD`:
  generic OCI registry location and least-privileged pull/push credentials.
- `DEPLOY_HOST`, `DEPLOY_SSH_USER`, `DEPLOY_SSH_KEY`, `DEPLOY_PATH`: restricted
  staging deploy account and its absolute release directory.
- `DEPLOY_KNOWN_HOSTS`: pinned known_hosts line(s) for the SSH endpoint. Never use
  `accept-new` or disable host-key checking.

Required non-secret GitHub staging variables: `STAGING_PUBLIC_API_URL` (passed only
at frontend build time) and `STAGING_READINESS_URL` (the HTTPS `/ready` endpoint).

## Host application environment

The pre-provisioned host keeps a mode-600 `.env` outside release bundles. Required
in hosted mode: `ENVIRONMENT=staging`, `DATABASE_URL`, `JWT_SECRET`, `REDIS_URL`,
`APP_BASE_URL`, `API_BASE_URL`, `CORS_ALLOWED_ORIGINS`, `TRUSTED_HOSTS`,
`SEED_ADMIN_EMAIL`, `SEED_ADMIN_PASSWORD`, SMTP settings, storage settings,
`POSTGRES_*`, and `TLS_CERT_PATH`/`TLS_KEY_PATH`. Set `SECRETS_ENCRYPTION_KEY` if
`OIDC_ENABLED=true`. Do not set localhost URLs or wildcard trusted hosts.

`docker-compose.prod.yml` accepts only `ENTERAI_BACKEND_IMAGE` and
`ENTERAI_FRONTEND_IMAGE` immutable digest references supplied by the workflow.
The frontend image must be built with `BUILD_ENVIRONMENT=staging` and
`NEXT_PUBLIC_API_URL=https://…`; runtime environment cannot change it.
Browser sessions are an httpOnly, SameSite=Lax, Secure cookie scoped to `/api`, so
`NEXT_PUBLIC_API_URL` must be on the same site as `APP_BASE_URL` (the bundled nginx
serves both from one host), or signed-in requests will not carry the session.

## Release and rollback boundaries

1. Provision a host, DNS, TLS certificate files, and private DB/Redis/object
   storage. Only nginx publishes ports 80/443.
2. Store host secrets with restrictive permissions and configure registry pull.
   The deploy account should be dedicated and limited to the release directory;
   Docker access is privileged and must be treated accordingly.
3. Run the manual workflow for one exact SHA. It pulls digest-pinned images, runs
   one migration job with `alembic current --check`, starts two replicas, and waits
   on public `/ready`.
4. Smoke test sign-in, a write-confirmation flow, uploads, SSO callback, and both
   replicas. Perform a disposable restore drill before relying on backups.
5. Application-image rollback is a deliberate new release of a prior digest.
   Do **not** automatically downgrade a database; follow the restore runbook.

Docker Compose can start two replicas but is not Kubernetes: it cannot guarantee
zero-downtime rolling replacement. The workflow proves migration then both running
replicas and readiness; schedule maintenance capacity and test failure recovery.

## Production (`Guarded Production Deploy`)

Manual-only, same bundle and digest model as staging, plus:

- **Gates.** The SHA must be on `main`, with a successful `Enter AI CI` run. Every
  job runs in the `production` GitHub environment, so add yourself as a
  *required reviewer* under Settings → Environments → production. Every release
  then waits for your approval.
- **Backup first.** The deploy takes a verified `pg_dump` (`scripts/backup.sh`)
  immediately before `alembic upgrade`. That dump is the rollback point for a bad
  migration (see [INCIDENT_RESPONSE.md](INCIDENT_RESPONSE.md)).
- **Backups keep running.** The deploy starts `backup` and `objectstore-backup`
  with the release.
- **Frontend rebuilt for production.** The public API URL and legal-page URLs are
  compiled into the browser bundle.

Production environment secrets have the same names as staging:
`IMAGE_REGISTRY`, `IMAGE_REPOSITORY`, `REGISTRY_USERNAME`, `REGISTRY_PASSWORD`,
`DEPLOY_HOST`, `DEPLOY_SSH_USER`, `DEPLOY_SSH_KEY`, `DEPLOY_KNOWN_HOSTS`,
`DEPLOY_PATH`. Use a separate host and credentials.

Production environment variables, all HTTPS:

- `PRODUCTION_PUBLIC_API_URL`, e.g. `https://api.yourdomain.com/api`
- `PRODUCTION_READINESS_URL`, e.g. `https://api.yourdomain.com/ready`
- `PRODUCTION_TERMS_URL`
- `PRODUCTION_PRIVACY_URL`

The host's `$DEPLOY_PATH/.env` must set `ENVIRONMENT=production` and everything
`deployment_config.py` requires: `DATABASE_URL`, `JWT_SECRET`, `REDIS_URL`,
`APP_BASE_URL`, `API_BASE_URL`, `CORS_ALLOWED_ORIGINS`, `TRUSTED_HOSTS`,
`SEED_ADMIN_EMAIL`, `SEED_ADMIN_PASSWORD`, SMTP settings, storage keys,
`STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET`, `BILLING_PLANS` and
`SECRETS_ENCRYPTION_KEY`. Startup refuses unsafe values.

Release order: merge to `main` → CI green → `Guarded Staging Deploy` with the SHA
→ check staging → `Guarded Production Deploy` with the same SHA → approve.

## Monitoring and billing checks

- **`Uptime check`** runs every 10 minutes once merged to `main`. It reads
  `PRODUCTION_READINESS_URL` / `STAGING_READINESS_URL` from repository variables,
  and two consecutive failures open an `outage` issue.
- **`Stripe sandbox billing check`** runs manually. It needs the repository secret
  `STRIPE_TEST_SECRET_KEY` (a test-mode key only). It runs the paid lifecycle
  against Stripe test mode end to end.
