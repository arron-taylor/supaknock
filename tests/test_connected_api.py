import copy,json,time
from dataclasses import replace
from datetime import datetime,timezone,timedelta
import pytest
from fastapi.testclient import TestClient
from feedback.api import create_app
from feedback.security import digest
from feedback.maintenance import backup
from test_connected_protocol import EXAMPLES,QUERIES
from conftest import storage_fingerprint

@pytest.fixture
def connected(workflow):
    w=workflow;service=w['app'].state.connected_apps
    async def transport(url,token,op,query,deadline):
        assert token=='private-app-sentinel'
        data=copy.deepcopy(EXAMPLES[op])
        if op in ('users','activity'):
            data['expires_at']=(datetime.now(timezone.utc)+timedelta(minutes=20)).strftime('%Y-%m-%dT%H:%M:%SZ')
        if op=='summary':data['interval']=query
        return data
    service.transport=transport
    root='/api/v1/projects/'+w['p']['id']+'/app-data'
    a=w['admin'];r=a.put(root+'/connection',json={'version':0,'base_url':'https://app.example.com','token':'private-app-sentinel'})
    assert r.status_code==200,r.text
    r=a.post(root+'/connection/test',json={'version':1});assert r.status_code==200,r.text
    test=r.json()
    r=a.post(root+'/connection/enable',json={'version':1,'receipt':test['receipt'],'link_origins':['https://app.example.com'],'support_linked':True})
    assert r.status_code==200,r.text
    return w,root,service

@pytest.mark.parametrize('op',['summary','users','profile','activity'])
def test_reads_and_auth(connected,op):
    w,root,s=connected;body={'version':2,'query':QUERIES[op]}
    response=w['admin'].post(root+'/'+op+'/query',json=body)
    assert response.status_code==200,response.text
    assert response.headers['cache-control']=='no-store'
    for client in ('anon','user','trusted','manager'):
        assert w[client].post(root+'/'+op+'/query',json=body).status_code in (401,403)
    assert 'private-app-sentinel' not in response.text

def test_csrf_and_bearer(connected):
    w,root,s=connected
    client=TestClient(w['app']);client.cookies.update(w['admin'].cookies)
    assert client.post(root+'/users/query',json={'version':2,'query':{}}).status_code==403

def test_receipt_cross_scope_and_disabled_late_read(connected):
    w,root,s=connected;a=w['admin']
    assert a.post(root+'/connection/enable',json={'version':2,'receipt':'fake'}).status_code==409
    original=s.transport
    async def disable(url,token,op,query,deadline):
        if op=='users':
            principal=w['app'].state.storage.session(digest(a.cookies['feedback_session']))
            p={'kind':'admin','user_id':principal['id'],'session_id':principal['session_id']}
            s.storage.connected_change(w['p']['id'],p,2,'disable')
        return await original(url,token,op,query,deadline)
    s.transport=disable
    assert a.post(root+'/users/query',json={'version':2,'query':{}}).status_code==409

def test_continuation_filters_process_and_order(connected):
    w,root,s=connected;a=w['admin'];original=s.transport
    expiry=(datetime.now(timezone.utc)+timedelta(minutes=20)).strftime('%Y-%m-%dT%H:%M:%SZ')
    async def pages(url,token,op,query,deadline):
        data=await original(url,token,op,query,deadline)
        if op=='users':
            data['expires_at']=expiry
            data['next_cursor']='private-cursor-sentinel'
            if query.get('cursor'):data['next_cursor']=None # same row: should reject nonprogress
        return data
    s.transport=pages
    response=a.post(root+'/users/query',json={'version':2,'query':{}})
    assert response.status_code==200,response.text
    token=response.json()['continuation'];assert 'private-cursor-sentinel' not in token
    assert a.post(root+'/users/query',json={'version':2,'query':{'query':'changed'},'continuation':token}).status_code==409
    assert a.post(root+'/users/query',json={'version':2,'query':{},'continuation':token}).status_code==502
    s.epoch='restarted'
    assert a.post(root+'/users/query',json={'version':2,'query':{},'continuation':token}).status_code==409

def test_identity_and_backup(connected,tmp_path):
    w,root,s=connected;a=w['admin'];original=s.transport
    async def wrong(url,token,op,query,deadline):
        data=await original(url,token,op,query,deadline);data['app_instance_id']='swapped';return data
    s.transport=wrong
    assert a.post(root+'/users/query',json={'version':2,'query':{}}).status_code==502
    # Built-in backup includes encrypted configuration, never the raw secret.
    out=tmp_path/'backup'
    backup(w['cfg'],out)
    assert all(b'private-app-sentinel' not in f.read_bytes() for f in out.rglob('*') if f.is_file())
    restarted=create_app(w['cfg'])
    r=TestClient(restarted);r.cookies.update(a.cookies)
    assert r.get(root+'/connection').json()['enabled'] is True

