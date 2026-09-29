# Guarded staging deployment

Use [`PRODUCTION_GO_NO_GO.md`](PRODUCTION_GO_NO_GO.md) for the scope-specific
launch decision and [`PHASE5_VERIFICATION_RUNBOOK.md`](PHASE5_VERIFICATION_RUNBOOK.md)
for the evidence collection that must precede broader production access.

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

The workflow also requires `jq` and Docker Compose v2 on the staging host. The
release bundle contains `scripts/verify_staging_config.sh`, which runs after the
immutable image pull but before any staging service is started. It checks the mode-600 host
environment, resolves the
Compose contract without printing secrets, requires digest-pinned images, confirms
invite-only registration, verifies TLS files and a separate object-backup bucket,
then runs the backend's canonical hosted-configuration validator inside the exact
backend image. A failure leaves the previous release untouched.

## Host application environment

The pre-provisioned host keeps a mode-600 `.env` outside release bundles. Required
in hosted mode: `ENVIRONMENT=staging`, `DATABASE_URL`, `JWT_SECRET`, `REDIS_URL`,
`APP_BASE_URL`, `API_BASE_URL`, `CORS_ALLOWED_ORIGINS`, `TRUSTED_HOSTS`,
`TERMS_URL`, `PRIVACY_URL`,
`AI_PROVIDER`,
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
The workflow also injects `RELEASE_SHA` into the release env file so feedback
reports and readiness diagnostics identify the exact deployed commit.
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
4. The workflow then runs `scripts/staging_smoke.sh` against the public origin. It
   checks `/live`, `/ready`, `/health`, the frontend root, and the exact HTTPS Terms
   and Privacy URLs. To exercise a real invited pilot account as well, run it with
   `STAGING_SMOKE_EMAIL` and `STAGING_SMOKE_PASSWORD`; the credentials stay in the
   temporary cookie check and are never printed. Follow this with sign-in,
   write-confirmation, upload, SSO callback, and both-replica checks, then perform a
   disposable restore drill before relying on backups.
5. Application-image rollback is a deliberate new release of a prior digest.
   Do **not** automatically downgrade a database; follow the restore runbook.

Docker Compose can start two replicas but is not Kubernetes: it cannot guarantee
zero-downtime rolling replacement. The workflow proves migration then both running
replicas and readiness; schedule maintenance capacity and test failure recovery.

## External prerequisites the repository cannot provide

Before a release can pass the workflow, an operator still has to provision the
staging host and supply: a DNS name and publicly trusted TLS certificate/key;
firewall rules exposing only 80/443; a dedicated SSH deploy account, pinned host
key, absolute release directory, and Docker Compose v2 with `jq`; registry push and
host pull credentials; PostgreSQL/Redis/object-storage capacity; a transactional
SMTP relay; a tested AI provider and embedding provider (plus their keys); a
separate versioned backup bucket; published Terms and Privacy pages; and the
GitHub staging environment secrets/variables listed above. The workflow does not
create any of those resources or mutate GitHub settings. `AI_PROVIDER` must be
explicit in the host `.env`; deterministic is allowed only for a deliberately
offline staging demo.
