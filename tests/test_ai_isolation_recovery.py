"""Integrated AI16 gaps: supported restore, adapter changes and live page links.

All model/provider work is an in-process fixture; HTTP is the actual application
transport and durable state uses real SQLite databases.
"""
from dataclasses import replace
from types import MappingProxyType
from uuid import uuid4

from fastapi.testclient import TestClient
import httpx
import pytest

from feedback.api import create_app
from feedback.config import Config
from feedback.maintenance import backup
from feedback.security import Vault, digest
from test_ai_authoring import authoring, FixtureProvider
from test_ai_chat_isolation import configure
from test_ai_reports_api import reports, customer, prepare, confirmation
from test_chat import ORIGIN, send


def restored_client(app, original):
    client = TestClient(app)
    client.headers.update(original.headers)
    client.cookies.update(original.cookies)
    return client


def no_http_network(monkeypatch):
    def denied(*args, **kwargs):
        pytest.fail('An external HTTP request is forbidden in this recovery test')
    async def denied_async(*args, **kwargs):
        denied()
    monkeypatch.setattr(httpx.HTTPTransport, 'handle_request', denied)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, 'handle_async_request', denied_async)


def test_supported_bundle_restores_guest_receipts_history_and_human_access_without_models(reports, tmp_path, monkeypatch):
    ai = reports
    client, cv, token = customer(ai)
    prefix = '/api/v1/chat/conversations/' + cv['id']
    first, first_url = prepare(ai, client, cv, title='Pending restored report')
    first_confirmation = confirmation(first)
    pending = client.post(first_url + '/confirm', json=first_confirmation).json()
    second, second_url = prepare(ai, client, cv, kind='bug', title='Accepted restored report')
    accepted = client.post(second_url + '/confirm', json=confirmation(second)).json()
    response = ai['admin'].post('/api/v1/requests/' + accepted['request_id'] + '/accept', json={'version': accepted['version']})
    assert response.status_code == 200, response.text
    accepted = client.get(prefix + '/reports/' + accepted['id']).json()
    unfinished, unfinished_url = prepare(ai, client, cv, title='Unconfirmed before backup')
    before_history = {row['id']: ai['s'].history(row['request_id'], 100, 0) for row in (pending, accepted)}
    before_messages = client.get(prefix + '/messages').json()['items']
    with ai['s'].transaction() as c:
        originals = {table: [tuple(row) for row in c.execute('SELECT * FROM ' + table + ' ORDER BY 1')]
                     for table in ('ai_guest_owners', 'ai_reports', 'ai_report_confirmations', 'request_events', 'admin_notifications')}

    bundle = tmp_path / 'supported-bundle'
    backup(ai['cfg'], bundle)
    assert {path.name for path in bundle.iterdir()} == {'feedback.sqlite', 'application.key', 'setup.token', 'README.txt'}
    assert token.encode() not in (bundle / 'feedback.sqlite').read_bytes()
    # Use the documented restoration inputs, including persisted effective keys.
    monkeypatch.setenv('DATABASE_URL', 'sqlite:///' + str(bundle / 'feedback.sqlite'))
    monkeypatch.setenv('PUBLIC_BASE_URL', ai['cfg'].base_url)
    monkeypatch.delenv('APPLICATION_SECRET', raising=False)
    monkeypatch.delenv('SETUP_TOKEN', raising=False)
    no_http_network(monkeypatch)
    config = replace(Config.from_env(), worker=False)
    assert config.secret == ai['cfg'].secret and config.setup_token == ai['cfg'].setup_token
    app = create_app(config)
    store = app.state.storage
    owner, admin, public = restored_client(app, client), restored_client(app, ai['admin']), TestClient(app)
    assert store.ai_runtime(ai['principal'])['state'] == 'not_downloaded'
    assert not (bundle / 'models').exists()
    assert not store.ai_settings(ai['p']['id'], ai['principal'])['settings']['enabled']
    assert not app.state.ai_chat_worker.run_once()
    assert Vault(config.secret).reveal(store.ai_provider_snapshot(ai['p']['id'], ai['principal'],
        store.ai_settings(ai['p']['id'], ai['principal'])['version'])['protected_key']) == 'fixture-provider-secret'
    with store.transaction() as c:
        for table, rows in originals.items():
            assert [tuple(row) for row in c.execute('SELECT * FROM ' + table + ' ORDER BY 1')] == rows
    assert owner.get(prefix + '/messages').json()['items'] == before_messages
    assert owner.get(prefix + '/reports/' + pending['id']).json() == pending
    assert owner.get(prefix + '/reports/' + accepted['id']).json() == accepted
    assert owner.post(first_url + '/confirm', json=first_confirmation).json() == pending
    assert public.get('/api/v1/requests/' + pending['request_id']).status_code == 404
    assert public.get('/api/v1/requests/' + accepted['request_id']).status_code == 200
    assert not owner.get(unfinished_url).json()['can_confirm']
    assert owner.post(unfinished_url + '/confirm', json=confirmation(unfinished)).status_code == 409
    for report in (pending, accepted):
        assert store.history(report['request_id'], 100, 0) == before_history[report['id']]
    locked_url = prefix + '/reports/' + accepted['id']
    assert owner.put(locked_url, json={'version': accepted['version'], 'title': 'Too late', 'description': ''}).status_code == 409
    assert owner.delete(locked_url, params={'version': accepted['version']}).status_code == 409
    assert store.history(accepted['request_id'], 100, 0) == before_history[accepted['id']]
    changed = owner.put(prefix + '/reports/' + pending['id'], json={
        'version': pending['version'], 'title': 'Restored owner correction', 'description': pending['description']})
    assert changed.status_code == 200, changed.text
    current = owner.get(prefix).json()
    assert owner.put(prefix + '/support-mode', json={'mode': 'waiting_human', 'mode_version': current['mode_version']}).status_code == 200
    assert send(owner, cv, 'A human can still help after restoration.').status_code == 201
    assert send(admin, cv, 'A human response after restoration.').status_code == 201

    # Credential cleanup removes access, not the immutable guest/report records.
    with store.transaction(True) as c:
        c.execute('DELETE FROM chat_sessions WHERE token_hash=?', (digest(token),))
    second_bundle = tmp_path / 'after-cleanup'
    backup(config, second_bundle)
    reopened = create_app(replace(config, database_path=second_bundle / 'feedback.sqlite'))
    old_owner = restored_client(reopened, client)
    assert old_owner.get(prefix + '/reports').status_code == 401
    new_guest = TestClient(reopened, headers={'Origin': ORIGIN})
    created = new_guest.post('/api/v1/chat/projects/' + ai['p']['id'] + '/sessions')
    assert created.status_code == 201, created.text
    new_guest.headers['X-Chat-Token'] = created.json()['token']
    assert new_guest.get(prefix + '/reports/' + pending['id']).status_code == 404
    recovered_admin = restored_client(reopened, ai['admin'])
    summary = recovered_admin.get(prefix).json()
    assert summary['report_count'] == 2 and all(row['guest'] for row in summary['reports'])
    assert reopened.state.storage.history(accepted['request_id'], 100, 0) == before_history[accepted['id']]
    with reopened.state.storage.transaction() as c:
        assert c.execute('SELECT count(*) FROM ai_guest_owners').fetchone()[0] == 1
        assert c.execute('SELECT count(*) FROM ai_reports').fetchone()[0] == 2


