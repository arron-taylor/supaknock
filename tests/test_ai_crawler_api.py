"""Website import lifecycle and access boundaries against real SQLite."""
from contextlib import contextmanager
import hashlib
import io
import json
import time
from uuid import uuid4

import pytest

from feedback.ai_crawler import CrawlError, crawl
from feedback.security import digest
from feedback.storage.sqlite import SQLiteStorage


ORIGIN = 'https://docs.example'


def payload(**changes):
    return dict(client_import_id=str(uuid4()), name='Public documentation', config={
        'seed_url': ORIGIN + '/docs', 'approved_origins': [ORIGIN],
        'include_paths': ['/docs'], 'exclude_paths': ['/docs/private'], 'max_pages': 5, 'max_depth': 2}) | changes


@pytest.fixture
def ai(workflow):
    w = workflow
    storage = w['app'].state.storage
    session = storage.session(digest(w['admin'].cookies['feedback_session']))
    return w | {'storage': storage, 'worker': w['app'].state.ai_crawl_worker,
        'principal': {'kind': 'admin', 'user_id': session['id'], 'session_id': session['session_id']},
        'url': '/api/v1/projects/' + w['p']['id'] + '/ai/knowledge'}


def create(ai, data=None):
    response = ai['admin'].post(ai['url'] + '/websites', json=data or payload())
    assert response.status_code == 201, response.text
    return response.json()


def run_url(ai, imported):
    return ai['url'] + '/sources/' + imported['source']['id'] + '/crawls/' + imported['run']['id']


def source_url(ai, imported):
    return ai['url'] + '/sources/' + imported['source']['id']


def page(url=ORIGIN + '/docs', content='Documented support information for this project.'):
    return {'url': url, 'title': 'Support guide', 'content': content, 'status': 'fetched', 'error': None}


def engine_page(outcome):
    return {k: outcome[k] for k in ('url', 'title', 'content', 'error')} | {'depth': 0,
        'state': 'succeeded' if outcome['status'] == 'fetched' else 'failed', 'byte_count': len(outcome['content'].encode())}


def successful_runner(config, *, on_page, cancelled):
    assert not cancelled()
    on_page(engine_page(page()))
    return {'complete': True, 'error': None}


def finish_crawl(ai, imported):
    ai['worker'].runner = successful_runner
    assert ai['worker'].run_once()
    result = ai['admin'].get(run_url(ai, imported))
    assert result.status_code == 200, result.text
    assert result.json()['status'] == 'succeeded', result.text
    return result.json()


def index_current(ai, source_id):
    s, p, pid = ai['storage'], ai['principal'], ai['p']['id']
    job = s.ai_claim_job(time.time(), kinds=('ingest',))
    assert job and job['resource_id'] == source_id
    material = s.ai_job_material(job['id'], job['lease_token'])
    doc = material['document']
    revision = s.ai_revision_stage(pid, p, source_id, job['authority']['source_version'], doc['title'], doc['content'], 1, job_id=job['id'], lease_token=job['lease_token'])
    s.ai_revision_chunks(pid, p, revision['id'], [{'ordinal': i, 'title': x['title'], 'content': x['content'],
        'location': x['url'], 'vector': [1.] + [0.] * 767} for i, x in enumerate(material['pages'])], lease_token=job['lease_token'])
    s.ai_revision_publish(pid, p, revision['id'], lease_token=job['lease_token'])
    return revision


def test_begin_retry_conflict_defaults_no_network_or_implicit_approval(ai):
    data = payload()
    first = create(ai, data)
    assert first == create(ai, data)
    assert first['source']['kind'] == 'website' and first['source']['index_status'] == 'pending'
    assert first['source']['active'] and not first['source']['approved']
    assert first['run']['status'] == 'queued' and first['run']['pages'] == []
    assert first['run']['attempt_count'] == 0 and first['source']['latest_document'] is None
    assert not any(key in json.dumps(first) for key in ('session_id', 'lease_token', 'protected_key'))
    conflict = ai['admin'].post(ai['url'] + '/websites', json=data | {'name': 'Different'})
    assert conflict.status_code == 409
    assert ai['storage'].ai_knowledge_chunks(ai['p']['id'], ai['principal']) == []
    assert not ai['storage'].ai_settings(ai['p']['id'], ai['principal'])['settings']['enabled']


