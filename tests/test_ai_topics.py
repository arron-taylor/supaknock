"""Independent decision fixtures and real SQLite authority/race checks."""
import asyncio
from dataclasses import replace
import json
import math
import time
from uuid import uuid4

import pytest

from feedback.ai_chat import AIChatWorker
from feedback.ai_decision_providers import DecisionResult, decision_provider, validate_decision
from feedback.ai_generation import AIGeneration
from feedback.ai_providers import ProviderError, Usage
from feedback.ai_retrieval import RetrievalService
from feedback.ai_topic_service import TopicGate, TopicSettingsService
from feedback.ai_topics import default_topic_policy, probability_outcome, topic_context, validate_topic_policy
from feedback.errors import Error
from feedback.storage.sqlite import SQLiteStorage
from test_ai_chat import Adapter, Embeddings, Vault
from test_ai_chat_isolation import ai_chat, configure, conversation, turn
from test_ai_settings_api import ai

MODEL = 'typesafe/jev-1.13.0'


def send(env, customer, body='How long do I have to return this?'):
    return env['store'].chat_send(customer['conversation']['id'], customer['principal'], str(uuid4()), body)


class Decision:
    transport_verified = True
    def __init__(self, probability=0.99, error=None, hook=None):
        self.probability, self.error, self.hook, self.calls = probability, error, hook, []
    async def list_models(self, key):
        return (MODEL,)
    async def classify(self, key, model, **data):
        self.calls.append(data)
        if self.hook:
            self.hook()
        if self.error:
            raise self.error
        return DecisionResult(self.probability, model, Usage(20, 0, 20))


class TopicVault(Vault):
    def reveal(self, protected):
        return 'decision-fixture' if protected == 'protected-decision-fixture' else super().reveal(protected)


def ready_topics(env, decision=None, *, preset='balanced', reuse=False):
    s, p, admin = env['store'], env['project'], env['admin']
    service = TopicSettingsService(s, TopicVault(), provider_factory=lambda _: decision or Decision())
    topic = service.read(p, admin)
    topic = service.save(p, admin, topic['version'], topic['policy'] | {
        'model': MODEL, 'scope': 'Support for the Acorn notes app only.',
        'public_summary': 'Acorn support', 'preset': preset,
        'refusal_messages': ['I can help with {scope}.', 'Please ask about {scope}.']})
    key = s.ai_provider_keys(p, admin)['keys'][0]
    topic = s.ai_decision_key_add(p, admin, topic['version'], 'Decision', 'Default account',
        None if reuse else 'protected-decision-fixture', reuse_key_id=key['id'] if reuse else None)
    key = topic['keys'][0]
    topic = s.ai_decision_catalogue(p, admin, topic['version'], key['id'], key['version'], [MODEL])
    result = asyncio.run(service.test(p, admin, topic['version'], key['id'], key['version'], '', probe=True))
    assert result['probability'] is not None
    topic = service.save(p, admin, topic['version'], topic['policy'] | {'enabled': True})
    return topic, service


def engine(env, decision):
    class Counted(Embeddings):
        calls = 0
        def embed_queries(self, texts):
            self.calls += 1
            return super().embed_queries(texts)
    embeddings, answers = Counted(), Adapter()
    generation = AIGeneration(env['store'], Vault(), RetrievalService(env['store'], embeddings),
        provider_factory=lambda _: answers, topic_gate=TopicGate(env['store'], TopicVault(), provider_factory=lambda _: decision))
    return generation, embeddings, answers


@pytest.mark.parametrize('value', [None, True, '0.9', float('nan'), float('inf'), -0.01, 1.01])
def test_invalid_probabilities_never_allow(value):
    with pytest.raises((ProviderError, Error)):
        validate_decision(DecisionResult(value, MODEL, Usage()), MODEL)


def test_presets_and_context_bounds():
    assert probability_outcome(0.9, 'balanced') == 'allow'
    assert probability_outcome(0.9, 'strict') == 'clarify'
    assert probability_outcome(0.1, 'strict') == 'refuse'
    assert topic_context([{'sender_role':'customer','body':'é'*5000}]) == ([], True)
    context, limited = topic_context([{'sender_role':'ai','body':str(i)} for i in range(8)])
    assert not limited and [m['text'] for m in context] == ['4','5','6','7']


