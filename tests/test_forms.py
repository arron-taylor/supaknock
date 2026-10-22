from uuid import uuid4
import pytest
from test_push import configure, device

DEFINITION={'name':'iPhone early access','description':'Try the app','enabled':True,'fields':[{'key':'email','label':'Email','type':'email','required':True},{'key':'reason','label':'What are you building?','type':'textarea'}]}

def create(w):
    r=w['admin'].post('/api/v1/admin/projects/'+w['p']['id']+'/forms',json=DEFINITION)
    assert r.status_code==201,r.text
    return r.json()

def submit(client,f,body=None):
    return client.post('/api/v1/forms/'+f['id']+'/submissions',json=body or {'client_request_id':str(uuid4()),'answers':{'email':'person@example.com','reason':'A private answer'}})

def test_forms_authorization_and_private_receipts(workflow):
    w=workflow; f=create(w); backend,_=w['key'](w['p'],['forms:submit'])
    for role in ('anon','user','trusted','manager'):
        assert w[role].get('/api/v1/admin/forms').status_code in (401,403)
        assert submit(w[role],f).status_code in (401,403)
    assert backend.get('/api/v1/admin/forms').status_code==403
    wrong,_=w['key'](w['q'],['forms:submit']); assert submit(wrong,f).status_code==404
    for h in ({'Origin':w['cfg'].base_url},{'Sec-Fetch-Site':'same-origin'}):
        backend.headers.update(h); assert submit(backend,f).status_code==403
        for k in h: del backend.headers[k]
    r=submit(backend,f); assert r.status_code==201,r.text
    assert set(r.json())=={'id','created_at'}
    sid=r.json()['id']
    assert backend.get('/api/v1/admin/form-submissions/'+sid).status_code==403
    assert w['admin'].get('/api/v1/admin/form-submissions/'+sid).json()['answers']['email']=='person@example.com'

@pytest.mark.parametrize('answers',[{}, {'email':'invalid'}, {'email':'a@b','extra':'bad'}, {'email':True}, {'email':'a@b','reason':'x'*4001}])
def test_forms_validate_answers(workflow,answers):
    w=workflow; f=create(w); backend,_=w['key'](w['p'],['forms:submit'])
    assert submit(backend,f,{'client_request_id':str(uuid4()),'answers':answers}).status_code in (400,422)
    assert w['admin'].get('/api/v1/admin/forms/'+f['id']+'/submissions').json()['items']==[]

def test_edit_retry_review_delete_and_atomic_notifications(workflow,monkeypatch):
    w=workflow; f=create(w); backend,_=w['key'](w['p'],['forms:submit']); s=w['app'].state.storage
    body={'client_request_id':str(uuid4()),'answers':{'email':'person@example.com','reason':'Private answer'}}
    original=s._queue_notification
    def broken(*args):
        original(*args); raise RuntimeError('Rollback')
    monkeypatch.setattr(s,'_queue_notification',broken)
    with pytest.raises(RuntimeError): submit(backend,f,body)
    monkeypatch.setattr(s,'_queue_notification',original)
    assert w['admin'].get('/api/v1/admin/forms/'+f['id']+'/submissions').json()['items']==[]
    receipt=submit(backend,f,body).json(); sid=receipt['id']
    assert submit(backend,f,body).json()==receipt
    rotated,_=w['key'](w['p'],['forms:submit']); assert submit(rotated,f,body).json()==receipt
    assert submit(backend,f,{**body,'answers':{'email':'different@example.com'}}).status_code==409
    path='/api/v1/admin/forms/'+f['id']; edit={**DEFINITION,'version':1,'enabled':False,'fields':[{'key':'email','label':'New label','type':'text'}]}
    r=w['admin'].put(path,json=edit); assert r.status_code==200,r.text
    assert w['admin'].put(path,json=edit).status_code==409
    assert submit(backend,f).status_code==409
    assert submit(backend,f,body).json()==receipt
    detail='/api/v1/admin/form-submissions/'+sid
    assert w['admin'].get(detail).json()['answers']==body['answers']
    reviewed=w['admin'].put(detail+'/state',json={'version':1,'state':'reviewed'}).json()
    assert reviewed['version']==2
    assert w['admin'].put(detail+'/state',json={'version':1,'state':'archived'}).status_code==409
    assert w['admin'].put(detail+'/state',json={'version':2,'state':'reviewed'}).json()['version']==2
    notifications=w['admin'].get('/api/v1/admin/notifications').json()['items']
    matching=[n for n in notifications if n['type']=='form.submission_created']; assert len(matching)==1
    assert 'person@example.com' not in str(matching) and 'Private answer' not in str(matching)
    assert w['admin'].request('DELETE',detail,json={'version':2}).status_code==204
    assert w['admin'].get(detail).status_code==404
    assert submit(backend,f,body).json()==receipt
    assert w['admin'].get(path+'/submissions').json()['items']==[]
    with s.transaction() as c:
        rows=c.execute("SELECT payload FROM request_events WHERE type LIKE 'form.%'").fetchall()
        assert 'person@example.com' not in str([tuple(r) for r in rows])

