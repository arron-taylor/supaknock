"""Transient provider continuation survives real SQLite-backed tool rounds."""
from dataclasses import replace
import json
import re

import httpx
import pytest

from feedback.ai_generation import AIGeneration, MAX_CONTEXT_BYTES
from feedback.ai_providers import GeminiProvider, Message, GenerationResult, Usage
from feedback.errors import Error
from test_ai_chat_isolation import ai_chat, turn
from test_ai_tools import SequenceProvider, answered, called, enable_tools, engine, generate


def test_signed_continuation_reaches_next_round_without_entering_answer(ai_chat):
    env = ai_chat
    enable_tools(env, ['search_knowledge'])
    _, _, job, material = turn(env)
    state = b'{"fixture":"opaque-native-provider-continuation"}'
    first = replace(called('search_knowledge', {'query': 'returns'}), provider_state=state)
    class FollowingProvider:
        def __init__(self):
            self.calls = []

        async def generate(self, key, model, messages, **options):
            self.calls.append({'messages': messages, **options})
            if len(self.calls) == 1:
                return first
            if json.loads(messages[-1].content).get('operation') == 'evidence_check':
                assert all(message.provider_state is None for message in messages)
                return GenerationResult('{"assessment":"Fixture support verified.","supported":true,"issues":[]}', (), Usage(20,10), 'stop')
            return answered(json.loads(messages[-1].content)['previous_source_ids'][0])

    adapter = FollowingProvider()
    generation, _ = engine(env, adapter)
    result = generate(generation, job, material)
    assistant = adapter.calls[1]['messages'][-2]
    assert assistant.provider_state == state
    assert assistant.tool_calls == first.tool_calls
    assert 'opaque-native' not in repr(assistant)
    assert 'opaque-native' not in str(result)
    assert result['status'] == 'answered' and result['citation_ids'] == [env['chunk']]
    assert env['store'].ai_usage(env['project'], env['admin'])['reported_output_tokens'] == 25


def test_oversized_continuation_stops_before_tool_execution(ai_chat):
    env = ai_chat
    enable_tools(env, ['search_knowledge'])
    _, _, job, material = turn(env)
    first = replace(called('search_knowledge', {'query': 'returns'}),
                    provider_state=b'x' * MAX_CONTEXT_BYTES)
    adapter = SequenceProvider([first])
    generation, embeddings = engine(env, adapter)
    with pytest.raises(Error) as caught:
        generate(generation, job, material)
    assert caught.value.code == 'invalid_input'
    assert len(adapter.calls) == 1
    assert len(embeddings.calls) == 1  # Only initial retrieval; proposed search never ran.


def test_continuation_counts_towards_conservative_context_budget():
    plain = Message('assistant', 'text')
    state = b'{"opaque":"signed-value"}'
    with_state = replace(plain, provider_state=state)
    assert AIGeneration._context_size([with_state], ()) == AIGeneration._context_size([plain], ()) + len(state)
    with pytest.raises(Error):
        AIGeneration._context_size([replace(plain, provider_state='invalid-string')], ())


