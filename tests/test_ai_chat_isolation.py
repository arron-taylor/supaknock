"""Independent AI turn races against real SQLite; no model or network calls."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
import threading
import time
from uuid import uuid4

import pytest

from feedback.ai_domain import EMBEDDING_PROFILE, default_ai_settings
from feedback.ai_chat import AIChatWorker
from feedback.ai_generation import AIGeneration
from feedback.ai_providers import GenerationResult, Usage
from feedback.ai_retrieval import RetrievalService
from feedback.domain import Actor
from feedback.errors import Error
from conftest import make_storage, reopen_storage


ORIGIN = 'https://customer.example'


@pytest.fixture
def ai_chat(tmp_path, database_url):
    store = make_storage(tmp_path / 'ai-chat-isolation.sqlite', 'https://feedback.example', database_url)
    account = store.create_native_account('owner@example.com', 'fixture-hash', 'Owner', 'admin', True)
    store.create_session(account['id'], 'admin-hash', 'csrf-hash', time.time() + 3600)
    admin = {'kind': 'admin', 'user_id': account['id'],
             'session_id': store.session('admin-hash')['session_id']}
    pid = store.create_project({'identifier': 'support', 'name': 'Support', 'description': ''}, account['id'])['id']
    store.chat_configure(pid, admin, True, [ORIGIN], 0)
    settings = default_ai_settings()
    settings['model'] = 'fixture-model'
    cfg = store.ai_configure(pid, admin, 0, settings, protected_key='protected-fixture-key')
    store.ai_store_provider_evidence(pid, admin, cfg['version'], cfg['credential_version'], 'fixture-model',
                                    catalogue=['fixture-model'], capabilities={
                                        'text_generation': True, 'tool_calling': False,
                                        'streaming': False, 'structured_output': False})
    operation = store.ai_runtime_begin(admin, 0, 'download')
    lease = store.ai_runtime_claim(time.time())
    assert store.ai_runtime_finish(operation['operation_id'], lease['lease_token'], manifest=EMBEDDING_PROFILE)
    source = store.ai_source_create(pid, admin, 'markdown', 'Support guide')
    revision = store.ai_revision_stage(pid, admin, source['id'], source['version'],
                                      'Support guide', 'Returns are accepted for thirty days.', 1)
    store.ai_revision_chunks(pid, admin, revision['id'], [{
        'ordinal': 0, 'title': 'Support guide', 'content': 'Returns are accepted for thirty days.',
        'location': None, 'vector': [1.0] + [0.0] * 767}])
    source = store.ai_revision_publish(pid, admin, revision['id'])
    source = store.ai_source_update(pid, admin, source['id'], source['version'], active=True, approved=True)
    env = {'store': store, 'project': pid, 'admin': admin, 'source': source}
    configure(env, enabled=True)
    env['chunk'] = store.ai_knowledge_chunks(pid, admin)[0]['id']
    return env


def configure(env, **changes):
    store, pid, admin = env['store'], env['project'], env['admin']
    cfg = store.ai_settings(pid, admin)
    settings = deepcopy(cfg['settings'])
    settings.update(changes)
    return store.ai_configure(pid, admin, cfg['version'], settings)


def conversation(env, kind='visitor', *, mode='ai'):
    store, pid, admin = env['store'], env['project'], env['admin']
    token_hash = str(uuid4())
    actor = None
    if kind == 'visitor':
        cv = store.chat_create_session(pid, ORIGIN, token_hash, time.time() + 3600, '203.0.113.8')
        principal = store.chat_visitor(token_hash, ORIGIN)
    else:
        parent = None
        external = ' exact external account '
        if kind == 'external':
            key = store.create_api_key(pid, 'Fixture backend', str(uuid4()), 'sb_test', ['participate'], admin['user_id'])
            actor = Actor(project_id=pid, key_id=key, permissions=('participate',))
        else:
            user = store.create_native_account(str(uuid4()) + '@example.com', 'fixture-hash', 'Customer')
            parent_hash = str(uuid4())
            store.create_session(user['id'], parent_hash, str(uuid4()), time.time() + 3600)
            parent = store.session(parent_hash)['session_id']
            actor, external = Actor(user_id=user['id'], role='participant'), None
        store.chat_create_customer_session(pid, actor, external, token_hash, time.time() + 3600, ORIGIN, parent)
        principal = store.chat_customer_session(token_hash, ORIGIN)
        cv = store.chat_create_conversation(principal, str(uuid4()))
    principal['ip_address'] = '203.0.113.8'
    if mode != 'human':
        cv = store.chat_support_mode(cv['id'], principal, mode, cv['mode_version'])
    return {'conversation': cv, 'principal': principal, 'actor': actor, 'parent': parent if kind != 'visitor' else None}


def turn(env, customer=None, *, question='How long do I have to return this?', dispatch=False):
    customer = customer or conversation(env)
    store = env['store']
    message = store.chat_send(customer['conversation']['id'], customer['principal'], str(uuid4()), question)
    job = store.ai_claim_job(time.time() + .01, lease_seconds=120, kinds=('turn',))
    assert job is not None
    if dispatch:
        assert store.ai_reserve_usage(job['id'], job['lease_token'], 100)
        assert store.ai_job_before_dispatch(job['id'], job['lease_token'])
    material = store.ai_job_material(job['id'], job['lease_token'])
    return customer, message, job, material


def reply(env, job, material, **overrides):
    values = {'body': 'Returns are accepted for thirty days.', 'citation_ids': [env['chunk']],
              'knowledge_version': material['job']['authority']['knowledge_version']}
    values.update(overrides)
    return env['store'].ai_commit_reply(job['id'], job['lease_token'], **values)


def race(env, action, count=8):
    store = env['store']
    handles = [reopen_storage(store) for _ in range(count)]
    barrier = threading.Barrier(count)

    def run(index):
        barrier.wait(timeout=10)
        return action(handles[index], index)

    with ThreadPoolExecutor(max_workers=count) as pool:
        return list(pool.map(run, range(count)))


def job_rows(env):
    return env['store'].ai_jobs(env['project'], env['admin'])


@pytest.mark.parametrize('mode', ['human', 'waiting_human'])
def test_human_modes_and_disabled_default_enqueue_no_customer_ai_work(ai_chat, mode):
    env, store = ai_chat, ai_chat['store']
    configure(env, enabled=False)
    customer = conversation(env, mode=mode)
    store.chat_send(customer['conversation']['id'], customer['principal'], str(uuid4()), 'Human support please')
    assert job_rows(env) == []
    assert store.ai_claim_job(time.time() + 1, kinds=('turn',)) is None
    with pytest.raises(Error):
        store.chat_support_mode(customer['conversation']['id'], customer['principal'], 'ai',
                                customer['conversation']['mode_version'])


def test_concurrent_customer_retry_creates_exactly_one_message_event_and_turn(ai_chat):
    env, store = ai_chat, ai_chat['store']
    customer = conversation(env)
    cid, mid = customer['conversation']['id'], str(uuid4())
    messages = race(env, lambda s, _: s.chat_send(cid, customer['principal'], mid, 'Return policy?'))
    assert len({message['id'] for message in messages}) == 1
    assert len(job_rows(env)) == 1
    with store.transaction() as c:
        assert c.execute('SELECT count(*) FROM chat_messages WHERE conversation_id=?', (cid,)).fetchone()[0] == 1
        assert c.execute('SELECT count(*) FROM request_events WHERE id=?', (messages[0]['id'],)).fetchone()[0] == 1
        assert c.execute('SELECT count(*) FROM ai_turns WHERE trigger_message_id=?', (messages[0]['id'],)).fetchone()[0] == 1
    with pytest.raises(Error) as error:
        store.chat_send(cid, customer['principal'], mid, 'Different content')
    assert error.value.status == 409
    assert len(job_rows(env)) == 1


def test_visible_ai_status_tracks_newest_message_when_job_timestamps_tie(ai_chat, monkeypatch):
    from feedback.storage import relational as adapter

    env, store = ai_chat, ai_chat['store']
    customer = conversation(env)
    cid = customer['conversation']['id']
    stamp = adapter.now()
    ids = iter([str(uuid4()), 'ffffffff-ffff-4fff-8fff-ffffffffffff',
                str(uuid4()), '00000000-0000-4000-8000-000000000001'])
    monkeypatch.setattr(adapter, 'now', lambda: stamp)
    monkeypatch.setattr(adapter, 'new_id', lambda: next(ids))
    store.chat_send(cid, customer['principal'], str(uuid4()), 'First question')
    store.chat_send(cid, customer['principal'], str(uuid4()), 'Newer question')
    assert store.chat_conversation(cid, customer['principal'])['ai_status'] == 'queued'


def test_concurrent_completed_reply_has_one_durable_message_event_and_delivery(ai_chat):
    env, store = ai_chat, ai_chat['store']
    store.update_webhook(env['project'], 'https://receiver.example/events', True,
                         ['chat.message_created'], 'protected-webhook-fixture', 1)
    customer, trigger, job, material = turn(env, dispatch=True)
    version = material['job']['authority']['knowledge_version']
    results = race(env, lambda s, _: s.ai_commit_reply(job['id'], job['lease_token'],
                   'Returns are accepted for thirty days.', [env['chunk']], version))
    replies = [result for result in results if result is not None]
    assert replies and len({result['id'] for result in replies}) == 1
    cid = customer['conversation']['id']
    messages = store.chat_messages(cid, customer['principal'], 0, 100)
    assert [message['sender_role'] for message in messages] == ['customer', 'ai']
    assert messages[-1]['sequence'] == 2 and messages[-1]['citations']
    with store.transaction() as c:
        event = c.execute('SELECT payload FROM request_events WHERE id=?', (replies[0]['id'],)).fetchone()[0]
        assert 'Returns are accepted' not in event and 'protected-' not in event
        assert json.loads(event)['message']['sender_role'] == 'ai'
        assert c.execute('SELECT count(*) FROM webhook_deliveries WHERE event_id=?', (replies[0]['id'],)).fetchone()[0] == 1
        assert c.execute('SELECT count(*) FROM admin_notifications WHERE event_id=?', (replies[0]['id'],)).fetchone()[0] == 0
        cv = c.execute('SELECT first_reply_at,last_customer_ip_address FROM chat_conversations WHERE id=?', (cid,)).fetchone()
        assert cv['first_reply_at'] is None and cv['last_customer_ip_address'] == '203.0.113.8'
    assert job_rows(env)[0]['state'] == 'succeeded'


def test_reply_racing_new_customer_message_obeys_one_transaction_order(ai_chat):
    env, store = ai_chat, ai_chat['store']
    customer, _, old_job, material = turn(env, dispatch=True)
    cid, new_client_id = customer['conversation']['id'], str(uuid4())

    def operation(handle, index):
        if index:
            return handle.chat_send(cid, customer['principal'], new_client_id, 'Newest customer question')
        return handle.ai_commit_reply(old_job['id'], old_job['lease_token'],
                                      'Returns are accepted for thirty days.', [env['chunk']],
                                      material['job']['authority']['knowledge_version'])

    accepted_reply, incoming = race(env, operation, 2)
    messages = store.chat_messages(cid, customer['principal'], 0, 100)
    assert messages[-1]['id'] == incoming['id']
    assert [message['sequence'] for message in messages] == list(range(1, len(messages) + 1))
    if accepted_reply is not None:
        assert [message['sender_role'] for message in messages] == ['customer', 'ai', 'customer']
    else:
        assert [message['sender_role'] for message in messages] == ['customer', 'customer']
    jobs = job_rows(env)
    assert len(jobs) == 2 and sum(job['state'] == 'queued' for job in jobs) == 1
    latest = store.ai_claim_job(time.time() + .01, kinds=('turn',))
    assert latest['authority']['trigger_sequence'] == incoming['sequence']
    assert reply(env, old_job, material) is None or accepted_reply is not None


def invalidate(env, customer, reason):
    store, cv, principal = env['store'], customer['conversation'], customer['principal']
    if reason in ('disable', 'off_on'):
        configure(env, enabled=False)
        if reason == 'off_on':
            configure(env, enabled=True)
    elif reason == 'personality':
        configure(env, soul='Use concise explanations.')
    elif reason == 'source_cycle':
        source = env['source']
        source = store.ai_source_update(env['project'], env['admin'], source['id'], source['version'], active=True, approved=False)
        store.ai_source_update(env['project'], env['admin'], source['id'], source['version'], active=True, approved=True)
    elif reason == 'mode_cycle':
        cv = store.chat_support_mode(cv['id'], principal, 'waiting_human', cv['mode_version'])
        store.chat_support_mode(cv['id'], principal, 'ai', cv['mode_version'])
    elif reason == 'close_cycle':
        cv = store.chat_change_state(cv['id'], env['admin'], 'closed', cv['version'])
        store.chat_change_state(cv['id'], env['admin'], 'open', cv['version'])
    elif reason == 'new_message':
        store.chat_send(cv['id'], principal, str(uuid4()), 'A newer question')
    elif reason == 'admin_reply':
        store.chat_send(cv['id'], env['admin'], str(uuid4()), 'A human is replying now.')
    elif reason == 'session_revoke':
        store.chat_revoke_customer_session(principal)
    elif reason == 'session_expire':
        table = 'chat_sessions' if principal['kind'] == 'visitor' else 'chat_customer_sessions'
        with store.transaction(True) as c:
            c.execute('UPDATE ' + table + ' SET expires_at=0 WHERE id=?', (principal['session_id'],))
    elif reason == 'issuing_key':
        store.revoke_api_key(env['project'], customer['actor'].key_id)
    elif reason == 'parent_session':
        store.revoke_session(customer['parent'])
    elif reason == 'origin_removed':
        cfg = store.chat_settings(env['project'])
        store.chat_configure(env['project'], env['admin'], True, [], cfg['version'])


@pytest.mark.parametrize('kind,reason', [
    ('visitor', 'disable'), ('visitor', 'off_on'), ('visitor', 'personality'),
    ('visitor', 'source_cycle'), ('visitor', 'mode_cycle'), ('visitor', 'close_cycle'),
    ('visitor', 'new_message'), ('visitor', 'admin_reply'), ('visitor', 'session_revoke'),
    ('visitor', 'session_expire'), ('visitor', 'origin_removed'),
    ('external', 'session_revoke'), ('external', 'session_expire'), ('external', 'issuing_key'),
    ('native', 'parent_session'),
])
@pytest.mark.parametrize('phase', ['before_dispatch', 'before_commit'])
def test_changed_authority_never_dispatches_or_commits_old_turn(ai_chat, kind, reason, phase):
    env, store = ai_chat, ai_chat['store']
    customer, _, job, material = turn(env, conversation(env, kind), dispatch=phase == 'before_commit')
    if phase == 'before_dispatch':
        assert store.ai_reserve_usage(job['id'], job['lease_token'], 100)
    invalidate(env, customer, reason)
    assert store.ai_job_material(job['id'], job['lease_token']) is None
    assert not store.ai_job_before_dispatch(job['id'], job['lease_token'])
    assert reply(env, job, material) is None
    with store.transaction() as c:
        assert c.execute("SELECT count(*) FROM chat_messages WHERE conversation_id=? AND sender='ai'",
                         (customer['conversation']['id'],)).fetchone()[0] == 0


def test_reply_and_customer_enqueue_roll_back_with_their_events(ai_chat, monkeypatch):
    env, store = ai_chat, ai_chat['store']
    customer, _, job, material = turn(env, dispatch=True)
    cid = customer['conversation']['id']
    original = store._persist_event

    def fail_event(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError('fixture transaction failure')

    monkeypatch.setattr(store, '_persist_event', fail_event)
    with pytest.raises(RuntimeError, match='fixture transaction failure'):
        reply(env, job, material)
    assert job_rows(env)[0]['state'] == 'running'
    assert store.chat_conversation(cid, customer['principal'])['last_sequence'] == 1
    assert len(store.chat_messages(cid, customer['principal'], 0, 100)) == 1
    monkeypatch.setattr(store, '_persist_event', original)
    assert reply(env, job, material)['sender_role'] == 'ai'

    def fail_enqueue(*args, **kwargs):
        raise RuntimeError('fixture enqueue failure')

    monkeypatch.setattr(store, '_chat_enqueue_turn', fail_enqueue)
    with pytest.raises(RuntimeError, match='fixture enqueue failure'):
        store.chat_send(cid, customer['principal'], str(uuid4()), 'This entire incoming write must roll back')
    assert len(job_rows(env)) == 1
    assert store.chat_conversation(cid, customer['principal'])['last_sequence'] == 2
    assert len(store.chat_messages(cid, customer['principal'], 0, 100)) == 2


def test_session_renewal_retries_original_receipt_without_reauthorizing_old_work(ai_chat):
    env, store = ai_chat, ai_chat['store']
    customer = conversation(env, 'external')
    cid, client_id = customer['conversation']['id'], str(uuid4())
    original = store.chat_send(cid, customer['principal'], client_id, 'Return policy?')
    old_job = store.ai_claim_job(time.time() + .01, kinds=('turn',))
    token_hash = str(uuid4())
    store.chat_create_customer_session(env['project'], customer['actor'], ' exact external account ',
                                       token_hash, time.time() + 3600, ORIGIN)
    renewed = store.chat_customer_session(token_hash, ORIGIN)
    store.chat_revoke_customer_session(customer['principal'])
    assert store.chat_send(cid, renewed, client_id, 'Return policy?')['id'] == original['id']
    assert len(job_rows(env)) == 1
    assert store.ai_job_material(old_job['id'], old_job['lease_token']) is None
    assert store.ai_claim_job(time.time() + 1, kinds=('turn',)) is None
    store.chat_send(cid, renewed, str(uuid4()), 'Here is a new question after renewal.')
    new_job = store.ai_claim_job(time.time() + .01, kinds=('turn',))
    assert new_job['id'] != old_job['id']
    material = store.ai_job_material(new_job['id'], new_job['lease_token'])
    assert material['job']['authority']['principal']['session_id'] == renewed['session_id']
    assert material['job']['authority']['customer_id'] == old_job['authority']['customer_id']


def test_recovered_lease_fences_old_reply_and_changed_success_retries(ai_chat):
    env, store = ai_chat, ai_chat['store']
    customer, _, old_job, material = turn(env, dispatch=True)
    with store.transaction(True) as c:
        c.execute('UPDATE ai_jobs SET lease_until=0 WHERE id=?', (old_job['id'],))
    recovered = store.ai_claim_job(time.time(), kinds=('turn',))
    assert recovered['lease_token'] != old_job['lease_token']
    assert reply(env, old_job, material) is None
    assert store.ai_reserve_usage(recovered['id'], recovered['lease_token'], 100)
    assert store.ai_job_before_dispatch(recovered['id'], recovered['lease_token'])
    result = reply(env, recovered, material)
    assert reply(env, recovered, material)['id'] == result['id']
    with pytest.raises(Error) as error:
        reply(env, recovered, material, body='A different reply cannot replace a committed answer.')
    assert error.value.status == 409
    assert len(store.chat_messages(customer['conversation']['id'], customer['principal'], 0, 100)) == 2
    usage = store.ai_usage(env['project'], env['admin'])
    assert usage['reserved_requests'] == 2


@pytest.mark.parametrize('invalid_citation', ['unknown', 'foreign', 'unapproved'])
def test_final_reply_rejects_citation_outside_current_project_evidence(ai_chat, invalid_citation):
    env, store = ai_chat, ai_chat['store']
    invalid_id = str(uuid4())
    if invalid_citation != 'unknown':
        pid = env['project']
        if invalid_citation == 'foreign':
            pid = store.create_project({'identifier': 'foreign', 'name': 'Foreign', 'description': ''}, env['admin']['user_id'])['id']
        source = store.ai_source_create(pid, env['admin'], 'markdown', 'Private evidence')
        revision = store.ai_revision_stage(pid, env['admin'], source['id'], source['version'],
                                           'Private evidence', 'Private evidence is never a public citation.', 1)
        store.ai_revision_chunks(pid, env['admin'], revision['id'], [{
            'ordinal': 0, 'title': 'Private evidence', 'content': 'Private evidence is never a public citation.',
            'location': None, 'vector': [1.0] + [0.0] * 767}])
        source = store.ai_revision_publish(pid, env['admin'], revision['id'])
        store.ai_source_update(pid, env['admin'], source['id'], source['version'],
                               active=True, approved=invalid_citation == 'foreign')
        with store.transaction() as c:
            invalid_id = c.execute('SELECT id FROM ai_chunks WHERE revision_id=?', (revision['id'],)).fetchone()[0]
    customer, _, job, material = turn(env, dispatch=True)
    with pytest.raises(Error) as error:
        reply(env, job, material, citation_ids=[env['chunk'], invalid_id])
    assert error.value.status == 409
    assert job_rows(env)[0]['state'] == 'running'
    assert len(store.chat_messages(customer['conversation']['id'], customer['principal'], 0, 100)) == 1
    assert reply(env, job, material)['sender_role'] == 'ai'


class FixedEmbeddings:
    def __init__(self):
        self.calls = []

    def embed_queries(self, items):
        self.calls.append(items)
        return [[1.0] + [0.0] * 767 for _ in items]


class FixtureVault:
    def reveal(self, protected):
        assert protected == 'protected-fixture-key'
        return 'fixture-only-no-network'


class FixtureProvider:
    def __init__(self, chunk, callback=None):
        self.chunk, self.callback, self.calls = chunk, callback, []

    async def generate(self, key, model, messages, **kwargs):
        self.calls.append({'model': model, 'messages': messages, **kwargs})
        if self.callback:
            self.callback()
        data = json.loads(messages[1].content)
        if data.get('operation') == 'evidence_check':
            return GenerationResult('{"assessment":"Fixture evidence supports this draft.","supported":true,"issues":[]}', (), Usage(20, 10, 30), 'stop')
        return GenerationResult(json.dumps({'status': 'answered', 'body': 'Returns are accepted for thirty days.',
                                            'citation_ids': [data['passages'][0]['id']]}), (), Usage(20, 10, 30), 'stop')


@pytest.mark.parametrize('disabled_case', ['new_project_default', 'explicitly_disabled'])
def test_disabled_chat_worker_makes_zero_embedding_and_provider_calls(ai_chat, disabled_case):
    env, store = ai_chat, ai_chat['store']
    if disabled_case == 'new_project_default':
        pid = store.create_project({'identifier': 'default-off', 'name': 'Default off', 'description': ''}, env['admin']['user_id'])['id']
        store.chat_configure(pid, env['admin'], True, [ORIGIN], 0)
        customer = conversation(env | {'project': pid}, mode='human')
        assert not store.ai_settings(pid, env['admin'])['settings']['enabled']
    else:
        customer = conversation(env)
        configure(env, enabled=False)
    store.chat_send(customer['conversation']['id'], customer['principal'], str(uuid4()), 'Please help me')
    embeddings, provider = FixedEmbeddings(), FixtureProvider(env['chunk'])
    engine = AIGeneration(store, FixtureVault(), RetrievalService(store, embeddings), provider_factory=lambda _: provider)
    worker = AIChatWorker(store, engine)
    assert worker.run_once() is False
    assert embeddings.calls == [] and provider.calls == []
    with store.transaction() as c:
        assert c.execute("SELECT count(*) FROM ai_jobs WHERE kind='turn'").fetchone()[0] == 0


@pytest.mark.parametrize('question', ['Q' * 4000, '返' * 4000], ids=['ascii', 'unicode'])
def test_maximum_chat_message_keeps_full_generation_question_and_bounded_local_search(ai_chat, question):
    env, store = ai_chat, ai_chat['store']
    customer, _, job, material = turn(env, question=question)
    embeddings = FixedEmbeddings()
    provider = FixtureProvider(env['chunk'])
    engine = AIGeneration(store, FixtureVault(), RetrievalService(store, embeddings), provider_factory=lambda _: provider)
    result = asyncio.run(engine.generate(job['id'], job['lease_token'], material['question'], history=material['messages']))
    assert len(provider.calls) == 2
    context = json.loads(provider.calls[0]['messages'][1].content)
    assert context['question'] == question
    assert provider.calls[0]['tools'] == ()
    assert len(embeddings.calls) == 1
    query = embeddings.calls[0][0]
    assert len(query) <= 1000 and len(query.encode('utf-8')) <= 1750
    committed = store.ai_commit_reply(job['id'], job['lease_token'], result['body'], result['citation_ids'], result['knowledge_version'])
    assert committed['sender_role'] == 'ai'


@pytest.mark.parametrize('reason', ['off_on', 'issuing_key', 'source_cycle', 'new_message'])
def test_late_provider_response_cannot_commit_after_turn_authority_changes(ai_chat, reason):
    env, store = ai_chat, ai_chat['store']
    customer, _, job, material = turn(env, conversation(env, 'external'))
    embeddings = FixedEmbeddings()
    provider = FixtureProvider(env['chunk'], lambda: invalidate(env, customer, reason))
    engine = AIGeneration(store, FixtureVault(), RetrievalService(store, embeddings), provider_factory=lambda _: provider)
    with pytest.raises(Error) as error:
        asyncio.run(engine.generate(job['id'], job['lease_token'], material['question'], history=material['messages']))
    assert error.value.status == 409
    assert len(provider.calls) == 1
    assert reply(env, job, material) is None
    assert store.ai_usage(env['project'], env['admin'])['reserved_requests'] == 1
    with store.transaction() as c:
        assert c.execute("SELECT count(*) FROM chat_messages WHERE conversation_id=? AND sender='ai'",
                         (customer['conversation']['id'],)).fetchone()[0] == 0


@pytest.mark.parametrize('reason', ['disable', 'issuing_key', 'new_message'])
def test_invalidated_turn_runs_no_query_embedding_or_provider_call(ai_chat, reason):
    env, store = ai_chat, ai_chat['store']
    customer, _, job, material = turn(env, conversation(env, 'external'))
    embeddings = FixedEmbeddings()
    provider = FixtureProvider(env['chunk'])
    engine = AIGeneration(store, FixtureVault(), RetrievalService(store, embeddings), provider_factory=lambda _: provider)
    invalidate(env, customer, reason)
    with pytest.raises(Error) as error:
        asyncio.run(engine.generate(job['id'], job['lease_token'], material['question']))
    assert error.value.status == 409
    assert embeddings.calls == [] and provider.calls == []
