"""Local indexing worker lifecycle and exact source provenance on real SQLite."""
import time
from uuid import uuid4

import pytest

from feedback.ai_domain import EMBEDDING_PROFILE
from feedback.ai_retrieval import IngestionWorker, RetrievalService
from feedback.ai_runtime import EmbeddingRuntimeError
from feedback.errors import Error
from conftest import make_storage

VECTOR = [1.0] + [0.0] * 767


class LocalFixture:
    def __init__(self):
        self.ready = True
        self.calls = []
        self.before_return = None
        self.maximum = None
        self.failure = None
        self.accepted = 0

    def available(self):
        return self.ready

    def embed_documents(self, texts, *, titles=None):
        self.calls.append((list(texts), list(titles or [])))
        if self.failure:
            raise EmbeddingRuntimeError(self.failure)
        if self.maximum and any(len(text) > self.maximum for text in texts):
            raise EmbeddingRuntimeError('input_too_long')
        if self.before_return:
            callback, self.before_return = self.before_return, None
            callback()
        self.accepted += len(texts)
        return [list(VECTOR) for _ in texts]

    def embed_queries(self, texts):
        if self.failure:
            raise EmbeddingRuntimeError(self.failure)
        return [list(VECTOR) for _ in texts]


@pytest.fixture
def ingest_store(tmp_path, database_url):
    s = make_storage(tmp_path / 'ingest.sqlite', 'https://feedback.example', database_url)
    account = s.create_native_account('admin@example.com', 'hash', 'Admin', 'admin', True)
    s.create_session(account['id'], 'session-hash', 'csrf', time.time() + 3600)
    p = {'kind': 'admin', 'user_id': account['id'], 'session_id': s.session('session-hash')['session_id']}
    pid = s.create_project({'identifier': 'one', 'name': 'One', 'description': ''}, account['id'])['id']
    return s, p, pid


def upload(env, content, *, source=None):
    s, p, pid = env
    kwargs = {} if source is None else {'source_id': source['id'], 'version': source['version']}
    return s.ai_knowledge_import(pid, p, str(uuid4()), 'guide.md', 'Guide', content, **kwargs)


def test_pending_work_is_preserved_until_model_available(ingest_store):
    s, p, pid = ingest_store
    imported = upload(ingest_store, '# Guide\nOriginal support content')
    runtime = LocalFixture()
    runtime.ready = False
    worker = IngestionWorker(s, runtime)
    assert worker.run_once() is False
    job = s.ai_jobs(pid, p)[0]
    assert job['state'] == 'queued' and job['attempt_count'] == 0
    runtime.ready = True
    assert worker.run_once() is True
    source = s.ai_knowledge_source(pid, p, imported['source']['id'])
    assert source['index_status'] == 'indexed' and not source['approved']
    assert s.ai_jobs(pid, p)[0]['state'] == 'succeeded'
    assert RetrievalService(s, runtime).retrieve(pid, p, 'support')['passages'] == []
    approved = s.ai_knowledge_change(pid, p, source['id'], source['version'], active=True, approved=True)
    passages = RetrievalService(s, runtime).retrieve(pid, p, 'support')['passages']
    assert passages and passages[0]['source_id'] == approved['id'] and passages[0]['url'] is None


def test_ingestion_never_claims_crawl_work(ingest_store):
    s, p, pid = ingest_store
    source = s.ai_source_create(pid, p, 'website', 'Website', 'https://docs.example/')
    crawl = s.ai_enqueue(pid, p, 'crawl', source['id'], 'crawl', 0)
    runtime = LocalFixture()
    assert IngestionWorker(s, runtime).run_once() is False
    assert s.ai_jobs(pid, p)[0]['id'] == crawl['id'] and s.ai_jobs(pid, p)[0]['attempt_count'] == 0
    assert runtime.calls == []


def test_ingest_job_cannot_trigger_question_inference(ingest_store):
    s, _, _ = ingest_store
    upload(ingest_store, 'Original support content')
    job = s.ai_claim_job(time.time(), kinds=('ingest',))
    runtime = LocalFixture()
    def unexpected_inference(_):
        pytest.fail('Ingestion jobs cannot perform question inference')
    runtime.embed_queries = unexpected_inference
    with pytest.raises(Error) as caught:
        RetrievalService(s, runtime).retrieve_job(job['id'], job['lease_token'], 'question')
    assert caught.value.status == 409


@pytest.mark.parametrize('code,status', [('input_too_long', 400), ('invalid_input', 400), ('busy', 503), ('unavailable', 503)])
def test_question_runtime_errors_distinguish_input_from_availability(ingest_store, code, status):
    s, p, pid = ingest_store
    runtime = LocalFixture()
    runtime.failure = code
    with pytest.raises(Error) as caught:
        RetrievalService(s, runtime).retrieve(pid, p, 'A short question')
    assert caught.value.status == status


def test_busy_runtime_defers_without_spending_attempt_budget(ingest_store):
    s, p, pid = ingest_store
    upload(ingest_store, 'Original support content')
    runtime = LocalFixture()
    runtime.failure = 'busy'
    assert IngestionWorker(s, runtime).run_once() is False
    job = s.ai_jobs(pid, p)[0]
    assert job['state'] == 'queued' and job['attempt_count'] == 0
    assert job['next_attempt'] > time.time()


