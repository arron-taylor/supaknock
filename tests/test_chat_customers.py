"""Authenticated chat contracts through the real API and SQLite adapter."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import hmac
import json
import time
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from feedback.api import create_app
from feedback.security import digest, Vault
from feedback.webhooks import WebhookWorker
from conftest import PASSWORD
from test_chat import ORIGIN, enable, send, visitor
from test_native import native


def customer(w, external=' 0007 Alice ', *, project=None, issuer=None,
             client_type='native', origin=None, display_name=None):
    project = project or w['p']
    issuer = issuer or w['trusted']
    body = {'client_type': client_type}
    if external is not None:
        body['external_user_id'] = external
    if origin is not None:
        body['origin'] = origin
    if display_name is not None:
        body['display_name'] = display_name
    response = issuer.post('/api/v1/chat/projects/' + project['id'] + '/customer-sessions', json=body)
    assert response.status_code == 201, response.text
    data = response.json()
    assert data['token'].startswith('cc_')
    client = TestClient(w['app'])
    client.headers['X-Chat-Token'] = data['token']
    if client_type == 'browser':
        client.headers['Origin'] = origin
    return client, data


def conversation(client, client_id=None):
    response = client.post('/api/v1/chat/conversations', json={
        'client_conversation_id': client_id or str(uuid4()),
    })
    assert response.status_code == 201, response.text
    return response.json()


def own(client):
    response = client.get('/api/v1/chat/conversations')
    assert response.status_code == 200, response.text
    return response.json()['items']


def configured_chat_webhook(w):
    response = w['admin'].put('/api/v1/projects/' + w['p']['id'] + '/webhook', json={
        'url': 'http://127.0.0.1:9999/customer-chat', 'enabled': True,
        'events': ['chat.message_created'], 'version': 1,
    })
    assert response.status_code == 200, response.text
    return response.json()['signing_secret']


def test_external_customer_is_project_scoped_exact_and_reuses_feedback_identity(workflow):
    w = workflow
    enable(w); enable(w, w['q'])
    first, a = customer(w, display_name='Alice Example')
    renewed, same = customer(w)
    trimmed, b = customer(w, '0007 Alice')
    folded, c = customer(w, ' 0007 alice ')
    another, d = customer(w, project=w['q'], issuer=w['other'])
    assert same['customer']['id'] == a['customer']['id']
    assert len({x['customer']['id'] for x in (a, b, c, d)}) == 4
    assert len({x['customer']['user_id'] for x in (a, b, c, d)}) == 4
    assert a['customer']['kind'] == 'external'
    assert a['customer']['external_user_id'] == ' 0007 Alice '
    store = w['app'].state.storage
    assert store.resolve_external_identity(w['p']['id'], ' 0007 Alice ') == a['customer']['user_id']
    with store.transaction() as db:
        assert db.execute('SELECT count(*) FROM local_credentials WHERE user_id=?',
                          (a['customer']['user_id'],)).fetchone()[0] == 0
    for client, label in [(first, 'First'), (trimmed, 'Trimmed'), (folded, 'Folded'), (another, 'Another project')]:
        item = conversation(client)
        assert send(client, item, label).status_code == 201
    assert len(own(renewed)) == 1
    assert own(renewed)[0]['customer']['id'] == a['customer']['id']
    assert 'email' not in json.dumps(a['customer'])


def test_exchange_permissions_and_browser_native_transport(workflow):
    w = workflow; enable(w)
    path = '/api/v1/chat/projects/' + w['p']['id'] + '/customer-sessions'
    body = {'external_user_id': 'customer', 'client_type': 'native'}
    assert w['anon'].post(path, json=body).status_code == 401
    assert w['manager'].post(path, json=body).status_code == 403
    assert w['other'].post(path, json=body).status_code in (403, 404)
    assert w['user'].post(path, json=body).status_code in (400, 403)
    for invalid in ['', 7, 'x' * 513]:
        assert w['trusted'].post(path, json={**body, 'external_user_id': invalid}).status_code == 400
    assert w['trusted'].post(path, json={**body, 'customer_id': str(uuid4())}).status_code == 400
    assert w['trusted'].post(path, json={**body, 'client_type': 'browser'}).status_code == 400
    assert w['trusted'].post(path, json={**body, 'client_type': 'browser', 'origin': 'https://evil.example'}).status_code == 403
    assert w['trusted'].post(path, json=body, headers={'Origin': ORIGIN}).status_code == 403
    assert w['trusted'].post(path, json={**body, 'origin': ORIGIN}).status_code == 400
    for invalid_origin in ['https://[invalid', 'https://website.example:99999', 'https://website.example:bad']:
        assert w['trusted'].post(path, json={**body, 'client_type': 'browser', 'origin': invalid_origin}).status_code == 400
    native, issued = customer(w)
    lifetime = (datetime.fromisoformat(issued['expires_at'].replace('Z', '+00:00')) - datetime.now(timezone.utc)).total_seconds()
    assert 3500 <= lifetime <= 3601
    assert native.get('/api/v1/chat/conversations').status_code == 200
    assert native.get('/api/v1/chat/conversations', headers={'Origin': ORIGIN}).status_code in (401, 403)
    browser, issued = customer(w, client_type='browser', origin=ORIGIN)
    assert browser.get('/api/v1/chat/conversations').status_code == 200
    assert browser.get('/api/v1/chat/conversations', headers={'Origin': 'https://evil.example'}).status_code in (401, 403)
    missing_origin = TestClient(w['app'], headers={'X-Chat-Token': issued['token']})
    assert missing_origin.get('/api/v1/chat/conversations').status_code in (401, 403)
    assert browser.get('/api/v1/admin/chat/conversations').status_code == 403
    assert browser.post(path, json=body).status_code == 400
    assert browser.get('/api/v1/chat/conversations', headers={'Authorization': 'Bearer ' + w['k']['secret']}).status_code == 400


def test_history_reads_and_message_retries_survive_customer_session_renewal(workflow):
    w = workflow; enable(w)
    first, issued = customer(w)
    second, renewed = customer(w)
    cid = str(uuid4())
    c = conversation(first, cid)
    assert conversation(second, cid)['id'] == c['id']
    mid = str(uuid4())
    result = send(first, c, 'A private question', mid).json()
    assert result['sender'] == 'visitor' and result['sender_role'] == 'customer'
    with ThreadPoolExecutor(max_workers=4) as pool:
        replies = list(pool.map(lambda _: send(second, c, 'A private question', mid), range(4)))
    assert all(r.status_code == 201 and r.json()['id'] == result['id'] for r in replies)
    assert send(second, c, 'Conflicting body', mid).status_code == 409
    assert send(w['admin'], c, 'A private reply').json()['sender_role'] == 'admin'
    path = '/api/v1/chat/conversations/' + c['id']
    assert second.get(path).json()['unread_count'] == 1
    assert first.post(path + '/read', json={'sequence': 2}).json()['read_sequence'] == 2
    assert second.get(path).json()['unread_count'] == 0
    assert second.post(path + '/read', json={'sequence': 0}).json()['read_sequence'] == 2
    page = second.get(path + '/messages', params={'after': 0, 'limit': 1}).json()
    assert page['has_more'] and page['next_sequence'] == 1
    assert second.get(path + '/messages', params={'after': 1}).json()['items'][0]['body'] == 'A private reply'
    assert first.delete('/api/v1/chat/session').status_code == 204
    assert first.get(path).status_code == 401
    assert second.get(path).status_code == 200
    third, again = customer(w)
    assert again['customer']['id'] == issued['customer']['id']
    assert [x['id'] for x in own(third)] == [c['id']]
    assert third.get(path).json()['unread_count'] == 0
    assert send(third, c, 'A private question', mid).json()['id'] == result['id']
    closed = w['admin'].patch('/api/v1/admin/chat/conversations/' + c['id'], json={'state': 'closed', 'version': c['version']})
    assert closed.status_code == 200
    assert send(third, c, 'A private question', mid).json()['id'] == result['id']
    assert send(third, c, 'A new message').status_code == 409


def test_customer_ownership_covers_history_writes_reads_tickets_and_admin(workflow):
    w = workflow; enable(w); enable(w, w['q'])
    owner, _ = customer(w)
    other, _ = customer(w, 'different')
    cross, _ = customer(w, project=w['q'], issuer=w['other'])
    c = conversation(owner); send(owner, c, 'Hidden conversation')
    path = '/api/v1/chat/conversations/' + c['id']
    for client in (other, cross):
        assert own(client) == []
        assert client.get(path).status_code == 404
        assert client.get(path + '/messages').status_code == 404
        assert send(client, c).status_code == 404
        assert client.post(path + '/read', json={'sequence': 1}).status_code == 404
        assert client.post('/api/v1/chat/tickets', json={'conversation_id': c['id']}).status_code == 404
    assert owner.patch('/api/v1/admin/chat/conversations/' + c['id'], json={'state': 'closed', 'version': 1}).status_code == 403
    assert owner.get('/api/v1/admin/dashboard').status_code in (401, 403)
    assert w['anon'].get(path).status_code in (401, 403)


@pytest.mark.parametrize('revocation', ['session_expiry', 'issuer_key', 'all_customer_sessions'])
def test_revocation_and_expiry_deny_access_without_losing_customer_history(workflow, revocation):
    w = workflow; enable(w)
    client, issued = customer(w)
    second, _ = customer(w)
    c = conversation(client); send(client, c, 'Keep this history')
    if revocation == 'session_expiry':
        with w['app'].state.storage.transaction(True) as db:
            db.execute('UPDATE chat_customer_sessions SET expires_at=? WHERE token_hash=?', (time.time() - 1, digest(issued['token'])))
    elif revocation == 'issuer_key':
        assert w['admin'].delete('/api/v1/projects/' + w['p']['id'] + '/keys/' + w['k']['id']).status_code == 204
    else:
        response = w['trusted'].post('/api/v1/chat/projects/' + w['p']['id'] + '/customer-sessions/revoke', json={'external_user_id': ' 0007 Alice '})
        assert response.status_code == 204, response.text
    assert client.get('/api/v1/chat/conversations/' + c['id']).status_code == 401
    if revocation != 'session_expiry':
        assert second.get('/api/v1/chat/conversations/' + c['id']).status_code == 401
    issuer = w['trusted'] if revocation != 'issuer_key' else w['key'](w['p'], ['participate'])[0]
    renewed, data = customer(w, issuer=issuer)
    assert data['customer']['id'] == issued['customer']['id']
    assert renewed.get('/api/v1/chat/conversations/' + c['id'] + '/messages').json()['items'][0]['body'] == 'Keep this history'


@pytest.mark.parametrize('revoke', ['logout', 'password_change'])
def test_native_participant_customer_sessions_follow_parent_revocation(workflow, revoke):
    w = workflow; enable(w)
    client, issued = customer(w, None, issuer=w['user'], client_type='browser', origin=w['cfg'].base_url)
    assert issued['customer']['kind'] == 'native'
    assert issued['customer']['external_user_id'] is None
    c = conversation(client); send(client, c, 'Native account question')
    second, again = customer(w, None, issuer=w['user'], client_type='native')
    assert again['customer']['id'] == issued['customer']['id']
    assert [x['id'] for x in own(second)] == [c['id']]
    if revoke == 'logout':
        assert w['user'].post('/api/v1/auth/logout').status_code == 204
    else:
        assert w['user'].post('/api/v1/auth/password', json={'current_password': PASSWORD, 'new_password': PASSWORD + '-changed'}).status_code == 204
    for current in (client, second):
        assert current.get('/api/v1/chat/conversations/' + c['id']).status_code == 401


def test_account_exchange_requires_csrf_and_native_admin_exchange_stays_customer_scoped(workflow):
    w = workflow; enable(w)
    path = '/api/v1/chat/projects/' + w['p']['id'] + '/customer-sessions'
    no_csrf = TestClient(w['app'])
    no_csrf.cookies.update(w['user'].cookies)
    assert no_csrf.post(path, json={'client_type': 'native'}, headers={'Origin': w['cfg'].base_url}).status_code == 403
    assert w['user'].post(path, json={'client_type': 'native', 'display_name': 'Impersonated admin'}).status_code == 400
    issuer, account = native(w['app'])
    client, issued = customer(w, None, issuer=issuer)
    assert issued['customer']['kind'] == 'native'
    assert issued['customer']['external_user_id'] is None
    c = conversation(client)
    assert send(client, c, 'An administrator acting as a customer').json()['sender_role'] == 'customer'
    assert client.get('/api/v1/admin/chat/conversations').status_code == 403
    assert client.patch('/api/v1/admin/chat/conversations/' + c['id'], json={'state': 'closed', 'version': c['version']}).status_code == 403
    with w['app'].state.storage.transaction(True) as db:
        db.execute('UPDATE sessions SET expires_at=? WHERE token_hash=?', (time.time() - 1, digest(account['token'])))
    assert client.get('/api/v1/chat/conversations/' + c['id']).status_code == 401
    fresh_issuer, _ = native(w['app'])
    fresh, renewed = customer(w, None, issuer=fresh_issuer)
    assert renewed['customer']['id'] == issued['customer']['id']
    assert fresh.get('/api/v1/chat/conversations/' + c['id']).status_code == 200


def test_chat_event_is_body_free_atomic_signed_and_immutable_on_retry(workflow, monkeypatch):
    w = workflow; enable(w)
    secret = configured_chat_webhook(w)
    client, issued = customer(w)
    c = conversation(client)
    store = w['app'].state.storage
    original = store._persist_event
    def interrupted(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError('synthetic failure after event and job persistence')
    with monkeypatch.context() as patch:
        patch.setattr(store, '_persist_event', interrupted)
        with pytest.raises(RuntimeError):
            send(client, c, 'This transaction must disappear')
    assert client.get('/api/v1/chat/conversations/' + c['id'] + '/messages').json()['items'] == []
    assert store.deliveries(w['p']['id'], 25, 0) == []
    mid = str(uuid4())
    message = send(client, c, 'Private body never included in a webhook', mid).json()
    assert send(client, c, 'Private body never included in a webhook', mid).json()['id'] == message['id']
    queued = store.deliveries(w['p']['id'], 25, 0)
    assert len(queued) == 1
    delivery = store.delivery(w['p']['id'], queued[0]['id'])
    payload = delivery['payload']
    assert payload['type'] == 'chat.message_created'
    assert payload['conversation']['id'] == c['id']
    assert payload['project'] == {'id': w['p']['id'], 'identifier': w['p']['identifier']}
    assert payload['message'] == {'id': message['id'], 'sequence': 1, 'sender_role': 'customer'}
    assert payload['customer']['external_user_id'] == ' 0007 Alice '
    assert payload['customer']['id'] == issued['customer']['id']
    serialized = json.dumps(payload)
    for value in ['Private body', 'This transaction', issued['token'], secret, w['k']['secret'], w['cfg'].secret, PASSWORD]:
        assert value not in serialized
    assert 'body' not in payload['message']
    observed = []
    def transport(url, headers, body, internal):
        expected = hmac.new(secret.encode(), headers['X-Feedback-Timestamp'].encode() + b'.' + body, hashlib.sha256).hexdigest()
        assert hmac.compare_digest(headers['X-Feedback-Signature'], 'v1=' + expected)
        observed.append((url, headers['X-Feedback-Event-ID'], body))
        return 503 if len(observed) == 1 else 204
    worker = WebhookWorker(store, Vault(w['cfg'].secret), True, transport)
    assert worker.once()
    assert store.delivery(w['p']['id'], delivery['id'])['state'] == 'queued'
    version = store.project(w['p']['id'])['version']
    assert w['admin'].put('/api/v1/projects/' + w['p']['id'] + '/webhook', json={
        'url': 'http://127.0.0.1:9998/rotated', 'enabled': True, 'events': [],
        'rotate_secret': True, 'version': version,
    }).status_code == 200
    assert worker.once(time.time() + 10000)
    assert observed[0] == observed[1]
    assert store.delivery(w['p']['id'], delivery['id'])['state'] == 'succeeded'


def test_admin_and_anonymous_chat_events_keep_identity_roles_without_body(workflow):
    w = workflow; enable(w); configured_chat_webhook(w)
    guest, c, token = visitor(w)
    assert send(guest, c, 'Anonymous private question').status_code == 201
    assert send(w['admin'], c, 'Administrator private reply').status_code == 201
    store = w['app'].state.storage
    payloads = [store.delivery(w['p']['id'], row['id'])['payload'] for row in store.deliveries(w['p']['id'], 25, 0)]
    assert len(payloads) == 2
    assert {p['message']['sender_role'] for p in payloads} == {'customer', 'admin'}
    assert len({p['customer']['id'] for p in payloads}) == 1
    assert all(p['customer']['kind'] == 'anonymous' and p['customer']['external_user_id'] is None for p in payloads)
    assert token not in json.dumps(payloads) and 'private' not in json.dumps(payloads)


def test_customer_api_history_and_reads_survive_restart_and_backup(workflow, tmp_path):
    w = workflow; enable(w)
    client, issued = customer(w)
    c = conversation(client); send(client, c, 'Durable question'); send(w['admin'], c, 'Durable reply')
    path = '/api/v1/chat/conversations/' + c['id']
    assert client.post(path + '/read', json={'sequence': 2}).status_code == 200
    backup = tmp_path / 'chat-restored.sqlite'
    w['app'].state.storage.backup(backup)
    for config in (w['cfg'], replace(w['cfg'], database_path=backup)):
        with TestClient(create_app(config)) as restored:
            restored.headers['X-Chat-Token'] = issued['token']
            assert restored.get(path).json()['unread_count'] == 0
            assert [x['body'] for x in restored.get(path + '/messages').json()['items']] == ['Durable question', 'Durable reply']
            assert own(restored)[0]['customer']['id'] == issued['customer']['id']
