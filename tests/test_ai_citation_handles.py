"""Attempt-local model references retain canonical SQLite citation authority."""
import asyncio
import json
import time
from uuid import uuid4

import pytest

from feedback.ai_generation import AIGeneration
from feedback.ai_providers import GenerationResult, ProviderError, ToolCall, Usage
from feedback.ai_retrieval import RetrievalService
from feedback.errors import Error
from test_ai_storage import ai_store, ready_project
from test_ai_generation import engine_case, AdapterFixture, Embeddings, VaultFixture, answer, generate
from test_ai_chat_isolation import ai_chat, turn
from test_ai_tools import SequenceProvider, called, enable_tools, engine, generate as generate_turn


def source_with_chunks(store, principal, project, texts):
    source = store.ai_source_create(project, principal, 'markdown', 'Additional guide')
    revision = store.ai_revision_stage(project, principal, source['id'], source['version'],
        'Additional guide', '\n\n'.join(texts), len(texts))
    store.ai_revision_chunks(project, principal, revision['id'], [
        {'ordinal': index, 'title': f'Guide {index}', 'content': content, 'location': None,
         'vector': [1.0] + [0.0] * 767} for index, content in enumerate(texts)])
    source = store.ai_revision_publish(project, principal, revision['id'])
    source = store.ai_source_update(project, principal, source['id'], source['version'], active=True, approved=True)
    rows = [row for row in store.ai_knowledge_chunks(project, principal, limit=100) if row['source_id'] == source['id']]
    return source, rows


@pytest.mark.parametrize('variant', ['canonical_uuid', 'lowercase', 'leading_space', 'trailing_space',
    'leading_zero', 'zero', 'unissued', 'duplicate', 'example_placeholder', 'non_string'])
def test_only_exact_issued_handles_survive_model_validation(engine_case, variant):
    store, principal, project, _, job, adapter, generation = engine_case
    original = generation.retrieval.retrieve_job(job['id'], job['lease_token'], 'backups')['passages'][0]['id']

    def respond(data):
        handle = data['passages'][0]['id']
        invalid = {'canonical_uuid': [original], 'lowercase': [handle.lower()],
            'leading_space': [' ' + handle], 'trailing_space': [handle + ' '],
            'leading_zero': ['S01'], 'zero': ['S0'], 'unissued': ['S2'],
            'duplicate': [handle, handle], 'example_placeholder': ['ACTUAL_SUPPLIED_PASSAGE_ID'], 'non_string': [{}]}
        return answer(citation_ids=invalid[variant])

    adapter.response = respond
    with pytest.raises(ProviderError, match='invalid_response'):
        generate(engine_case)
    assert store.ai_usage(project, principal)['reported_output_tokens'] == 30
    assert original not in adapter.calls[0][2][1].content


@pytest.mark.parametrize('mode', ['preview', 'sections'])
def test_multiple_references_restore_exact_canonical_ids_and_order(ai_store, mode):
    store, principal, project, _ = ai_store
    cfg, _, _ = ready_project(store, principal, project)
    source, rows = source_with_chunks(store, principal, project,
        ['Keep an original backup before changing devices.', 'Restore the backup to recover its contents.'])
    selected = [rows[1]['id'], rows[0]['id']]
    queued = store.ai_enqueue(project, principal, mode, source['id'] if mode == 'sections' else project,
                              str(uuid4()), cfg['version'])
    job = store.ai_claim_job(time.time(), kinds=(mode,))
    assert job['id'] == queued['id']
    adapter = AdapterFixture()

    def respond(data):
        handles = [row['id'] for row in data['passages']]
        assert handles == ['S1', 'S2']
        if mode == 'sections':
            return json.dumps({'sections': [
                {'kind': 'section', 'title': 'Backups', 'body': 'Keep and restore the original backup.',
                 'category': 'Support', 'citation_ids': handles[::-1]},
                {'kind': 'faq', 'title': 'What should I preserve?', 'body': 'Keep the original backup.',
                 'category': 'Support', 'citation_ids': [handles[1]]}]})
        return answer(citation_ids=handles[::-1])

    adapter.response = respond
    generation = AIGeneration(store, VaultFixture(), RetrievalService(store, Embeddings()), provider_factory=lambda _: adapter)
    result = asyncio.run(generation.generate(job['id'], job['lease_token'], 'Explain backups.', mode=mode,
                                            passages=[{'id': identity} for identity in selected]))
    if mode == 'sections':
        assert [row['citation_ids'] for row in result['sections']] == [selected[::-1], [selected[1]]]
    else:
        assert result['citation_ids'] == selected[::-1]
    assert all(identity not in adapter.calls[0][2][1].content for identity in selected)
    assert result['knowledge_version'] == job['authority']['knowledge_version']


