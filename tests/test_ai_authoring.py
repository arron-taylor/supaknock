"""Durable native-admin authoring APIs with real SQLite and fixture providers."""
import json
import asyncio
import threading
import time
from uuid import uuid4

import pytest

from feedback.ai_domain import EMBEDDING_PROFILE, default_ai_settings
from feedback.ai_providers import GenerationResult, ProviderError, ToolCall, Usage
from feedback.ai_retrieval import IngestionWorker
from feedback.errors import Error
from feedback.security import digest


class Embeddings:
    def available(self): return True
    def embed_documents(self, texts, *, titles=None): return [[1.] + [0.] * 767 for _ in texts]
    def embed_queries(self, texts): return [[1.] + [0.] * 767 for _ in texts]


class FixtureProvider:
    def __init__(self):
        self.calls = []
        self.mode = 'preview'
        self.hook = None
        self.responses = []

    async def generate(self, key, model, messages, **options):
        data = json.loads(messages[-1].content)
        self.calls.append({'key': key, 'model': model, 'messages': messages, 'data': data, **options})
        if self.hook:
            hook, self.hook = self.hook, None
            hook()
        if self.responses:
            response = self.responses.pop(0)
            if isinstance(response, Exception): raise response
            return response
        if data.get('operation') == 'evidence_check':
            return GenerationResult('{"assessment":"Fixture evidence supports this draft.","supported":true,"issues":[]}', (), Usage(7, 5, 12), 'stop')
        ids = [passage['id'] for passage in data['passages']][:1]
        if self.mode == 'sections':
            value = {'sections': [{'kind': 'section', 'title': 'Installation', 'body': 'Follow the documented installation procedure.', 'category': 'Getting started', 'citation_ids': ids},
                {'kind': 'faq', 'title': 'How do I restart?', 'body': 'Restart after saving the configuration.', 'category': 'Configuration', 'citation_ids': ids}]}
        else:
            value = {'status': 'answered', 'body': 'Restart after saving the configuration.', 'citation_ids': ids}
        return GenerationResult(json.dumps(value), (), Usage(7, 5, 12), 'stop')


@pytest.fixture
def authoring(workflow):
    w = workflow
    s = w['app'].state.storage
    session = s.session(digest(w['admin'].cookies['feedback_session']))
    p = {'kind': 'admin', 'user_id': session['id'], 'session_id': session['session_id']}
    pid = w['p']['id']
    operation = s.ai_runtime_begin(p, 0, 'download')
    lease = s.ai_runtime_claim(time.time())
    assert s.ai_runtime_finish(operation['operation_id'], lease['lease_token'], manifest=EMBEDDING_PROFILE)
    original = s.ai_knowledge_import(pid, p, str(uuid4()), 'guide.md', 'Support guide', '# Setup\n\nFollow the documented installation procedure. Restart after saving the configuration.')
    assert IngestionWorker(s, Embeddings()).run_once()
    source = s.ai_knowledge_source(pid, p, original['source']['id'])
    source = s.ai_knowledge_change(pid, p, source['id'], source['version'], active=True, approved=True)
    settings = w['app'].state.ai_settings.save(pid, p, 0, default_ai_settings() | {'model': 'fixture-model'}, key='fixture-provider-secret')
    settings = s.ai_store_provider_evidence(pid, p, settings['version'], settings['credential_version'], 'fixture-model',
        catalogue=['fixture-model'], capabilities={'text_generation': True, 'tool_calling': False, 'structured_output': False, 'streaming': False})
    fake = FixtureProvider()
    w['app'].state.ai_generation.provider_factory = lambda provider: fake
    w['app'].state.ai_retrieval.installation = Embeddings()
    return w | {'s': s, 'principal': p, 'fake': fake, 'source': source, 'settings': settings,
        'worker': w['app'].state.ai_authoring_worker, 'url': '/api/v1/projects/' + pid + '/ai'}


def preview(ai, **changes):
    body = {'client_request_id': str(uuid4()), 'version': ai['settings']['version'], 'question': 'When should I restart?'} | changes
    response = ai['admin'].post(ai['url'] + '/previews', json=body)
    assert response.status_code == 202, response.text
    return response.json(), body


def sections(ai):
    ai['fake'].mode = 'sections'
    response = ai['admin'].post(ai['url'] + '/knowledge/sources/' + ai['source']['id'] + '/sections/generate', json={
        'client_request_id': str(uuid4()), 'version': ai['settings']['version'], 'source_version': ai['source']['version']})
    assert response.status_code == 202, response.text
    return response.json()


def read(ai, run):
    response = ai['admin'].get(ai['url'] + '/authoring-runs/' + run['id'])
    assert response.status_code == 200, response.text
    return response.json()


def batched_source(ai, count=12):
    content = '\n\n'.join(f'# Topic {i}\n\nOriginal support facts for topic {i}.' for i in range(count))
    ai['s'].ai_knowledge_import(ai['p']['id'], ai['principal'], str(uuid4()), 'guide.md', 'Support guide', content,
        source_id=ai['source']['id'], version=ai['source']['version'])
    assert IngestionWorker(ai['s'], Embeddings()).run_once()
    ai['source'] = ai['s'].ai_knowledge_source(ai['p']['id'], ai['principal'], ai['source']['id'])


