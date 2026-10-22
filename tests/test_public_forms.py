from uuid import uuid4
import hashlib
import json
import pytest
from fastapi.testclient import TestClient
from test_forms import DEFINITION


def create(w, public=True):
    r=w['admin'].post('/api/v1/admin/projects/'+w['p']['id']+'/forms',json={**DEFINITION,'public_enabled':public})
    assert r.status_code==201,r.text
    return r.json()


def definition(w,f):
    return w['anon'].get('/api/v1/public/forms/'+f['id'])


def body(w,f):
    return {'client_request_id':str(uuid4()),'answers':{'email':'private@example.com','reason':'Private answer'},'ticket':definition(w,f).json()['ticket']}


def submit(w,f,data=None,**headers):
    return w['anon'].post('/api/v1/public/forms/'+f['id']+'/submissions',headers={'Origin':w['cfg'].base_url,**headers},json=data or body(w,f))


def test_public_opt_in_schema_and_private_boundaries(workflow):
    w=workflow;f=create(w,False)
    assert definition(w,f).status_code==404
    assert w['anon'].get('/forms/'+f['id']).status_code==404
    f=w['admin'].put('/api/v1/admin/forms/'+f['id'],json={**DEFINITION,'public_enabled':True,'version':1}).json()
    d=definition(w,f);assert d.status_code==200
    assert set(d.json())=={'id','name','description','project_name','fields','ticket'}
    assert d.headers['cache-control']=='no-store'
    html=w['anon'].get('/forms/'+f['id']);assert html.status_code==200
    assert 'noindex' in html.headers['x-robots-tag'] and 'app.js' not in html.text
    for path in ('/api/v1/public/forms','/forms','/api/v1/admin/forms','/api/v1/admin/forms/'+f['id']+'/submissions'):
        assert w['anon'].get(path).status_code in (401,403,404)
    assert w['trusted'].get('/api/v1/admin/forms/'+f['id']+'/submissions').status_code==403


def test_public_submit_retry_delete_notifications_and_backend_separation(workflow):
    w=workflow;f=create(w);data=body(w,f)
    for _ in range(2):
        r=submit(w,f,data);assert r.status_code==201,r.text;assert r.json()=={'ok':True}
    assert submit(w,f,{**data,'answers':{'email':'changed@example.com'}}).status_code==409
    entries=w['admin'].get('/api/v1/admin/forms/'+f['id']+'/submissions').json()['items'];assert len(entries)==1
    entry=entries[0];assert entry['answers']==data['answers']
    notices=w['admin'].get('/api/v1/admin/notifications').json()['items'];assert len([n for n in notices if n['type']=='form.submission_created'])==1
    assert 'private@example.com' not in json.dumps(notices) and 'Private answer' not in json.dumps(notices)
    assert w['anon'].get('/api/v1/admin/form-submissions/'+entry['id']).status_code==403
    assert w['admin'].request('DELETE','/api/v1/admin/form-submissions/'+entry['id'],json={'version':1}).status_code==204
    assert submit(w,f,data).status_code==201
    assert w['admin'].get('/api/v1/admin/forms/'+f['id']+'/submissions').json()['items']==[]
    backend,_=w['key'](w['p'],['forms:submit'])
    r=backend.post('/api/v1/forms/'+f['id']+'/submissions',json={k:v for k,v in data.items() if k!='ticket'});assert r.status_code==201
    assert 'id' in r.json()


def test_public_origin_ticket_expiry_cross_form_and_honeypot(workflow,monkeypatch):
    w=workflow;f=create(w);other=create(w);data=body(w,f)
    for headers in ({'Origin':''},{'Origin':'https://evil.example'},{'Sec-Fetch-Site':'cross-site'}):
        assert submit(w,f,data,**headers).status_code==403
    assert submit(w,other,data).status_code==403
    assert submit(w,f,{**data,'ticket':data['ticket']+'x'}).status_code==403
    assert submit(w,f,{**data,'ticket':'broken'}).status_code==403
    assert submit(w,f,{**data,'website':'bot'}).status_code==201
    assert w['admin'].get('/api/v1/admin/forms/'+f['id']+'/submissions').json()['items']==[]
    # Sign an old timestamp with the same production ticket algorithm.
    import hmac,time
    parts=data['ticket'].split('.');parts[1]=str(int(time.time())-7201);payload='.'.join(parts[:3]);parts[3]=hmac.new(w['cfg'].secret.encode(),('public-form:'+f['id']+':'+payload).encode(),hashlib.sha256).hexdigest()
    assert submit(w,f,{**data,'ticket':'.'.join(parts)}).status_code==403


def test_public_settings_fence_pending_work(workflow):
    w=workflow;f=create(w);data=body(w,f)
    edit={**DEFINITION,'public_enabled':False,'version':1}
    assert w['admin'].put('/api/v1/admin/forms/'+f['id'],json=edit).status_code==200
    assert submit(w,f,data).status_code==404
    assert definition(w,f).status_code==404
    edit.update(public_enabled=True,version=2)
    assert w['admin'].put('/api/v1/admin/forms/'+f['id'],json=edit).status_code==200
    assert submit(w,f,data).status_code==409
    fresh=body(w,f)
    assert w['admin'].put('/api/v1/projects/'+w['p']['id']+'/archive',json={'version':w['p']['version'],'archived':True}).status_code==200
    assert submit(w,f,fresh).status_code==404
    assert definition(w,f).status_code==404


