"""Fixed tools and bounded provider loops with real SQLite and fixture adapters."""
import asyncio
from copy import deepcopy
import json
import time
from uuid import uuid4

import pytest

from feedback.ai_chat import AIChatWorker
from feedback.ai_generation import AIGeneration
from feedback.ai_providers import GenerationResult, ToolCall, Usage, ProviderError
from feedback.ai_retrieval import RetrievalService
from feedback.ai_tools import AIToolDispatcher, ReportService, validate_call
from feedback.errors import Error
from test_ai_chat_isolation import ai_chat, configure, conversation, turn, FixtureVault, FixedEmbeddings


def enable_tools(env, names, *, guests=False, rounds=2):
    store, pid, admin = env['store'], env['project'], env['admin']
    cfg = store.ai_settings(pid, admin)
    store.ai_store_provider_evidence(pid, admin, cfg['version'], cfg['credential_version'], 'fixture-model',
                                    catalogue=['fixture-model'], capabilities={
                                        'text_generation': True, 'tool_calling': True,
                                        'streaming': False, 'structured_output': False})
    board = store.create_board(pid, {'identifier': 'feedback-' + str(uuid4()), 'name': 'Feedback', 'description': ''})
    settings = deepcopy(cfg['settings'])
    settings.update(tools={name: name in names for name in settings['tools']},
                    allow_guest_reports=guests, feature_board_id=board['id'], bug_board_id=board['id'],
                    max_tool_rounds=rounds)
    store.ai_configure(pid, admin, cfg['version'], settings)
    return board


class SequenceProvider:
    def __init__(self, results):
        self.results, self.calls = list(results), []

    async def generate(self, key, model, messages, **kwargs):
        self.calls.append({'model': model, 'messages': messages, **kwargs})
        if json.loads(messages[-1].content).get('operation') == 'evidence_check':
            return GenerationResult('{"assessment":"Fixture evidence supports this draft.","supported":true,"issues":[]}', (), Usage(20, 10, 30), 'stop')
        result = self.results.pop(0)
        return result(messages) if callable(result) else result


def called(name, arguments=None, call_id='call-1'):
    return GenerationResult('', (ToolCall(call_id, name, arguments or {}),), Usage(10, 5, 15), 'tool_calls')


def answered(chunk):
    return GenerationResult(json.dumps({'status': 'answered', 'body': 'Returns are accepted for thirty days.',
                                        'citation_ids': [chunk]}), (), Usage(20, 10, 30), 'stop')


def answer_from_messages(messages):
    data = json.loads(messages[-1].content)
    return answered(data['passages'][0]['id'] if data['passages'] else data['previous_source_ids'][0])


def engine(env, provider, embeddings=None):
    embeddings = embeddings or FixedEmbeddings()
    retrieval = RetrievalService(env['store'], embeddings)
    return AIGeneration(env['store'], FixtureVault(), retrieval, provider_factory=lambda _: provider), embeddings


def generate(generation, job, material, **kwargs):
    return asyncio.run(generation.generate(job['id'], job['lease_token'], material['question'], **kwargs))


