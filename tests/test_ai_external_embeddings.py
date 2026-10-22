"""Real relational storage and synthetic external embeddings."""
from dataclasses import replace
import json
import httpx
import pytest
from feedback.ai_embeddings import EmbeddingService
from feedback.ai_providers import ProviderError
from feedback.ai_retrieval import IngestionWorker, RetrievalService
from feedback.errors import Error
from feedback.security import Vault
from test_ai_ingestion import ingest_store, upload, LocalFixture
from test_ai_settings_api import ai
from test_ai_account_keys import add_account, library, bind
from test_ai_providers import run, response

CONFIG = {'base_url':'https://embedding.example/v1','model':'test-embedding','dimensions':3,'send_dimensions':True,'query_prefix':'query: ','document_prefix':'passage: '}


def vector_response(request):
    body = json.loads(request.content)
    return response({'data': [{'index': i, 'embedding': [3.0,4.0,0.0]} for i in reversed(range(len(body['input'])))]})


def service(ai, handler=vector_response):
    embeddings = ai['app'].state.ai_embeddings
    embeddings.transport = httpx.MockTransport(handler)
    return embeddings


def configure(ai, config=CONFIG):
    current = ai['admin'].get('/api/v1/ai/embeddings').json()
    r = ai['admin'].put('/api/v1/ai/embeddings', json={'version':current['version'],'mode':'external','config':config,'key':'embedding-secret'})
    assert r.status_code == 200, r.text
    return r.json()


@pytest.mark.parametrize('client',['anon','user','trusted','manager','other'])
def test_external_embeddings_admin_only(ai, client):
    calls=[];service(ai, lambda req: calls.append(req) or vector_response(req))
    assert ai[client].get('/api/v1/ai/embeddings').status_code in (401,403)
    assert ai[client].put('/api/v1/ai/embeddings',json={'version':0,'mode':'external','config':CONFIG,'key':'secret'}).status_code in (401,403)
    assert not calls


def test_verify_normalize_reindex_and_preserve_originals(ai):
    embeddings = service(ai)
    s,p,pid = ai['storage'],ai['principal'],ai['p']['id']
    item = upload((s,p,pid), '# Guide\nContact us through the chat button.')
    IngestionWorker(s, LocalFixture()).run_once()
    source = s.ai_knowledge_sources(pid,p)['items'][0]
    s.ai_knowledge_change(pid,p,source['id'],source['version'],active=True,approved=True)
    before = s.ai_retrieval_context(pid,p)
    configured = configure(ai)
    assert configured['reindex'] == {'queued':1,'remaining':0}
    assert 'embedding-secret' not in json.dumps(configured)
    assert s.ai_embedding_settings(internal=True)['protected_key'] != 'embedding-secret'
    assert s.ai_retrieval_context(pid,p)['knowledge_version'] > before['knowledge_version']
    assert not s.ai_knowledge_sources(pid,p)['items'][0]['index_ready']
    assert IngestionWorker(s, embeddings).run_once()
    found = RetrievalService(s, embeddings).retrieve(pid,p,'How can I contact support?')
    assert found['passages'] and 'chat button' in found['passages'][0]['excerpt']
    with s.transaction() as c:
        assert c.execute('SELECT count(*) FROM ai_documents').fetchone()[0] == 1
        assert c.execute('SELECT count(*) FROM ai_revisions').fetchone()[0] == 2
    # Old model vectors must not leak into the new profile, even with matching dimensions.
    configure(ai, CONFIG | {'model':'different-model'})
    assert not s.ai_knowledge_sources(pid,p)['items'][0]['index_ready']
    with pytest.raises(Error):
        s.ai_retrieve(pid,p,'support',[.6,.8,0.0],found['knowledge_version'])


def test_late_verification_and_invalid_vectors_cannot_replace_settings(ai):
    embeddings=service(ai)
    before=ai['admin'].get('/api/v1/ai/embeddings').json()
    for vectors in ([0,0,0],[1,0],[True,0,0],[float('inf'),0,0]):
        embeddings.transport=httpx.MockTransport(lambda req:response({'data':[{'index':0,'embedding':vectors}]}))
        r=ai['admin'].put('/api/v1/ai/embeddings',json={'version':0,'mode':'external','config':CONFIG,'key':'secret'})
        assert r.status_code == 400 and ai['admin'].get('/api/v1/ai/embeddings').json()==before
    def stale(req):
        s=ai['storage'];s.ai_embedding_configure(ai['principal'],s.ai_embedding_settings(internal=True)['version'],'local',None,None,None)
        return vector_response(req)
    embeddings.transport=httpx.MockTransport(stale)
    r=ai['admin'].put('/api/v1/ai/embeddings',json={'version':0,'mode':'external','config':CONFIG,'key':'secret'})
    assert r.status_code == 409 and ai['admin'].get('/api/v1/ai/embeddings').json()['mode']=='local'