def test_batches_preserve_drafts_and_explicit_retry_skips_completed_work(authoring):
    ai = authoring
    batched_source(ai)
    first = {'sections': [{'kind': 'section', 'title': 'First batch', 'body': 'Original support facts.', 'category': 'Support', 'citation_ids': ['S1']}]}
    ai['fake'].responses = [GenerationResult(json.dumps(first), (), Usage(7, 5, 12), 'stop'), ProviderError('incomplete_response')]
    run = sections(ai)
    assert ai['worker'].run_once()
    partial = read(ai, run)
    assert partial['status'] == 'partial' and partial['error'] == 'incomplete_response'
    assert partial['coverage']['completed_batches'] == 1 and partial['coverage']['completed_passages'] == 5
    assert len(partial['sections']) == 1
    saved = partial['sections'][0]
    edit = {k: saved[k] for k in ('title', 'body', 'category', 'citation_ids')}
    edit['title'] = 'Administrator edited this saved draft'
    assert ai['admin'].put(ai['url'] + '/knowledge/sections/' + saved['id'], json=edit | {'version': saved['version']}).status_code == 200
    body = {'version': partial['version'], 'client_request_id': str(uuid4())}
    endpoint = ai['url'] + '/authoring-runs/' + run['id'] + '/retry'
    queued = ai['admin'].post(endpoint, json=body)
    assert queued.status_code == 202 and queued.json()['status'] == 'queued'
    assert ai['admin'].post(endpoint, json=body).json() == queued.json()
    assert ai['worker'].run_once()
    done = read(ai, run)
    assert done['status'] == 'succeeded' and done['error'] is None
    assert done['coverage']['completed_batches'] == 3 and done['coverage']['completed_passages'] == 12
    assert len(done['sections']) == 5 and len({s['id'] for s in done['sections']}) == 5
    assert next(s for s in done['sections'] if s['id'] == saved['id'])['title'] == edit['title']
    assert len(ai['fake'].calls) == 4
    assert [len(call['data']['passages']) for call in ai['fake'].calls] == [5, 5, 5, 2]
    assert all(call['max_output_tokens'] == 4096 for call in ai['fake'].calls)
    assert ai['s'].ai_usage(ai['p']['id'], ai['principal'])['reserved_requests'] == 4
    assert not ai['worker'].run_once()


@pytest.mark.parametrize('response', [ProviderError('incomplete_response'), GenerationResult('{"sections":[', (), Usage(7, 5, 12), 'length')])
def test_truncated_first_batch_has_clear_error_without_partial_json_or_automatic_retry(authoring, response):
    ai = authoring
    ai['fake'].responses = [response]
    run = sections(ai)
    assert ai['worker'].run_once()
    done = read(ai, run)
    assert done['status'] == 'failed' and done['error'] == 'incomplete_response'
    assert done['sections'] == [] and done['result'] is None
    assert done['coverage']['completed_batches'] == 0
    assert not ai['worker'].run_once() and len(ai['fake'].calls) == 1


def test_saved_output_limit_is_preserved_while_new_defaults_increase(authoring):
    ai = authoring
    assert ai['settings']['settings']['max_output_tokens'] == 4096
    changed = ai['admin'].put(ai['url'], json={'version': ai['settings']['version'], 'settings': ai['settings']['settings'] | {'max_output_tokens': 1024}})
    assert changed.status_code == 200
    ai['settings'] = changed.json()
    from feedback.storage.sqlite import SQLiteStorage
    from conftest import reopen_storage
    reopened = reopen_storage(ai['s'])
    assert reopened.ai_settings(ai['p']['id'], ai['principal'])['settings']['max_output_tokens'] == 1024
    run = sections(ai)
    assert ai['worker'].run_once()
    assert read(ai, run)['status'] == 'succeeded'
    assert ai['fake'].calls[0]['max_output_tokens'] == 1024
    assert ai['admin'].get(ai['url']).json()['settings']['max_output_tokens'] == 1024


def test_retry_requires_native_admin_current_run_and_unchanged_authority(authoring):
    ai = authoring
    ai['fake'].responses = [ProviderError('incomplete_response')]
    run = sections(ai)
    assert ai['worker'].run_once()
    done = read(ai, run)
    endpoint = ai['url'] + '/authoring-runs/' + run['id'] + '/retry'
    body = {'version': done['version'], 'client_request_id': str(uuid4())}
    for name in ('anon', 'user', 'trusted', 'manager', 'other'):
        assert ai[name].post(endpoint, json=body).status_code in (401, 403)
    assert ai['admin'].post(endpoint.replace(ai['p']['id'], ai['q']['id']), json=body).status_code == 404
    assert ai['admin'].post(endpoint, json=body | {'version': done['version'] + 1}).status_code == 409
    ai['app'].state.ai_authoring.save_personality(ai['p']['id'], ai['principal'], ai['settings']['version'], 'Changed tone')
    assert ai['admin'].post(endpoint, json=body).status_code == 409
    assert len(ai['fake'].calls) == 1


def test_batch_budget_exhaustion_keeps_completed_work_without_an_extra_paid_call(authoring):
    ai = authoring
    batched_source(ai)
    current = ai['admin'].get(ai['url']).json()
    changed = ai['admin'].put(ai['url'], json={'version': current['version'], 'settings': current['settings'] | {'daily_request_limit': 1}})
    assert changed.status_code == 200
    ai['settings'] = changed.json()
    run = sections(ai)
    assert ai['worker'].run_once()
    done = read(ai, run)
    assert done['status'] == 'partial' and done['error'] == 'budget_exhausted'
    assert done['coverage']['completed_batches'] == 1 and len(done['sections']) == 2
    assert len(ai['fake'].calls) == 1
    assert ai['s'].ai_usage(ai['p']['id'], ai['principal'])['reserved_requests'] == 1


