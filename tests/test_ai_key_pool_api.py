"""Real SQLite/native-admin pool contracts; injected provider traffic only."""
from copy import deepcopy
import time

import pytest

from feedback.ai_providers import ProviderError
from test_ai_settings_api import ai, save, select_model, ready_knowledge


def pool(ai):
    response = ai['admin'].get(ai['url'] + '/keys')
    assert response.status_code == 200, response.text
    return response.json()


def add(ai, key='second-fixture-key', label='Second key', group='Account two'):
    current = pool(ai)
    response = ai['admin'].post(ai['url'] + '/keys', json={
        'version': current['version'], 'label': label, 'quota_group': group, 'key': key})
    assert response.status_code == 200, response.text
    return response.json()


def check(ai, key_id, action='models/refresh', **extra):
    current = pool(ai)
    row = next(k for k in current['keys'] if k['id'] == key_id)
    return ai['admin'].post(ai['url'] + '/keys/' + key_id + '/' + action,
        json={'version': current['version'], 'key_version': row['version'], **extra})


@pytest.mark.parametrize('client', ['anon', 'user', 'trusted', 'manager', 'other'])
@pytest.mark.parametrize('operation', ['read', 'add', 'update', 'delete', 'retry', 'refresh', 'probe'])
def test_pool_authority_is_native_admin_only(ai, client, operation):
    select_model(ai)
    before = pool(ai)
    row = before['keys'][0]
    path = ai['url'] + '/keys'
    body = {'version': before['version'], 'key_version': row['version']}
    client = ai[client]
    if operation == 'read': response = client.get(path)
    elif operation == 'add': response = client.post(path, json={
        'version': before['version'], 'label': 'Untrusted', 'quota_group': 'Default account', 'key': 'must-not-save'})
    elif operation == 'update': response = client.put(path + '/' + row['id'], json=body | {
        'label': 'Untrusted', 'quota_group': 'Default account', 'enabled': False})
    elif operation == 'delete': response = client.request('DELETE', path + '/' + row['id'], json=body)
    else:
        suffix = {'retry': 'retry', 'refresh': 'models/refresh', 'probe': 'model/probe'}[operation]
        response = client.post(path + '/' + row['id'] + '/' + suffix, json=body)
    assert response.status_code in (401, 403)
    assert pool(ai) == before
    assert len(ai['fake'].calls) == 1


def test_duplicate_plaintext_cannot_be_added_under_another_quota_group(ai):
    select_model(ai)
    before = pool(ai)
    response = ai['admin'].post(ai['url'] + '/keys', json={
        'version': before['version'], 'label': 'Duplicate', 'quota_group': 'Different label',
        'key': 'fixture-provider-secret'})
    assert response.status_code == 409 and response.json()['error']['code'] == 'ai_duplicate_key'
    assert 'fixture-provider-secret' not in response.text
    assert pool(ai) == before
    added = add(ai)
    response = ai['admin'].put(ai['url'], json={'version': added['version'],
        'settings': ai['admin'].get(ai['url']).json()['settings'], 'key': 'second-fixture-key'})
    assert response.status_code == 409 and pool(ai) == added


@pytest.mark.parametrize('change', ['null', 'whitespace', 'control', 'extra', 'version_bool'])
def test_pool_write_validation_never_echoes_secret(ai, change):
    select_model(ai)
    current = pool(ai)
    row = current['keys'][0]
    body = {'version': current['version'], 'key_version': row['version'], 'label': 'Primary',
            'quota_group': 'Default account', 'enabled': True, 'key': 'sensitive-fixture-value'}
    if change == 'null': body['key'] = None
    elif change == 'whitespace': body['key'] += ' invalid'
    elif change == 'control': body['key'] += '\n'
    elif change == 'extra': body['endpoint'] = 'https://untrusted.example'
    else: body['version'] = True
    response = ai['admin'].put(ai['url'] + '/keys/' + row['id'], json=body)
    assert response.status_code == 400
    assert 'sensitive-fixture-value' not in response.text
    assert pool(ai) == current


def test_per_key_checks_never_borrow_default_key_or_catalogue(ai):
    selected = select_model(ai)
    current = add(ai)
    first, second = current['keys']
    assert first['is_default'] and not second['model_available']
    response = check(ai, second['id'], 'model/probe', tools=True)
    assert response.status_code == 409 and len(ai['fake'].calls) == 1
    assert check(ai, second['id']).status_code == 200
    assert check(ai, second['id'], 'model/probe', tools=True).status_code == 200
    assert ai['fake'].calls[-2:] == [('catalogue', 'second-fixture-key', None, False),
        ('probe', 'second-fixture-key', selected['settings']['model'], True)]
    current = pool(ai)
    rows = {r['id']: r for r in current['keys']}
    assert rows[second['id']]['eligible'] and not rows[first['id']]['eligible']
    assert current['verified_key_count'] == 1
    encoded = str(current)
    assert 'second-fixture-key' not in encoded and 'fixture-provider-secret' not in encoded
    assert all('protected_key' not in r and 'catalogue' not in r for r in current['keys'])