def test_form_push_has_separate_preference(workflow,monkeypatch):
    w=workflow; configure(w,monkeypatch); client,did=device(w)
    f=create(w); backend,_=w['key'](w['p'],['forms:submit']); s=w['app'].state.storage
    assert submit(backend,f).status_code==201
    assert s.push_deliveries(100,0)==[]
    body={'token':'ab'*32,'environment':'sandbox','request_notifications':False,'chat_notifications':False,'form_notifications':True}
    r=client.put('/api/v1/admin/devices/'+did,json=body); assert r.status_code==200,r.text
    assert r.json()['form_notifications'] is True
    receipt=submit(backend,f).json()
    jobs=s.push_deliveries(100,0); assert len(jobs)==1 and jobs[0]['kind']=='form.submission_created' and jobs[0]['resource_id']==receipt['id']
    deliveries=w['admin'].get('/api/v1/admin/push/deliveries'); assert deliveries.status_code==200,deliveries.text
    assert deliveries.json()['items'][0]['kind']=='form.submission_created'
    assert client.put('/api/v1/admin/devices/'+did,json={**body,'form_notifications':False}).status_code==200
    job=s.claim_push(__import__('time').time()); assert job and job['cancel']


def test_concurrent_retries_and_revoked_key_recheck(workflow):
    from concurrent.futures import ThreadPoolExecutor
    from feedback.domain import Actor
    from feedback.errors import Error
    w=workflow; f=create(w); backend,key=w['key'](w['p'],['forms:submit'])
    body={'client_request_id':str(uuid4()),'answers':{'email':'same@example.com'}}
    with ThreadPoolExecutor(max_workers=4) as pool:
        receipts=list(pool.map(lambda _:submit(backend,f,body).json(),range(4)))
    assert len({r['id'] for r in receipts})==1
    assert len(w['admin'].get('/api/v1/admin/forms/'+f['id']+'/submissions').json()['items'])==1
    assert w['admin'].delete('/api/v1/projects/'+w['p']['id']+'/keys/'+key['id']).status_code==204
    assert submit(backend,f,body).status_code==401
    actor=Actor(role='integration',project_id=w['p']['id'],key_id=key['id'],permissions=frozenset(['forms:submit']))
    with pytest.raises(Error) as error: w['app'].state.storage.forms_submit(actor,f['id'],body)
    assert error.value.status==401


def test_forms_survive_restart_and_fixed_backup(workflow,tmp_path):
    from dataclasses import replace
    from fastapi.testclient import TestClient
    from feedback.api import create_app
    from feedback.maintenance import backup
    from test_native import native
    w=workflow
    if w['cfg'].database_provider!='sqlite': pytest.skip('SQLite backup contract; PostgreSQL backup has its own container smoke')
    f=create(w); backend,_=w['key'](w['p'],['forms:submit']); sid=submit(backend,f).json()['id']
    admin,_=native(w['app']); expected=admin.get('/api/v1/admin/form-submissions/'+sid).json()
    bundle=tmp_path/'backup'; backup(w['cfg'],bundle)
    for config in (w['cfg'],replace(w['cfg'],database_path=bundle/'feedback.sqlite')):
        with TestClient(create_app(config),headers={'Authorization':admin.headers['Authorization']}) as restored:
            assert restored.get('/api/v1/admin/form-submissions/'+sid).json()==expected
            assert restored.get('/api/v1/admin/forms/'+f['id']).json()==f


