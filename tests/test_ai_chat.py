"""Durable chat worker integration with the real engine and fixture providers."""
import asyncio
import json
import threading
import time
from uuid import uuid4

import pytest

from feedback.ai_chat import AIChatWorker
from feedback.ai_generation import AIGeneration
from feedback.ai_providers import GenerationResult, ProviderError, Usage
from feedback.ai_retrieval import RetrievalService
from feedback.storage.sqlite import SQLiteStorage
from test_ai_chat_isolation import ai_chat, configure, conversation
from test_ai_authoring import authoring
from test_chat import ORIGIN, enable, visitor


class Embeddings:
    def embed_queries(self, texts):
        return [[1.0] + [0.0] * 767 for _ in texts]


class Vault:
    def reveal(self, protected):
        assert protected == 'protected-fixture-key'
        return 'fixture-key'


class Adapter:
    def __init__(self):
        self.calls = []
        self.failure = None
        self.hook = None
        self.delay = 0
        self.started = threading.Event()
        self.cancelled = threading.Event()

    async def generate(self, key, model, messages, **kwargs):
        data = json.loads(messages[-1].content)
        self.calls.append(data)
        self.started.set()
        if self.hook:
            hook, self.hook = self.hook, None
            hook()
        if self.failure:
            raise self.failure
        try:
            await asyncio.sleep(self.delay)
        except asyncio.CancelledError:
            self.cancelled.set()
            raise
        if data.get('operation') == 'evidence_check':
            return GenerationResult('{"assessment":"Fixture evidence supports this draft.","supported":true,"issues":[]}', (), Usage(20, 10, 30), 'stop')
        return GenerationResult(json.dumps({'body': 'Returns are accepted for thirty days.',
            'status': 'answered', 'citation_ids': [data['passages'][0]['id']]}), (), Usage(20, 10, 30), 'stop')


def worker(env, adapter, storage=None):
    storage = storage or env['store']
    engine = AIGeneration(storage, Vault(), RetrievalService(storage, Embeddings()), provider_factory=lambda _: adapter)
    return AIChatWorker(storage, engine)


def send(env, customer):
    return env['store'].chat_send(customer['conversation']['id'], customer['principal'], str(uuid4()), 'What is the return policy?')


def jobs(env):
    return env['store'].ai_jobs(env['project'], env['admin'])


def test_worker_commits_citations_usage_and_customer_unread_after_restart(ai_chat):
    env, adapter = ai_chat, Adapter()
    customer = conversation(env)
    send(env, customer)
    reopened = SQLiteStorage(env['store'].path, env['store'].base_url)
    resumed = worker(env, adapter, reopened)
    assert resumed.run_once()
    assert not resumed.run_once()
    messages = reopened.chat_messages(customer['conversation']['id'], customer['principal'], 0, 100)
    assert len(messages) == 2 and messages[-1]['sender_role'] == 'ai'
    assert messages[-1]['citations'][0]['id'] == env['chunk']
    assert reopened.chat_conversation(customer['conversation']['id'], customer['principal'])['unread_count'] == 1
    usage = reopened.ai_usage(env['project'], env['admin'])
    assert usage['reserved_requests'] == 2 and usage['reported_input_tokens'] == 40
    assert len(adapter.calls) == 2
    assert 'customer_id' not in json.dumps(adapter.calls) and '203.0.113.8' not in json.dumps(adapter.calls)


