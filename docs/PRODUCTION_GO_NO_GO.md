# Production go / no-go package

This is the decision record template for moving Enter AI from the Phase 3 release
baseline through the Phase 7 pilot into a broader production launch. It is an
evidence gate, not a claim that external staging, security, identity, billing,
or recovery work has already happened. Complete it against one exact commit and
the immutable backend/frontend image digests built from that commit.

The detailed operational verification steps are in
`docs/PHASE5_VERIFICATION_RUNBOOK.md`. The deployment contract is in
`docs/DEPLOY.md`; backup recovery is in `docs/RESTORE_RUNBOOK.md`; monitoring
expectations are in `docs/MONITORING.md`; the invited-cohort operating model is
in `docs/PILOT_OPERATIONS.md`.

## Decision rule

The release is **GO** only when every required gate below has a named owner,
fresh evidence, and an explicit result. A missing external dependency is
**BLOCKED**, not passed by inference from a local test or CI run. A **NO-GO** is
required for any critical/high security finding, tenant-data exposure, failed
restore, unconfirmed AI write, unreconciled migration, failed rollback, or
unowned incident path.

The decision applies separately to these scopes:

| Scope | Permitted before broader launch approval | Additional required evidence |
| --- | --- | --- |
| Internal demo | Disposable or non-sensitive data | Local/CI checks and an operator present |
| Invited pilot | Limited users and workspaces, no sensitive data by default | Phase 5 staging evidence, recovery drill, support owner, security gate |
| Paid production | Customer data and billing | Measured SLOs, live Stripe sandbox, external security review, game-day, rollback evidence |
| Enterprise / SSO | Enterprise workspaces and provisioning | Real IdP validation, SCIM contract evidence, secret rotation, security sign-off |

## Required release identity

```text
Decision scope: INTERNAL / INVITED PILOT / PAID / ENTERPRISE
Release SHA:
CI run URL:
Backend image digest:
Frontend image digest:
Database migration head:
Staging URL:
Decision owner:
Security approver:
Operations approver:
Decision timestamp (UTC):
Decision: GO / GO WITH WRITTEN LIMITS / NO-GO / BLOCKED
Rollback target SHA and image digests:
Evidence index or ticket:
```

## Gate 1 — release and regression evidence

Required evidence for the selected SHA:

- all required CI jobs are green on that same SHA, including backend tests,
  frontend type checks/build, browser/axe, security/load checks, PostgreSQL
  migration checks, production-shaped container validation, and the multi-replica
  job;
- the final diff has been reviewed, `git diff --check` is clean, and no
  untracked release-critical file is omitted from the image or bundle;
- the production image metadata identifies the exact SHA and immutable digest;
- migrations upgrade a fresh database and the deployed database reports exactly
  the image's Alembic head;
- the deployed smoke verifies `/live`, `/ready`, `/health`, HTTPS, legal pages,
  invite-only admission, login, `/api/me`, project listing, and logout;
- the Phase 5 browser/API journeys pass for invitation, terms consent, reset,
  uploads, feedback scoping, AI confirmation, and replay rejection.

Record CI URLs, test artifact names, image digests, migration output, and the
staging smoke output. “Tests exist” is not evidence that the selected release ran
them.

## Gate 2 — measurable reliability and performance

These are launch targets to measure and approve; they are not current service
claims. Run the load test against production-shaped Postgres, Redis, object
storage, nginx, and two API replicas with representative authenticated traffic.
Exclude health probes from user latency measurements and report errors by route.

| Measure | Invited pilot target | Paid production target | Evidence |
| --- | ---: | ---: | --- |
| Monthly HTTPS availability | >=99.5% | >=99.9% | Monitor export over the agreed window |
| `/ready` response p95 / p99 | <500ms / <1s | <300ms / <750ms | 30-minute steady load plus failure window |
| Authenticated CRUD p95 | <1s | <750ms | k6/HTTP report with route breakdown |
| 5MB upload acknowledgement p95 | <3s | <2s | Object-store upload report |
| Non-AI API 5xx rate | <0.5% | <0.1% | Access/error metrics |
| AI plan timeout/error rate | <2% | <1% | Provider and API metrics, excluding intentional fault tests |
| Database backup RPO | <=24h | <=1h | Archive timestamps and restore record |
| Service RTO after API failure | <=30min | <=15min | Game-day timeline |
| Full host loss RTO/RPO | Explicitly limited | Must meet approved contract | Host-failure design and drill |

The current Compose topology runs two API replicas on one host. It improves
process/container failure tolerance but does not provide host-level high
availability. Until a second host or equivalent managed failover is proven, the
availability target must exclude host loss and the decision record must state that
limit. Do not advertise a host-failure SLA from this topology.

For each load run record request rate, concurrency, dataset shape, provider mode,
CPU/memory, database pool settings, Redis health, p50/p95/p99 latency, error
rate, queue depth, and the exact image digests. A five-user in-memory simulation
does not satisfy this gate.

## Gate 3 — security review and external testing

Required before paid or enterprise launch:

- an independent review of authentication, cookie/CSRF behavior, tenant
  isolation, RBAC/SCIM, OIDC state/nonce/PKCE, confirmation replay/races,
  uploads/object paths, webhooks, rate limits, logging, and backup access;
- dependency/SAST and container scans from the final image, with manifests and
  tool versions retained;
- an authorized DAST/penetration test against staging with the scope, dates,
  source IPs, exclusions, and test account recorded;
- zero open critical or high findings, and an owner, due date, mitigation, and
  risk acceptance for every remaining medium/low finding;
- a retest report showing that every critical/high finding is closed on the exact
  release candidate.