@pytest.mark.parametrize('change', ['provider', 'model', 'key'])
@pytest.mark.parametrize('phase', ['queued', 'dispatched'])
def test_reviewed_adapter_model_or_key_switch_fences_old_work_and_keeps_index(reports, monkeypatch, change, phase):
    import feedback.ai_providers as adapters
    import feedback.ai_settings as settings_service

    class SecondFixture(FixtureProvider):
        id, name = 'fixture-second', 'Fixture second adapter'

    registry = MappingProxyType(dict(adapters.PROVIDERS) | {'fixture-second': SecondFixture})
    monkeypatch.setattr(adapters, 'PROVIDERS', registry)
    monkeypatch.setattr(settings_service, 'PROVIDERS', registry)
    ai, old, second = reports, reports['fake'], SecondFixture()
    generation = ai['app'].state.ai_generation
    selected = []
    def factory(identity):
        selected.append(identity)
        assert identity in registry
        return second if identity == 'fixture-second' else old
    generation.provider_factory = factory
    assert ai['admin'].get('/api/v1/ai/providers').json()[-1]['id'] == 'fixture-second'
    draft_owner, draft_cv, _ = customer(ai)
    draft, draft_url = prepare(ai, draft_owner, draft_cv)
    owner, cv, _ = customer(ai)
    assert send(owner, cv, 'When should I restart?').status_code == 201
    chunks = ai['s'].ai_knowledge_chunks(ai['p']['id'], ai['principal'])
    initial = ai['s'].ai_settings(ai['p']['id'], ai['principal'])
    usage = ai['s'].ai_usage(ai['p']['id'], ai['principal'])['reserved_requests']
    settings_url = '/api/v1/projects/' + ai['p']['id'] + '/ai'
    chosen = {'provider': 'fixture-second'} if change == 'provider' else {'model': 'Exact/Second:Model'} if change == 'model' else {}
    def switch():
        body = {'version': initial['version'], 'settings': initial['settings'] | chosen | {'enabled': False}}
        if change in ('provider', 'key'):
            body['key'] = 'second-fixture-provider-secret'
        response = ai['admin'].put(settings_url, json=body)
        assert response.status_code == 200, response.text
        saved = response.json()
        assert saved['capabilities'] == {}
        assert saved['credential_version'] == initial['credential_version'] + (change != 'model')
        if change != 'model':
            assert saved['catalogue'] == []
        if change == 'provider':
            assert saved['settings']['model'] is None
            # A new provider needs its own catalogue and an explicit model
            # selection; the old provider's exact model ID cannot carry over.
            saved = ai['s'].ai_store_provider_evidence(ai['p']['id'], ai['principal'], saved['version'],
                saved['credential_version'], None, catalogue=['Exact/Second:Model'])
            response = ai['admin'].put(settings_url, json={'version': saved['version'],
                'settings': saved['settings'] | {'model': 'Exact/Second:Model'}})
            assert response.status_code == 200, response.text
            saved = response.json()
        # A fresh exact model probe is represented by normalized fixture evidence.
        saved = ai['s'].ai_store_provider_evidence(ai['p']['id'], ai['principal'], saved['version'],
            saved['credential_version'], saved['settings']['model'], catalogue=[saved['settings']['model']],
            capabilities={'text_generation': True, 'tool_calling': True, 'streaming': False, 'structured_output': False})
        enabled = ai['admin'].put(settings_url, json={'version': saved['version'], 'settings': saved['settings'] | {'enabled': True}})
        assert enabled.status_code == 200, enabled.text
    if phase == 'queued':
        switch()
    else:
        old.hook = switch
    assert not ai['app'].state.ai_chat_worker.run_once()
    assert not second.calls  # No cross-provider fallback for an old turn.
    assert ai['s'].ai_usage(ai['p']['id'], ai['principal'])['reserved_requests'] == usage + (phase == 'dispatched')
    assert ai['s'].ai_knowledge_chunks(ai['p']['id'], ai['principal']) == chunks
    assert draft_owner.get(draft_url).json()['state'] == 'cancelled'
    assert draft_owner.post(draft_url + '/confirm', json=confirmation(draft)).status_code == 409
    messages = owner.get('/api/v1/chat/conversations/' + cv['id'] + '/messages').json()['items']
    assert all(row['sender'] != 'ai' for row in messages)
    assert send(owner, cv, 'A fresh question after re-verification.').status_code == 201
    assert ai['app'].state.ai_chat_worker.run_once()
    current_adapter = second if change == 'provider' else old
    assert selected[-1] == chosen.get('provider', 'kodekey')
    assert current_adapter.calls[-1]['model'] == ('Exact/Second:Model' if change in ('provider', 'model') else initial['settings']['model'])
    assert current_adapter.calls[-1]['key'] == ('second-fixture-provider-secret' if change in ('provider', 'key') else 'fixture-provider-secret')
    assert all(call['key'] != 'fixture-provider-secret' for call in second.calls)
    state = owner.get('/api/v1/chat/conversations/' + cv['id']).json()
    assert owner.put('/api/v1/chat/conversations/' + cv['id'] + '/support-mode', json={
        'mode': 'waiting_human', 'mode_version': state['mode_version']}).status_code == 200


