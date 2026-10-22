"""Real relational tests for independent, origin-bound browser form intake."""
import json
from uuid import uuid4
import pytest
from feedback.errors import Error
from test_forms import DEFINITION

ORIGIN='https://example.com'
OTHER='https://other.example'

def create(w, **changes):
    r=w['admin'].post('/api/v1/admin/projects/'+w['p']['id']+'/forms',json={**DEFINITION,'embed_enabled':True,'embed_origins':[ORIGIN,OTHER],**changes})
    assert r.status_code==201,r.text
    return r.json()

def url(f):return '/api/v1/embed/forms/'+f['id']

def body(w,f,origin=ORIGIN):
    r=w['anon'].get(url(f),headers={'Origin':origin});assert r.status_code==200,r.text
    return {'client_request_id':str(uuid4()),'answers':{'email':'private@example.com','reason':'Private answer'},'ticket':r.json()['ticket']}

def submit(w,f,data,origin=ORIGIN):
    return w['anon'].post(url(f)+'/submissions',headers={'Origin':origin,'Sec-Fetch-Site':'cross-site'},json=data)

def edit(w,f,**changes):
    return w['admin'].put('/api/v1/admin/forms/'+f['id'],json={k:v for k,v in {**f,**changes}.items() if k in ('name','description','enabled','public_enabled','embed_enabled','embed_origins','fields','version')})

def test_embed_independent_of_public_page_and_other_forms(workflow):
    w=workflow;f=create(w);closed=create(w,embed_enabled=False)
    r=w['anon'].get(url(f),headers={'Origin':ORIGIN})
    assert set(r.json())=={'id','name','description','fields','project_name','ticket'}
    assert r.headers['access-control-allow-origin']==ORIGIN and r.headers['cache-control']=='no-store'
    assert w['anon'].get('/forms/'+f['id']).status_code==404
    for origin in ('','null','https://example.com.evil.test','https://example.com/','http://example.com'):
        r=w['anon'].get(url(f),headers={'Origin':origin});assert r.status_code==403 and 'access-control-allow-origin' not in r.headers
    assert w['anon'].get(url(closed),headers={'Origin':ORIGIN}).status_code==404
    assert w['trusted'].get(url(f),headers={'Origin':ORIGIN}).status_code==403
    assert w['admin'].get(url(f)).status_code==403
    assert w['anon'].get('/api/v1/embed/forms',headers={'Origin':ORIGIN}).status_code==404
    assert w['anon'].get('/form-widget.js').status_code==200
    assert w['anon'].get('/form-widget.css').status_code==200
    assert "frame-ancestors 'none'" in w['anon'].get('/login').headers['content-security-policy']


def test_cors_is_exact_route_method_origin_and_never_credentials(workflow):
    w=workflow;f=create(w)
    headers={'Origin':ORIGIN,'Access-Control-Request-Method':'POST','Access-Control-Request-Headers':'content-type'}
    r=w['anon'].options(url(f)+'/submissions',headers=headers)
    assert r.status_code==204 and r.headers['access-control-allow-origin']==ORIGIN
    assert 'access-control-allow-credentials' not in r.headers
    for change in ({'Origin':OTHER+'bad'},{'Access-Control-Request-Method':'PUT'},{'Access-Control-Request-Headers':'authorization'},{'Access-Control-Request-Headers':'x-chat-token'}):
        r=w['anon'].options(url(f)+'/submissions',headers={**headers,**change});assert r.status_code==403
    assert w['anon'].options(url(f),headers=headers).status_code==403
    assert w['anon'].options(url(f),headers={**headers,'Access-Control-Request-Method':'GET'}).status_code==204
    for path in (url(f)+'/submissions/extra','/api/v1/admin/forms/'+f['id'],'/api/v1/forms/'+f['id']+'/submissions'):
        assert w['anon'].options(path,headers=headers).status_code in (400,403)
    r=submit(w,f,{**body(w,f),'answers':{}})
    assert r.status_code==400 and r.headers['access-control-allow-origin']==ORIGIN
    r=w['anon'].post(url(f)+'/submissions',headers={'Origin':ORIGIN},content=b'x'*100000)
    assert r.status_code==413 and r.headers['access-control-allow-origin']==ORIGIN


