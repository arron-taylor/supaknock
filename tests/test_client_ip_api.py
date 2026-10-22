"""Connection metadata through real HTTP/WebSocket authorization and SQLite."""
from uuid import uuid4

from fastapi.testclient import TestClient
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from conftest import PASSWORD, submission
from test_chat import ORIGIN, enable, send
from test_chat_customers import customer, conversation


def peer(w, address, as_client=None, forwarded=None, trust=None):
    app = ProxyHeadersMiddleware(w['app'], trusted_hosts=trust or [])
    client = TestClient(app, client=(address, 54321))
    if as_client is not None:
        client.cookies.update(as_client.cookies)
        client.headers.update(as_client.headers)
    if forwarded:
        client.headers['X-Forwarded-For'] = forwarded
        client.headers['X-Real-IP'] = '192.0.2.199'
    return client


def test_request_ip_private_and_headers_trusted_only_at_proxy_boundary(workflow):
    w = workflow
    untrusted = peer(w, '198.51.100.10', w['user'], '192.0.2.222', ['10.0.0.0/8'])
    item = submission(untrusted, w['b'])
    assert 'submitted_ip_address' not in item
    rid = item['id']
    assert w['admin'].get('/api/v1/requests/' + rid).json()['submitted_ip_address'] == '198.51.100.10'
    approved = peer(w, '10.0.0.10', w['admin'], '192.0.2.222, 203.0.113.20, 10.0.0.2', ['10.0.0.0/8'])
    assert approved.post('/api/v1/requests/' + rid + '/accept', json={'version': 1}).status_code == 200
    audit = w['admin'].get('/api/v1/requests/' + rid + '/history').json()['items']
    assert [e['ip_address'] for e in audit] == ['198.51.100.10', '203.0.113.20']
    for caller in (w['user'], w['anon'], w['trusted'], w['manager']):
        assert 'submitted_ip_address' not in caller.get('/api/v1/requests/' + rid).json()
        assert 'ip_address' not in caller.get('/api/v1/requests/' + rid + '/history').text
        assert 'submitted_ip_address' not in caller.get('/api/v1/boards/' + w['b']['id'] + '/requests').text
    rejected = untrusted.post('/api/v1/boards/' + w['b']['id'] + '/requests', json={'title': 'Spoofed', 'ip_address': '1.2.3.4'})
    assert rejected.status_code == 400


def test_backend_submission_records_backend_peer_not_external_identity(workflow):
    w = workflow
    backend = peer(w, '2001:0db8:0:0::7', w['trusted'], '198.51.100.123')
    item = submission(backend, w['b'], ' opaque-customer ')
    assert w['admin'].get('/api/v1/requests/' + item['id']).json()['submitted_ip_address'] == '2001:db8::7'
    assert 'submitted_ip_address' not in backend.get('/api/v1/me/requests?external_user_id=%20opaque-customer%20').text


def test_chat_ip_http_and_socket_reconnect_is_admin_only(workflow):
    w = workflow
    enable(w)
    visitor = peer(w, '198.51.100.11', forwarded='192.0.2.222')
    visitor.headers['Origin'] = ORIGIN
    issued = visitor.post('/api/v1/chat/projects/glean/sessions').json()
    assert 'ip_address' not in str(issued)
    visitor.headers['X-Chat-Token'] = issued['token']
    c = issued['conversation']
    response = send(visitor, c)
    assert response.status_code == 201 and 'ip_address' not in response.text
    ticket = visitor.post('/api/v1/chat/tickets', json={'conversation_id': c['id']}).json()['ticket']
    socket_peer = peer(w, '2001:db8::12', visitor)
    mid = str(uuid4())
    with socket_peer.websocket_connect('/api/v1/chat/ws', headers={'Origin': ORIGIN}) as ws:
        ws.send_json({'ticket': ticket, 'after': 1})
        assert ws.receive_json()['type'] == 'ready'
        frame = ws.receive_json()
        assert frame['type'] == 'state' and 'ip_address' not in str(frame)
        ws.send_json({'type': 'message', 'message': {'client_message_id': mid, 'body': 'From a new connection'}})
        ack = ws.receive_json()
        assert ack['type'] == 'ack' and 'ip_address' not in str(ack)
    detail = w['admin'].get('/api/v1/admin/chat/conversations/' + c['id']).json()
    assert detail['initial_ip_address'] == '198.51.100.11'
    assert detail['last_customer_ip_address'] == '2001:db8::12'
    messages = w['admin'].get('/api/v1/chat/conversations/' + c['id'] + '/messages').json()['items']
    assert [m['ip_address'] for m in messages] == ['198.51.100.11', '2001:db8::12']
    assert send(visitor, c, 'From a new connection', mid).status_code == 201
    assert send(peer(w, '192.0.2.90', w['admin']), c, 'Administrator reply').status_code == 201
    detail = w['admin'].get('/api/v1/admin/chat/conversations/' + c['id']).json()
    assert detail['last_customer_ip_address'] == '2001:db8::12'
    assert 'ip_address' not in visitor.get('/api/v1/chat/conversations/' + c['id']).text
    assert 'ip_address' not in visitor.get('/api/v1/chat/conversations/' + c['id'] + '/messages').text
    assert visitor.get('/api/v1/admin/account-activity').status_code in (401, 403)


