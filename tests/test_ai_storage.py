"""Real SQLite AI foundations: opt-ins, authorization, index publication and fences."""
import json
import time
from uuid import uuid4

import pytest

from feedback.ai_domain import EMBEDDING_PROFILE, GuestActor, default_ai_settings
from feedback.errors import Error
from conftest import make_storage

CAPS = dict(text_generation=True, tool_calling=False, streaming=False, structured_output=False)


@pytest.fixture
def ai_store(tmp_path, database_url):
    storage = make_storage(tmp_path / 'ai.sqlite', 'https://feedback.example', database_url)
    user = storage.create_native_account('admin@example.com', 'hash', 'Admin', 'admin', True)
    storage.create_session(user['id'], 'admin-token-hash', 'csrf', time.time() + 3600)
    principal = {'kind': 'admin', 'user_id': user['id'], 'session_id': storage.session('admin-token-hash')['session_id']}
    project = storage.create_project(dict(identifier='one', name='One', description=''), user['id'])
    other = storage.create_project(dict(identifier='two', name='Two', description=''), user['id'])
    return storage, principal, project['id'], other['id']


def ready_runtime(s, p):
    started = s.ai_runtime_begin(p, s.ai_runtime(p)['version'], 'download', 'protected-hf-token')
    claimed = s.ai_runtime_claim(time.time())
    assert claimed['operation_id'] == started['operation_id']
    assert s.ai_runtime_advance(claimed['operation_id'], claimed['lease_token'], 10, 10, verifying=True)
    assert s.ai_runtime_finish(claimed['operation_id'], claimed['lease_token'], manifest=EMBEDDING_PROFILE)


def chunk(ordinal=0, content='Original guide for backups'):
    return dict(ordinal=ordinal, title='Guide', content=content, location=None, vector=[1.0] + [0.0] * 767)


def publish_source(s, p, pid, text='Original guide for backups'):
    source = s.ai_source_create(pid, p, 'markdown', 'Guide')
    revision = s.ai_revision_stage(pid, p, source['id'], source['version'], 'Guide', text, 1)
    s.ai_revision_chunks(pid, p, revision['id'], [chunk(content=text)])
    source = s.ai_revision_publish(pid, p, revision['id'])
    source = s.ai_source_update(pid, p, source['id'], source['version'], active=True, approved=True)
    return source, revision


def ready_project(s, p, pid):
    config = default_ai_settings()
    config['model'] = 'exact/model:ID'
    result = s.ai_configure(pid, p, 0, config, protected_key='encrypted-key')
    s.ai_store_provider_evidence(pid, p, result['version'], result['credential_version'], config['model'], catalogue=[config['model']], capabilities=CAPS)
    ready_runtime(s, p)
    source, revision = publish_source(s, p, pid)
    return s.ai_settings(pid, p), source, revision


def test_defaults_and_native_admin_boundary(ai_store):
    s, p, pid, other = ai_store
    default = s.ai_settings(pid, p)
    assert default['version'] == 0 and default['settings'] == default_ai_settings()
    assert default['has_key'] is False and 'protected_key' not in default
    assert not any(default['settings']['tools'].values())
    for principal in ({'kind': 'visitor'}, {**p, 'kind': 'external'}, {**p, 'session_id': 'missing'}):
        with pytest.raises(Error) as exc:
            s.ai_settings(pid, principal)
        assert exc.value.status in (401, 403)
    incomplete = default_ai_settings()
    del incomplete['allow_guest_reports']
    with pytest.raises(Error) as exc:
        s.ai_configure(pid, p, 0, incomplete)
    assert exc.value.code == 'invalid_input'
    assert s.ai_configure(pid, p, 0, default_ai_settings())['version'] == 0