@pytest.mark.parametrize('name,arguments', [
    ('search_knowledge', {'query': 'return policy'}), ('support_link', {'passage_id': 'fixture'}),
    ('request_human', {}), ('create_feature_request', {'title': 'New feature', 'description': 'Please add export.'}),
    ('create_bug_report', {'title': 'Export bug', 'description': 'Export stops unexpectedly.'}),
])
def test_each_tool_is_independently_advertised_and_cannot_create_a_report(ai_chat, name, arguments):
    env, store = ai_chat, ai_chat['store']
    enable_tools(env, [name])
    customer, _, job, material = turn(env)
    embeddings = FixedEmbeddings()
    dispatcher = AIToolDispatcher(store, RetrievalService(store, embeddings))
    assert [tool.name for tool in dispatcher.schemas(material)] == [name]
    if name == 'support_link':
        arguments = {'passage_id': env['chunk']}
    outcome = dispatcher.execute(job['id'], job['lease_token'], ToolCall('operation', name, arguments))
    if name in ('search_knowledge', 'support_link'):
        assert [passage['id'] for passage in outcome.passages] == [env['chunk']]
    elif name == 'request_human':
        assert outcome.stopped
        assert store.chat_conversation(customer['conversation']['id'], customer['principal'])['support_mode'] == 'waiting_human'
    else:
        assert outcome.reply == 'A draft is ready for you to review. Nothing has been submitted.'
        drafts = ReportService(store).proposals(customer['conversation']['id'], customer['principal'])['items']
        assert len(drafts) == 1 and not drafts[0]['can_confirm']
    with store.transaction() as c:
        assert c.execute('SELECT count(*) FROM requests').fetchone()[0] == 0
        assert c.execute('SELECT count(*) FROM ai_guest_owners').fetchone()[0] == 0


@pytest.mark.parametrize('name', ['search_knowledge', 'support_link', 'request_human', 'create_feature_request', 'create_bug_report', 'run_shell'])
def test_disabled_or_unknown_tool_denied_by_dispatcher_independently_of_provider(ai_chat, name):
    env, store = ai_chat, ai_chat['store']
    _, _, job, material = turn(env)
    arguments = {'query': 'returns'} if name == 'search_knowledge' else ({'passage_id': env['chunk']} if name == 'support_link' else
                 {'title': 'Title', 'description': ''} if name.startswith('create_') else {})
    dispatcher = AIToolDispatcher(store, RetrievalService(store, FixedEmbeddings()))
    assert dispatcher.schemas(material) == ()
    with pytest.raises(Error) as error:
        dispatcher.execute(job['id'], job['lease_token'], ToolCall('call', name, arguments))
    assert error.value.code == 'tool_not_enabled'


@pytest.mark.parametrize('arguments', [
    {'title': 'Title', 'description': '', 'user_id': 'attacker'},
    {'title': 'Title', 'description': '', 'board_id': 'foreign'},
    {'title': 'Title', 'description': '', 'confirmed': True},
    {'title': '', 'description': ''}, {'title': 'Title', 'description': '\ud800'},
])
def test_report_tool_rejects_identity_destination_confirmation_and_malformed_arguments(arguments):
    with pytest.raises(Error):
        validate_call(ToolCall('call', 'create_bug_report', arguments))


def test_search_tool_rounds_preserve_model_grounding_and_individual_usage(ai_chat):
    env, store = ai_chat, ai_chat['store']
    enable_tools(env, ['search_knowledge'])
    _, _, job, material = turn(env)
    provider = SequenceProvider([called('search_knowledge', {'query': 'returns'}), answer_from_messages])
    generation, embeddings = engine(env, provider)
    result = generate(generation, job, material)
    assert result['citation_ids'] == [env['chunk']] and len(provider.calls) == 3
    assert all([tool.name for tool in call['tools']] == ['search_knowledge'] for call in provider.calls[:2])
    assert provider.calls[2]['tools'] == ()
    assert len(embeddings.calls) == 2
    tool_message = provider.calls[1]['messages'][-1]
    assert tool_message.role == 'tool' and tool_message.tool_call_id == 'call-1'
    initial_handle = json.loads(provider.calls[0]['messages'][1].content)['passages'][0]['id']
    assert json.loads(tool_message.content)['passages'] == []
    assert json.loads(tool_message.content)['previous_source_ids'] == [initial_handle]
    assert initial_handle != env['chunk']
    usage = store.ai_usage(env['project'], env['admin'])
    assert usage['reserved_requests'] == 3
    assert (usage['reported_input_tokens'], usage['reported_output_tokens']) == (50, 25)