def test_embed_ticket_cannot_cross_origin_form_or_transport(workflow):
    w=workflow;f=create(w,public_enabled=True);other=create(w);data=body(w,f)
    assert submit(w,f,data,OTHER).status_code==403
    assert submit(w,other,data).status_code==403
    assert submit(w,f,{**data,'ticket':'broken'}).status_code==403
    public=w['anon'].get('/api/v1/public/forms/'+f['id']).json()['ticket']
    assert submit(w,f,{**data,'ticket':public}).status_code==403
    r=w['anon'].post('/api/v1/public/forms/'+f['id']+'/submissions',headers={'Origin':w['cfg'].base_url},json=data)
    assert r.status_code==403
    assert w['anon'].post(url(f)+'/submissions',json=data).status_code==403
    assert submit(w,f,data).status_code==201
    assert submit(w,f,data).json()=={'ok':True}
    entries=w['admin'].get('/api/v1/admin/forms/'+f['id']+'/submissions').json()['items'];assert len(entries)==1
    assert entries[0]['answers']==data['answers']
    notices=w['admin'].get('/api/v1/admin/notifications').json()['items']
    assert len([n for n in notices if n['type']=='form.submission_created'])==1
    assert 'private@example.com' not in json.dumps(notices) and 'Private answer' not in json.dumps(notices)


@pytest.mark.parametrize('change',[{'embed_enabled':False},{'embed_origins':[OTHER]},{'enabled':False},{'name':'Changed questions'}])
def test_current_settings_fence_http_and_atomic_operation(workflow,change):
    w=workflow;f=create(w);data=body(w,f)
    assert edit(w,f,**change).status_code==200
    assert submit(w,f,data).status_code in (403,404,409)
    with pytest.raises(Error):
        w['app'].state.storage.forms_public_submit(f['id'],{'client_request_id':'embed_'+str(uuid4()),'answers':data['answers']},f['version'],'qa-client',ORIGIN)
    assert w['admin'].get('/api/v1/admin/forms/'+f['id']+'/submissions').json()['items']==[]


def test_embed_atomic_rollback_and_shared_spam_budget(workflow,monkeypatch):
    w=workflow;f=create(w,public_enabled=True);data=body(w,f);s=w['app'].state.storage
    original=s._queue_notification
    def broken(*args):original(*args);raise RuntimeError('rollback')
    monkeypatch.setattr(s,'_queue_notification',broken)
    with pytest.raises(RuntimeError):submit(w,f,data)
    with s.transaction() as c:
        assert c.execute('SELECT count(*) FROM form_submissions').fetchone()[0]==0
        assert c.execute('SELECT count(*) FROM form_public_limits').fetchone()[0]==0
    monkeypatch.setattr(s,'_queue_notification',original)
    for _ in range(10):assert submit(w,f,{**data,'client_request_id':str(uuid4())}).status_code==201
    assert submit(w,f,data).status_code==429
    public={**data,'ticket':w['anon'].get('/api/v1/public/forms/'+f['id']).json()['ticket']}
    assert w['anon'].post('/api/v1/public/forms/'+f['id']+'/submissions',headers={'Origin':w['cfg'].base_url},json=public).status_code==429


def test_embed_validation_admin_boundary_honeypot_and_noop(workflow):
    w=workflow;f=create(w)
    for origins in (['*'],['https://example.com/'],['http://example.com'],['https://user:pass@example.com'],['null'],['https://example.com']*21):
        assert edit(w,f,embed_origins=origins).status_code==400
    assert edit(w,f).json()['version']==f['version']
    assert w['trusted'].put('/api/v1/admin/forms/'+f['id'],json={**DEFINITION,'version':1}).status_code==403
    assert submit(w,f,{**body(w,f),'website':'bot'}).json()=={'ok':True}
    assert w['admin'].get('/api/v1/admin/forms/'+f['id']+'/submissions').json()['items']==[]


