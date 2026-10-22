"""Adversarial scoped retrieval against real SQLite and deterministic vectors."""
import hashlib
import json
import struct
import time
from uuid import uuid4

import pytest

from feedback.ai_domain import EMBEDDING_PROFILE
from feedback.ai_retrieval import RetrievalService, chunk_markdown
from feedback.errors import Error
from feedback.storage.sqlite import SQLiteStorage


QUERY_VECTOR = [1.0] + [0.0] * 767
ELIGIBLE_VECTOR = [.6, .8] + [0.0] * 766


class DeterministicEmbeddings:
    def __init__(self, before_return=None):
        self.before_return = before_return
        self.calls = []

    def embed_queries(self, texts):
        self.calls.append(list(texts))
        if self.before_return:
            callback, self.before_return = self.before_return, None
            callback()
        return [list(QUERY_VECTOR) for _ in texts]


@pytest.fixture
def retrieval_store(tmp_path):
    store = SQLiteStorage(tmp_path / 'retrieval-isolation.sqlite', 'https://feedback.example')
    account = store.create_native_account('owner@example.com', 'test-hash', 'Owner', 'admin', True)
    store.create_session(account['id'], 'retrieval-admin-hash', 'csrf-hash', time.time() + 3600)
    principal = {'kind': 'admin', 'user_id': account['id'],
                 'session_id': store.session('retrieval-admin-hash')['session_id']}
    projects = [store.create_project({'identifier': name, 'name': name, 'description': ''}, account['id'])['id']
                for name in ('alpha', 'beta')]
    return store, principal, projects


def publish(env, project, content, *, vector=QUERY_VECTOR, count=1,
            name='Support guide', approved=True, active=True, profile=None):
    store, principal, _ = env
    source = store.ai_source_create(project, principal, 'markdown', name)
    if profile is None:
        revision = store.ai_revision_stage(project, principal, source['id'], source['version'],
                                          name, content, count)
        for start in range(0, count, 100):
            store.ai_revision_chunks(project, principal, revision['id'], [
                {'ordinal': index, 'title': name, 'content': content, 'location': None, 'vector': list(vector)}
                for index in range(start, min(start + 100, count))])
        source = store.ai_revision_publish(project, principal, revision['id'])
    else:
        # Emulate a previously valid historical index. New incompatible indexes
        # cannot be created through today's public publishing contract.
        revision_id = str(uuid4())
        with store.transaction(True) as connection:
            connection.execute('''INSERT INTO ai_revisions
                (id,source_id,project_id,source_version,state,title,content,content_hash,profile,expected_chunks,created_at)
                VALUES(?,?,?,?,'active',?,?,?,?,?,?)''',
                (revision_id, source['id'], project, source['version'], name, content,
                 hashlib.sha256(content.encode()).hexdigest(), json.dumps(profile, sort_keys=True, separators=(',', ':')),
                 count, '2026-10-03T00:00:00Z'))
            connection.executemany('''INSERT INTO ai_chunks
                (id,revision_id,project_id,ordinal,title,content,location,vector) VALUES(?,?,?,?,?,?,NULL,?)''',
                [(str(uuid4()), revision_id, project, index, name, content, struct.pack('<768f', *vector))
                 for index in range(count)])
            connection.execute('UPDATE ai_sources SET active_revision_id=? WHERE id=?', (revision_id, source['id']))
    return store.ai_source_update(project, principal, source['id'], source['version'], active=active, approved=approved)


@pytest.mark.parametrize('excluded', ['other_project', 'unapproved', 'inactive', 'profile_mismatch'])
def test_more_than_top_k_ineligible_hits_cannot_hide_scoped_result(retrieval_store, excluded):
    store, principal, (project, other) = retrieval_store
    valid = publish(retrieval_store, project, 'The eligible answer is in this passage.',
                    vector=ELIGIBLE_VECTOR, name='Eligible guide')
    kwargs = {'count': 120, 'name': 'Excluded guide'}
    bad_project = other if excluded == 'other_project' else project
    if excluded == 'unapproved':
        kwargs['approved'] = False
    elif excluded == 'inactive':
        kwargs['active'] = False
    elif excluded == 'profile_mismatch':
        kwargs['profile'] = EMBEDDING_PROFILE | {'revision': 'incompatible-old-model'}
    publish(retrieval_store, bad_project, 'needle needle needle excluded private evidence', **kwargs)
    result = RetrievalService(store, DeterministicEmbeddings()).retrieve(project, principal, 'needle', limit=1)
    assert result['project_id'] == project
    assert len(result['passages']) == 1
    assert result['passages'][0]['source_id'] == valid['id']
    assert result['passages'][0]['excerpt'] == 'The eligible answer is in this passage.'
    assert result['passages'][0]['url'] is None


