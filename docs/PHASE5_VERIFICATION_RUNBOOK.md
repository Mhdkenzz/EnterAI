# Phase 5 production-like verification

This runbook records evidence for an exact staging release. It complements the
automated CI jobs and is intended to be run against a real HTTPS staging host,
with a disposable pilot account and a disposable restore target. It does not
replace the release, backup, or incident runbooks.

## Evidence header

Create one copy of this header for every verification pass. Store the completed
record with the release ticket; never include passwords, session cookies, raw
invite/reset/OIDC tokens, provider keys, or private user documents.

```text
Release SHA:
Backend image digest:
Frontend image digest:
Staging base URL:
Verifier / owner:
Started (UTC):
Finished (UTC):
Environment file or secret reference (name only):
CI run URL and green commit:
Result: PASS / PASS WITH FOLLOW-UP / BLOCKED
Evidence locations:
```

## 1. Public and deployment smoke

Run the smoke script from the reviewed release bundle. The deploy workflow sets
the legal-page and invite-only gates; operators can enable the same checks by
setting the variables explicitly.

```bash
STAGING_SMOKE_REQUIRE_LEGAL=true \
STAGING_SMOKE_REQUIRE_INVITE_ONLY=true \
STAGING_TERMS_URL='https://staging.example.test/terms' \
STAGING_PRIVACY_URL='https://staging.example.test/privacy' \
scripts/staging_smoke.sh 'https://staging.example.test'
```

Record `/live`, `/ready`, `/health`, frontend HTML, Terms, Privacy, and the
invite-only registration probe. `/ready` must report the current migration head
and healthy PostgreSQL/Redis dependencies. If using an invited pilot credential,
also set `STAGING_SMOKE_EMAIL` and `STAGING_SMOKE_PASSWORD`; this checks login,
`/api/me`, project listing, and logout. Set `STAGING_SMOKE_REQUIRE_AI=true` only
when the configured provider is available; it sends a read-only planning request.

## 2. Authenticated user journeys

Use a unique invited address and the actual SMTP inbox or test mailbox. Keep the
mailbox and account isolated to this release.

1. An admin sends an invitation. Confirm the message arrives over the configured
   SMTP relay, the recipient and workspace are correct, and the link uses the
   public HTTPS application URL.
2. Open the link in a clean browser profile. Confirm the workspace and role shown
   match the invitation. With published legal URLs, submit once without terms
   consent and verify the server rejects it; accept the terms and privacy links,
   submit again, and verify the user is signed in.
3. Verify the new user can reach `/api/me`, sees only the inviting workspace, and
   cannot open admin-only controls. Confirm the database row has the current
   `tos_version` and `tos_accepted_at`, and the audit stream contains
   `invite_accepted`; record identifiers only.
4. Sign out, sign back in, and exercise password recovery from the real mailbox.
   Verify the reset link expires, changes the password, invalidates the previous
   session, and does not reveal whether an unknown address exists.
5. Submit one harmless `POST /api/feedback` report as the invited user (include a
   disposable marker such as `phase5-smoke`, never a real secret). Verify the
   response redacts credential-like values, the reporter can see their own report,
   and a member cannot read the organization-wide triage endpoint. As an admin,
   verify the report is visible only within that workspace and record its
   identifier.

The repository browser suite covers the disposable happy paths. The real mailbox
and deployed HTTPS cookie path still require this staging pass.

## 3. Projects, documents, AI confirmation, and replay

With the invited account, create a disposable project and task, upload a harmless
text fixture, and read it back from a second browser session. Confirm the object
path is in the shared object store and the document remains visible after the
request reaches the other API replica.

Ask Copilot a read-only question and record the response. Then ask it to create a
disposable task. Verify the proposal names the target workspace/project and no
write occurs before confirmation. Confirm once, record the created task and audit
event, then replay the exact confirmation request (using a captured request in a
local proxy or the multi-replica harness). The first execution must succeed and
the replay must return `409` without a second task or second success audit event.

Do not paste confirmation tokens into the evidence record. The CI
`multi-replica` job already checks cross-replica replay protection; this step
proves the same behavior through the deployed HTTPS path.

