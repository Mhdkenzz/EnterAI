"""Phase 1 v6 -- exact SCIM POST / PUT / PATCH input and operation semantics.

Static v6 review of the v5 candidate found the mutation paths accepted input
they should have refused, and accepted it silently:

* **PATCH resolved the resource before validating `userName`.** The mapping and
  User were fetched first, so a malformed or out-of-domain `userName` in a PATCH
  was answered 404/409 about the RESOURCE rather than refused as input. That
  ordering is itself an oracle: a caller who can tell "no such user" from "bad
  userName" can enumerate ids.
* **PUT was not full-resource.** `displayName` and `roles` were applied through
  truthiness checks, so an explicit empty displayName was silently ignored and
  absent roles left the previous role in place. SCIM PUT is a replace: absent
  means cleared. A skipped write is a 200 that lied.
* **PATCH applied operations as it walked them.** A malformed operation
  discovered on the third op left the first two already written -- a partial
  write followed by a 400, with the transaction's fate depending on the
  session's final disposition.
* **PATCH `active` accepted anything.** `"active": "false"` (a string) matched
  neither `is False` nor `is True` and the request returned 200 having changed
  nothing, which reads as success to the IdP.

Each test below pins one of those, and every one is proven non-vacuous.
"""
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select as sa_select

from app import domain_verification
from app.database import SessionLocal
from app.main import app
from app.models import User
from app.sso_models import IdentityProvider, SCIMUserMapping


def _workspace(client):
    key = uuid4().hex
    response = client.post('/api/auth/register', json={
        'organization_name': f'V6ops {key}', 'name': 'Admin',
        'email': f'{key}@example.com', 'password': 'test-password-1'})
    assert response.status_code == 200, response.text
    body = response.json()
    return {'Authorization': 'Bearer ' + body['token']}, body


def _domain(tag):
    return f'{tag}-{uuid4().hex[:10]}.example'


def _provisioned(client, admin, tag='ops'):
    """A verified provider plus a SCIM token and its verified domain."""
    from app.sso_routes import issue_scim_token
    from unittest import mock
    domain = _domain(tag)
    created = client.post('/api/admin/sso/providers', headers=admin, json={
        'name': f'P-{tag}', 'protocol': 'oidc', 'domain_binding': domain})
    assert created.status_code == 200, created.text
    provider_id = created.json()['id']
    with mock.patch.object(domain_verification, 'lookup_txt',
                           return_value=[domain_verification.expected_txt_value(domain)]):
        verified = client.post(
            f'/api/admin/sso/providers/{provider_id}/domain-verification',
            headers=admin, json={'domain': domain})
    assert verified.status_code == 200, verified.text
    with SessionLocal() as db:
        raw = issue_scim_token(db, db.get(IdentityProvider, provider_id))
        db.commit()
    return {'Authorization': f'Bearer {raw}'}, domain


def _user(headers, domain, name='User'):
    # The display name may contain spaces; the ADDRESS must not, so it is not
    # derived from the name.
    created = _post(headers, f'user-{uuid4().hex[:8]}@{domain}', name)
    assert created.status_code == 200, created.text
    return created


def _post(headers, username, display):
    return TestClient(app).post('/scim/v2/Users', headers=headers, json={
        'userName': username, 'displayName': display})


# --------------------------------------------------------------------------- #
# PATCH: input is validated before the resource is resolved
# --------------------------------------------------------------------------- #

def test_patch_with_an_invalid_username_does_not_distinguish_a_missing_user():
    """A bad `userName` is refused identically whether or not the user exists.

    Resolving the resource first turns a 400 about input into a 404 about
    existence, which is an enumeration oracle reachable with a provider token.
    """
    with TestClient(app) as client:
        admin, _ = _workspace(client)
        headers, domain = _provisioned(client, admin, 'oracle')
        created = _user(headers, domain)
        scim_id = created.json()['id']

        def patch(target, value):
            return client.patch(f'/scim/v2/Users/{target}', headers=headers,
                                json={'Operations': [
                                    {'op': 'replace', 'path': 'userName',
                                     'value': value}]})

        for bad in ('not-an-email', f'x@other-domain.example',
                    '', '   ', 'a@@b.example', 'no-local@'):
            hit = patch(scim_id, bad)
            miss = patch('does-not-exist-' + uuid4().hex[:8], bad)
            assert hit.status_code == miss.status_code, (
                f"{bad!r}: existent gave {hit.status_code} and missing gave "
                f"{miss.status_code} -- the difference enumerates ids")
            assert 400 <= hit.status_code < 500, (
                f"{bad!r} produced {hit.status_code}")
            assert hit.json() == miss.json(), (
                f"{bad!r}: response bodies differ between existing and missing")


