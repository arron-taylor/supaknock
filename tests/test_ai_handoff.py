"""Versioned human handoff and shared attention semantics against real SQLite."""
import json
import time
from uuid import uuid4

import pytest

from feedback.domain import now
from feedback.errors import Error
from test_ai_chat_isolation import (
    ORIGIN, ai_chat, configure, conversation, job_rows, race, reply, turn,
)
from test_ai_authoring import authoring
from test_chat import ORIGIN as WEB_ORIGIN, enable, visitor


def notifications(env):
    admin = env['admin']
    return env['store'].notifications(admin['user_id'], admin['session_id'], False, 100, 0)['items']


def handoff_events(store, cid):
    with store.transaction() as c:
        events = [json.loads(row[0]) for row in c.execute(
            "SELECT payload FROM request_events WHERE type='chat.support_mode_changed'")]
    return [event for event in events if event['conversation']['id'] == cid
            and event['conversation']['support_mode'] == 'waiting_human']


def alerts(env):
    store, admin = env['store'], env['admin']
    row = store.push_prepare(admin['user_id'], admin['session_id'], 'https://relay.example', 'protected-proof-fixture')
    store.push_enable(admin['user_id'], admin['session_id'], row['version'], 'protected-relay-fixture')
    recipients = []
    for index, enabled in enumerate((True, True, False)):
        did = str(uuid4())
        store.register_device(admin['user_id'], admin['session_id'], did,
                              'protected-device-fixture-' + str(index), str(uuid4()), {
                                  'name': 'Fixture device', 'environment': 'sandbox', 'enabled': True,
                                  'request_notifications': False, 'chat_notifications': enabled})
        if enabled:
            recipients.append(did)
    store.update_webhook(env['project'], 'https://receiver.example/events', True,
                         ['chat.message_created', 'chat.support_mode_changed'], 'protected-webhook-fixture', 1)
    return recipients


def proposal(env, customer):
    """AI12 will expose creation; seed only a valid scoped pending row here."""
    store, pid = env['store'], env['project']
    board = store.create_board(pid, {'identifier': 'feedback-' + str(uuid4()), 'name': 'Feedback', 'description': ''})
    cv = store.chat_conversation(customer['conversation']['id'], env['admin'])
    proposal_id = str(uuid4())
    version = store.ai_settings(pid, env['admin'])['version']
    with store.transaction(True) as c:
        c.execute('''INSERT INTO ai_proposals(id,project_id,conversation_id,customer_id,kind,board_id,title,
                     description,config_version,mode_version,client_id,created_at)
                     VALUES(?,?,?,?,?,?,?,?,?,?,?,?)''',
                  (proposal_id, pid, cv['id'], cv['customer']['id'], 'feature', board['id'],
                   'Private draft title', 'Private draft content', version, cv['mode_version'], str(uuid4()), now()))
    return proposal_id


def proposal_state(store, proposal_id):
    with store.transaction() as c:
        return dict(c.execute('SELECT state,version FROM ai_proposals WHERE id=?', (proposal_id,)).fetchone())


def set_mode(env, customer, mode, principal=None):
    store, cid = env['store'], customer['conversation']['id']
    actor = principal or customer['principal']
    current = store.chat_conversation(cid, actor)
    return store.chat_support_mode(cid, actor, mode, current['mode_version'])


def test_concurrent_handoff_creates_one_shared_inbox_event_and_per_device_delivery(ai_chat):
    env, store = ai_chat, ai_chat['store']
    recipients = alerts(env)
    customer, _, job, material = turn(env, dispatch=True)
    pending = proposal(env, customer)
    cid, version = customer['conversation']['id'], customer['conversation']['mode_version']

    def request(handle, _):
        try:
            return handle.chat_support_mode(cid, customer['principal'], 'waiting_human', version)
        except Error as error:
            assert error.status == 409
            return None

    results = race(env, request)
    assert len([result for result in results if result]) == 1
    current = store.chat_conversation(cid, customer['principal'])
    assert current['support_mode'] == 'waiting_human' and current['mode_version'] == version + 1
    assert current['state'] == 'open' and current['version'] == customer['conversation']['version']
    assert proposal_state(store, pending) == {'state': 'cancelled', 'version': 2}
    assert reply(env, job, material) is None
    events = handoff_events(store, cid)
    assert len(events) == 1
    event = events[0]
    assert len(notifications(env)) == 1 and notifications(env)[0]['id'] == event['id']
    assert notifications(env)[0]['title'] == 'Human support requested'
    jobs = store.push_deliveries(100, 0)
    assert len(jobs) == len(recipients) == 2
    assert {job['device_id'] for job in jobs} == set(recipients)
    assert {job['event_id'] for job in jobs} == {event['id']}
    assert {job['resource_id'] for job in jobs} == {cid}
    with store.transaction() as c:
        assert c.execute('SELECT count(*) FROM webhook_deliveries WHERE event_id=?', (event['id'],)).fetchone()[0] == 1
    public_event = json.dumps(event)
    assert not any(value in public_event for value in ('Private draft', 'protected-', '203.0.113.8', 'session_id', 'external_user_id'))
    assert set_mode(env, customer, 'waiting_human')['mode_version'] == current['mode_version']
    assert len(handoff_events(store, cid)) == 1 and len(store.push_deliveries(100, 0)) == 2


