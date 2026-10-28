"""AI lease, authority and usage races against independent real SQLite handles."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timezone
import threading
import time
from uuid import uuid4

import pytest

from feedback.ai_domain import EMBEDDING_PROFILE, default_ai_settings
from feedback.errors import Error
from conftest import make_storage, reopen_storage


@pytest.fixture
def ready_ai_jobs(tmp_path, database_url):
    store = make_storage(tmp_path / 'ai-job-races.sqlite', 'https://feedback.example', database_url)
    account = store.create_native_account('admin@example.com', 'test-hash', 'Owner', 'admin', True)
    store.create_session(account['id'], 'admin-token-hash', 'csrf-hash', time.time() + 3600)
    principal = {'kind': 'admin', 'user_id': account['id'],
                 'session_id': store.session('admin-token-hash')['session_id']}
    project = store.create_project({'identifier': 'support', 'name': 'Support', 'description': ''}, account['id'])
    pid = project['id']
    settings = default_ai_settings()
    settings['model'] = 'test-model'
    config = store.ai_configure(pid, principal, 0, settings, protected_key='protected-test-key')
    store.ai_store_provider_evidence(
        pid, principal, config['version'], config['credential_version'], 'test-model',
        catalogue=['test-model'], capabilities={'text_generation': True, 'tool_calling': False,
                                               'streaming': False, 'structured_output': False})
    operation = store.ai_runtime_begin(principal, 0, 'download')
    lease = store.ai_runtime_claim(time.time())
    assert store.ai_runtime_finish(operation['operation_id'], lease['lease_token'], manifest=EMBEDDING_PROFILE)
    source = store.ai_source_create(pid, principal, 'markdown', 'Support guide')
    revision = store.ai_revision_stage(pid, principal, source['id'], source['version'],
                                      'Support guide', 'Original support evidence.', 1)
    store.ai_revision_chunks(pid, principal, revision['id'], [
        {'ordinal': 0, 'title': 'Support guide', 'content': 'Original support evidence.',
         'location': None, 'vector': [1.0] + [0.0] * 767}])
    source = store.ai_revision_publish(pid, principal, revision['id'])
    source = store.ai_source_update(pid, principal, source['id'], source['version'], active=True, approved=True)
    config = store.ai_settings(pid, principal)
    settings = deepcopy(config['settings'])
    settings['enabled'] = True
    store.ai_configure(pid, principal, config['version'], settings)
    return {'store': store, 'project': pid, 'principal': principal, 'source': source}


def independent_stores(env, count):
    store = env['store']
    return [reopen_storage(store) for _ in range(count)]


def concurrent(stores, operation):
    barrier = threading.Barrier(len(stores))

    def run(index):
        barrier.wait(timeout=10)
        return operation(stores[index], index)

    with ThreadPoolExecutor(max_workers=len(stores)) as pool:
        return list(pool.map(run, range(len(stores))))


def enqueue(env, *, kind='preview'):
    store, pid, principal = env['store'], env['project'], env['principal']
    config = store.ai_settings(pid, principal)
    resource = env['source']['id'] if kind in ('ingest', 'crawl') else pid
    version = 0 if kind in ('ingest', 'crawl') else config['version']
    return store.ai_enqueue(pid, principal, kind, resource, str(uuid4()), version)


def job_state(store, job_id):
    with store.transaction() as connection:
        return dict(connection.execute(
            'SELECT state,attempt_count,lease_token,lease_until FROM ai_jobs WHERE id=?',
            (job_id,)).fetchone())


def configure(env, **changes):
    store, pid, principal = env['store'], env['project'], env['principal']
    current = store.ai_settings(pid, principal)
    settings = deepcopy(current['settings'])
    settings.update(changes)
    return store.ai_configure(pid, principal, current['version'], settings)


def usage(env, timestamp):
    day = datetime.fromtimestamp(timestamp, timezone.utc).date().isoformat()
    return env['store'].ai_usage(env['project'], env['principal'], day)


@pytest.mark.parametrize('kind', ['ingest', 'preview'])
def test_claim_is_exclusive_across_independent_sqlite_handles(ready_ai_jobs, kind):
    env = ready_ai_jobs
    job = enqueue(env, kind=kind)
    stamp = time.time() + 1
    claims = concurrent(independent_stores(env, 8),
                        lambda store, _: store.ai_claim_job(stamp))
    winners = [claim for claim in claims if claim is not None]
    assert len(winners) == 1
    assert winners[0]['id'] == job['id']
    assert winners[0]['attempt_count'] == 1
    if kind == 'preview':
        assert env['store'].ai_reserve_usage(job['id'], winners[0]['lease_token'], 40, stamp + 1)
    assert env['store'].ai_job_before_dispatch(job['id'], winners[0]['lease_token'], stamp + 1)


def test_expired_lease_and_recovered_token_cannot_finish_each_others_work(ready_ai_jobs):
    env = ready_ai_jobs
    store = env['store']
    queued = enqueue(env)
    stamp = time.time() + 1
    first = store.ai_claim_job(stamp, lease_seconds=5)
    assert store.ai_reserve_usage(first['id'], first['lease_token'], 40, stamp + 1)
    assert not store.ai_job_before_dispatch(first['id'], first['lease_token'], stamp + 5)
    assert not store.ai_finish_job(first['id'], first['lease_token'], 'succeeded', current_time=stamp + 5)
    restarted = independent_stores(env, 1)[0]
    recovered = restarted.ai_claim_job(stamp + 6, lease_seconds=5)
    assert recovered['id'] == queued['id']
    assert recovered['attempt_count'] == 2
    assert recovered['lease_token'] != first['lease_token']
    assert not store.ai_job_before_dispatch(first['id'], first['lease_token'], stamp + 7)
    assert not store.ai_finish_job(first['id'], first['lease_token'], 'failed', current_time=stamp + 7)
    assert restarted.ai_reserve_usage(recovered['id'], recovered['lease_token'], 40, stamp + 7)
    assert restarted.ai_job_before_dispatch(recovered['id'], recovered['lease_token'], stamp + 7)
    assert job_state(store, queued['id'])['lease_token'] == recovered['lease_token']
    assert restarted.ai_finish_job(recovered['id'], recovered['lease_token'], 'succeeded', current_time=stamp + 7)
    assert job_state(store, queued['id'])['state'] == 'succeeded'


def test_repeated_worker_loss_stops_at_the_attempt_limit(ready_ai_jobs):
    env = ready_ai_jobs
    job = enqueue(env, kind='ingest')
    stamp = time.time() + 1
    claims = [env['store'].ai_claim_job(stamp + attempt * 6, lease_seconds=5) for attempt in range(3)]
    assert [claim['attempt_count'] for claim in claims] == [1, 2, 3]
    assert len({claim['lease_token'] for claim in claims}) == 3
    assert env['store'].ai_claim_job(stamp + 18, lease_seconds=5) is None
    assert job_state(env['store'], job['id'])['state'] == 'failed'
    for claim in claims:
        assert not env['store'].ai_finish_job(job['id'], claim['lease_token'], 'succeeded', current_time=stamp + 19)


@pytest.mark.parametrize('request_limit,token_limit,expected', [(3, 1000, 3), (20, 60, 3)])
def test_parallel_reservations_cannot_overrun_request_or_token_budget(
        ready_ai_jobs, request_limit, token_limit, expected):
    env = ready_ai_jobs
    configure(env, daily_request_limit=request_limit, daily_token_limit=token_limit)
    for _ in range(12):
        enqueue(env)
    stamp = time.time() + 1
    claims = [env['store'].ai_claim_job(stamp) for _ in range(12)]
    assert all(claims)
    results = concurrent(independent_stores(env, 12), lambda store, index: store.ai_reserve_usage(
        claims[index]['id'], claims[index]['lease_token'], 20, stamp + 1))
    assert results.count(True) == expected
    assert results.count(False) == 12 - expected
    counters = usage(env, stamp)
    assert counters['reserved_requests'] == expected
    assert counters['reserved_tokens'] == expected * 20
    with env['store'].transaction() as connection:
        assert connection.execute('SELECT count(*) FROM ai_usage_reservations').fetchone()[0] == expected


def test_concurrent_duplicate_reservation_and_completion_are_counted_once(ready_ai_jobs):
    env = ready_ai_jobs
    enqueue(env)
    stamp = time.time() + 1
    claim = env['store'].ai_claim_job(stamp)
    stores = independent_stores(env, 8)
    reservations = concurrent(stores, lambda store, _: store.ai_reserve_usage(
        claim['id'], claim['lease_token'], 40, stamp + 1))
    assert all(reservations)
    assert usage(env, stamp)['reserved_requests'] == 1
    assert usage(env, stamp)['reserved_tokens'] == 40
    dispatches = concurrent(stores, lambda store, _: store.ai_job_before_dispatch(
        claim['id'], claim['lease_token'], stamp + 1))
    assert dispatches.count(True) == 1
    completions = concurrent(stores, lambda store, _: store.ai_finish_job(
        claim['id'], claim['lease_token'], 'succeeded', input_tokens=17, output_tokens=13,
        current_time=stamp + 2))
    assert completions.count(True) == 1
    counters = usage(env, stamp)
    assert counters['reported_input_tokens'] == 17
    assert counters['reported_output_tokens'] == 13
    assert counters['reserved_requests'] == 1
    assert counters['reserved_tokens'] == 30


def test_recovery_reserves_another_attempt_without_refunding_unknown_usage(ready_ai_jobs):
    env = ready_ai_jobs
    enqueue(env)
    stamp = time.time() + 1
    first = env['store'].ai_claim_job(stamp, lease_seconds=5)
    assert env['store'].ai_reserve_usage(first['id'], first['lease_token'], 30, stamp + 1)
    assert env['store'].ai_job_before_dispatch(first['id'], first['lease_token'], stamp + 1)
    recovered = independent_stores(env, 1)[0].ai_claim_job(stamp + 6, lease_seconds=10)
    assert env['store'].ai_reserve_usage(recovered['id'], recovered['lease_token'], 30, stamp + 7)
    assert env['store'].ai_job_before_dispatch(recovered['id'], recovered['lease_token'], stamp + 7)
    assert not env['store'].ai_finish_job(first['id'], first['lease_token'], 'succeeded',
        input_tokens=100, output_tokens=100, current_time=stamp + 8)
    assert env['store'].ai_finish_job(recovered['id'], recovered['lease_token'], 'succeeded',
        input_tokens=7, output_tokens=3, current_time=stamp + 8)
    counters = usage(env, stamp)
    assert counters['reserved_requests'] == 2 and counters['reserved_tokens'] == 40
    assert counters['reported_input_tokens'] == 7 and counters['reported_output_tokens'] == 3
    with env['store'].transaction() as connection:
        assert [row[0] for row in connection.execute(
            'SELECT attempt FROM ai_usage_reservations ORDER BY attempt')] == [1, 2]


def test_usage_migration_reconciles_reported_calls_and_preserves_uncertain_spend(ready_ai_jobs):
    env = ready_ai_jobs
    stamp = time.time() + 1
    for _ in range(2):
        enqueue(env)
    first, second = [env['store'].ai_claim_job(stamp) for _ in range(2)]
    assert env['store'].ai_reserve_usage(first['id'], first['lease_token'], 100, stamp + 1)
    assert env['store'].ai_job_before_dispatch(first['id'], first['lease_token'], stamp + 1)
    assert env['store'].ai_finish_job(first['id'], first['lease_token'], 'succeeded', input_tokens=12, output_tokens=8, current_time=stamp + 2)
    assert env['store'].ai_reserve_usage(second['id'], second['lease_token'], 100, stamp + 3)
    assert env['store'].ai_job_before_dispatch(second['id'], second['lease_token'], stamp + 3)
    from feedback.storage.sqlite import SQLiteStorage
    migration = '028_ai_usage_reconciliation.sql' if isinstance(env['store'], SQLiteStorage) else '008_ai_usage_reconciliation.sql'
    with env['store'].transaction(True) as c:
        # Reconstruct the legacy counter, including an uncertain second call.
        c.execute('UPDATE ai_usage SET reserved_tokens=200 WHERE project_id=?', (env['project'],))
        c.execute('DELETE FROM schema_migrations WHERE version=?', (migration,))
    restored = reopen_storage(env['store'])
    assert restored.ai_usage(env['project'], env['principal'])['reserved_tokens'] == 120
    assert reopen_storage(restored).ai_usage(env['project'], env['principal'])['reserved_tokens'] == 120


@pytest.mark.parametrize('phase', ['before_dispatch', 'before_finish'])
@pytest.mark.parametrize('revocation', ['session_revoked', 'session_expired', 'admin_demoted'])
def test_native_authority_is_rechecked_after_claim(ready_ai_jobs, phase, revocation):
    env = ready_ai_jobs
    job = enqueue(env)
    store = env['store']
    stamp = time.time() + 1
    claim = store.ai_claim_job(stamp)
    assert store.ai_reserve_usage(job['id'], claim['lease_token'], 40, stamp + 1)
    if phase == 'before_finish':
        assert store.ai_job_before_dispatch(job['id'], claim['lease_token'], stamp + 1)
    if revocation == 'session_revoked':
        store.revoke_session(env['principal']['session_id'])
    else:
        with store.transaction(True) as connection:
            if revocation == 'session_expired':
                connection.execute('UPDATE sessions SET expires_at=0 WHERE id=?', (env['principal']['session_id'],))
            else:
                connection.execute("UPDATE users SET role='participant' WHERE id=?", (env['principal']['user_id'],))
    if phase == 'before_dispatch':
        assert not store.ai_job_before_dispatch(job['id'], claim['lease_token'], stamp + 2)
    assert not store.ai_finish_job(job['id'], claim['lease_token'], 'succeeded', current_time=stamp + 2)
    assert store.ai_claim_job(stamp + 61) is None
    assert job_state(store, job['id'])['state'] == 'cancelled'


@pytest.mark.parametrize('phase', ['before_dispatch', 'before_finish'])
@pytest.mark.parametrize('change', ['personality', 'provider_key', 'off_on'])
def test_config_changes_fence_claimed_work_even_after_reenable(ready_ai_jobs, phase, change):
    env = ready_ai_jobs
    store, pid, principal = env['store'], env['project'], env['principal']
    original = store.ai_settings(pid, principal)
    job = enqueue(env)
    stamp = time.time() + 1
    claim = store.ai_claim_job(stamp)
    assert store.ai_reserve_usage(job['id'], claim['lease_token'], 40, stamp + 1)
    if phase == 'before_finish':
        assert store.ai_job_before_dispatch(job['id'], claim['lease_token'], stamp + 1)
    if change == 'personality':
        updated = configure(env, soul='Use concise explanations.')
    elif change == 'off_on':
        configure(env, enabled=False)
        updated = configure(env, enabled=True)
    else:
        # A replacement key has no capability evidence and cannot inherit an
        # enabled generation. Explicitly disable while replacing it.
        settings = deepcopy(original['settings'])
        settings['enabled'] = False
        updated = store.ai_configure(pid, principal, original['version'], settings,
                                     protected_key='different-protected-key')
    assert updated['version'] > original['version']
    if phase == 'before_dispatch':
        assert not store.ai_job_before_dispatch(job['id'], claim['lease_token'], stamp + 2)
    assert not store.ai_finish_job(job['id'], claim['lease_token'], 'succeeded', current_time=stamp + 2)
    assert store.ai_claim_job(stamp + 61) is None
    assert job_state(store, job['id'])['state'] == 'cancelled'


def test_configuration_change_racing_claim_cannot_leave_dispatchable_old_work(ready_ai_jobs):
    env = ready_ai_jobs
    job = enqueue(env)
    stamp = time.time() + 1
    stores = independent_stores(env, 2)
    original = env['store'].ai_settings(env['project'], env['principal'])
    settings = deepcopy(original['settings'])
    settings['soul'] = 'Updated while a worker was claiming.'

    def race(store, index):
        if index == 0:
            return store.ai_claim_job(stamp)
        return store.ai_configure(env['project'], env['principal'], original['version'], settings)

    claim, changed = concurrent(stores, race)
    assert changed['version'] > original['version']
    if claim is not None:
        assert not stores[0].ai_reserve_usage(job['id'], claim['lease_token'], 40, stamp + 1)
        assert not stores[0].ai_job_before_dispatch(job['id'], claim['lease_token'], stamp + 1)
        assert not stores[0].ai_finish_job(job['id'], claim['lease_token'], 'succeeded', current_time=stamp + 1)
    assert job_state(env['store'], job['id'])['state'] == 'cancelled'


def test_concurrent_imports_preserve_only_newest_intent_and_receipt_retries(ready_ai_jobs):
    env = ready_ai_jobs
    store, pid, principal, source = env['store'], env['project'], env['principal'], env['source']
    stores = independent_stores(env, 8)
    original = store.ai_knowledge_chunks(pid, principal)
    assert [chunk['content'] for chunk in original] == ['Original support evidence.']
    first = store.ai_enqueue(pid, principal, 'ingest', source['id'], 'upload-a', 0,
                             payload={'upload': 'A'})
    first_lease = store.ai_claim_job(time.time() + 1)
    assert first_lease['id'] == first['id']
    first_version = first_lease['authority']['source_version']
    assert first_version == source['version'] + 1
    first_revision = store.ai_revision_stage(
        pid, principal, source['id'], first_version, 'Older upload', 'Older replacement.', 1,
        job_id=first['id'], lease_token=first_lease['lease_token'])
    first_chunks = [{'ordinal': 0, 'title': 'Older upload', 'content': 'Older replacement.',
                     'location': None, 'vector': [1.0] + [0.0] * 767}]
    store.ai_revision_chunks(pid, principal, first_revision['id'], first_chunks,
                             lease_token=first_lease['lease_token'])
    assert store.ai_knowledge_chunks(pid, principal) == original

    uploads = concurrent(stores, lambda worker, index: worker.ai_enqueue(
        pid, principal, 'ingest' if index % 2 == 0 else 'crawl', source['id'],
        f'concurrent-upload-{index}', 0, payload={'upload': index}))
    assert {job['authority']['source_version'] for job in uploads} == set(range(first_version + 1, first_version + 9))
    latest = max(uploads, key=lambda job: job['authority']['source_version'])
    claims = concurrent(stores, lambda worker, _: worker.ai_claim_job(time.time() + 1))
    winners = [claim for claim in claims if claim is not None]
    assert len(winners) == 1
    latest_lease = winners[0]
    assert latest_lease['id'] == latest['id']
    latest_version = latest_lease['authority']['source_version']
    assert latest_version == first_version + 8
    assert job_state(store, first['id'])['state'] == 'cancelled'
    assert all(job_state(store, job['id'])['state'] == 'cancelled'
               for job in uploads if job['id'] != latest['id'])
    assert store.ai_knowledge_chunks(pid, principal) == original
    # Even adopting the newer source version cannot make the superseded lease valid.
    for version in (first_version, latest_version):
        with pytest.raises(Error) as exc:
            store.ai_revision_stage(
                pid, principal, source['id'], version, 'Stale retry', 'Stale replacement.', 1,
                job_id=first['id'], lease_token=first_lease['lease_token'])
        assert exc.value.status == 409
    with pytest.raises(Error) as exc:
        store.ai_revision_chunks(pid, principal, first_revision['id'], first_chunks,
                                 lease_token=first_lease['lease_token'])
    assert exc.value.status == 409
    with pytest.raises(Error) as exc:
        store.ai_revision_publish(pid, principal, first_revision['id'],
                                  lease_token=first_lease['lease_token'])
    assert exc.value.status == 409

    receipt = store.ai_enqueue(pid, principal, 'ingest', source['id'], 'upload-a', 0,
                               payload={'upload': 'A'})
    assert receipt['id'] == first['id'] and receipt['state'] == 'cancelled'
    retries = concurrent(stores, lambda worker, index: worker.ai_enqueue(
        pid, principal, uploads[index]['kind'], source['id'], uploads[index]['dedupe_key'],
        0, payload=uploads[index]['payload']))
    assert [receipt['id'] for receipt in retries] == [job['id'] for job in uploads]
    assert sum(receipt['state'] == 'cancelled' for receipt in retries) == 7
    assert next(receipt for receipt in retries if receipt['id'] == latest['id'])['state'] == 'running'
    with pytest.raises(Error) as exc:
        store.ai_enqueue(pid, principal, 'ingest', source['id'], 'upload-a', 0,
                         payload={'upload': 'changed'})
    assert exc.value.status == 409
    current_source = next(item for item in store.ai_sources(pid, principal) if item['id'] == source['id'])
    assert current_source['version'] == latest_version
    assert current_source['active_revision_id'] == source['active_revision_id']
    assert job_state(store, latest['id'])['lease_token'] == latest_lease['lease_token']
    assert job_state(store, latest['id'])['state'] == 'running'

    latest_revision = stores[0].ai_revision_stage(
        pid, principal, source['id'], latest_version, 'Newest upload', 'Newest replacement.', 1,
        job_id=latest['id'], lease_token=latest_lease['lease_token'])
    stores[0].ai_revision_chunks(pid, principal, latest_revision['id'], [
        {'ordinal': 0, 'title': 'Newest upload', 'content': 'Newest replacement.',
         'location': None, 'vector': [1.0] + [0.0] * 767}], lease_token=latest_lease['lease_token'])
    assert store.ai_knowledge_chunks(pid, principal) == original
    published = stores[0].ai_revision_publish(
        pid, principal, latest_revision['id'], lease_token=latest_lease['lease_token'])
    assert published['active_revision_id'] == latest_revision['id']
    assert published['version'] == latest_version + 1
    assert [chunk['content'] for chunk in store.ai_knowledge_chunks(pid, principal)] == ['Newest replacement.']
    # Publication itself commits job completion; no separate worker finish is needed.
    assert job_state(store, latest['id'])['state'] == 'succeeded'
    assert job_state(store, latest['id'])['lease_token'] is None
    assert job_state(store, first['id'])['state'] == 'cancelled'