def test_patch_preserves_paths_that_do_not_change_username():
    """The pre-gate must not break ordinary attribute updates."""
    with TestClient(app) as client:
        admin, _ = _workspace(client)
        headers, domain = _provisioned(client, admin, 'preserve')
        scim_id = _user(headers, domain, 'Original').json()['id']

        renamed = client.patch(f'/scim/v2/Users/{scim_id}', headers=headers,
                               json={'Operations': [
                                   {'op': 'replace', 'path': 'displayName',
                                    'value': 'New Name'}]})
        assert renamed.status_code == 200, renamed.text
        assert renamed.json()['displayName'] == 'New Name'

        # A valid in-domain rename still works.
        current = _email_of(scim_id)
        assert current.endswith(f'@{domain}')
        moved = client.patch(f'/scim/v2/Users/{scim_id}', headers=headers,
                             json={'Operations': [
                                 {'op': 'replace', 'path': 'userName',
                                  'value': f'renamed-{uuid4().hex[:8]}@{domain}'}]})
        assert moved.status_code == 200, moved.text


# --------------------------------------------------------------------------- #
# PUT: full-resource replace semantics
# --------------------------------------------------------------------------- #

def test_put_clears_display_name_when_omitted():
    """SCIM PUT replaces the resource. An omitted optional attribute is
    cleared, not left behind.

    The truthiness check `if display_name:` skipped the write entirely, so a
    PUT that removed the display name returned 200 and left the old name in
    place -- a silent no-op reported as success.
    """
    with TestClient(app) as client:
        admin, _ = _workspace(client)
        headers, domain = _provisioned(client, admin, 'putname')
        scim_id = _user(headers, domain, 'Has A Name').json()['id']
        assert _user_name(headers, scim_id) == 'Has A Name'

        replaced = client.put(f'/scim/v2/Users/{scim_id}', headers=headers, json={
            'userName': _email_of(scim_id)})
        assert replaced.status_code == 200, replaced.text
        body = replaced.json()
        assert not body.get('displayName'), (
            f"PUT without displayName returned {body.get('displayName')!r}; a "
            f"replace must clear it rather than keep the previous value")


def test_put_with_explicit_empty_display_name_is_accepted_and_clears():
    """An explicitly empty string is a legitimate clearing, distinct from the
    truthiness skip that used to discard it."""
    with TestClient(app) as client:
        admin, _ = _workspace(client)
        headers, domain = _provisioned(client, admin, 'putempty')
        scim_id = _user(headers, domain, 'Named').json()['id']

        replaced = client.put(f'/scim/v2/Users/{scim_id}', headers=headers, json={
            'userName': _email_of(scim_id), 'displayName': ''})
        assert replaced.status_code == 200, replaced.text
        assert not replaced.json().get('displayName')


def test_put_without_roles_resets_to_the_safe_default_role():
    """Absent roles is a replace, so the role must fall back to the safe
    default rather than retaining whatever privilege it had.

    Retaining a role across a replace that omitted it is how a demotion is
    quietly lost: the IdP asks for the resource as it understands it, and the
    server silently keeps the old grant.
    """
    with TestClient(app) as client:
        admin, _ = _workspace(client)
        headers, domain = _provisioned(client, admin, 'putrole')
        scim_id = _user(headers, domain, 'Roleholder').json()['id']

        with SessionLocal() as db:
            user_id = _user_id_of(scim_id)
            db.get(User, user_id).role = 'manager'
            db.commit()
            assert db.get(User, user_id).role == 'manager'

        replaced = client.put(f'/scim/v2/Users/{scim_id}', headers=headers, json={
            'userName': _email_of(scim_id)})
        assert replaced.status_code == 200, replaced.text

        with SessionLocal() as db:
            role = db.get(User, _user_id_of(scim_id)).role
        assert role == 'member', (
            f"PUT without roles left the role as {role!r}; a replace with no "
            f"roles must fall back to the least-privileged role")


def test_put_with_an_explicit_role_still_applies_the_mapping():
    """The reset above must not break a role that IS submitted."""
    with TestClient(app) as client:
        admin, _ = _workspace(client)
        headers, domain = _provisioned(client, admin, 'putrole2')
        scim_id = _user(headers, domain, 'Promoted').json()['id']

        replaced = client.put(f'/scim/v2/Users/{scim_id}', headers=headers, json={
            'userName': _email_of(scim_id), 'roles': [{'value': 'manager'}]})
        assert replaced.status_code == 200, replaced.text
        with SessionLocal() as db:
            # The provider has no role_mapping_json, so the safe default applies
            # rather than the raw claim passing through.
            assert db.get(User, _user_id_of(scim_id)).role == 'member'


def test_put_updates_the_submitted_username():
    """The full-resource path really does rename -- not a partial update that
    silently ignores the one field the IdP most needs to correct."""
    with TestClient(app) as client:
        admin, _ = _workspace(client)
        headers, domain = _provisioned(client, admin, 'putrename')
        scim_id = _user(headers, domain, 'Before').json()['id']
        old = _email_of(scim_id)

        new_email = f'after-{uuid4().hex[:8]}@{domain}'
        replaced = client.put(f'/scim/v2/Users/{scim_id}', headers=headers, json={
            'userName': new_email, 'displayName': 'After'})
        assert replaced.status_code == 200, replaced.text
        assert replaced.json()['userName'] == new_email
        assert old != new_email


