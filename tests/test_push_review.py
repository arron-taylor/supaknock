"""Regression cases found in the independent relay/integration review."""
import time
from uuid import uuid4
from feedback.push import PushWorker,RelayFailure
from feedback.security import Vault
from conftest import submission
from test_push import configure,device


def test_deleted_registration_cannot_reanimate_an_old_queued_notification(workflow,monkeypatch):
    w=workflow;configure(w,monkeypatch);client,did=device(w)
    submission(w['user'],w['b'])
    storage=w['app'].state.storage
    old_generation=storage.push_deliveries(1,0)[0]['generation']
    assert client.delete('/api/v1/admin/devices/'+did).status_code==204
    renewed=client.put('/api/v1/admin/devices/'+did,json={'token':'bc'*32,'environment':'sandbox'})
    assert renewed.status_code==200
    calls=[]
    def transport(origin,path,credential,body,internal):
        calls.append(path)
        return {'id':str(uuid4()),'state':'accepted'}
    worker=PushWorker(storage,Vault(w['cfg'].secret),transport=transport)
    assert worker.once()
    assert '/notifications' not in calls,'An old device generation was reused after removal/re-registration'
    assert storage.push_deliveries(1,0)[0]['state']=='cancelled'
    assert renewed.json()['generation']!=old_generation


def test_logout_cancels_uncertain_remote_work_after_local_retry_exhaustion(workflow,monkeypatch):
    w=workflow;configure(w,monkeypatch);client,did=device(w)
    submission(w['user'],w['b']);storage=w['app'].state.storage;calls=[]
    def transport(origin,path,credential,body,internal):
        calls.append(path)
        if path=='/notifications':
            # The relay may have persisted the request before this response was lost.
            raise RelayFailure('relay_connection_failed',True)
        return {'id':str(uuid4()),'state':'cancelled'}
    worker=PushWorker(storage,Vault(w['cfg'].secret),transport=transport)
    started=time.time()
    for attempt in range(8):
        assert worker.once(started+attempt*4000)
    assert storage.push_deliveries(1,0)[0]['state']=='failed'
    assert client.post('/api/v1/auth/logout').status_code==204
    assert worker.once(started+32000),'Failed uncertain jobs must still be eligible for revocation cancellation'
    assert calls[-1]=='/notifications/cancel-by-key'
    assert storage.push_deliveries(1,0)[0]['state']=='cancelled'


def test_disable_then_reenable_does_not_revive_queued_historical_notifications(workflow,monkeypatch):
    w=workflow;configure(w,monkeypatch);device(w);submission(w['user'],w['b'])
    assert w['admin'].put('/api/v1/admin/push',json={'enabled':False}).status_code==200
    assert w['admin'].put('/api/v1/admin/push',json={'enabled':True,'relay_url':'https://relay.example'}).status_code==200
    calls=[]
    def transport(origin,path,credential,body,internal):
        calls.append(path);return {'id':str(uuid4()),'state':'accepted'}
    storage=w['app'].state.storage; worker=PushWorker(storage,Vault(w['cfg'].secret),transport=transport)
    assert worker.once()
    assert '/notifications' not in calls
    assert storage.push_deliveries(1,0)[0]['state']=='cancelled'


def test_failed_cancellation_can_retry_without_resubmitting_after_reenable(workflow,monkeypatch):
    w=workflow;configure(w,monkeypatch);device(w);submission(w['user'],w['b'])
    calls=[];receipt=str(uuid4())
    def transport(origin,path,credential,body,internal):
        calls.append(path)
        if path=='/notifications': return {'id':receipt,'state':'queued'}
        if calls.count('/notifications/cancel-by-key')==1: raise RelayFailure('relay_connection_failed',True)
        return {'id':receipt,'state':'cancelled'}
    storage=w['app'].state.storage; worker=PushWorker(storage,Vault(w['cfg'].secret),transport=transport)
    started=time.time();worker.once(started)
    w['admin'].put('/api/v1/admin/push',json={'enabled':False})
    worker.once(started+100)
    assert storage.push_deliveries(1,0)[0]['state']=='queued'
    w['admin'].put('/api/v1/admin/push',json={'enabled':True,'relay_url':'https://relay.example'})
    worker.once(started+500)
    assert calls==['/notifications','/notifications/cancel-by-key','/notifications/cancel-by-key']
    assert storage.push_deliveries(1,0)[0]['state']=='cancelled'


def test_cancellation_retries_are_bounded_and_manually_recoverable(workflow,monkeypatch):
    w=workflow;configure(w,monkeypatch);client,_=device(w);submission(w['user'],w['b'])
    calls=[];receipt=str(uuid4());recover=False
    def transport(origin,path,credential,body,internal):
        calls.append(path)
        if path=='/notifications': return {'id':receipt,'state':'queued'}
        if not recover: raise RelayFailure('relay_connection_failed',True)
        return {'id':receipt,'state':'cancelled'}
    storage=w['app'].state.storage; worker=PushWorker(storage,Vault(w['cfg'].secret),transport=transport)
    started=time.time();worker.once(started);client.post('/api/v1/auth/logout')
    for attempt in range(1,9): assert worker.once(started+attempt*4000)
    job=storage.push_deliveries(1,0)[0]
    assert job['state']=='failed' and not worker.once(started+36000)
    assert w['admin'].post('/api/v1/admin/push/deliveries/'+job['id']+'/retry').status_code==202
    recover=True
    assert worker.once(started+40000)
    assert calls.count('/notifications')==1
    assert storage.push_deliveries(1,0)[0]['state']=='cancelled'


import pytest

@pytest.mark.parametrize('terminal',['failed','expired'])
def test_terminal_remote_failure_is_a_completed_cancellation(workflow,monkeypatch,terminal):
    w=workflow;configure(w,monkeypatch);client,_=device(w);submission(w['user'],w['b'])
    receipt=str(uuid4())
    def transport(origin,path,credential,body,internal):
        return {'id':receipt,'state':'queued' if path=='/notifications' else terminal}
    storage=w['app'].state.storage; worker=PushWorker(storage,Vault(w['cfg'].secret),transport=transport)
    started=time.time();worker.once(started);client.post('/api/v1/auth/logout');worker.once(started+100)
    assert storage.push_deliveries(1,0)[0]['state']=='cancelled'
