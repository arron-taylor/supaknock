"""Bounded excerpt diversity, exact provenance and real SQLite authorization."""
import math
import time
from uuid import uuid4

import pytest

from feedback.ai_domain import EMBEDDING_PROFILE, default_ai_settings
from feedback.ai_retrieval import IngestionWorker, RetrievalService, _select_passages
from feedback.errors import Error
from test_ai_ingestion import ingest_store


INTRO = '## Voice drafting\n\nSpeak or type a starting point, then review the suggested report focus.\n'
CONTENTS = [
    '## Workspace selection\n\nChoose a workspace before preparing a saved report. Select only the material you want to include.',
    '## Microphone privacy\n\nRaw microphone audio is processed on the device. Only submitted text can be sent for processing.',
    INTRO + 'Read details',
    INTRO.replace('## ', '# ', 1) + 'On this page',
    INTRO + 'Read details',
    '## Recover a draft\n\nResume an interrupted draft from the saved drafts collection. Review the text before continuing.',
    INTRO + 'Read details    Reports2 min read',
    '## Submitted text\n\nReview any transcript before sending it. Remove personal information you do not want included.',
    '## Apply the focus\n\nApply focus returns the text to the report editor. It does not generate the entire report.',
    INTRO + 'Read details   Account & access3 min read',
]


class RankedEmbeddings:
    def __init__(self):
        self.query_calls = 0
        self.before_return = None

    def available(self):
        return True

    def embed_documents(self, texts, *, titles=None):
        # A deterministic semantic order isolates selection from model variance.
        return [[math.cos((CONTENTS.index(text) + 1) * .06),
                 math.sin((CONTENTS.index(text) + 1) * .06)] + [0.0] * 766 for text in texts]

    def embed_queries(self, texts):
        self.query_calls += 1
        if self.before_return:
            self.before_return()
        return [[1.0] + [0.0] * 767 for _ in texts]


def website(env):
    store, principal, project = env
    config = {'seed_url': 'https://docs.example/start', 'approved_origins': ['https://docs.example'],
              'include_paths': ['/'], 'exclude_paths': [], 'max_pages': 10, 'max_depth': 2}
    crawl = store.ai_crawl_begin(project, principal, str(uuid4()), 'Product guides', config)
    job = store.ai_claim_job(time.time(), kinds=('crawl',))
    for index, content in enumerate(CONTENTS):
        assert store.ai_crawl_record(job['id'], job['lease_token'], {
            'url': 'https://docs.example/' + str(index), 'title': 'Product guide', 'content': content,
            'status': 'fetched', 'error': None})
    assert store.ai_crawl_finish(job['id'], job['lease_token'], True)
    runtime = RankedEmbeddings()
    assert IngestionWorker(store, runtime).run_once()
    source = store.ai_knowledge_source(project, principal, crawl['source']['id'])
    source = store.ai_knowledge_change(project, principal, source['id'], source['version'], active=True, approved=True)
    return runtime, source


@pytest.mark.parametrize('job_path', [False, True])
def test_repeated_website_intros_do_not_hide_ninth_ranked_original(ingest_store, job_path):
    store, principal, project = ingest_store
    runtime, source = website(ingest_store)
    context = store.ai_retrieval_context(project, principal)
    raw = store.ai_retrieve(project, principal, 'Where?', [1.0] + [0.0] * 767, context['knowledge_version'], 20)
    assert raw['passages'][8]['content'] == CONTENTS[8]
    assert CONTENTS[8] not in [row['content'] for row in raw['passages'][:6]]
    service = RetrievalService(store, runtime)
    if job_path:
        config = store.ai_configure(project, principal, 0, default_ai_settings() | {'model': 'fixture'}, protected_key='fixture-key')
        store.ai_store_provider_evidence(project, principal, config['version'], config['credential_version'], 'fixture',
            catalogue=['fixture'], capabilities=dict(text_generation=True, tool_calling=False, streaming=False, structured_output=False))
        operation = store.ai_runtime_begin(principal, 0, 'download')
        lease = store.ai_runtime_claim(time.time())
        assert store.ai_runtime_finish(operation['operation_id'], lease['lease_token'], manifest=EMBEDDING_PROFILE)
        job = store.ai_enqueue(project, principal, 'preview', project, str(uuid4()), config['version'])
        lease = store.ai_claim_job(time.time(), kinds=('preview',))
        assert lease['id'] == job['id']
        result = service.retrieve_job(job['id'], lease['lease_token'], 'Where?')
    else:
        result = service.retrieve(project, principal, 'Where?')
    assert runtime.query_calls == 1
    assert len(result['passages']) == 6
    assert [row['excerpt'] for row in result['passages']] == [CONTENTS[i] for i in (0, 1, 2, 5, 7, 8)]
    originals = {row['id']: row for row in raw['passages']}
    for row in result['passages']:
        original = originals[row['id']]
        assert row['excerpt'] == original['content']
        assert row['source_id'] == source['id']
        assert row['revision_id'] == original['revision_id']
        assert row['score'] == original['score']
        assert row['url'] == original['page_url']
    citations = service.validate_citations(project, principal, result['knowledge_version'], [row['id'] for row in result['passages']])
    assert [row['excerpt'] for row in citations] == [row['excerpt'] for row in result['passages']]


