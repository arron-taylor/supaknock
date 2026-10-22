"""Production Groq translation and durable chat using synthetic local HTTP only."""
import json
import re
import time
from uuid import uuid4

from cryptography.fernet import Fernet
import httpx
import pytest

from feedback.ai_generation import AIGeneration
from feedback.ai_chat import AIChatWorker
from feedback.ai_providers import GroqProvider, ProviderError
from feedback.ai_retrieval import RetrievalService
from feedback.errors import Error
from feedback.security import Vault
from test_ai_chat_isolation import ai_chat, conversation, turn, FixedEmbeddings
from test_ai_mistral_integration import select_provider
from test_ai_tools import answered, generate


MODEL = 'openai/gpt-oss-120b'


def response(message, reason='stop', status=200, *, model=MODEL):
    value = {'model': model, 'choices': [{'index': 0, 'message': message, 'finish_reason': reason}],
             'usage': {'prompt_tokens': 100, 'completion_tokens': 40, 'total_tokens': 140,
                       'completion_tokens_details': {'reasoning_tokens': 10}}}
    return httpx.Response(status, headers={'Content-Type': 'application/json'},
                          stream=httpx.ByteStream(json.dumps(value).encode()))


@pytest.mark.parametrize('switch_during_answer', [False, True])
@pytest.mark.parametrize('answer_format', ['plain', 'json_fence'])
def test_native_groq_grounded_roundtrip_never_exposes_thoughts_and_fences_switch(ai_chat, switch_during_answer, answer_format):
    env, requests = ai_chat, []
    store = env['store']
    vault = Vault(Fernet.generate_key().decode())
    select_provider(env, vault, 'groq', model=MODEL, tools=('search_knowledge',))
    customer, _, job, material = turn(env)
    native_call = {'id': 'groq_call_1', 'type': 'function', 'function': {
        'name': 'search_knowledge', 'arguments': '{"query":"return policy"}'}}
    private_thought = 'Private fixture reasoning, never customer content.'
    prompt_handles = []

    def respond(request):
        assert str(request.url) == 'https://api.groq.com/openai/v1/chat/completions'
        assert request.headers['authorization'] == 'Bearer groq-fixture-only-key'
        assert 'x-goog-api-key' not in request.headers
        payload = json.loads(request.content)
        assert payload['model'] == MODEL and payload['include_reasoning'] is False
        assert payload['max_completion_tokens'] > 0 and 'max_tokens' not in payload
        assert 'reasoning_format' not in payload
        requests.append(payload)
        if json.loads(payload['messages'][-1]['content']).get('operation') == 'evidence_check':
            assert len(requests) == 3 and not payload.get('tools') and len(payload['messages']) == 2
            assert private_thought not in json.dumps(payload)
            return response({'role':'assistant','content':'{"assessment":"Fixture evidence supports this draft.","supported":true,"issues":[]}'})
        if len(requests) == 1:
            prompt_handles.extend(p['id'] for p in json.loads(payload['messages'][1]['content'])['passages'])
            assert all(re.fullmatch(r'S(?:[1-9]|1[0-9]|20)', handle) for handle in prompt_handles)
            assert env['chunk'] not in json.dumps(payload)
            assert [tool['function']['name'] for tool in payload['tools']] == ['search_knowledge']
            assert payload['parallel_tool_calls'] is False
            return response({'role': 'assistant', 'content': None, 'reasoning': private_thought,
                             'tool_calls': [native_call]}, 'tool_calls')
        assert len(requests) == 2 and not payload.get('tools')
        assert payload['messages'][-2] == {'role': 'assistant', 'content': None, 'tool_calls': [native_call]}
        tool = payload['messages'][-1]
        assert tool['role'] == 'tool' and tool['name'] == 'search_knowledge' and tool['tool_call_id'] == native_call['id']
        passage = json.loads(tool['content'])['passages'][0]
        assert passage['id'] == prompt_handles[0] and passage['text'] == 'Returns are accepted for thirty days.'
        assert env['chunk'] not in json.dumps(payload)
        assert private_thought not in json.dumps(payload)
        if switch_during_answer:
            select_provider(env, vault, 'gemini', model='gemini-3.8-flash')
        text = answered(passage['id']).text
        if answer_format == 'json_fence':
            text = '```json\n' + text + '\n```'
        return response({'role': 'assistant', 'content': text, 'reasoning': private_thought})

    native = GroqProvider(transport=httpx.MockTransport(respond))
    def factory(identity):
        assert identity == 'groq'
        return native
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
        assert usage['reported_input_tokens'] == 300 and usage['reported_output_tokens'] == 120
    assert len(requests) == (2 if switch_during_answer else 3) and len(embeddings.calls) == 2
    history = store.chat_messages(customer['conversation']['id'], customer['principal'], 0, 100)
    assert sum(row['sender_role'] == 'ai' for row in history) == (not switch_during_answer)
    for private in ('provider_state', 'groq-fixture-only-key', private_thought):
        assert private not in json.dumps(history)