def test_recovered_attempt_rebuilds_mapping_and_rejects_prior_unissued_handle(ai_store):
    store, principal, project, _ = ai_store
    cfg, _, _ = ready_project(store, principal, project)
    _, rows = source_with_chunks(store, principal, project, ['First backup guide.', 'Second restore guide.'])
    store.ai_enqueue(project, principal, 'preview', project, str(uuid4()), cfg['version'])
    first = store.ai_claim_job(time.time(), kinds=('preview',))
    adapter = AdapterFixture()
    adapter.response = lambda data: answer(citation_ids=[data['passages'][-1]['id']])
    generation = AIGeneration(store, VaultFixture(), RetrievalService(store, Embeddings()), provider_factory=lambda _: adapter)
    result = asyncio.run(generation.generate(first['id'], first['lease_token'], 'Backups?', mode='preview', passages=rows))
    assert result['citation_ids'] == [rows[-1]['id']]
    prior_handle = json.loads(adapter.calls[0][2][1].content)['passages'][-1]['id']
    stamp = time.time()
    assert store.ai_finish_job(first['id'], first['lease_token'], 'queued', error='unavailable', retry_at=stamp, current_time=stamp)
    second = store.ai_claim_job(stamp + .01, kinds=('preview',))
    assert second['attempt_count'] == first['attempt_count'] + 1
    adapter.response = answer(citation_ids=[prior_handle])
    with pytest.raises(ProviderError, match='invalid_response'):
        asyncio.run(generation.generate(second['id'], second['lease_token'], 'Restore?', mode='preview', passages=[rows[-1]]))
    assert json.loads(adapter.calls[2][2][1].content)['passages'][0]['id'] == 'S1'
    assert prior_handle == 'S2'


def test_search_adds_stable_handles_and_repeated_originals_reuse_them(ai_chat):
    env, store = ai_chat, ai_chat['store']
    _, additional = source_with_chunks(store, env['admin'], env['project'], ['Returns require the original purchase receipt.'])
    enable_tools(env, ['search_knowledge'], rounds=2)
    _, _, job, material = turn(env)

    def respond(messages):
        passages = json.loads(messages[-1].content)['passages']
        return GenerationResult(answer(citation_ids=[row['id'] for row in passages]), (), Usage(20, 10), 'stop')

    provider = SequenceProvider([called('search_knowledge', {'query': 'returns'}),
        called('search_knowledge', {'query': 'returns receipt'}, 'search-again'), respond])
    generation, _ = engine(env, provider)
    result = generate_turn(generation, job, material, passages=[{'id': env['chunk']}])
    first = json.loads(provider.calls[0]['messages'][1].content)['passages']
    second = json.loads(provider.calls[1]['messages'][-1].content)['passages']
    third = json.loads(provider.calls[2]['messages'][-1].content)['passages']
    assert first[0]['id'] == 'S1'
    by_text = {row['text']: row['id'] for row in second}
    assert by_text == {row['text']: row['id'] for row in third}
    assert by_text[first[0]['text']] == 'S1' and set(by_text.values()) == {'S1', 'S2'}
    assert set(result['citation_ids']) == {env['chunk'], additional[0]['id']}
    assert len(provider.calls) == 4 and provider.calls[-1]['tools'] == ()


@pytest.mark.parametrize('invalid', ['raw_uuid', 'unknown', 'wrong_case', 'whitespace'])
def test_invalid_support_handle_prevents_entire_batch_including_earlier_reads_and_later_actions(ai_chat, invalid):
    env, store = ai_chat, ai_chat['store']
    enable_tools(env, ['search_knowledge', 'support_link', 'create_bug_report'], guests=True)
    _, _, job, material = turn(env)

    def respond(messages):
        valid = json.loads(messages[1].content)['passages'][0]['id']
        handle = {'raw_uuid': env['chunk'], 'unknown': 'S20', 'wrong_case': valid.lower(), 'whitespace': valid + ' '}[invalid]
        return GenerationResult('', (
            ToolCall('earlier-read', 'search_knowledge', {'query': 'returns'}),
            ToolCall('bad-source', 'support_link', {'passage_id': handle}),
            ToolCall('later-action', 'create_bug_report', {'title': 'No draft', 'description': 'Must never be prepared'})),
            Usage(10, 5), 'tool_calls')

    provider = SequenceProvider([respond])
    generation, embeddings = engine(env, provider)
    executions = []
    original = generation.tool_dispatcher.execute
    def execute(*args):
        executions.append(args)
        return original(*args)
    generation.tool_dispatcher.execute = execute
    with pytest.raises(ProviderError, match='invalid_response'):
        generate_turn(generation, job, material)
    assert not executions and len(embeddings.calls) == 1
    with store.transaction() as connection:
        assert connection.execute('SELECT count(*) FROM ai_proposals').fetchone()[0] == 0


def test_tool_cannot_expand_mapping_past_twenty_originals_or_dispatch_another_round(ai_chat, monkeypatch):
    env, store = ai_chat, ai_chat['store']
    _, rows = source_with_chunks(store, env['admin'], env['project'], [f'Return instruction {i}.' for i in range(21)])
    enable_tools(env, ['search_knowledge'])
    _, _, job, material = turn(env)
    provider = SequenceProvider([called('search_knowledge', {'query': 'returns'})])
    generation, _ = engine(env, provider)
    # A search can legitimately find a new project-approved original, but it
    # cannot expand the whole attempt's context beyond the existing bound.
    monkeypatch.setattr(generation.retrieval, 'retrieve_job', lambda *args, **kwargs: {'passages': [rows[-1]]})
    with pytest.raises(Error, match='Too many support passages'):
        generate_turn(generation, job, material, passages=rows[:20])
    assert len(provider.calls) == 1
    assert len(json.loads(provider.calls[0]['messages'][1].content)['passages']) == 20
    assert store.ai_usage(env['project'], env['admin'])['reserved_requests'] == 1
