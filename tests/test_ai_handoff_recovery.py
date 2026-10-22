"""Human-transfer state, alert receipts and stale turns survive SQLite recovery."""
from uuid import uuid4

import pytest

from feedback.errors import Error
from feedback.storage.sqlite import SQLiteStorage
from test_ai_chat_isolation import ai_chat, configure, conversation, turn


@pytest.mark.parametrize('kind', ['visitor', 'external', 'native'])
def test_handoff_backup_preserves_alert_identity_read_state_and_human_clock(ai_chat, tmp_path, kind):
    env, store, admin = ai_chat, ai_chat['store'], ai_chat['admin']
    store.push_prepare(admin['user_id'], admin['session_id'], 'https://relay.example', 'fixture-proof')
    store.push_enable(admin['user_id'], admin['session_id'], 1, 'protected-fixture-relay')
    store.register_device(admin['user_id'], admin['session_id'], str(uuid4()), 'protected-fixture-device', str(uuid4()),
        {'name': 'Phone', 'environment': 'sandbox', 'enabled': True, 'request_notifications': False, 'chat_notifications': True})
    customer = conversation(env, kind)
    customer, message, job, material = turn(env, customer, dispatch=True)
    cid, principal = customer['conversation']['id'], customer['principal']
    with store.transaction() as c:
        clock = dict(c.execute('SELECT first_message_at,first_reply_at,last_customer_ip_address FROM chat_conversations WHERE id=?', (cid,)).fetchone())
    waiting = store.chat_support_mode(cid, principal, 'waiting_human', customer['conversation']['mode_version'])
    alerts = store.notifications(admin['user_id'], admin['session_id'], False, 25, 0)
    assert len(alerts['items']) == 1 and alerts['items'][0]['type'] == 'chat.support_mode_changed'
    alert = alerts['items'][0]
    store.set_notification_read(admin['user_id'], admin['session_id'], alert['id'], True)
    push = store.push_deliveries(100, 0)
    assert len(push) == 1 and push[0]['event_id'] == alert['id']
    configure(env, enabled=False)
    destination = tmp_path / ('handoff-' + kind + '.sqlite')
    store.backup(destination)
    restored = SQLiteStorage(destination, store.base_url)
    saved = restored.chat_conversation(cid, principal)
    assert (saved['support_mode'], saved['mode_version']) == ('waiting_human', waiting['mode_version'])
    assert restored.push_deliveries(100, 0) == push
    assert restored.notifications(admin['user_id'], admin['session_id'], False, 25, 0)['items'][0]['read_at'] is not None
    assert restored.ai_commit_reply(job['id'], job['lease_token'], 'Late answer', [env['chunk']], material['job']['authority']['knowledge_version']) is None
    assert restored.chat_support_mode(cid, principal, 'waiting_human', saved['mode_version'])['mode_version'] == saved['mode_version']
    assert len(restored.push_deliveries(100, 0)) == 1
    with pytest.raises(Error) as caught:
        restored.chat_support_mode(cid, principal, 'ai', saved['mode_version'])
    assert caught.value.code == 'ai_not_ready'
    new_message = restored.chat_send(cid, principal, str(uuid4()), 'Human help is still needed')
    fresh = restored.notifications(admin['user_id'], admin['session_id'], True, 25, 0)
    assert [item['id'] for item in fresh['items']] == [new_message['id']]
    reply = restored.chat_send(cid, admin | {'ip_address': '198.51.100.4'}, str(uuid4()), 'A human is here to help')
    human = restored.chat_conversation(cid, admin)
    assert human['support_mode'] == 'human' and human['mode_version'] == saved['mode_version'] + 1
    with restored.transaction() as c:
        timing = c.execute('SELECT first_message_at,first_reply_at,last_customer_ip_address FROM chat_conversations WHERE id=?', (cid,)).fetchone()
        assert timing['first_message_at'] == clock['first_message_at']
        assert timing['first_reply_at'] == reply['created_at']
        assert timing['last_customer_ip_address'] == clock['last_customer_ip_address']
    assert len(restored.ai_jobs(env['project'], admin)) == 1
