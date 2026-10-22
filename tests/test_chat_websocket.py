from uuid import uuid4
import time
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from feedback.security import digest
from test_chat import ORIGIN,enable,visitor,send


def ticket(client,c):
    r=client.post('/api/v1/chat/tickets',json={'conversation_id':c['id']})
    assert r.status_code==200,r.text
    return r.json()['ticket']


def frame(ws,kind):
    for _ in range(10):
        data=ws.receive_json()
        if data['type']==kind:return data
        assert data['type'] not in ('error',),data
    pytest.fail('Expected WebSocket frame missing')


def test_socket_two_party_durable_ack_and_reconnect(workflow):
    w=workflow;enable(w);v,c,_=visitor(w)
    vt,at=ticket(v,c),ticket(w['admin'],c)
    with v.websocket_connect('/api/v1/chat/ws',headers={'Origin':ORIGIN}) as visitor_ws:
        visitor_ws.send_json({'ticket':vt,'after':0});assert frame(visitor_ws,'ready')['conversation_id']==c['id']
        with w['admin'].websocket_connect('/api/v1/chat/ws',headers={'Origin':w['cfg'].base_url}) as admin_ws:
            admin_ws.send_json({'ticket':at,'after':0});frame(admin_ws,'ready')
            mid=str(uuid4())
            visitor_ws.send_json({'type':'message','message':{'client_message_id':mid,'body':'Live hello'}})
            assert frame(visitor_ws,'ack')['message']['sequence']==1
            assert frame(admin_ws,'message')['message']['body']=='Live hello'
            assert send(w['admin'],c,'Live reply').status_code==201
            assert frame(visitor_ws,'message')['message']['sequence']==1
            assert frame(visitor_ws,'message')['message']['body']=='Live reply'
            visitor_ws.send_json({'type':'typing'})
            assert frame(admin_ws,'typing')['sender']=='visitor'
            assert send(v,c,'Live hello',mid).json()['sequence']==1
    assert send(w['admin'],c,'While disconnected').status_code==201
    with v.websocket_connect('/api/v1/chat/ws',headers={'Origin':ORIGIN}) as ws:
        ws.send_json({'ticket':ticket(v,c),'after':2})
        assert frame(ws,'message')['message']['body']=='While disconnected'


def test_socket_ticket_single_use_origin_expiry_and_revoke(workflow):
    w=workflow;enable(w);v,c,_=visitor(w);secret=ticket(v,c)
    with v.websocket_connect('/api/v1/chat/ws',headers={'Origin':ORIGIN}) as ws:
        ws.send_json({'ticket':secret,'after':0});frame(ws,'ready')
    with v.websocket_connect('/api/v1/chat/ws',headers={'Origin':ORIGIN}) as ws:
        ws.send_json({'ticket':secret,'after':0});assert frame(ws,'error')['error']['code']=='unauthorized'
    secret=ticket(v,c)
    with w['app'].state.storage.transaction(True) as db:db.execute('UPDATE chat_tickets SET expires_at=? WHERE token_hash=?',(time.time()-1,digest(secret)))
    with v.websocket_connect('/api/v1/chat/ws',headers={'Origin':ORIGIN}) as ws:
        ws.send_json({'ticket':secret,'after':0});assert frame(ws,'error')['error']['code']=='unauthorized'
    secret=ticket(v,c)
    with pytest.raises(WebSocketDisconnect):
        with v.websocket_connect('/api/v1/chat/ws',headers={'Origin':'https://evil.example'}) as ws:ws.send_json({'ticket':secret})
    with v.websocket_connect('/api/v1/chat/ws',headers={'Origin':ORIGIN}) as ws:
        ws.send_json({'ticket':secret,'after':0});frame(ws,'ready')
        v.delete('/api/v1/chat/conversations/'+c['id']+'/session')
        assert frame(ws,'error')['error']['code']=='not_found'


def test_socket_native_revocation_and_no_cross_injection(workflow):
    w=workflow;enable(w);v,c,_=visitor(w)
    with w['admin'].websocket_connect('/api/v1/chat/ws',headers={'Origin':w['cfg'].base_url}) as ws:
        ws.send_json({'ticket':ticket(w['admin'],c),'after':0});frame(ws,'ready')
        w['admin'].post('/api/v1/auth/logout')
        assert frame(ws,'error')['error']['code']=='unauthorized'
    other,q,_=visitor(w)
    assert v.post('/api/v1/chat/tickets',json={'conversation_id':q['id']}).status_code==404
    with v.websocket_connect('/api/v1/chat/ws',headers={'Origin':ORIGIN}) as ws:
        ws.send_json({'ticket':ticket(v,c),'after':0});frame(ws,'ready')
        ws.send_json({'type':'message','message':{'client_message_id':str(uuid4()),'body':'bad','conversation_id':q['id']}})
        with pytest.raises(WebSocketDisconnect):
            while True:ws.receive_json()
    assert other.get('/api/v1/chat/conversations/'+q['id']+'/messages').json()['items']==[]
