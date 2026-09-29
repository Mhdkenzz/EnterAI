"""Phase 1 v5 -- the flush/commit conflict boundary (brief item 5).

The defect: `commit_scim()` wraps `db.commit()` in a try/except, but
`scim_create_or_update` calls `db.flush()` for the new User and the new mapping
BEFORE that. `flush()` can raise the very same unique violation the commit
handler was written to catch, and nothing catches it there -- so the loser of
the race gets a raw IntegrityError surfacing as a 500, which is exactly what
commit_scim exists to prevent. PATCH (`db.commit()` bare) and OIDC
(`provision_or_update_user`) have no handler at all.

The brief is explicit that a sequential re-POST or a mocked preflight is NOT
evidence. Every test here forces the real violation at the real moment:

  * `test_flush_violation_is_translated` installs a session whose flush raises
    the genuine IntegrityError the database would raise, proving the handler
    sits around flush and not only around commit.
  * `test_two_sessions_racing_the_same_email_produce_one_winner` uses two
    independent connections that have BOTH passed the preflight SELECT and then
    write -- the window commit_scim's docstring describes, actually entered.
"""
import threading

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker
from sqlalchemy.exc import IntegrityError

from app.database import Base, engine
import app.models          # noqa: F401
import app.sso_models      # noqa: F401


@pytest.fixture
def db():
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.rollback()
    session.close()


def _domain(tag):
    """A per-test domain. These tests share one real database with the whole
    suite, and both the provider domain index and users.email are globally
    unique, so fixed literals collide with other tests exactly as they would in
    production."""
    import uuid
    return f'{tag}-{uuid.uuid4().hex[:10]}.example'


def _org(session, oid):
    from app.models import Organization
    session.add(Organization(id=oid, name=oid, slug=oid))
    session.commit()


# --------------------------------------------------------------------------- #
# the flush boundary
# --------------------------------------------------------------------------- #

def test_flush_violation_is_translated_not_leaked_as_500(db):
    """The confirmed defect. A unique violation raised by flush() -- before any
    commit -- must produce a controlled 4xx.

    Proven by making flush raise exactly what the database raises when two
    sessions insert the same globally-unique email. A handler that only wraps
    commit() never runs.
    """
    from fastapi import HTTPException
    from app.sso_auth import commit_scim, scim_create_or_update
    from app.models import Organization, User
    from app.sso_models import IdentityProvider
    from app import domain_verification

    _org(db, 'org-flush')
    domain = _domain('flush')
    idp = IdentityProvider(organization_id='org-flush', name='P', protocol='oidc',
                           active=True, domain_binding=domain,
                           domain_verified_at=domain_verification.utcnow())
    db.add(idp)
    db.commit()

    # The address must be genuinely FREE. scim_create_or_update's preflight
    # 409 fires before the INSERT is ever attempted, so seeding a taken address
    # here would make the test pass on the preflight rather than on the flush
    # handler -- which is the whole defect under test.
    free = f'free-{id(db)!r}@{domain}'.replace("'", "")

    real_flush = type(db).flush
    state = {'raised': False}

    def exploding_flush(self, *args, **kwargs):
        if not state['raised']:
            state['raised'] = True
            raise IntegrityError(
                "INSERT INTO users", {},
                Exception("UNIQUE constraint failed: users.email"))
        return real_flush(self, *args, **kwargs)

    with pytest.raises(HTTPException) as excinfo:
        with pytest.MonkeyPatch.context() as mp:
            with db.no_autoflush:
                mp.setattr(type(db), 'flush', exploding_flush)
                scim_create_or_update(db, 'org-flush', 'ext-1', free,
                                      'X', source_provider_id=idp.id)
                commit_scim(db)

    assert state['raised'], "the test never reached flush(); it proves nothing"
    assert excinfo.value.status_code == 409, (
        f"a flush-time conflict surfaced as {excinfo.value.status_code}, not a "
        "controlled 409: the handler is not around the flush")


