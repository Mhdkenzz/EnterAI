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