@pytest.mark.parametrize('change', ['revoke', 'deactivate', 'unapprove'])
def test_larger_selection_pool_still_rechecks_authority_after_embedding(ingest_store, change):
    store, principal, project = ingest_store
    runtime, source = website(ingest_store)
    if change == 'revoke':
        runtime.before_return = lambda: store.revoke_session(principal['session_id'])
    else:
        runtime.before_return = lambda: store.ai_knowledge_change(project, principal, source['id'], source['version'],
            active=change != 'deactivate', approved=change != 'unapprove')
    with pytest.raises(Error) as caught:
        RetrievalService(store, runtime).retrieve(project, principal, 'Where?')
    assert caught.value.status == (401 if change == 'revoke' else 409)


def row(content, index, **changes):
    return dict(id=str(index), source_id='source', revision_id='revision', kind='website',
                title='Policy', content=content, page_url='https://docs.example/' + str(index), score=1 / (index + 1)) | changes


def test_unique_short_and_conflicting_policies_keep_original_rank():
    rows = [row('## Refunds\n\nRefunds are available for 30 days.', 0),
            row('## Refunds\n\nRefunds are unavailable after 14 days.', 1),
            row('## Refunds\n\nRead the refund policy.\nExcept subscriptions.', 2),
            row('## Refunds\n\nRead the refund policy.\nSubscriptions are eligible.', 3)]
    assert _select_passages(rows, 6) == rows


def test_ambiguous_trailing_labels_are_deferred_not_destroyed():
    rows = [row(INTRO + 'Read details', 0), row(INTRO + 'Except subscriptions', 1),
            row('## Privacy\n\nAudio stays on the device.', 2)]
    selected = _select_passages(rows, 20)
    assert selected == [rows[0], rows[2], rows[1]]
    assert selected[-1]['content'].endswith('Except subscriptions')


@pytest.mark.parametrize('boundary', ['source_id', 'revision_id'])
def test_identical_text_never_collapses_distinct_source_or_revision(boundary):
    rows = [row(INTRO + 'Read details', 0), row(INTRO + 'Read details', 1, **{boundary: 'other'})]
    assert _select_passages(rows, 6) == rows


def test_exact_website_duplicates_collapse_but_keep_first_original_bytes():
    first = row('## Refunds\n\nKeep  the receipt.\n', 0)
    duplicate = row('# Refunds\n\nKeep the receipt.\n\n', 1)
    selected = _select_passages([first, duplicate], 6)
    assert selected == [first]
    assert selected[0] is first


@pytest.mark.parametrize('content', [INTRO + 'Read details', '```python\nif ready:\n    submit()\n```',
                                   '    preserve_code_indentation()', '# Policy\n\nUse `exact identifiers`.'])
def test_markdown_and_code_shaped_text_remain_unmodified(content):
    rows = [row(content, 0, kind='markdown'), row(content, 1, kind='markdown')]
    assert _select_passages(rows, 6) == rows
    if '`' in content or content.startswith('    '):
        website_rows = [row(content, 0), row(content, 1)]
        assert _select_passages(website_rows, 6) == website_rows


def test_requested_result_limit_is_preserved_and_duplicates_need_not_fill_it():
    rows = [row('## Privacy\n\nAudio stays on the device.', i) for i in range(20)]
    assert len(_select_passages(rows, 6)) == 1
    unique = [row('## Policy\n\nLimit ' + str(i) + '.', i) for i in range(20)]
    assert _select_passages(unique, 1) == unique[:1]
    assert _select_passages(unique, 20) == unique
