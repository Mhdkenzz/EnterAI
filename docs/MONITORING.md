# Minimum staging monitoring

The measurable pilot and paid-launch SLO targets, evidence fields, and alert
ownership gate are maintained in [`PRODUCTION_GO_NO_GO.md`](PRODUCTION_GO_NO_GO.md).
This page describes the signals that must feed those targets.

Use any approved monitoring system. For application errors the API speaks the
Sentry protocol when `SENTRY_DSN` is set -- Sentry itself or a self-hosted
GlitchTip -- with cookies, auth headers, IPs and request bodies stripped from every
event. It is off by default. Configure alerts for:

- HTTPS endpoint unavailable and public `/ready` non-200;
- API/nginx/container restart loops and sustained elevated 5xx responses;
- database or Redis unavailable, volume/disk pressure, and migration failures;
- backup job nonzero exit, missed expected backup interval, or failed off-host copy;
- scheduler/background-loop failures in structured logs;
- repeated AI-provider errors; and
- unusual authentication, password-reset, or SSO callback failure volume.

Probe `/live` for process liveness and `/ready` for routing readiness. `/health`
remains a compatibility alias for `/ready`. Readiness checks PostgreSQL, required
Redis, and the current Alembic heads; it intentionally does not make external AI
provider availability a base-application outage. Object-storage upload smoke tests
belong in release verification and restore drills.

Retain structured logs long enough to investigate authentication and deployment
events, redact credentials/tokens, and page a human for backup or migration failure.
Test alert delivery and a restore drill before declaring staging operational.
