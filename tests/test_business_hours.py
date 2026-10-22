"""Schedules and private contacts against actual SQLite and HTTP authority boundaries."""
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4
import json
import pytest
from pydantic import ValidationError
from feedback.business_hours import WeeklyHours, availability
from feedback.storage.sqlite import SQLiteStorage
from feedback.security import digest
from test_chat import enable, visitor, send, ORIGIN

ACCOUNT='/api/v1/admin/chat/business-hours'

def schedule(days=None, zone='America/Chicago', **values):
    return dict(enabled=True,timezone=zone,days=days or [[{'start':'09:00','end':'17:00'}] for _ in range(5)]+[[],[]],collect_email=True,**values)

def save(w, data=None, project=None, version=0, mode='override'):
    url=ACCOUNT if project is None else '/api/v1/admin/chat/projects/'+project+'/business-hours'
    return w['admin'].put(url,json={'version':version,'mode':mode,'schedule':data or schedule()})

def moment(value):return datetime.fromisoformat(value.replace('Z','+00:00'))

@pytest.mark.parametrize('at,opened,next_at',[
 ('2026-10-05T13:59:59Z',False,'2026-10-05T14:00:00Z'),
 ('2026-10-05T14:00:00Z',True,None),('2026-10-05T21:59:59Z',True,None),
 ('2026-10-05T22:00:00Z',False,'2026-10-06T14:00:00Z'),
 ('2026-10-10T12:00:00Z',False,'2026-10-12T14:00:00Z')])
def test_boundaries(at,opened,next_at):
    a=availability(schedule(),'account',moment(at));assert a['is_open']==opened;assert a['next_open_at']==next_at

def test_dst_gap_fold_and_empty_week():
    days=[[] for _ in range(7)];days[6]=[{'start':'02:30','end':'04:00'}]
    a=availability(schedule(days),'account',moment('2026-03-08T07:50:00Z'))
    assert a['next_open_at']=='2026-03-08T08:00:00Z' # spring gap skips 02:30
    days[6]=[{'start':'01:30','end':'01:45'}]
    a=availability(schedule(days),'account',moment('2026-11-01T06:50:00Z'))
    assert a['next_open_at']=='2026-11-01T07:30:00Z' # second 01:30 remains valid
    assert availability(schedule([[] for _ in range(7)]),'project',moment('2026-10-06T00:00:00Z'))['next_open_at'] is None
    assert availability(WeeklyHours().model_dump(),'account')['is_open'] is None

@pytest.mark.parametrize('change',[
 {'timezone':'not/a-zone'},{'days':[]},{'days':[[{'start':'17:00','end':'09:00'}]]+[[]]*6},
 {'days':[[{'start':'09:00','end':'17:00'},{'start':'12:00','end':'18:00'}]]+[[]]*6},
 {'days':[[{'start':'24:00','end':'24:00'}]]+[[]]*6},
 {'days':[[{'start':'00:00','end':'24:00'}]*4]+[[]]*6}])
def test_invalid_schedules(change):
    with pytest.raises(ValidationError): WeeklyHours.model_validate(schedule()|change)

def test_inheritance_off_conflict_and_authorization(workflow):
    w=workflow;enable(w)
    url='/api/v1/chat/projects/glean/config';headers={'Origin':ORIGIN}
    assert not w['anon'].get(url,headers=headers).json()['business_hours']['enabled']
    assert save(w).status_code==200
    a=w['anon'].get(url,headers=headers).json()['business_hours'];assert a['enabled'] and a['source']=='account'
    assert save(w).status_code==409
    assert save(w,project='glean',mode='off').status_code==200
    assert not w['anon'].get(url,headers=headers).json()['business_hours']['enabled']
    assert save(w,project='glean',mode='override',version=1,data=schedule(zone='Asia/Tokyo')).status_code==200
    assert w['anon'].get(url,headers=headers).json()['business_hours']['timezone']=='Asia/Tokyo'
    assert save(w,project='glean',mode='inherit',version=2).status_code==200
    assert save(w,version=1,data=schedule(zone='Europe/London')).status_code==200
    assert w['anon'].get(url,headers=headers).json()['business_hours']['timezone']=='Europe/London'
    for client in [w['user'],w['trusted'],w['manager'],w['anon']]:
        assert client.get(ACCOUNT).status_code==403
        assert client.put(ACCOUNT,json={'version':2,'schedule':schedule()}).status_code==403
    with ThreadPoolExecutor(2) as pool:
        results=list(pool.map(lambda _:save(w,version=2).status_code,range(2)))
    assert sorted(results)==[200,409]

