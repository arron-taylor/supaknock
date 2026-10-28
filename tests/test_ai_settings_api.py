"""Admin AI transport/service tests with real SQLite and zero provider network."""
from copy import deepcopy
from itertools import permutations
import asyncio
import time

from fastapi.testclient import TestClient
import pytest

from feedback.ai_domain import EMBEDDING_PROFILE, default_ai_settings
from feedback.ai_providers import ModelCapabilities, ProviderError
from feedback.ai_settings import resolve_feedback_destination
from feedback.errors import Error
from feedback.security import Vault, digest

SHIPPED_PROVIDERS = ('kodekey', 'gemini', 'mistral', 'groq', 'openai', 'anthropic', 'openrouter', 'ramp', 'custom')
PROVIDER_SWITCHES = tuple(permutations(SHIPPED_PROVIDERS, 2))


class FakeProvider:
    id, name = 'kodekey', 'Fixture provider'

    def __init__(self):
        self.calls = []
        self.models = ('Exact/Model:ID', 'text-only')
        self.error = None
        self.hook = None

    def action(self, action, key, model=None, tools=False):
        self.calls.append((action, key, model, tools))
        if self.hook:
            self.hook()
        if self.error:
            raise self.error

    async def list_models(self, key):
        self.action('catalogue', key)
        return self.models

    async def probe_model(self, key, model, *, tools=False):
        self.action('probe', key, model, tools)
        return ModelCapabilities(text_generation=True, tool_calling=tools)


@pytest.fixture
def ai(workflow):
    fake = FakeProvider()
    workflow['app'].state.ai_settings.provider_factory = lambda name: fake
    storage = workflow['app'].state.storage
    session = storage.session(digest(workflow['admin'].cookies['feedback_session']))
    principal = {'kind': 'admin', 'user_id': session['id'], 'session_id': session['session_id']}
    return workflow | {'fake': fake, 'storage': storage, 'principal': principal,
                       'url': '/api/v1/projects/' + workflow['p']['id'] + '/ai'}


def save(ai, settings=None, **extra):
    current = ai['admin'].get(ai['url']).json()
    response = ai['admin'].put(ai['url'], json={'version': current['version'],
        'settings': current['settings'] if settings is None else settings, **extra})
    assert response.status_code == 200, response.text
    return response.json()


def select_model(ai):
    current = save(ai, key='fixture-provider-secret')
    response = ai['admin'].post(ai['url'] + '/models/refresh', json={'version': current['version']})
    assert response.status_code == 200, response.text
    current = response.json()
    return save(ai, current['settings'] | {'model': 'Exact/Model:ID'})


def ready_knowledge(ai):
    s, p, pid = ai['storage'], ai['principal'], ai['p']['id']
    operation = s.ai_runtime_begin(p, 0, 'download')
    lease = s.ai_runtime_claim(time.time())
    assert s.ai_runtime_finish(operation['operation_id'], lease['lease_token'], manifest=EMBEDDING_PROFILE)
    source = s.ai_source_create(pid, p, 'markdown', 'Guide')
    revision = s.ai_revision_stage(pid, p, source['id'], source['version'], 'Guide', 'Original evidence.', 1)
    s.ai_revision_chunks(pid, p, revision['id'], [{'ordinal': 0, 'title': 'Guide',
        'content': 'Original evidence.', 'location': None, 'vector': [1.] + [0.] * 767}])
    source = s.ai_revision_publish(pid, p, revision['id'])
    s.ai_source_update(pid, p, source['id'], source['version'], active=True, approved=True)


