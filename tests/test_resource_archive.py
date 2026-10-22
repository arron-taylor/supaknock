"""Real SQLite archive/restore behavior and in-flight operation fences."""
import copy
import time
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4
import pytest
from fastapi.testclient import TestClient
from conftest import PASSWORD, submission
from feedback.api import create_app
from feedback.errors import Error
from feedback.security import digest
from feedback.maintenance import backup
from dataclasses import replace
from test_chat import enable, visitor, send, ORIGIN
from test_connected_api import connected
from test_ai_job_races import ready_ai_jobs, enqueue


def archive(w, kind, resource, value=True, version=None):
    return w['admin'].put('/api/v1/'+kind+'/'+resource['id']+'/archive',json={'archived':value,'version':version if version is not None else resource['version']})


def principal(w):
    session=w['app'].state.storage.session(digest(w['admin'].cookies['feedback_session']))
    return {'kind':'admin','user_id':session['id'],'session_id':session['session_id']}


@pytest.mark.parametrize('kind,key',[('projects','p'),('boards','b')])
def test_authorization_version_noop_and_restore(workflow,kind,key):
    w=workflow;r=w[key];path='/api/v1/'+kind+'/'+r['id']+'/archive'
    for role in ['anon','user','trusted','manager','other']:
        assert w[role].put(path,json={'archived':True,'version':r['version']}).status_code in (401,403)
    assert w['admin'].put(path,json={'archived':'true','version':r['version']}).status_code==400
    result=archive(w,kind,r);assert result.status_code==200,result.text
    saved=result.json();assert saved['archived_at'] and saved['version']==r['version']+1
    assert archive(w,kind,r).status_code==409
    assert archive(w,kind,saved).json()==saved
    restored=archive(w,kind,saved,False).json()
    assert restored['archived_at'] is None and restored['version']==saved['version']+1
    assert restored['id']==r['id'] and restored['identifier']==r['identifier']


def test_project_restore_preserves_individual_board_archives_and_pagination(workflow):
    w=workflow;a=w['admin'];p=w['p'];b=w['b'];base='/api/v1/projects/'+p['id']
    other=a.post(base+'/boards',json={'identifier':'bugs','name':'Bugs'}).json()
    child=archive(w,'boards',b).json();parent=archive(w,'projects',p).json()
    assert {x['id'] for x in a.get('/api/v1/projects?limit=1').json()['items']}=={w['q']['id']}
    assert a.get('/api/v1/projects?archive=archived').json()['items'][0]['id']==p['id']
    assert archive(w,'boards',child,False).status_code==409
    archive(w,'projects',parent,False)
    assert [x['id'] for x in a.get(base+'/boards').json()['items']]==[other['id']]
    assert a.get(base+'/boards?archive=archived').json()['items'][0]['id']==b['id']
    assert archive(w,'boards',child,False).status_code==200
    assert len(a.get(base+'/boards').json()['items'])==2
    for role in ['anon','user','trusted','manager']:
        assert w[role].get('/api/v1/projects?archive=all').status_code in (401,403)
        assert w[role].get(base+'/boards?archive=all').status_code in (401,403)


@pytest.mark.parametrize('kind,key',[('projects','p'),('boards','b')])
def test_history_preserved_hidden_and_writes_stop(workflow,kind,key):
    w=workflow;a=w['admin'];r=submission(w['user'],w['b']);pending=submission(w['user'],w['b'])
    r=a.post('/api/v1/requests/'+r['id']+'/accept',json={'version':r['version']}).json()
    path='/api/v1/requests/'+r['id'];bpath='/api/v1/boards/'+w['b']['id']
    assert w['user'].put(path+'/vote',json={'present':True}).status_code==200
    history=a.get(path+'/history').json()
    archived=archive(w,kind,w[key]).json()
    for role in ['anon','user','trusted','manager']:
        assert w[role].get(bpath).status_code==404
        assert w[role].get(path).status_code==404
        assert w[role].get(path+'/history').status_code==404
    assert a.get(path).json()['archived'] is True
    assert a.get(path+'/history').json()==history
    rows=a.get(bpath+'/requests?moderation=all').json()['items']
    assert {x['id'] for x in rows}=={r['id'],pending['id']}
    assert not a.get('/api/v1/moderation').json()['items']
    assert not w['user'].get('/api/v1/me/requests').json()['items']
    assert a.post(bpath+'/requests',json={'title':'blocked'}).status_code==409
    assert a.put(path+'/vote',json={'present':True}).status_code==409
    assert a.patch(path+'/content',json={'title':'blocked','description':'','version':r['version']}).status_code==409
    assert a.post('/api/v1/requests/'+pending['id']+'/accept',json={'version':pending['version']}).status_code==409
    assert w['user'].patch('/api/v1/requests/'+pending['id'],json={'title':'blocked','version':pending['version']}).status_code==409
    assert a.get(path).json()['vote_count']==1
    archive(w,kind,archived,False)
    assert w['anon'].get(path).status_code==200
    assert a.get(path).json()['accepted_at']==r['accepted_at']
    assert a.get(path+'/history').json()==history


