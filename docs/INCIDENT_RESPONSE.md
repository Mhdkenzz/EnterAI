# Incident response (solo on-call)

Written for one person holding the pager. Everything here runs on the production
host from the current release directory unless it says otherwise.

```sh
cd "$DEPLOY_PATH/releases/<current sha>"          # newest dir under releases/
dc() { docker compose --env-file "$DEPLOY_PATH/.env" --env-file images.env "$@"; }
```

## How you find out

- **Uptime check** (`.github/workflows/uptime.yml`): `/ready` fails twice in a row,
  so an issue labelled `outage` opens on GitHub. Turn on push notifications for
  issues in the GitHub mobile app. It closes itself on recovery.
- **Error reporting**: set `SENTRY_DSN` (Sentry or self-hosted GlitchTip) to get
  unhandled exceptions with stack traces.
- **Customers**: support email. Ask for the time, their workspace and what they clicked.

## Severity

| Level | Means | Respond |
|---|---|---|
| SEV1 | Everyone is down, data is exposed or being corrupted, or billing is charging wrongly | Now. Stop other work. |
| SEV2 | One feature broken for everyone (AI, uploads, SSO, email), or one customer fully down | Within 1 hour |
| SEV3 | Degraded, with a workaround | Next working day |

**Possible data exposure is always SEV1**, even if the site is up.

## First 15 minutes

1. **Acknowledge.** Comment on the outage issue with what you know (for example,
   "Investigating, 14:05 GST"). This is your timeline later.
2. **Check what is failing.** `curl -s https://<api>/ready` returns per-check
   status: `db`, `redis`, `migrations`.
3. **Check containers:** `dc ps`. Anything restarting or unhealthy?
4. **Read recent logs:** `dc logs --since 15m api web lb | tail -200`.
5. **Ask what changed.** A deploy in the last hour means **roll back first and
   debug after** (see below).
6. **Check the host:** `df -h` (a full disk breaks Postgres and the backup),
   `free -m`, `uptime`.

## Common failures

| Symptom | Likely cause | Fix |
|---|---|---|
| `/ready` shows `db: error` | Postgres down or out of disk | `dc logs db`; free disk (old `releases/`, `docker image prune`); `dc up -d db` |
| `/ready` shows `redis: error` | Redis down | `dc up -d redis`. Rate limits are shared through Redis, so the API refuses to run without it. |
| `/ready` shows `migrations: error` | Image and database schema disagree, usually a half-finished deploy | Re-run the deploy for the same SHA, or roll back to the previous SHA |
| 502 from nginx | API replicas not running | `dc ps api`, `dc logs api`, `dc up -d --no-build --scale api=2 api` |
| AI requests return 503 | AI provider down or slow | Check the provider status page; raise `AI_TIMEOUT_SECONDS` or switch `AI_PROVIDER` in `.env`, then `dc up -d api` |
| Emails not arriving | SMTP rejecting | `dc logs api \| grep -i smtp`. The outbox retries on its own once SMTP works. |
| Paid customer shows as trial or canceled | Webhook not delivered | Stripe Dashboard → Developers → Webhooks → endpoint → resend the failed event. Never edit subscription rows by hand. |
| TLS errors in browsers | Certificate expired | Renew, replace files under the nginx TLS mount, `dc restart lb` |

## Rolling back

**Code only, with no migration in the bad release:** run **Guarded Production Deploy**
with the previous good SHA. It is safe to repeat.

**Bad migration:** the production deploy takes a verified `pg_dump` immediately
before migrating, into the `backup_data` volume. Restore it:

1. `dc stop api web`, so nothing writes while you restore.
2. List dumps: `dc run --rm --entrypoint ls backup -l /var/backups/enterai`. The
   newest one taken just before the deploy is your restore point.
3. Restore it with `pg_restore` as in [RESTORE_RUNBOOK.md](RESTORE_RUNBOOK.md).
4. Deploy the previous SHA.
5. Check `/ready`, sign in, and open a project.

Anything written between the backup and the restore is lost. Note the window
in the incident issue.

## Security incidents

Treat these as SEV1:

- **Leaked secret.** Rotate it per [SECRETS_MANAGEMENT.md](SECRETS_MANAGEMENT.md).
  Rotating `JWT_SECRET` signs **everyone** out, which is correct if session signing
  may be compromised.
- **One compromised account.** Admin console → deactivate the user. This ends their
  sessions immediately. Then reset their password or re-enable them through SSO.
- **Compromised SCIM token.** Rotate it under SSO settings in the admin console.
  The old token stops working immediately.
- **Possible cross-tenant data exposure.** Take the API down (`dc stop api`) and
  preserve logs (`dc logs api > /root/incident-$(date +%s).log`) before anything else.

If personal data may have been exposed, the UAE PDPL and your customer contracts
can require notifying affected customers and the regulator. Get legal advice the
same day. Don't guess the deadline.

## Talking to customers

Short, honest, and timestamped in GST:

> We're investigating an issue affecting [what] since [time]. [Data is safe / We are
> checking whether data was affected.] Next update by [time + 30 min].

Send an update when you said you would, even if nothing has changed. Send a final
note covering what happened and what you changed.

## After the incident (within 2 working days)

In the outage issue, record:

- timeline
- root cause
- what detected it, and how long detection took
- what made it worse
- one or two concrete follow-ups, each as its own issue

Skip assigning blame: it's you.