@pytest.mark.parametrize('client', ['anon', 'user', 'trusted', 'manager', 'other'])
def test_native_admin_only_for_all_crawl_routes(ai, client):
    imported = create(ai)
    client = ai[client]
    responses = [client.post(ai['url'] + '/websites', json=payload()),
        client.put(source_url(ai, imported) + '/website', json=payload(version=imported['source']['version'])),
        client.get(source_url(ai, imported) + '/crawls'), client.get(run_url(ai, imported)),
        client.post(run_url(ai, imported) + '/cancel', json={'version': 1})]
    assert all(r.status_code in (401, 403) for r in responses)


def test_csrf_chat_credentials_and_cross_project_scopes(ai):
    imported = create(ai)
    assert ai['admin'].post(ai['url'] + '/websites', json=payload(), headers={'X-CSRF-Token': 'wrong'}).status_code == 403
    assert ai['admin'].get(run_url(ai, imported), headers={'X-Chat-Token': 'not-admin'}).status_code == 400
    foreign = run_url(ai, imported).replace(ai['p']['id'], ai['q']['id'])
    assert ai['admin'].get(foreign).status_code == 404
    assert ai['admin'].post(foreign + '/cancel', json={'version': 1}).status_code == 404
    assert ai['admin'].put(source_url(ai, imported).replace(ai['p']['id'], ai['q']['id']) + '/website', json=payload(version=imported['source']['version'])).status_code == 404


@pytest.mark.parametrize('change', ['private', 'credentials', 'extra', 'bool', 'surrogate'])
def test_invalid_configuration_fails_without_persistent_work(ai, change):
    data = payload()
    if change == 'private': data['config']['seed_url'] = 'http://169.254.169.254/latest'
    elif change == 'credentials': data['config']['seed_url'] = 'https://user:secret@docs.example/docs'
    elif change == 'extra': data['config']['headers'] = {'Authorization': 'private'}
    elif change == 'bool': data['config']['max_pages'] = True
    else: data['name'] = '\ud800'
    response = ai['admin'].post(ai['url'] + '/websites', content=json.dumps(data), headers={'Content-Type': 'application/json'})
    assert response.status_code == 400, response.text
    assert 'secret' not in response.text and 'Authorization' not in response.text
    assert ai['admin'].get(ai['url'] + '/sources').json()['items'] == []


def test_worker_success_preserves_page_originals_and_queues_only_ingestion(ai):
    imported = create(ai)
    result = finish_crawl(ai, imported)
    assert result['version'] == 2 and result['document_id'] and result['finished_at']
    assert result['pages'][0]['content'] == page()['content']
    assert result['pages'][0]['content_hash'] == hashlib.sha256(page()['content'].encode()).hexdigest()
    listing = ai['admin'].get(source_url(ai, imported) + '/crawls').json()
    assert 'pages' not in listing['items'][0] and listing['items'][0]['status'] == 'succeeded'
    source = ai['admin'].get(source_url(ai, imported)).json()
    assert source['latest_document']['id'] == result['document_id'] and source['index_status'] == 'pending'
    assert source['version'] == imported['source']['version'] and source['active_revision_id'] is None
    assert not ai['worker'].run_once()  # It must leave ingestion for its own worker.
    ingest = ai['storage'].ai_claim_job(time.time(), kinds=('ingest',))
    material = ai['storage'].ai_job_material(ingest['id'], ingest['lease_token'])
    assert material['pages'][0]['url'] == page()['url'] and material['pages'][0]['content'] == page()['content']
    assert material['document']['filename'] == 'website.md'