@pytest.mark.parametrize('outcome,probability', [('allow',0.99),('refuse',0.01),('clarify',0.5)])
def test_durable_gate_precedes_embedding_and_generation(ai_chat, outcome, probability):
    env = ai_chat
    ready_topics(env)
    decision = Decision(probability)
    generation, embeddings, answers = engine(env, decision)
    customer = conversation(env)
    send(env, customer, body='How do I export Acorn notes?')
    assert AIChatWorker(env['store'], generation).run_once()
    assert len(decision.calls) == 1
    assert embeddings.calls == (1 if outcome == 'allow' else 0)
    assert len(answers.calls) == (2 if outcome == 'allow' else 0)
    assert env['store'].ai_usage(env['project'], env['admin'])['reserved_requests'] == (4 if outcome == 'allow' else 2)
    messages = env['store'].chat_messages(customer['conversation']['id'], customer['principal'], 0, 100)
    assert messages[-1]['sender_role'] == 'ai'
    assert messages[-1]['citations'] if outcome == 'allow' else not messages[-1]['citations']
    assert env['store'].chat_conversation(customer['conversation']['id'], customer['principal'])['support_mode'] == 'ai'
    with env['store'].transaction() as c:
        receipt = c.execute('SELECT * FROM ai_topic_receipts').fetchone()
        assert receipt['outcome'] == outcome and receipt['trigger_sequence'] == messages[0]['sequence']
        assert 'body' not in json.loads(c.execute("SELECT payload FROM request_events WHERE type='chat.message_created' ORDER BY rowid DESC LIMIT 1").fetchone()[0])['message']


def test_disabled_gate_has_no_decision_calls(ai_chat):
    decision = Decision()
    generation, embeddings, answers = engine(ai_chat, decision)
    customer = conversation(ai_chat)
    send(ai_chat, customer)
    assert AIChatWorker(ai_chat['store'], generation).run_once()
    assert not decision.calls and len(answers.calls) == 2


@pytest.mark.parametrize('error', ['invalid_response', 'timeout', 'access_denied', 'invalid_credentials', 'rate_limited'])
def test_decision_failure_is_unavailable_not_off_topic_or_fail_open(ai_chat, error):
    ready_topics(ai_chat)
    decision = Decision(error=ProviderError(error))
    generation, embeddings, answers = engine(ai_chat, decision)
    customer = conversation(ai_chat)
    send(ai_chat, customer)
    assert AIChatWorker(ai_chat['store'], generation).run_once()
    assert embeddings.calls == 0 and not answers.calls
    messages = ai_chat['store'].chat_messages(customer['conversation']['id'], customer['principal'], 0, 100)
    assert 'try again later' in messages[-1]['body']
    assert ai_chat['store'].ai_topic_settings(ai_chat['project'], ai_chat['admin'])['policy']['enabled']


@pytest.mark.parametrize('question,expected', [('Hello!',0), ('Hi, write a workout plan',1)])
def test_local_greeting_cannot_allow_mixed_requests(ai_chat, question, expected):
    ready_topics(ai_chat)
    decision = Decision(0.01)
    generation, embeddings, answers = engine(ai_chat, decision)
    send(ai_chat, conversation(ai_chat), body=question)
    assert AIChatWorker(ai_chat['store'], generation).run_once()
    assert len(decision.calls) == expected and embeddings.calls == 0 and not answers.calls


@pytest.mark.parametrize('change', ['policy','human','key','source','logout'])
def test_decision_late_work_is_fenced(ai_chat, change):
    env = ai_chat
    topic, service = ready_topics(env)
    customer = conversation(env)
    def hook():
        s,p,a = env['store'],env['project'],env['admin']
        if change == 'policy': service.save(p,a,topic['version'],topic['policy']|{'scope':'Acorn billing only'})
        elif change == 'human':
            cv=s.chat_conversation(customer['conversation']['id'],a)
            s.chat_support_mode(cv['id'],a,'human',cv['mode_version'])
        elif change == 'key': service.remove_key(p,a,topic['version'],topic['keys'][0]['id'],topic['keys'][0]['version'])
        elif change == 'source': s.ai_source_update(p,a,env['source']['id'],env['source']['version'],active=False,approved=False)
        else:
            with s.transaction(True) as c:c.execute('UPDATE chat_sessions SET expires_at=0')
    decision = Decision(hook=hook)
    generation, embeddings, answers = engine(env, decision)
    send(env,customer)
    assert not AIChatWorker(env['store'],generation).run_once()
    assert len(decision.calls)==1 and embeddings.calls==0 and not answers.calls
    with env['store'].transaction() as c: assert c.execute("SELECT count(*) FROM chat_messages WHERE sender='ai'").fetchone()[0]==0