@pytest.mark.parametrize('kind', ['feature', 'bug'])
@pytest.mark.parametrize('destination', ['specific', 'general', 'native'])
def test_destination_change_during_open_draft_requires_new_review_without_fetch(reports, monkeypatch, kind, destination):
    ai = reports
    no_http_network(monkeypatch)
    original = 'https://customer.example/original?view=feedback#new'
    general = 'https://customer.example/general?tab=ideas#submit'
    specific = 'https://customer.example/' + kind + '?source=website#new'
    configure(ai['env'], feedback_url=general, **{kind + '_feedback_url': original})
    owner, cv, token = customer(ai)
    draft, url = prepare(ai, owner, cv, kind=kind)
    configure(ai['env'], feedback_url=None if destination == 'native' else general,
              **{kind + '_feedback_url': specific if destination == 'specific' else None})
    expected = specific if destination == 'specific' else general if destination == 'general' else ai['cfg'].base_url + '/boards/' + ai['report_board']['id']
    current = owner.get(url).json()
    assert current['feedback_url'] == expected and not current['can_confirm']
    assert owner.post(url + '/confirm', json=confirmation(draft)).status_code == 409
    assert token not in expected and cv['id'] not in expected and draft['id'] not in expected
    fresh, fresh_url = prepare(ai, owner, cv, kind=kind)
    receipt = owner.post(fresh_url + '/confirm', json=confirmation(fresh))
    assert receipt.status_code == 200, receipt.text
    assert receipt.json()['feedback_url'] == expected and receipt.json()['public_url'] is None
    assert len(ai['fake'].calls) == 2


@pytest.mark.parametrize('identity', ['unreviewed', 'https://example.com/adapter', 'feedback.untrusted:Adapter'])
def test_unreviewed_adapter_names_cannot_mutate_configuration(reports, identity):
    ai = reports
    before = ai['s'].ai_settings(ai['p']['id'], ai['principal'])
    response = ai['admin'].put('/api/v1/projects/' + ai['p']['id'] + '/ai', json={
        'version': before['version'], 'settings': before['settings'] | {'provider': identity, 'enabled': False}})
    assert response.status_code == 400
    assert ai['s'].ai_settings(ai['p']['id'], ai['principal']) == before
    assert not ai['fake'].calls
