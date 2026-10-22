"""Real SQLite chat contracts: identity, durability, idempotence and limits."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from uuid import uuid4
import time
import pytest
from fastapi.testclient import TestClient
from feedback.api import create_app
from feedback.security import digest
from feedback.storage.sqlite import SQLiteStorage
from conftest import PASSWORD

ORIGIN='https://website.example'

def enable(w, project=None, origins=None):
    p=project or w['p']
    result=w['admin'].put('/api/v1/admin/chat/projects/'+p['id'],json={'enabled':True,'origins':origins or [ORIGIN],'version':0})
    assert result.status_code==200,result.text
    return result.json()

def visitor(w, project=None, origin=ORIGIN):
    client=TestClient(w['app'])
    client.headers['Origin']=origin
    result=client.post('/api/v1/chat/projects/'+(project or w['p'])['identifier']+'/sessions')
    assert result.status_code==201,result.text
    data=result.json()
    client.headers['X-Chat-Token']=data['token']
    return client,data['conversation'],data['token']

def send(client,c,body='Hello',mid=None):
    return client.post('/api/v1/chat/conversations/'+c['id']+'/messages',json={'client_message_id':mid or str(uuid4()),'body':body})

def test_chat_default_disabled_origin_and_identity(workflow):
    w=workflow
    assert w['anon'].post('/api/v1/chat/projects/glean/sessions',headers={'Origin':ORIGIN}).status_code==404
    enable(w)
    assert w['anon'].post('/api/v1/chat/projects/glean/sessions',headers={'Origin':'https://evil.example'}).status_code==403
    first,c,token=visitor(w)
    second,other,_=visitor(w)
    assert send(first,c).status_code==201
    assert second.get('/api/v1/chat/conversations/'+c['id']).status_code==404
    assert send(second,c).status_code==404
    for client in (w['user'],w['manager'],w['trusted'],w['anon']):
        assert client.get('/api/v1/admin/chat/conversations').status_code==403
        assert client.get('/api/v1/chat/conversations/'+c['id']+'/messages').status_code==403
    assert first.get('/api/v1/admin/chat/conversations').status_code==403
    assert first.get('/api/v1/chat/conversations/'+c['id'],headers={'Origin':'https://evil.example'}).status_code==403
    enable(w,w['q'])
    cross,q,_=visitor(w,w['q'])
    assert send(first,q).status_code==404
    with w['app'].state.storage.transaction() as db:
        saved=db.execute('SELECT token_hash FROM chat_sessions WHERE conversation_id=?',(c['id'],)).fetchone()[0]
    assert saved==digest(token) and token not in saved
    assert 'sender_id' not in send(first,c).json()


def test_chat_idempotence_cursor_unread_and_close(workflow):
    w=workflow;enable(w);v,c,_=visitor(w);mid=str(uuid4())
    def action(_):return send(v,c,mid=mid)
    with ThreadPoolExecutor(max_workers=6) as pool:
        replies=list(pool.map(action,range(8)))
    assert all(x.status_code==201 for x in replies)
    assert {x.json()['sequence'] for x in replies}=={1}
    assert len({x.json()['id'] for x in replies})==1
    assert send(v,c,'Changed',mid).status_code==409
    listing=w['admin'].get('/api/v1/admin/chat/conversations').json()['items']
    assert len(listing)==1 and listing[0]['unread_count']==1
    read='/api/v1/chat/conversations/'+c['id']+'/read'
    assert w['admin'].post(read,json={'sequence':10000}).json()=={'read_sequence':1}
    assert w['admin'].get('/api/v1/admin/chat/conversations').json()['items'][0]['unread_count']==0
    assert send(w['admin'],c,'Reply').json()['sequence']==2
    assert v.get('/api/v1/chat/conversations/'+c['id']).json()['unread_count']==1
    assert v.post(read,json={'sequence':2}).json()['read_sequence']==2
    assert v.post(read,json={'sequence':0}).json()['read_sequence']==2
    page=v.get('/api/v1/chat/conversations/'+c['id']+'/messages?limit=1').json()
    assert page['has_more'] and page['next_sequence']==1
    assert v.get('/api/v1/chat/conversations/'+c['id']+'/messages?after=1').json()['items'][0]['body']=='Reply'
    url='/api/v1/admin/chat/conversations/'+c['id']
    closed=w['admin'].patch(url,json={'state':'closed','version':1}).json()
    assert closed['version']==2
    assert send(v,c).status_code==409
    assert send(v,c,mid=mid).status_code==201 # lost acknowledgement can still retry
    assert w['admin'].patch(url,json={'state':'open','version':1}).status_code==409
    assert w['admin'].patch(url,json={'state':'open','version':2}).status_code==200
    assert send(v,c).status_code==201


def test_chat_origin_configuration_versions_and_cors(workflow):
    w=workflow;enable(w);v,c,_=visitor(w)
    preflight={'Origin':ORIGIN,'Access-Control-Request-Method':'POST','Access-Control-Request-Headers':'Content-Type,X-Chat-Token'}
    r=w['anon'].options('/api/v1/chat/tickets',headers=preflight)
    assert r.status_code==204 and r.headers['access-control-allow-origin']==ORIGIN
    assert 'access-control-allow-credentials' not in r.headers
    assert w['anon'].options('/api/v1/chat/tickets',headers={**preflight,'Origin':'https://evil.example'}).status_code==403
    assert w['anon'].options('/api/v1/chat/tickets',headers={**preflight,'Access-Control-Request-Headers':'Authorization'}).status_code==403
    r=send(v,c);assert r.headers['access-control-allow-origin']==ORIGIN
    assert w['admin'].put('/api/v1/admin/chat/projects/glean',json={'enabled':True,'origins':[ORIGIN],'version':0}).status_code==409
    assert w['admin'].put('/api/v1/admin/chat/projects/glean',json={'enabled':True,'origins':['https://bad.example/path'],'version':1}).status_code==400
    assert w['admin'].put('/api/v1/admin/chat/projects/glean',json={'enabled':False,'origins':[ORIGIN],'version':1}).status_code==200
    assert send(v,c).status_code==403


def test_chat_revocation_expiration_and_native_logout(workflow):
    w=workflow;enable(w);v,c,token=visitor(w)
    native=TestClient(w['app'])
    result=native.post('/api/v1/auth/native/login',json={'email':'admin@example.com','password':PASSWORD}).json()
    native.headers['Authorization']='Bearer '+result['token']
    assert send(native,c,'Admin reply').status_code==201
    ticket=native.post('/api/v1/chat/tickets',json={'conversation_id':c['id']}).json()
    assert native.post('/api/v1/auth/logout').status_code==204
    with pytest.raises(Exception):w['app'].state.storage.chat_consume_ticket(digest(ticket['ticket']),None)
    assert v.delete('/api/v1/chat/conversations/'+c['id']+'/session').status_code==204
    assert send(v,c).status_code==401
    other,new,token=visitor(w)
    with w['app'].state.storage.transaction(True) as db:db.execute('UPDATE chat_sessions SET expires_at=? WHERE token_hash=?',(time.time()-1,digest(token)))
    assert other.get('/api/v1/chat/conversations/'+new['id']).status_code==401


def test_chat_backup_restart_and_atomic_push_failure(workflow,tmp_path,monkeypatch):
    w=workflow;enable(w);v,c,token=visitor(w);store=w['app'].state.storage
    def fail(*args):raise RuntimeError('test transaction rollback')
    with monkeypatch.context() as m:
        m.setattr(store,'_queue_push',fail)
        with pytest.raises(RuntimeError):send(v,c)
    assert v.get('/api/v1/chat/conversations/'+c['id']+'/messages').json()['items']==[]
    assert send(v,c,'Persisted').status_code==201
    target=tmp_path/'restore.sqlite';store.backup(target)
    restored=SQLiteStorage(target,w['cfg'].base_url)
    p=restored.chat_visitor(digest(token),ORIGIN)
    assert restored.chat_messages(c['id'],p,0,100)[0]['body']=='Persisted'
    restarted=TestClient(create_app(w['cfg']))
    restarted.headers.update({'Origin':ORIGIN,'X-Chat-Token':token})
    assert restarted.get('/api/v1/chat/conversations/'+c['id']+'/messages').json()['items'][0]['body']=='Persisted'


def test_chat_rate_and_content_bounds(workflow):
    w=workflow;enable(w);v,c,_=visitor(w)
    assert send(v,c,' ').status_code==400
    assert send(v,c,'x'*4001).status_code==400
    assert send(v,c,'<script>alert(1)</script>').json()['body']=='<script>alert(1)</script>'
    for _ in range(29):assert send(v,c).status_code==201
    assert send(v,c).status_code==429
    assert v.get('/api/v1/chat/conversations/'+c['id']+'/messages?limit=101').status_code==400


def test_response_time_samples_truthful(workflow):
    w=workflow;enable(w);service=w['app'].state.chat_service
    assert service.response_time(w['p']['id'])['label'] is None
    base=datetime.now(timezone.utc)-timedelta(days=2)
    ids=[]
    for i in range(5):
        v,c,_=visitor(w);send(v,c);send(w['admin'],c,'Reply');ids.append(c['id'])
        with w['app'].state.storage.transaction(True) as db:
            db.execute('UPDATE chat_conversations SET first_message_at=?,first_reply_at=? WHERE id=?',((base+timedelta(hours=i)).isoformat().replace('+00:00','Z'),(base+timedelta(hours=i,minutes=10+i)).isoformat().replace('+00:00','Z'),c['id']))
    metric=service.response_time(w['p']['id'])
    assert metric['answered_count']==5 and metric['median_seconds']==720
    assert metric['label']=='Typically responds in 12 minutes'
    assert send(w['admin'],{'id':ids[-1]},'A second reply').status_code==201
    assert service.response_time(w['p']['id'])['median_seconds']==720
    for _ in range(5):v,c,_=visitor(w);send(v,c)
    metric=service.response_time(w['p']['id'])
    assert metric['unanswered_count']==5 and metric['label'] is None and not metric['recent_reply']
    with w['app'].state.storage.transaction(True) as db:
        db.execute('UPDATE chat_conversations SET first_message_at=? WHERE id=?',((base-timedelta(days=40)).isoformat().replace('+00:00','Z'),ids[0]))
    assert service.response_time(w['p']['id'])['answered_count']==4


def test_chat_alerts_only_committed_messages_and_five_minute_coalescing(workflow,monkeypatch):
    from test_push import configure,device
    w=workflow;enable(w);configure(w,monkeypatch);admin,did=device(w)
    v,c,_=visitor(w);store=w['app'].state.storage
    assert store.push_deliveries(100,0)==[]
    ticket=v.post('/api/v1/chat/tickets',json={'conversation_id':c['id']})
    assert ticket.status_code==200 and store.push_deliveries(100,0)==[]
    first=send(v,c,'Private message content').json()
    assert len(store.push_deliveries(100,0))==1
    job=store.push_deliveries(100,0)[0]
    assert job['kind']=='chat.message' and job['event_id']==first['id'] and job['resource_id']==c['id']
    assert 'Private message content' not in str(job)
    send(v,c,'Another message')
    assert len(store.push_deliveries(100,0))==1
    with store.transaction(True) as db:db.execute('UPDATE chat_conversations SET last_alert_at=? WHERE id=?',(time.time()-301,c['id']))
    send(v,c,'Later message')
    assert len(store.push_deliveries(100,0))==2
    w['admin'].put('/api/v1/admin/push',json={'enabled':False})
    second,other,_=visitor(w);assert send(second,other).status_code==201
    assert len(store.push_deliveries(100,0))==2