def test_exact_existing_support_namespace(connected):
    from conftest import submission
    w,root,s=connected
    report=submission(w['trusted'],w['b'],external=' e\u0301 ')
    a=w['admin']
    for identity,count in [(' e\u0301 ',1),(' é ',0),('e\u0301',0),(' E\u0301 ',0)]:
        r=a.post(root+'/related/query',json={'version':2,'user_id':identity})
        assert r.status_code==200,r.text
        assert len(r.json()['requests'])==count
        if count:assert r.json()['requests'][0]['id']==report['id']

@pytest.mark.parametrize('capability',['summary','users','profiles','activity'])
def test_capability_only_apps(connected,capability):
    w,root,s=connected;original=s.transport
    async def only(url,token,op,query,deadline):
        data=await original(url,token,op,query,deadline)
        if op=='manifest':data['capabilities']={capability:True}
        return data
    s.transport=only
    for op in ('summary','users','profile','activity'):
        response=w['admin'].post(root+'/'+op+'/query',json={'version':2,'query':QUERIES[op]})
        assert response.status_code==(200 if {'profile':'profiles'}.get(op,op)==capability else 409),response.text

def test_remote_content_is_not_written(connected):
    w,root,s=connected
    before=storage_fingerprint(w['cfg'],w['app'].state.storage)
    for op in ('summary','users','profile','activity'):
        assert w['admin'].post(root+'/'+op+'/query',json={'version':2,'query':QUERIES[op]}).status_code==200
    # Read transactions do not change the database image.
    assert storage_fingerprint(w['cfg'],w['app'].state.storage)==before

@pytest.mark.parametrize('op',['users','profile'])
def test_phone_is_private_on_demand_and_not_stored(connected,op):
    w,root,s=connected;original=s.transport
    phone='+1 (312) 555-0142 ext. 9'
    async def with_phone(url,token,operation,query,deadline):
        data=await original(url,token,operation,query,deadline)
        if operation in ('users','profile'):
            user=data['items'][0] if operation=='users' else data['user']
            user['phone_number']=phone
        return data
    s.transport=with_phone
    before=storage_fingerprint(w['cfg'],w['app'].state.storage)
    body={'version':2,'query':QUERIES[op]}
    response=w['admin'].post(root+'/'+op+'/query',json=body)
    assert response.status_code==200,response.text
    data=response.json()['data']
    assert (data['items'][0] if op=='users' else data['user'])['phone_number']==phone
    assert response.headers['cache-control']=='no-store'
    for role in ('anon','user','trusted','manager'):
        denied=w[role].post(root+'/'+op+'/query',json=body)
        assert denied.status_code in (401,403)
        assert phone not in denied.text
    assert storage_fingerprint(w['cfg'],w['app'].state.storage)==before

def test_restore_new_database_and_wrong_key_fail_closed(connected,tmp_path):
    from cryptography.fernet import Fernet
    w,root,s=connected
    bundle=tmp_path/'restore-source';backup(w['cfg'],bundle)
    restored_cfg=replace(w['cfg'],database_path=bundle/'feedback.sqlite',secret=(bundle/'application.key').read_text())
    restored=create_app(restored_cfg);client=TestClient(restored);client.cookies.update(w['admin'].cookies)
    assert client.get(root+'/connection').json()['enabled']
    before=(bundle/'feedback.sqlite').read_bytes()
    with pytest.raises(Exception):create_app(replace(restored_cfg,secret=Fernet.generate_key().decode()))
    assert (bundle/'feedback.sqlite').read_bytes()==before

def test_read_post_budget_is_separate_from_mutations(connected):
    from feedback.api import RequestGuard
    w,root,s=connected
    client=TestClient(RequestGuard(w['app'],replace(w['cfg'],rate_limit=1),w['app'].state.storage))
    client.cookies.update(w['admin'].cookies);client.headers.update(w['admin'].headers)
    for _ in range(3):assert client.post(root+'/users/query',json={'version':2,'query':{}}).status_code==200
    assert client.post(root+'/connection/test',json={'version':2}).status_code==200
    assert client.post(root+'/connection/test',json={'version':2}).status_code==429
    assert client.post(root+'/users/query',json={'version':2,'query':{}}).status_code==200

def test_openapi_describes_admin_only_post_reads(connected):
    w,root,s=connected
    spec=w['app'].openapi()['paths']['/api/v1/projects/{project}/app-data/users/query']['post']
    assert 'Administrator account required (browser session or native bearer token)' in spec['description']
    assert {} not in spec['security']
    assert '504' in spec['responses']