@pytest.mark.parametrize('violation', ['disabled_tool', 'hosted_tool', 'fallback_model'])
def test_native_groq_protocol_violation_cannot_become_customer_reply_or_action(ai_chat, violation):
    env, requests = ai_chat, []
    store, vault = env['store'], Vault(Fernet.generate_key().decode())
    select_provider(env, vault, 'groq', model=MODEL, tools=('search_knowledge',))
    customer, _, job, material = turn(env)

    def respond(request):
        requests.append(request)
        payload = json.loads(request.content)
        handle = json.loads(payload['messages'][1]['content'])['passages'][0]['id']
        message = {'role': 'assistant', 'content': answered(handle).text}
        reason = 'stop'
        if violation == 'disabled_tool':
            message['tool_calls'] = [{'id': 'forged', 'type': 'function', 'function': {
                'name': 'create_bug_report', 'arguments': '{"title":"Invented","description":"Unauthorized"}'}}]
            reason = 'tool_calls'
        elif violation == 'hosted_tool':
            message['executed_tools'] = [{'type': 'browser_search', 'output': 'Unapproved source'}]
        return response(message, reason, model='openai/gpt-oss-20b' if violation == 'fallback_model' else MODEL)

    native = GroqProvider(transport=httpx.MockTransport(respond))
    generation = AIGeneration(store, vault, RetrievalService(store, FixedEmbeddings()), provider_factory=lambda _: native)
    with pytest.raises(ProviderError, match='tool_not_enabled|invalid_response'):
        generate(generation, job, material)
    assert len(requests) == 1
    assert not store.ai_proposals(customer['conversation']['id'], customer['principal'])['items']
    assert not store.ai_reports(customer['conversation']['id'], customer['principal'])['items']
    assert all(row['sender_role'] != 'ai' for row in store.chat_messages(customer['conversation']['id'], customer['principal'], 0, 100))


def test_unreadable_saved_secret_fails_closed_before_reservation_or_network(ai_chat, caplog):
    env, store = ai_chat, ai_chat['store']
    vault = Vault(Fernet.generate_key().decode())
    select_provider(env, vault, 'groq', model=MODEL)
    customer = conversation(env)
    store.chat_send(customer['conversation']['id'], customer['principal'], str(uuid4()), 'Return policy?')

    class UnreadableVault:
        def reveal(self, protected):
            raise ValueError('fixture-private-corrupt-ciphertext')

    native = GroqProvider(transport=httpx.MockTransport(lambda r: pytest.fail('Unreadable key dispatched')))
    generation = AIGeneration(store, UnreadableVault(), RetrievalService(store, FixedEmbeddings()), provider_factory=lambda _: native)
    assert not AIChatWorker(store, generation).run_once()
    jobs = store.ai_jobs(env['project'], env['admin'])
    assert len(jobs) == 1 and jobs[0]['state'] == 'failed'
    assert store.ai_usage(env['project'], env['admin'])['reserved_requests'] == 0
    history = store.chat_messages(customer['conversation']['id'], customer['principal'], 0, 100)
    assert all(row['sender_role'] != 'ai' for row in history)
    assert 'fixture-private-corrupt-ciphertext' not in json.dumps(jobs) + json.dumps(history) + caplog.text


