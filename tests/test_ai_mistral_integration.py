"""Shipped provider switches and native Mistral tool rounds on real SQLite.

Keys are generated fixture values; every provider response is local and no
embedding model, external credential or network connection is used.
"""
from itertools import permutations
import json
import re
from uuid import uuid4

from cryptography.fernet import Fernet
import httpx
import pytest

from feedback.ai_chat import AIChatWorker
from feedback.ai_generation import AIGeneration
from feedback.ai_providers import MistralProvider, GenerationResult, Usage
from feedback.ai_retrieval import RetrievalService
from feedback.errors import Error
from feedback.security import Vault
from test_ai_chat_isolation import ai_chat, conversation, turn, FixedEmbeddings
from test_ai_tools import answered, generate


PROVIDER_SWITCHES = tuple(permutations(('kodekey', 'gemini', 'mistral', 'groq'), 2))


def select_provider(env, vault, identity, *, model='shared-fixture-model', tools=()):
    """Use the public adapter operations to represent explicit verified setup."""
    store, project, admin = env['store'], env['project'], env['admin']
    current = store.ai_settings(project, admin)
    current = store.ai_configure(project, admin, current['version'], current['settings'] | {
        'enabled': False, 'provider': identity,
        'tools': {name: name in tools for name in current['settings']['tools']}, 'max_tool_rounds': 1},
        protected_key=vault.protect(identity + '-fixture-only-key'))
    current = store.ai_store_provider_evidence(project, admin, current['version'],
        current['credential_version'], current['settings']['model'], catalogue=[model])
    current = store.ai_configure(project, admin, current['version'], current['settings'] | {'model': model})
    current = store.ai_store_provider_evidence(project, admin, current['version'],
        current['credential_version'], model, capabilities={
            'text_generation': True, 'tool_calling': bool(tools), 'streaming': False, 'structured_output': False})
    return store.ai_configure(project, admin, current['version'], current['settings'] | {'enabled': True})


@pytest.mark.parametrize(('original', 'replacement'), PROVIDER_SWITCHES)
@pytest.mark.parametrize('phase', ['queued', 'dispatched'])
def test_all_shipped_provider_switches_fence_old_turns_and_dispatch_only_fresh_keys(ai_chat, original, replacement, phase):
    env, store = ai_chat, ai_chat['store']
    vault = Vault(Fernet.generate_key().decode())
    initial = select_provider(env, vault, original)
    customer = conversation(env)
    cid, principal = customer['conversation']['id'], customer['principal']
    store.chat_send(cid, principal, str(uuid4()), 'What is the return policy?')
    index = store.ai_knowledge_chunks(env['project'], env['admin'])
    calls = []

    def switch():
        changed = select_provider(env, vault, replacement)
        assert changed['credential_version'] == initial['credential_version'] + 1
        assert changed['version'] > initial['version']

    class FixtureAdapter:
        def __init__(self, identity):
            self.identity = identity

        async def generate(self, key, model, messages, **options):
            calls.append((self.identity, key, model))
            assert key == self.identity + '-fixture-only-key'
            if phase == 'dispatched' and len(calls) == 1:
                switch()
            if json.loads(messages[-1].content).get('operation') == 'evidence_check':
                return GenerationResult('{"assessment":"Fixture evidence supports this draft.","supported":true,"issues":[]}', (), Usage(20,10), 'stop')
            return answered(json.loads(messages[-1].content)['passages'][0]['id'])

    embeddings = FixedEmbeddings()
    generation = AIGeneration(store, vault, RetrievalService(store, embeddings), provider_factory=FixtureAdapter)
    worker = AIChatWorker(store, generation)
    if phase == 'queued':
        switch()
    assert not worker.run_once()
    assert calls == ([(original, original + '-fixture-only-key', 'shared-fixture-model')] if phase == 'dispatched' else [])
    assert len(embeddings.calls) == (phase == 'dispatched')
    assert all(row['sender_role'] != 'ai' for row in store.chat_messages(cid, principal, 0, 100))
    assert store.ai_knowledge_chunks(env['project'], env['admin']) == index
    assert store.ai_usage(env['project'], env['admin'])['reserved_requests'] == (phase == 'dispatched')

    store.chat_send(cid, principal, str(uuid4()), 'Please answer with the newly selected provider.')
    assert worker.run_once()
    assert calls[-1] == (replacement, replacement + '-fixture-only-key', 'shared-fixture-model')
    assert len(calls) == 2 + (phase == 'dispatched')
    replies = [row for row in store.chat_messages(cid, principal, 0, 100) if row['sender_role'] == 'ai']
    assert len(replies) == 1 and replies[0]['citations'][0]['id'] == env['chunk']