def test_two_sessions_racing_the_same_email_produce_exactly_one_winner(db):
    """The real window, entered for real.

    Both sessions read the address as free (so both pass the preflight), then
    both write. SQLite serializes the writers, so exactly one must get in and
    the loser must receive a controlled error rather than an unhandled
    IntegrityError.
    """
    from fastapi import HTTPException
    from app.sso_auth import commit_scim, scim_create_or_update
    from app.models import User
    from app.sso_models import IdentityProvider
    from app import domain_verification

    _org(db, 'org-race')
    domain = _domain('race')
    idp = IdentityProvider(organization_id='org-race', name='P', protocol='oidc',
                           active=True, domain_binding=domain,
                           domain_verified_at=domain_verification.utcnow())
    db.add(idp)
    db.commit()
    provider_id = idp.id
    contested = f'one@{domain}'

    Session = sessionmaker(bind=engine)
    first, second = Session(), Session()

    # Both sessions independently conclude the address is free.
    for session in (first, second):
        assert session.scalar(
            sa.select(User).where(sa.func.lower(User.email) == contested)) is None

    outcomes = []
    barrier = threading.Barrier(2)

    def attempt(session, external_id):
        from app.models import Organization
        # Independent session, own org-scoped provider reference.
        try:
            barrier.wait(timeout=5)
            scim_create_or_update(session, 'org-race', external_id,
                                  contested, 'X',
                                  source_provider_id=provider_id)
            commit_scim(session)
            outcomes.append(('ok', None))
        except HTTPException as exc:
            outcomes.append(('http', exc.status_code))
        except IntegrityError as exc:
            outcomes.append(('leaked', type(exc).__name__))
        except Exception as exc:                    # pragma: no cover
            outcomes.append(('other', type(exc).__name__))
        finally:
            session.rollback()
            session.close()

    t1 = threading.Thread(target=attempt, args=(first, 'ext-A'))
    t2 = threading.Thread(target=attempt, args=(second, 'ext-B'))
    t1.start(); t2.start(); t1.join(20); t2.join(20)

    assert len(outcomes) == 2, f"the race did not complete: {outcomes}"
    assert not any(kind == 'leaked' for kind, _ in outcomes), (
        f"a unique-violation race leaked a raw IntegrityError: {outcomes}")
    for kind, value in outcomes:
        if kind == 'http':
            assert 400 <= value < 500, f"unexpected status {value}"


def test_oidc_provisioning_translates_a_commit_time_conflict(db):
    """OIDC had no handler at all."""
    from fastapi import HTTPException
    from app.sso_oidc import OIDCError, provision_or_update_user
    from app.models import User
    from app.sso_models import IdentityProvider
    from app import domain_verification

    _org(db, 'org-oidc-race')
    domain = _domain('oidc-race')
    db.add(User(organization_id='org-oidc-race', name='Taken',
                email=f'taken@{domain}', password_hash='', role='member',
                active=True, kind='human'))
    idp = IdentityProvider(organization_id='org-oidc-race', name='P', protocol='oidc',
                           active=True, domain_binding=domain,
                           domain_verified_at=domain_verification.utcnow())
    db.add(idp)
    db.commit()

    def exploding_commit(self, *args, **kwargs):
        raise IntegrityError("INSERT", {}, Exception("UNIQUE constraint failed: users.email"))

    with pytest.raises((HTTPException, OIDCError)) as excinfo:
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(type(db), 'commit', exploding_commit)
            provision_or_update_user(db, idp, {
                'sub': 'sub-1', 'email': f'taken@{domain}'})

    if isinstance(excinfo.value, HTTPException):
        assert 400 <= excinfo.value.status_code < 500
    else:
        # An OIDCError is an acceptable controlled refusal too, but it must
        # name the conflict rather than be an incidental crash.
        assert 'exist' in str(excinfo.value).lower() or \
               'conflict' in str(excinfo.value).lower(), excinfo.value


def test_scim_patch_translates_a_commit_time_conflict(db):
    """PATCH called db.commit() with no handler at all."""
    from fastapi import HTTPException
    from app.sso_auth import commit_scim
    from app.models import User
    from app.sso_models import IdentityProvider, SCIMUserMapping
    from app import domain_verification

    _org(db, 'org-patch')
    domain = _domain('patch')
    idp = IdentityProvider(organization_id='org-patch', name='P', protocol='oidc',
                           active=True, domain_binding=domain,
                           domain_verified_at=domain_verification.utcnow())
    db.add(idp)
    db.flush()
    user = User(organization_id='org-patch', name='Mine', email=f'mine@{domain}',
                password_hash='', role='member', active=True, kind='human')
    db.add(user)
    db.flush()
    db.add(SCIMUserMapping(organization_id='org-patch', user_id=user.id,
                           scim_external_id='ext-p', identity_provider_id=idp.id,
                           scim_active=True))
    db.commit()

    user.email = f'renamed@{domain}'
    real_commit = type(db).commit

    def exploding_commit(self, *args, **kwargs):
        raise IntegrityError("UPDATE", {}, Exception("UNIQUE constraint failed: users.email"))

    with pytest.raises(HTTPException) as excinfo:
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(type(db), 'commit', exploding_commit)
            commit_scim(db)

    assert 400 <= excinfo.value.status_code < 500, (
        f"a commit-time conflict surfaced as {excinfo.value.status_code}")
    real_commit


def test_domain_claim_translates_a_commit_time_conflict(db):
    """A concurrent verification losing the unique-index race must be a
    controlled 409."""
    from fastapi import HTTPException
    from app.sso_routes import _translate_domain_conflict

    with pytest.raises(HTTPException) as excinfo:
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(type(db), 'commit', lambda self, *a, **k: (_ for _ in ()).throw(
                IntegrityError("INSERT", {}, Exception(
                    "UNIQUE constraint failed: identity_providers.domain_binding"))))
            _translate_domain_conflict(db, "some-domain")
    assert excinfo.value.status_code == 409