def test_last_permitted_round_forces_tools_off_and_rejects_more_calls(ai_chat):
    env, store = ai_chat, ai_chat['store']
    enable_tools(env, ['search_knowledge'], rounds=1)
    _, _, job, material = turn(env)
    provider = SequenceProvider([called('search_knowledge', {'query': 'returns'}),
                                 called('search_knowledge', {'query': 'more returns'}, 'call-2')])
    generation, embeddings = engine(env, provider)
    with pytest.raises(ProviderError) as error:
        generate(generation, job, material)
    assert error.value.code == 'invalid_response'
    assert len(provider.calls) == 2 and provider.calls[-1]['tools'] == ()
    assert len(embeddings.calls) == 2
    assert store.ai_usage(env['project'], env['admin'])['reserved_requests'] == 2


def test_invalid_later_call_prevents_entire_tool_batch_execution(ai_chat):
    env = ai_chat
    enable_tools(env, ['search_knowledge'])
    _, _, job, material = turn(env)
    provider = SequenceProvider([GenerationResult('', (
        ToolCall('read', 'search_knowledge', {'query': 'returns'}),
        ToolCall('write', 'create_bug_report', {'title': 'Bug', 'description': 'Do not create'})), Usage(10, 5), 'tool_calls')])
    generation, embeddings = engine(env, provider)
    with pytest.raises(ProviderError):
        generate(generation, job, material)
    assert len(embeddings.calls) == 1  # Baseline grounding only; neither tool ran.


def test_disabled_between_provider_response_and_tool_execution_has_no_effect(ai_chat):
    env, store = ai_chat, ai_chat['store']
    enable_tools(env, ['create_bug_report'])
    _, _, job, material = turn(env)

    def disable(messages):
        configure(env, enabled=False)
        return called('create_bug_report', {'title': 'Bug', 'description': 'Must not persist'})

    generation, _ = engine(env, SequenceProvider([disable]))
    with pytest.raises(Error) as error:
        generate(generation, job, material)
    assert error.value.status == 409
    with store.transaction() as c:
        assert c.execute('SELECT count(*) FROM ai_proposals').fetchone()[0] == 0


def test_report_action_without_retrieved_evidence_prepares_only_reviewable_draft(ai_chat):
    env, store = ai_chat, ai_chat['store']
    enable_tools(env, ['create_feature_request'], guests=True)
    customer, _, job, material = turn(env)
    provider = SequenceProvider([called('create_feature_request', {'title': 'Customer idea', 'description': 'Please add export.'})])
    generation, embeddings = engine(env, provider)
    result = generate(generation, job, material, passages=[])
    assert result['status'] == 'action' and not result['citation_ids']
    assert len(provider.calls) == 1 and embeddings.calls == []
    assert store.ai_commit_reply(job['id'], job['lease_token'], result['body'], result['citation_ids'], result['knowledge_version'])
    draft = ReportService(store).proposals(customer['conversation']['id'], customer['principal'])['items'][0]
    assert draft['can_confirm'] and draft['guest'] and draft['tracking_notice']
    assert draft['copy_text'] == 'Customer idea\n\nPlease add export.'
    with store.transaction() as c:
        assert c.execute('SELECT count(*) FROM requests').fetchone()[0] == 0
        assert c.execute('SELECT count(*) FROM ai_guest_owners').fetchone()[0] == 0


def test_handoff_tool_stops_generation_before_an_ai_reply(ai_chat):
    env, store = ai_chat, ai_chat['store']
    enable_tools(env, ['request_human'])
    customer = conversation(env)
    store.chat_send(customer['conversation']['id'], customer['principal'], str(uuid4()), 'Please get a human')
    provider = SequenceProvider([called('request_human')])
    generation, _ = engine(env, provider)
    assert AIChatWorker(store, generation).run_once() is False
    assert len(provider.calls) == 1
    assert store.chat_conversation(customer['conversation']['id'], customer['principal'])['support_mode'] == 'waiting_human'
    assert len(store.chat_messages(customer['conversation']['id'], customer['principal'], 0, 100)) == 1


