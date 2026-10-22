"""Real customer WebSocket authorization, durable retry, and live revocation."""
import time
from uuid import uuid4
import pytest
from starlette.websockets import WebSocketDisconnect
from feedback.security import digest
from test_chat import ORIGIN, enable, send
from test_chat_websocket import frame, ticket
from test_chat_customers import customer, conversation


def test_native_customer_socket_ack_reconnect_and_cross_device_read(workflow):
    w = workflow; enable(w)
    client, issued = customer(w)
    c = conversation(client)
    mid = str(uuid4())
    with w['anon'].websocket_connect('/api/v1/chat/ws') as ws:
        ws.send_json({'ticket': ticket(client, c), 'after': 0})
        assert frame(ws, 'ready')['conversation_id'] == c['id']
        ws.send_json({'type': 'message', 'message': {'client_message_id': mid, 'body': 'Native customer message'}})
        message = frame(ws, 'ack')['message']
        assert message['sender_role'] == 'customer' and message['sequence'] == 1
        assert send(w['admin'], c, 'Administrator answer').status_code == 201
        assert frame(ws, 'message')['message']['sequence'] == 1
        assert frame(ws, 'message')['message']['body'] == 'Administrator answer'
        ws.send_json({'type': 'read', 'sequence': 2})
        # Read frames have no acknowledgment. Keep the transport open until the
        # durable cursor advances rather than racing disconnect against receive.
        deadline = time.monotonic() + 5
        while client.get('/api/v1/chat/conversations/' + c['id']).json()['unread_count']:
            assert time.monotonic() < deadline, 'Socket read cursor did not commit'
            time.sleep(0.01)
    renewed, data = customer(w)
    assert data['customer']['id'] == issued['customer']['id']
    assert send(renewed, c, 'Native customer message', mid).json()['id'] == message['id']
    assert renewed.get('/api/v1/chat/conversations/' + c['id']).json()['unread_count'] == 0
    assert send(w['admin'], c, 'Sent while disconnected').status_code == 201
    with w['anon'].websocket_connect('/api/v1/chat/ws') as ws:
        ws.send_json({'ticket': ticket(renewed, c), 'after': 2})
        frame(ws, 'ready')
        assert frame(ws, 'message')['message']['body'] == 'Sent while disconnected'


def test_browser_customer_ticket_binds_origin_expires_and_cannot_replay(workflow):
    w = workflow; enable(w, origins=[ORIGIN, 'https://second.example'])
    client, _ = customer(w, client_type='browser', origin=ORIGIN)
    c = conversation(client)
    token = ticket(client, c)
    with w['anon'].websocket_connect('/api/v1/chat/ws', headers={'Origin': ORIGIN}) as ws:
        ws.send_json({'ticket': token, 'after': 0}); frame(ws, 'ready')
    with w['anon'].websocket_connect('/api/v1/chat/ws', headers={'Origin': ORIGIN}) as ws:
        ws.send_json({'ticket': token, 'after': 0})
        assert frame(ws, 'error')['error']['code'] == 'unauthorized'
    token = ticket(client, c)
    with w['anon'].websocket_connect('/api/v1/chat/ws', headers={'Origin': 'https://second.example'}) as ws:
        ws.send_json({'ticket': token, 'after': 0})
        assert frame(ws, 'error')['error']['code'] == 'unauthorized'
    token = ticket(client, c)
    with w['app'].state.storage.transaction(True) as db:
        db.execute('UPDATE chat_tickets SET expires_at=? WHERE token_hash=?', (time.time() - 1, digest(token)))
    with w['anon'].websocket_connect('/api/v1/chat/ws', headers={'Origin': ORIGIN}) as ws:
        ws.send_json({'ticket': token, 'after': 0})
        assert frame(ws, 'error')['error']['code'] == 'unauthorized'


@pytest.mark.parametrize('revocation', ['self', 'expiry', 'issuer_key', 'native_parent'])
def test_active_customer_socket_closes_when_access_is_revoked(workflow, revocation):
    w = workflow; enable(w)
    if revocation == 'native_parent':
        client, issued = customer(w, None, issuer=w['user'])
    else:
        client, issued = customer(w)
    c = conversation(client)
    with w['anon'].websocket_connect('/api/v1/chat/ws') as ws:
        ws.send_json({'ticket': ticket(client, c), 'after': 0}); frame(ws, 'ready')
        if revocation == 'self':
            assert client.delete('/api/v1/chat/session').status_code == 204
        elif revocation == 'expiry':
            with w['app'].state.storage.transaction(True) as db:
                db.execute('UPDATE chat_customer_sessions SET expires_at=? WHERE token_hash=?', (time.time() - 1, digest(issued['token'])))
        elif revocation == 'issuer_key':
            assert w['admin'].delete('/api/v1/projects/' + w['p']['id'] + '/keys/' + w['k']['id']).status_code == 204
        else:
            assert w['user'].post('/api/v1/auth/logout').status_code == 204
        ws.send_json({'type': 'ping'})
        assert frame(ws, 'error')['error']['code'] in ('unauthorized', 'not_found', 'forbidden')
        with pytest.raises(WebSocketDisconnect):
            ws.receive_json()


def test_customer_ticket_does_not_authorize_another_customer_or_project(workflow):
    w = workflow; enable(w); enable(w, w['q'])
    client, _ = customer(w)
    other, _ = customer(w, 'other')
    cross, _ = customer(w, project=w['q'], issuer=w['other'])
    c = conversation(client)
    other_c = conversation(other)
    for denied in (other, cross):
        assert denied.post('/api/v1/chat/tickets', json={'conversation_id': c['id']}).status_code == 404
    token = ticket(client, c)
    assert client.delete('/api/v1/chat/session').status_code == 204
    with w['anon'].websocket_connect('/api/v1/chat/ws') as ws:
        ws.send_json({'ticket': token, 'after': 0})
        assert frame(ws, 'error')['error']['code'] in ('unauthorized', 'not_found', 'forbidden')
    assert other.get('/api/v1/chat/conversations/' + other_c['id'] + '/messages').json()['items'] == []