def test_archive_atomic_session_recheck_and_competing_versions(workflow):
    w=workflow;s=w['app'].state.storage;p=principal(w)
    def attempt(_):
        try:return s.resource_archive('project',w['p']['id'],p,True,w['p']['version'])['version']
        except Error as e:return e.status
    with ThreadPoolExecutor(max_workers=2) as pool:result=list(pool.map(attempt,range(2)))
    assert sorted(result)==[2,409]
    s.revoke_session(p['session_id'])
    with pytest.raises(Error) as exc:s.resource_archive('project',w['p']['id'],p,False,2)
    assert exc.value.status in (401,403)


def test_project_chat_pause_history_and_old_sessions_stay_revoked(workflow):
    w=workflow;enable(w);v,cv,_=visitor(w);assert send(v,cv).status_code==201
    a=w['admin'];base='/api/v1/chat/conversations/'+cv['id']
    history=a.get(base+'/messages').json()
    paused=archive(w,'projects',w['p']).json()
    assert send(v,cv).status_code in (401,403,404,409)
    assert w['anon'].post('/api/v1/chat/projects/glean/sessions',headers={'Origin':ORIGIN}).status_code in (404,409)
    assert a.get(base).json()['project_archived_at']
    assert a.get(base+'/messages').json()==history
    assert send(a,cv).status_code==409
    restored=archive(w,'projects',paused,False);assert restored.status_code==200
    assert send(v,cv).status_code in (401,403,404)
    new,c2,_=visitor(w);assert send(new,c2).status_code==201


def test_connected_inflight_read_fenced_even_after_restore(connected):
    w,root,s=connected;original=s.transport
    async def in_flight(url,token,op,query,deadline):
        data=await original(url,token,op,query,deadline)
        if op=='users':
            p=s.storage.project(w['p']['id']);arch=s.storage.resource_archive('project',p['id'],principal(w),True,p['version'])
            s.storage.resource_archive('project',p['id'],principal(w),False,arch['version'])
        return data
    s.transport=in_flight
    assert w['admin'].post(root+'/users/query',json={'version':2,'query':{}}).status_code==409
    assert w['admin'].get(root+'/connection').json()['enabled'] is True


@pytest.mark.parametrize('kind',['preview','ingest'])
def test_archive_cancels_claimed_ai_work_and_preserves_sources_and_account_keys(ready_ai_jobs,kind):
    env=ready_ai_jobs;s=env['store'];pid=env['project'];pr=env['principal']
    key_ids=[k['id'] for k in s.ai_provider_keys(pid,pr)['keys']]
    source=copy.deepcopy(s.ai_sources(pid,pr))
    job=enqueue(env,kind=kind);leased=s.ai_claim_job(time.time());assert leased['id']==job['id']
    p=s.resource_archive('project',pid,pr,True,s.project(pid)['version'])
    assert s.ai_claim_job(time.time()+100) is None
    s.resource_archive('project',pid,pr,False,p['version'])
    assert s.ai_claim_job(time.time()+200) is None
    assert [{k: row[k] for k in ("id","active_revision_id","approved","active")} for row in s.ai_sources(pid,pr)] == [{k: row[k] for k in ("id","active_revision_id","approved","active")} for row in source]
    assert [k['id'] for k in s.ai_provider_keys(pid,pr)['keys']]==key_ids
    assert s.ai_jobs(pid,pr)[0]['state']=='cancelled'


def test_archive_survives_restart_and_fixed_backup_restore(workflow,tmp_path):
    w=workflow;archived=archive(w,'projects',w['p']).json();bundle=tmp_path/'backup'
    backup(w['cfg'],bundle)
    cfg=replace(w['cfg'],database_path=bundle/'feedback.sqlite',secret=(bundle/'application.key').read_text())
    restarted=create_app(cfg);client=TestClient(restarted);client.cookies.update(w['admin'].cookies);client.headers.update(w['admin'].headers)
    assert client.get('/api/v1/projects/'+archived['id']).json()==archived
    assert [p['id'] for p in client.get('/api/v1/projects?archive=archived').json()['items']]==[archived['id']]
    assert client.put('/api/v1/projects/'+archived['id']+'/archive',json={'version':archived['version'],'archived':False}).status_code==200


def test_board_archive_does_not_cancel_unrelated_ai_work(ready_ai_jobs):
    env=ready_ai_jobs;s=env['store'];pid=env['project']
    board=s.create_board(pid,{'identifier':'unrelated','name':'Unrelated','description':''})
    job=enqueue(env,kind='preview')
    s.resource_archive('board',board['id'],env['principal'],True,board['version'])
    assert s.ai_claim_job(time.time())['id']==job['id']
