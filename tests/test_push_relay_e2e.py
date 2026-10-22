"""Optional cross-repository integration: real SQLite + disposable PostgreSQL + both APIs.

Run from suggestion-box-relay's test virtualenv with RELAY_E2E_DATABASE_URL set to
its disposable localhost Postgres URL. This suite creates/drops a fresh DB, never
uses Supabase credentials, and only sends to an in-memory fake APNs transport.
"""
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4
import os
import sys
import time
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from feedback.api import create_app
from feedback.push import PushWorker, RelayFailure
from feedback.security import Vault
from conftest import PASSWORD, submission
from test_push import device
from test_chat import enable as enable_chat, visitor, send

DSN=os.getenv('RELAY_E2E_DATABASE_URL')
pytestmark=pytest.mark.skipif(not DSN,reason='Set RELAY_E2E_DATABASE_URL to run cross-repository disposable PostgreSQL tests')


@pytest.fixture
def relay_stack():
    psycopg=pytest.importorskip('psycopg')
    from psycopg.types.json import Jsonb
    from psycopg.conninfo import conninfo_to_dict, make_conninfo
    assert urlparse(DSN).hostname in ('127.0.0.1','localhost','::1'),'Refusing a non-local PostgreSQL target'
    sibling=Path(os.environ.get('RELAY_SOURCE_DIR', str(Path(__file__).resolve().parents[3]/'suggestion-box-relay')))
    assert (sibling/'relay').is_dir(), 'Set RELAY_SOURCE_DIR to the separate relay checkout'
    sys.path.insert(0,str(sibling))
    from relay.app import create_app as relay_app
    from relay.config import Config as RelayConfig
    from relay.apns import Outcome
    from relay.worker import Worker
    name='relay_e2e_'+uuid4().hex[:16]
    with psycopg.connect(DSN,autocommit=True) as conn:
        for role in ('anon','authenticated','service_role'):
            conn.execute(f'DO $$ BEGIN CREATE ROLE {role}; EXCEPTION WHEN duplicate_object THEN NULL; END $$')
        conn.execute(psycopg.sql.SQL('CREATE DATABASE {}').format(psycopg.sql.Identifier(name)))
    params=conninfo_to_dict(DSN);params['dbname']=name;dsn=make_conninfo(**params)
    class Store:
        def call(self,command,payload):
            with psycopg.connect(dsn) as conn:
                return conn.execute('SELECT public.sb_relay_rpc(%s,%s)',(command,Jsonb(payload))).fetchone()[0]
    class APNs:
        sent=[]
        def send(self,job,token):
            self.sent.append((job,token));return Outcome('accepted')
    try:
        with psycopg.connect(dsn,autocommit=True) as conn:
            for migration in sorted((sibling/'migrations').glob('*.sql')):conn.execute(migration.read_text())
        config=RelayConfig(supabase_url='https://test.supabase.co',supabase_key='synthetic',encryption_key=Fernet.generate_key().decode(),operator_secret='o'*40,ip_hash_secret='i'*40,worker_enabled=False)
        app=relay_app(config,Store());apns=APNs()
        with TestClient(app) as client:
            def transport(origin,path,credential,body,internal):
                headers={'Authorization':'Bearer '+credential} if credential else {}
                response=client.request('POST' if body is not None else 'GET','/api/v1'+path,headers=headers,json=body)
                if response.status_code>=400:
                    raise RelayFailure('relay_http_'+str(response.status_code),response.status_code==429 or response.status_code>=500)
                return response.json()
            yield client,Worker(app.state.service,apns),transport,apns
    finally:
        with psycopg.connect(DSN,autocommit=True) as conn:
            conn.execute(psycopg.sql.SQL('DROP DATABASE {}').format(psycopg.sql.Identifier(name)))
        sys.path.remove(str(sibling))


def enroll_installation(w,monkeypatch,relay_stack):
    monkeypatch.setattr('feedback.push.relay_origin',lambda origin,internal:origin.rstrip('/'))
    w['app'].state.push.transport=relay_stack[2]
    response=w['admin'].put('/api/v1/admin/push',json={'enabled':True,'relay_url':'https://relay.test'})
    assert response.status_code==200,response.text
    return PushWorker(w['app'].state.storage,Vault(w['cfg'].secret),transport=relay_stack[2])