@pytest.mark.parametrize('switch_during_answer', [False, True])
@pytest.mark.parametrize('content_shape', ['plain', 'thinking'])
@pytest.mark.parametrize('answer_format', ['plain', 'json_fence'])
@pytest.mark.parametrize('tool_name', ['search_knowledge', 'support_link'])
def test_native_mistral_tool_round_uses_grounded_evidence_and_fences_late_provider_switch(ai_chat, switch_during_answer, content_shape, answer_format, tool_name):
    env, calls = ai_chat, []
    store = env['store']
    vault = Vault(Fernet.generate_key().decode())
    select_provider(env, vault, 'mistral', model='ministral-14b-latest', tools=(tool_name,))
    customer, _, job, material = turn(env)
    native_call = {'id': 'call12345', 'type': 'function', 'function': {
        'name': tool_name, 'arguments': '{"query":"return policy"}'}}
    prompt_handles = []
    private_thought = 'Private fixture thought; never publish.'
    native_content = None if content_shape == 'plain' else [{
        'type': 'thinking', 'thinking': [{'type': 'text', 'text': private_thought}],
        'signature': 'fixture-signed-continuation', 'closed': True}]

    def respond(request):
        assert str(request.url) == 'https://api.mistral.ai/v1/chat/completions'
        assert request.headers['authorization'] == 'Bearer mistral-fixture-only-key'
        assert 'x-goog-api-key' not in request.headers
        payload = json.loads(request.content)
        assert payload['model'] == 'ministral-14b-latest'
        calls.append(payload)
        if json.loads(payload['messages'][-1]['content']).get('operation') == 'evidence_check':
            assert len(calls) == 3 and not payload.get('tools')
            assert len(payload['messages']) == 2
            assert private_thought not in json.dumps(payload) and 'fixture-signed-continuation' not in json.dumps(payload)
            message, reason = {'role':'assistant','content':'{"assessment":"Fixture evidence supports this draft.","supported":true,"issues":[]}'}, 'stop'
        elif len(calls) == 1:
            initial = json.loads(payload['messages'][1]['content'])['passages']
            prompt_handles.extend(passage['id'] for passage in initial)
            assert all(re.fullmatch(r'S(?:[1-9]|1[0-9]|20)', handle) for handle in prompt_handles)
            assert env['chunk'] not in json.dumps(payload)
            assert [tool['function']['name'] for tool in payload['tools']] == [tool_name]
            if tool_name == 'support_link':
                native_call['function']['arguments'] = json.dumps(
                    {'passage_id': prompt_handles[0]}, separators=(',', ':'))
            message = {'role': 'assistant', 'content': native_content, 'tool_calls': [native_call]}
            reason = 'tool_calls'
        else:
            assert len(calls) == 2 and not payload.get('tools')
            assert payload['messages'][-2]['content'] == native_content
            assert payload['messages'][-2]['tool_calls'] == [native_call]
            tool = payload['messages'][-1]
            assert tool['role'] == 'tool' and tool['name'] == tool_name and tool['tool_call_id'] == native_call['id']
            passage = json.loads(tool['content'])['passages'][0]
            assert passage['id'] == prompt_handles[0] and passage['text'] == 'Returns are accepted for thirty days.'
            assert env['chunk'] not in json.dumps(payload)
            if tool_name == 'support_link':
                assert json.loads(payload['messages'][-2]['tool_calls'][0]['function']['arguments']) == {'passage_id': prompt_handles[0]}
            if switch_during_answer:
                select_provider(env, vault, 'gemini', model='gemini-3.8-flash')
            text = answered(passage['id']).text
            if answer_format == 'json_fence':
                text = '```json\n' + text + '\n```'
            message, reason = {'role': 'assistant', 'content': text}, 'stop'
        value = {'choices': [{'index': 0, 'message': message, 'finish_reason': reason}],
                 'usage': {'prompt_tokens': 100, 'completion_tokens': 30, 'total_tokens': 130}}
        return httpx.Response(200, headers={'Content-Type': 'application/json'},
                              stream=httpx.ByteStream(json.dumps(value).encode()))

    adapter = MistralProvider(transport=httpx.MockTransport(respond))
    selected = []
    def factory(identity):
        selected.append(identity)
        assert identity == 'mistral'
        return adapter
    embeddings = FixedEmbeddings()
    generation = AIGeneration(store, vault, RetrievalService(store, embeddings), provider_factory=factory)
    if switch_during_answer:
        with pytest.raises(Error) as caught:
            generate(generation, job, material)
        assert caught.value.status == 409
        assert store.ai_commit_reply(job['id'], job['lease_token'], 'Stale answer', [env['chunk']],
                                     material['job']['authority']['knowledge_version']) is None
    else:
        answer = generate(generation, job, material)
        assert answer['citation_ids'] == [env['chunk']]
        committed = store.ai_commit_reply(job['id'], job['lease_token'], answer['body'],
                                         answer['citation_ids'], answer['knowledge_version'])
        assert committed['sender_role'] == 'ai' and committed['citations'][0]['id'] == env['chunk']
        usage = store.ai_usage(env['project'], env['admin'])
        assert usage['reported_input_tokens'] == 300 and usage['reported_output_tokens'] == 90
    assert len(calls) == (2 if switch_during_answer else 3) and selected == ['mistral']
    assert len(embeddings.calls) == (2 if tool_name == 'search_knowledge' else 1)
    history = store.chat_messages(customer['conversation']['id'], customer['principal'], 0, 100)
    assert sum(row['sender_role'] == 'ai' for row in history) == (not switch_during_answer)
    assert 'provider_state' not in json.dumps(history) and 'mistral-fixture-only-key' not in json.dumps(history)
    assert private_thought not in json.dumps(history) and 'fixture-signed-continuation' not in json.dumps(history)
