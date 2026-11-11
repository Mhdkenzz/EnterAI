from uuid import uuid4

from fastapi.testclient import TestClient

from app.main import app


def test_replacing_prerequisites_keeps_unchanged_links():
    with TestClient(app) as client:
        suffix = uuid4().hex[:8].upper()
        registration = client.post('/api/auth/register', json={
            'organization_name': f'Prerequisite test {suffix}',
            'name': f'Prerequisite admin {suffix}',
            'email': f'prerequisite-{suffix}@example.com',
            'password': f'Isolated-test-{suffix}-password',
        })
        assert registration.status_code == 200, registration.text
        headers = {'Authorization': 'Bearer ' + registration.json()['token']}
        project_response = client.post('/api/projects', headers=headers, json={'name': f'Prerequisite test {suffix}', 'code': f'PRE{suffix}'})
        assert project_response.status_code == 201, project_response.text
        project = project_response.json()
        tasks = [client.post('/api/tasks', headers=headers, json={'project_id': project['id'], 'title': title}).json() for title in ('First', 'Second', 'Review')]
        url = '/api/tasks/' + tasks[2]['id']
        first, second = tasks[0]['id'], tasks[1]['id']
        assert client.patch(url, headers=headers, json={'depends_on': [first]}).status_code == 200
        expanded = client.patch(url, headers=headers, json={'depends_on': [first, second]})
        assert expanded.status_code == 200
        assert {item['id'] for item in expanded.json()['depends_on']} == {first, second}
        reduced = client.patch(url, headers=headers, json={'depends_on': [second]})
        assert reduced.status_code == 200
        assert {item['id'] for item in reduced.json()['depends_on']} == {second}
        assert client.patch(url, headers=headers, json={'depends_on': [second]}).status_code == 200
