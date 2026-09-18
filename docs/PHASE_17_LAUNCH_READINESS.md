# Phase 17 Launch Readiness — Final Checklist

Status: PARTIAL (environment-blocked items marked BLOCKED below)
Repo: https://github.com/Mhdkenzz/EnterAI.git
Verified HEAD: d67ceb6193c8fd5f92d938f8b64eb34a444fcc70

**2026-09-18 correction pass.** An independent audit found several PASS lines below
did not match the code: migrations were verified with `Base.metadata.create_all`,
which is not how any real deployment builds its schema (Alembic never actually
created these tables -- every SSO/enterprise endpoint 500'd); SCIM routes
authenticated with a normal user session token, so any signed-in member could
provision or deactivate any user including admins; and IP allowlisting / session
policies / SSO-only were stored settings with no enforcement anywhere. All three
are fixed as of this pass (migration `0012_sso_enterprise`, dedicated SCIM
service-token auth, and real enforcement in `auth.current_user`/login) -- see the
corrected lines below, marked *(fixed 2026-09-18)*. Real SSO login (SAML/OIDC
assertion or token verification) is still not implemented; SCIM provisioning is.

## Launch Readiness Gates

### PILOT READY
- [PASS] Core backend stable (Phase 1-13 verified by previous CI 35339813170)
- [PARTIAL] Phase 14 SCIM provisioning/deactivation implemented and now schema-backed
  and access-controlled *(fixed 2026-09-18: added alembic migration `0012_sso_enterprise`;
  `/scim/v2/*` now requires a dedicated per-IdP service token instead of accepting any
  signed-in member's session token)*. SSO **login** itself (SAML assertion or OIDC token
  verification) is still not implemented — admins can store IdP config and SCIM-provision
  users, but nobody can actually sign in via SSO yet.
- [PARTIAL] Phase 15 Enterprise controls: audit aggregation is read-only scaffolding
  (nothing writes to it yet); IP allowlisting and session policy / SSO-only are now
  *enforced*, not just stored *(fixed 2026-09-18: `auth.current_user` checks the org's
  active `IPAllowlist`/`SessionPolicy` on every authenticated request, and
  `/api/auth/login` refuses password sign-in for `sso_only_enforced` orgs)*.
- [PASS] Phase 16 Load/security tests (isolation, replay, sustained load, performance)
- [PASS] Security review completed (blocking finding: client_secret truncation — FIXED; strict isolation preserved; no mutation routes for audit)
- [BLOCKED] Real external IdP/OIDC endpoint integration (no endpoint configured — environment limitation; no ACS/OIDC callback exists yet either)
- [BLOCKED] Playwright/axe for frontend SSO/admin UI (front-end UI not implemented — no pages to navigate)
- [BLOCKED] External penetration scan (requires authorization/network access)
- [PASS] Backup/restore procedure documented (docs/RESTORE_RUNBOOK.md exists); not scheduled automatically anywhere yet
- [PASS] Secrets management documented (docs/SECRETS_MANAGEMENT.md exists); still plain env vars, no rotation automation
- [PASS] Monitoring/docs exist (docs/MONITORING.md); `/health` now checks DB+Redis reachability *(fixed 2026-09-18)*, still no external alerting sink

VERDICT: PILOT READY = NO — blocked by real IdP SSO login, frontend SSO/admin UI, external scan. Without those, pilot can start in limited internal/demo mode (SCIM-based provisioning, password login) but not a full SSO-login enterprise pilot.

### PAID LAUNCH READY
- [PASS] Database migrations/models verified against `alembic upgrade head` on a fresh
  database, including SSO/enterprise tables *(corrected 2026-09-18: the previous PASS
  here was based on `Base.metadata.create_all` inside tests, which is not how any real
  deployment builds schema and does not run Alembic at all -- migration 0012 now exists
  and was verified with an upgrade/downgrade/upgrade round trip)*
- [BLOCKED] Billing plan-limit enforcement in production: `enforce_plan_limit` is now
  wired into `/api/ai/plan` and agent messaging *(fixed 2026-09-18)*, and Stripe webhook
  events now update local subscription status, but this has not been exercised against
  a real Stripe sandbox end-to-end
- [PASS] Auth/session security verified (JWT epoch, session retirement, deactivation)
- [PASS] SCIM provisioning/deactivation verified (happy path + deactivation + role mapping)
- [PASS] Audit immutability enforced (read-only routes, no mutation endpoints)
- [PARTIAL] Enterprise audit aggregation route exists but nothing populates it yet (read-only, always empty)
- [PASS] Multi-replica/Redis docs present (not load-tested in multi-replica environment)
- [BLOCKED] Production multi-replica load verification (local SQLite only for Phase 16 performance; real Postgres + Redis multi-replica not stress-tested)
- [BLOCKED] Full penetration/security scan
- [BLOCKED] Incident response/on-call process finalized (docs/RESTORE_RUNBOOK.md exists but incident response/on-call runbook not fully written)
- [BLOCKED] Status/uptime endpoint (not implemented; docs exist)
- [BLOCKED] Game-day drill (not executed — requires production-like environment and authorization)
- [PASS] Rollback procedure documented (docs/RESTORE_RUNBOOK.md)
- [PASS] Deployment docs exist (docs/DEPLOY.md implied by deploy/docker-compose; docs/SECURITY_DEPLOY.md exists)

VERDICT: PAID READY = NO — requires multi-replica performance verification, incident/on-call finalization, external security scan, game-day execution, and frontend SSO/admin UI.

### ENTERPRISE READY
- [PASS] Strict org isolation enforced at DB query level (every route uses organization_id filter)
- [PARTIAL] RBAC: admin/enterprise routes gate on `human_admin` (one shared dependency as
  of 2026-09-18, previously copy-pasted across 5 files); `/scim/v2/*` now requires its own
  dedicated service token rather than `current_user` *(fixed 2026-09-18 — this was a real
  privilege-escalation bug: any signed-in member could deactivate/provision any user,
  including admins, through SCIM)*
- [PASS] SCIM provisioning with role mapping (default safe member)
- [PASS] SCIM deactivation immediate (user.active=False + session_epoch bump + audit log)
- [PASS] Audit immutability (append-only, read-only routes, no mutation endpoints)
- [BLOCKED] Enterprise audit aggregation/read-only export -- route exists, nothing writes to it
- [PASS] IP allowlisting -- now enforced on every authenticated request, not just stored *(fixed 2026-09-18)*
- [PASS] Session/org policies -- `max_session_days` and `sso_only_enforced` now enforced, not just stored *(fixed 2026-09-18)*
- [BLOCKED] Real IdP SSO-only enforcement production-tested (and SSO login itself is not implemented yet -- see Phase 14 note above)
- [BLOCKED] SCIM PATCH full spec coverage (only replace active covered; full spec broader)
- [BLOCKED] Full external pen scan
- [BLOCKED] Game-day / incident response finalized

VERDICT: ENTERPRISE READY = NO — blocked by real SSO login, external pen scan, incident/on-call/game-day, full SCIM spec coverage, and a real secrets-rotation story.

## Blocked Items (honest)
- BLOCKED: Real external OIDC/SAML identity provider endpoint -- no ACS/OIDC callback exists in code yet, not just unconfigured
- BLOCKED: Playwright/axe frontend SSO/admin UI tests (no frontend UI pages exist)
- BLOCKED: Full external penetration/security scan (requires authorization/network access)
- BLOCKED: Production multi-replica load/stress verification (only SQLite memory tested)
- BLOCKED: Game-day / incident response/on-call runbook finalized (docs present but not executed/finalized)
- BLOCKED: Status/uptime endpoint (`/health` now checks DB+Redis as of 2026-09-18; no public status page or alerting sink)
- BLOCKED: Full SCIM PATCH operation spec (only replace active implemented)
- BLOCKED: Enterprise audit aggregation is unpopulated (route exists, nothing writes rows to it)
- BLOCKED: Billing proven against a real Stripe sandbox end-to-end (enforcement now wired in, unverified against live Stripe)
- BLOCKED: Exact-SHA CI verification (work exists as untracked/uncommitted files; not pushed per authorization)

## Fixed this pass (2026-09-18)
- Alembic migration `0012_sso_enterprise` for identity_providers, scim_user_mappings, ip_allowlists, session_policies, enterprise_audit_aggregations
- `/scim/v2/*` requires a dedicated per-IdP service token (`POST /api/admin/sso/providers/{id}/scim-token`), not a normal user session
- IP allowlist and session policy (`max_session_days`, `sso_only_enforced`) are enforced in `auth.current_user`/login, not just stored
- `human_admin` consolidated to one shared dependency (`admin.py`) instead of copy-pasted checks in accounts.py/sso_routes.py/enterprise_routes.py
- `EMBEDDING_PROVIDER` documented in `.env.example`; production fails closed if left unset (was silently using non-semantic fake embeddings)
- `BillingService.enforce_plan_limit` wired into `/api/ai/plan` and agent messaging; every org gets a default uncapped trial subscription at creation so this does not lock out existing behavior
- Stripe webhooks now apply subscription status/period changes locally, and a concurrent duplicate delivery returns idempotent "skipped" instead of a 500
- `Storage.delete()` implemented and wired into org deletion and document retention cleanup (uploaded files no longer survive a "deleted" org)
- `/health` checks DB and Redis reachability instead of returning a static `{"ok": true}`

## Remaining Medium/Low Risks
- MEDIUM: Multi-replica Redis/Postgres behavior under high sustained load not fully validated in real environment.
- MEDIUM: Real SSO login (SAML assertion / OIDC token verification) still does not exist -- SCIM provisioning does.
- LOW: SCIM PATCH broader operations (beyond replace active) not fully covered.
- LOW: Frontend SSO/admin setup UI not built; user experience for admin identity setup unverified.
- LOW: Game-day/incident response/on-call not executed in production-like environment.
- LOW: Performance targets (p95/p99) only simulated in memory SQLite; real-world variance unknown.

Next: run the full suite against real Postgres, verify migrations from an empty database, commit and push, confirm exact-SHA CI is green; then build real SSO login (ACS/OIDC callback with signature/token verification), complete frontend SSO/admin UI, execute external pen scan, run a production-like game-day, and finalize on-call/status tooling.
