"""Application-level encryption at rest for credentials this app must later
*recover*, not just verify -- an IdP's OIDC client_secret has to be sent back
to the token endpoint on every login, so (unlike a password or the SCIM
bearer token) a one-way hash cannot be used for it.

Fernet (AES-128-CBC + HMAC-SHA256, authenticated, from the `cryptography`
package) keyed by SECRETS_ENCRYPTION_KEY. This is real encryption: without the
key, ciphertext is not recoverable, and a tampered ciphertext fails to decrypt
rather than silently returning garbage.

This module owns *at-rest encryption of the value*; where the key itself
comes from is a deployment decision. Where the environment allows a real
external secrets manager (AWS Secrets Manager, GCP Secret Manager, HashiCorp
Vault, etc.), SECRETS_ENCRYPTION_KEY should be sourced from one of those at
process start rather than a plain environment variable -- see
docs/SECRETS_MANAGEMENT.md. Nothing in this app has network access to a real
secrets-manager account in this environment, so that wiring is documented,
not exercised, here.

Rotation, without external infrastructure: set SECRETS_ENCRYPTION_KEY to a
new value and SECRETS_ENCRYPTION_KEY_PREVIOUS to the old one; decrypt() tries
the current key first, then the previous one, so nothing stored under the old
key becomes unreadable mid-rotation. Then run rotate_stored_secrets() (a real,
tested function, not just a runbook step) to rewrite every stored ciphertext
under the new key, and only then remove SECRETS_ENCRYPTION_KEY_PREVIOUS.
"""
from __future__ import annotations

import base64
import hashlib
import os

from cryptography.fernet import Fernet, InvalidToken, MultiFernet


class SecretDecryptionError(ValueError):
    pass


def _derive_fernet_key(raw: str) -> bytes:
    return base64.urlsafe_b64encode(hashlib.sha256(raw.encode()).digest())


def _fernet_from_raw(raw: str) -> Fernet:
    # Accept either a ready-made Fernet key (32 url-safe-base64 bytes) or an
    # arbitrary passphrase, the same "any length in, fixed key out" shape as
    # every other secret in this app -- operators should not need a
    # special key-generation step just to set an env var.
    try:
        return Fernet(raw.encode())
    except (ValueError, TypeError):
        return Fernet(_derive_fernet_key(raw))


def _cipher() -> MultiFernet:
    environment = os.getenv("ENVIRONMENT", "development").strip().lower()
    raw_primary = os.getenv("SECRETS_ENCRYPTION_KEY", "").strip()
    if not raw_primary:
        if environment == "production":
            raise RuntimeError(
                "SECRETS_ENCRYPTION_KEY must be set in production -- it encrypts IdP"
                " client secrets and other recoverable credentials at rest. Generate"
                " one with: python -c \"from cryptography.fernet import Fernet;"
                " print(Fernet.generate_key().decode())\""
            )
        # Deterministic dev fallback (not a random per-process key) so values
        # encrypted in one dev process are still readable after a restart --
        # the same reasoning as auth.py's "dev-secret-change-me" JWT fallback.
        from .auth import SECRET as _dev_jwt_secret
        raw_primary = f"enterai:secrets-store:dev-fallback:{_dev_jwt_secret}"
    keys = [_fernet_from_raw(raw_primary)]
    raw_previous = os.getenv("SECRETS_ENCRYPTION_KEY_PREVIOUS", "").strip()
    if raw_previous:
        keys.append(_fernet_from_raw(raw_previous))
    return MultiFernet(keys)


def encrypt(plaintext: str) -> str:
    return _cipher().encrypt(plaintext.encode()).decode()


def decrypt(ciphertext: str) -> str:
    try:
        return _cipher().decrypt(ciphertext.encode()).decode()
    except InvalidToken as error:
        raise SecretDecryptionError(
            "Stored secret could not be decrypted -- wrong or already-rotated"
            " SECRETS_ENCRYPTION_KEY (see SECRETS_ENCRYPTION_KEY_PREVIOUS)"
        ) from error


def rotate_stored_secrets(db, model, column_name: str, *, organization_id: str | None = None) -> dict:
    """Re-encrypt every non-null value of `column_name` on `model` under the
    current SECRETS_ENCRYPTION_KEY. Call this after setting a new
    SECRETS_ENCRYPTION_KEY (with the old one in SECRETS_ENCRYPTION_KEY_PREVIOUS
    so old ciphertext still decrypts) and before removing the previous key.

    One row failing to decrypt (stale ciphertext, a key removed too early)
    must not abort every other row's rotation in the same call -- that failure
    is skipped and counted separately, not raised, so the caller can rotate
    everything that is rotatable and then go fix the one row that is not."""
    from sqlalchemy import select
    column = getattr(model, column_name)
    stmt = select(model).where(column.isnot(None))
    if organization_id is not None and hasattr(model, "organization_id"):
        stmt = stmt.where(model.organization_id == organization_id)
    rewritten, failed = 0, []
    for row in db.scalars(stmt):
        value = getattr(row, column_name)
        try:
            setattr(row, column_name, encrypt(decrypt(value)))
            rewritten += 1
        except SecretDecryptionError:
            failed.append(getattr(row, "id", None))
    db.flush()
    return {"rewritten": rewritten, "failed_ids": failed}