def test_handoff_push_failure_rolls_back_mode_cancellation_inbox_and_outbound_jobs(ai_chat, monkeypatch):
    env, store = ai_chat, ai_chat['store']
    alerts(env)
    customer, _, job, material = turn(env, dispatch=True)
    pending = proposal(env, customer)
    cid = customer['conversation']['id']
    before = store.chat_conversation(cid, customer['principal'])
    original = store._queue_push

    def fail(c, *args):
        original(c, *args)
        raise RuntimeError('fixture delivery failure')

    monkeypatch.setattr(store, '_queue_push', fail)
    with pytest.raises(RuntimeError, match='fixture delivery failure'):
        set_mode(env, customer, 'waiting_human')
    assert store.chat_conversation(cid, customer['principal']) == before
    assert proposal_state(store, pending) == {'state': 'pending', 'version': 1}
    assert store.ai_job_material(job['id'], job['lease_token']) is not None
    assert handoff_events(store, cid) == [] and notifications(env) == []
    assert store.push_deliveries(100, 0) == []
    with store.transaction() as c:
        assert not c.execute("SELECT 1 FROM webhook_deliveries d JOIN request_events e ON e.id=d.event_id WHERE e.type='chat.support_mode_changed' AND e.payload LIKE '%waiting_human%'").fetchone()
    monkeypatch.setattr(store, '_queue_push', original)
    assert set_mode(env, customer, 'waiting_human')['support_mode'] == 'waiting_human'
    assert proposal_state(store, pending)['state'] == 'cancelled'


@pytest.mark.parametrize('start', ['ai', 'human'])
def test_direct_human_controls_work_with_ai_and_every_tool_off(ai_chat, start):
    env, store = ai_chat, ai_chat['store']
    customer = conversation(env, mode=start)
    configure(env, enabled=False)
    assert not any(store.ai_settings(env['project'], env['admin'])['settings']['tools'].values())
    waiting = set_mode(env, customer, 'waiting_human')
    assert waiting['support_mode'] == 'waiting_human'
    assert len(handoff_events(store, waiting['id'])) == 1
    human = set_mode(env, customer, 'human', env['admin'])
    assert human['support_mode'] == 'human'
    assert len(job_rows(env)) == 0
    with pytest.raises(Error) as error:
        set_mode(env, customer, 'ai')
    assert error.value.code == 'ai_not_ready'
    with pytest.raises(Error) as error:
        store.chat_support_mode(human['id'], customer['principal'], 'human', human['mode_version'])
    assert error.value.status == 403


@pytest.mark.parametrize('takeover', ['explicit', 'reply'])
def test_admin_takeover_cancels_turn_and_proposal_and_requires_explicit_return(ai_chat, takeover):
    env, store = ai_chat, ai_chat['store']
    customer, _, job, material = turn(env, dispatch=True)
    pending = proposal(env, customer)
    cid = customer['conversation']['id']
    if takeover == 'explicit':
        set_mode(env, customer, 'human', env['admin'])
    else:
        store.chat_send(cid, env['admin'], str(uuid4()), 'A human is here to help.')
    human = store.chat_conversation(cid, customer['principal'])
    assert human['support_mode'] == 'human'
    assert human['mode_version'] == customer['conversation']['mode_version'] + 1
    assert proposal_state(store, pending) == {'state': 'cancelled', 'version': 2}
    assert reply(env, job, material) is None
    store.chat_send(cid, customer['principal'], str(uuid4()), 'Follow-up for the human')
    assert len(job_rows(env)) == 1
    closed = store.chat_change_state(cid, env['admin'], 'closed', human['version'])
    reopened = store.chat_change_state(cid, env['admin'], 'open', closed['version'])
    assert reopened['support_mode'] == 'human'
    assert reopened['mode_version'] == human['mode_version']
    with pytest.raises(Error) as error:
        store.chat_support_mode(cid, customer['principal'], 'ai', customer['conversation']['mode_version'])
    assert error.value.status == 409
    ai = set_mode(env, customer, 'ai')
    assert ai['mode_version'] == human['mode_version'] + 1
    assert len(job_rows(env)) == 1  # Switching modes cannot replay an old question.
    store.chat_send(cid, customer['principal'], str(uuid4()), 'An explicit new AI question')
    jobs = job_rows(env)
    assert len(jobs) == 2 and sum(job['state'] == 'queued' for job in jobs) == 1
    assert proposal_state(store, pending)['state'] == 'cancelled'


