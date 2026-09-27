"""The three upload routes must do their blocking work off the event loop.

`assist_project_draft`, `add_project_document` and `add_attachment` each read the
upload, extract its text, write it to storage, and touch the database
(and, for the draft, run the provider) before responding. Every one of those is
blocking work. In an `async def` handler they run on the event loop itself, so a
single 5 MB upload stalls every other request the worker is serving.

FastAPI runs a plain `def` endpoint in its AnyIO worker pool, which is why these
three are spelled `def`. Asserting that spelling proves nothing -- a rename back
to `async def` would look identical to a test that only inspects source. So each
test here installs a probe *inside* the handler's own blocking work and asks the
runtime a question the answer to which cannot be faked: is there an event loop
running in this thread right now?

A worker thread has no running loop, so `asyncio.get_running_loop()` raises
`RuntimeError` there. On the event loop it returns a live loop object. That
distinction is the whole assertion, and it is what turns a silent regression back
into a red test.
"""
import asyncio
import threading
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app import main
from app.main import MAX_UPLOAD_BYTES, app


def workspace(client):
    """Register a throwaway org and authenticate with a bearer header.

    Same shape as the helper in test_observability.py: registering also sets a
    session cookie, which these tests drop so later unauthenticated requests
    really are anonymous.
    """
    key = uuid4().hex
    response = client.post('/api/auth/register', json={
        'organization_name': key, 'name': 'Admin',
        'email': f'{key}@example.com', 'password': 'test-password',
    })
    assert response.status_code == 200
    client.cookies.clear()
    return {'Authorization': 'Bearer ' + response.json()['token']}, response.json()


class LoopProbe:
    """Records the event-loop state of the thread that does the blocking work.

    Installed by patching a collaborator the handler calls *after* it has read
    the upload, so the observation lands inside the real request path rather than
    beside it.
    """

    def __init__(self):
        self.thread_name = None
        self.running_loop = None
        self.probed = 0
        self.called_from = None

    def record(self, called_from):
        self.called_from = called_from
        self.probed += 1
        self.thread_name = threading.current_thread().name
        try:
            self.running_loop = asyncio.get_running_loop()
        except RuntimeError:
            self.running_loop = None  # No loop in this thread: a worker thread.

    @property
    def off_the_event_loop(self):
        return self.running_loop is None


@pytest.fixture
def storage_probe(monkeypatch):
    """A storage backend that probes the calling thread, then does the real save.

    `get_storage` is called by all three routes before any database write, and the
    real `save` still runs, so this observes the production path without
    replacing the behavior under test.
    """
    probe = LoopProbe()
    real_get_storage = main.get_storage

    class ProbedStorage:
        def __init__(self, inner):
            self._inner = inner

        def save(self, folder, filename, content):
            probe.record('save')
            return self._inner.save(folder, filename, content)

        def read(self, identifier):
            return self._inner.read(identifier)

        def delete(self, identifier):
            return self._inner.delete(identifier)

    monkeypatch.setattr(main, 'get_storage', lambda: ProbedStorage(real_get_storage()))
    return probe


def test_the_assist_draft_route_does_its_blocking_work_off_the_event_loop(storage_probe):
    """The provider call and storage write must not run on the loop.

    This route is the worst of the three: it extracts text, writes storage, and
    calls the AI provider, all before it can answer.
    """
    with TestClient(app) as client:
        headers, _ = workspace(client)
        response = client.post('/api/project-drafts/assist', headers=headers,
                               files={'file': ('brief.txt', b'Launch readiness brief',
                                               'text/plain')})

    assert response.status_code == 201, response.text
    assert storage_probe.probed == 1, "the probe never ran: the blocking path was bypassed"
    assert storage_probe.off_the_event_loop, (
        f"assist_project_draft ran on a thread with a live event loop "
        f"({storage_probe.running_loop!r} in {storage_probe.thread_name!r}); "
        f"it must be dispatched to the AnyIO worker pool")


def test_the_project_document_route_does_its_blocking_work_off_the_event_loop(storage_probe):
    with TestClient(app) as client:
        headers, _ = workspace(client)
        suffix = uuid4().hex[:8]
        team = client.post('/api/teams', headers=headers,
                           json={'name': f'Probe Team {suffix}', 'description': 'probe'})
        project = client.post('/api/projects', headers=headers, json={
            'name': f'Probe Project {suffix}', 'code': 'PB' + suffix[:4].upper(),
            'team_id': team.json()['id']})
        assert project.status_code == 201, project.text

        response = client.post(f"/api/projects/{project.json()['id']}/documents",
                               headers=headers,
                               files={'file': ('spec.txt', b'Project specification body',
                                               'text/plain')})

    assert response.status_code == 201, response.text
    assert storage_probe.probed == 1, "the probe never ran: the blocking path was bypassed"
    assert storage_probe.off_the_event_loop, (
        f"add_project_document ran on a thread with a live event loop "
        f"({storage_probe.running_loop!r} in {storage_probe.thread_name!r})")


def test_the_attachment_route_does_its_blocking_work_off_the_event_loop(storage_probe):
    with TestClient(app) as client:
        headers, _ = workspace(client)
        suffix = uuid4().hex[:8]
        team = client.post('/api/teams', headers=headers,
                           json={'name': f'Probe Team {suffix}', 'description': 'probe'})
        project = client.post('/api/projects', headers=headers, json={
            'name': f'Probe Project {suffix}', 'code': 'PA' + suffix[:4].upper(),
            'team_id': team.json()['id']})
        task = client.post('/api/tasks', headers=headers, json={
            'project_id': project.json()['id'], 'title': f'Probe task {suffix}'})
        assert task.status_code == 201, task.text

        response = client.post(f"/api/tasks/{task.json()['id']}/attachments",
                               headers=headers,
                               files={'file': ('evidence.png', b'\x89PNG\r\n\x1a\n' + b'x' * 64,
                                               'image/png')})

    assert response.status_code == 201, response.text
    assert storage_probe.probed == 1, "the probe never ran: the blocking path was bypassed"
    assert storage_probe.off_the_event_loop, (
        f"add_attachment ran on a thread with a live event loop "
        f"({storage_probe.running_loop!r} in {storage_probe.thread_name!r})")