def test_exact_provider_evidence_and_key_rotation(ai_store):
    s, p, pid, other = ai_store
    settings = default_ai_settings() | {'model': 'exact/model:ID'}
    initial = s.ai_configure(pid, p, 0, settings, protected_key='first-key')
    assert initial['has_key'] and initial['credential_version'] == 1
    assert 'first-key' not in json.dumps(initial)
    s.ai_store_provider_evidence(pid, p, 1, 1, settings['model'], catalogue=[settings['model']], capabilities=CAPS)
    rotated = s.ai_configure(pid, p, 1, settings, protected_key='second-key')
    assert rotated['credential_version'] == 2 and rotated['capabilities'] == {} and rotated['catalogue'] == []
    with pytest.raises(Error) as exc:
        s.ai_store_provider_evidence(pid, p, 1, 1, settings['model'], catalogue=[settings['model']], capabilities=CAPS)
    assert exc.value.status == 409
    assert s.ai_provider_snapshot(pid, p, 2)['protected_key'] == 'second-key'
    with s.transaction(True) as c:
        c.execute('UPDATE sessions SET revoked_at=? WHERE id=?', ('revoked', p['session_id']))
    with pytest.raises(Error) as exc:
        s.ai_store_provider_evidence(pid, p, 2, 2, settings['model'], catalogue=[settings['model']], capabilities=CAPS)
    assert exc.value.status == 401


@pytest.mark.parametrize('catalogue', [[{}], [['model']], [1]])
def test_malformed_catalogue_is_safe_error(ai_store, catalogue):
    s, p, pid, _ = ai_store
    with pytest.raises(Error) as exc:
        s.ai_store_provider_evidence(pid, p, 0, 0, None, catalogue=catalogue)
    assert exc.value.code == 'invalid_input'


@pytest.mark.parametrize('destination', [4, [], {}, 'javascript:alert(1)', 'https://name:password@example.com/x'])
def test_feedback_destinations_validate_without_fetch(ai_store, destination):
    s, p, pid, _ = ai_store
    settings = default_ai_settings() | {'feedback_url': destination}
    with pytest.raises(Error) as exc:
        s.ai_configure(pid, p, 0, settings)
    assert exc.value.code == 'invalid_input'


def test_enable_requires_readiness_and_explicit_tool_capability(ai_store):
    s, p, pid, other = ai_store
    with pytest.raises(Error) as exc:
        s.ai_configure(pid, p, 0, default_ai_settings() | {'enabled': True})
    assert exc.value.code == 'ai_not_ready'
    cfg, source, _ = ready_project(s, p, pid)
    settings = cfg['settings'] | {'enabled': True, 'feedback_url': 'https://support.example/feedback?project=one#bugs'}
    enabled = s.ai_configure(pid, p, cfg['version'], settings)
    assert enabled['settings']['enabled'] and not any(enabled['settings']['tools'].values())
    settings = json.loads(json.dumps(settings))
    settings['tools']['support_link'] = True
    with pytest.raises(Error) as exc:
        s.ai_configure(pid, p, enabled['version'], settings)
    assert exc.value.code == 'ai_not_ready'
    assert s.ai_settings(pid, p)['version'] == enabled['version']
    s.ai_store_provider_evidence(pid, p, enabled['version'], cfg['credential_version'], settings['model'], capabilities=CAPS | {'tool_calling': True})
    tools = s.ai_configure(pid, p, enabled['version'], settings)
    assert tools['settings']['tools']['support_link'] and not tools['settings']['allow_guest_reports']
    # Catalogue access loss explicitly turns the project off; it cannot silently fallback.
    lost = s.ai_store_provider_evidence(pid, p, tools['version'], cfg['credential_version'], settings['model'], catalogue=[])
    assert lost['version'] == tools['version'] + 1 and not lost['settings']['enabled']


def test_destination_board_must_belong_to_project(ai_store):
    s, p, pid, other = ai_store
    board = s.create_board(other, dict(identifier='bugs', name='Bugs', description=''))
    with pytest.raises(Error) as exc:
        s.ai_configure(pid, p, 0, default_ai_settings() | {'bug_board_id': board['id']})
    assert exc.value.status == 404


