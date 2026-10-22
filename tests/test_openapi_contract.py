"""Keep the downloadable reference aligned with routes and actual wire responses."""
import copy
import json
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from fastapi.routing import APIRoute, APIWebSocketRoute

from feedback.api import create_app
from feedback.config import Config
from feedback.openapi import SUMMARIES


@pytest.fixture(scope='module')
def documented_app(tmp_path_factory):
    directory = tmp_path_factory.mktemp('openapi')
    return create_app(Config(directory / 'feedback.sqlite', Fernet.generate_key().decode(),
                             'temporary-setup-token-for-reference-tests', worker=False))


def operations(spec):
    return {(method, path): op for path, item in spec['paths'].items()
            for method, op in item.items()}


def resolve(spec, value):
    if '$ref' not in value:
        return value
    target = spec
    for part in value['$ref'].removeprefix('#/').split('/'):
        target = target[part.replace('~1', '/').replace('~0', '~')]
    return target


def test_reference_exports_every_runtime_operation(documented_app):
    spec = documented_app.openapi()
    actual = {(method.lower(), route.path) for route in documented_app.routes
              if isinstance(route, APIRoute) and route.include_in_schema
              for method in route.methods}
    assert set(operations(spec)) == actual
    assert set(SUMMARIES) == actual  # A new route needs a concrete summary too.
    for op in operations(spec).values():
        assert op['description'] and op['tags'] and op['summary']
    socket_paths = {route.path for route in documented_app.routes
                    if isinstance(route, APIWebSocketRoute)}
    assert set(spec['x-websockets']) == socket_paths
    assert not socket_paths.intersection(spec['paths'])


def test_committed_reference_matches_temporary_runtime(documented_app):
    saved = json.loads((Path(__file__).parents[1] / 'docs/openapi.json').read_text())
    assert saved == documented_app.openapi()


def test_references_security_and_response_shapes_are_complete(documented_app):
    spec = documented_app.openapi()

    def walk(value):
        if isinstance(value, dict):
            if '$ref' in value:
                assert value['$ref'].startswith('#/'), value['$ref']
                assert resolve(spec, value)
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(spec)
    for (method, path), op in operations(spec).items():
        assert '422' not in op['responses'], (method, path)
        parameters = [(p['in'], p['name']) for p in op.get('parameters', [])]
        assert len(parameters) == len(set(parameters)), (method, path)
        for alternative in op['security']:
            assert set(alternative).issubset(spec['components']['securitySchemes'])
        for code, response in op['responses'].items():
            if code == '204':
                assert 'content' not in response, (method, path)
            elif code.startswith('2'):
                assert response['content']['application/json']['schema'], (method, path)
        if path.startswith('/api/v1/'):
            assert {'400', '401', '403', '404', '409', '413', '429', '500', '503'} <= set(op['responses'])


def test_administrator_routes_do_not_advertise_project_or_anonymous_access(documented_app):
    for (method, path), op in operations(documented_app.openapi()).items():
        admin = (path.startswith('/api/v1/admin/') or '/app-data/' in path
                 or path.startswith('/api/v1/ai/') or '/ai/' in path or path.endswith('/ai')
                 or path.endswith('/archive') or '/keys' in path
                 or (path in ('/api/v1/projects', '/api/v1/projects/{project}') and method in ('post', 'patch')))
        if admin:
            assert op['security'] == [{'APIKeyCookie': []}, {'NativeBearer': []}], (method, path)
            assert 'Administrator account required' in op['description']


def test_authentication_transport_boundaries_are_explicit(documented_app):
    spec = documented_app.openapi()
    ops = operations(spec)
    participant = ops['post', '/api/v1/boards/{board}/requests']
    assert participant['security'] == [{'APIKeyCookie': []}, {'NativeBearer': []}, {'ProjectBearer': []}]
    assert 'participate' in participant['description'] and 'opaque' in participant['description']
    headers = {p['name']: p for p in participant['parameters'] if p['in'] == 'header'}
    assert headers['X-CSRF-Token']['required'] is False
    assert 'only with a feedback_session cookie' in headers['X-CSRF-Token']['description']
    assert 'do not use CSRF' in participant['description']
    for path in ('/api/v1/auth/login', '/api/v1/auth/native/login', '/api/v1/auth/register', '/api/v1/setup'):
        assert ops['post', path]['security'] == []
    assert 'Participant accounts cannot use this login' in ops['post', '/api/v1/auth/native/login']['description']
    report = ops['post', '/api/v1/chat/conversations/{conversation_id}/proposals/{proposal_id}/confirm']
    assert report['security'] == [{'ChatCustomer': []}, {'ChatVisitor': []}]
    revoke = ops['post', '/api/v1/chat/projects/{project}/customer-sessions/revoke']
    assert 'Revokes every chat credential' in revoke['description']
    assert 'returned cc_' not in revoke['description']
    assert {} not in ops['get', '/api/v1/requests/{request_id}/vote']['security']