def test_retry_after_is_honored_and_three_failures_request_human_once(ai_chat, monkeypatch):
    env, adapter = ai_chat, Adapter()
    adapter.failure = ProviderError('rate_limited', retryable=True, retry_after=60)
    customer = conversation(env)
    send(env, customer)
    runner = worker(env, adapter)
    assert not runner.run_once()
    assert jobs(env)[0]['next_attempt'] >= time.time() + 59
    clock = [time.time()]
    monkeypatch.setattr(time, 'time', lambda: clock[0])
    for attempt in range(1, 3):
        pending = jobs(env)[0]
        account = env['store'].ai_provider_keys(env['project'], env['admin'])['groups'][0]
        assert pending['state'] == 'queued' and pending['next_attempt'] >= account['retry_at']
        assert not runner.run_once() and len(adapter.calls) == attempt
        clock[0] = pending['next_attempt'] + 0.001
        assert not runner.run_once()
    assert jobs(env)[0]['state'] == 'failed' and jobs(env)[0]['attempt_count'] == 3
    assert env['store'].chat_conversation(customer['conversation']['id'], customer['principal'])['support_mode'] == 'waiting_human'
    with env['store'].transaction() as c:
        assert c.execute('SELECT count(*) FROM admin_notifications').fetchone()[0] == 1
        assert c.execute("SELECT count(*) FROM chat_messages WHERE sender='ai'").fetchone()[0] == 0
    assert len(adapter.calls) == 3
    assert env['store'].ai_usage(env['project'], env['admin'])['reserved_requests'] == 3



def test_long_provider_cooldown_hands_off_once_without_waiting_or_paid_retry(ai_chat):
    env, adapter = ai_chat, Adapter()
    adapter.failure = ProviderError('rate_limited', retryable=True, retry_after=900)
    customer = conversation(env)
    send(env, customer)
    runner = worker(env, adapter)
    assert not runner.run_once()
    job = jobs(env)[0]
    assert job['state'] == 'failed' and job['attempt_count'] == 1
    assert job['next_attempt'] >= time.time() + 890
    assert not runner.run_once() and len(adapter.calls) == 1
    assert env['store'].chat_conversation(customer['conversation']['id'], customer['principal'])['support_mode'] == 'waiting_human'
    assert env['store'].ai_usage(env['project'], env['admin'])['reserved_requests'] == 1
    pool = env['store'].ai_provider_keys(env['project'], env['admin'])
    assert pool['keys'][0]['eligible'] and pool['keys'][0]['status'] == 'cooling_down'
    assert env['store'].ai_settings(env['project'], env['admin'])['settings']['enabled']
    with env['store'].transaction() as c:
        assert c.execute('SELECT count(*) FROM admin_notifications').fetchone()[0] == 1
        assert c.execute("SELECT count(*) FROM chat_messages WHERE sender='ai'").fetchone()[0] == 0

def test_exhausted_allowance_never_calls_provider_and_preserves_message(ai_chat):
    env, adapter = ai_chat, Adapter()
    configure(env, daily_token_limit=1)
    customer = conversation(env)
    original = send(env, customer)
    assert not worker(env, adapter).run_once()
    assert adapter.calls == [] and jobs(env)[0]['error'] == 'budget_exhausted'
    messages = env['store'].chat_messages(customer['conversation']['id'], customer['principal'], 0, 100)
    assert [message['id'] for message in messages] == [original['id']]


def test_shutdown_cancels_provider_without_refunding_uncertain_usage(ai_chat):
    env, adapter = ai_chat, Adapter()
    adapter.delay = 3600
    customer = conversation(env)
    send(env, customer)
    runner = worker(env, adapter)
    runner.start()
    assert adapter.started.wait(5)
    runner.stop()
    assert adapter.cancelled.is_set()
    assert not runner._thread.is_alive()
    assert jobs(env)[0]['state'] == 'queued'
    usage = env['store'].ai_usage(env['project'], env['admin'])
    assert usage['reserved_requests'] == 1 and usage['reserved_tokens'] > 0
    assert len(env['store'].chat_messages(customer['conversation']['id'], customer['principal'], 0, 100)) == 1