def test_change_during_later_batch_keeps_only_earlier_stale_drafts(authoring):
    ai = authoring
    batched_source(ai)
    original = ai['fake'].generate
    async def changed_during_second(*args, **kwargs):
        result = await original(*args, **kwargs)
        if len(ai['fake'].calls) == 2:
            ai['s'].ai_knowledge_change(ai['p']['id'], ai['principal'], ai['source']['id'], ai['source']['version'], active=False, approved=False)
        return result
    ai['fake'].generate = changed_during_second
    run = sections(ai)
    assert ai['worker'].run_once()
    done = read(ai, run)
    assert done['status'] == 'cancelled' and done['stale']
    assert done['coverage']['completed_batches'] == 1 and len(done['sections']) == 2
    assert all(section['stale'] for section in done['sections'])
    assert len(ai['fake'].calls) == 2


def test_oversized_batch_result_is_rejected_without_saving_any_drafts(authoring):
    ai = authoring
    value = {'kind': 'section', 'title': 'Original facts', 'body': 'Documented installation procedure.', 'category': 'Setup', 'citation_ids': ['S1']}
    ai['fake'].responses = [GenerationResult(json.dumps({'sections': [value] * 4}), (), Usage(7, 5, 12), 'stop')]
    run = sections(ai)
    assert ai['worker'].run_once()
    done = read(ai, run)
    assert done['status'] == 'failed' and done['error'] == 'invalid_response'
    assert done['sections'] == [] and done['coverage']['completed_batches'] == 0


def test_all_four_batches_can_retain_maximum_valid_unicode_drafts(authoring):
    ai = authoring
    batched_source(ai, 20)
    value = {'kind': 'section', 'title': 'Support', 'body': '🙂' * 4000, 'category': 'Support', 'citation_ids': ['S1']}
    response = GenerationResult(json.dumps({'sections': [value] * 3}, ensure_ascii=False), (), Usage(7, 5, 12), 'stop')
    ai['fake'].responses = [response] * 4
    run = sections(ai)
    assert ai['worker'].run_once()
    done = read(ai, run)
    assert done['status'] == 'succeeded' and len(done['sections']) == 12
    assert done['coverage']['completed_passages'] == 20


def test_checkpoint_survives_expired_worker_lease_and_rejects_duplicate_or_wrong_batch(authoring, tmp_path):
    ai = authoring
    batched_source(ai)
    run = sections(ai)
    job = ai['s'].ai_claim_job(time.time(), kinds=('sections',))
    material = ai['s'].ai_job_material(job['id'], job['lease_token'])
    ids = [row['id'] for row in material['source_passages']]
    value = {'kind': 'section', 'title': 'Saved batch', 'body': 'Original facts.', 'category': 'Support', 'citation_ids': [ids[0]]}
    result = {'sections': [value], 'knowledge_version': run['knowledge_version']}
    wrong = result | {'sections': [value | {'citation_ids': [ids[7]]}]}
    with pytest.raises(Error):
        ai['s'].ai_authoring_finish(job['id'], job['lease_token'], wrong, [ids[7]], batch_index=0)
    assert ai['s'].ai_authoring_finish(job['id'], job['lease_token'], result, [ids[0]], batch_index=0)
    with pytest.raises(Error):
        ai['s'].ai_authoring_finish(job['id'], job['lease_token'], result, [ids[0]], batch_index=0)
    from feedback.maintenance import backup
    from feedback.storage.sqlite import SQLiteStorage
    saved = tmp_path / 'batch-backup'
    backup(ai['cfg'], saved)
    restored = SQLiteStorage(saved / 'feedback.sqlite', ai['cfg'].base_url)
    checkpoint = restored.ai_authoring_run(ai['p']['id'], ai['principal'], run['id'])
    assert checkpoint['coverage']['completed_batches'] == 1
    assert len(checkpoint['sections']) == 1 and checkpoint['result'] == result
    with ai['s'].transaction(True) as c:
        c.execute('UPDATE ai_jobs SET lease_until=? WHERE id=?', (time.time() - 1, job['id']))
    assert ai['worker'].run_once()
    done = read(ai, run)
    assert done['status'] == 'succeeded' and len(done['sections']) == 5
    assert len(ai['fake'].calls) == 2 and [len(call['data']['passages']) for call in ai['fake'].calls] == [5, 2]
    assert not ai['s'].ai_authoring_finish(job['id'], job['lease_token'], result, [ids[0]], batch_index=0)