def test_document_change_during_embedding_cannot_publish(ingest_store):
    s, p, pid = ingest_store
    original = upload(ingest_store, 'Old original source')
    runtime = LocalFixture()
    runtime.before_return = lambda: upload(ingest_store, 'Newest original source', source=original['source'])
    worker = IngestionWorker(s, runtime)
    assert worker.run_once() is False
    source = s.ai_knowledge_source(pid, p, original['source']['id'])
    assert source['active_revision_id'] is None
    assert worker.run_once() is True
    source = s.ai_knowledge_source(pid, p, original['source']['id'])
    assert source['index_status'] == 'indexed'
    with s.transaction() as c:
        assert c.execute("SELECT content FROM ai_revisions WHERE state='active'").fetchone()[0] == 'Newest original source'


def test_identical_original_reuses_embeddings_with_new_provenance(ingest_store):
    s, p, pid = ingest_store
    original = upload(ingest_store, '# Backup\n\nPreserve the exact source text.')
    runtime = LocalFixture()
    worker = IngestionWorker(s, runtime)
    assert worker.run_once()
    first_calls = len(runtime.calls)
    source = s.ai_knowledge_source(pid, p, original['source']['id'])
    replacement = upload(ingest_store, '# Backup\n\nPreserve the exact source text.', source=source)
    assert worker.run_once()
    assert len(runtime.calls) == first_calls
    with s.transaction() as c:
        row = c.execute("SELECT document_id,profile FROM ai_revisions WHERE state='active'").fetchone()
        assert row['document_id'] == replacement['document']['id']
        assert 'embeddinggemma-retrieval-v1' in row['profile']


def test_runtime_token_overflow_splits_without_losing_original(ingest_store):
    s, p, pid = ingest_store
    content = '# Long source\n\n' + 'Full source content survives normalization. ' * 90 + 'FINAL_TAIL'
    imported = upload(ingest_store, content)
    runtime = LocalFixture()
    runtime.maximum = 120
    worker = IngestionWorker(s, runtime)
    assert worker.run_once()
    with s.transaction() as c:
        chunks = c.execute('SELECT k.content FROM ai_chunks k JOIN ai_revisions r ON r.id=k.revision_id WHERE r.state=\'active\' ORDER BY ordinal').fetchall()
    assert len(chunks) > 10 and all(len(row[0]) <= 120 for row in chunks)
    assert ''.join(''.join(row[0].split()) for row in chunks) == ''.join(content.split())
    assert 'FINAL_TAIL' in chunks[-1][0]
    assert s.ai_knowledge_source(pid, p, imported['source']['id'])['index_status'] == 'indexed'


def test_adaptive_split_refresh_reuses_every_exact_descendant(ingest_store):
    s, p, pid = ingest_store
    content = '# Guide\n\n' + 'Retain the entire original text. ' * 20 + 'FINAL_TAIL'
    original = upload(ingest_store, content)
    runtime = LocalFixture()
    runtime.maximum = 120
    worker = IngestionWorker(s, runtime)
    assert worker.run_once()
    accepted = runtime.accepted
    assert accepted > 1
    source = s.ai_knowledge_source(pid, p, original['source']['id'])
    replacement = upload(ingest_store, content, source=source)
    assert worker.run_once()
    assert runtime.accepted == accepted
    with s.transaction() as c:
        rows = c.execute('SELECT k.content,r.document_id FROM ai_chunks k JOIN ai_revisions r ON r.id=k.revision_id WHERE r.state=\'active\' ORDER BY ordinal').fetchall()
    assert all(row['document_id'] == replacement['document']['id'] for row in rows)
    assert ''.join(''.join(row['content'].split()) for row in rows) == ''.join(content.split())


def test_crawled_passages_have_only_original_page_urls(ingest_store):
    s, p, pid = ingest_store
    config = {'seed_url': 'https://docs.example/start', 'approved_origins': ['https://docs.example'], 'include_paths': ['/'], 'exclude_paths': [], 'max_pages': 5, 'max_depth': 2}
    crawl = s.ai_crawl_begin(pid, p, str(uuid4()), 'Website', config)
    job = s.ai_claim_job(time.time(), kinds=('crawl',))
    for url, title, content in [('https://docs.example/start', 'Start', '# Start\nThis is the first original.'), ('https://docs.example/help', 'Help', '# Help\nThe support answer is here.')]:
        assert s.ai_crawl_record(job['id'], job['lease_token'], {'url': url, 'title': title, 'content': content, 'status': 'fetched', 'error': None})
    assert s.ai_crawl_finish(job['id'], job['lease_token'], True)
    runtime = LocalFixture()
    assert IngestionWorker(s, runtime).run_once()
    source = s.ai_knowledge_source(pid, p, crawl['source']['id'])
    s.ai_knowledge_change(pid, p, source['id'], source['version'], active=True, approved=True)
    service = RetrievalService(s, runtime)
    found = service.retrieve(pid, p, 'support answer')
    assert {row['url'] for row in found['passages']} == {'https://docs.example/start', 'https://docs.example/help'}
    assert all(row['kind'] == 'website' for row in found['passages'])
    assert service.validate_citations(pid, p, found['knowledge_version'], [row['id'] for row in found['passages']])