def test_pool_mutations_fence_late_per_key_probe_results(ai):
    select_model(ai)
    current = add(ai)
    second = current['keys'][1]
    assert check(ai, second['id']).status_code == 200
    def mutate():
        current = pool(ai)
        target = next(k for k in current['keys'] if k['id'] == second['id'])
        result = ai['admin'].put(ai['url'] + '/keys/' + second['id'], json={
            'version': current['version'], 'key_version': target['version'],
            'label': 'Changed during check', 'quota_group': target['quota_group'], 'enabled': False})
        assert result.status_code == 200
    ai['fake'].hook = mutate
    response = check(ai, second['id'], 'model/probe', tools=True)
    assert response.status_code == 409
    target = next(k for k in pool(ai)['keys'] if k['id'] == second['id'])
    assert not target['enabled'] and not target['capabilities'].get('text_generation')


def test_admin_rate_limit_preserves_verified_model_and_recovery_is_explicit(ai):
    selected = select_model(ai)
    ready_knowledge(ai)
    ai['admin'].post(ai['url'] + '/model/probe', json={'version': selected['version'], 'tools': True}).raise_for_status()
    enabled = save(ai, selected['settings'] | {'enabled': True})
    key = pool(ai)['keys'][0]
    ai['fake'].error = ProviderError('rate_limited', status=429, retryable=True, retry_after=120)
    failed = check(ai, key['id'], 'model/probe', tools=True)
    assert failed.status_code == 429
    current = pool(ai)
    assert current['keys'][0]['status'] == 'cooling_down'
    assert current['keys'][0]['retry_at'] > time.time() + 100
    assert current['keys'][0]['capabilities']['text_generation']
    assert ai['admin'].get(ai['url']).json()['settings']['enabled']
    calls = len(ai['fake'].calls)
    assert check(ai, key['id'], 'retry').status_code == 200
    assert len(ai['fake'].calls) == calls
    assert pool(ai)['keys'][0]['status'] in ('recovering', 'ready')


@pytest.mark.parametrize('error', [ProviderError('invalid_credentials', status=401),
                                  ProviderError('payment_required', status=402)])
def test_catalogue_and_manual_retry_cannot_repair_permanent_key_block(ai, error):
    select_model(ai)
    key = pool(ai)['keys'][0]
    ai['fake'].error = error
    assert check(ai, key['id'], 'model/probe').status_code == 409
    assert pool(ai)['keys'][0]['status'] == 'blocked'
    assert check(ai, key['id'], 'retry').status_code in (200, 409)
    assert pool(ai)['keys'][0]['status'] == 'blocked'
    ai['fake'].error = None
    assert check(ai, key['id']).status_code == 200
    assert pool(ai)['keys'][0]['status'] == 'blocked'
    assert check(ai, key['id'], 'model/probe').status_code == 200
    assert pool(ai)['keys'][0]['status'] == 'ready'


def test_cross_project_key_id_and_stale_versions_fail_before_provider_io(ai):
    select_model(ai)
    old = pool(ai)
    row = old['keys'][0]
    current = add(ai)
    before = len(ai['fake'].calls)
    stale = ai['admin'].post(ai['url'] + '/keys/' + row['id'] + '/model/probe', json={
        'version': old['version'], 'key_version': row['version']})
    assert stale.status_code == 409
    project = ai['admin'].post('/api/v1/projects', json={'name': 'Other pool project', 'identifier': 'pool-other'}).json()
    other_url = '/api/v1/projects/' + project['id'] + '/ai'
    other = ai['admin'].get(other_url).json()
    crossed = ai['admin'].post(other_url + '/keys/' + row['id'] + '/model/probe', json={
        'version': other['version'], 'key_version': row['version']})
    assert crossed.status_code == 404
    assert len(ai['fake'].calls) == before and pool(ai) == current


def test_replacement_key_loses_only_its_verification_and_never_returns_secret(ai):
    select_model(ai)
    current = add(ai)
    for row in current['keys']:
        assert check(ai, row['id']).status_code == 200
        assert check(ai, row['id'], 'model/probe', tools=True).status_code == 200
    current = pool(ai)
    first, second = current['keys']
    updated = ai['admin'].put(ai['url'] + '/keys/' + second['id'], json={
        'version': current['version'], 'key_version': second['version'], 'label': 'Replacement',
        'quota_group': second['quota_group'], 'enabled': True, 'key': 'replaced-secret-value'})
    assert updated.status_code == 200, updated.text
    assert 'replaced-secret-value' not in updated.text and 'second-fixture-key' not in updated.text
    rows = {r['id']: r for r in updated.json()['keys']}
    assert rows[first['id']]['eligible'] and not rows[second['id']]['eligible']
    assert rows[second['id']]['catalogue_count'] == 0