def test_unapproved_sources_are_not_sent_to_external_endpoint(ai):
    calls=[];embeddings=service(ai,lambda req:calls.append(json.loads(req.content)) or vector_response(req))
    configure(ai);calls.clear()
    s,p,pid=ai['storage'],ai['principal'],ai['p']['id']
    upload((s,p,pid),'Unreviewed source content')
    assert not IngestionWorker(s,embeddings).run_once() and not calls
    source=s.ai_knowledge_sources(pid,p)['items'][0]
    s.ai_knowledge_change(pid,p,source['id'],source['version'],active=True,approved=True)
    assert IngestionWorker(s,embeddings).run_once() and calls


def test_custom_connection_encrypted_replacement_fences_all_projects(ai):
    key=add_account(ai,provider='custom',secret='',base_url='https://llm.example/v1',models=['one'])
    raw=ai['storage'].ai_account_key_snapshot(ai['principal'],library(ai)['version'],key['id'],key['version'])
    decoded=json.loads(Vault(ai['cfg'].secret).reveal(raw['key']['protected_key']))
    assert decoded=={'base_url':'https://llm.example/v1','models':['one'],'key':''}
    cfg=ai['admin'].get(ai['url']).json()
    ai['admin'].put(ai['url'],json={'version':cfg['version'],'settings':cfg['settings']|{'provider':'custom'}}).raise_for_status()
    bind(ai,ai['p']['id'],[key['id']])
    version=ai['admin'].get(ai['url']).json()['version']
    r=ai['admin'].put('/api/v1/ai/keys/'+key['id'],json={'version':library(ai)['version'],'key_version':key['version'],'label':key['label'],'quota_group':key['quota_group'],'enabled':True,'key':'new-key','base_url':'https://new.example/v1','models':['two']})
    assert r.status_code==200,r.text
    assert ai['admin'].get(ai['url']).json()['version']>version
    assert not r.json()['keys'][0]['catalogue'] and 'new-key' not in r.text and 'https://new.example' not in r.text


def test_external_retrieval_inside_async_worker_and_late_response_fence(ai):
    """Generation calls synchronous retrieval from an async attempt loop."""
    embeddings = service(ai)
    configure(ai)
    async def query():
        return embeddings.embed_queries(['support question'])
    assert run(query()) == [[.6, .8, 0.0]]
    def change_during_request(request):
        s=ai['storage']
        s.ai_embedding_configure(ai['principal'],s.ai_embedding_settings(internal=True)['version'],'local',None,None,None)
        return vector_response(request)
    embeddings.transport=httpx.MockTransport(change_during_request)
    from feedback.ai_runtime import EmbeddingRuntimeError
    with pytest.raises(EmbeddingRuntimeError):
        run(query())


def test_embedding_probe_respects_admission_limit(ai):
    embeddings=service(ai)
    embeddings._slots.acquire();embeddings._slots.acquire()
    try:
        r=ai['admin'].put('/api/v1/ai/embeddings',json={'version':0,'mode':'external','config':CONFIG,'key':'secret'})
        assert r.status_code==409 and r.json()['error']['code']=='embedding_busy'
        assert embeddings.status(ai['principal'])['version']==0
    finally:
        embeddings._slots.release();embeddings._slots.release()


def test_sqlite_upgrade_preserves_vectors_fts_and_backup(ingest_store, tmp_path):
    from pathlib import Path
    from feedback.storage.sqlite import SQLiteStorage
    s,p,pid=ingest_store
    if not isinstance(s,SQLiteStorage):
        pytest.skip('SQLite table rebuild contract')
    imported=upload(ingest_store,'# Guide\nSearchable original support content')
    IngestionWorker(s,LocalFixture()).run_once()
    source=s.ai_knowledge_sources(pid,p)['items'][0]
    s.ai_knowledge_change(pid,p,source['id'],source['version'],active=True,approved=True)
    with s.transaction() as c:
        before=[tuple(row) for row in c.execute('SELECT rowid,* FROM ai_chunks')]
    # Reconstruct the previous fixed-dimension table, with existing rows and
    # FTS rowids, then remove only the new migration's ledger entry/settings.
    migration=Path('feedback/storage/migrations/022_external_embeddings.sql').read_text()
    rebuild=migration[migration.index('DROP TRIGGER'):].replace('length(vector) BETWEEN 4 AND 16384 AND length(vector)%4=0','length(vector)=3072')
    c=s.connect()
    try:
        c.execute('PRAGMA foreign_keys=OFF')
        c.executescript(rebuild)
        c.execute('DROP TABLE ai_embedding_settings')
        c.execute("DELETE FROM schema_migrations WHERE version='022_external_embeddings.sql'")
    finally:
        c.close()
    upgraded=SQLiteStorage(s.path,s.base_url)
    with upgraded.transaction() as c:
        assert before==[tuple(row) for row in c.execute('SELECT rowid,* FROM ai_chunks')]
        assert c.execute('PRAGMA foreign_key_check').fetchall()==[]
        assert c.execute("SELECT count(*) FROM ai_chunks_fts WHERE ai_chunks_fts MATCH 'Searchable'").fetchone()[0]==1
    backup=tmp_path/'restored.sqlite'
    upgraded.backup(backup)
    restored=SQLiteStorage(backup,s.base_url)
    assert RetrievalService(restored,LocalFixture()).retrieve(pid,p,'Searchable support')['passages']