def test_explicit_preview_is_durable_idempotent_and_does_not_enable_ai(authoring):
    ai = authoring
    run, body = preview(ai)
    assert run['status'] == 'queued' and run['result'] is None and run['citations'] == []
    assert ai['fake'].calls == []
    again = ai['admin'].post(ai['url'] + '/previews', json=body)
    assert again.json() == run
    assert ai['admin'].post(ai['url'] + '/previews', json=body | {'question': 'Different question'}).status_code == 409
    assert ai['worker'].run_once()
    done = read(ai, run)
    assert done['status'] == 'succeeded' and done['result']['status'] == 'answered'
    assert done['result']['body'] == 'Restart after saving the configuration.'
    assert done['citations'][0]['id'] == done['result']['citation_ids'][0]
    assert done['citations'][0]['source_id'] == ai['source']['id'] and done['citations'][0]['url'] is None
    assert 'configuration' in done['citations'][0]['excerpt']
    assert len(ai['fake'].calls) == 2 and ai['fake'].calls[0]['tools'] == ()
    assert ai['fake'].calls[0]['model'] == 'fixture-model'
    current = ai['admin'].get(ai['url']).json()
    assert not current['settings']['enabled'] and not any(current['settings']['tools'].values())
    usage = ai['s'].ai_usage(ai['p']['id'], ai['principal'])
    assert usage['reserved_requests'] == 2 and usage['reported_input_tokens'] == 14 and usage['reported_output_tokens'] == 10
    assert all(key not in json.dumps(done) for key in ('fixture-provider-secret', 'lease_token', 'session_id', 'protected_key'))
    with ai['s'].transaction() as c:
        assert c.execute('SELECT count(*) FROM chat_messages').fetchone()[0] == 0
    assert ai['admin'].post(ai['url'] + '/previews', json=body).json()['result'] == done['result']
    assert not ai['worker'].run_once()


@pytest.mark.parametrize('client', ['anon', 'user', 'trusted', 'manager', 'other'])
def test_authoring_and_personality_require_native_admin(authoring, client):
    ai = authoring
    run, _ = preview(ai)
    responses = [ai[client].get(ai['url'] + '/personality'),
        ai[client].put(ai['url'] + '/personality', json={'version': ai['settings']['version'], 'content': 'Be helpful.'}),
        ai[client].post(ai['url'] + '/previews', json={'client_request_id': str(uuid4()), 'version': ai['settings']['version'], 'question': 'Question?'}),
        ai[client].get(ai['url'] + '/authoring-runs'), ai[client].get(ai['url'] + '/authoring-runs/' + run['id']),
        ai[client].post(ai['url'] + '/authoring-runs/' + run['id'] + '/cancel', json={'version': run['version']})]
    assert all(response.status_code in (401, 403) for response in responses)
    assert ai['fake'].calls == []


def test_csrf_chat_token_and_project_scope(authoring):
    ai = authoring
    run, _ = preview(ai)
    assert ai['admin'].put(ai['url'] + '/personality', json={'version': ai['settings']['version'], 'content': 'Be helpful.'}, headers={'X-CSRF-Token': 'wrong'}).status_code == 403
    assert ai['admin'].get(ai['url'] + '/authoring-runs', headers={'X-Chat-Token': 'not-an-admin'}).status_code == 400
    foreign = ai['url'].replace(ai['p']['id'], ai['q']['id']) + '/authoring-runs/' + run['id']
    assert ai['admin'].get(foreign).status_code == 404
    assert ai['admin'].post(foreign + '/cancel', json={'version': run['version']}).status_code == 404


def test_sections_remain_editable_derived_material_with_original_citations(authoring):
    ai = authoring
    before = ai['s'].ai_knowledge_chunks(ai['p']['id'], ai['principal'])
    run = sections(ai)
    assert ai['worker'].run_once()
    done = read(ai, run)
    assert done['status'] == 'succeeded' and len(done['sections']) == 2
    assert done['coverage'] == {'scope':'all', 'page_ids':None, 'selected_passages': len(before), 'total_passages': len(before), 'batch_size': 5, 'total_batches': 1, 'completed_batches': 1, 'completed_passages': len(before)}
    draft = done['sections'][0]
    assert draft['citations'][0]['id'] == before[0]['id'] and not draft['stale']
    url = ai['url'] + '/knowledge/sections/' + draft['id']
    edit = {'version': draft['version'], 'title': 'Revised installation', 'body': 'An administrator-reviewed internal summary.', 'category': 'Support', 'citation_ids': draft['citation_ids']}
    changed = ai['admin'].put(url, json=edit)
    assert changed.status_code == 200, changed.text
    assert changed.json()['version'] == draft['version'] + 1 and changed.json()['body'] == edit['body']
    assert ai['admin'].put(url, json=edit).status_code == 409
    assert ai['s'].ai_knowledge_chunks(ai['p']['id'], ai['principal']) == before
    assert read(ai, run)['result']['sections'][0]['body'] != edit['body']  # Immutable generated result survives draft edits.
    removed = ai['admin'].delete(url, params={'version': changed.json()['version']})
    assert removed.status_code == 200 and removed.json()['deleted_at']
    assert ai['s'].ai_knowledge_chunks(ai['p']['id'], ai['principal']) == before
    assert len(ai['fake'].calls) == 1


def test_personality_is_bounded_versioned_and_invalidates_queued_generation(authoring):
    ai = authoring
    run, _ = preview(ai)
    url = ai['url'] + '/personality'
    current = ai['admin'].get(url).json()
    assert current == {'version': ai['settings']['version'], 'filename': 'soul.md', 'content': ''}
    changed = ai['admin'].put(url, json={'version': current['version'], 'filename': 'soul.md', 'content': '# Personality\nBe concise and kind.'})
    assert changed.status_code == 200 and changed.json()['version'] > current['version']
    assert ai['admin'].put(url, json={'version': current['version'], 'content': 'Stale'}).status_code == 409
    assert not ai['worker'].run_once() and ai['fake'].calls == []
    assert read(ai, run)['status'] == 'cancelled'
    settings = ai['admin'].get(ai['url']).json()
    assert not settings['settings']['enabled'] and not any(settings['settings']['tools'].values())