@pytest.mark.parametrize('method,path,kib', [
    ('post', '/api/v1/boards/{board}/requests', 96),
    ('post', '/api/v1/chat/conversations/{conversation_id}/messages', 32),
    ('put', '/api/v1/chat/conversations/{conversation_id}/proposals/{proposal_id}', 400),
    ('put', '/api/v1/chat/conversations/{conversation_id}/reports/{report_id}', 400),
    ('post', '/api/v1/projects/{project}/ai/knowledge/markdown', 400),
    ('put', '/api/v1/projects/{project}/ai/knowledge/sources/{source_id}/markdown', 400),
    ('put', '/api/v1/projects/{project}/ai/personality', 400),
    ('put', '/api/v1/projects/{project}/ai', 400),
])
def test_body_limits_match_request_guard(documented_app, method, path, kib):
    operation = documented_app.openapi()['paths'][path][method]
    assert f'{kib} KiB' in operation['responses']['413']['description']


def test_webhooks_cover_subscription_allowlist_and_test_delivery(documented_app):
    spec = documented_app.openapi()
    allowed = set(spec['components']['schemas']['WebhookInput']['properties']['events']['items']['enum'])
    subscribable = {name for name, item in spec['webhooks'].items() if item['post']['x-subscribable']}
    assert subscribable == allowed
    assert set(spec['webhooks']) == allowed | {'chat.support_mode_changed', 'webhook.test'}
    for item in spec['webhooks'].values():
        headers = {p['name'] for p in item['post']['parameters'] if p['required']}
        assert headers == {'X-Feedback-Event-ID', 'X-Feedback-Timestamp', 'X-Feedback-Signature'}
    assert 'not currently selectable' in spec['webhooks']['chat.support_mode_changed']['post']['description']
    schemas = spec['components']['schemas']
    assert 'customer' not in schemas['ChatAIMessageCreatedEvent']['properties']
    assert 'body' not in schemas['ChatHumanMessageCreatedEvent']['properties']['message']['properties']


def test_connected_success_schemas_match_actual_wire_responses(workflow):
    from test_connected_protocol import EXAMPLES
    w = workflow
    spec = w['app'].openapi()
    client = w['admin']
    root = '/api/v1/projects/' + w['p']['id'] + '/app-data'
    template = '/api/v1/projects/{project}/app-data'

    def check(method, url, documented_path, body=None):
        response = client.request(method, url, **({'json': body} if body is not None else {}))
        assert response.status_code == 200, response.text
        schema = resolve(spec, spec['paths'][documented_path][method.lower()]['responses']['200']['content']['application/json']['schema'])
        data = response.json()
        assert set(schema['required']) == set(data)
        assert set(schema['properties']) == set(data)
        return data

    for path in ('/healthz', '/readyz', '/api/v1/admin/connected-apps', '/api/v1/admin/connected-apps/preferences'):
        check('GET', path, path)
    preferences = check('GET', '/api/v1/admin/connected-apps/preferences', '/api/v1/admin/connected-apps/preferences')
    check('PUT', '/api/v1/admin/connected-apps/preferences', '/api/v1/admin/connected-apps/preferences', {'version': preferences['version'], 'timezone': 'America/Chicago'})
    check('GET', root + '/connection', template + '/connection')
    saved = check('PUT', root + '/connection', template + '/connection', {'version': 0, 'base_url': 'https://app.example.com', 'token': 'test-only-connected-token'})

    async def transport(url, token, operation, query, deadline):
        return copy.deepcopy(EXAMPLES[operation])

    w['app'].state.connected_apps.transport = transport
    tested = check('POST', root + '/connection/test', template + '/connection/test', {'version': saved['version']})
    manifest = resolve(spec, spec['components']['schemas']['ConnectedTestOutput']['properties']['manifest'])
    assert set(manifest['required']) <= set(tested['manifest']) <= set(manifest['properties'])
    enabled = check('POST', root + '/connection/enable', template + '/connection/enable', {'version': saved['version'], 'receipt': tested['receipt'], 'link_origins': [], 'support_linked': True})
    check('POST', root + '/related/query', template + '/related/query', {'version': enabled['version'], 'user_id': 'unknown-exact-id'})
    disabled = check('POST', root + '/connection/disable', template + '/connection/disable', {'version': enabled['version']})
    check('POST', root + '/connection/remove', template + '/connection/remove', {'version': disabled['version']})


def test_websocket_ticket_protocol_and_message_contact_boundary(documented_app):
    spec = documented_app.openapi()
    socket = spec['x-websockets']['/api/v1/chat/ws']
    assert socket['ticketOperation'] == 'POST /api/v1/chat/tickets'
    assert socket['limits']['ticketLifetimeSeconds'] == 60
    assert socket['limits']['helloTimeoutSeconds'] == 5
    frames = resolve(spec, socket['clientFrames'])['oneOf']
    message = next(frame for frame in frames if frame['properties']['type']['const'] == 'message')
    assert set(message['properties']['message']['properties']) == {'client_message_id', 'body', 'context'}
    assert message['properties']['message']['required']==['client_message_id','body']
    assert 'follow_up_email' in spec['components']['schemas']['MessageInput']['properties']
    assert 'through HTTP' in socket['description']