def test_request_and_chat_cross_both_durable_queues(workflow,monkeypatch,relay_stack):
    w=workflow;local=enroll_installation(w,monkeypatch,relay_stack);device(w)
    request=submission(w['user'],w['b']);started=time.time()
    assert local.once(started)
    queued=w['app'].state.storage.push_deliveries(1,0)[0]
    assert queued['state']=='awaiting'
    assert relay_stack[1].once()
    # Restart the installation storage/client between acceptance and polling.
    restarted=create_app(w['cfg']);local.storage=restarted.state.storage
    assert local.once(started+100)
    assert local.storage.push_deliveries(1,0)[0]['state']=='succeeded'
    enable_chat(w);v,c,_=visitor(w)
    before=len(local.storage.push_deliveries(100,0))
    assert len(relay_stack[3].sent)==1 # Opening widget/session does not notify.
    assert send(v,c,'A private chat body').status_code==201
    assert len(local.storage.push_deliveries(100,0))==before+1
    assert local.once(started+200);assert relay_stack[1].once();assert local.once(started+300)
    assert {job['type'] for job,token in relay_stack[3].sent}=={'request.created','chat.message'}
    assert {job['resource_id'] for job,token in relay_stack[3].sent}=={request['id'],c['id']}
    assert all(token=='ab'*32 for job,token in relay_stack[3].sent)
    assert all('A private chat body' not in str(job) for job,token in relay_stack[3].sent)
    assert all(job['state']=='succeeded' for job in local.storage.push_deliveries(100,0))


def test_cross_installation_credentials_and_optout_cancellation(workflow,monkeypatch,relay_stack,tmp_path):
    w=workflow;local=enroll_installation(w,monkeypatch,relay_stack);device(w);submission(w['user'],w['b'])
    assert local.once();job=w['app'].state.storage.push_deliveries(1,0)[0]
    other_cfg=replace(w['cfg'],database_path=tmp_path/'other.sqlite',secret=Fernet.generate_key().decode())
    other_app=create_app(other_cfg);other_admin=TestClient(other_app)
    response=other_admin.post('/api/v1/setup',json={'email':'other@example.com','password':PASSWORD,'setup_token':other_cfg.setup_token})
    assert response.status_code==201,response.text
    other_admin.headers.update({'Origin':other_cfg.base_url,'X-CSRF-Token':response.json()['csrf_token']})
    other={'app':other_app,'admin':other_admin,'cfg':other_cfg};enroll_installation(other,monkeypatch,relay_stack)
    credential=Vault(other_cfg.secret).reveal(other_app.state.storage.push_settings()['credential'])
    assert relay_stack[0].get('/api/v1/notifications/'+job['relay_id'],headers={'Authorization':'Bearer '+credential}).status_code==404
    assert other_app.state.storage.installation_id()!=w['app'].state.storage.installation_id()
    assert w['admin'].put('/api/v1/admin/push',json={'enabled':False}).status_code==200
    assert local.once(time.time()+100)
    assert not relay_stack[1].once() and relay_stack[3].sent==[]
    assert w['app'].state.storage.push_deliveries(1,0)[0]['state']=='cancelled'


def test_lost_relay_receipt_and_logout_cancel_real_pending_job(workflow,monkeypatch,relay_stack):
    w=workflow;local=enroll_installation(w,monkeypatch,relay_stack);native,_=device(w);submission(w['user'],w['b'])
    actual=relay_stack[2]
    def lose_response(origin,path,credential,body,internal):
        result=actual(origin,path,credential,body,internal)
        if path=='/notifications': raise RelayFailure('relay_connection_failed',True)
        return result
    local.transport=lose_response
    assert local.once()
    assert w['app'].state.storage.push_deliveries(1,0)[0]['relay_id'] is None
    assert native.post('/api/v1/auth/logout').status_code==204
    assert local.once(time.time()+100)
    assert not relay_stack[1].once() and relay_stack[3].sent==[]
    assert w['app'].state.storage.push_deliveries(1,0)[0]['state']=='cancelled'


def test_form_crosses_both_queues_without_answers(workflow,monkeypatch,relay_stack):
    from test_forms import create, submit
    w=workflow; local=enroll_installation(w,monkeypatch,relay_stack)
    client,did=device(w)
    assert client.put('/api/v1/admin/devices/'+did,json={'token':'ab'*32,'environment':'sandbox','form_notifications':True}).status_code==200
    f=create(w); backend,_=w['key'](w['p'],['forms:submit']); receipt=submit(backend,f).json()
    started=time.time(); assert local.once(started); assert relay_stack[1].once(); assert local.once(started+100)
    jobs=local.storage.push_deliveries(100,0); assert len(jobs)==1 and jobs[0]['state']=='succeeded'
    job,token=relay_stack[3].sent[-1]
    assert job['type']=='form.submission_created' and job['resource_id']==receipt['id']
    assert 'person@example.com' not in str(job) and 'A private answer' not in str(job)