def test_public_current_validation_and_atomic_rollback(workflow,monkeypatch):
    w=workflow;f=create(w);data=body(w,f)
    for answers in ({},{'email':'bad'},{'email':'a@b','unknown':'no'},{'email':'a@b','reason':'x'*4001}):
        assert submit(w,f,{**data,'answers':answers}).status_code in (400,422)
    storage=w['app'].state.storage;original=storage._queue_notification
    def broken(*args):original(*args);raise RuntimeError('rollback')
    monkeypatch.setattr(storage,'_queue_notification',broken)
    with pytest.raises(RuntimeError):submit(w,f,data)
    with storage.transaction() as c:
        assert c.execute('SELECT count(*) FROM form_submissions').fetchone()[0]==0
        assert c.execute('SELECT count(*) FROM form_public_limits').fetchone()[0]==0
    monkeypatch.setattr(storage,'_queue_notification',original)
    assert submit(w,f,data).status_code==201


def test_public_rate_limits_ignore_forwarded_headers_and_retry_does_not_consume(workflow):
    w=workflow;f=create(w);data=body(w,f)
    assert submit(w,f,data).status_code==201
    for _ in range(12): assert submit(w,f,data).status_code==201
    for i in range(9):assert submit(w,f,{**data,'client_request_id':str(uuid4())},**{'X-Forwarded-For':f'203.0.113.{i}'}).status_code==201
    assert submit(w,f,{**data,'client_request_id':str(uuid4())},**{'X-Forwarded-For':'198.51.100.4'}).status_code==429
    assert submit(w,f,data).status_code==201
    with w['app'].state.storage.transaction() as c:
        assert c.execute('SELECT count(*) FROM form_submissions').fetchone()[0]==10
        rows=c.execute('SELECT bucket FROM form_public_limits').fetchall()
        assert '203.0.113' not in str([tuple(r) for r in rows])


def test_public_concurrent_duplicate_commits_once(workflow):
    from concurrent.futures import ThreadPoolExecutor
    w=workflow;f=create(w);data=body(w,f)
    with ThreadPoolExecutor(max_workers=4) as pool:results=list(pool.map(lambda _:submit(w,f,data),range(4)))
    assert all(r.status_code==201 for r in results)
    assert len(w['admin'].get('/api/v1/admin/forms/'+f['id']+'/submissions').json()['items'])==1


def test_public_form_hourly_cap_and_rollover(workflow,monkeypatch):
    import time
    from feedback.errors import Error
    w=workflow;f=create(w);storage=w['app'].state.storage
    hour=int(time.time()//3600)
    with storage.transaction(True) as c:
        c.execute('INSERT INTO form_public_limits(bucket,window_start,count) VALUES(?,?,?)',('form:'+f['id'],hour,200))
    data={'client_request_id':'public_'+str(uuid4()),'answers':{'email':'qa@example.com'}}
    with pytest.raises(Error) as error:storage.forms_public_submit(f['id'],data,1,'synthetic-client')
    assert error.value.status==429
    # The next hour allows a new submission, independently of the browser/client.
    monkeypatch.setattr('feedback.storage.forms.time.time',lambda:(hour+1)*3600+1)
    assert storage.forms_public_submit(f['id'],data,1,'synthetic-client')=={'ok':True}


def test_existing_forms_migrate_with_public_access_off(tmp_path,monkeypatch,database_url):
    from pathlib import Path
    from dataclasses import replace
    from cryptography.fernet import Fernet
    from feedback.api import create_app
    from feedback.config import Config
    from feedback.domain import now
    from conftest import PASSWORD
    cfg=Config(tmp_path/'before.sqlite',Fernet.generate_key().decode(),'migration-test-token-long-enough',worker=False)
    if database_url:cfg=replace(cfg,database_provider='postgresql',database_url=database_url)
    original=Path.glob
    def prior(self,pattern):return (f for f in original(self,pattern) if f.name not in ('024_public_forms.sql','004_public_forms.sql','025_form_embeds.sql','005_form_embeds.sql'))
    fid=str(uuid4());sid=str(uuid4());ts=now()
    with monkeypatch.context() as patch:
        patch.setattr(Path,'glob',prior)
        app=create_app(cfg);admin=TestClient(app)
        login=admin.post('/api/v1/setup',json={'email':'admin@example.com','password':PASSWORD,'setup_token':cfg.setup_token}).json()
        admin.headers.update({'Origin':cfg.base_url,'X-CSRF-Token':login['csrf_token']})
        project=admin.post('/api/v1/projects',json={'identifier':'prior','name':'Prior project'}).json()
        with app.state.storage.transaction(True) as c:
            c.execute('INSERT INTO forms(id,project_id,name,fields,enabled,created_at,updated_at) VALUES(?,?,?,?,?,?,?)',(fid,project['id'],'Existing form',json.dumps(DEFINITION['fields']),1,ts,ts))
            c.execute('INSERT INTO form_submissions(id,form_id,client_request_id,answers,answer_hash,created_at,updated_at) VALUES(?,?,?,?,?,?,?)',(sid,fid,str(uuid4()),'{"email":"private@example.com"}','existing-hash',ts,ts))
            before=dict(c.execute('SELECT * FROM form_submissions WHERE id=?',(sid,)).fetchone())
    upgraded=create_app(cfg)
    with upgraded.state.storage.transaction() as c:
        row=dict(c.execute('SELECT * FROM forms WHERE id=?',(fid,)).fetchone())
        assert row['public_enabled']==0 and row['enabled']==1 and row['name']=='Existing form'
        assert dict(c.execute('SELECT * FROM form_submissions WHERE id=?',(sid,)).fetchone())==before
    assert TestClient(upgraded).get('/api/v1/public/forms/'+fid).status_code==404