def test_newer_customer_question_during_call_discards_old_reply(ai_chat):
    env, adapter = ai_chat, Adapter()
    customer = conversation(env)
    send(env, customer)
    adapter.hook = lambda: send(env, customer)
    runner = worker(env, adapter)
    assert not runner.run_once()
    assert runner.run_once()
    messages = env['store'].chat_messages(customer['conversation']['id'], customer['principal'], 0, 100)
    assert [row['sender_role'] for row in messages] == ['customer', 'customer', 'ai']


def test_access_loss_disables_exact_model_and_hands_off_pending_turns(ai_chat):
    env, adapter = ai_chat, Adapter()
    adapter.failure = ProviderError('access_denied')
    first, second = conversation(env), conversation(env)
    send(env, first)
    send(env, second)
    assert not worker(env, adapter).run_once()
    settings = env['store'].ai_settings(env['project'], env['admin'])
    assert not settings['settings']['enabled'] and not settings['capabilities']
    assert settings['settings']['model'] in settings['catalogue']
    key = env['store'].ai_provider_keys(env['project'], env['admin'])['keys'][0]
    assert not key['eligible'] and key['reason'] == 'access_denied'
    assert all(job['state'] == 'failed' for job in jobs(env) if job['kind'] == 'turn')
    for customer in (first, second):
        assert env['store'].chat_conversation(customer['conversation']['id'], customer['principal'])['support_mode'] == 'waiting_human'
    assert len(adapter.calls) == 1
    assert env['store'].ai_usage(env['project'], env['admin'])['reserved_requests'] == 1


def test_stale_access_loss_does_not_disable_replacement_config(ai_chat):
    env = ai_chat
    customer = conversation(env)
    send(env, customer)
    job = env['store'].ai_claim_job(time.time(), kinds=('turn',))
    updated = configure(env, soul='A changed personality')
    assert not env['store'].ai_provider_access_lost(job['id'], job['lease_token'])
    current = env['store'].ai_settings(env['project'], env['admin'])
    assert current == updated and current['settings']['enabled']


def test_http_and_websocket_publish_explicit_ai_sender_and_reconnect_log(authoring, monkeypatch):
    ai = authoring
    enable(ai)
    cfg = ai['s'].ai_settings(ai['p']['id'], ai['principal'])
    ai['s'].ai_configure(ai['p']['id'], ai['principal'], cfg['version'], cfg['settings'] | {'enabled': True})
    customer, cv, _ = visitor(ai)
    config = customer.get('/api/v1/chat/projects/' + ai['p']['id'] + '/config').json()
    assert config['ai_available'] and 'model' not in config
    mode_url = '/api/v1/chat/conversations/' + cv['id'] + '/support-mode'
    preflight = customer.options(mode_url, headers={'Access-Control-Request-Method': 'PUT',
        'Access-Control-Request-Headers': 'Content-Type,X-Chat-Token'})
    assert preflight.status_code == 204 and 'PUT' in preflight.headers['access-control-allow-methods']
    assert customer.put(mode_url, json={'mode': 'human', 'mode_version': 1}).status_code == 403
    changed = customer.put(mode_url, json={'mode': 'ai', 'mode_version': 1})
    assert changed.status_code == 200 and changed.json()['mode_version'] == 2
    assert customer.put(mode_url, json={'mode': 'waiting_human', 'mode_version': 1}).status_code == 409
    assert ai['admin'].put(mode_url, headers={'X-CSRF-Token': ''}, json={'mode': 'human', 'mode_version': 2}).status_code == 403
    ticket_url = '/api/v1/chat/tickets'
    ticket = customer.post(ticket_url, json={'conversation_id': cv['id']}).json()['ticket']
    with customer.websocket_connect('/api/v1/chat/ws', headers={'Origin': ORIGIN}) as ws:
        ws.send_json({'ticket': ticket})
        assert ws.receive_json()['type'] == 'ready'
        assert ws.receive_json()['conversation']['support_mode'] == 'ai'
        ws.send_json({'type': 'message', 'message': {'client_message_id': str(uuid4()), 'body': 'When should I restart?'}})
        for _ in range(8):
            frame = ws.receive_json()
            if frame['type'] == 'ack':
                break
        else:
            pytest.fail('Customer message acknowledgement was not delivered')
        store=ai['app'].state.storage
        held=store.ai_claim_job(time.time(),120,kinds=('turn',))
        for stage in ('reading','checking'):
            assert store.ai_job_progress(held['id'],held['lease_token'],stage)
            assert customer.get('/api/v1/chat/conversations/'+cv['id']).json()['ai_progress']==stage
            for _ in range(10):
                frame=ws.receive_json()
                if frame['type']=='state' and frame['conversation']['ai_progress']==stage:
                    assert frame['conversation']['ai_trigger_sequence']==1
                    break
            else:pytest.fail('Progress change was not delivered over the existing socket')
        with monkeypatch.context() as patch:
            patch.setattr(store,'ai_claim_job',lambda *a,**kw:held)
            assert ai['app'].state.ai_chat_worker.run_once()
        for _ in range(10):
            frame = ws.receive_json()
            if frame['type'] == 'message' and frame['message']['sender_role'] == 'ai':
                message = frame['message']
                break
        else:
            pytest.fail('Committed AI message was not delivered')
        assert message['sender'] == 'ai' and message['citations'][0]['url'] is None
        assert 'ip_address' not in message
    http_messages = customer.get('/api/v1/chat/conversations/' + cv['id'] + '/messages').json()['items']
    assert http_messages[-1]['id'] == message['id'] and http_messages[-1]['sender_role'] == 'ai'
    ticket = customer.post(ticket_url, json={'conversation_id': cv['id']}).json()['ticket']
    with customer.websocket_connect('/api/v1/chat/ws', headers={'Origin': ORIGIN}) as ws:
        ws.send_json({'ticket': ticket, 'after': 1})
        assert ws.receive_json()['type'] == 'ready'
        state=ws.receive_json()['conversation']
        assert state['ai_status'] == 'succeeded' and state['ai_progress'] is None
        assert ws.receive_json()['message']['id'] == message['id']