def test_refusal_receipt_idempotent_and_cannot_be_arbitrary_uncited_reply(ai_chat):
    topic,_=ready_topics(ai_chat)
    customer,message,job,material=turn(ai_chat)
    generation,embeddings,answers=engine(ai_chat,Decision(0.01))
    result=asyncio.run(generation.generate(job['id'],job['lease_token'],message['body']))
    s=ai_chat['store'];args=(job['id'],job['lease_token'],result['body'],[],result['knowledge_version'])
    with pytest.raises(Error):s.ai_commit_reply(*args)
    with pytest.raises(Error):s.ai_commit_reply(job['id'],job['lease_token'],'Made up reply',[],result['knowledge_version'],policy_outcome='refuse')
    first=s.ai_commit_reply(*args,policy_outcome='refuse')
    assert s.ai_commit_reply(*args,policy_outcome='refuse')['id']==first['id']
    assert SQLiteStorage(s.path,s.base_url).ai_topic_settings(ai_chat['project'],ai_chat['admin'])['policy']==topic['policy']


def test_generation_provider_switch_preserves_explicit_decision_role(ai_chat):
    topic,_=ready_topics(ai_chat,reuse=True)
    configure(ai_chat, enabled=False,provider='mistral')
    saved=ai_chat['store'].ai_topic_settings(ai_chat['project'],ai_chat['admin'])
    assert saved['keys'][0]['verified'] and not saved['keys'][0]['shared_with_generation']
    assert not ai_chat['store'].ai_provider_keys(ai_chat['project'],ai_chat['admin'])['keys']


def test_real_kodekey_adapter_fails_before_dispatch():
    adapter=decision_provider('kodekey')
    assert not adapter.transport_verified
    with pytest.raises(ProviderError,match='decision_transport_unavailable'):
        asyncio.run(adapter.classify('fixture',MODEL,policy={},question='synthetic',context=[]))


def test_decision_only_preview_uses_same_gate_without_retrieval(ai_chat):
    from feedback.ai_authoring import AuthoringWorker
    ready_topics(ai_chat)
    s,p,a=ai_chat['store'],ai_chat['project'],ai_chat['admin']
    config=s.ai_settings(p,a)
    run=s.ai_authoring_begin(p,a,str(uuid4()),'preview',config['version'],query='Write a workout plan')
    generation,embeddings,answers=engine(ai_chat,Decision(0.01))
    assert AuthoringWorker(s,generation).run_once()
    completed=s.ai_authoring_run(p,a,run['id'])
    assert completed['result']['status']=='refuse' and not completed['result']['citation_ids']
    assert embeddings.calls==0 and not answers.calls
    with s.transaction() as c:assert c.execute('SELECT count(*) FROM chat_messages').fetchone()[0]==0


def test_topic_context_uses_immediate_whole_exchange_and_marks_oversize(ai_chat):
    ready_topics(ai_chat)
    customer=conversation(ai_chat)
    s=ai_chat['store']
    # Simulate a committed earlier human exchange before returning to AI.
    cv=s.chat_conversation(customer['conversation']['id'],ai_chat['admin'])
    s.chat_support_mode(cv['id'],ai_chat['admin'],'human',cv['mode_version'])
    send(ai_chat,customer,body='How do I export notes?')
    s.chat_send(cv['id'],ai_chat['admin'],str(uuid4()),'Export steps are in the Acorn help page.')
    cv=s.chat_conversation(cv['id'],customer['principal'])
    s.chat_support_mode(cv['id'],customer['principal'],'ai',cv['mode_version'])
    decision=Decision();generation,embeddings,answers=engine(ai_chat,decision)
    send(ai_chat,customer,body='What about on iPhone?')
    assert AIChatWorker(s,generation).run_once()
    assert [item['speaker'] for item in decision.calls[0]['context']]==['customer','admin']
    assert decision.calls[0]['context'][0]['text']=='How do I export notes?'


