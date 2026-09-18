"""OIDC login: discovery-document cache on identity_providers (Phase 14.2).

Real SSO login needs the IdP's authorization/token endpoints and JWKS URI,
resolved from its issuer via OIDC discovery (`{issuer}/.well-known/
openid-configuration`) rather than hand-entered by the admin -- that is how
every real OIDC client behaves, and it is the only way the JWKS URI used to
verify ID token signatures is guaranteed to match what the IdP actually
signs with. The discovery document is cached on the row (not in per-process
memory) so every replica shares one fetch and a restart does not lose it;
sso_oidc.py refreshes it when stale.
"""
from alembic import op
import sqlalchemy as sa

revision = "0013_oidc_login"
down_revision = "0012_sso_enterprise"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    columns = {c["name"] for c in sa.inspect(bind).get_columns("identity_providers")}
    if "oidc_issuer" not in columns:
        op.add_column("identity_providers", sa.Column("oidc_issuer", sa.String(500), nullable=True))
    if "oidc_discovery_json" not in columns:
        op.add_column("identity_providers", sa.Column("oidc_discovery_json", sa.JSON(), nullable=True))
    if "oidc_discovery_fetched_at" not in columns:
        op.add_column("identity_providers", sa.Column("oidc_discovery_fetched_at", sa.DateTime(), nullable=True))
    if "client_secret_encrypted" not in columns:
        # client_secret_hash (0012) is a one-way hash -- fine for SCIM's bearer
        # token, useless for OIDC's token exchange, which must resend the raw
        # secret to the IdP. This is real, recoverable, at-rest encryption
        # (app/secrets_store.py), not another hash. client_secret_hash is left
        # in place (unread anywhere) rather than dropped, to avoid touching a
        # column another in-flight branch might still reference.
        op.add_column("identity_providers", sa.Column("client_secret_encrypted", sa.Text(), nullable=True))


def downgrade():
    op.drop_column("identity_providers", "client_secret_encrypted")
    op.drop_column("identity_providers", "oidc_discovery_fetched_at")
    op.drop_column("identity_providers", "oidc_discovery_json")
    op.drop_column("identity_providers", "oidc_issuer")