def test_defaults_save_and_read_never_contact_provider(ai):
    current = ai['admin'].get(ai['url']).json()
    assert current['settings'] == default_ai_settings()
    assert ai['admin'].get(ai['url'] + '/jobs').json() == []
    assert current['has_key'] is False and current['capabilities'] == {}
    assert ai['admin'].get('/api/v1/ai/providers').json() == [
        {'id': 'kodekey', 'name': 'KodeKloud KodeKey'}, {'id': 'gemini', 'name': 'Google Gemini'},
        {'id': 'mistral', 'name': 'Mistral AI'}, {'id': 'groq', 'name': 'Groq'},
        {'id':'openai','name':'OpenAI'}, {'id':'anthropic','name':'Anthropic Claude'}, {'id':'openrouter','name':'OpenRouter'}, {'id':'ramp','name':'Ramp Router'}, {'id':'custom','name':'Custom OpenAI-compatible'}]
    assert ai['app'].openapi()['components']['schemas']['AISettingsInput']['properties']['provider']['enum'] == list(SHIPPED_PROVIDERS)
    saved = save(ai, key='fixture-provider-secret')
    assert saved['has_key'] and saved['credential_version'] == 1
    assert 'fixture-provider-secret' not in str(saved) and 'protected_key' not in saved
    snapshot = ai['storage'].ai_provider_snapshot(ai['p']['id'], ai['principal'], saved['version'])
    assert snapshot['protected_key'] != 'fixture-provider-secret'
    assert Vault(ai['cfg'].secret).reveal(snapshot['protected_key']) == 'fixture-provider-secret'
    assert ai['admin'].get(ai['url'] + '/feedback-destinations').json() == {'feature': None, 'bug': None}
    assert ai['fake'].calls == []
    preserved = save(ai, saved['settings'] | {'soul': 'Be kind.'})
    assert preserved['has_key'] and preserved['credential_version'] == saved['credential_version']
    removed = save(ai, key=None)
    assert not removed['has_key'] and removed['credential_version'] == 2


@pytest.mark.parametrize('client', ['anon', 'user', 'trusted', 'manager', 'other'])
@pytest.mark.parametrize('operation', ['read', 'save', 'refresh', 'probe', 'providers', 'destinations', 'jobs'])
def test_native_admin_only(ai, client, operation):
    client = ai[client]
    if operation == 'read':
        response = client.get(ai['url'])
    elif operation == 'save':
        response = client.put(ai['url'], json={'version': 0, 'settings': default_ai_settings(), 'key': 'fixture-key'})
    elif operation == 'providers':
        response = client.get('/api/v1/ai/providers')
    elif operation == 'jobs':
        response = client.get(ai['url'] + '/jobs')
    elif operation == 'destinations':
        response = client.get(ai['url'] + '/feedback-destinations')
    else:
        response = client.post(ai['url'] + ('/models/refresh' if operation == 'refresh' else '/model/probe'), json={'version': 0})
    assert response.status_code in (401, 403)
    assert ai['fake'].calls == []


def test_cookie_mutations_require_csrf_and_reject_chat_credentials(ai):
    client = TestClient(ai['app'])
    client.cookies.update(ai['admin'].cookies)
    body = {'version': 0, 'settings': default_ai_settings()}
    assert client.put(ai['url'], json=body).status_code == 403
    response = ai['admin'].put(ai['url'], json=body, headers={'X-Chat-Token': 'not-an-admin-credential'})
    assert response.status_code in (400, 403)
    assert ai['admin'].get(ai['url'], headers={'X-Chat-Token': 'not-an-admin-credential'}).status_code in (400, 403)
    assert ai['fake'].calls == []


@pytest.mark.parametrize('change', ['missing_ai', 'missing_guest', 'missing_tool', 'coerce_bool', 'extra', 'url', 'key', 'version_bool'])
def test_strict_complete_settings_and_safe_validation(ai, change):
    body = {'version': 0, 'settings': default_ai_settings()}
    if change == 'missing_ai': del body['settings']['enabled']
    elif change == 'missing_guest': del body['settings']['allow_guest_reports']
    elif change == 'missing_tool': del body['settings']['tools']['support_link']
    elif change == 'coerce_bool': body['settings']['enabled'] = 'false'
    elif change == 'extra': body['settings']['endpoint'] = 'https://untrusted.example'
    elif change == 'url': body['settings']['feedback_url'] = 'https://user:secret@untrusted.example'
    elif change == 'key': body['key'] = 'fixture-secret\ninvalid'
    else: body['version'] = False
    response = ai['admin'].put(ai['url'], json=body)
    assert response.status_code == 400
    assert 'fixture-secret' not in response.text and 'user:secret' not in response.text
    assert ai['storage'].ai_settings(ai['p']['id'], ai['principal'])['version'] == 0
    assert ai['fake'].calls == []


