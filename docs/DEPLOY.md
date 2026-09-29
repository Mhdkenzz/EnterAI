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
at frontend build time), `STAGING_TERMS_URL`, `STAGING_PRIVACY_URL`, and
`STAGING_READINESS_URL` (the HTTPS `/ready` endpoint). The legal URLs are compiled
into the frontend and supplied to the backend release environment as well.
Optional `STAGING_LOCAL_EMBEDDINGS=true` adds the local sentence-transformers
dependencies to the backend image; leave it unset for the normal hosted OpenAI
embedding path.

## Host application environment

The pre-provisioned host keeps a mode-600 `.env` outside release bundles. Required
in hosted mode: `ENVIRONMENT=staging`, `DATABASE_URL`, `JWT_SECRET`, `REDIS_URL`,
`APP_BASE_URL`, `API_BASE_URL`, `CORS_ALLOWED_ORIGINS`, `TRUSTED_HOSTS`,
`TERMS_URL`, `PRIVACY_URL`,
`EMBEDDING_PROVIDER` (and its matching `EMBEDDING_MODEL`, `EMBEDDING_DIMENSION`,
`EMBEDDING_BASE_URL`, and `OPENAI_API_KEY` when applicable),
`SEED_ADMIN_EMAIL`, `SEED_ADMIN_PASSWORD`, SMTP settings, storage settings,
`BACKUP_OBJECTS_BUCKET`, `BACKUP_OBJECTS_ACCESS_KEY`, and
`BACKUP_OBJECTS_SECRET_KEY` for the off-site object mirror,
`POSTGRES_*`, and `TLS_CERT_PATH`/`TLS_KEY_PATH`. Set `SECRETS_ENCRYPTION_KEY` for
every hosted release; staging and production fail closed without it. Do not set
localhost URLs or wildcard trusted hosts.

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
3. Run the manual workflow for one exact SHA after all required CI checks are green
   for that same SHA. It pulls digest-pinned images, runs one migration job with
   `alembic current --check`, starts two API replicas plus database/object-storage
   backup services, and waits on public `/ready`.
4. Smoke test sign-in, a write-confirmation flow, uploads, SSO callback, and both
   replicas. Perform a disposable restore drill before relying on backups.
5. Application-image rollback is a deliberate new release of a prior digest.
   Do **not** automatically downgrade a database; follow the restore runbook.

Docker Compose can start two replicas but is not Kubernetes: it cannot guarantee
zero-downtime rolling replacement. The workflow proves migration then both running
replicas and readiness; schedule maintenance capacity and test failure recovery.
