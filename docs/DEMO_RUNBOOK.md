# Demo-user runbook

This guide is for a small, invited demonstration. It is not a public-production
launch checklist. For the complete 5–10 user operating model, use
[`docs/PILOT_OPERATIONS.md`](PILOT_OPERATIONS.md); this page is the short preflight
and handoff checklist.

## Before inviting anyone

1. Run the backend tests and frontend production build from the release checkout.
   Do not demo an uncommitted working tree.
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

6. Create a restricted cohort register before sending any invitation. Record
   pseudonymous user/workspace IDs, role, invitation and consent timestamps, and
   removal status; keep email addresses and credentials in the approved operator
   system only.

7. Name a pilot owner, support owner, release owner, workspace admin, and
   security/privacy contact. The pilot owner signs the preflight record and owns
   pause/resume and expansion decisions.

8. Confirm the deployed release has passed the public smoke, invite/terms/session,
   backup/restore, object-storage, and alert checks in
   [`docs/PHASE5_VERIFICATION_RUNBOOK.md`](PHASE5_VERIFICATION_RUNBOOK.md). A local
   test or CI result is not evidence of a real staging journey.

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
- Keep the in-product **Report a problem** action visible to every invited user.
  Reports are scoped to the submitting organization, rate-limited, and contain
  only the normalized page, client version, environment, and release identifier
  as diagnostics. Ask users not to paste passwords, tokens, cookies, prompts, or
  customer data; the API redacts common credential assignments before storage.

## Feedback triage

Workspace administrators review **Admin → Demo feedback**. Use this ownership
flow for each report:

1. Acknowledge a new report and assign it to one active human owner in the same
   workspace.
2. Move it to **investigating** while reproducing it against the recorded release
   and page. Keep reproduction notes free of secrets and customer content.
3. Move it to **resolved** only after the reporter-visible behavior is verified;
   use **closed** when the reporter confirms the outcome or the issue is otherwise
   dispositioned. Leave an unassigned report visible until its owner is explicit.

The triage API is organization-scoped and returns no reports from another
workspace. Platform operators still need an external escalation channel for
cross-workspace incidents, security disclosures, and attachments/logs that must
not be entered in the feedback form.

## Stop conditions

Pause immediately for suspected tenant-data exposure, credential/session
compromise, data loss, an unintended or replayed AI write, failed backup/restore,
unhealthy readiness, migration drift, or an unresolved sign-in/invite failure.
Keep the affected evidence redacted, stop invitations, and follow the severity and
resume gates in `docs/PILOT_OPERATIONS.md`. Never restore over active staging or
automatically downgrade a database during rollback.

Do not expand beyond the initial 5–10 users until seven days of daily records,
zero open S0/S1 findings, passed deployed user journeys, current backup/object
restore evidence, tested alert delivery, and an explicit owner decision are
recorded. Do not expand this demo into a paid or public service until the real
OIDC tenant, Stripe sandbox flow, external security scan, sustained
multi-replica load test, restore drill, and on-call/incident process are complete.

## Daily operator handoff

At the start of each active day, check `/ready`, error/restart trends, backup and
object-mirror age, provider failures, open reports, and cohort membership. After
each session, record only workflow results, safe disposable markers, timestamps,
and report IDs. At the end of the day, classify reports, assign owners/due dates,
confirm the next backup signal, and sign the daily record. Use the copyable record
in `docs/PILOT_OPERATIONS.md`; do not put passwords, cookies, invite/reset tokens,
private documents, or raw request headers in it.