def test_catalogue_is_explicit_exact_and_not_capability_evidence(ai):
    missing = ai['admin'].post(ai['url'] + '/models/refresh', json={'version': 0})
    assert missing.status_code == 409 and ai['fake'].calls == []
    selected = select_model(ai)
    assert selected['settings']['model'] == 'Exact/Model:ID'
    assert selected['catalogue'] == ['Exact/Model:ID', 'text-only'] and selected['capabilities'] == {}
    assert len(ai['fake'].calls) == 1
    response = ai['admin'].post(ai['url'] + '/model/probe', json={'version': selected['version']})
    assert response.status_code == 200, response.text
    assert response.json()['capabilities'] == {'text_generation': True, 'tool_calling': False, 'streaming': False, 'structured_output': False}
    assert ai['fake'].calls[-1] == ('probe', 'fixture-provider-secret', 'Exact/Model:ID', False)
    assert not response.json()['settings']['enabled'] and not any(response.json()['settings']['tools'].values())
    # Changing case creates a different exact model ID, never an alias or fallback.
    selected = save(ai, selected['settings'] | {'model': 'exact/model:id'})
    assert selected['capabilities'] == {}
    response = ai['admin'].post(ai['url'] + '/model/probe', json={'version': selected['version']})
    assert response.status_code == 409 and len(ai['fake'].calls) == 2


def test_enabling_ai_and_each_tool_is_explicit_and_verified(ai):
    selected = select_model(ai)
    response = ai['admin'].put(ai['url'], json={'version': selected['version'], 'settings': selected['settings'] | {'enabled': True}})
    assert response.status_code == 409
    ready_knowledge(ai)
    ai['admin'].post(ai['url'] + '/model/probe', json={'version': selected['version']}).raise_for_status()
    enabled = save(ai, selected['settings'] | {'enabled': True})
    assert enabled['settings']['enabled'] and not any(enabled['settings']['tools'].values())
    tools = deepcopy(enabled['settings'])
    tools['tools']['create_bug_report'] = True
    response = ai['admin'].put(ai['url'], json={'version': enabled['version'], 'settings': tools})
    assert response.status_code == 409
    probe = ai['admin'].post(ai['url'] + '/model/probe', json={'version': enabled['version'], 'tools': True})
    assert probe.status_code == 200
    changed = save(ai, tools)
    assert changed['settings']['tools']['create_bug_report'] and not changed['settings']['allow_guest_reports']
    assert sum(changed['settings']['tools'].values()) == 1
    changed = save(ai, tools | {'allow_guest_reports': True, 'enabled': False})
    assert changed['settings']['allow_guest_reports'] and not changed['settings']['enabled']


@pytest.mark.parametrize('operation', ['models/refresh', 'model/probe'])
@pytest.mark.parametrize('race', ['rotate', 'revoke'])
def test_late_provider_result_cannot_verify_new_key_or_revoked_session(ai, operation, race):
    current = select_model(ai)
    def change():
        if race == 'rotate':
            ai['app'].state.ai_settings.save(ai['p']['id'], ai['principal'], current['version'], current['settings'], key='replacement-secret')
        else:
            ai['storage'].revoke_session(ai['principal']['session_id'])
    ai['fake'].hook = change
    response = ai['admin'].post(ai['url'] + '/' + operation, json={'version': current['version']})
    assert response.status_code == (409 if race == 'rotate' else 401)
    assert 'replacement-secret' not in response.text and 'fixture-provider-secret' not in response.text
    if race == 'rotate':
        updated = ai['admin'].get(ai['url']).json()
        assert updated['catalogue'] == [] and updated['capabilities'] == {}


