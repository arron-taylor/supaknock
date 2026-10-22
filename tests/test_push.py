from dataclasses import replace
from uuid import uuid4
import time
import pytest
from feedback.api import create_app
from feedback.push import PushService, RelayFailure
from feedback.security import Vault
from test_native import native
from conftest import submission


def configure(w,monkeypatch):
    app=w['app']; push=app.state.push
    monkeypatch.setattr('feedback.push.relay_origin',lambda url,allow_internal: url.rstrip('/'))
    calls=[]
    def enroll(url,path,credential,body,internal):
        calls.append(body)
        return {'installation_id':body['installation_id'],'credential':'sr_'+'A'*43}
    push.transport=enroll
    r=w['admin'].put('/api/v1/admin/push',json={'enabled':True,'relay_url':'https://relay.example'})
    assert r.status_code==200,r.text
    return calls


def device(w):
    client,session=native(w['app'])
    did=str(uuid4())
    r=client.put('/api/v1/admin/devices/'+did,json={'token':'ab'*32,'environment':'sandbox'})
    assert r.status_code==200,r.text
    return client,did


def test_enrollment_permissions_recovery_and_opt_out(workflow,monkeypatch):
    w=workflow; push=w['app'].state.push
    for caller in ('anon','user','trusted','manager'):
        assert w[caller].get('/api/v1/admin/push').status_code in (401,403)
        assert w[caller].put('/api/v1/admin/push',json={'enabled':True}).status_code in (401,403)
    calls=configure(w,monkeypatch)
    assert len(calls)==1
    assert w['admin'].put('/api/v1/admin/push',json={'enabled':True,'relay_url':'https://relay.example'}).status_code==200
    assert len(calls)==1
    row=w['app'].state.storage.push_settings()
    assert 'sr_' not in row['credential'] and calls[0]['enrollment_proof'] not in row['enrollment_proof']
    assert 'credential' not in w['admin'].get('/api/v1/admin/push').text
    assert w['admin'].put('/api/v1/admin/push',json={'enabled':True,'relay_url':'https://different.example'}).status_code==409
    def failed(*args): raise RelayFailure('relay_connection_failed',True)
    push.transport=failed
    assert w['admin'].post('/api/v1/admin/push/rotate').status_code==503
    assert w['admin'].put('/api/v1/admin/push',json={'enabled':False}).json()['enabled'] is False
    restored=create_app(replace(w['cfg'],database_path=w['cfg'].database_path))
    assert restored.state.push.settings()['installation_id']==push.settings()['installation_id']
    assert restored.state.storage.push_settings()['enrollment_proof']==row['enrollment_proof']


def test_atomic_jobs_retry_identity_and_restart(workflow,monkeypatch):
    from feedback.push import PushWorker
    w=workflow; configure(w,monkeypatch); _,did=device(w)
    s=w['app'].state.storage
    original=s._queue_push
    def broken(c,*args):
        original(c,*args)
        raise RuntimeError('force rollback')
    monkeypatch.setattr(s,'_queue_push',broken)
    with pytest.raises(RuntimeError):
        submission(w['user'],w['b'])
    assert s.push_deliveries(100,0)==[]
    assert w['admin'].get('/api/v1/moderation').json()['items']==[]
    monkeypatch.setattr(s,'_queue_push',original)
    r=submission(w['user'],w['b'])
    jobs=s.push_deliveries(100,0); assert len(jobs)==1
    eid=jobs[0]['event_id']; seen=[]; rid=str(uuid4())
    def transport(origin,path,credential,body,internal):
        seen.append((path,body))
        assert credential=='sr_'+'A'*43
        if len(seen)==1:
            raise RelayFailure('relay_connection_failed',True)
        if path=='/notifications':
            return {'id':rid,'state':'queued'}
        return {'id':rid,'state':'accepted'}
    worker=PushWorker(s,Vault(w['cfg'].secret),transport=transport)
    start=time.time(); assert worker.once(start)
    restored=create_app(w['cfg']); worker.storage=restored.state.storage
    assert worker.once(start+100)
    assert seen[0][1]==seen[1][1]
    assert seen[0][1]['event_id']==eid and seen[0][1]['recipient_id']==did
    assert worker.once(start+200)
    assert s.push_deliveries(1,0)[0]['state']=='succeeded'
    view=w['admin'].get('/api/v1/admin/push/deliveries').json()
    assert 'ab'*32 not in str(view) and 'sr_' not in str(view)
    assert s.push_deliveries(1,0)[0]['token']==''