def test_failed_refresh_retains_last_approved_index_and_original(ai):
    imported = create(ai)
    finish_crawl(ai, imported)
    source = ai['admin'].get(source_url(ai, imported)).json()
    approved = ai['admin'].patch(source_url(ai, imported), json={'version': source['version'], 'active': True, 'approved': True})
    assert approved.status_code == 200, approved.text
    revision = index_current(ai, imported['source']['id'])
    source = ai['admin'].get(source_url(ai, imported)).json()
    refresh = ai['admin'].put(source_url(ai, imported) + '/website', json=payload(version=source['version'])).json()
    def failure(config, *, on_page, cancelled):
        on_page(engine_page(page(content='New partial text must not replace the active searchable generation.')))
        on_page(engine_page(page(ORIGIN + '/docs/broken', '') | {'status': 'failed', 'error': 'http_failed'}))
        return {'complete': False, 'error': 'http_failed'}
    ai['worker'].runner = failure
    assert ai['worker'].run_once()
    current = ai['admin'].get(source_url(ai, imported)).json()
    assert current['active_revision_id'] == revision['id'] and current['latest_document'] == source['latest_document']
    assert current['index_status'] == 'failed' and current['index_stale']
    assert ai['storage'].ai_knowledge_chunks(ai['p']['id'], ai['principal'])[0]['content'] == page()['content']
    result = ai['admin'].get(run_url(ai, refresh)).json()
    assert result['status'] == 'failed' and result['document_id'] is None and len(result['pages']) == 2


def test_configured_page_cap_indexes_bounded_content_and_persists_normal_stop(ai):
    from test_ai_crawler import fixture, Response, html
    from test_ai_storage import ready_runtime
    ready_runtime(ai['storage'], ai['principal'])
    data = payload()
    data['config']['max_pages'] = 1
    imported = create(ai, data)
    def bounded(config, *, on_page, cancelled):
        result, seen, pages = fixture({ORIGIN + '/docs': Response(html(links='<a href="/docs/unused">unused</a>'))}, config)
        assert ORIGIN + '/docs/unused' not in seen and ORIGIN + '/sitemap.xml' not in seen
        for outcome in pages:
            on_page(outcome)
        return result
    ai['worker'].runner = bounded
    assert ai['worker'].run_once()
    result = ai['admin'].get(run_url(ai, imported)).json()
    assert result['status'] == 'succeeded' and result['error'] is None
    assert result['stop_reason'] == 'page_limit' and len(result['pages']) == 1
    assert result['document_id'] is not None
    source = ai['admin'].get(source_url(ai, imported)).json()
    assert not source['approved'] and not source['index_ready']
    assert ai['admin'].patch(source_url(ai, imported), json={'version': source['version'], 'active': True, 'approved': True}).status_code == 200
    index_current(ai, imported['source']['id'])
    source = ai['admin'].get(source_url(ai, imported)).json()
    assert source['index_ready'] and source['index_status'] == 'indexed'
    assert len(ai['storage'].ai_knowledge_chunks(ai['p']['id'], ai['principal'])) == 1
    reopened = SQLiteStorage(ai['storage'].path, ai['storage'].base_url)
    assert reopened.ai_crawl_run(ai['p']['id'], ai['principal'], imported['source']['id'], result['id'])['stop_reason'] == 'page_limit'


def test_cancel_fences_record_finish_and_retry_does_not_restart(ai):
    data = payload()
    imported = create(ai, data)
    job = ai['storage'].ai_claim_job(time.time(), kinds=('crawl',))
    assert ai['storage'].ai_crawl_record(job['id'], job['lease_token'], page())
    cancelled = ai['admin'].post(run_url(ai, imported) + '/cancel', json={'version': 1})
    assert cancelled.status_code == 200 and cancelled.json()['status'] == 'cancelled'
    assert not ai['storage'].ai_crawl_record(job['id'], job['lease_token'], page(ORIGIN + '/docs/new'))
    assert not ai['storage'].ai_crawl_finish(job['id'], job['lease_token'], True)
    assert create(ai, data)['run']['status'] == 'cancelled'
    assert not ai['worker'].run_once()
    assert ai['admin'].post(run_url(ai, imported) + '/cancel', json={'version': 1}).status_code == 409
    assert ai['admin'].post(run_url(ai, imported) + '/cancel', json={'version': cancelled.json()['version']}).json() == cancelled.json()