@pytest.mark.parametrize('content', ['x' * 20001, '\ud800', 'NUL\x00text'])
def test_personality_rejects_oversized_or_invalid_text(authoring, content):
    ai = authoring
    response = ai['admin'].put(ai['url'] + '/personality', content=json.dumps({'version': ai['settings']['version'], 'content': content}), headers={'Content-Type': 'application/json'})
    assert response.status_code == 400
    assert ai['admin'].get(ai['url'] + '/personality').json()['content'] == ''
    assert ai['fake'].calls == []


def test_personality_accepts_complete_escaped_unicode_within_bound(authoring):
    ai = authoring
    content = '🙂' * 20000
    response = ai['admin'].put(ai['url'] + '/personality', content=json.dumps({'version': ai['settings']['version'], 'content': content}), headers={'Content-Type': 'application/json'})
    assert response.status_code == 200, response.text
    assert response.json()['content'] == content


def test_cancelled_preview_never_calls_provider_or_finishes_stale_lease(authoring):
    ai = authoring
    run, _ = preview(ai)
    job = ai['s'].ai_claim_job(time.time(), kinds=('preview',))
    cancelled = ai['admin'].post(ai['url'] + '/authoring-runs/' + run['id'] + '/cancel', json={'version': run['version']})
    assert cancelled.status_code == 200 and cancelled.json()['status'] == 'cancelled'
    assert not ai['s'].ai_authoring_finish(job['id'], job['lease_token'], {'body': 'Stale', 'citation_ids': [], 'knowledge_version': run['knowledge_version'], 'status': 'insufficient'}, [])
    assert not ai['worker'].run_once() and ai['fake'].calls == []


@pytest.mark.parametrize('change', ['personality', 'source', 'session'])
def test_change_during_provider_call_prevents_result_publication(authoring, change):
    ai = authoring
    run, _ = preview(ai)
    if change == 'personality':
        ai['fake'].hook = lambda: ai['app'].state.ai_authoring.save_personality(ai['p']['id'], ai['principal'], ai['settings']['version'], 'New tone')
    elif change == 'source':
        ai['fake'].hook = lambda: ai['s'].ai_knowledge_change(ai['p']['id'], ai['principal'], ai['source']['id'], ai['source']['version'], active=False, approved=False)
    else:
        ai['fake'].hook = lambda: ai['s'].revoke_session(ai['principal']['session_id'])
    assert ai['worker'].run_once()
    with ai['s'].transaction() as c:
        row = c.execute('SELECT result FROM ai_authoring_runs WHERE id=?', (run['id'],)).fetchone()
        assert row['result'] is None
    if change != 'session':
        assert read(ai, run)['status'] == 'cancelled'
    assert len(ai['fake'].calls) == 1


def test_budget_reservation_refuses_provider_call(authoring):
    ai = authoring
    current = ai['admin'].get(ai['url']).json()
    changed = ai['admin'].put(ai['url'], json={'version': current['version'], 'settings': current['settings'] | {'daily_token_limit': 1}})
    assert changed.status_code == 200
    run, _ = preview(ai, version=changed.json()['version'])
    assert ai['worker'].run_once()
    done = read(ai, run)
    assert done['status'] == 'failed' and done['error'] == 'budget_exhausted'
    assert ai['fake'].calls == []


def test_retry_is_durable_and_does_not_refund_uncertain_usage(authoring, monkeypatch):
    ai = authoring
    run, _ = preview(ai)
    ai['fake'].responses = [ProviderError('timeout', retryable=True)]
    assert ai['worker'].run_once()
    assert read(ai, run)['status'] == 'queued'
    queued = next(job for job in ai['s'].ai_jobs(ai['p']['id'], ai['principal']) if job['kind'] == 'preview')
    key = ai['s'].ai_provider_keys(ai['p']['id'], ai['principal'])['keys'][0]
    assert queued['next_attempt'] >= key['retry_at'] > time.time()
    assert not ai['worker'].run_once() and len(ai['fake'].calls) == 1
    # Advance the clock to the persisted recovery time, without rewriting the
    # queue or bypassing the account's durable cooldown.
    monkeypatch.setattr(time, 'time', lambda: queued['next_attempt'] + 0.001)
    assert ai['worker'].run_once()
    assert read(ai, run)['status'] == 'succeeded' and len(ai['fake'].calls) == 3
    assert ai['s'].ai_provider_keys(ai['p']['id'], ai['principal'])['keys'][0]['status'] == 'ready' 
    usage = ai['s'].ai_usage(ai['p']['id'], ai['principal'])
    assert usage['reserved_requests'] == 3 and usage['reported_input_tokens'] == 14


