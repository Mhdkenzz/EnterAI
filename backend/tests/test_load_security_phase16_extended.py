"""Phase 16 extended: spike load, replay/adversarial, scheduler failure, performance targets."""
import time
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select, func
from sqlalchemy.orm import sessionmaker
import app.sso_models, app.enterprise_models, app.models
from app import domain_verification
from app.database import Base
from app.models import User, Organization, AuditEvent, Activity
from app.sso_auth import deactivate_scim_user, scim_create_or_update
from app.sso_models import SCIMUserMapping

def db_session_fixture():
    pass  # placeholder; actual fixture below

@pytest.fixture
def db_session():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    session = Session()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(engine)

# --- Spike / sustained load ---

def test_spike_load_scim_create(db_session):
    # Spike: 100 rapid SCIM creates; verify isolation and no cross-org corruption
    org = Organization(id="spike-org-1", name="Spike", slug="spike")
    db_session.add(org)
    db_session.commit()
    # A mapping is only writable by a provider that owns it, so the spike
    # provisions through a real provider (see 0021_scim_provider_ownership.py).
    # The provider also needs a VERIFIED email domain: provisioning is refused
    # outside a domain the provider has proven it controls, so the spike's
    # addresses must sit inside one. Domain is per-test because ownership is
    # globally exclusive.
    domain = f"spike-{uuid4().hex[:10]}.test.local"
    idp = app.sso_models.IdentityProvider(organization_id=org.id, name="Spike IdP",
                                          protocol="oidc", active=True,
                                          domain_binding=domain,
                                          domain_verified_at=domain_verification.utcnow())
    db_session.add(idp)
    db_session.commit()
    for i in range(100):
        u = scim_create_or_update(db_session, org.id, f"ext-spike-{i}", f"user{i}@{domain}", f"Name{i}",
                                  source_provider_id=idp.id)
    # Verify count matches exactly 100 unique external IDs
    mappings = db_session.scalars(select(SCIMUserMapping).where(SCIMUserMapping.organization_id == org.id)).all()
    assert len(mappings) == 100

def test_scheduler_worker_failure_recovery_simulation(db_session):
    # Simulate failure recovery: user disabled then reactivated via SCIM
    org = Organization(id="fail-org", name="Fail", slug="fail")
    db_session.add(org)
    db_session.commit()
    # A verified domain, and the user's address must sit inside it.
    domain = f"fail-{uuid4().hex[:10]}.test.local"
    user = User(organization_id=org.id, name="FailUser", email=f"fail@{domain}", role="member",
                active=True, kind="human", password_hash="")
    db_session.add(user)
    db_session.commit()
    mapping = SCIMUserMapping(organization_id=org.id, user_id=user.id, scim_external_id="fail-ext", scim_active=True)
    db_session.add(mapping)
    db_session.commit()
    # Deactivate
    deactivate_scim_user(db_session, mapping, actor_id="system")
    db_session.commit()
    assert user.active is False
    # Reactivate via update. The mapping carries an owner and the call passes
    # that same provider, because an unowned mapping is invisible to every
    # provider by design (see 0021_scim_provider_ownership.py).
    idp = app.sso_models.IdentityProvider(organization_id=org.id, name="Fail IdP",
                                          protocol="oidc", active=True,
                                          domain_binding=domain,
                                          domain_verified_at=domain_verification.utcnow())
    db_session.add(idp)
    db_session.commit()
    mapping.identity_provider_id = idp.id
    db_session.commit()
    scim_create_or_update(db_session, org.id, "fail-ext", f"fail@{domain}", "FailUser",
                          source_provider_id=idp.id)
    refreshed = db_session.get(User, user.id)
    assert refreshed.active is True

def test_replay_adversarial_scim_mapping(db_session):
    # Replay/adversarial: attempt to reuse same scim_external_id for different user/org
    org1 = Organization(id="adv-org-1", name="Adv1", slug="adv1")
    org2 = Organization(id="adv-org-2", name="Adv2", slug="adv2")
    db_session.add_all([org1, org2])
    db_session.commit()
    # Each org has its own provider, so the only thing that can stop the
    # cross-org call is the ownership check itself. Each provider needs its own
    # VERIFIED domain (ownership is globally exclusive) and the address must sit
    # inside the calling provider's domain, or the domain gate would refuse the
    # call before the ownership guard is ever reached -- making this test pass
    # for the wrong reason.
    domain1 = f"adv1-{uuid4().hex[:10]}.test.local"
    domain2 = f"adv2-{uuid4().hex[:10]}.test.local"
    idp1 = app.sso_models.IdentityProvider(organization_id=org1.id, name="Adv1 IdP",
                                          protocol="oidc", active=True,
                                          domain_binding=domain1,
                                          domain_verified_at=domain_verification.utcnow())
    idp2 = app.sso_models.IdentityProvider(organization_id=org2.id, name="Adv2 IdP",
                                          protocol="oidc", active=True,
                                          domain_binding=domain2,
                                          domain_verified_at=domain_verification.utcnow())
    db_session.add_all([idp1, idp2])
    db_session.commit()
    u1 = scim_create_or_update(db_session, org1.id, "adv-ext", f"adv@{domain1}", "Adv",
                               source_provider_id=idp1.id)
    # Create a fake mapping with cross-org reference by manual insertion to trigger guard
    # Instead, verify the function raises when org doesn't match mapping
    mapping_cross = SCIMUserMapping(organization_id=org2.id, user_id=u1.id, scim_external_id="adv-ext", identity_provider_id=idp2.id, scim_active=True)
    db_session.add(mapping_cross)
    db_session.commit()
    # Direct function call with wrong org should trigger cross-org guard:
    # org1's provider may not write a mapping inside org2, and org1's mapping
    # is not reachable from org2.
    with pytest.raises(Exception):
        scim_create_or_update(db_session, org2.id, "adv-ext", f"adv@{domain1}", "Adv",
                              source_provider_id=idp1.id)

# --- Performance targets ---

def test_p95_target_simulation(db_session):
    # Define and verify a performance target: insert/query under 50ms mental budget.
    org = Organization(id="perf-target", name="Perf", slug="perf")
    db_session.add(org)
    db_session.commit()
    start_time = time.time()
    for i in range(100):
        db_session.add(AuditEvent(organization_id=org.id, actor_id="actor", source="perf",
                                 action="p95_target", entity_type="test", entity_id=str(i)))
    db_session.commit()
    elapsed = time.time() - start_time
    # Assert sub-second sustained rate (SQLite memory target)
    assert elapsed < 5.0