## 4. Real OIDC / SSO

Use a non-production test tenant and a test user whose domain is explicitly
verified in the provider configuration.

1. Record the provider issuer, client ID label, registered redirect URI, and
   configuration version, excluding the client secret.
2. Start SSO from the public login page and verify the redirect URI is the API
   callback, HTTPS, and exact for the deployed host.
3. Complete login at the IdP. Verify state, nonce, PKCE, issuer, audience, and
   email/domain checks succeed; the callback must establish an httpOnly session
   cookie without putting an access or ID token in the URL.
4. Verify first-login provisioning, repeat-login account linking, logout, and a
   rejected callback with a modified state or code. Capture HTTP status, request
   identifier, and redacted application logs.

The in-process fake IdP tests are necessary regression coverage, but they are not
evidence for a real Okta, Entra, or Google tenant.

## 5. Backup, object mirror, and restore

Confirm a recent database archive exists, is non-empty, passes `pg_restore --list`,
and reached the configured off-site destination. Confirm the object mirror has a
known version of the uploaded fixture. Keep the database archive and object
snapshot timestamps together.

Run the existing restore script only against a separately named disposable
database and matching disposable bucket:

```bash
RESTORE_DATABASE_URL='postgresql+psycopg://…/enterai_restore_drill' \
RESTORE_ARCHIVE='/secure/backups/enterai_YYYYMMDD_HHMMSS.dump' \
RESTORE_CONFIRM=RESTORE_TO_DISPOSABLE_DATABASE \
scripts/verify_restore.sh
```

After restoration, run migrations to the image head, query the restored fixture,
and verify its object-store key resolves. Record archive name, database name,
object snapshot/version, migration heads, duration, row/object checks, and result.
Do not restore over active staging or destroy the disposable target until the
evidence has been retained and a separate deletion decision is recorded.

## 6. Replica failure and recovery

Record the two API container IDs and baseline `/ready` response. During a planned
window, stop one API replica through the deployment operator, then verify the load
balancer continues serving authenticated read-only requests, `/ready` remains
healthy for the surviving replica, and no session or tenant boundary changes.
Restore the replica and confirm both are healthy, migrations remain at the same
head, and the backup services remain exactly one each. Record start/end UTC,
container IDs, request counts/statuses, and recovery duration.

This is a planned game-day action and must not be performed against production
without the deployment owner’s change window.

## 7. Alert delivery

Before declaring staging operational, identify the external alert sink and the
human recipient. Test each alert class with a controlled, reversible trigger:

- make a readiness dependency unavailable in a disposable window;
- create a controlled backup failure or missed-interval condition;
- emit a provider-error condition if the monitoring contract pages for it.

For each trigger, record trigger time, expected alert, actual delivery time,
recipient/channel, request or incident ID, deduplication behavior, recovery
notification, and the redacted log/monitor URL. Restore the dependency and close
the test incident. A configured Sentry DSN alone is not proof that an alert was
delivered.

## Completion record

```text
Public smoke: PASS / FAIL / BLOCKED — evidence:
Invite + terms + session lifecycle: PASS / FAIL / BLOCKED — evidence:
Feedback capture + redaction + admin scope: PASS / FAIL / BLOCKED — evidence:
Project/document/object storage path: PASS / FAIL / BLOCKED — evidence:
AI proposal + confirmed write + replay rejection: PASS / FAIL / BLOCKED — evidence:
Real OIDC tenant: PASS / FAIL / BLOCKED — evidence:
SMTP delivery: PASS / FAIL / BLOCKED — evidence:
Disposable DB + object restore: PASS / FAIL / BLOCKED — evidence:
One-replica failure/recovery: PASS / FAIL / BLOCKED — evidence:
Alert delivery: PASS / FAIL / BLOCKED — evidence:
Open findings / owner / due date:
Go/no-go decision:
```

External staging credentials, a real IdP tenant, SMTP relay, object-store
credentials, monitoring sink, and an approved restore/failure window are required
to turn these checks into launch evidence. Local tests and CI cannot establish
those facts.