def test_unverified_decision_capability_cannot_enable_or_dispatch(ai_chat):
    s,p,a=ai_chat['store'],ai_chat['project'],ai_chat['admin']
    topic=s.ai_topic_settings(p,a)
    policy=topic['policy']|{'enabled':True,'model':MODEL,'scope':'Acorn support','public_summary':'Acorn'}
    with pytest.raises(Error,match='Verify an exact decision model'):s.ai_topic_configure(p,a,topic['version'],policy)
    customer,message,job,material=turn(ai_chat)
    assert s.ai_topic_require_allow(job['id'],job['lease_token']) is None


def test_new_role_keeps_99_cap_for_same_provider(ai_chat):
    s,p,a=ai_chat['store'],ai_chat['project'],ai_chat['admin']
    topic=s.ai_topic_settings(p,a)
    for i in range(98):topic=s.ai_decision_key_add(p,a,topic['version'],'Decision '+str(i),'Default account','protected-fixture')
    assert len(topic['keys'])==98 and len(s.ai_provider_keys(p,a)['keys'])==1
    with pytest.raises(Error,match='99 provider keys'):s.ai_decision_key_add(p,a,topic['version'],'Excess','Default account','protected-fixture')


def test_decision_limit_shares_generation_allowance(ai_chat):
    topic,service=ready_topics(ai_chat)
    configure(ai_chat,daily_request_limit=1)
    key=topic['keys'][0]
    with pytest.raises(Error,match='usage allowance'):
        asyncio.run(service.test(ai_chat['project'],ai_chat['admin'],topic['version'],key['id'],key['version'],'Acorn export?'))


def test_group_cooldown_blocks_both_roles_and_recovers_exclusively(ai_chat):
    topic,_=ready_topics(ai_chat)
    s,p,a=ai_chat['store'],ai_chat['project'],ai_chat['admin']
    customer,message,job,material=turn(ai_chat)
    claim=s.ai_topic_claim(job['id'],job['lease_token']);assert claim['available']
    index=material['settings']['max_tool_rounds']+1
    assert s.ai_reserve_usage(job['id'],job['lease_token'],200,call_index=index)
    assert s.ai_topic_before_dispatch(job['id'],job['lease_token'],200)
    receipt=s.ai_topic_record(job['id'],job['lease_token'],'a'*64,error='rate_limited',outcome='rate_limited',retry_after=60)
    assert receipt['outcome']=='unavailable'
    assert not s.ai_provider_claim(job['id'],job['lease_token'])['available']
    snap=s.ai_decision_snapshot(p,a,topic['version'],topic['keys'][0]['id'],topic['keys'][0]['version'])
    assert snap['group']['cooldown_until']>time.time()


def test_native_admin_api_default_off_auth_secrets_conflicts_and_blocked_probe(ai):
    path=ai['url']+'/topics';admin=ai['admin']
    for client in [ai['anon'],ai['user'],ai['trusted'],ai['manager']]:
        assert client.get(path).status_code in (401,403)
        assert client.get(path+'/decisions').status_code in (401,403)
    assert admin.get(path+'/decisions').json()['items']==[]
    topic=admin.get(path).json();assert topic['policy']==default_topic_policy()
    response=admin.put(path,json={'version':topic['version'],'policy':topic['policy']|{'model':MODEL,'scope':'Acorn support','public_summary':'Acorn'}})
    assert response.status_code==200,response.text
    topic=response.json()
    assert admin.put(path,json={'version':0,'policy':topic['policy']}).status_code==409
    response=admin.post(path+'/keys',json={'version':topic['version'],'label':'Decision','key':'secret-fixture-decision'})
    assert response.status_code==200,response.text
    assert 'secret-fixture-decision' not in response.text
    topic=response.json();key=topic['keys'][0]
    action={'version':topic['version'],'key_version':key['version']}
    response=admin.post(path+'/keys/'+key['id']+'/probe',json=action)
    assert response.status_code==409 and response.json()['error']['code']=='ai_decision_transport_unavailable'
    response=admin.put(path,json={'version':topic['version'],'policy':topic['policy']|{'enabled':True}})
    assert response.status_code==409
    assert ai['storage'].ai_usage(ai['p']['id'],ai['principal'])['reserved_requests']==0