def test_newer_refresh_and_admin_revocation_fence_stale_worker(ai):
    imported = create(ai)
    old = ai['storage'].ai_claim_job(time.time(), kinds=('crawl',))
    replacement = ai['admin'].put(source_url(ai, imported) + '/website', json=payload(version=imported['source']['version']))
    assert replacement.status_code == 200, replacement.text
    assert not ai['storage'].ai_crawl_record(old['id'], old['lease_token'], page())
    assert not ai['storage'].ai_crawl_finish(old['id'], old['lease_token'], True)
    new = ai['storage'].ai_claim_job(time.time(), kinds=('crawl',))
    with ai['storage'].transaction(True) as c:
        c.execute('UPDATE sessions SET revoked_at=? WHERE id=?', ('revoked', ai['principal']['session_id']))
    assert not ai['storage'].ai_crawl_record(new['id'], new['lease_token'], page())
    assert not ai['storage'].ai_crawl_finish(new['id'], new['lease_token'], True)


def test_recovery_does_not_mix_old_attempt_pages_or_accept_old_lease(ai):
    imported = create(ai)
    old = ai['storage'].ai_claim_job(time.time(), kinds=('crawl',))
    assert ai['storage'].ai_crawl_record(old['id'], old['lease_token'], page(ORIGIN + '/docs/old', 'Old abandoned attempt original.'))
    with ai['storage'].transaction(True) as c:
        c.execute('UPDATE ai_jobs SET lease_until=? WHERE id=?', (time.time() - 1, old['id']))
    reopened = SQLiteStorage(ai['storage'].path, ai['storage'].base_url)
    newer = reopened.ai_claim_job(time.time(), kinds=('crawl',))
    assert newer['id'] == old['id'] and newer['attempt_count'] == 2
    assert not reopened.ai_crawl_record(old['id'], old['lease_token'], page())
    assert reopened.ai_crawl_record(newer['id'], newer['lease_token'], page())
    assert reopened.ai_crawl_finish(newer['id'], newer['lease_token'], True)
    result = ai['admin'].get(run_url(ai, imported)).json()
    assert [p['url'] for p in result['pages']] == [ORIGIN + '/docs'] and result['pages'][0]['attempt'] == 2
    assert (result['fetched_count'], result['skipped_count'], result['failed_count']) == (1, 0, 0)
    with reopened.transaction() as c:
        assert c.execute('SELECT count(*) FROM ai_crawl_pages').fetchone()[0] == 2
        assert 'abandoned' not in c.execute('SELECT content FROM ai_documents').fetchone()[0]


def test_worker_whole_crawl_timeout_is_visible_safe_failure(ai):
    imported = create(ai)
    def timeout(config, **kwargs): raise CrawlError('timeout')
    ai['worker'].runner = timeout
    assert ai['worker'].run_once()
    result = ai['admin'].get(run_url(ai, imported)).json()
    assert result['status'] == 'failed' and result['error'] == 'timeout' and result['document_id'] is None


def test_worker_only_claims_crawls_and_fails_closed_on_raw_exception(ai):
    markdown = {'client_import_id': str(uuid4()), 'filename': 'help.md', 'name': 'Help', 'content': '# Original support content'}
    assert ai['admin'].post(ai['url'] + '/markdown', json=markdown).status_code == 201
    imported = create(ai)
    def failure(config, **kwargs): raise RuntimeError('Secret remote response body')
    ai['worker'].runner = failure
    assert ai['worker'].run_once()
    result = ai['admin'].get(run_url(ai, imported)).json()
    assert result['error'] == 'crawl_failed' and 'Secret remote' not in json.dumps(result)
    assert ai['storage'].ai_claim_job(time.time(), kinds=('ingest',))['kind'] == 'ingest'


