"""Project settings must control only that project's website chat."""
from fastapi.testclient import TestClient

from feedback.api import create_app
from test_chat import send, visitor


def test_chat_configuration_isolated_between_projects(workflow):
    w = workflow
    first, second = w['p'], w['q']
    first_url = '/api/v1/admin/chat/projects/' + first['id']
    second_url = '/api/v1/admin/chat/projects/' + second['identifier']
    first_origin, second_origin = 'https://first.example', 'https://second.example'

    for url in (first_url, second_url):
        settings = w['admin'].get(url).json()
        assert settings['enabled'] is False
        assert settings['origins'] == []
        assert settings['version'] == 0

    saved = w['admin'].put(first_url, json={
        'enabled': True, 'origins': [first_origin], 'version': 0,
    })
    assert saved.status_code == 200, saved.text
    assert saved.json()['version'] == 1
    untouched = w['admin'].get(second_url).json()
    assert untouched['enabled'] is False and untouched['version'] == 0

    saved = w['admin'].put(second_url, json={
        'enabled': True, 'origins': [second_origin], 'version': 0,
    })
    assert saved.status_code == 200, saved.text
    assert saved.json()['version'] == 1
    second_settings = w['admin'].get(second_url).json()

    for project, own_origin, other_origin in (
        (first, first_origin, second_origin),
        (second, second_origin, first_origin),
    ):
        url = '/api/v1/chat/projects/' + project['identifier'] + '/config'
        public = w['anon'].get(url, headers={'Origin': own_origin})
        assert public.status_code == 200
        assert public.json()['project'] == project['identifier']
        assert 'origins' not in public.json()
        assert w['anon'].get(url, headers={'Origin': other_origin}).status_code == 403

    first_visitor, first_conversation, _ = visitor(w, first, first_origin)
    second_visitor, second_conversation, _ = visitor(w, second, second_origin)

    # An outdated settings page must not overwrite either project's settings.
    stale = w['admin'].put(first_url, json={
        'enabled': False, 'origins': [], 'version': 0,
    })
    assert stale.status_code == 409
    assert w['admin'].get(first_url).json()['enabled'] is True
    assert w['admin'].get(second_url).json() == second_settings

    disabled = w['admin'].put(first_url, json={
        'enabled': False, 'origins': [first_origin], 'version': 1,
    })
    assert disabled.status_code == 200, disabled.text
    assert disabled.json()['version'] == 2
    assert send(first_visitor, first_conversation).status_code == 403
    assert w['admin'].get(second_url).json() == second_settings
    assert send(second_visitor, second_conversation).status_code == 201
    assert w['anon'].get('/api/v1/chat/projects/glean/config', headers={
        'Origin': first_origin,
    }).status_code == 404

    # Reopening the installation preserves both independent configurations.
    fresh = TestClient(create_app(w['cfg']))
    assert fresh.get('/api/v1/chat/projects/glean/config', headers={
        'Origin': first_origin,
    }).status_code == 404
    public = fresh.get('/api/v1/chat/projects/another/config', headers={
        'Origin': second_origin,
    })
    assert public.status_code == 200 and public.json()['enabled'] is True


def test_chat_project_settings_require_administrator(workflow):
    w = workflow
    url = '/api/v1/admin/chat/projects/' + w['p']['id']
    before = w['admin'].get(url).json()
    for client in (w['anon'], w['user'], w['trusted'], w['manager'], w['other']):
        assert client.get(url).status_code == 403
        assert client.put(url, json={
            'enabled': True, 'origins': ['https://website.example'], 'version': 0,
        }).status_code == 403
    assert w['admin'].get(url).json() == before
