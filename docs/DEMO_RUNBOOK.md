# Demo-user runbook

This guide is for a small, invited demonstration. It is not a public-production
launch checklist.

## Before inviting anyone

1. Run the backend tests and the frontend production build from the release
   checkout. Do not demo an uncommitted working tree.
2. Use `ENVIRONMENT=staging`, never `development`, for any demo reachable by
   other people. The application will then reject placeholder secrets and unsafe
   host/CORS configuration at startup.
3. Set a unique `JWT_SECRET`, a dedicated PostgreSQL database, Redis, SMTP, and
   S3-compatible storage. Keep credentials outside Git in the host environment
   or a secrets manager.
4. Give `CORS_ALLOWED_ORIGINS`, `TRUSTED_HOSTS`, `APP_BASE_URL`, and
   `API_BASE_URL` the exact HTTPS demo addresses. The web app and API must share
   the same registrable domain for the session cookie to work.
5. Use invited accounts only. Keep the demo workspace separate from real customer
   data, Stripe live mode, and production identity providers.

## Demo defaults

- Leave `AI_PROVIDER=deterministic` unless a chosen provider has been tested with
  structured tool calling. Copilot writes always require the visible confirmation
  step; do not bypass it.
- Keep `ENABLE_API_DOCS=false` and use the supplied reverse proxy/TLS setup.
- Back up the demo database before each session. The supplied backup script checks
  archive integrity but does not encrypt dumps; store the destination on encrypted
  storage.
- Treat uploaded documents as demo data. Use the S3 adapter, not local single-node
  storage, when running more than one API replica.

## Stop conditions

Do not expand this demo into a paid or public service until a real OIDC tenant,
Stripe sandbox flow, external security scan, sustained multi-replica load test,
restore drill, and on-call/incident process have been completed.