@pytest.mark.parametrize('mode', ['preview', 'sections'])
def test_authoring_never_advertises_or_executes_tools_even_when_all_project_flags_are_on(ai_chat, mode):
    env, store = ai_chat, ai_chat['store']
    enable_tools(env, ['search_knowledge', 'support_link', 'request_human', 'create_feature_request', 'create_bug_report'], guests=True)
    cfg = store.ai_settings(env['project'], env['admin'])
    resource = env['project'] if mode == 'preview' else env['source']['id']
    store.ai_enqueue(env['project'], env['admin'], mode, resource, str(uuid4()), cfg['version'])
    job = store.ai_claim_job(time.time() + .01, kinds=(mode,))
    provider = SequenceProvider([called('create_bug_report', {'title': 'Never submit', 'description': 'Forbidden authoring action'})])
    generation, _ = engine(env, provider)
    with pytest.raises(ProviderError):
        asyncio.run(generation.generate(job['id'], job['lease_token'], 'Explain the return policy', mode=mode,
                                         passages=[{'id': env['chunk']}]))
    assert len(provider.calls) == 1 and provider.calls[0]['tools'] == ()
    with store.transaction() as c:
        assert c.execute('SELECT count(*) FROM ai_proposals').fetchone()[0] == 0


def test_second_generation_round_cannot_overrun_request_allowance(ai_chat):
    env, store = ai_chat, ai_chat['store']
    enable_tools(env, ['search_knowledge'])
    configure(env, daily_request_limit=1)
    _, _, job, material = turn(env)
    provider = SequenceProvider([called('search_knowledge', {'query': 'returns'}), answer_from_messages])
    generation, _ = engine(env, provider)
    with pytest.raises(Error) as error:
        generate(generation, job, material)
    assert error.value.code == 'budget_exhausted'
    assert len(provider.calls) == 1
    assert store.ai_usage(env['project'], env['admin'])['reserved_requests'] == 1


def test_total_tool_call_limit_forces_subsequent_generation_to_answer_only(ai_chat):
    env = ai_chat
    enable_tools(env, ['search_knowledge'], rounds=8)
    _, _, job, material = turn(env)
    batch = tuple(ToolCall('call-' + str(index), 'search_knowledge', {'query': 'returns'}) for index in range(8))
    provider = SequenceProvider([GenerationResult('', batch, Usage(10, 5), 'tool_calls'), answer_from_messages])
    generation, embeddings = engine(env, provider)
    assert generate(generation, job, material)['status'] == 'answered'
    assert len(provider.calls) == 3 and provider.calls[-1]['tools'] == ()
    assert len(embeddings.calls) == 9


def test_revocation_during_tool_search_stops_following_tools_and_provider_round(ai_chat):
    env, store = ai_chat, ai_chat['store']
    enable_tools(env, ['search_knowledge'])
    _, _, job, material = turn(env)

    class RevokingEmbeddings(FixedEmbeddings):
        def embed_queries(self, items):
            result = super().embed_queries(items)
            if len(self.calls) == 2:
                configure(env, enabled=False)
            return result

    provider = SequenceProvider([GenerationResult('', (
        ToolCall('first', 'search_knowledge', {'query': 'returns'}),
        ToolCall('second', 'search_knowledge', {'query': 'more returns'})), Usage(10, 5), 'tool_calls')])
    generation, embeddings = engine(env, provider, RevokingEmbeddings())
    with pytest.raises(Error) as error:
        generate(generation, job, material)
    assert error.value.status == 409
    assert len(provider.calls) == 1 and len(embeddings.calls) == 2