def test_authenticated_chat_records_actual_customer_client_and_keeps_events_clean(workflow):
    w = workflow
    enable(w)
    hook = w['admin'].put('/api/v1/projects/' + w['p']['id'] + '/webhook', json={'url': 'http://127.0.0.1:9999/events', 'enabled': True, 'events': ['chat.message_created'], 'version': 1})
    assert hook.status_code == 200
    original, session = customer(w)
    client = peer(w, '203.0.113.30', original)
    c = conversation(client)
    assert 'ip_address' not in c
    assert send(client, c).status_code == 201
    detail = w['admin'].get('/api/v1/admin/chat/conversations/' + c['id']).json()
    assert detail['initial_ip_address'] == '203.0.113.30'
    assert 'ip_address' not in client.get('/api/v1/chat/conversations').text
    deliveries = w['admin'].get('/api/v1/projects/' + w['p']['id'] + '/deliveries').json()['items']
    payload = w['admin'].get('/api/v1/projects/' + w['p']['id'] + '/deliveries/' + deliveries[0]['id']).json()['payload']
    assert 'ip_address' not in str(payload) and '203.0.113.30' not in str(payload)
    assert 'ip_address' not in w['admin'].get('/api/v1/admin/notifications').text


def test_account_activity_successful_signup_login_permissions_and_pagination(workflow):
    w = workflow
    new = peer(w, '198.51.100.40', forwarded='192.0.2.222')
    signed = new.post('/api/v1/auth/register', json={'email': 'network@example.com', 'password': PASSWORD, 'display_name': 'Network tester'})
    assert signed.status_code == 201 and 'ip_address' not in signed.text
    uid = signed.json()['user']['id']
    bad = new.post('/api/v1/auth/login', json={'email': 'network@example.com', 'password': 'wrong'})
    assert bad.status_code == 401
    path = '/api/v1/admin/account-activity?user_id=' + uid
    records = w['admin'].get(path).json()['items']
    assert len(records) == 2 and {r['event_type'] for r in records} == {'login', 'signup'}
    assert {r['ip_address'] for r in records} == {'198.51.100.40'}
    assert all(r['email'] == 'network@example.com' for r in records)
    page = w['admin'].get(path + '&limit=1').json()
    assert len(page['items']) == 1 and page['has_more']
    assert w['admin'].get(path + '&limit=1&offset=1').json()['items'][0]['id'] != page['items'][0]['id']
    native = peer(w, '2001:db8::41')
    signed = native.post('/api/v1/auth/native/login', json={'email': 'admin@example.com', 'password': PASSWORD})
    assert signed.status_code == 200 and 'ip_address' not in signed.text
    native.headers['Authorization'] = 'Bearer ' + signed.json()['token']
    rows = native.get('/api/v1/admin/account-activity?user_id=' + signed.json()['user']['id']).json()['items']
    assert rows[0]['transport'] == 'native' and rows[0]['ip_address'] == '2001:db8::41'
    for caller in (w['anon'], w['user'], w['manager'], w['trusted'], new):
        assert caller.get(path).status_code == (401 if caller is w['anon'] else 403)
    assert native.post('/api/v1/auth/logout').status_code == 204
    assert native.get(path).status_code == 401
    assert w['admin'].get('/api/v1/admin/account-activity?limit=101').status_code == 400


def test_non_ip_peer_is_unknown_not_arbitrary_text(workflow):
    w = workflow
    for address in ('unknown', '2001:db8::1%zone', '127.0.0.1:5000'):
        c = peer(w, address, w['user'])
        r = submission(c, w['b'])
        assert w['admin'].get('/api/v1/requests/' + r['id']).json()['submitted_ip_address'] is None


def test_admin_socket_refreshes_customer_ip_after_new_message(workflow):
    w = workflow
    enable(w)
    visitor = peer(w, '198.51.100.60')
    visitor.headers['Origin'] = ORIGIN
    issued = visitor.post('/api/v1/chat/projects/glean/sessions').json()
    visitor.headers['X-Chat-Token'] = issued['token']
    cid = issued['conversation']['id']
    admin = peer(w, '192.0.2.60', w['admin'])
    ticket = admin.post('/api/v1/chat/tickets', json={'conversation_id': cid}).json()['ticket']
    with admin.websocket_connect('/api/v1/chat/ws', headers={'Origin': w['cfg'].base_url}) as ws:
        ws.send_json({'ticket': ticket, 'after': 0})
        assert ws.receive_json()['type'] == 'ready'
        initial = ws.receive_json()['conversation']
        changed = peer(w, '2001:db8::60', visitor)
        assert send(changed, issued['conversation']).status_code == 201
        state = ws.receive_json()
        assert state['type'] == 'state'
        assert state['conversation']['version'] == initial['version']
        assert state['conversation']['last_customer_ip_address'] == '2001:db8::60'
        assert ws.receive_json()['message']['ip_address'] == '2001:db8::60'