def test_duplicate_text_keeps_distinct_source_and_revision_provenance(retrieval_store):
    store, principal, (project, _) = retrieval_store
    first = publish(retrieval_store, project, 'Identical approved support text.', name='First original')
    second = publish(retrieval_store, project, 'Identical approved support text.', name='Second original')
    service = RetrievalService(store, DeterministicEmbeddings())
    result = service.retrieve(project, principal, 'approved support', limit=6)
    assert {p['source_id'] for p in result['passages']} == {first['id'], second['id']}
    assert len({p['id'] for p in result['passages']}) == 2
    assert len({p['revision_id'] for p in result['passages']}) == 2
    ids = [p['id'] for p in result['passages']]
    citations = service.validate_citations(project, principal, result['knowledge_version'], ids)
    assert {c['id'] for c in citations} == set(ids)
    assert {c['source_id'] for c in citations} == {first['id'], second['id']}
    assert all('score' not in citation and citation['url'] is None for citation in citations)


@pytest.mark.parametrize('excluded', ['other_project', 'unapproved'])
def test_ineligible_term_frequencies_cannot_change_eligible_order_or_scores(retrieval_store, excluded):
    store, principal, (project, other) = retrieval_store
    publish(retrieval_store, project, 'alphaterm alphaterm alphaterm', name='Alpha original')
    publish(retrieval_store, project, 'betaterm betaterm', name='Beta original')
    service = RetrievalService(store, DeterministicEmbeddings())
    before = service.retrieve(project, principal, 'alphaterm betaterm', limit=2)
    baseline = [(passage['id'], passage['score']) for passage in before['passages']]
    assert len(baseline) == 2
    publish(retrieval_store, other if excluded == 'other_project' else project,
            'alphaterm ' * 80, count=300, name='Ineligible alpha-heavy corpus',
            approved=excluded != 'unapproved')
    after = service.retrieve(project, principal, 'alphaterm betaterm', limit=2)
    assert [(passage['id'], passage['score']) for passage in after['passages']] == baseline


@pytest.mark.parametrize(('common', 'rare'), [
    ('how can I find', 'anaphase'),
    ('どの ように 私は 探す', '回復鍵'),
])
def test_rare_evidence_survives_common_query_filler_at_six_passages(retrieval_store, common, rare):
    store, principal, (project, _) = retrieval_store
    # More than the lexical shortlist repeats the question's common words.
    # The rare-topic passage is the best semantic hit but unweighted lexical
    # coverage excludes it, letting every double-ranked filler outrank it.
    target = publish(retrieval_store, project, rare + ' specific support evidence.', name=rare)
    publish(retrieval_store, project, common + ' unrelated generic text.',
            name=common, vector=ELIGIBLE_VECTOR, count=30)
    result = RetrievalService(store, DeterministicEmbeddings()).retrieve(
        project, principal, common + ' ' + rare, limit=6)
    assert len(result['passages']) == 6
    assert result['passages'][0]['source_id'] == target['id']
    assert result['passages'][0]['excerpt'] == rare + ' specific support evidence.'


@pytest.mark.parametrize('change', ['deactivate', 'unapprove', 'delete', 'replace'])
def test_source_change_invalidates_citations_before_answer(retrieval_store, change):
    store, principal, (project, _) = retrieval_store
    source = publish(retrieval_store, project, 'The current approved support answer.')
    service = RetrievalService(store, DeterministicEmbeddings())
    result = service.retrieve(project, principal, 'support answer')
    ids = [passage['id'] for passage in result['passages']]
    assert service.validate_citations(project, principal, result['knowledge_version'], ids)
    if change == 'replace':
        revision = store.ai_revision_stage(project, principal, source['id'], source['version'],
                                          'Replacement', 'Updated answer.', 1)
        store.ai_revision_chunks(project, principal, revision['id'], [
            {'ordinal': 0, 'title': 'Replacement', 'content': 'Updated answer.',
             'location': None, 'vector': QUERY_VECTOR}])
        store.ai_revision_publish(project, principal, revision['id'])
    else:
        store.ai_source_update(project, principal, source['id'], source['version'],
                               active=change not in ('deactivate', 'delete'),
                               approved=change != 'unapprove', delete=change == 'delete')
    with pytest.raises(Error) as exc:
        service.validate_citations(project, principal, result['knowledge_version'], ids)
    assert exc.value.status == 409


def test_source_removed_while_query_embeds_cannot_enter_context(retrieval_store):
    store, principal, (project, _) = retrieval_store
    source = publish(retrieval_store, project, 'Evidence removed during an in-flight query.')
    def remove():
        store.ai_source_update(project, principal, source['id'], source['version'],
                               active=False, approved=False, delete=True)
    service = RetrievalService(store, DeterministicEmbeddings(before_return=remove))
    with pytest.raises(Error) as exc:
        service.retrieve(project, principal, 'query')
    assert exc.value.status == 409


def test_native_session_revoked_while_query_embeds_cannot_receive_passages(retrieval_store):
    store, principal, (project, _) = retrieval_store
    publish(retrieval_store, project, 'Private evidence for the current administrator.')
    embeddings = DeterministicEmbeddings(before_return=lambda: store.revoke_session(principal['session_id']))
    with pytest.raises(Error) as exc:
        RetrievalService(store, embeddings).retrieve(project, principal, 'private evidence')
    assert exc.value.status == 401
    assert len(embeddings.calls) == 1


