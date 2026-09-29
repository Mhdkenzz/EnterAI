"""Phase 14.1 — Extended SCIM coverage, isolation, and audit verification.

Runs against the suite's real, externally-migrated database (see
test_sso_phase14.py's module docstring for why this replaced an isolated
`Base.metadata.create_all()` schema).
"""
from uuid import uuid4

import pytest
from sqlalchemy import select

from app import domain_verification
from app.database import SessionLocal
from app.models import Organization, User
from app.sso_auth import deactivate_scim_user, scim_create_or_update
from app.sso_models import IdentityProvider, SCIMUserMapping


@pytest.fixture
def db_session():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def test_org(db_session):
    key = uuid4().hex[:12]
    org = Organization(name=f"SSO Test Extended {key}", slug=f"sso-test-ext-{key}")
    db_session.add(org)
    db_session.commit()
    return org.id


def test_scim_patch_deactivation(db_session, test_org):
    user = User(organization_id=test_org, name="Patch User", email=f"patch-{uuid4().hex[:8]}@test.local",
                role="member", active=True, kind="human", password_hash="")
    db_session.add(user)
    db_session.commit()
    mapping = SCIMUserMapping(organization_id=test_org, user_id=user.id,
                               scim_external_id=f"ext-patch-{uuid4().hex[:8]}", scim_active=True)
    db_session.add(mapping)
    db_session.commit()
    deactivate_scim_user(db_session, mapping, actor_id=None)
    db_session.commit()
    refreshed = db_session.get(User, user.id)
    assert refreshed.active is False
    assert mapping.scim_active is False


def test_scim_reactivation(db_session, test_org):
    # Domain is per-test: ownership is globally exclusive, so a fixed name is
    # taken by whichever test runs first. The user's email must sit in it.
    domain = f"react-{uuid4().hex[:10]}.test.local"
    user = User(organization_id=test_org, name="React User",
                email=f"react-{uuid4().hex[:8]}@{domain}",
                role="member", active=False, kind="human", password_hash="")
    db_session.add(user)
    db_session.commit()
    # A mapping is only reachable by the provider that owns it, so this
    # deprovisioned identity needs an owner for the reactivation below to
    # resolve it. An unowned mapping matches no provider by design
    # (see 0021_scim_provider_ownership.py).
    idp = IdentityProvider(organization_id=test_org, name="React IdP", protocol="oidc",
                           active=True,
                           # Provisioning requires a verified domain (see
                           # app/domain_verification.py); set explicitly here
                           # because this provider is built directly rather
                           # than through the admin API + DNS challenge.
                           domain_binding=domain,
                           domain_verified_at=domain_verification.utcnow())
    db_session.add(idp)
    db_session.commit()
    scim_id = f"ext-react-{uuid4().hex[:8]}"
    mapping = SCIMUserMapping(organization_id=test_org, user_id=user.id,
                               scim_external_id=scim_id, identity_provider_id=idp.id,
                               scim_active=False)
    db_session.add(mapping)
    db_session.commit()
    user2 = scim_create_or_update(db_session, test_org, scim_id, user.email, "React User",
                                  source_provider_id=idp.id)
    db_session.commit()
    mapping_refreshed = db_session.scalar(select(SCIMUserMapping).where(
        SCIMUserMapping.scim_external_id == scim_id))
    assert user2.active is True
    assert mapping_refreshed.scim_active is True