def test_handoff_shared_inbox_reads_are_separate_from_admin_chat_reads(ai_chat):
    env, store, admin = ai_chat, ai_chat['store'], ai_chat['admin']
    customer, _, _, _ = turn(env)
    cid = customer['conversation']['id']
    set_mode(env, customer, 'waiting_human')
    handoff = notifications(env)[0]
    store.chat_read(cid, admin, 1)
    assert store.chat_conversation(cid, admin)['unread_count'] == 0
    assert store.notification_summary(admin['user_id'], admin['session_id'])['unread_count'] == 1
    second = store.create_native_account('second-admin@example.com', 'fixture-hash', 'Second administrator', 'admin')
    store.create_session(second['id'], 'second-admin-session', 'csrf', time.time() + 3600)
    second_principal = {'kind': 'admin', 'user_id': second['id'],
                        'session_id': store.session('second-admin-session')['session_id']}
    assert store.chat_conversation(cid, second_principal)['unread_count'] == 1
    store.set_notification_read(second['id'], second_principal['session_id'], handoff['id'], True)
    assert store.notification_summary(admin['user_id'], admin['session_id'])['unread_count'] == 0
    assert store.chat_conversation(cid, second_principal)['unread_count'] == 1
    store.chat_send(cid, customer['principal'], str(uuid4()), 'Another message while waiting')
    assert store.notification_summary(admin['user_id'], admin['session_id'])['unread_count'] == 1
    assert store.chat_conversation(cid, admin)['unread_count'] == 1


def test_waiting_human_notification_and_push_coalescing_survive_customer_renewal(ai_chat):
    env, store = ai_chat, ai_chat['store']
    recipients = alerts(env)
    customer, _, _, _ = turn(env, conversation(env, 'external'))
    cid = customer['conversation']['id']
    set_mode(env, customer, 'waiting_human')
    token = str(uuid4())
    store.chat_create_customer_session(env['project'], customer['actor'], ' exact external account ',
                                       token, time.time() + 3600, ORIGIN)
    renewed = store.chat_customer_session(token, ORIGIN)
    store.chat_revoke_customer_session(customer['principal'])
    store.chat_ticket(cid, renewed, 'reconnect-ticket', time.time() + 60)
    returned_id, reconnected = store.chat_consume_ticket('reconnect-ticket', ORIGIN)
    assert returned_id == cid and store.chat_conversation(cid, reconnected)['support_mode'] == 'waiting_human'
    message = store.chat_send(cid, reconnected, str(uuid4()), 'Private waiting message after reconnect')
    assert len(notifications(env)) == 2
    assert notifications(env)[0]['id'] == message['id']
    assert notifications(env)[0]['title'] == 'New customer message'
    assert len(store.push_deliveries(100, 0)) == len(recipients)  # Existing five-minute coalescing.
    with store.transaction(True) as c:
        c.execute('UPDATE chat_conversations SET last_alert_at=? WHERE id=?', (time.time() - 301, cid))
    client_id = str(uuid4())
    later = store.chat_send(cid, reconnected, client_id, 'Later waiting message')
    assert store.chat_send(cid, reconnected, client_id, 'Later waiting message')['id'] == later['id']
    assert len(notifications(env)) == 3
    assert len(store.push_deliveries(100, 0)) == 2 * len(recipients)
    assert {job['event_id'] for job in store.push_deliveries(100, 0)} == {handoff_events(store, cid)[0]['id'], later['id']}
    assert store.chat_conversation(cid, reconnected)['support_mode'] == 'waiting_human'
    assert all(job['state'] == 'cancelled' for job in job_rows(env))


def test_websocket_reconnect_waiting_for_human_preserves_new_message_notifications(authoring):
    ai = authoring
    enable(ai)
    store, admin, pid = ai['s'], ai['principal'], ai['p']['id']
    cfg = store.ai_settings(pid, admin)
    store.ai_configure(pid, admin, cfg['version'], cfg['settings'] | {'enabled': True})
    customer, cv, _ = visitor(ai)
    mode_url = '/api/v1/chat/conversations/' + cv['id'] + '/support-mode'
    ai_mode = customer.put(mode_url, json={'mode': 'ai', 'mode_version': 1})
    assert ai_mode.status_code == 200
    waiting = customer.put(mode_url, json={'mode': 'waiting_human', 'mode_version': ai_mode.json()['mode_version']})
    assert waiting.status_code == 200
    env = {'store': store, 'admin': admin}
    assert len(notifications(env)) == 1
    for connection in range(2):
        ticket = customer.post('/api/v1/chat/tickets', json={'conversation_id': cv['id']}).json()['ticket']
        with customer.websocket_connect('/api/v1/chat/ws', headers={'Origin': WEB_ORIGIN}) as ws:
            ws.send_json({'ticket': ticket, 'after': connection})
            assert ws.receive_json()['type'] == 'ready'
            state = ws.receive_json()
            assert state['type'] == 'state' and state['conversation']['support_mode'] == 'waiting_human'
            ws.send_json({'type': 'message', 'message': {
                'client_message_id': str(uuid4()), 'body': 'Waiting message ' + str(connection)}})
            for _ in range(8):
                frame = ws.receive_json()
                if frame['type'] == 'ack':
                    break
            else:
                pytest.fail('Waiting customer message was not acknowledged')
            assert frame['message']['sender_role'] == 'customer'
        assert len(notifications(env)) == connection + 2
    assert not any(job['kind'] == 'turn' for job in store.ai_jobs(pid, admin))