def test_the_probe_would_actually_notice_a_live_event_loop():
    """The assertion above is only as good as the probe behind it.

    A test that cannot fail is not a test. Run the identical probe on a real
    event loop and confirm it sees one -- otherwise `off_the_event_loop` would be
    vacuously true and the three tests above would pass forever.
    """
    probe = LoopProbe()

    async def on_the_loop():
        probe.record('async')

    asyncio.run(on_the_loop())

    assert probe.running_loop is not None, (
        "the probe failed to detect a running event loop, so it cannot be trusted "
        "to detect one being absent")
    assert not probe.off_the_event_loop


@pytest.mark.parametrize("route,expected", [
    ("/api/project-drafts/assist", "Documents must be 5 MB or smaller"),
    ("/api/tasks/{task_id}/attachments", "Attachments must be 5 MB or smaller"),
])
def test_an_oversize_upload_is_rejected_at_the_5_mb_boundary(route, expected):
    """MAX_UPLOAD_BYTES + 1 is the rejection point, and it still rejects.

    The bounded read is the reason this is cheap: the handler reads one byte past
    the limit to decide, rather than pulling the whole body into memory first.
    """
    oversized = b'A' * (MAX_UPLOAD_BYTES + 1)

    with TestClient(app) as client:
        headers, _ = workspace(client)
        if "task_id" in route:
            suffix = uuid4().hex[:8]
            team = client.post('/api/teams', headers=headers,
                               json={'name': f'Probe Team {suffix}', 'description': 'probe'})
            project = client.post('/api/projects', headers=headers, json={
                'name': f'Probe Project {suffix}', 'code': 'PB' + suffix[:4].upper(),
                'team_id': team.json()['id']})
            task = client.post('/api/tasks', headers=headers, json={
                'project_id': project.json()['id'], 'title': f'Probe task {suffix}'})
            path = route.format(task_id=task.json()['id'])
        else:
            path = route

        response = client.post(path, headers=headers,
                               files={'file': ('big.txt', oversized, 'text/plain')})

    assert response.status_code == 413, response.text
    assert expected in response.json()['detail']


def test_a_project_document_just_over_the_limit_is_rejected():
    oversized = b'A' * (MAX_UPLOAD_BYTES + 1)

    with TestClient(app) as client:
        headers, _ = workspace(client)
        suffix = uuid4().hex[:8]
        team = client.post('/api/teams', headers=headers,
                           json={'name': f'Probe Team {suffix}', 'description': 'probe'})
        project = client.post('/api/projects', headers=headers, json={
            'name': f'Probe Project {suffix}', 'code': 'PD' + suffix[:4].upper(),
            'team_id': team.json()['id']})
        response = client.post(f"/api/projects/{project.json()['id']}/documents",
                               headers=headers,
                               files={'file': ('big.txt', oversized, 'text/plain')})

    assert response.status_code == 413, response.text
    assert "Documents must be 5 MB or smaller" in response.json()['detail']


def test_an_upload_at_the_limit_is_accepted_not_rejected():
    """The boundary is inclusive: exactly 5 MB must still be served.

    Without this the 413 tests alone would pass even if the handler rejected
    everything, including files that fit.
    """
    at_limit = b'A' * MAX_UPLOAD_BYTES

    with TestClient(app) as client:
        headers, _ = workspace(client)
        suffix = uuid4().hex[:8]
        team = client.post('/api/teams', headers=headers,
                           json={'name': f'Probe Team {suffix}', 'description': 'probe'})
        project = client.post('/api/projects', headers=headers, json={
            'name': f'Probe Project {suffix}', 'code': 'PL' + suffix[:4].upper(),
            'team_id': team.json()['id']})
        task = client.post('/api/tasks', headers=headers, json={
            'project_id': project.json()['id'], 'title': f'Probe task {suffix}'})
        response = client.post(f"/api/tasks/{task.json()['id']}/attachments",
                               headers=headers,
                               files={'file': ('exact.txt', at_limit, 'text/plain')})

    assert response.status_code == 201, response.text
    assert response.json()['file_name'] == 'exact.txt'


def test_successful_uploads_still_return_their_payloads():
    """Off-loading the work must not have changed what a success returns."""
    with TestClient(app) as client:
        headers, _ = workspace(client)

        draft = client.post('/api/project-drafts/assist', headers=headers,
                            files={'file': ('brief.txt', b'Project: Launch readiness',
                                            'text/plain')})
        assert draft.status_code == 201
        assert draft.json()['document']['file_name'] == 'brief.txt'
        assert draft.json()['draft'], "the draft body went missing"

        suffix = uuid4().hex[:8]
        team = client.post('/api/teams', headers=headers,
                           json={'name': f'Probe Team {suffix}', 'description': 'probe'})
        project = client.post('/api/projects', headers=headers, json={
            'name': f'Probe Project {suffix}', 'code': 'PP' + suffix[:4].upper(),
            'team_id': team.json()['id']})
        document = client.post(f"/api/projects/{project.json()['id']}/documents",
                               headers=headers,
                               files={'file': ('spec.txt', b'Specification text', 'text/plain')})
        assert document.status_code == 201
        assert document.json()['file_name'] == 'spec.txt'