@pytest.mark.parametrize('failure', ['tool', 'foreign_id', 'url', 'raw_exception'])
def test_model_outputs_cannot_run_tools_invent_citations_or_leak_errors(authoring, failure):
    ai = authoring
    run, _ = preview(ai)
    if failure == 'raw_exception':
        value = RuntimeError('private prompt or upstream error')
    elif failure == 'tool':
        value = GenerationResult('', (ToolCall('call', 'create_bug_report', {'title': 'Unrequested action'}),), Usage(7, 5), 'tool_calls')
    else:
        content = {'status': 'answered', 'body': 'https://malicious.example' if failure == 'url' else 'Answer', 'citation_ids': [str(uuid4())]}
        value = GenerationResult(json.dumps(content), (), Usage(7, 5), 'stop')
    ai['fake'].responses = [value]
    assert ai['worker'].run_once()
    done = read(ai, run)
    assert done['status'] == 'failed' and done['result'] is None
    assert 'private prompt' not in json.dumps(done)
    with ai['s'].transaction() as c:
        assert c.execute('SELECT count(*) FROM requests').fetchone()[0] == 0


def test_results_and_sections_show_staleness_after_original_removed(authoring):
    ai = authoring
    run = sections(ai)
    assert ai['worker'].run_once()
    current = read(ai, run)
    assert not current['stale']
    ai['s'].ai_knowledge_change(ai['p']['id'], ai['principal'], ai['source']['id'], ai['source']['version'], active=False, approved=False)
    stale = read(ai, run)
    assert stale['stale'] and all(section['stale'] for section in stale['sections'])
    assert stale['citations'] and stale['citations'][0]['source_id'] == ai['source']['id']


def test_unverified_model_cannot_queue_paid_work(workflow):
    ai = workflow
    response = ai['admin'].post('/api/v1/projects/' + ai['p']['id'] + '/ai/previews', json={
        'client_request_id': str(uuid4()), 'version': 0, 'question': 'Question?'})
    assert response.status_code == 409


def test_section_edits_and_removal_have_independent_admin_scope(authoring):
    ai = authoring
    run = sections(ai)
    assert ai['worker'].run_once()
    draft = read(ai, run)['sections'][0]
    url = ai['url'] + '/knowledge/sections/' + draft['id']
    data = {key: draft[key] for key in ('version', 'title', 'body', 'category', 'citation_ids')}
    for client in ('anon', 'user', 'trusted', 'manager', 'other'):
        assert ai[client].put(url, json=data).status_code in (401, 403)
        assert ai[client].delete(url, params={'version': draft['version']}).status_code in (401, 403)
    foreign = url.replace(ai['p']['id'], ai['q']['id'])
    assert ai['admin'].put(foreign, json=data).status_code == 404
    assert ai['admin'].delete(foreign, params={'version': draft['version']}).status_code == 404
    ai['s'].ai_knowledge_change(ai['p']['id'], ai['principal'], ai['source']['id'], ai['source']['version'], active=False, approved=False)
    assert ai['admin'].put(url, json=data).status_code == 409
    # Stale drafts can still be removed; deletion does not require a model or active source.
    assert ai['admin'].delete(url, params={'version': draft['version']}).status_code == 200


def test_run_citations_preserve_generated_provenance_after_draft_edit(authoring):
    ai = authoring
    s, p, pid = ai['s'], ai['principal'], ai['p']['id']
    s.ai_knowledge_import(pid, p, str(uuid4()), 'guide.md', 'Support guide', '# First\n\nOriginal first passage.\n\n# Second\n\nOriginal second passage.', source_id=ai['source']['id'], version=ai['source']['version'])
    assert IngestionWorker(s, Embeddings()).run_once()
    ai['source'] = s.ai_knowledge_source(pid, p, ai['source']['id'])
    run = sections(ai)
    assert ai['worker'].run_once()
    done = read(ai, run)
    original_ids = set(done['result']['sections'][0]['citation_ids'])
    other = next(row['id'] for row in s.ai_knowledge_chunks(pid, p) if row['id'] not in original_ids)
    for draft in done['sections']:
        data = {key: draft[key] for key in ('version', 'title', 'body', 'category')}
        response = ai['admin'].put(ai['url'] + '/knowledge/sections/' + draft['id'], json=data | {'citation_ids': [other]})
        assert response.status_code == 200, response.text
        assert response.json()['citations'][0]['id'] == other
    changed = read(ai, run)
    assert {row['id'] for row in changed['citations']} == original_ids
    assert all(row['citation_ids'] == [other] for row in changed['sections'])


def test_sections_cover_entire_source_through_durable_job_checkpoints(authoring):
    ai = authoring
    s, p, pid = ai['s'], ai['principal'], ai['p']['id']
    content = '\n\n'.join(f'# Topic {i}\n\nOriginal support facts for topic {i}.' for i in range(25))
    s.ai_knowledge_import(pid, p, str(uuid4()), 'guide.md', 'Support guide', content, source_id=ai['source']['id'], version=ai['source']['version'])
    assert IngestionWorker(s, Embeddings()).run_once()
    ai['source'] = s.ai_knowledge_source(pid, p, ai['source']['id'])
    run = sections(ai)
    assert run['coverage'] == {'scope':'all', 'page_ids':None, 'selected_passages': 25, 'total_passages': 25, 'batch_size': 5, 'total_batches': 5, 'completed_batches': 0, 'completed_passages': 0}
    assert ai['worker'].run_once()
    assert len(ai['fake'].calls) == 4 and all(len(call['data']['passages']) == 5 for call in ai['fake'].calls)
    assert read(ai, run)['coverage'] == run['coverage'] | {'completed_batches': 4, 'completed_passages': 20}
    assert read(ai,run)['status']=='queued'
    assert ai['worker'].run_once()
    assert read(ai,run)['status']=='succeeded', read(ai,run)
    assert read(ai,run)['coverage']['completed_passages']==25
    assert len(ai['fake'].calls)==5