def test_runtime_is_explicit_fenced_and_native_authorized(ai_store):
    s, p, pid, _ = ai_store
    assert s.ai_runtime(p)['state'] == 'not_downloaded'
    operation = s.ai_runtime_begin(p, 0, 'download', 'protected-hf')
    with pytest.raises(Error):
        s.ai_runtime_begin(p, operation['version'], 'download')
    first = s.ai_runtime_claim(time.time(), 1)
    recovered = s.ai_runtime_claim(first['lease_until'] + .1, 60)
    assert first['lease_token'] != recovered['lease_token']
    assert not s.ai_runtime_finish(first['operation_id'], first['lease_token'], manifest=EMBEDDING_PROFILE)
    assert s.ai_runtime_finish(recovered['operation_id'], recovered['lease_token'], manifest=EMBEDDING_PROFILE, current_time=first['lease_until'] + .2)
    view = s.ai_runtime(p)
    assert view['state'] == 'ready' and 'protected_token' not in view
    assert 'protected-hf' not in s.signing_secrets()
    removal = s.ai_runtime_begin(p, view['version'], 'remove')
    with pytest.raises(Error):
        s.ai_runtime_cancel(p, removal['version'])
    claim = s.ai_runtime_claim(time.time())
    assert s.ai_runtime_finish(claim['operation_id'], claim['lease_token'], error='unavailable')
    assert s.ai_runtime(p)['state'] == 'failed'


def test_runtime_revoked_initiator_cannot_activate(ai_store):
    s, p, pid, _ = ai_store
    s.ai_runtime_begin(p, 0, 'download', 'protected-hf')
    claim = s.ai_runtime_claim(time.time())
    with s.transaction(True) as c:
        c.execute('UPDATE sessions SET revoked_at=? WHERE id=?', ('revoked', p['session_id']))
    assert not s.ai_runtime_finish(claim['operation_id'], claim['lease_token'], manifest=EMBEDDING_PROFILE)
    with s.transaction() as c:
        row = c.execute('SELECT * FROM ai_runtime').fetchone()
        assert row['active_manifest'] is None and row['protected_token'] is None


def test_revision_atomic_publication_keeps_old_until_complete_and_scoped(ai_store):
    s, p, pid, other = ai_store
    source, first = publish_source(s, p, pid)
    assert s.ai_knowledge_chunks(other, p) == []
    assert s.ai_knowledge_chunks(pid, p, 'backups* OR "bad"')[0]['revision_id'] == first['id']
    with pytest.raises(Error) as exc:
        s.ai_revision_stage(other, p, source['id'], source['version'], 'Bad', 'wrong project', 1)
    assert exc.value.status == 404
    second = s.ai_revision_stage(pid, p, source['id'], source['version'], 'Updated', 'Updated docs', 2)
    s.ai_revision_chunks(pid, p, second['id'], [chunk(0, 'Updated docs')])
    with pytest.raises(Error):
        s.ai_revision_publish(pid, p, second['id'])
    assert s.ai_knowledge_chunks(pid, p)[0]['revision_id'] == first['id']
    s.ai_revision_chunks(pid, p, second['id'], [chunk(1, 'Other section')])
    assert s.ai_revision_chunks(pid, p, second['id'], [chunk(1, 'Other section')]) == 2
    source = s.ai_revision_publish(pid, p, second['id'])
    assert {x['revision_id'] for x in s.ai_knowledge_chunks(pid, p)} == {second['id']}
    s.ai_source_update(pid, p, source['id'], source['version'], active=False, approved=True)
    assert s.ai_knowledge_chunks(pid, p) == []
    with s.transaction() as c:
        assert c.execute('SELECT length(vector) FROM ai_chunks LIMIT 1').fetchone()[0] == 3072
        assert c.execute('SELECT count(*) FROM ai_chunks').fetchone()[0] == 3


def test_chunks_bad_vector_and_batch_conflict_roll_back(ai_store):
    s, p, pid, _ = ai_store
    source = s.ai_source_create(pid, p, 'markdown', 'Guide')
    revision = s.ai_revision_stage(pid, p, source['id'], source['version'], 'Guide', 'Original', 2)
    bad = chunk() | {'vector': [float('nan')] + [0] * 767}
    with pytest.raises(Error):
        s.ai_revision_chunks(pid, p, revision['id'], [bad])
    s.ai_revision_chunks(pid, p, revision['id'], [chunk(0)])
    with pytest.raises(Error):
        s.ai_revision_chunks(pid, p, revision['id'], [chunk(1), chunk(0, 'Conflicting text')])
    with s.transaction() as c:
        assert c.execute('SELECT count(*) FROM ai_chunks').fetchone()[0] == 1


