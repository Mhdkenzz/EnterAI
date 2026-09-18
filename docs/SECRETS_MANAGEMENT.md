# Secrets Manager Integration

## Two different kinds of secret, handled differently
1. **Process secrets** (JWT_SECRET, DATABASE_URL, OPENAI_API_KEY, STRIPE_*, etc.) --
   environment variables injected at deploy time, never committed. Rotation is a
   manual procedure today: deploy the new value, restart services, verify `/health`,
   revoke the old value at its source. Nothing in this repo automates that rotation.
2. **Application-stored secrets that must later be recovered**, not just verified
   (currently: an OIDC identity provider's `client_secret`, which the backend must
   resend to the IdP's token endpoint on every login) -- these cannot be a one-way
   hash. `backend/app/secrets_store.py` encrypts them at rest (Fernet,
   AES-128-CBC+HMAC-SHA256) under `SECRETS_ENCRYPTION_KEY`, and this rotation IS
   real and tested (`rotate_stored_secrets`, exercised in
   `backend/tests/test_enterprise_phase15.py`):
   - Set `SECRETS_ENCRYPTION_KEY` to a new value and `SECRETS_ENCRYPTION_KEY_PREVIOUS`
     to the old one (decrypt tries the current key first, then falls back to the
     previous one, so nothing becomes unreadable mid-rotation).
   - Call `POST /api/admin/sso/rotate-secrets` (human admin, org-scoped) to
     re-encrypt every stored secret under the new key.
   - Remove `SECRETS_ENCRYPTION_KEY_PREVIOUS` once done.
   - Production refuses to boot -- fails closed -- with no `SECRETS_ENCRYPTION_KEY`
     set (see `secrets_store._cipher`), the same pattern as JWT_SECRET.

## Where the key itself should live
Where the environment allows a real external secrets manager (AWS Secrets Manager,
GCP Secret Manager, HashiCorp Vault), `SECRETS_ENCRYPTION_KEY` (and the process
secrets above) should be sourced from it at process start rather than a plain
environment variable. This repo has no network access to a real secrets-manager
account to integrate against, so that wiring is documented here, not implemented or
exercised, and must not be reported as done until it is.

Never log secrets; audit all secret-adjacent admin actions via audit_events (provider
config changes and rotations are logged today -- see `sso_routes.py`).