@pytest.mark.parametrize('failure', ['rate_limit', 'monthly_quota'])
@pytest.mark.parametrize('same_account', [False, True])
def test_native_groq_limit_cools_account_and_only_retries_a_different_verified_account(ai_chat, failure, same_account):
    env, store, requests = ai_chat, ai_chat['store'], []
    pid, admin = env['project'], env['admin']
    vault = Vault(Fernet.generate_key().decode())
    cfg = select_provider(env, vault, 'groq', model=MODEL, tools=('search_knowledge',))
    original = store.ai_provider_keys(pid, admin)['keys'][0]
    spare_secret = 'groq-spare-fixture-only-key'
    pool = store.ai_provider_key_add(pid, admin, cfg['version'], 'Spare key',
        original['quota_group'] if same_account else 'Separate fictional account', vault.protect(spare_secret))
    spare = next(key for key in pool['keys'] if key['id'] != original['id'])
    snapshot = store.ai_provider_snapshot(pid, admin, pool['version'], key_id=spare['id'])
    store.ai_store_provider_evidence(pid, admin, snapshot['version'], snapshot['credential_version'], MODEL,
        catalogue=[MODEL], capabilities={'text_generation': True, 'tool_calling': True,
            'streaming': False, 'structured_output': False}, key_id=spare['id'], key_version=snapshot['key_version'],
        health_epoch=snapshot['health_epoch'], group_epoch=snapshot['group_epoch'])
    customer = conversation(env)
    cid, principal = customer['conversation']['id'], customer['principal']
    store.chat_send(cid, principal, str(uuid4()), 'Return policy?')
    call = {'id': 'retry_tool_1', 'type': 'function', 'function': {
        'name': 'search_knowledge', 'arguments': '{"query":"return policy"}'}}

    def respond(request):
        payload = json.loads(request.content)
        key = request.headers['authorization'].removeprefix('Bearer ')
        assert key in ('groq-fixture-only-key', spare_secret) and payload['model'] == MODEL
        assert str(request.url) == 'https://api.groq.com/openai/v1/chat/completions'
        requests.append((key, payload))
        if len(requests) == 1:
            body = {'error': {'code': 'blocked_api_access' if failure == 'monthly_quota' else 'rate_limit_exceeded',
                              'message': 'Unretained private fixture ' + key}}
            return httpx.Response(400 if failure == 'monthly_quota' else 429,
                headers={'Content-Type': 'application/json', 'Retry-After': '7200'},
                stream=httpx.ByteStream(json.dumps(body).encode()))
        assert not same_account and key != requests[0][0]
        if json.loads(payload['messages'][-1]['content']).get('operation') == 'evidence_check':
            assert len(requests) == 4 and key == requests[1][0] and not payload.get('tools')
            assert len(payload['messages']) == 2
            return response({'role':'assistant','content':'{"assessment":"Fixture evidence supports this draft.","supported":true,"issues":[]}'})
        if len(requests) == 2:
            assert not any(message['role'] == 'tool' for message in payload['messages'])
            return response({'role': 'assistant', 'content': None, 'tool_calls': [call]}, 'tool_calls')
        assert len(requests) == 3 and key == requests[1][0] and not payload.get('tools')
        assert payload['messages'][-1]['tool_call_id'] == call['id']
        passage = json.loads(payload['messages'][-1]['content'])['passages'][0]
        assert re.fullmatch(r'S(?:[1-9]|1[0-9]|20)', passage['id'])
        assert env['chunk'] not in json.dumps(payload)
        return response({'role': 'assistant', 'content': answered(passage['id']).text})

    native = GroqProvider(transport=httpx.MockTransport(respond))
    generation = AIGeneration(store, vault, RetrievalService(store, FixedEmbeddings()), provider_factory=lambda _: native)
    assert not AIChatWorker(store, generation).run_once()
    assert len(requests) == 1  # The adapter and first worker attempt never retry inline.
    jobs = store.ai_jobs(pid, admin)
    pool = store.ai_provider_keys(pid, admin)
    cooling = [key for key in pool['keys'] if key['status'] == 'cooling_down']
    assert len(cooling) == (2 if same_account else 1)
    assert all(key['reason'] == ('quota_exhausted' if failure == 'monthly_quota' else 'rate_limited') for key in cooling)
    if same_account:
        # This cooldown exceeds the bounded customer-turn wait, so hand off.
        assert jobs[0]['state'] == 'failed'
        assert store.chat_conversation(cid, principal)['support_mode'] == 'waiting_human'
    else:
        assert jobs[0]['state'] == 'queued' and jobs[0]['next_attempt'] < time.time() + 300
        job = store.ai_claim_job(jobs[0]['next_attempt'] + .01, lease_seconds=120, kinds=('turn',))
        assert job is not None and job['attempt_count'] == 2
        material = store.ai_job_material(job['id'], job['lease_token'])
        result = generate(generation, job, material)
        assert store.ai_commit_reply(job['id'], job['lease_token'], result['body'], result['citation_ids'], result['knowledge_version'])
        assert len(requests) == 4
    history = store.chat_messages(cid, principal, 0, 100)
    assert sum(row['sender_role'] == 'ai' for row in history) == (not same_account)
    usage = store.ai_usage(pid, admin)
    assert usage['reserved_requests'] == (1 if same_account else 4)
    assert usage['reported_input_tokens'] == (0 if same_account else 300)
    public = json.dumps(history) + json.dumps(pool) + json.dumps(store.ai_jobs(pid, admin))
    assert 'groq-fixture-only-key' not in public and spare_secret not in public