def test_shutdown_cancels_local_provider_wait_without_publishing_or_refund(authoring):
    ai = authoring
    run, _ = preview(ai)
    started, cancelled = threading.Event(), threading.Event()
    class WaitingProvider:
        async def generate(self, *args, **kwargs):
            started.set()
            try:
                await asyncio.sleep(30)
            finally:
                cancelled.set()
    ai['app'].state.ai_generation.provider_factory = lambda name: WaitingProvider()
    thread = threading.Thread(target=ai['worker'].run_once)
    thread.start()
    assert started.wait(3)
    ai['worker'].stop()
    thread.join(3)
    assert not thread.is_alive() and cancelled.is_set()
    result = read(ai, run)
    assert result['result'] is None and result['status'] == 'running'
    assert ai['s'].ai_usage(ai['p']['id'], ai['principal'])['reserved_requests'] == 1


def test_storage_refuses_oversized_section_citation_set_atomically(authoring):
    ai = authoring
    s, p, pid = ai['s'], ai['principal'], ai['p']['id']
    content = '\n\n'.join(f'# Topic {i}\n\nOriginal support facts for topic {i}.' for i in range(21))
    s.ai_knowledge_import(pid, p, str(uuid4()), 'guide.md', 'Support guide', content, source_id=ai['source']['id'], version=ai['source']['version'])
    assert IngestionWorker(s, Embeddings()).run_once()
    ai['source'] = s.ai_knowledge_source(pid, p, ai['source']['id'])
    run = sections(ai)
    job = s.ai_claim_job(time.time(), kinds=('sections',))
    ids = [row['id'] for row in s.ai_knowledge_chunks(pid, p, limit=100)]
    assert len(ids) == 21
    value = {'kind': 'section', 'title': 'Topics', 'body': 'Review the original support topics.', 'category': 'Support', 'citation_ids': ids}
    with pytest.raises(Error) as error:
        s.ai_authoring_finish(job['id'], job['lease_token'], {'sections': [value], 'knowledge_version': run['knowledge_version']}, ids, batch_index=0)
    assert error.value.status == 400
    rejected = read(ai, run)
    assert rejected['result'] is None and rejected['sections'] == [] and rejected['status'] == 'running'
    value['citation_ids'] = ids[:5]
    assert s.ai_authoring_finish(job['id'], job['lease_token'], {'sections': [value], 'knowledge_version': run['knowledge_version']}, ids[:5], batch_index=0)
    assert len(read(ai, run)['sections']) == 1


def test_reviewed_draft_approval_is_explicit_idempotent_and_indexed(authoring):
    ai=authoring
    run=sections(ai)
    assert ai['worker'].run_once()
    draft=read(ai,run)['sections'][0]
    url=ai['url']+'/knowledge/sections/'+draft['id']+'/approve'
    before=ai['s'].ai_knowledge_chunks(ai['p']['id'],ai['principal'])
    for role in ('anon','user','trusted','manager','other'):
        assert ai[role].post(url,json={'version':draft['version']}).status_code in (401,403)
    response=ai['admin'].post(url,json={'version':draft['version']})
    assert response.status_code==200,response.text
    source=response.json()
    assert source['approved'] and source['active'] and not source['index_ready']
    assert ai['admin'].post(url,json={'version':draft['version']}).json()['id']==source['id']
    assert ai['s'].ai_knowledge_chunks(ai['p']['id'],ai['principal'])==before
    assert IngestionWorker(ai['s'],Embeddings()).run_once()
    assert len(ai['s'].ai_knowledge_chunks(ai['p']['id'],ai['principal']))>len(before)
    assert read(ai,run)['sections'][0]['published_source_id']==source['id']
    library=ai['admin'].get(ai['url']+'/knowledge/library').json()
    assert len(library['items'])==2 and all(i['ready'] for i in library['items'])
    item=next(i for i in library['items'] if i['source_id']==source['id'])
    detail=ai['admin'].get(ai['url']+'/knowledge/library/'+item['id'])
    assert detail.status_code==200 and draft['body'] in detail.json()['content']
    # A backup includes approval provenance and its new original, without regenerating.
    from feedback.storage.sqlite import SQLiteStorage
    if isinstance(ai['s'],SQLiteStorage):
        backup=ai['s'].path.with_name('reviewed-backup.sqlite')
        ai['s'].backup(backup)
        restored=SQLiteStorage(backup,ai['cfg'].base_url)
        assert restored.ai_authoring_run(ai['p']['id'],ai['principal'],run['id'])['sections'][0]['published_source_id']==source['id']


def test_draft_edit_survives_unrelated_settings_but_not_removed_evidence(authoring):
    ai=authoring
    run=sections(ai);assert ai['worker'].run_once()
    draft=read(ai,run)['sections'][0]
    settings=ai['s'].ai_settings(ai['p']['id'],ai['principal'])
    ai['s'].ai_configure(ai['p']['id'],ai['principal'],settings['version'],settings['settings']|{'max_output_tokens':8192})
    assert read(ai,run)['stale'] and not read(ai,run)['sections'][0]['stale']
    url=ai['url']+'/knowledge/sections/'+draft['id']
    value={k:draft[k] for k in ('version','title','body','category','citation_ids')}
    value['title']='Reviewed support answer'
    result=ai['admin'].put(url,json=value)
    assert result.status_code==200,result.text
    source=ai['s'].ai_knowledge_source(ai['p']['id'],ai['principal'],ai['source']['id'])
    ai['s'].ai_knowledge_change(ai['p']['id'],ai['principal'],source['id'],source['version'],active=False,approved=True)
    assert ai['admin'].post(url+'/approve',json={'version':result.json()['version']}).status_code==409