Local tests and a CI security job do not replace an external penetration test.
Never run destructive testing against production or a customer workspace.

## Gate 4 — identity and billing boundaries

OIDC remains blocked for enterprise launch until a real non-production Okta,
Entra, or Google tenant proves discovery, authorization-code exchange, PKCE,
state/nonce validation, issuer/audience checks, domain ownership, JIT linking,
repeat login, logout, and a rejected callback. Capture redacted request IDs and
provider configuration labels; never record tokens or client secrets. SAML is not
implemented and must not be included in the product promise.

Stripe billing remains blocked for paid launch until a real Stripe test-mode
customer/subscription, successful payment, failure, cancellation, plan limit,
webhook signature, duplicate delivery, and out-of-order event sequence are
observed end to end. Keep billing disabled when this evidence is absent.

SCIM requires a separate service token, verified domain, role mapping, deactivate
and reactivation evidence, and a written decision on the currently unsupported
`remove` PATCH operation before enterprise scope is approved.

## Gate 5 — secrets and configuration rotation

Before launch, record the owner and expiry/rotation path for JWT signing,
encryption keys, OIDC client secrets, SMTP credentials, storage credentials,
registry/deploy credentials, database credentials, Redis credentials, Stripe
secrets, and monitoring DSNs.

Exercise in a disposable or staging environment:

1. stage a new secret and retain the previous value only in the supported
   previous-key slot;
2. restart or redeploy through the normal release path;
3. verify existing sessions, OIDC secret use, SMTP delivery, object upload,
   backup upload, Stripe webhook verification, and monitoring delivery according
   to the secret changed;
4. remove the previous value, verify old credentials fail, and retain redacted
   audit evidence;
5. document emergency revocation and who can execute it.

Do not declare rotation complete from documentation alone. If a component cannot
rotate without downtime, record the maintenance window and customer impact.

## Gate 6 — backup, restore, rollback, and game-day

The release owner must retain one matching database archive and object-store
snapshot/version. A disposable restore must prove:

- `pg_restore --list` succeeds and the archive is non-empty;
- the database restores to the selected migration head;
- uploaded document/attachment rows resolve to restored object keys;
- a harmless login, project read, and document read succeed in the restored
  environment;
- measured RTO/RPO meet the scope target.

Rollback evidence must show the exact previous digest, migration compatibility
decision, deployment start/end, `/ready` recovery, browser smoke, audit/session
integrity, and the decision to roll forward or remain rolled back. Never
automatically downgrade a database migration.

Run one authorized game-day that covers an API replica loss, Redis or database
dependency loss, provider timeout, backup failure alert, and recovery. Record
trigger, detection, page time, owner acknowledgement, mitigation, recovery,
customer impact, and follow-up. The one-host limitation means a host-loss drill
must either be performed in the real failover design or remain explicitly
BLOCKED.

## Gate 7 — monitoring, on-call, and incident ownership

Before broader launch, name a primary and secondary on-call owner, timezone and
coverage hours, escalation contact, deployment owner, security incident owner,
database/backup owner, and customer communications owner. Each alert needs a
runbook link, severity, acknowledgement target, mitigation target, and recovery
notification.

Minimum alert tests:

- public HTTPS or `/ready` failure;
- API/container restart loop and sustained 5xx;
- PostgreSQL/Redis failure or disk pressure;
- missed/failed database or object backup;
- repeated AI-provider failures;
- unusual authentication, recovery, or OIDC callback failures.

Record alert delivery to the actual approved sink and human recipient. A Sentry
DSN, log line, or dashboard without a delivered alert is not proof. A public
status page is optional for an invited pilot only when a human status process is
documented; it is required by the paid SLA if that SLA promises public status.

## Launch decision record

```text
Gate 1 release/regression: PASS / FAIL / BLOCKED — owner/evidence:
Gate 2 SLO/performance: PASS / FAIL / BLOCKED — owner/evidence:
Gate 3 security/pen test: PASS / FAIL / BLOCKED — owner/evidence:
Gate 4 OIDC/Stripe/SCIM scope: PASS / FAIL / BLOCKED — owner/evidence:
Gate 5 secret rotation: PASS / FAIL / BLOCKED — owner/evidence:
Gate 6 backup/restore/rollback/game-day: PASS / FAIL / BLOCKED — owner/evidence:
Gate 7 monitoring/on-call: PASS / FAIL / BLOCKED — owner/evidence:

Approved scope:
Explicit launch limits:
Open findings and risk acceptances:
Rollback target:
Primary on-call:
Secondary on-call:
Decision owner:
Security approval:
Operations approval:
Decision: GO / GO WITH WRITTEN LIMITS / NO-GO / BLOCKED
Decision date (UTC):
Next review date:
```

## Current external gates

Based on the repository evidence available before a live deployment, these items
remain external gates and must not be marked complete from code inspection alone:

- real staging HTTPS smoke with invited user, terms, feedback, uploads, and
  confirmation/replay evidence;
- sustained load/SLO measurements using real Postgres/Redis/object storage and
  the two-replica topology;
- authorized external penetration test and retest;
- real OIDC tenant verification;
- real Stripe test-mode end-to-end billing verification;
- SMTP delivery and credential rotation;
- off-site database/object restore with measured RTO/RPO;
- replica/dependency/backup game-day and delivered alert tests;
- named on-call coverage and incident ownership;
- a host-resilience decision or multi-host failover evidence.

Until the applicable scope’s gates are evidenced, the honest decision is
`BLOCKED` or `NO-GO`, with the product limited to the narrower scope stated in
the decision record.