def test_replaced_worker_cannot_stage_append_or_publish(ai_store, monkeypatch):
    import feedback.storage.sqlite as adapter
    s, p, pid, _ = ai_store
    source = s.ai_source_create(pid, p, 'markdown', 'Guide')
    queued = s.ai_enqueue(pid, p, 'ingest', source['id'], 'ingest-once', 0)
    source['version'] = queued['authority']['source_version']
    t = time.time()
    first = s.ai_claim_job(t + .01, 1)
    revision = s.ai_revision_stage(pid, p, source['id'], source['version'], 'Guide', 'Original', 1, job_id=queued['id'], lease_token=first['lease_token'])
    s.ai_revision_chunks(pid, p, revision['id'], [chunk()], lease_token=first['lease_token'])
    second = s.ai_claim_job(t + 2, 60)
    monkeypatch.setattr(adapter.time, 'time', lambda: t + 2.1)
    with pytest.raises(Error):
        s.ai_revision_stage(pid, p, source['id'], source['version'], 'Guide', 'Stale', 1, job_id=queued['id'], lease_token=first['lease_token'])
    with pytest.raises(Error):
        s.ai_revision_chunks(pid, p, revision['id'], [chunk()], lease_token=first['lease_token'])
    with pytest.raises(Error):
        s.ai_revision_publish(pid, p, revision['id'], lease_token=first['lease_token'])
    s.ai_revision_publish(pid, p, revision['id'], lease_token=second['lease_token'])
    assert s.ai_jobs(pid, p)[0]['state'] == 'succeeded'


def test_queue_limits_preserve_idempotent_retries(ai_store):
    s, p, pid, _ = ai_store
    source = s.ai_source_create(pid, p, 'markdown', 'Guide')
    first = s.ai_enqueue(pid, p, 'ingest', source['id'], '0', 0)
    for index in range(1, 50):
        another = s.ai_source_create(pid, p, 'markdown', 'Guide')
        s.ai_enqueue(pid, p, 'ingest', another['id'], str(index), 0)
    assert s.ai_enqueue(pid, p, 'ingest', source['id'], '0', 0)['id'] == first['id']
    with pytest.raises(Error) as exc:
        another = s.ai_source_create(pid, p, 'markdown', 'Guide')
        s.ai_enqueue(pid, p, 'ingest', another['id'], 'overflow', 0)
    assert exc.value.code == 'ai_busy'


@pytest.mark.parametrize('old_kind,new_kind', [('ingest', 'crawl'), ('crawl', 'ingest')])
def test_new_import_supersedes_old_work_without_hiding_published_knowledge(ai_store, old_kind, new_kind):
    s, p, pid, _ = ai_store
    source, active = publish_source(s, p, pid)
    old = s.ai_enqueue(pid, p, old_kind, source['id'], 'old', 0, {'content': 'old'})
    first = s.ai_claim_job(time.time())
    revision = s.ai_revision_stage(pid, p, source['id'], old['authority']['source_version'], 'Guide', 'Older import', 1, job_id=old['id'], lease_token=first['lease_token'])
    s.ai_revision_chunks(pid, p, revision['id'], [chunk()], lease_token=first['lease_token'])
    new = s.ai_enqueue(pid, p, new_kind, source['id'], 'new', 0, {'content': 'new'})
    assert new['authority']['source_version'] == old['authority']['source_version'] + 1
    assert s.ai_knowledge_chunks(pid, p)[0]['revision_id'] == active['id']
    with pytest.raises(Error):
        s.ai_revision_publish(pid, p, revision['id'], lease_token=first['lease_token'])
    retry = s.ai_enqueue(pid, p, old_kind, source['id'], 'old', 0, {'content': 'old'})
    assert retry['id'] == old['id'] and retry['state'] == 'cancelled'
    with pytest.raises(Error):
        s.ai_enqueue(pid, p, old_kind, source['id'], 'old', 0, {'content': 'changed'})
    assert s.ai_sources(pid, p)[0]['version'] == new['authority']['source_version']
    assert s.ai_claim_job(time.time())['id'] == new['id']