def test_library_and_draft_search_are_private_and_project_scoped(authoring):
    ai=authoring
    run=sections(ai);assert ai['worker'].run_once()
    root=ai['url']+'/knowledge'
    for path in ('/library','/sections'):
        for role in ('anon','user','trusted','manager','other'):
            assert ai[role].get(root+path).status_code in (401,403)
    assert len(ai['admin'].get(root+'/library?query=restart').json()['items'])==1
    assert not ai['admin'].get(root+'/library?query=unfindable').json()['items']
    page=ai['admin'].get(root+'/sections?limit=1').json()
    assert page['has_more'] and len(page['items'])==1
    assert len(ai['admin'].get(root+'/sections?query=restart').json()['items'])==1
    item=ai['admin'].get(root+'/library').json()['items'][0]
    other='/api/v1/projects/'+ai['q']['id']+'/ai/knowledge/library/'+item['id']
    assert ai['admin'].get(other).status_code==404


def test_enable_reports_exact_missing_tool_capability(authoring):
    ai=authoring
    saved=ai['admin'].get(ai['url']).json()
    settings=saved['settings']|{'enabled':True,'tools':saved['settings']['tools']|{'search_knowledge':True}}
    response=ai['admin'].put(ai['url'],json={'version':saved['version'],'settings':settings})
    assert response.status_code==409 and 'tool access' in response.json()['error']['message']
    # The existing text-only evidence still authorizes basic grounded answers.
    response=ai['admin'].put(ai['url'],json={'version':saved['version'],'settings':saved['settings']|{'enabled':True}})
    assert response.status_code==200,response.text


def test_website_library_page_selection_and_foreign_page_rejection(authoring):
    ai=authoring
    config={'seed_url':'https://docs.example/docs','approved_origins':['https://docs.example'],'include_paths':['/docs'],'exclude_paths':[],'max_pages':5,'max_depth':2}
    response=ai['admin'].post(ai['url']+'/knowledge/websites',json={'client_import_id':str(uuid4()),'name':'Website guide','config':config})
    assert response.status_code==201,response.text
    source=response.json()['source']
    def runner(config,*,on_page,cancelled):
        for name in ('Install','Backup'):
            on_page({'url':'https://docs.example/docs/'+name.lower(),'title':name,'content':name+' instructions. Save your configuration safely.', 'depth':0,'state':'succeeded','error':None,'byte_count':70})
        return {'complete':True,'error':None}
    ai['app'].state.ai_crawl_worker.runner=runner
    assert ai['app'].state.ai_crawl_worker.run_once()
    assert IngestionWorker(ai['s'],Embeddings()).run_once()
    source=ai['s'].ai_knowledge_source(ai['p']['id'],ai['principal'],source['id'])
    source=ai['s'].ai_knowledge_change(ai['p']['id'],ai['principal'],source['id'],source['version'],active=True,approved=True)
    rows=ai['admin'].get(ai['url']+'/knowledge/library',params={'source_id':source['id']}).json()['items']
    assert len(rows)==2 and all(p['ready'] for p in rows)
    chosen=next(p for p in rows if p['title']=='Backup')
    endpoint=ai['url']+'/knowledge/sources/'+source['id']+'/sections/generate'
    request={'client_request_id':str(uuid4()),'version':ai['settings']['version'],'source_version':source['version'],'page_ids':[chosen['id']]}
    rejected=ai['admin'].post(endpoint,json=request|{'page_ids':[str(uuid4())]})
    assert rejected.status_code==409
    response=ai['admin'].post(endpoint,json=request)
    assert response.status_code==202,response.text
    run=response.json()
    assert run['coverage']['selected_passages']==1 and run['coverage']['total_passages']==2
    ai['fake'].mode='sections';assert ai['worker'].run_once()
    assert read(ai,run)['status']=='succeeded'
    assert ai['fake'].calls[0]['data']['passages'][0]['title']=='Backup'


def test_large_source_keeps_bounded_views_and_all_drafts_searchable(authoring):
    ai=authoring
    settings=ai['s'].ai_settings(ai['p']['id'],ai['principal'])
    ai['settings']=ai['s'].ai_configure(ai['p']['id'],ai['principal'],settings['version'],settings['settings']|{'daily_token_limit':1000000})
    batched_source(ai,251)
    run=sections(ai)
    for _ in range(20):
        if not ai['worker'].run_once():break
    done=read(ai,run)
    assert done['status']=='succeeded',done['error']
    assert done['coverage']['completed_passages']==251
    assert done['coverage']['completed_batches']==51 and len(ai['fake'].calls)==51
    assert len(done['sections'])==100 and done['section_count']==102
    assert len(done['result']['sections'])==100
    page=ai['admin'].get(ai['url']+'/knowledge/sections?offset=100&limit=100').json()
    assert len(page['items'])==2 and not page['has_more']
