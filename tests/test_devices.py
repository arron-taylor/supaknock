from uuid import uuid4
from fastapi.testclient import TestClient
from conftest import PASSWORD
from test_native import native
from feedback.security import Vault
from feedback.maintenance import backup
from feedback.storage.sqlite import SQLiteStorage


def payload(token='a'*64):
    return dict(token=token,environment='sandbox',name='Test iPhone',enabled=True,request_notifications=True,chat_notifications=True)


def test_device_ownership_idempotence_and_secret_protection(workflow):
    w=workflow; c, login = native(w['app']); did=str(uuid4()); path='/api/v1/admin/devices/'+did
    first=c.put(path,json=payload())
    assert first.status_code==200,first.text
    assert 'token' not in first.json()
    assert c.put(path,json=payload()).json()==first.json()
    assert w['user'].put(path,json=payload()).status_code==403
    assert w['manager'].put(path,json=payload()).status_code==403
    assert w['anon'].put(path,json=payload()).status_code==401
    assert c.put(path,json=payload()|{'user_id':'fake'}).status_code==400
    assert c.get('/api/v1/admin/devices').json()['items'][0]['id']==did
    with w['app'].state.storage.transaction() as db:
        protected=db.execute('SELECT token FROM devices WHERE id=?',(did,)).fetchone()[0]
        assert protected!='a'*64
        assert Vault(w['cfg'].secret).reveal(protected)=='a'*64
    assert c.post('/api/v1/auth/logout').status_code==204
    assert not w['admin'].get('/api/v1/admin/devices').json()['items'][0]['enabled']


def test_device_account_switch_and_restore(workflow,tmp_path):
    w=workflow; a,_=native(w['app']); did=str(uuid4()); path='/api/v1/admin/devices/'+did
    a.put(path,json=payload())
    w['admin'].post('/api/v1/admin/accounts',json={'email':'two@example.com','password':PASSWORD})
    b=TestClient(w['app']); login=b.post('/api/v1/auth/native/login',json={'email':'two@example.com','password':PASSWORD}).json()
    b.headers['Authorization']='Bearer '+login['token']
    assert b.put(path,json=payload('b'*64)).status_code==404
    assert b.delete(path).status_code==204
    assert len(a.get('/api/v1/admin/devices').json()['items'])==1
    assert b.put(path,json=payload()).status_code==200
    assert not a.get('/api/v1/admin/devices').json()['items']
    assert len(b.get('/api/v1/admin/devices').json()['items'])==1
    backup(w['cfg'],tmp_path/'backup')
    restored=SQLiteStorage(tmp_path/'backup'/'feedback.sqlite',w['cfg'].base_url)
    assert restored.devices(login['user']['id'])[0]['id']==did
    assert b.post('/api/v1/auth/password',json={'current_password':PASSWORD,'new_password':'changed-long-password'}).status_code==204
    assert not w['app'].state.storage.devices(login['user']['id'])[0]['enabled']
