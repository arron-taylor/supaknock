"""Exercise the published integration example against the real application."""
import re
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient



ROOT = Path(__file__).resolve().parents[1]


def test_published_feedback_example_preserves_identity_and_private_state(workflow, monkeypatch):
    w = workflow
    for key, value in {
        'SUPAKNOCK_URL': w['cfg'].base_url,
        'SUPAKNOCK_PROJECT': w['p']['identifier'],
        'SUPAKNOCK_BOARD': w['b']['identifier'],
        'SUPAKNOCK_API_KEY': w['k']['secret'],
    }.items():
        monkeypatch.setenv(key, value)

    # Route the example's unchanged HTTP calls into the real ASGI application.
    def documented_client(**kwargs):
        return TestClient(w['app'], base_url=kwargs['base_url'], headers=kwargs['headers'])

    monkeypatch.setattr(httpx, 'Client', documented_client)
    document = (ROOT / 'docs/integration.md').read_text()
    source = re.search(r'```python\n(.*?)\n```', document, re.S).group(1)
    namespace = {}
    exec(compile(source, 'docs/integration.md', 'exec'), namespace)
    user = SimpleNamespace(id=' 000123 Case ')
    created = namespace['submit_feedback'](user, 'Offline reading', 'Read saved articles offline.')
    assert created['title'] == 'Offline reading'
    assert created['moderation'] == 'pending'
    assert w['anon'].get('/api/v1/requests/' + created['id']).status_code == 404
    own = w['trusted'].get('/api/v1/me/requests', params={'external_user_id': user.id})
    assert own.status_code == 200
    assert [item['id'] for item in own.json()['items']] == [created['id']]
    assert w['trusted'].get('/api/v1/me/requests', params={'external_user_id': user.id.strip().lower()}).json()['items'] == []

    # Bad setup fails during discovery rather than creating a request elsewhere.
    monkeypatch.setenv('SUPAKNOCK_BOARD', 'missing-board')
    with pytest.raises(ValueError, match='configured feedback board was not found'):
        namespace['submit_feedback'](user, 'Not submitted', '')
    assert len(w['trusted'].get('/api/v1/me/requests', params={'external_user_id': user.id}).json()['items']) == 1



def test_application_serves_schema_without_marketing_guide(installation):
    app, cfg, admin, user, anon = installation
    assert anon.get("/api-docs").status_code == 404
    specification = anon.get("/api/v1/openapi.json")
    assert specification.status_code == 200
    assert specification.json()["paths"]