def test_http_fixture_to_durable_page_bundle_then_ingestion_material(ai):
    imported = create(ai)
    seen = []
    class Response:
        def __init__(self, body=b'', status=200, kind='text/html'):
            self.status, self.body, self.kind = status, io.BytesIO(body), kind
        def getheader(self, name, default=None): return self.kind if name == 'Content-Type' else default
        def read(self, size): return self.body.read(size)
    @contextmanager
    def request(url):
        seen.append(url)
        if url == ORIGIN + '/robots.txt':
            yield Response(b'User-agent: *\nDisallow: /docs/private', kind='text/plain')
        elif url == ORIGIN + '/docs':
            yield Response(b'<title>Guide</title><main><h1>Product support</h1><p>Follow the installation procedure.</p><a href="/docs/setup">Set up</a></main>')
        elif url == ORIGIN + '/docs/setup':
            yield Response(b'<title>Set up</title><main><h2>Configuration</h2><pre>support.enabled = true</pre><p>Restart the service after saving.</p></main>')
        else: yield Response(status=404)
    def runner(config, *, on_page, cancelled):
        return crawl(config, request=request, on_page=on_page, cancelled=cancelled)
    ai['worker'].runner = runner
    assert ai['worker'].run_once()
    result = ai['admin'].get(run_url(ai, imported)).json()
    assert result['status'] == 'succeeded' and len(result['pages']) == 2
    assert '```\nsupport.enabled = true\n```' in result['pages'][1]['content']
    job = ai['storage'].ai_claim_job(time.time(), kinds=('ingest',))
    material = ai['storage'].ai_job_material(job['id'], job['lease_token'])
    assert {page['url'] for page in material['pages']} == {ORIGIN + '/docs', ORIGIN + '/docs/setup'}
    assert seen == [ORIGIN + '/robots.txt', ORIGIN + '/docs', ORIGIN + '/docs/setup', ORIGIN + '/sitemap.xml']


def test_full_queue_rolls_back_new_source_but_allows_own_ingestion_continuation(ai):
    imported = [create(ai) for _ in range(50)]
    response = ai['admin'].post(ai['url'] + '/websites', json=payload())
    assert response.status_code == 429
    with ai['storage'].transaction() as c:
        assert c.execute('SELECT count(*) FROM ai_sources').fetchone()[0] == 50
        assert c.execute('SELECT count(*) FROM ai_crawl_runs').fetchone()[0] == 50
    ai['worker'].runner = successful_runner
    assert ai['worker'].run_once()
    with ai['storage'].transaction() as c:
        assert c.execute("SELECT count(*) FROM ai_jobs WHERE state IN ('queued','running')").fetchone()[0] == 50
        assert c.execute('SELECT count(*) FROM ai_documents').fetchone()[0] == 1
        assert c.execute("SELECT count(*) FROM ai_jobs WHERE kind='ingest'").fetchone()[0] == 1


@pytest.mark.parametrize('change', ['unapprove', 'deactivate', 'delete'])
def test_source_policy_changes_fence_running_crawl(ai, change):
    imported = create(ai)
    if change == 'unapprove':
        source = ai['admin'].patch(source_url(ai, imported), json={
            'version': imported['source']['version'], 'active': True, 'approved': True}).json()
        imported = ai['admin'].put(source_url(ai, imported) + '/website', json=payload(version=source['version'])).json()
    job = ai['storage'].ai_claim_job(time.time(), kinds=('crawl',))
    assert job
    source = ai['admin'].get(source_url(ai, imported)).json()
    if change == 'delete':
        response = ai['admin'].delete(source_url(ai, imported), params={'version': source['version']})
    else:
        response = ai['admin'].patch(source_url(ai, imported), json={
            'version': source['version'], 'active': change != 'deactivate', 'approved': False})
    assert response.status_code == 200, response.text
    assert not ai['storage'].ai_crawl_record(job['id'], job['lease_token'], page())
    assert not ai['storage'].ai_crawl_finish(job['id'], job['lease_token'], True)
    assert ai['admin'].get(run_url(ai, imported)).json()['status'] == 'cancelled'