@pytest.mark.parametrize('tool_name', ['search_knowledge', 'support_link'])
def test_native_gemini_tool_round_commits_only_grounded_answer(ai_chat, tool_name):
    env, calls = ai_chat, []
    store, pid, admin = env['store'], env['project'], env['admin']
    cfg = store.ai_settings(pid, admin)
    cfg = store.ai_configure(pid, admin, cfg['version'],
        cfg['settings'] | {'enabled': False, 'provider': 'gemini'}, protected_key='protected-fixture-key')
    model = 'gemini-3.8-flash'
    store.ai_store_provider_evidence(pid, admin, cfg['version'], cfg['credential_version'], None, catalogue=[model])
    cfg = store.ai_configure(pid, admin, cfg['version'], cfg['settings'] | {'model': model})
    store.ai_store_provider_evidence(pid, admin, cfg['version'], cfg['credential_version'], model,
        capabilities={'text_generation': True, 'tool_calling': True, 'streaming': False, 'structured_output': False})
    cfg = store.ai_settings(pid, admin)
    store.ai_configure(pid, admin, cfg['version'], cfg['settings'] | {'enabled': True,
        'max_tool_rounds': 1, 'tools': cfg['settings']['tools'] | {tool_name: True}})
    customer, _, job, material = turn(env)
    native_parts = []
    prompt_handles = []

    def respond(request):
        assert request.url.host == 'generativelanguage.googleapis.com'
        assert request.url.path == '/v1beta/models/gemini-3.8-flash:generateContent'
        assert request.headers['x-goog-api-key'] == 'fixture-only-no-network'
        payload = json.loads(request.content)
        calls.append(payload)
        if len(calls) == 3:
            assert 'tools' not in payload and len(payload['contents']) == 1
            assert json.loads(payload['contents'][0]['parts'][0]['text'])['operation'] == 'evidence_check'
            assert 'thoughtSignature' not in json.dumps(payload)
            content = {'role':'model','parts':[{'text':'{"assessment":"Fixture support verified.","supported":true,"issues":[]}'}]}
        elif len(calls) == 1:
            initial = json.loads(payload['contents'][0]['parts'][0]['text'])['passages']
            prompt_handles.extend(passage['id'] for passage in initial)
            assert all(re.fullmatch(r'S(?:[1-9]|1[0-9]|20)', handle) for handle in prompt_handles)
            assert env['chunk'] not in json.dumps(payload)
            assert 'responseMimeType' not in payload['generationConfig']
            assert payload['tools'][0]['functionDeclarations'][0]['name'] == tool_name
            arguments = {'query': 'return policy'} if tool_name == 'search_knowledge' else {'passage_id': prompt_handles[0]}
            native_parts.extend([
                {'text': 'Private fixture thought; never publish.', 'thought': True},
                {'functionCall': {'name': tool_name, 'args': arguments, 'id': 'native-tool'},
                 'thoughtSignature': 'c2lnbmVkLWZpeHR1cmU='}])
            content = {'role': 'model', 'parts': native_parts}
        else:
            assert payload['generationConfig']['responseMimeType'] == 'application/json'
            assert 'tools' not in payload  # Last round cannot propose another tool.
            assert payload['contents'][-2] == {'role': 'model', 'parts': native_parts}
            result = payload['contents'][-1]['parts'][0]['functionResponse']
            assert result['id'] == 'native-tool' and result['name'] == tool_name
            assert result['response']['passages'] == []
            assert result['response']['previous_source_ids'] == [prompt_handles[0]]
            passage = json.loads(payload['contents'][0]['parts'][0]['text'])['passages'][0]
            assert passage['text'] == 'Returns are accepted for thirty days.'
            assert env['chunk'] not in json.dumps(payload)
            if tool_name == 'support_link':
                # Rewriting a signed native call would invalidate continuation.
                assert payload['contents'][-2]['parts'][1]['functionCall']['args'] == {'passage_id': prompt_handles[0]}
            content = {'role': 'model', 'parts': [{'text': answered(passage['id']).text}]}
        value = {'candidates': [{'content': content, 'finishReason': 'STOP'}],
            'usageMetadata': {'promptTokenCount': 100, 'candidatesTokenCount': 30,
                              'thoughtsTokenCount': 20, 'totalTokenCount': 150}}
        return httpx.Response(200, headers={'Content-Type': 'application/json'},
                              stream=httpx.ByteStream(json.dumps(value).encode()))

    adapter = GeminiProvider(transport=httpx.MockTransport(respond))
    generation, _ = engine(env, adapter)
    answer = generate(generation, job, material)
    assert answer['citation_ids'] == [env['chunk']]
    committed = store.ai_commit_reply(job['id'], job['lease_token'], answer['body'],
                                     answer['citation_ids'], answer['knowledge_version'])
    assert committed and len(calls) == 3
    assert committed['citations'][0]['id'] == env['chunk']
    history = json.dumps(store.chat_messages(customer['conversation']['id'], customer['principal'], 0, 100))
    assert 'Private fixture thought' not in history and 'c2lnbmVkLWZpeHR1cmU=' not in history
    assert 'provider_state' not in history
    usage = store.ai_usage(pid, admin)
    assert usage['reported_input_tokens'] == 300 and usage['reported_output_tokens'] == 150
