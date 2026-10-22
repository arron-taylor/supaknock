"""Durable provider selection, account health and migration on real SQLite."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
import json
import shutil
import time
from uuid import uuid4

import pytest
from cryptography.fernet import Fernet

from feedback.ai_domain import default_ai_settings
from feedback.errors import Error
from feedback.security import Vault
from feedback.storage.sqlite import SQLiteStorage
from conftest import reopen_storage
import feedback.storage.sqlite as adapter
from test_ai_job_races import ready_ai_jobs, enqueue, independent_stores, concurrent, job_state
from test_ai_storage import ai_store, CAPS, ready_runtime, publish_source
from test_ai_migration import migration_directory


def add(env, label='Second', group='Second account', secret='protected-second'):
    s, p, pid = env['store'], env['principal'], env['project']
    current = s.ai_provider_keys(pid, p)
    before = {k['id'] for k in current['keys']}
    result = s.ai_provider_key_add(pid, p, current['version'], label, group, secret)
    return next(k for k in result['keys'] if k['id'] not in before)


def evidence(env, kid=None, catalogue=None, caps=None):
    s, p, pid = env['store'], env['principal'], env['project']
    current = s.ai_settings(pid, p)
    snapshot = s.ai_provider_snapshot(pid, p, current['version'], key_id=kid)
    return s.ai_store_provider_evidence(pid, p, snapshot['version'], snapshot['credential_version'], snapshot['settings']['model'],
        key_id=snapshot['key_id'], key_version=snapshot['key_version'], health_epoch=snapshot['health_epoch'], group_epoch=snapshot['group_epoch'],
        catalogue=catalogue if catalogue is not None else [snapshot['settings']['model']], capabilities=CAPS if caps is None else caps)


def check_outcome(env, code, kid=None, **kwargs):
    s, p, pid = env['store'], env['principal'], env['project']
    snap = s.ai_provider_snapshot(pid, p, s.ai_settings(pid, p)['version'], key_id=kid)
    return s.ai_provider_check_outcome(pid, p, snap['version'], snap['key_id'], snap['key_version'], snap['health_epoch'], snap['group_epoch'], code, **kwargs)


def claim(env, stamp=None, lease=60):
    stamp = time.time() if stamp is None else stamp
    job = enqueue(env)
    result = env['store'].ai_claim_job(stamp, lease_seconds=lease, kinds=('preview',))
    assert result['id'] == job['id']
    return result, env['store'].ai_provider_claim(result['id'], result['lease_token'], stamp)


def dispatch(s, job, stamp, index=0):
    assert s.ai_reserve_usage(job['id'], job['lease_token'], 100, stamp, call_index=index)
    assert s.ai_job_before_dispatch(job['id'], job['lease_token'], stamp, call_index=index)


def test_round_robin_is_durable_and_tool_rounds_pin_attempt(ready_ai_jobs):
    env, stamp = ready_ai_jobs, time.time() + 1
    s = env['store']
    second = add(env)
    evidence(env, second['id'])
    first_job, first = claim(env, stamp)
    dispatch(s, first_job, stamp)
    assert s.ai_provider_outcome(first_job['id'], first_job['lease_token'], 0, 'success', current_time=stamp)['accepted']
    reopened = reopen_storage(s)
    assert reopened.ai_provider_claim(first_job['id'], first_job['lease_token'], stamp)['key_id'] == first['key_id']
    assert reopened.ai_finish_job(first_job['id'], first_job['lease_token'], 'succeeded', current_time=stamp)
    next_job, next_key = claim(env, stamp)
    assert next_key['key_id'] != first['key_id']
    assert reopened.ai_finish_job(next_job['id'], next_job['lease_token'], 'succeeded', current_time=stamp)
    third_job, third = claim(env, stamp)
    assert third['key_id'] == first['key_id']


def test_concurrent_claims_never_share_a_key_lease(ready_ai_jobs):
    env, stamp = ready_ai_jobs, time.time() + 1
    second = add(env)
    evidence(env, second['id'])
    s = env['store']
    for _ in range(8): enqueue(env)
    jobs = [s.ai_claim_job(stamp, kinds=('preview',)) for _ in range(8)]
    results = concurrent(independent_stores(env, 8), lambda handle, i: handle.ai_provider_claim(jobs[i]['id'], jobs[i]['lease_token'], stamp))
    winners = [r for r in results if r['available']]
    assert len(winners) == len({r['key_id'] for r in winners}) == 2
    assert all(r['reason'] == 'busy' for r in results if not r['available'])


@pytest.mark.parametrize('code', ['rate_limited', 'quota_exhausted', 'transient'])
def test_group_cooldown_persists_and_new_keys_inherit_it(ready_ai_jobs, tmp_path, code):
    env, stamp = ready_ai_jobs, time.time() + 1
    s, p, pid = env['store'], env['principal'], env['project']
    job, selected = claim(env, stamp)
    dispatch(s, job, stamp)
    outcome = s.ai_provider_outcome(job['id'], job['lease_token'], 0, code, current_time=stamp)
    assert outcome['accepted'] and outcome['retry_at'] >= stamp + (86400 if code == 'quota_exhausted' else 60)
    after = s.ai_provider_keys(pid, p)
    assert after['keys'][0]['eligible'] and after['keys'][0]['status'] == 'cooling_down'
    assert s.ai_settings(pid, p)['settings']['enabled']
    later = add(env, group='Default account')
    evidence(env, later['id'])
    assert s.ai_provider_keys(pid, p)['available_key_count'] == 0
    target = tmp_path / 'backup.sqlite'
    s.backup(target)
    restored = SQLiteStorage(target, s.base_url)
    assert restored.ai_provider_keys(pid, p) == s.ai_provider_keys(pid, p)
    assert restored.ai_provider_keys(pid, p)['groups'][0]['retry_at'] == outcome['retry_at']


def test_quota_groups_are_installation_wide_and_exact_provider_labels(ai_store):
    s, p, pid, other = ai_store
    envs = [dict(store=s, principal=p, project=project) for project in (pid, other)]
    for env in envs:
        s.ai_configure(env['project'], p, 0, default_ai_settings() | {'model': 'same'}, protected_key='secret-' + env['project'])
        evidence(env)
    check_outcome(envs[0], 'rate_limited')
    assert s.ai_provider_keys(other, p)['keys'][0]['status'] == 'cooling_down'
    distinct = add(envs[1], group='Default account ')
    evidence(envs[1], distinct['id'])
    assert next(k for k in s.ai_provider_keys(other, p)['keys'] if k['id'] == distinct['id'])['status'] == 'ready'
    settings = s.ai_settings(other, p)
    s.ai_configure(other, p, settings['version'], settings['settings'] | {'provider': 'gemini'}, protected_key='new-provider')
    current = s.ai_settings(other, p)
    s.ai_configure(other, p, current['version'], current['settings'] | {'model': 'same'})
    evidence(envs[1])
    assert s.ai_provider_keys(other, p)['keys'][0]['status'] == 'ready'


def test_only_one_recovery_trial_and_expired_lease_can_recover(ready_ai_jobs):
    env, stamp = ready_ai_jobs, time.time() + 1
    s, p, pid = env['store'], env['principal'], env['project']
    second = add(env, group='Default account')
    evidence(env, second['id'])
    check_outcome(env, 'rate_limited')
    pool = s.ai_provider_keys(pid, p)
    recovery = pool['keys'][0]['retry_at'] + 1
    for _ in range(4): enqueue(env)
    jobs = [s.ai_claim_job(recovery, lease_seconds=5, kinds=('preview',)) for _ in range(4)]
    results = concurrent(independent_stores(env, 4), lambda handle, i: handle.ai_provider_claim(jobs[i]['id'], jobs[i]['lease_token'], recovery))
    assert sum(r['available'] for r in results) == 1
    first_index = next(i for i, r in enumerate(results) if r['available'])
    old = jobs[first_index]
    assert s.ai_renew_job(old['id'], old['lease_token'], recovery + 1, lease_seconds=10)
    with s.transaction() as c:
        assert c.execute('SELECT trial_until FROM ai_provider_quota_groups WHERE id=?', (results[first_index]['quota_group_id'],)).fetchone()[0] == recovery + 11
    replacement = s.ai_claim_job(recovery + 12, kinds=('preview',))
    selected = s.ai_provider_claim(replacement['id'], replacement['lease_token'], recovery + 12)
    assert selected['available']
    assert not s.ai_job_before_dispatch(old['id'], old['lease_token'], recovery + 12)
    dispatch(s, replacement, recovery + 12)
    assert s.ai_provider_outcome(replacement['id'], replacement['lease_token'], 0, 'success', current_time=recovery + 12)['accepted']
    assert s.ai_provider_keys(pid, p)['groups'][0]['status'] == 'ready'


def test_late_success_never_clears_newer_group_cooldown(ready_ai_jobs):
    env, stamp = ready_ai_jobs, time.time() + 1
    s, p, pid = env['store'], env['principal'], env['project']
    second = add(env, group='Default account')
    evidence(env, second['id'])
    old, old_key = claim(env, stamp)
    newer, newer_key = claim(env, stamp)
    assert old_key['key_id'] != newer_key['key_id']
    dispatch(s, old, stamp)
    dispatch(s, newer, stamp)
    failed = s.ai_provider_outcome(newer['id'], newer['lease_token'], 0, 'rate_limited', current_time=stamp)
    result = s.ai_provider_outcome(old['id'], old['lease_token'], 0, 'success', current_time=stamp)
    assert result['accepted']
    assert s.ai_provider_keys(pid, p)['groups'][0]['retry_at'] == failed['retry_at']
    assert s.ai_finish_job(old['id'], old['lease_token'], 'succeeded', current_time=stamp)
    assert not s.ai_provider_outcome(old['id'], old['lease_token'], 0, 'success', current_time=stamp)['accepted']


@pytest.mark.parametrize('code', ['invalid_credentials', 'payment_required', 'access_denied', 'model_or_endpoint_unavailable'])
def test_failed_key_isolated_and_next_attempt_rotates_without_refund(ready_ai_jobs, code):
    env, stamp = ready_ai_jobs, time.time() + 1
    s, p, pid = env['store'], env['principal'], env['project']
    second = add(env)
    evidence(env, second['id'])
    job, key = claim(env, stamp)
    dispatch(s, job, stamp)
    outcome = s.ai_provider_outcome(job['id'], job['lease_token'], 0, code, current_time=stamp)
    assert outcome == dict(accepted=True, retryable=True, retry_at=stamp)
    assert s.ai_settings(pid, p)['settings']['enabled']
    assert s.ai_job_material(job['id'], job['lease_token'], stamp) is None
    assert s.ai_provider_defer(job['id'], job['lease_token'], stamp + 1, stamp)
    replacement = s.ai_claim_job(stamp + 1, kinds=('preview',))
    next_key = s.ai_provider_claim(job['id'], replacement['lease_token'], stamp + 1)
    assert next_key['key_id'] != key['key_id'] and replacement['attempt_count'] == 2
    dispatch(s, replacement, stamp + 1)
    assert s.ai_usage(pid, p)['reserved_requests'] == 2
    assert not s.ai_provider_outcome(job['id'], job['lease_token'], 0, 'success', current_time=stamp + 1)['accepted']


def test_unpaid_wait_preserves_attempt_allowance_but_has_absolute_deadline(ready_ai_jobs):
    env, stamp = ready_ai_jobs, time.time() + 1
    s = env['store']
    held, _ = claim(env, stamp)
    waiting, unavailable = claim(env, stamp)
    assert not unavailable['available']
    assert s.ai_provider_defer(waiting['id'], waiting['lease_token'], stamp + 2, stamp)
    assert job_state(s, waiting['id'])['attempt_count'] == 0
    retry = s.ai_claim_job(stamp + 2, kinds=('preview',))
    assert retry['id'] == waiting['id'] and retry['attempt_count'] == 1
    assert s.ai_provider_defer(retry['id'], retry['lease_token'], stamp + 86400, stamp + 2)
    assert job_state(s, retry['id'])['state'] == 'failed'
    assert s.ai_usage(env['project'], env['principal'])['reserved_requests'] == 0


def test_missing_tool_capability_cannot_be_assembled_across_keys(ai_store):
    s, p, pid, _ = ai_store
    env = dict(store=s, principal=p, project=pid)
    s.ai_configure(pid, p, 0, default_ai_settings() | {'model': 'same'}, protected_key='one')
    evidence(env)
    second = add(env)
    evidence(env, second['id'], caps=CAPS | {'text_generation': False, 'tool_calling': True})
    ready_runtime(s, p)
    publish_source(s, p, pid)
    cfg = s.ai_settings(pid, p)
    settings = deepcopy(cfg['settings'])
    settings['tools']['search_knowledge'] = True
    with pytest.raises(Error) as caught:
        s.ai_configure(pid, p, cfg['version'], settings | {'enabled': True})
    assert caught.value.code == 'ai_not_ready'


def test_catalogue_union_overflow_rolls_back_exact_key_evidence(ai_store):
    s, p, pid, _ = ai_store
    env = dict(store=s, principal=p, project=pid)
    s.ai_configure(pid, p, 0, default_ai_settings() | {'model': 'm0'}, protected_key='one')
    evidence(env, catalogue=['m' + str(i) for i in range(1000)])
    second = add(env)
    before = s.ai_provider_keys(pid, p)
    with pytest.raises(Error) as caught:
        evidence(env, second['id'], catalogue=['m0', 'overflow'])
    assert caught.value.code == 'ai_catalogue_limit'
    assert s.ai_provider_keys(pid, p) == before


def test_99_key_cap_is_atomic_with_concurrent_additions(ai_store):
    s, p, pid, _ = ai_store
    env = dict(store=s, principal=p, project=pid)
    s.ai_configure(pid, p, 0, default_ai_settings(), protected_key='one')
    for i in range(97): add(env, label='Key ' + str(i), secret=str(i))
    version = s.ai_provider_keys(pid, p)['version']
    def operation(handle, index):
        try:
            return handle.ai_provider_key_add(pid, p, version, 'Final', 'Default account', str(index))
        except Error as error:
            return error.code
    results = concurrent(independent_stores(env, 4), operation)
    assert sum(isinstance(r, dict) for r in results) == 1
    current = s.ai_provider_keys(pid, p)
    assert len(current['keys']) == 99
    with pytest.raises(Error, match='at most 99'):
        s.ai_provider_key_add(pid, p, current['version'], 'Extra', 'Default account', 'extra')


def test_pre014_migration_preserves_encryption_evidence_and_job_authority(tmp_path, monkeypatch):
    migrations = tmp_path / 'old' / 'migrations'
    migrations.mkdir(parents=True)
    for file in Path(adapter.__file__).with_name('migrations').glob('*.sql'):
        if file.name < '014': shutil.copyfile(file, migrations / file.name)
    path, base = tmp_path / 'old.sqlite', 'https://feedback.example'
    vault = Vault(Fernet.generate_key().decode())
    with migration_directory(monkeypatch, migrations):
        s = SQLiteStorage(path, base)
    user = s.create_native_account('admin@example.com', 'hash', 'Admin', 'admin', True)
    s.create_session(user['id'], 'session', 'csrf', time.time() + 3600)
    p = dict(kind='admin', user_id=user['id'], session_id=s.session('session')['session_id'])
    pid = s.create_project(dict(identifier='migration', name='Migration', description=''), user['id'])['id']
    protected = vault.protect('migration-fixture-secret')
    settings = default_ai_settings() | {'model': 'preserved'}
    with s.transaction(True) as c:
        c.execute('''INSERT INTO ai_settings(project_id,settings,version,credential_version,protected_key,catalogue,capabilities,capabilities_model,capabilities_credential_version,updated_at)
            VALUES(?,?,7,3,?,?,?,?,3,?)''', (pid, json.dumps(settings), protected, '["preserved"]', json.dumps(CAPS), 'preserved', '2026-10-03T00:00:00Z'))
        c.execute('''INSERT INTO ai_jobs(id,project_id,kind,resource_id,dedupe_key,config_version,authority,state,attempt_count,next_attempt,created_at,updated_at)
            VALUES(?,?,'preview',?,'old',7,?,'queued',0,0,?,?)''', ('preserved-job', pid, pid, json.dumps({'principal': p}), '2026-10-03T00:00:00Z', '2026-10-03T00:00:00Z'))
    with ThreadPoolExecutor(max_workers=4) as pool:
        stores = list(pool.map(lambda _: SQLiteStorage(path, base), range(4)))
    for handle in stores:
        cfg = handle.ai_settings(pid, p)
        assert cfg['version'] == 7 and cfg['credential_version'] == 3 and cfg['capabilities'] == CAPS
        snapshot = handle.ai_provider_snapshot(pid, p, 7)
        assert vault.reveal(snapshot['protected_key']) == 'migration-fixture-secret'
        assert snapshot['key_version'] == 1 and snapshot['catalogue'] == ['preserved']
        assert handle.ai_jobs(pid, p)[0]['state'] == 'queued'
    env = dict(store=stores[0], principal=p, project=pid)
    add(env, secret=vault.protect('second-fixture-secret'))
    backup = tmp_path / 'backup.sqlite'
    stores[0].backup(backup)
    restored = SQLiteStorage(backup, base)
    assert set(vault.reveal(value) for value in restored.signing_secrets()) == {'migration-fixture-secret', 'second-fixture-secret'}


def test_late_invalid_key_is_blocked_even_after_newer_account_cooldown(ready_ai_jobs):
    env, stamp = ready_ai_jobs, time.time() + 1
    s, p, pid = env['store'], env['principal'], env['project']
    second = add(env, group='Default account')
    evidence(env, second['id'])
    old, old_key = claim(env, stamp)
    newer, _ = claim(env, stamp)
    dispatch(s, old, stamp)
    dispatch(s, newer, stamp)
    failed = s.ai_provider_outcome(newer['id'], newer['lease_token'], 0, 'rate_limited', current_time=stamp)
    assert s.ai_provider_outcome(old['id'], old['lease_token'], 0, 'invalid_credentials', current_time=stamp)['accepted']
    pool = s.ai_provider_keys(pid, p)
    assert next(k for k in pool['keys'] if k['id'] == old_key['key_id'])['status'] == 'blocked'
    assert pool['groups'][0]['retry_at'] == failed['retry_at']


def test_cross_project_group_failure_fences_admin_evidence_and_pending_dispatch(ai_store):
    s, p, pid, other = ai_store
    envs = [dict(store=s, principal=p, project=project) for project in (pid, other)]
    for env in envs:
        s.ai_configure(env['project'], p, 0, default_ai_settings() | {'model': 'same'}, protected_key='secret-' + env['project'])
        evidence(env)
    ready_runtime(s, p)
    publish_source(s, p, pid)
    snapshot = s.ai_provider_snapshot(pid, p, s.ai_settings(pid, p)['version'])
    stamp = time.time() + 1
    job, selected = claim(envs[0], stamp)
    assert s.ai_reserve_usage(job['id'], job['lease_token'], 100, stamp)
    check_outcome(envs[1], 'rate_limited')
    assert not s.ai_job_before_dispatch(job['id'], job['lease_token'], stamp)
    assert not s.ai_provider_claim(job['id'], job['lease_token'], stamp)['available']
    with pytest.raises(Error) as caught:
        s.ai_store_provider_evidence(pid, p, snapshot['version'], snapshot['credential_version'], snapshot['settings']['model'],
            key_id=snapshot['key_id'], key_version=snapshot['key_version'], health_epoch=snapshot['health_epoch'], group_epoch=snapshot['group_epoch'], capabilities=CAPS)
    assert caught.value.status == 409
    assert s.ai_provider_keys(pid, p)['keys'][0]['eligible']


def test_paid_failover_remains_limited_to_three_attempts(ready_ai_jobs):
    env, stamp = ready_ai_jobs, time.time() + 1
    s, p, pid = env['store'], env['principal'], env['project']
    for i in range(3):
        key = add(env, label=str(i), group='Account' + str(i), secret='encrypted' + str(i))
        evidence(env, key['id'])
    job, _ = claim(env, stamp)
    for attempt in range(1, 4):
        assert job['attempt_count'] == attempt
        dispatch(s, job, stamp)
        result = s.ai_provider_outcome(job['id'], job['lease_token'], 0, 'invalid_credentials', current_time=stamp)
        assert result['retryable'] == (attempt < 3)
        assert s.ai_provider_defer(job['id'], job['lease_token'], stamp + 1, stamp)
        if attempt < 3:
            stamp += 1
            job = s.ai_claim_job(stamp, kinds=('preview',))
            assert s.ai_provider_claim(job['id'], job['lease_token'], stamp)['available']
    assert job_state(s, job['id'])['state'] == 'failed'
    assert s.ai_usage(pid, p)['reserved_requests'] == 3
    assert s.ai_provider_keys(pid, p)['available_key_count'] == 1
    assert s.ai_claim_job(stamp + 2, kinds=('preview',)) is None


def test_unchanged_releases_only_owned_trial_and_preserves_group_recovery(ready_ai_jobs):
    env, stamp = ready_ai_jobs, time.time() + 1
    s, p, pid = env['store'], env['principal'], env['project']
    check_outcome(env, 'rate_limited')
    pool = s.ai_provider_keys(pid, p)
    recovery = pool['keys'][0]['retry_at'] + 1
    job, selected = claim(env, recovery)
    dispatch(s, job, recovery)
    result = s.ai_provider_outcome(job['id'], job['lease_token'], 0, 'unchanged', current_time=recovery)
    assert result == dict(accepted=True, retryable=False, retry_at=None)
    with s.transaction() as c:
        group = c.execute('SELECT * FROM ai_provider_quota_groups WHERE id=?', (selected['quota_group_id'],)).fetchone()
        assert group['recovery_required'] and group['trial_job_id'] is None
        assert group['health_epoch'] == selected['group_epoch'] and group['failure_count'] == 1
    assert s.ai_finish_job(job['id'], job['lease_token'], 'failed', error='invalid_response', current_time=recovery)


def test_startup_checks_nondefault_pool_secrets_and_supported_backup(ai_store, tmp_path):
    from feedback.api import create_app
    from feedback.config import Config
    from feedback.maintenance import backup
    s, p, pid, _ = ai_store
    key = Fernet.generate_key().decode()
    vault = Vault(key)
    env = dict(store=s, principal=p, project=pid)
    s.ai_configure(pid, p, 0, default_ai_settings(), protected_key=vault.protect('primary-fixture-key'))
    secondary = add(env, secret=vault.protect('secondary-fixture-key'))
    cfg = Config(s.path, key, 's' * 32, base_url=s.base_url, worker=False)
    create_app(cfg)
    bundle = tmp_path / 'bundle'
    backup(cfg, bundle)
    restored_cfg = Config(bundle / 'feedback.sqlite', (bundle / 'application.key').read_text().strip(), 's' * 32, base_url=s.base_url, worker=False)
    restored = create_app(restored_cfg).state.storage
    assert len(restored.ai_provider_keys(pid, p)['keys']) == 2
    bad = Vault(Fernet.generate_key().decode()).protect('unreadable-secondary')
    with s.transaction(True) as c:
        c.execute('UPDATE ai_provider_keys SET protected_key=? WHERE id=?', (bad, secondary['id']))
    with pytest.raises(RuntimeError, match='installation secrets'):
        create_app(cfg)


def test_disappearing_model_needs_fresh_probe_after_catalogue_reappearance(ready_ai_jobs):
    env = ready_ai_jobs
    s, p, pid = env['store'], env['principal'], env['project']
    def catalogue(models):
        snapshot = s.ai_provider_snapshot(pid, p, s.ai_settings(pid, p)['version'])
        return s.ai_store_provider_evidence(pid, p, snapshot['version'], snapshot['credential_version'], snapshot['settings']['model'],
            key_id=snapshot['key_id'], key_version=snapshot['key_version'], health_epoch=snapshot['health_epoch'], group_epoch=snapshot['group_epoch'], catalogue=models)
    before = s.ai_provider_snapshot(pid, p, s.ai_settings(pid, p)['version'])
    catalogue([])
    assert not s.ai_settings(pid, p)['settings']['enabled']
    assert s.ai_provider_keys(pid, p)['keys'][0]['capabilities'] == {}
    catalogue([before['settings']['model']])
    current = s.ai_provider_keys(pid, p)
    assert not current['keys'][0]['eligible'] and current['keys'][0]['status'] == 'unverified'
    assert current['available_key_count'] == 0
    evidence(env)
    assert s.ai_provider_keys(pid, p)['keys'][0]['eligible']
    assert not s.ai_settings(pid, p)['settings']['enabled']


@pytest.mark.parametrize('later_code,retry_after', [('rate_limited', 3600), ('quota_exhausted', None)])
def test_concurrent_account_failures_preserve_the_longer_limit(ready_ai_jobs, later_code, retry_after):
    env, stamp = ready_ai_jobs, time.time() + 1
    s, p, pid = env['store'], env['principal'], env['project']
    second = add(env, group='Default account')
    evidence(env, second['id'])
    older, _ = claim(env, stamp)
    newer, _ = claim(env, stamp)
    dispatch(s, older, stamp)
    dispatch(s, newer, stamp)
    first = s.ai_provider_outcome(older['id'], older['lease_token'], 0, 'rate_limited', retry_after=60, current_time=stamp)
    later = s.ai_provider_outcome(newer['id'], newer['lease_token'], 0, later_code, retry_after=retry_after, current_time=stamp + 1)
    assert first['accepted'] and later['accepted']
    assert later['retry_at'] >= stamp + 1 + (retry_after or 86400)
    assert s.ai_provider_keys(pid, p)['groups'][0]['retry_at'] == later['retry_at']


def test_pre_reset_failure_cannot_extend_a_new_account_cooldown(ready_ai_jobs):
    env, stamp = ready_ai_jobs, time.time() + 1
    s, p, pid = env['store'], env['principal'], env['project']
    second = add(env, group='Default account')
    evidence(env, second['id'])
    other_pid = s.create_project(dict(identifier='resetter', name='Resetter', description=''), p['user_id'])['id']
    s.ai_configure(other_pid, p, 0, default_ai_settings() | {'model': 'test-model'}, protected_key='other-key')
    other = dict(store=s, principal=p, project=other_pid)
    evidence(other)
    delayed, _ = claim(env, stamp)
    initial, _ = claim(env, stamp)
    dispatch(s, delayed, stamp)
    dispatch(s, initial, stamp)
    assert s.ai_provider_outcome(initial['id'], initial['lease_token'], 0, 'rate_limited', current_time=stamp)['accepted']
    assert s.ai_finish_job(initial['id'], initial['lease_token'], 'failed', error='rate_limited', current_time=stamp)
    # An account-wide reset fences work in every bound project. A failure
    # from the revoked lease cannot extend the next cooldown cycle.
    before_reset = s.ai_settings(pid, p)['version']
    pool = s.ai_provider_keys(other_pid, p)
    key = pool['keys'][0]
    s.ai_provider_key_retry(other_pid, p, pool['version'], key['id'], key['version'])
    assert s.ai_settings(pid, p)['version'] > before_reset
    current, _ = claim(env, stamp + 1)
    dispatch(s, current, stamp + 1)
    new_cycle = s.ai_provider_outcome(current['id'], current['lease_token'], 0, 'rate_limited', current_time=stamp + 1)
    assert new_cycle['accepted']
    assert not s.ai_provider_outcome(delayed['id'], delayed['lease_token'], 0, 'quota_exhausted', current_time=stamp + 2)['accepted']
    assert s.ai_provider_keys(pid, p)['groups'][0]['retry_at'] == new_cycle['retry_at']


def test_pre_recovery_failure_cannot_restore_a_successfully_recovered_cooldown(ready_ai_jobs):
    env, stamp = ready_ai_jobs, time.time() + 1
    s, p, pid = env['store'], env['principal'], env['project']
    second = add(env, group='Default account')
    evidence(env, second['id'])
    delayed, _ = claim(env, stamp, lease=180)
    initial, _ = claim(env, stamp, lease=180)
    dispatch(s, delayed, stamp)
    dispatch(s, initial, stamp)
    failed = s.ai_provider_outcome(initial['id'], initial['lease_token'], 0, 'rate_limited', current_time=stamp)
    assert s.ai_finish_job(initial['id'], initial['lease_token'], 'failed', error='rate_limited', current_time=stamp)
    recovery_time = failed['retry_at'] + 1
    recovery, selected = claim(env, recovery_time)
    dispatch(s, recovery, recovery_time)
    assert s.ai_provider_outcome(recovery['id'], recovery['lease_token'], 0, 'success', current_time=recovery_time)['accepted']
    assert s.ai_provider_outcome(delayed['id'], delayed['lease_token'], 0, 'quota_exhausted', current_time=recovery_time + 1)['accepted']
    with s.transaction() as c:
        group = c.execute('SELECT * FROM ai_provider_quota_groups WHERE id=?', (selected['quota_group_id'],)).fetchone()
        assert group['reset_epoch'] > selected['group_epoch']
        assert group['cooldown_until'] == 0 and group['recovery_required'] == 0


@pytest.mark.parametrize('paid', [False, True])
@pytest.mark.parametrize('resume_age', [300, 600])
def test_restart_after_absolute_wait_deadline_never_reclaims_generation(ready_ai_jobs, paid, resume_age):
    from datetime import datetime
    env, stamp = ready_ai_jobs, time.time() + 1
    s = env['store']
    if paid:
        job, _ = claim(env, stamp)
        dispatch(s, job, stamp)
        result = s.ai_provider_outcome(job['id'], job['lease_token'], 0, 'rate_limited', current_time=stamp)
        assert s.ai_provider_defer(job['id'], job['lease_token'], result['retry_at'], stamp, error='rate_limited')
    else:
        held, _ = claim(env, stamp)
        job, blocked = claim(env, stamp)
        assert not blocked['available'] and blocked['reason'] == 'busy'
        assert s.ai_provider_defer(job['id'], job['lease_token'], blocked['retry_at'], stamp)
        assert s.ai_finish_job(held['id'], held['lease_token'], 'cancelled', current_time=stamp)
    created = datetime.fromisoformat(job['created_at'].replace('Z', '+00:00')).timestamp()
    restarted = reopen_storage(s)
    # By this point the busy lease/rate limit has expired. Authority, model and
    # credentials remain valid; only the absolute wait deadline rejects work.
    assert restarted.ai_claim_job(created + resume_age, kinds=('preview',)) is None
    result = next(row for row in restarted.ai_jobs(env['project'], env['principal']) if row['id'] == job['id'])
    assert result['state'] == 'failed' and result['error'] == 'unavailable'
    assert result['attempt_count'] == int(paid)
    assert restarted.ai_usage(env['project'], env['principal'])['reserved_requests'] == int(paid)


def test_first_attempt_can_start_after_ordinary_queue_delay(ready_ai_jobs):
    from datetime import datetime
    env = ready_ai_jobs
    job = enqueue(env)
    created = datetime.fromisoformat(job['created_at'].replace('Z', '+00:00')).timestamp()
    claimed = env['store'].ai_claim_job(created + 600, kinds=('preview',))
    assert claimed['id'] == job['id'] and claimed['attempt_count'] == 1