def test_lost_acceptance_then_logout_cancels_by_key(workflow,monkeypatch):
    from feedback.push import PushWorker
    w=workflow;configure(w,monkeypatch);client,did=device(w);submission(w['user'],w['b'])
    s=w['app'].state.storage;seen=[]
    def transport(origin,path,credential,body,internal):
        seen.append((path,body))
        if path=='/notifications': raise RelayFailure('relay_connection_failed',True)
        return {'id':str(uuid4()),'state':'cancelled'}
    worker=PushWorker(s,Vault(w['cfg'].secret),transport=transport)
    worker.once(); assert client.post('/api/v1/auth/logout').status_code==204
    worker.once(time.time()+100)
    assert [x[0] for x in seen]==['/notifications','/notifications/cancel-by-key']
    assert seen[1][1]=={k:seen[0][1][k] for k in ('event_id','recipient_id')}
    assert s.push_deliveries(1,0)[0]['state']=='cancelled'


def test_refresh_suppression_lease_fencing_and_manual_retry(workflow,monkeypatch):
    from feedback.push import PushWorker
    w=workflow;configure(w,monkeypatch);client,did=device(w);submission(w['user'],w['b']);s=w['app'].state.storage
    job=s.claim_push(time.time()); assert job
    # Reclaim an abandoned worker and reject its late completion.
    nextjob=s.claim_push(time.time()+61); assert nextjob['lease_token']!=job['lease_token']
    assert not s.finish_push(job['id'],job['lease_token'],'succeeded',str(uuid4()),None,0)
    assert s.finish_push(nextjob['id'],nextjob['lease_token'],'failed',None,'relay_delivery_failed',0)
    assert w['admin'].post('/api/v1/admin/push/deliveries/'+job['id']+'/retry').status_code==202
    assert client.put('/api/v1/admin/devices/'+did,json={'token':'bc'*32,'environment':'sandbox'}).status_code==200
    def nosend(*args): raise AssertionError('Old event must never target a refreshed registration')
    worker=PushWorker(s,Vault(w['cfg'].secret),transport=nosend);worker.once()
    assert s.push_deliveries(1,0)[0]['state']=='cancelled'


def test_optout_inflight_and_invalid_token_generation(workflow,monkeypatch):
    from feedback.push import PushWorker
    w=workflow;configure(w,monkeypatch);client,did=device(w);submission(w['user'],w['b']);s=w['app'].state.storage
    rid=str(uuid4());calls=[]
    def transport(origin,path,credential,body,internal):
        calls.append(path)
        if path=='/notifications':
            assert w['admin'].put('/api/v1/admin/push',json={'enabled':False}).status_code==200
            return {'id':rid,'state':'queued'}
        return {'id':str(uuid4()),'state':'cancelled'}
    worker=PushWorker(s,Vault(w['cfg'].secret),transport=transport)
    worker.once(); worker.once()
    assert calls==['/notifications','/notifications/cancel-by-key']
    assert s.push_deliveries(1,0)[0]['state']=='cancelled'
    configure(w,monkeypatch);submission(w['user'],w['b'])
    def invalid(*args):
        client.put('/api/v1/admin/devices/'+did,json={'token':'bc'*32,'environment':'sandbox'})
        return {'id':str(uuid4()),'state':'failed','invalid_token':True}
    worker.transport=invalid;worker.once()
    assert client.get('/api/v1/admin/devices').json()['items'][0]['enabled'] is True


def test_push_backup_restore_and_url_defenses(workflow,monkeypatch,tmp_path):
    from feedback.push import relay_origin
    from feedback.errors import Error
    w=workflow;configure(w,monkeypatch);device(w);submission(w['user'],w['b'])
    path=tmp_path/'restored.sqlite';w['app'].state.storage.backup(path)
    restored=create_app(replace(w['cfg'],database_path=path))
    assert restored.state.push.settings()==w['app'].state.push.settings()
    assert restored.state.storage.push_deliveries(10,0)==w['app'].state.storage.push_deliveries(10,0)
    monkeypatch.undo()
    for url in ('http://relay.example','https://127.0.0.1','https://169.254.169.254','https://user:password@example.com','https://example.com/path','https://example.com/?token=x'):
        with pytest.raises(Error): relay_origin(url)
    with pytest.raises(Error): relay_origin('http://169.254.169.254',True)