def test_probe_failure_removes_verification_and_disables_enabled_ai(ai):
    selected = select_model(ai)
    ready_knowledge(ai)
    ai['admin'].post(ai['url'] + '/model/probe', json={'version': selected['version']}).raise_for_status()
    enabled = save(ai, selected['settings'] | {'enabled': True})
    ai['fake'].error = ProviderError('access_denied')
    response = ai['admin'].post(ai['url'] + '/model/probe', json={'version': enabled['version']})
    assert response.status_code == 409 and response.json()['error']['code'] == 'ai_provider_access_denied'
    current = ai['admin'].get(ai['url']).json()
    assert current['version'] == enabled['version'] + 1 and not current['settings']['enabled']
    assert not any(current['capabilities'].values())


def test_catalogue_access_loss_requires_new_probe_after_access_returns(ai):
    selected = select_model(ai)
    ready_knowledge(ai)
    ai['admin'].post(ai['url'] + '/model/probe', json={'version': selected['version']}).raise_for_status()
    enabled = save(ai, selected['settings'] | {'enabled': True})
    ai['fake'].error = ProviderError('access_denied')
    denied = ai['admin'].post(ai['url'] + '/models/refresh', json={'version': enabled['version']})
    assert denied.status_code == 409
    current = ai['admin'].get(ai['url']).json()
    assert current['catalogue'] == [] and not any(current['capabilities'].values())
    assert not current['settings']['enabled'] and current['version'] > enabled['version']
    ai['fake'].error = None
    restored = ai['admin'].post(ai['url'] + '/models/refresh', json={'version': current['version']})
    assert restored.status_code == 200 and restored.json()['catalogue']
    response = ai['admin'].put(ai['url'], json={'version': current['version'], 'settings': current['settings'] | {'enabled': True}})
    assert response.status_code == 409
    ai['admin'].post(ai['url'] + '/model/probe', json={'version': current['version']}).raise_for_status()
    assert save(ai, current['settings'] | {'enabled': True})['settings']['enabled']


@pytest.mark.parametrize('error', [ProviderError('unexpected-raw-secret'), RuntimeError('unexpected-raw-secret')])
def test_upstream_errors_never_escape_as_raw_messages(ai, error):
    current = select_model(ai)
    ai['fake'].error = error
    response = ai['admin'].post(ai['url'] + '/models/refresh', json={'version': current['version']})
    assert response.status_code == 502 and 'unexpected-raw-secret' not in response.text
    assert response.json()['error']['code'] == 'ai_provider_unavailable'


def test_feedback_destinations_keep_custom_page_separate_from_board(ai):
    configured = default_ai_settings() | {'feature_board_id': ai['b']['id'], 'bug_board_id': ai['b']['id']}
    save(ai, configured)
    response = ai['admin'].get(ai['url'] + '/feedback-destinations')
    assert response.json() == {'feature': ai['cfg'].base_url + '/boards/' + ai['b']['id'], 'bug': ai['cfg'].base_url + '/boards/' + ai['b']['id']}
    configured |= {'feedback_url': 'https://customer.example/feedback?view=public#updates', 'bug_feedback_url': 'https://customer.example/bugs'}
    changed = save(ai, configured)
    assert changed['settings']['feature_board_id'] == ai['b']['id']
    assert ai['admin'].get(ai['url'] + '/feedback-destinations').json() == {'feature': configured['feedback_url'], 'bug': configured['bug_feedback_url']}
    assert resolve_feedback_destination(configured, 'feature', ai['cfg'].base_url) == configured['feedback_url']
    assert ai['fake'].calls == []
    response = ai['admin'].put(ai['url'], json={'version': changed['version'], 'settings': configured | {'bug_board_id': ai['qb']['id']}})
    assert response.status_code == 404


def test_stale_settings_update_does_not_change_key_or_permissions(ai):
    current = save(ai, key='fixture-key')
    response = ai['admin'].put(ai['url'], json={'version': 0, 'settings': default_ai_settings() | {'allow_guest_reports': True}, 'key': None})
    assert response.status_code == 409
    assert ai['admin'].get(ai['url']).json() == current


