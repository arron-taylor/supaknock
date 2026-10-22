"""Private Markdown import APIs and real SQLite original/index lifecycle."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import time
from uuid import uuid4

import pytest

from feedback.errors import Error
from feedback.storage.sqlite import SQLiteStorage
from conftest import reopen_storage


def prefix(w, project=None):
    return '/api/v1/projects/' + (project or w['p'])['id'] + '/ai/knowledge'


def body(content='# Help\n\nUse the documented procedure.\n', **changes):
    return dict(client_import_id=str(uuid4()), filename='guide.md', name='Support guide', content=content) | changes


def create(w, data=None):
    response = w['admin'].post(prefix(w) + '/markdown', json=data or body())
    assert response.status_code == 201, response.text
    return response.json()


def principal(w):
    s = w['app'].state.storage
    with s.transaction() as c:
        row = c.execute("SELECT s.id,s.user_id FROM sessions s JOIN users u ON u.id=s.user_id WHERE u.role='admin' AND s.revoked_at IS NULL LIMIT 1").fetchone()
    return {'kind': 'admin', 'session_id': row['id'], 'user_id': row['user_id']}


def index_one(w):
    s = w['app'].state.storage
    p = principal(w)
    job = s.ai_claim_job(time.time())
    assert job and job['kind'] == 'ingest'
    material = s.ai_job_material(job['id'], job['lease_token'])
    doc = material['document']
    revision = s.ai_revision_stage(job['project_id'], p, job['resource_id'], job['authority']['source_version'], doc['title'], doc['content'], 1, job_id=job['id'], lease_token=job['lease_token'])
    s.ai_revision_chunks(job['project_id'], p, revision['id'], [{'ordinal': 0, 'title': doc['title'], 'content': doc['content'], 'location': None, 'vector': [1.0] + [0.0] * 767}], lease_token=job['lease_token'])
    s.ai_revision_publish(job['project_id'], p, revision['id'], lease_token=job['lease_token'])
    return job, revision


def test_import_preserves_exact_original_and_pending_state_without_runtime(workflow):
    w = workflow
    original = '# Héllo\r\n\r\n```python\r\nprint("hi")\r\n```\r\n<script>untrusted()</script>\n'
    imported = create(w, body(original))
    source, document = imported['source'], imported['document']
    assert source['active'] and not source['approved'] and source['index_status'] == 'pending'
    assert source['active_revision_id'] is None and source['job']['state'] == 'queued'
    assert document['content_hash'] == hashlib.sha256(original.encode()).hexdigest()
    assert document['byte_count'] == len(original.encode())
    assert 'content' not in document
    url = prefix(w) + '/sources/' + source['id'] + '/documents/' + document['id']
    response = w['admin'].get(url)
    assert response.status_code == 200 and response.json()['content'] == original
    assert response.headers['content-type'].startswith('application/json')
    s = w['app'].state.storage
    assert s.ai_runtime(principal(w))['state'] == 'not_downloaded'
    with s.transaction() as c:
        assert c.execute('SELECT count(*) FROM ai_chunks').fetchone()[0] == 0
        assert c.execute('SELECT count(*) FROM ai_revisions').fetchone()[0] == 0
        job = c.execute('SELECT payload FROM ai_jobs').fetchone()[0]
        assert json.loads(job) == {'document_id': document['id']}
    assert s.ai_knowledge_chunks(w['p']['id'], principal(w)) == []


def test_import_exact_retry_and_conflict(workflow):
    w = workflow
    data = body()
    first = create(w, data)
    again = create(w, data)
    assert first == again
    changed = w['admin'].post(prefix(w) + '/markdown', json=data | {'content': 'Different original'})
    assert changed.status_code == 409
    with w['app'].state.storage.transaction() as c:
        assert c.execute('SELECT count(*) FROM ai_documents').fetchone()[0] == 1
        assert c.execute('SELECT count(*) FROM ai_jobs').fetchone()[0] == 1
        assert c.execute('SELECT count(*) FROM ai_sources').fetchone()[0] == 1


def test_parallel_import_retry_creates_one_original_and_job(workflow):
    w = workflow
    s, p, data = w['app'].state.storage, principal(w), body()
    def run(_):
        handle = reopen_storage(s)
        return handle.ai_knowledge_import(w['p']['id'], p, data['client_import_id'], data['filename'], data['name'], data['content'])
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(run, range(8)))
    assert len({x['document']['id'] for x in results}) == 1
    assert len({x['source']['job']['id'] for x in results}) == 1


def test_native_admin_csrf_and_project_isolation(workflow):
    w = workflow
    imported = create(w)
    source, document = imported['source'], imported['document']
    for client in (w['anon'], w['user'], w['trusted'], w['manager'], w['other']):
        assert client.post(prefix(w) + '/markdown', json=body()).status_code == 403
        assert client.get(prefix(w) + '/sources').status_code == 403
    assert w['admin'].post(prefix(w) + '/markdown', json=body(), headers={'X-CSRF-Token': 'wrong'}).status_code == 403
    assert w['admin'].get(prefix(w) + '/sources', headers={'X-Chat-Token': 'cv_not-an-admin'}).status_code == 400
    foreign = prefix(w, w['q']) + '/sources/' + source['id']
    assert w['admin'].get(foreign).status_code == 404
    assert w['admin'].get(foreign + '/documents/' + document['id']).status_code == 404
    assert w['admin'].put(foreign + '/markdown', json=body(version=source['version'])).status_code == 404


@pytest.mark.parametrize('changes', [
    {'filename': '../guide.md'}, {'filename': 'C:\\guide.md'}, {'filename': 'guide.pdf'},
    {'content': ' \r\n\t'}, {'content': '\x00binary'}, {'content': '\ud800'},
    {'filename': '\ud800.md'}, {'name': '\ud800'}, {'name': '   '},
])
def test_invalid_markdown_is_rejected_without_partial_source(workflow, changes):
    w = workflow
    # Explicit JSON escaping permits a malformed Unicode fixture through transport.
    response = w['admin'].post(prefix(w) + '/markdown', content=json.dumps(body(**changes)), headers={'Content-Type': 'application/json'})
    assert response.status_code == 400, response.text
    assert w['admin'].get(prefix(w) + '/sources').json()['items'] == []


def test_utf8_byte_limit_and_json_transport_bound(workflow):
    w = workflow
    response = w['admin'].post(prefix(w) + '/markdown', json=body('é' * 32769))
    assert response.status_code == 413 and response.json()['error']['code'] == 'document_too_large'
    # Escaping every code point takes more than96KiB, but the original is exactly64KiB.
    response = w['admin'].post(prefix(w) + '/markdown', content=json.dumps(body('é' * 32768)), headers={'Content-Type': 'application/json'})
    assert response.status_code == 201, response.text
    assert response.json()['document']['byte_count'] == 65536
    huge = w['admin'].post(prefix(w) + '/markdown', content=b'x' * (400 * 1024 + 1), headers={'Content-Type': 'application/json'})
    assert huge.status_code == 413 and huge.json()['error']['code'] == 'body_too_large'
    ordinary = w['admin'].patch(prefix(w) + '/sources/' + str(uuid4()), content=b'x' * 98305, headers={'Content-Type': 'application/json'})
    assert ordinary.status_code == 413 and ordinary.json()['error']['code'] == 'body_too_large'


def test_replacement_versions_history_and_receipts(workflow):
    w = workflow
    original = create(w)
    source = original['source']
    url = prefix(w) + '/sources/' + source['id']
    replacement = body('# Replacement', version=source['version'])
    response = w['admin'].put(url + '/markdown', json=replacement)
    assert response.status_code == 200, response.text
    second = response.json()
    assert second['document']['revision_number'] == 2
    assert second['source']['version'] == source['version'] + 1
    assert w['admin'].put(url + '/markdown', json=replacement).json() == second
    assert w['admin'].put(url + '/markdown', json=body('Stale', version=source['version'])).status_code == 409
    documents = w['admin'].get(url + '/documents?limit=1').json()
    assert documents['has_more'] and documents['items'][0]['id'] == second['document']['id']
    first_text = w['admin'].get(url + '/documents/' + original['document']['id']).json()['content']
    assert first_text == '# Help\n\nUse the documented procedure.\n'
    with w['app'].state.storage.transaction() as c:
        assert c.execute('SELECT state FROM ai_jobs WHERE id=?', (source['job']['id'],)).fetchone()[0] == 'cancelled'


def test_approval_pending_requeues_but_never_claims_indexed(workflow):
    w = workflow
    source = create(w)['source']
    url = prefix(w) + '/sources/' + source['id']
    response = w['admin'].patch(url, json={'version': source['version'], 'active': True, 'approved': True})
    assert response.status_code == 200, response.text
    approved = response.json()
    assert approved['approved'] and approved['index_status'] == 'pending'
    assert approved['job']['id'] != source['job']['id']
    assert w['app'].state.storage.ai_knowledge_chunks(w['p']['id'], principal(w)) == []
    _, revision = index_one(w)
    indexed = w['admin'].get(url).json()
    assert indexed['index_status'] == 'indexed' and indexed['active_revision_id'] == revision['id']
    assert indexed['job'] is None and not indexed['index_stale']


def test_failed_refresh_keeps_approved_previous_index(workflow):
    w = workflow
    source = create(w)['source']
    url = prefix(w) + '/sources/' + source['id']
    source = w['admin'].patch(url, json={'version': source['version'], 'active': True, 'approved': True}).json()
    _, old_revision = index_one(w)
    source = w['admin'].get(url).json()
    replacement = w['admin'].put(url + '/markdown', json=body('New unfinished original', version=source['version'])).json()
    s = w['app'].state.storage
    job = s.ai_claim_job(time.time())
    assert s.ai_finish_job(job['id'], job['lease_token'], 'failed', 'embedding_failed')
    failed = w['admin'].get(url).json()
    assert failed['index_status'] == 'failed' and failed['index_stale']
    assert failed['latest_document']['id'] == replacement['document']['id']
    assert {x['revision_id'] for x in s.ai_knowledge_chunks(w['p']['id'], principal(w))} == {old_revision['id']}


def test_disable_delete_cancel_worker_and_preserve_private_original(workflow):
    w = workflow
    imported = create(w)
    source = imported['source']
    url = prefix(w) + '/sources/' + source['id']
    s, p = w['app'].state.storage, principal(w)
    job = s.ai_claim_job(time.time())
    disabled = w['admin'].patch(url, json={'version': source['version'], 'active': False, 'approved': False}).json()
    assert disabled['index_status'] == 'inactive' and not s.ai_job_before_dispatch(job['id'], job['lease_token'])
    assert s.ai_job_material(job['id'], job['lease_token']) is None
    active = w['admin'].patch(url, json={'version': disabled['version'], 'active': True, 'approved': False}).json()
    assert active['index_status'] == 'pending' and active['job']['id'] != job['id']
    latest = s.ai_claim_job(time.time())
    deleted = w['admin'].delete(url + '?version=' + str(active['version'])).json()
    assert deleted['index_status'] == 'deleted'
    assert not s.ai_job_before_dispatch(latest['id'], latest['lease_token'])
    assert w['admin'].patch(url, json={'version': deleted['version'], 'active': True, 'approved': True}).status_code == 409
    assert w['admin'].get(url + '/documents/' + imported['document']['id']).status_code == 200
    assert s.ai_knowledge_chunks(w['p']['id'], p) == []


def test_expired_admin_rechecks_original_mutation_atomically(workflow):
    w = workflow
    s, p = w['app'].state.storage, principal(w)
    with s.transaction(True) as c:
        c.execute('UPDATE sessions SET expires_at=? WHERE id=?', (time.time() - 1, p['session_id']))
    data = body()
    with pytest.raises(Error) as exc:
        s.ai_knowledge_import(w['p']['id'], p, data['client_import_id'], data['filename'], data['name'], data['content'])
    assert exc.value.status == 401
    with s.transaction() as c:
        assert c.execute('SELECT count(*) FROM ai_documents').fetchone()[0] == 0
        assert c.execute('SELECT count(*) FROM ai_sources').fetchone()[0] == 0


def test_original_survives_restart_backup_and_provenance_cannot_change(workflow, tmp_path):
    w = workflow
    imported = create(w, body('Original **immutable** text'))
    s, p = w['app'].state.storage, principal(w)
    source, document = imported['source'], imported['document']
    backup = tmp_path / 'knowledge-copy.sqlite'
    s.backup(backup)
    restored = SQLiteStorage(backup, s.base_url)
    assert restored.ai_knowledge_document(w['p']['id'], p, source['id'], document['id'])['content'] == 'Original **immutable** text'
    assert restored.ai_knowledge_source(w['p']['id'], p, source['id'])['index_status'] == 'pending'
    with pytest.raises(Error):
        with s.transaction(True) as c:
            c.execute('UPDATE ai_documents SET content=? WHERE id=?', ('Rewritten', document['id']))
    job = s.ai_claim_job(time.time())
    with pytest.raises(Error):
        s.ai_revision_stage(w['p']['id'], p, source['id'], job['authority']['source_version'], document['title'], 'Invented original', 1, job_id=job['id'], lease_token=job['lease_token'])


def test_queue_overflow_rolls_back_source_original_and_job_together(workflow):
    w = workflow
    s, p = w['app'].state.storage, principal(w)
    for number in range(50):
        source = s.ai_source_create(w['p']['id'], p, 'markdown', 'Queued ' + str(number))
        s.ai_enqueue(w['p']['id'], p, 'ingest', source['id'], str(number), 0)
    response = w['admin'].post(prefix(w) + '/markdown', json=body())
    assert response.status_code == 429 and response.json()['error']['code'] == 'ai_busy'
    with s.transaction() as c:
        assert c.execute('SELECT count(*) FROM ai_sources').fetchone()[0] == 50
        assert c.execute('SELECT count(*) FROM ai_jobs').fetchone()[0] == 50
        assert c.execute('SELECT count(*) FROM ai_documents').fetchone()[0] == 0


def test_simultaneous_replacements_have_one_version_winner(workflow):
    w = workflow
    initial = create(w)
    s, p, source = w['app'].state.storage, principal(w), initial['source']
    def replace(number):
        handle = reopen_storage(s)
        try:
            return handle.ai_knowledge_import(w['p']['id'], p, str(uuid4()), 'guide.md', 'Guide', '# Candidate ' + str(number), source_id=source['id'], version=source['version'])
        except Error as exc:
            assert exc.status == 409
            return None
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(replace, range(8)))
    winners = [result for result in results if result]
    assert len(winners) == 1
    assert winners[0]['source']['version'] == source['version'] + 1
    with s.transaction() as c:
        assert c.execute('SELECT count(*) FROM ai_documents').fetchone()[0] == 2
        assert c.execute("SELECT count(*) FROM ai_jobs WHERE state='queued'").fetchone()[0] == 1


def test_unapproval_never_requires_free_queue_capacity(workflow):
    w = workflow
    source = create(w)['source']
    url = prefix(w) + '/sources/' + source['id']
    approved = w['admin'].patch(url, json={'version': source['version'], 'active': True, 'approved': True}).json()
    s, p = w['app'].state.storage, principal(w)
    job = s.ai_claim_job(time.time())
    assert s.ai_finish_job(job['id'], job['lease_token'], 'failed', 'embedding_failed')
    for number in range(50):
        other = s.ai_source_create(w['p']['id'], p, 'markdown', 'Queued ' + str(number))
        s.ai_enqueue(w['p']['id'], p, 'ingest', other['id'], str(number), 0)
    response = w['admin'].patch(url, json={'version': approved['version'], 'active': True, 'approved': False})
    assert response.status_code == 200, response.text
    removed = response.json()
    assert removed['approved'] is False and removed['index_status'] == 'cancelled' and removed['job'] is None
    with s.transaction() as c:
        assert c.execute("SELECT count(*) FROM ai_jobs WHERE state='queued'").fetchone()[0] == 50