def test_upgrade_keeps_forms_and_answers_with_embeds_off(tmp_path,monkeypatch,database_url):
    from pathlib import Path
    from dataclasses import replace
    from fastapi.testclient import TestClient
    from cryptography.fernet import Fernet
    from feedback.api import create_app
    from feedback.config import Config
    from feedback.domain import now
    from conftest import PASSWORD
    cfg=Config(tmp_path/'before.sqlite',Fernet.generate_key().decode(),'migration-test-token-long-enough',worker=False)
    if database_url:cfg=replace(cfg,database_provider='postgresql',database_url=database_url)
    original=Path.glob
    def prior(self,pattern):return (f for f in original(self,pattern) if f.name not in ('025_form_embeds.sql','005_form_embeds.sql'))
    fid=str(uuid4());sid=str(uuid4());ts=now()
    with monkeypatch.context() as patch:
        patch.setattr(Path,'glob',prior)
        app=create_app(cfg);admin=TestClient(app)
        login=admin.post('/api/v1/setup',json={'email':'admin@example.com','password':PASSWORD,'setup_token':cfg.setup_token}).json()
        admin.headers.update({'Origin':cfg.base_url,'X-CSRF-Token':login['csrf_token']})
        project=admin.post('/api/v1/projects',json={'identifier':'prior','name':'Prior'}).json()
        with app.state.storage.transaction(True) as c:
            c.execute('INSERT INTO forms(id,project_id,name,fields,enabled,public_enabled,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)',(fid,project['id'],'Existing form',json.dumps(DEFINITION['fields']),1,1,ts,ts))
            c.execute('INSERT INTO form_submissions(id,form_id,client_request_id,answers,answer_hash,created_at,updated_at) VALUES(?,?,?,?,?,?,?)',(sid,fid,str(uuid4()),'{"email":"private@example.com"}','existing-hash',ts,ts))
            before=dict(c.execute('SELECT * FROM form_submissions WHERE id=?',(sid,)).fetchone())
    upgraded=create_app(cfg)
    with upgraded.state.storage.transaction() as c:
        row=dict(c.execute('SELECT * FROM forms WHERE id=?',(fid,)).fetchone())
        assert row['embed_enabled']==0 and row['embed_origins']=='[]' and row['public_enabled']==1
        assert dict(c.execute('SELECT * FROM form_submissions WHERE id=?',(sid,)).fetchone())==before
    assert TestClient(upgraded).get(url({'id':fid}),headers={'Origin':ORIGIN}).status_code==404


def test_expired_ticket_deleted_retry_and_archived_project(workflow):
    import hashlib,hmac,time
    w=workflow;f=create(w);data=body(w,f)
    parts=data['ticket'].split('.');parts[1]=str(int(time.time())-7201)
    parts[3]=hmac.new(w['cfg'].secret.encode(),('public-form:'+f['id']+':embed:'+ORIGIN+':'+'.'.join(parts[:3])).encode(),hashlib.sha256).hexdigest()
    assert submit(w,f,{**data,'ticket':'.'.join(parts)}).status_code==403
    assert submit(w,f,data).status_code==201
    entry=w['admin'].get('/api/v1/admin/forms/'+f['id']+'/submissions').json()['items'][0]
    assert w['admin'].request('DELETE','/api/v1/admin/form-submissions/'+entry['id'],json={'version':1}).status_code==204
    assert submit(w,f,data).status_code==201
    assert w['admin'].get('/api/v1/admin/forms/'+f['id']+'/submissions').json()['items']==[]
    assert w['admin'].put('/api/v1/projects/'+w['p']['id']+'/archive',json={'version':w['p']['version'],'archived':True}).status_code==200
    assert submit(w,f,data).status_code in (403,404)
    with pytest.raises(Error):w['app'].state.storage.forms_public_submit(f['id'],{'client_request_id':'embed_'+str(uuid4()),'answers':data['answers']},1,'test',ORIGIN)