@pytest.mark.parametrize('page_cap', [2, 5])
def test_mixed_crawl_publishes_only_usable_pages_with_persisted_counts(ai, page_cap):
    from test_ai_crawler import fixture, Response, html
    imported_data = payload()
    imported_data['config']['max_pages'] = page_cap
    imported = create(ai, imported_data)
    def mixed(config, *, on_page, cancelled):
        result, _, pages = fixture({ORIGIN + '/docs': Response(html(links='<a href="/docs/login">Login</a>')),
            ORIGIN + '/docs/login': Response(b'<input type="password"><p>Private account login.</p>')}, config)
        for outcome in pages: on_page(outcome)
        return result
    ai['worker'].runner = mixed
    assert ai['worker'].run_once()
    detail = ai['admin'].get(run_url(ai, imported)).json()
    assert detail['status'] == 'succeeded' and detail['error'] is None
    assert (detail['fetched_count'], detail['skipped_count'], detail['failed_count']) == (1, 1, 0)
    assert detail['stop_reason'] == ('page_limit' if page_cap == 2 else None)
    skipped = next(p for p in detail['pages'] if p['status'] == 'skipped')
    assert skipped['content'] == '' and skipped['error'] == 'login_page'
    listing = ai['admin'].get(source_url(ai, imported) + '/crawls').json()['items'][0]
    assert all(listing[key] == detail[key] for key in ('fetched_count', 'skipped_count', 'failed_count'))
    source = ai['admin'].get(source_url(ai, imported)).json()
    assert not source['approved'] and source['latest_document']['id'] == detail['document_id']
    # Reopen the database before reading the ingestion continuation.
    reopened = SQLiteStorage(ai['storage'].path, ai['storage'].base_url)
    job = reopened.ai_claim_job(time.time(), kinds=('ingest',))
    material = reopened.ai_job_material(job['id'], job['lease_token'])
    assert len(material['pages']) == 1 and material['pages'][0]['url'] == ORIGIN + '/docs'
    assert 'Private account login' not in material['document']['content']


@pytest.mark.parametrize('fatal', [None, 'byte_limit', 'total_text_limit', 'timeout'])
def test_empty_or_aborted_refresh_preserves_previous_original_and_index(ai, fatal):
    imported = create(ai)
    finish_crawl(ai, imported)
    source = ai['admin'].get(source_url(ai, imported)).json()
    assert ai['admin'].patch(source_url(ai, imported), json={'version': source['version'], 'active': True, 'approved': True}).status_code == 200
    revision = index_current(ai, imported['source']['id'])
    source = ai['admin'].get(source_url(ai, imported)).json()
    refresh = ai['admin'].put(source_url(ai, imported) + '/website', json=payload(version=source['version'])).json()
    def unusable(config, *, on_page, cancelled):
        if fatal:
            on_page(engine_page(page(content='Partial new content from an interrupted refresh.')))
            raise CrawlError(fatal)
        on_page({'url': ORIGIN + '/docs', 'title': '', 'content': '', 'state': 'skipped', 'error': 'empty_page'})
        return {'complete': False, 'error': 'empty_page'}
    ai['worker'].runner = unusable
    assert ai['worker'].run_once()
    current = ai['admin'].get(source_url(ai, imported)).json()
    assert current['active_revision_id'] == revision['id'] and current['latest_document'] == source['latest_document']
    assert ai['storage'].ai_knowledge_chunks(ai['p']['id'], ai['principal'])[0]['content'] == page()['content']
    result = ai['admin'].get(run_url(ai, refresh)).json()
    assert result['status'] == 'failed' and result['document_id'] is None
    assert result['error'] == (fatal or 'empty_page')
    assert result['skipped_count'] == (0 if fatal else 1)
    assert ai['storage'].ai_claim_job(time.time(), kinds=('ingest',)) is None
