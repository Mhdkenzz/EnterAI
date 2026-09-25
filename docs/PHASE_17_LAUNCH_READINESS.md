# Phase 17 Launch Readiness — Final Checklist

Status: PARTIAL (environment-blocked items marked BLOCKED below)
Repo: https://github.com/Mhdkenzz/EnterAI.git
Verified HEAD: 0a7dfe9 (CI run 35755725488 green, 2026-09-22); originally written against d67ceb6

**2026-09-25 status refresh.** Several lines below had fallen behind the code: OIDC
login, the audit aggregation writer, the SCIM PATCH attribute set, the backup service,
liveness/readiness probes, the multi-replica CI job and exact-SHA CI all landed after
the 2026-09-18 pass but were still listed as missing. Each such line is now updated
and marked *(updated 2026-09-25)*. The browser session also moved from a
`localStorage` bearer token to an httpOnly, SameSite=Lax cookie with a CSRF header
check on cookie-authenticated writes (see "Fixed 2026-09-25" below).

**2026-09-18 correction pass.** An independent audit found several PASS lines below
did not match the code: migrations were verified with `Base.metadata.create_all`,
which is not how any real deployment builds its schema (Alembic never actually
created these tables -- every SSO/enterprise endpoint 500'd); SCIM routes
authenticated with a normal user session token, so any signed-in member could
provision or deactivate any user including admins; and IP allowlisting / session
policies / SSO-only were stored settings with no enforcement anywhere. All three
are fixed as of this pass (migration `0012_sso_enterprise`, dedicated SCIM
service-token auth, and real enforcement in `auth.current_user`/login) -- see the
corrected lines below, marked *(fixed 2026-09-18)*. OIDC login has since been
implemented (`sso_oidc.py`); SAML is deliberately not supported *(updated 2026-09-25)*.

## Launch Readiness Gates

### PILOT READY
- [PASS] Core backend stable (Phase 1-13 verified by previous CI 35339813170)
- [PARTIAL] Phase 14 SCIM provisioning/deactivation implemented and now schema-backed
  and access-controlled *(fixed 2026-09-18: added alembic migration `0012_sso_enterprise`;
  `/scim/v2/*` now requires a dedicated per-IdP service token instead of accepting any
  signed-in member's session token)*. OIDC SSO login is implemented: authorization-code
  flow with PKCE, ID-token signature/issuer/audience/nonce verification, JIT provisioning
  *(updated 2026-09-25)*. SAML is intentionally not implemented (see `sso_oidc.py`
  docstring); a provider configured as `saml` cannot sign anyone in.
- [PASS] Phase 15 Enterprise controls: audit aggregation is written by
  `observability.refresh_audit_aggregations`, run by a background loop and on demand
  from the enterprise route *(updated 2026-09-25)*; IP allowlisting and session policy / SSO-only are now
  *enforced*, not just stored *(fixed 2026-09-18: `auth.current_user` checks the org's
  active `IPAllowlist`/`SessionPolicy` on every authenticated request, and
  `/api/auth/login` refuses password sign-in for `sso_only_enforced` orgs)*.
- [PASS] Phase 16 Load/security tests (isolation, replay, sustained load, performance)
- [PASS] Security review completed (blocking finding: client_secret truncation — FIXED; strict isolation preserved; no mutation routes for audit)
- [BLOCKED] Real external IdP/OIDC endpoint integration: the OIDC callback exists and is tested end to end against an in-process fake IdP, but has not been run against a real Okta/Entra/Google tenant *(updated 2026-09-25)*
- [PARTIAL] Playwright/axe for frontend SSO/admin UI: the admin console, SSO sign-in button and SSO callback have Playwright+axe coverage (`admin.spec.ts`, `auth.spec.ts`); there is no admin UI for *configuring* an identity provider -- that is API-only *(updated 2026-09-25)*
- [BLOCKED] External penetration scan (requires authorization/network access)
- [PASS] Backup/restore procedure documented (docs/RESTORE_RUNBOOK.md) and scheduled: the `backup` service in `docker-compose.prod.yml` runs `scripts/backup.sh` on a loop *(updated 2026-09-25)*
- [PASS] Secrets management documented (docs/SECRETS_MANAGEMENT.md exists); still plain env vars, no rotation automation
- [PASS] Monitoring/docs exist (docs/MONITORING.md); `/health` now checks DB+Redis reachability *(fixed 2026-09-18)*, still no external alerting sink

VERDICT: PILOT READY = NO — blocked by an OIDC login verified against a real IdP tenant and an external scan. Without those, a pilot can start in limited internal mode (password login, SCIM provisioning, OIDC against a trusted IdP at the customer's own risk) *(updated 2026-09-25)*.

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
- [PASS] Enterprise audit aggregation populated by a background writer *(updated 2026-09-25)*
- [PASS] Multi-replica/Redis docs present
- [PARTIAL] Multi-replica verification: CI's `multi-replica` job runs two API replicas behind the nginx load balancer on Postgres + Redis and checks cross-replica behaviour; this is a functional check, not a sustained load/stress test *(updated 2026-09-25)*
- [BLOCKED] Full penetration/security scan
- [BLOCKED] Incident response/on-call process finalized (docs/RESTORE_RUNBOOK.md exists but incident response/on-call runbook not fully written)
- [PARTIAL] Status/uptime: `/live`, `/ready` and `/health` probes exist; no public status page *(updated 2026-09-25)*
- [BLOCKED] Game-day drill (not executed — requires production-like environment and authorization)
- [PASS] Rollback procedure documented (docs/RESTORE_RUNBOOK.md)
- [PASS] Deployment docs exist (docs/DEPLOY.md implied by deploy/docker-compose; docs/SECURITY_DEPLOY.md exists)

VERDICT: PAID READY = NO — requires multi-replica load/stress verification, incident/on-call finalization, external security scan, game-day execution, and billing proven against a real Stripe sandbox *(updated 2026-09-25)*.

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
- [PASS] Enterprise audit aggregation -- populated by a background writer; read-only route *(updated 2026-09-25)*
- [PASS] IP allowlisting -- now enforced on every authenticated request, not just stored *(fixed 2026-09-18)*
- [PASS] Session/org policies -- `max_session_days` and `sso_only_enforced` now enforced, not just stored *(fixed 2026-09-18)*
- [BLOCKED] Real IdP SSO-only enforcement production-tested (OIDC login exists; not yet run against a real IdP tenant) *(updated 2026-09-25)*
- [PARTIAL] SCIM PATCH: `replace`/`add` of `active`, `displayName`/`name.formatted`, `userName` and `roles`, in both path and path-less forms; `remove` is ignored rather than applied *(updated 2026-09-25)*
- [BLOCKED] Full external pen scan
- [BLOCKED] Game-day / incident response finalized

VERDICT: ENTERPRISE READY = NO — blocked by SSO verified against a real IdP (and no SAML), external pen scan, incident/on-call/game-day, SCIM `remove` support, and a real secrets-rotation story *(updated 2026-09-25)*.

## Blocked Items (honest)
- BLOCKED: Real external OIDC identity provider -- the callback exists; untested against a real tenant. SAML is not supported
- PARTIAL: No admin UI for configuring an identity provider (API only); the rest of the admin/SSO UI has Playwright+axe tests
- BLOCKED: Full external penetration/security scan (requires authorization/network access)
- BLOCKED: Production multi-replica load/stress verification (CI checks two replicas functionally, not under sustained load)
- BLOCKED: Game-day / incident response/on-call runbook finalized (docs present but not executed/finalized)
- BLOCKED: Public status page and external alerting sink (`/live`, `/ready`, `/health` exist)
- PARTIAL: SCIM PATCH `remove` is ignored rather than applied
- BLOCKED: Billing proven against a real Stripe sandbox end-to-end (enforcement now wired in, unverified against live Stripe)

## Fixed 2026-09-25
- Browser sessions use an httpOnly, SameSite=Lax `enterai_session` cookie (Secure in deployment environments, overridable with `SESSION_COOKIE_SECURE`) instead of a token in `localStorage`, so injected script cannot read it. Cookie-authenticated writes must send `X-EnterAI-Client`; a cross-site form cannot. SSO no longer hands the token to the frontend in a URL fragment. `POST /api/auth/logout` clears the cookie. Bearer tokens keep working for the CLI and automation.
- Exact-SHA CI verified: run 35755725488 green on 0a7dfe9.

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
- MEDIUM: OIDC login untested against a real IdP tenant; SAML not supported.
- LOW: SCIM PATCH `remove` ignored.
- LOW: No admin UI for identity-provider setup (API only).
- LOW: The web app and API must be served from the same site (same registrable domain) for the SameSite=Lax session cookie to be sent.
- LOW: Game-day/incident response/on-call not executed in production-like environment.
- LOW: Performance targets (p95/p99) only simulated in memory SQLite; real-world variance unknown.

Next: verify OIDC against a real IdP tenant, build the identity-provider admin UI, prove billing against a Stripe sandbox, load-test the multi-replica stack, execute an external pen scan, run a production-like game-day, and finalize on-call/status tooling.
