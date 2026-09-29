# Invited pilot operations

This runbook governs a small Enter AI pilot after the staging release has passed
the release and verification gates. The default cohort is 5–10 people across 2–3
separate workspaces, observed for at least seven calendar days. The pilot owner
may pause invitations or any affected feature at any time; expansion is a measured
decision, not an automatic consequence of a green deployment.

## Operating roles

Name one person for each role before the first invitation. One person may hold
more than one role, but the pilot owner must have a reachable backup.

| Role | Responsibility |
| --- | --- |
| Pilot owner | Go/no-go, cohort size, stop/resume decisions, daily review sign-off |
| Support owner | Receives reports, acknowledges the reporter, reproduces issues, keeps the triage queue current |
| Release owner | Records SHA/image digests, deployment and rollback actions, backup/restore evidence |
| Workspace admin | Invites only approved addresses, confirms workspace membership and roles, removes access when requested |
| Security/privacy contact | Handles suspected tenant exposure, credentials, personal-data requests, and retention/deletion decisions |

Do not use a pilot workspace as a customer support inbox. Keep credentials and
secrets in the approved secret store, and keep evidence in the release ticket or
restricted operations location named in the evidence record.

## Cohort control

Create a restricted cohort register before sending an invitation. Use an internal
identifier instead of an email address in shared evidence.

```text
Pilot ID:
Pilot owner / backup:
Start date (UTC):
Planned end date (UTC):
Release SHA / backend digest / frontend digest:
Staging base URL:
Approved data class: disposable demo data only / other approved class:

| User ID | Workspace ID | Role | Invite sent (UTC) | Accepted (UTC) | Terms version | Last active (UTC) | Removal due/complete |
|---------|--------------|------|-------------------|----------------|--------------|-------------------|---------------------|
|         |              |      |                   |                |              |                   |                     |
```

The workspace admin keeps the register current. Limit each workspace to the
approved users and role set. Do not add a new workspace, data class, integration,
or external identity provider without a new pilot-owner decision. If a person
leaves the cohort, revoke the invitation if unused or deactivate the account,
retire active sessions, and record completion without retaining their email in
general evidence.

## Before the first invitation

The release owner records the exact release and confirms the Phase 5 verification
record is complete enough for the pilot. At minimum, confirm:

1. Public HTTPS `/live`, `/ready`, `/health`, frontend, Terms, and Privacy checks
   pass. `/ready` reports healthy PostgreSQL, Redis, and migration heads.
2. Registration is invite-only. A disposable registration probe returns the
   expected rejection, and the invitation path has passed with terms consent.
3. SMTP delivery, password recovery, object storage, and the selected AI provider
   have been tested against this staging release. Deterministic AI is allowed only
   when the pilot owner explicitly records that the pilot is an offline AI demo.
4. The most recent database archive is non-empty and verified; object mirroring is
   current; the disposable restore drill has a recorded result.
5. Monitoring and alert recipients are known. A human owns the first response for
   readiness, authentication, backup, data-integrity, and provider alerts.
6. The feedback route is usable. Users are told to report category, impact, page,
   steps to reproduce, and the disposable data marker. They must not include
   passwords, tokens, private documents, or raw request headers.

The pilot owner signs the preflight record before any invitation is sent.

## Onboarding and consent

Use one clean browser profile per test user for the first journey. The workspace
admin sends the invitation to an approved address and confirms that the recipient,
workspace, role, expiry, and public HTTPS link are correct. The user then:

1. Opens the link from the intended mailbox and confirms the workspace and role.
2. Reads the published Terms and Privacy pages. Submitting without required terms
   consent must be rejected; accepting records the current terms version and time.
3. Creates a unique password, signs in, and confirms `/api/me` shows only the
   invited workspace. The user must not use a shared admin credential.
4. Creates or uses only harmless disposable data, such as `pilot-<id>` markers.
   Do not upload customer records, secrets, regulated data, or irreplaceable work.
5. Signs out and back in. The support owner records the journey result and any
   request or report identifier, never a session cookie or invite/reset token.

If the recipient reports an unexpected workspace, role, legal prompt, or data
visibility, stop onboarding for that workspace and classify it as a potential S0
until the boundary is disproved.

## Daily operating cadence

The pilot owner reviews the same compact record at three points each active day.

**Start of day (10 minutes):** check public readiness, error/5xx trend, container
restarts, database/Redis status, backup age, object mirror age, provider errors,
open S0–S2 reports, and the cohort register. Confirm the release SHA has not
changed. If the backup or readiness signal is stale, pause new sessions until the
owner decides.

**After each test session:** record workflow results for sign-in, project/task
work, upload/readback, AI read-only planning, confirmation-gated write (if in
scope), feedback submission, and sign-out. Record counts and identifiers only;
never copy private documents or tokens into the log.

**End of day (15 minutes):** review new reports and support acknowledgements,
compare incidents with the stop conditions below, confirm one current backup
signal, and write a daily sign-off. The release owner records any rollback or
configuration change as a new release event.

Daily record:

```text
Date (UTC):                 Pilot ID:
Release SHA/digests:        Reviewer / backup:
Active users / workspaces:  New users / removed users:
Readiness: PASS / FAIL      Latest DB backup (UTC):
Object mirror: PASS / FAIL  Alert sink: PASS / FAIL / NOT TESTED

Workflow checks
- Invite + terms + sign-in: PASS / FAIL / NOT RUN
- Workspace and role isolation: PASS / FAIL / NOT RUN
- Project/task/comment flow: PASS / FAIL / NOT RUN
- Upload/readback: PASS / FAIL / NOT RUN
- AI read-only / confirmed write: PASS / FAIL / NOT IN SCOPE
- Feedback and support acknowledgement: PASS / FAIL / NOT RUN
- Sign-out/password recovery: PASS / FAIL / NOT RUN

New reports:       S0:  S1:  S2:  S3:
Open reports / owners / due dates:
Decision: CONTINUE / PAUSE FEATURE / PAUSE PILOT / ROLLBACK REVIEW
Evidence links and notes:
Owner sign-off (UTC):
```

## Feedback and triage

Use the in-product feedback route when available (`POST /api/feedback`). The
service stores an organization-scoped report and a deliberately small diagnostics
allow-list; it redacts common credential-shaped values, but users remain
responsible for keeping secrets and private content out of descriptions. An admin
reviews the organization-scoped queue at `GET /api/admin/feedback`, assigns an
owner, and updates status through `PATCH /api/admin/feedback/{id}`. Keep the
report ID and release identifier in the evidence record, not the reporter's
private description.

Each report should answer: what happened, expected result, exact safe steps,
impact, page, disposable marker, approximate UTC time, and whether the issue is
repeatable. Support acknowledges receipt within one business day during the
pilot, or immediately for S0/S1. Acknowledge the reporter without promising a
fix date until the release owner has assessed the issue.

| Severity | Examples | Immediate action | Resume/close gate |
| --- | --- | --- | --- |
| S0 critical | Cross-workspace data exposure, credential/session compromise, data loss, an unconfirmed AI write, or unsafe confirmation replay | Stop all pilot activity; revoke affected sessions/invites; preserve redacted evidence; notify security/privacy and pilot owner | Security/privacy owner confirms containment and release owner verifies a fixed release and regression evidence |
| S1 high | Sign-in or invite flow unavailable, repeated failed writes, restore/backup failure, both replicas unhealthy, or a core workflow corrupts data | Pause affected workspace/feature and new invitations; assess rollback and communicate to active users | Reproduction is understood, mitigation is tested, and owner approves resume |
| S2 medium | Degraded performance, intermittent provider errors, broken non-core workflow, or feedback queue breach | Assign an owner and due date; continue only if no boundary or integrity risk | Fix or documented workaround verified in staging |
| S3 low | Cosmetic, copy, or minor usability issue with a safe workaround | Record and batch for review | Owner accepts or fix is verified |

Do not downgrade a report merely because it is hard to reproduce. A suspected
tenant boundary, data integrity, credential, or AI confirmation issue stays S0/S1
until evidence rules it out. Do not include raw prompts, uploaded files, cookies,
authorization headers, invite/reset links, or provider keys in a report.

## Stop, rollback, and resume

Pause the pilot immediately for any S0, any unresolved S1, stale/failed backups,
an unhealthy readiness probe, unexplained migration drift, authentication or
tenant-boundary anomaly, repeated unintended AI write, or a provider outage that
blocks a promised workflow. Pause only the affected feature for a contained S2
when the pilot owner and security/privacy contact agree that data and access
boundaries remain safe.

During a pause, stop invitations, tell active users which workflow is unavailable,
preserve timestamps and redacted identifiers, and avoid destructive cleanup. The
release owner may roll back to a prior immutable application image only after
checking migration compatibility and following `docs/RESTORE_RUNBOOK.md`; never
automatically downgrade the database. Resume requires an owner decision, a green
readiness/smoke check, updated triage records, and a targeted regression result.

## Expansion gate

The pilot owner may expand beyond the initial 5–10 users only when all of these
are true:

- at least seven days of daily records exist for the current release;
- no open S0/S1 and no unexplained cross-workspace, data-loss, credential, or AI
  confirmation finding exists;
- invitation, consent, authentication/recovery, core workspace, upload/readback,
  feedback, and sign-out journeys have each passed in the deployed environment;
- backups, object mirroring, disposable restore, and alert delivery have current
  evidence;
- observed support and incident load has an owner and remains manageable; and
- the owner has explicitly approved the next cohort size, workspace count, data
  class, and release SHA.

Expansion is blocked by missing evidence even if users report that the product
"feels fine." Record the decision and the next review date in the completion
record below.

## Pilot completion record

```text
Pilot ID / cohort size / workspace count:
Observed dates (UTC):
Release SHA / image digests:
Daily records completed (count):
Workflow pass counts / total sessions:
S0 / S1 / S2 / S3 opened, resolved, still open:
Backup / object mirror / restore evidence:
Alert delivery evidence:
Feedback queue owner and response summary:
Data deletion and access revocation completed (UTC):
Open findings, owner, due date:
Decision: CLOSE / EXTEND / EXPAND / PAUSE:
Pilot owner / security contact sign-off:
Next review date:
```

## External responsibilities

This runbook cannot create users, send invitations, approve a data class, supply
SMTP/AI/identity-provider credentials, configure alert recipients, perform a real
restore or failure drill, revoke access in an external identity provider, or make
a launch decision. Those actions require the named operators, external staging
resources, and the approved change window.