def test_provider_checks_are_bounded_and_cancellation_releases_slot(ai):
    service = ai['app'].state.ai_settings
    projects = []
    for index in range(5):
        project = ai['storage'].create_project({'identifier': f'probe-{index}', 'name': 'Probe', 'description': ''}, ai['principal']['user_id'])
        saved = service.save(project['id'], ai['principal'], 0, default_ai_settings(), key=f'fixture-key-{index}')
        pool=service.keys(project['id'],ai['principal']);key=pool['keys'][0]
        changed=service.update_key(project['id'],ai['principal'],pool['version'],key['id'],key['version'],label=key['label'],quota_group=f'Fixture account {index}',enabled=True)
        projects.append((project['id'], changed['version']))

    async def run():
        started, release = asyncio.Event(), asyncio.Event()
        count = 0
        class WaitingProvider:
            async def list_models(self, key):
                nonlocal count
                count += 1
                if count == 4:
                    started.set()
                await release.wait()
                return ('Exact/Model:ID',)
        service.provider_factory = lambda _: WaitingProvider()
        tasks = [asyncio.create_task(service.refresh_models(pid, ai['principal'], version)) for pid, version in projects[:4]]
        await asyncio.wait_for(started.wait(), 2)
        for pid, version in (projects[0], projects[4]):
            with pytest.raises(Error) as exc:
                await service.refresh_models(pid, ai['principal'], version)
            assert exc.value.code == 'ai_operation_running'
        tasks[0].cancel()
        with pytest.raises(asyncio.CancelledError):
            await tasks[0]
        release.set()
        replacement = await service.refresh_models(projects[4][0], ai['principal'], projects[4][1])
        assert replacement['catalogue'] == ['Exact/Model:ID']
        await asyncio.gather(*tasks[1:])
        assert service._operations == set()
    asyncio.run(run())


def paired_providers(ai):
    providers = {identity: FakeProvider() for identity in SHIPPED_PROVIDERS}
    # Deliberately share an exact ID: provider identity, rather than a different
    # model spelling, must be what invalidates old credentials and evidence.
    for adapter in providers.values():
        adapter.models = ('shared-fixture-model',)
    ai['app'].state.ai_settings.provider_factory = lambda identity: providers[identity]
    return providers


def verified_provider(ai, identity):
    current = save(ai, default_ai_settings() | {'provider': identity}, key=identity + '-fixture-secret')
    response = ai['admin'].post(ai['url'] + '/models/refresh', json={'version': current['version']})
    assert response.status_code == 200, response.text
    current = save(ai, current['settings'] | {'model': 'shared-fixture-model'})
    response = ai['admin'].post(ai['url'] + '/model/probe', json={'version': current['version'], 'tools': True})
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.parametrize(('original', 'replacement'), PROVIDER_SWITCHES)
@pytest.mark.parametrize('key_mode', ['omitted', 'removed', 'replacement'])
def test_provider_switch_never_reuses_saved_key_model_or_evidence(ai, original, replacement, key_mode):
    providers = paired_providers(ai)
    current = verified_provider(ai, original)
    extra = {'key': None} if key_mode == 'removed' else {'key': replacement + '-new-fixture-secret'} if key_mode == 'replacement' else {}
    switched = save(ai, current['settings'] | {'provider': replacement}, **extra)
    assert switched['settings']['provider'] == replacement and switched['settings']['model'] is None
    assert switched['version'] == current['version'] + 1
    assert switched['credential_version'] == current['credential_version'] + 1
    assert switched['has_key'] is (key_mode == 'replacement')
    assert switched['catalogue'] == [] and switched['capabilities'] == {}
    assert not switched['settings']['enabled'] and not any(switched['settings']['tools'].values())
    assert providers[replacement].calls == []
    snapshot = ai['storage'].ai_provider_snapshot(ai['p']['id'], ai['principal'], switched['version'])
    if key_mode == 'replacement':
        assert Vault(ai['cfg'].secret).reveal(snapshot['protected_key']) == replacement + '-new-fixture-secret'
    else:
        assert snapshot['protected_key'] is None
        denied = ai['admin'].post(ai['url'] + '/models/refresh', json={'version': switched['version']})
        assert denied.status_code == 409 and denied.json()['error']['code'] == 'ai_key_required'
        assert providers[replacement].calls == []
        switched = save(ai, key=replacement + '-new-fixture-secret')
    refreshed = ai['admin'].post(ai['url'] + '/models/refresh', json={'version': switched['version']})
    assert refreshed.status_code == 200, refreshed.text
    assert refreshed.json()['capabilities'] == {} and refreshed.json()['settings']['model'] is None
    current = save(ai, refreshed.json()['settings'] | {'model': 'shared-fixture-model'})
    probed = ai['admin'].post(ai['url'] + '/model/probe', json={'version': current['version']})
    assert probed.status_code == 200, probed.text
    assert not probed.json()['settings']['enabled']
    assert all(call[1] == replacement + '-new-fixture-secret' for call in providers[replacement].calls)
    assert original + '-fixture-secret' not in str(probed.json())