def test_usage_tracks_each_call_conservatively_and_caps_rounds(ai_store):
    s, p, pid, _ = ai_store
    cfg, _, _ = ready_project(s, p, pid)
    s.ai_enqueue(pid, p, 'preview', pid, 'preview-one', cfg['version'])
    job = s.ai_claim_job(time.time())
    jid, token = job['id'], job['lease_token']
    assert not s.ai_job_before_dispatch(jid, token)
    assert s.ai_reserve_usage(jid, token, 100)
    assert s.ai_reserve_usage(jid, token, 100)
    assert s.ai_job_before_dispatch(jid, token)
    assert not s.ai_job_before_dispatch(jid, token)
    assert s.ai_report_usage(jid, token, 0, 110, 20)
    assert s.ai_report_usage(jid, token, 0, 110, 20)
    assert s.ai_reserve_usage(jid, token, 100, call_index=1)
    assert not s.ai_reserve_usage(jid, token, 100, call_index=9)
    assert s.ai_finish_job(jid, token, 'cancelled')
    usage = s.ai_usage(pid, p)
    assert usage['reserved_requests'] == 2 and usage['reserved_tokens'] == 230
    assert usage['reported_input_tokens'] == 110 and usage['reported_output_tokens'] == 20


def test_guest_mapping_is_private_credentialless_and_does_not_rewrite_customer(ai_store):
    s, p, pid, other = ai_store
    s.chat_configure(pid, p, True, ['https://website.example'], 0)
    conversation = s.chat_create_session(pid, 'https://website.example', 'visitor-hash', time.time() + 3600)
    with s.transaction() as c:
        customer = dict(c.execute('SELECT * FROM chat_customers WHERE id=(SELECT customer_id FROM chat_conversations WHERE id=?)', (conversation['id'],)).fetchone())
    assert customer['user_id'] is None and customer['kind'] == 'anonymous'
    with pytest.raises(Error):
        with s.transaction(True) as c:
            c.execute('INSERT INTO ai_guest_owners VALUES(?,?,?,?)', (customer['id'], pid, p['user_id'], 'now'))
    guest_id = str(uuid4())
    with s.transaction(True) as c:
        c.execute("INSERT INTO users VALUES(?,NULL,'participant','now','now')", (guest_id,))
    with pytest.raises(Error):
        with s.transaction(True) as c:
            c.execute('INSERT INTO ai_guest_owners VALUES(?,?,?,?)', (customer['id'], other, guest_id, 'now'))
    with s.transaction(True) as c:
        c.execute('INSERT INTO ai_guest_owners VALUES(?,?,?,?)', (customer['id'], pid, guest_id, 'now'))
    with s.transaction() as c:
        assert c.execute('SELECT user_id FROM chat_customers WHERE id=?', (customer['id'],)).fetchone()[0] is None
        assert not c.execute('SELECT 1 FROM local_credentials WHERE user_id=?', (guest_id,)).fetchone()
        assert not c.execute('SELECT 1 FROM external_identities WHERE user_id=?', (guest_id,)).fetchone()
    assert GuestActor(user_id=guest_id).record() == {'user_id': guest_id, 'api_key_id': None, 'kind': 'guest'}


def test_abandon_unresumable_download_preserves_prior_cache(ai_store):
    s, p, pid, _ = ai_store
    ready_runtime(s, p)
    active = s.ai_runtime(p)
    replacement = s.ai_runtime_begin(p, active['version'], 'download', 'protected-replacement-token')
    job = s.ai_runtime_claim(time.time())
    assert not s.ai_runtime_abandon_download(replacement['version'] - 1)
    assert s.ai_runtime_abandon_download(replacement['version'])
    restored = s.ai_runtime_local()
    assert restored['state'] == 'ready' and restored['active_cache_id'] == active['active_cache_id']
    assert restored['active_manifest'] == EMBEDDING_PROFILE and restored['operation_id'] is None
    assert 'protected-replacement-token' not in s.signing_secrets()
    assert not s.ai_runtime_finish(job['operation_id'], job['lease_token'], manifest=EMBEDDING_PROFILE)
    assert not s.ai_runtime_abandon_download(restored['version'])
    removal = s.ai_runtime_begin(p, restored['version'], 'remove')
    assert not s.ai_runtime_abandon_download(removal['version'])