def test_contact_atomic_private_idempotent_clear_and_restore(workflow,tmp_path,monkeypatch):
    w=workflow;enable(w);assert save(w).status_code==200
    v,c,token=visitor(w);store=w['app'].state.storage
    path='/api/v1/chat/conversations/'+c['id'];email='visitor+support@example.test'
    body={'client_message_id':str(uuid4()),'body':'Please help with my order','follow_up_email':email}
    with monkeypatch.context() as m:
        def fail(*args,**kwargs):raise RuntimeError('synthetic failure')
        m.setattr(store,'_persist_event',fail)
        with pytest.raises(RuntimeError):v.post(path+'/messages',json=body)
    assert v.get(path).json()['follow_up_email'] is None
    result=v.post(path+'/messages',json=body);assert result.status_code==201
    assert 'follow_up_email' not in result.json() and email not in result.text
    assert v.post(path+'/messages',json=body).json()['id']==result.json()['id']
    assert v.post(path+'/messages',json=body|{'follow_up_email':'other@example.test'}).status_code==409
    own=v.get(path).json();assert own['follow_up_email']==email and own['contact_version']==1
    assert w['admin'].get('/api/v1/admin/chat/conversations/'+c['id']).json()['follow_up_email']==email
    with store.transaction() as db:
        # No follow-up email in immutable events, notifications or AI message material.
        for table in ['request_events','admin_notifications','push_deliveries','webhook_deliveries']:
            rows=[dict(x) for x in db.execute('SELECT * FROM '+table)]
            assert email not in json.dumps(rows)
    stranger,other,_=visitor(w)
    assert stranger.get(path).status_code==404
    assert stranger.post(path+'/contact/remove',json={'version':1}).status_code==404
    assert w['trusted'].get(path).status_code==403
    target=tmp_path/'hours-backup.sqlite';store.backup(target);restored=SQLiteStorage(target,w['cfg'].base_url)
    principal=restored.chat_visitor(digest(token),ORIGIN)
    assert restored.chat_conversation(c['id'],principal)['follow_up_email']==email
    assert restored.chat_business_hours('glean')['effective']['collect_email']
    assert v.post(path+'/contact/remove',json={'version':0}).status_code==409
    assert v.post(path+'/contact/remove',json={'version':1}).json()['follow_up_email'] is None
    assert v.post(path+'/messages',json=body).status_code==201 # retry must not restore removed contact
    assert v.get(path).json()['follow_up_email'] is None
    assert v.delete(path+'/session').status_code==204
    assert v.post(path+'/contact/remove',json={'version':2}).status_code==401

@pytest.mark.parametrize('email',['bad','a@b','a\nb@example.test','<a>@b.test','a@b.test\r\nBcc: x@y.test'])
def test_invalid_email_does_not_send(workflow,email):
    w=workflow;enable(w);save(w);v,c,_=visitor(w)
    r=v.post('/api/v1/chat/conversations/'+c['id']+'/messages',json={'client_message_id':str(uuid4()),'body':'Hello','follow_up_email':email})
    assert r.status_code==400
    assert v.get('/api/v1/chat/conversations/'+c['id']+'/messages').json()['items']==[]

def test_contact_policy_fenced_and_normal_messages_always_available(workflow):
    w=workflow;enable(w);v,c,_=visitor(w);path='/api/v1/chat/conversations/'+c['id']
    body={'client_message_id':str(uuid4()),'body':'Help','follow_up_email':'person@example.test'}
    assert v.post(path+'/messages',json=body).status_code==409
    assert send(v,c).status_code==201
    assert save(w).status_code==200
    assert v.post(path+'/messages',json=body).status_code==201
    assert save(w,version=1,data=schedule()|{'collect_email':False}).status_code==200
    assert v.post(path+'/messages',json=body|{'client_message_id':str(uuid4())}).status_code==409
    assert v.post(path+'/contact/remove',json={'version':1}).status_code==200
    assert send(v,c,'I can still leave a message').status_code==201