@pytest.mark.parametrize('operation', ['models/refresh', 'model/probe'])
@pytest.mark.parametrize('round_trip', [False, True])
@pytest.mark.parametrize(('original', 'replacement'), PROVIDER_SWITCHES)
def test_provider_switch_fences_late_catalogue_and_probe_even_after_switch_back(ai, operation, round_trip, original, replacement):
    providers = paired_providers(ai)
    current = verified_provider(ai, original)
    def switch():
        changed = save(ai, current['settings'] | {'provider': replacement}, key=replacement + '-new-fixture-secret')
        if round_trip:
            changed = save(ai, changed['settings'] | {'provider': original}, key=original + '-replacement-secret')
            save(ai, changed['settings'] | {'model': 'shared-fixture-model'})
    providers[original].hook = switch
    late = ai['admin'].post(ai['url'] + '/' + operation, json={'version': current['version']})
    assert late.status_code == 409
    stored = ai['admin'].get(ai['url']).json()
    assert stored['credential_version'] == current['credential_version'] + (2 if round_trip else 1)
    assert stored['catalogue'] == [] and stored['capabilities'] == {}
    assert not stored['settings']['enabled'] and providers[replacement].calls == []


@pytest.mark.parametrize(('original', 'replacement'), PROVIDER_SWITCHES)
def test_enabled_or_stale_provider_switch_rolls_back_secret_and_model(ai, original, replacement):
    paired_providers(ai)
    current = verified_provider(ai, original)
    ready_knowledge(ai)
    current = save(ai, current['settings'] | {'enabled': True})
    private = ai['storage'].ai_provider_snapshot(ai['p']['id'], ai['principal'], current['version'])
    for version, enabled in ((current['version'], True), (current['version'] - 1, False)):
        rejected = ai['admin'].put(ai['url'], json={'version': version,
            'settings': current['settings'] | {'provider': replacement, 'enabled': enabled}, 'key': replacement + '-new-fixture-secret'})
        assert rejected.status_code == 409
        assert ai['admin'].get(ai['url']).json() == current
        assert ai['storage'].ai_provider_snapshot(ai['p']['id'], ai['principal'], current['version']) == private


@pytest.mark.parametrize(('original', 'replacement'), PROVIDER_SWITCHES)
def test_each_provider_requires_its_own_catalogue_and_exact_probe_before_enabling(ai, original, replacement):
    paired_providers(ai)
    old = verified_provider(ai, original)
    ready_knowledge(ai)
    current = save(ai, old['settings'] | {'provider': replacement}, key=replacement + '-new-fixture-secret')
    for stage in ('unselected', 'selected', 'catalogue'):
        if stage == 'selected':
            current = save(ai, current['settings'] | {'model': 'shared-fixture-model'})
        elif stage == 'catalogue':
            response = ai['admin'].post(ai['url'] + '/models/refresh', json={'version': current['version']})
            assert response.status_code == 200, response.text
            current = response.json()
        rejected = ai['admin'].put(ai['url'], json={'version': current['version'], 'settings': current['settings'] | {'enabled': True}})
        assert rejected.status_code == 409
    response = ai['admin'].post(ai['url'] + '/model/probe', json={'version': current['version']})
    assert response.status_code == 200 and not response.json()['settings']['enabled']
    assert save(ai, response.json()['settings'] | {'enabled': True})['settings']['enabled']
