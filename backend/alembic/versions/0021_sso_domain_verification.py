"""Prove ownership of an SSO domain before it can route logins or vouch for emails.

Until now any organization admin (and on a self-serve SaaS anyone can become one)
could bind an identity provider to *any* domain -- another customer's, or gmail.com --
and `domain_binding` was neither verified nor unique, so the staff of the real company
could be routed to an attacker's IdP at login.

This adds the proof: a per-provider random token the admin publishes as a DNS TXT
record, and the timestamp of the moment it was seen. A partial unique index then lets
only one *verified* provider hold a domain, platform-wide.

Existing providers are deliberately NOT backfilled as verified: nothing ever proved
they own their domain, and grandfathering them would keep the hole open for exactly
the rows an attacker already created. Until their admin publishes the TXT record and
calls verify-domain, SSO login for that domain stops routing (users fall back to
password sign-in).
"""
from alembic import op
import sqlalchemy as sa

revision = "0021_sso_domain_verification"
down_revision = "0020_trial_end_dates"
branch_labels = None
depends_on = None

INDEX_NAME = "uq_identity_providers_verified_domain"
VERIFIED = "domain_verified_at IS NOT NULL"


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {c["name"] for c in inspector.get_columns("identity_providers")}
    # Plain add_column (no batch rebuild): both are nullable with no default, which
    # SQLite and PostgreSQL both add in place -- same approach as 0013.
    if "domain_verification_token" not in columns:
        op.add_column("identity_providers", sa.Column("domain_verification_token", sa.String(64), nullable=True))
    if "domain_verified_at" not in columns:
        op.add_column("identity_providers", sa.Column("domain_verified_at", sa.DateTime(), nullable=True))
    indexes = {i["name"] for i in inspector.get_indexes("identity_providers")}
    if INDEX_NAME not in indexes:
        # Both dialect kwargs are passed; each dialect ignores the other's.
        op.create_index(
            INDEX_NAME, "identity_providers", ["domain_binding"], unique=True,
            postgresql_where=sa.text(VERIFIED), sqlite_where=sa.text(VERIFIED),
        )


def downgrade():
    # The index goes first: SQLite refuses to drop a column an index (including a
    # partial index's WHERE clause) still references.
    op.drop_index(INDEX_NAME, table_name="identity_providers")
    op.drop_column("identity_providers", "domain_verified_at")
    op.drop_column("identity_providers", "domain_verification_token")