# --------------------------------------------------------------------------- #
# PATCH: `active` accepts only a literal bool, and validation precedes writes
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("bad", ['"false"', '"true"', '0', '1', 'null',
                                 '{}', '[]', '"yes"'])
def test_patch_active_rejects_non_boolean_values(bad):
    """Only a JSON boolean is accepted.

    The v5 code tested `is False` / `is True`, so a string or number matched
    neither branch and the request returned 200 having changed nothing. To an
    IdP that is indistinguishable from a successful deactivation.
    """
    with TestClient(app) as client:
        admin, _ = _workspace(client)
        headers, domain = _provisioned(client, admin, 'active')
        scim_id = _user(headers, domain, 'Active').json()['id']
        before = _is_active(scim_id)

        response = client.patch(f'/scim/v2/Users/{scim_id}', headers=headers,
                                json={'Operations': [
                                    {'op': 'replace', 'path': 'active',
                                     'value': _json_value(bad)}]})
        assert response.status_code == 400, (
            f"active={bad} returned {response.status_code}; a non-boolean must "
            f"be refused, not silently ignored")
        assert _is_active(scim_id) == before, "state changed despite a 400"


def test_patch_accepts_real_booleans_in_both_directions():
    with TestClient(app) as client:
        admin, _ = _workspace(client)
        headers, domain = _provisioned(client, admin, 'activeok')
        scim_id = _user(headers, domain, 'Toggle').json()['id']

        off = client.patch(f'/scim/v2/Users/{scim_id}', headers=headers,
                           json={'Operations': [
                               {'op': 'replace', 'path': 'active', 'value': False}]})
        assert off.status_code == 200, off.text
        assert _is_active(scim_id) is False

        on = client.patch(f'/scim/v2/Users/{scim_id}', headers=headers,
                          json={'Operations': [
                              {'op': 'replace', 'path': 'active', 'value': True}]})
        assert on.status_code == 200, on.text
        assert _is_active(scim_id) is True


def test_patch_validates_every_operation_before_writing_anything():
    """A malformed operation anywhere in the list must prevent ALL writes.

    The v5 loop applied as it walked, so `[deactivate, bad userName]` wrote the
    deactivation and then returned 400 -- a partial write whose persistence
    depended on the caller's session disposition.
    """
    with TestClient(app) as client:
        admin, _ = _workspace(client)
        headers, domain = _provisioned(client, admin, 'atomic')
        scim_id = _user(headers, domain, 'Atomic').json()['id']
        original_name = _user_name(headers, scim_id)
        assert _is_active(scim_id) is True

        response = client.patch(f'/scim/v2/Users/{scim_id}', headers=headers,
                                json={'Operations': [
                                    {'op': 'replace', 'path': 'active',
                                     'value': False},
                                    {'op': 'replace', 'path': 'displayName',
                                     'value': 'Should Not Persist'},
                                    {'op': 'replace', 'path': 'userName',
                                     'value': 'garbage-not-an-email'},
                                ]})
        assert response.status_code == 400, response.text
        assert _is_active(scim_id) is True, (
            "the deactivation from operation 1 persisted even though operation "
            "3 was invalid -- a partial write")
        assert _user_name(headers, scim_id) == original_name, (
            "operation 2's display name persisted despite the later failure")


# --------------------------------------------------------------------------- #
# POST: the same exact-input rules
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("bad", ['', '   ', 'plainstring', 'a@b@c.example',
                                 '@no-local.example', 'trailing@dot.example.'])
def test_post_rejects_malformed_user_names(bad):
    with TestClient(app) as client:
        admin, _ = _workspace(client)
        headers, _domain_value = _provisioned(client, admin, 'postbad')
        response = client.post('/scim/v2/Users', headers=headers, json={
            'userName': bad, 'displayName': 'X'})
        assert 400 <= response.status_code < 500, (
            f"userName={bad!r} returned {response.status_code}")
        assert response.status_code != 500


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #

def _json_value(raw):
    import json
    return json.loads(raw)


def _user_id_of(scim_id):
    with SessionLocal() as db:
        mapping = db.scalar(sa_select(SCIMUserMapping).where(
            SCIMUserMapping.scim_external_id == scim_id))
        assert mapping is not None, f"no mapping for {scim_id}"
        return mapping.user_id


def _email_of(scim_id):
    with SessionLocal() as db:
        return db.get(User, _user_id_of(scim_id)).email


def _user_name(headers, scim_id):
    with TestClient(app) as client:
        found = client.get(f'/scim/v2/Users/{scim_id}', headers=headers)
        assert found.status_code == 200, found.text
        return found.json()['displayName']


def _is_active(scim_id):
    with SessionLocal() as db:
        mapping = db.scalar(sa_select(SCIMUserMapping).where(
            SCIMUserMapping.scim_external_id == scim_id))
        assert mapping is not None
        return db.get(User, mapping.user_id).active