def test_late_worker_restart_fails_unpaid_wait_and_notifies_human_once(ai_chat, monkeypatch):
    from datetime import datetime
    env, adapter = ai_chat, Adapter()
    first = conversation(env)
    send(env, first)
    held = env['store'].ai_claim_job(time.time(), kinds=('turn',))
    assert env['store'].ai_provider_claim(held['id'], held['lease_token'])['available']
    customer = conversation(env)
    send(env, customer)
    runner = worker(env, adapter)
    assert not runner.run_once()
    waiting = next(job for job in jobs(env) if job['id'] != held['id'])
    assert waiting['state'] == 'queued' and waiting['attempt_count'] == 0
    assert env['store'].ai_finish_job(held['id'], held['lease_token'], 'cancelled')
    resume_time = datetime.fromisoformat(waiting['created_at'].replace('Z', '+00:00')).timestamp() + 600
    monkeypatch.setattr(time, 'time', lambda: resume_time)
    reopened = SQLiteStorage(env['store'].path, env['store'].base_url)
    resumed = worker(env, adapter, reopened)
    assert not resumed.run_once() and not resumed.run_once()
    final = next(job for job in reopened.ai_jobs(env['project'], env['admin']) if job['id'] == waiting['id'])
    assert final['state'] == 'failed' and final['error'] == 'unavailable' and final['attempt_count'] == 0
    assert not adapter.calls and reopened.ai_usage(env['project'], env['admin'])['reserved_requests'] == 0
    assert reopened.chat_conversation(customer['conversation']['id'], customer['principal'])['support_mode'] == 'waiting_human'
    with reopened.transaction() as c:
        assert c.execute('SELECT count(*) FROM admin_notifications').fetchone()[0] == 1
        assert c.execute("SELECT count(*) FROM chat_messages WHERE sender='ai'").fetchone()[0] == 0