def test_upgrade_preserves_existing_alerts_and_push_jobs(tmp_path,monkeypatch,database_url):
    from pathlib import Path
    from dataclasses import replace
    from fastapi.testclient import TestClient
    from cryptography.fernet import Fernet
    from feedback.api import create_app
    from feedback.config import Config
    from conftest import PASSWORD
    cfg=Config(tmp_path/'before.sqlite',Fernet.generate_key().decode(),'migration-test-token-long-enough',worker=False)
    if database_url: cfg=replace(cfg,database_provider='postgresql',database_url=database_url)
    original=Path.glob
    def prior(self,pattern):
        return (f for f in original(self,pattern) if f.name not in ('023_forms.sql','003_forms.sql','024_public_forms.sql','004_public_forms.sql','025_form_embeds.sql','005_form_embeds.sql'))
    with monkeypatch.context() as patch:
        patch.setattr(Path,'glob',prior)
        app=create_app(cfg); admin=TestClient(app)
        login=admin.post('/api/v1/setup',json={'email':'admin@example.com','password':PASSWORD,'setup_token':cfg.setup_token}).json()
        admin.headers.update({'Origin':cfg.base_url,'X-CSRF-Token':login['csrf_token']})
        p=admin.post('/api/v1/projects',json={'identifier':'old','name':'Old project'}).json()
        b=admin.post('/api/v1/projects/'+p['id']+'/boards',json={'identifier':'ideas','name':'Ideas'}).json()
        r=admin.post('/api/v1/boards/'+b['id']+'/requests',json={'title':'Existing request','description':'Keep this history'}); assert r.status_code==201,r.text
        with app.state.storage.transaction(True) as c:
            n=dict(c.execute('SELECT * FROM admin_notifications').fetchone())
            c.execute('UPDATE admin_notifications SET read_at=? WHERE id=?',('2026-10-12T00:00:00Z',n['id']))
            c.execute('INSERT INTO push_deliveries(id,event_id,kind,resource_id,device_id,generation,token,environment,next_attempt_at,created_at,expires_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)',(str(uuid4()),n['event_id'],'request.created',r.json()['id'],str(uuid4()),1,__import__('feedback.security',fromlist=['Vault']).Vault(cfg.secret).protect('ab'*32),'sandbox',1,1,9999999999))
            before=dict(c.execute('SELECT * FROM push_deliveries').fetchone())
    upgraded=create_app(cfg)
    with upgraded.state.storage.transaction() as c:
        assert dict(c.execute('SELECT * FROM push_deliveries').fetchone())==before
        after=dict(c.execute('SELECT * FROM admin_notifications').fetchone())
        assert after=={**n,'read_at':'2026-10-12T00:00:00Z'}
        assert c.execute('SELECT count(*) FROM forms').fetchone()[0]==0


def test_choice_validation_closed_default_and_archived_project(workflow):
    w=workflow; path='/api/v1/admin/projects/'+w['p']['id']+'/forms'
    definition={'name':'Choose a platform','fields':[{'key':'platform','label':'Platform','type':'choice','choices':['iPhone','Mac'],'required':True}]}
    invalid={**definition,'fields':definition['fields']*2}
    assert w['admin'].post(path,json=invalid).status_code==400
    f=w['admin'].post(path,json=definition).json(); assert f['enabled'] is False
    backend,_=w['key'](w['p'],['forms:submit']); body={'client_request_id':str(uuid4()),'answers':{'platform':'iPhone'}}
    assert submit(backend,f,body).status_code==409
    edit={**definition,'enabled':True,'version':1}; url='/api/v1/admin/forms/'+f['id']
    assert w['admin'].put(url,json=edit).json()['version']==2
    assert w['admin'].put(url,json={**edit,'version':2}).json()['version']==2
    assert submit(backend,f,{**body,'answers':{'platform':'Unknown'}}).status_code==400
    receipt=submit(backend,f,body).json()
    assert w['admin'].put('/api/v1/projects/'+w['p']['id']+'/archive',json={'version':w['p']['version'],'archived':True}).status_code==200
    assert submit(backend,f,{**body,'client_request_id':str(uuid4())}).status_code==409
    assert submit(backend,f,body).json()==receipt
