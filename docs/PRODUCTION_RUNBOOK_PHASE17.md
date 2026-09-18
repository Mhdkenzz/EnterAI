# Production Runbooks — Phase 17 Launch Readiness

Repo: https://github.com/Mhdkenzz/EnterAI.git
Verified HEAD reference: d67ceb6193c8fd5f92d938f8b64eb34a444fcc70

## 1. Rollback Procedure
Reference: docs/RESTORE_RUNBOOK.md (existing)
Steps:
1. Confirm deployment SHA (git rev-parse HEAD).
2. Verify database migrations match the previous green SHA.
3. Execute rollback: redeploy previous image/container tag (see docker-compose.prod.yml / deploy/).
4. Confirm health endpoint responds before accepting traffic.
5. Re-run regression tests (pytest backend/tests) against rolled-back environment.
6. Verify audit events and identity provider settings intact after rollback.
7. Notify admin/user through notification channel.
Status: DOCUMENTED; not executed in production environment. BLOCKED: requires production deployment authorization.

## 2. Incident Response / On-Call Process
- Detection: CI failure alerts, monitoring metrics (docs/MONITORING.md), audit anomaly (enterprise audit aggregation route).
- Escalation: Human admin (user with role=admin) verifies issue; cross-org isolation ensures no data leakage.
- Recovery: Rollback procedure above + session retirement (user.session_epoch bump for compromised accounts) + SCIM deactivation for deprovisioned accounts.
- Communication: Admin audit events logged; incident details captured in AuditEvent table.
Status: DOCUMENTED; full on-call execution BLOCKED (requires production authorization/on-call roster).

## 3. Status / Uptime Process
- Current: docs/MONITORING.md exists; monitoring concepts documented.
- `GET /health` (as of 2026-09-18) checks database connectivity (`SELECT 1`) and, when
  `REDIS_URL` is configured, a Redis `PING`; returns 503 with per-check detail if either
  fails, instead of the previous unconditional `{"ok": true}`.
- Missing: a public status page, an external alerting sink (Sentry/PagerDuty/Slack
  webhook), and audit-aggregation-based anomaly detection. BLOCKED.
Status: PARTIAL — liveness/readiness now real; no external alerting or public status page yet.

## 4. Backup / Restore Procedure
Reference: docs/RESTORE_RUNBOOK.md (existing)
Status: DOCUMENTED; drill not executed in production environment (only verified locally with SQLite/test DB).

## 5. Production Secrets / Rotation Procedure
Reference: docs/SECRETS_MANAGEMENT.md (existing)
Status: DOCUMENTED; rotation executed only for JWT_SECRET (derived from environment). Client secrets stored as SHA-256 hashes; rotation requires new hash generation in identity_providers table.
BLOCKED: full rotation drill requires live IdP endpoint.

## 6. Deployment / Game-Day Drill
Status: DOCUMENTED; not executed. BLOCKED: requires production-like multi-replica environment (real Postgres + Redis + multiple replicas) and authorization.

## 7. Pilot / Paid Launch Readiness Gate (Summary)
- Pilot READY: NO (blocked: real IdP integration, Playwright/frontend UI, external scan)
- Paid READY: NO (blocked: multi-replica verification, full external scan, incident/on-call finalized, status endpoint)
- Enterprise READY: NO (blocked: real IdP production validation, external pen scan, full SCIM spec, production runbook execution)
- All critical/high security/regression findings from independent review RESOLVED (secret hash fixed, isolation verified, audit read-only, no mutation endpoints for audit, session retirement active).
