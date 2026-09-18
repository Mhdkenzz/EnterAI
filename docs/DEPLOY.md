# Deployment

## Current state (honest)
- `.github/workflows/deploy.yml` `build-images` job: **real**, runs on every push to
  `main`. It builds the backend and frontend Docker images from their Dockerfiles.
  A broken Dockerfile or an uninstallable dependency fails this job.
- `deploy` job: **real but conditional**. It only runs if `DEPLOY_HOST`,
  `DEPLOY_SSH_USER`, and `DEPLOY_SSH_KEY` are configured as repository secrets. As of
  this writing, `gh secret list` on this repo returns nothing -- **no deployment
  target is configured, and this job has never run against a real host.** Its logic
  (SSH in, `git checkout` the new SHA, `docker compose up -d --build`, health-check,
  roll back to the previously-recorded SHA on failure) is written and syntax-checked,
  not validated end to end. Do not report it as a working deploy pipeline until it
  has actually deployed something.
- `no-target-configured` job: runs instead, and says so explicitly in the Actions log
  rather than silently succeeding.

## Manual deployment (the real procedure today)
Until `DEPLOY_HOST`/`DEPLOY_SSH_USER`/`DEPLOY_SSH_KEY` are configured:

1. Provision a host with Docker + Docker Compose v2 installed.
2. Copy `.env.example` to `.env` on that host and fill in every value marked
   required (JWT_SECRET, DATABASE_URL or the `POSTGRES_*` vars, SEED_ADMIN_*,
   STORAGE_*, SMTP_*, APP_BASE_URL, and — as of Phase 14.2 — `API_BASE_URL` set to
   this host's own externally-reachable URL, and `SECRETS_ENCRYPTION_KEY` if OIDC
   login is in use). Generate real random values; never reuse anything from
   `.env.example` or `docker-compose.yml` verbatim (production refuses to boot on
   the placeholder JWT_SECRET; do the same for every other secret by policy, not
   because the app enforces it for all of them).
3. `git clone` (or `git pull` on an existing checkout) this repo at the commit you
   intend to run.
4. `docker compose -f docker-compose.prod.yml up -d --build`
5. Confirm `curl -fsS https://<host>/health` returns `{"ok": true, "db": "ok", ...}`.
6. Run migrations happen automatically as the compose stack's one-shot `migrate`
   service; confirm it exited 0 (`docker compose -f docker-compose.prod.yml ps
   migrate`) before trusting step 5.

## Rollback (manual)
1. `git checkout <previous-known-good-sha>` on the host.
2. `docker compose -f docker-compose.prod.yml up -d --build`
3. Re-check `/health`.
4. If the database schema itself needs to roll back (not just the application code),
   see `docs/RESTORE_RUNBOOK.md` -- that is a separate, more disruptive operation and
   should not be the default response to a bad deploy.

## To make the automated `deploy` job real
Set these repository secrets, then push to `main` (or run the workflow manually):
- `DEPLOY_HOST` — the target host's address
- `DEPLOY_SSH_USER`, `DEPLOY_SSH_KEY` — SSH access to it (a deploy-only key, not a
  personal one)
- `DEPLOY_PATH` (optional, defaults to `/opt/enterai`) — where the repo is checked
  out on the host
- `DEPLOY_HEALTH_URL` (optional, defaults to `https://$DEPLOY_HOST/health`)

The first real run against a live host should be treated as a test, not an
assumption: watch it, confirm the health check and (by deliberately breaking
something) the rollback path both actually work, before relying on it unattended.