@pytest.mark.parametrize('denied', ['guest', 'native_participant', 'revoked_admin'])
def test_unauthorized_principal_cannot_start_embedding(retrieval_store, denied):
    store, principal, (project, _) = retrieval_store
    if denied == 'guest':
        principal = principal | {'kind': 'guest'}
    elif denied == 'native_participant':
        user = store.create_native_account('reader@example.com', 'test-hash', 'Reader', 'participant', False)
        store.create_session(user['id'], 'participant-session-hash', 'csrf-hash', time.time() + 3600)
        principal = {'kind': 'admin', 'user_id': user['id'],
                     'session_id': store.session('participant-session-hash')['session_id']}
    else:
        store.revoke_session(principal['session_id'])
    embeddings = DeterministicEmbeddings()
    with pytest.raises(Error) as exc:
        RetrievalService(store, embeddings).retrieve(project, principal, 'support')
    assert exc.value.status == (403 if denied == 'guest' else 401)
    assert embeddings.calls == []


def test_crawl_job_cannot_start_query_embedding(retrieval_store):
    store, principal, (project, _) = retrieval_store
    source = publish(retrieval_store, project, 'Support evidence.')
    job = store.ai_enqueue(project, principal, 'crawl', source['id'], str(uuid4()), 0)
    claim = store.ai_claim_job(time.time() + 1)
    assert claim['id'] == job['id']
    embeddings = DeterministicEmbeddings()
    with pytest.raises(Error) as exc:
        RetrievalService(store, embeddings).retrieve_job(job['id'], claim['lease_token'], 'support')
    assert exc.value.status == 409
    assert embeddings.calls == []


def test_unknown_or_foreign_citation_cannot_be_validated(retrieval_store):
    store, principal, (project, other) = retrieval_store
    publish(retrieval_store, project, 'Allowed original.')
    publish(retrieval_store, other, 'Private foreign original.')
    service = RetrievalService(store, DeterministicEmbeddings())
    result = service.retrieve(project, principal, 'original')
    foreign = service.retrieve(other, principal, 'original')['passages'][0]['id']
    for untrusted_id in (foreign, str(uuid4())):
        with pytest.raises(Error) as exc:
            service.validate_citations(project, principal, result['knowledge_version'], [untrusted_id])
        assert exc.value.status == 409


@pytest.mark.parametrize('query', ['"needle" OR * - NEAR(a b)', 'title:needle NOT secret',
                                  "needle' ); DROP TABLE ai_chunks; --", '(((("*'] )
def test_fts_syntax_is_literal_and_cannot_escape_project(retrieval_store, query):
    store, principal, (project, other) = retrieval_store
    source = publish(retrieval_store, project, 'needle support details')
    publish(retrieval_store, other, 'secret private details')
    result = RetrievalService(store, DeterministicEmbeddings()).retrieve(project, principal, query)
    assert {p['source_id'] for p in result['passages']} == {source['id']}
    assert store.ai_knowledge_chunks(project, principal)


@pytest.mark.parametrize('query,limit', [('x' * 1001, 6), ('question', 0), ('question', 21)])
def test_query_and_result_bounds_reject_before_embedding(retrieval_store, query, limit):
    store, principal, (project, _) = retrieval_store
    embeddings = DeterministicEmbeddings()
    with pytest.raises(Error) as exc:
        RetrievalService(store, embeddings).retrieve(project, principal, query, limit=limit)
    assert exc.value.status == 400
    assert embeddings.calls == []


@pytest.mark.parametrize('content', [
    '# Headline\n\n' + 'Every paragraph remains evidence.\n\n' * 900 + 'FINAL_UNIQUE_TAIL',
    '# Unicode\n\n' + '支持🙂 Résumé e\u0301\n' * 900 + '\n## 最後\nUNICODE_TAIL',
    '# Code guide\n\n```python\n' + '    value = "preserve this indentation"\n' * 650 + '```\n\nCODE_TAIL',
    '# Long token\n\n' + 'unbrokenvalue' * 2500 + '\nLONG_TOKEN_TAIL',
])
def test_chunking_preserves_original_spans_headings_code_and_full_tail(content):
    chunks = chunk_markdown(content, 'Customer support')
    assert len(chunks) > 1
    previous = 0
    for ordinal, chunk in enumerate(chunks):
        assert chunk['ordinal'] == ordinal
        start, end = chunk['start'], chunk['end']
        assert previous <= start < end <= len(content)
        assert not content[previous:start].strip()
        assert chunk['content'] == content[start:end]
        assert chunk['content'].strip()
        prompt = f"title: {chunk['title'] or 'none'} | text: {chunk['content']}"
        assert len(prompt.encode('utf-8')) <= 1800
        previous = end
    assert not content[previous:].strip()
    assert ''.join(''.join(chunk['content'].split()) for chunk in chunks) == ''.join(content.split())
    assert content.rsplit('\n', 1)[-1] in chunks[-1]['content']