def test_changed_provider_call_id_cannot_create_duplicate_draft_on_recovered_attempt(ai_chat):
    env, store = ai_chat, ai_chat['store']
    enable_tools(env, ['create_bug_report'], guests=True)
    customer, _, job, material = turn(env)
    dispatcher = AIToolDispatcher(store, RetrievalService(store, FixedEmbeddings()))
    arguments = {'title': 'One report', 'description': 'A customer requested draft.'}
    dispatcher.execute(job['id'], job['lease_token'], ToolCall('original-provider-id', 'create_bug_report', arguments))
    with store.transaction(True) as c:
        c.execute('UPDATE ai_jobs SET lease_until=0 WHERE id=?', (job['id'],))
    recovered = store.ai_claim_job(time.time(), kinds=('turn',))
    dispatcher.execute(recovered['id'], recovered['lease_token'], ToolCall('different-provider-id', 'create_bug_report', arguments))
    service = ReportService(store)
    drafts = service.proposals(customer['conversation']['id'], customer['principal'])['items']
    assert len(drafts) == 1
    with pytest.raises(Error) as error:
        dispatcher.execute(recovered['id'], recovered['lease_token'], ToolCall('third-provider-id', 'create_bug_report', arguments | {'title': 'Changed by model'}))
    assert error.value.status == 409
    assert service.proposal(customer['conversation']['id'], customer['principal'], drafts[0]['id'])['title'] == 'One report'


@pytest.mark.parametrize('action', ['cancel', 'confirm', 'edit'])
def test_customer_action_racing_draft_acknowledgement_fences_late_reply_and_retry(ai_chat, monkeypatch, action):
    env, store = ai_chat, ai_chat['store']
    enable_tools(env, ['create_bug_report'], guests=True)
    customer, _, job, material = turn(env)
    provider = SequenceProvider([called('create_bug_report', {'title': 'One draft', 'description': 'Customer content'})])
    generation, _ = engine(env, provider)
    service, cid = ReportService(store), customer['conversation']['id']
    original = generation.tool_dispatcher.execute

    def execute_then_customer_action(job_id, token, call):
        result = original(job_id, token, call)
        draft = service.proposals(cid, customer['principal'])['items'][0]
        if action == 'cancel':
            service.cancel_proposal(cid, customer['principal'], draft['id'], draft['version'])
        elif action == 'edit':
            service.edit_proposal(cid, customer['principal'], draft['id'], draft['version'], 'Customer edited title', 'Customer edited content')
        else:
            service.confirm(cid, customer['principal'], draft['id'], draft['version'], draft['draft_hash'], str(uuid4()))
        return result

    monkeypatch.setattr(generation.tool_dispatcher, 'execute', execute_then_customer_action)
    with pytest.raises(Error) as error:
        generate(generation, job, material)
    assert error.value.status == 409
    assert len(provider.calls) == 1
    assert store.ai_commit_reply(job['id'], job['lease_token'], 'Nothing has been submitted.', [],
                                 material['job']['authority']['knowledge_version']) is None
    assert store.ai_claim_job(time.time() + 1, kinds=('turn',)) is None
    with pytest.raises(Error):
        original(job['id'], job['lease_token'], ToolCall('retry', 'create_bug_report', {'title': 'One draft', 'description': 'Customer content'}))
    with store.transaction() as c:
        assert c.execute('SELECT count(*) FROM requests').fetchone()[0] == (1 if action == 'confirm' else 0)
        assert c.execute("SELECT count(*) FROM chat_messages WHERE conversation_id=? AND sender='ai'", (cid,)).fetchone()[0] == 0
    if action == 'edit':
        draft = service.proposals(cid, customer['principal'])['items'][0]
        assert draft['title'] == 'Customer edited title'
        assert draft['description'] == 'Customer edited content'
        assert draft['state'] == 'pending' and draft['can_confirm']
        receipt = service.confirm(cid, customer['principal'], draft['id'], draft['version'], draft['draft_hash'], str(uuid4()))
        assert receipt['title'] == 'Customer edited title'
        assert receipt['description'] == 'Customer edited content'
