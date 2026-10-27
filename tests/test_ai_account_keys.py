"""Account credential library: real SQLite, explicit bindings, synthetic providers."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import json
import shutil
import time

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from feedback.ai_domain import default_ai_settings
from feedback.ai_decision_providers import DecisionResult
from feedback.ai_providers import ProviderError, Usage
from feedback.ai_topics import default_topic_policy
from feedback.errors import Error
from feedback.security import Vault
from feedback.storage.sqlite import SQLiteStorage
import feedback.storage.sqlite as adapter
from test_ai_migration import migration_directory
from test_ai_settings_api import ai, select_model, save, ready_knowledge
from test_ai_storage import ai_store, CAPS, ready_runtime, publish_source
from test_ai_key_pool import evidence, claim, dispatch
from test_ai_job_races import ready_ai_jobs, enqueue


def library(ai):
    response=ai['admin'].get('/api/v1/ai/keys')
    assert response.status_code==200,response.text
    return response.json()


def add_account(ai,provider='kodekey',purpose='generation',secret='synthetic-account-secret',**extra):
    response=ai['admin'].post('/api/v1/ai/keys',json={'version':library(ai)['version'],
        'provider':provider,'purpose':purpose,'label':'Shared key','quota_group':'Shared account','key':secret,**extra})
    assert response.status_code==200,response.text
    return response.json()['keys'][-1]


def bind(ai,pid,key_ids,role='generation'):
    base='/api/v1/projects/'+pid+'/ai'+('/topics' if role=='decision' else '')
    response=ai['admin'].put(base+'/keys/selection',json={'version':ai['admin'].get(base).json()['version'],
        'library_version':library(ai)['version'],'key_ids':key_ids})
    assert response.status_code==200,response.text
    return response.json()


def other_project(ai):
    return ai['storage'].create_project({'identifier':'other-ai','name':'Other AI','description':''},ai['principal']['user_id'])['id']


@pytest.mark.parametrize('client',['anon','user','trusted','manager','other'])
def test_account_library_is_native_admin_only(ai,client):
    key=add_account(ai)
    before=library(ai)
    body={'version':before['version'],'key_version':key['version']}
    denied=[ai[client].get('/api/v1/ai/keys'),
        ai[client].post('/api/v1/ai/keys',json=body|{'provider':'kodekey','purpose':'generation','label':'X','key':'untrusted'}),
        ai[client].put('/api/v1/ai/keys/'+key['id'],json=body|{'label':'Changed','quota_group':'Shared account','enabled':False}),
        ai[client].post('/api/v1/ai/keys/'+key['id']+'/models/refresh',json=body|{'role':'generation'}),
        ai[client].request('DELETE','/api/v1/ai/keys/'+key['id'],json=body),
        ai[client].put(ai['url']+'/keys/selection',json={'version':0,'library_version':before['version'],'key_ids':[key['id']]})]
    assert all(r.status_code in (401,403) for r in denied)
    assert library(ai)==before and not ai['fake'].calls


def test_account_keys_require_explicit_binding_and_are_redacted(ai):
    key=add_account(ai)
    current=ai['admin'].get(ai['url']).json()
    assert not current['has_key'] and not current['settings']['enabled']
    assert library(ai)['keys'][0]['projects']==[]
    assert bind(ai,ai['p']['id'],[key['id']])['keys'][0]['id']==key['id']
    safe=library(ai)
    assert safe['keys'][0]['projects']==[{'id':ai['p']['id'],'name':ai['p']['name'],'role':'generation'}]
    assert 'synthetic-account-secret' not in json.dumps(safe) and 'protected_key' not in json.dumps(safe)
    with ai['storage'].transaction() as c:
        raw=c.execute('SELECT project_id,protected_key FROM ai_provider_keys WHERE id=?',(key['id'],)).fetchone()
        assert raw['project_id'] is None and raw['protected_key']!='synthetic-account-secret'
    assert not any(ai['admin'].get(ai['url']).json()['settings']['tools'].values())


def test_two_projects_keep_separate_models_and_shared_verification(ai):
    select_model(ai)
    pid=ai['p']['id'];other=other_project(ai)
    key=library(ai)['keys'][0]
    bind(ai,other,[key['id']])
    urls=[ai['url'],'/api/v1/projects/'+other+'/ai']
    for url,model in zip(urls,['Exact/Model:ID','text-only']):
        cfg=ai['admin'].get(url).json()
        r=ai['admin'].put(url,json={'version':cfg['version'],'settings':cfg['settings']|{'model':model}})
        assert r.status_code==200,r.text
        r=ai['admin'].post(url+'/keys/'+key['id']+'/model/probe',json={'version':r.json()['version'],'key_version':key['version']})
        assert r.status_code==200,r.text
    assert {e['model'] for e in library(ai)['keys'][0]['verified_models']}=={'Exact/Model:ID','text-only'}
    for url in urls:
        assert ai['admin'].get(url+'/keys').json()['keys'][0]['eligible']
    cfg=ai['admin'].get(urls[1]).json()
    ai['admin'].put(urls[1],json={'version':cfg['version'],'settings':cfg['settings']|{'model':'Exact/Model:ID'}}).raise_for_status()
    assert ai['admin'].get(urls[0]+'/keys').json()['keys'][0]['eligible']
    assert ai['admin'].get(urls[1]+'/keys').json()['keys'][0]['eligible']
    assert len(library(ai)['keys'][0]['projects'])==2


@pytest.mark.parametrize('action',['pause','replace','delete'])
def test_global_changes_fence_all_bound_projects_and_clear_replacement(ai,action):
    cfg=select_model(ai)
    ai['admin'].post(ai['url']+'/model/probe',json={'version':cfg['version']}).raise_for_status()
    key=library(ai)['keys'][0];other=other_project(ai);bind(ai,other,[key['id']])
    snap=ai['storage'].ai_provider_snapshot(ai['p']['id'],ai['principal'],ai['admin'].get(ai['url']).json()['version'])
    before=[ai['storage'].ai_settings(p,ai['principal'])['version'] for p in (ai['p']['id'],other)]
    body={'version':library(ai)['version'],'key_version':key['version']}
    path='/api/v1/ai/keys/'+key['id']
    if action=='delete':r=ai['admin'].request('DELETE',path,json=body)
    else:r=ai['admin'].put(path,json=body|{'label':key['label'],'quota_group':key['quota_group'],'enabled':action!='pause',**({'key':'replacement-secret'} if action=='replace' else {})})
    assert r.status_code==200,r.text
    assert all(ai['storage'].ai_settings(p,ai['principal'])['version']>v for p,v in zip((ai['p']['id'],other),before))
    with pytest.raises(Error):
        ai['storage'].ai_store_provider_evidence(ai['p']['id'],ai['principal'],snap['version'],snap['credential_version'],snap['settings']['model'],key_id=key['id'],key_version=key['version'],capabilities=CAPS)
    for p in (ai['p']['id'],other):
        current=ai['storage'].ai_provider_keys(p,ai['principal'])
        assert not current['keys'] if action=='delete' else current['keys'][0]['status'] in ('paused','unverified')
    if action=='replace':assert not library(ai)['keys'][0]['verified_models'] and not library(ai)['keys'][0]['catalogue']


def test_project_detach_and_provider_switch_preserve_other_projects(ai):
    select_model(ai);other=other_project(ai);key=library(ai)['keys'][0]
    bind(ai,other,[key['id']]);bind(ai,ai['p']['id'],[])
    assert ai['admin'].get('/api/v1/projects/'+other+'/ai').json()['has_key']
    assert len(library(ai)['keys'])==1
    bind(ai,ai['p']['id'],[key['id']]);cfg=ai['admin'].get(ai['url']).json()
    r=ai['admin'].put(ai['url'],json={'version':cfg['version'],'settings':cfg['settings']|{'provider':'groq'}})
    assert r.status_code==200 and r.json()['settings']['model'] is None
    assert len(library(ai)['keys'])==1 and library(ai)['keys'][0]['projects'][0]['id']==other


def test_global_duplicate_and_library_conflict_reject_before_io(ai):
    key=add_account(ai);before=library(ai)
    r=ai['admin'].post('/api/v1/ai/keys',json={'version':before['version'],'provider':'kodekey','purpose':'generation','label':'Duplicate','quota_group':'different','key':'synthetic-account-secret'})
    assert r.status_code==409 and library(ai)==before
    bind(ai,ai['p']['id'],[key['id']])
    r=ai['admin'].put('/api/v1/ai/keys/'+key['id'],json={'version':before['version'],'key_version':key['version'],'label':'Old edit','quota_group':key['quota_group'],'enabled':False})
    assert r.status_code==409 and not ai['fake'].calls
    # Participant cookie CSRF enforcement also applies to the new global routes.
    client=TestClient(ai['app']);client.cookies.update(ai['admin'].cookies)
    assert client.post('/api/v1/ai/keys',json={'version':library(ai)['version'],'provider':'kodekey','purpose':'generation','label':'CSRF','key':'csrf-secret'}).status_code==403


@pytest.mark.parametrize('provider,purpose',[('cloudflare','generation'),('groq','decision'),('gemini','both'),('unreviewed','generation')])
def test_unsupported_provider_roles_and_wrong_provider_bindings(ai,provider,purpose):
    r=ai['admin'].post('/api/v1/ai/keys',json={'version':library(ai)['version'],'provider':provider,'purpose':purpose,'label':'Bad role','key':'synthetic-token',**({'account_id':'a'*32} if provider=='cloudflare' else {})})
    assert r.status_code==400 and library(ai)['keys']==[]
    key=add_account(ai,provider='groq')
    r=ai['admin'].put(ai['url']+'/keys/selection',json={'version':0,'library_version':library(ai)['version'],'key_ids':[key['id']]})
    assert r.status_code==409 and not ai['fake'].calls


def test_global_catalogue_refresh_fences_late_revocation_and_role_separation(ai):
    key=add_account(ai,purpose='both')
    def pause():
        ai['fake'].hook=None
        r=ai['admin'].put('/api/v1/ai/keys/'+key['id'],json={'version':library(ai)['version'],'key_version':key['version'],'label':key['label'],'quota_group':key['quota_group'],'enabled':False})
        assert r.status_code==200
    ai['fake'].hook=pause
    r=ai['admin'].post('/api/v1/ai/keys/'+key['id']+'/models/refresh',json={'version':library(ai)['version'],'key_version':key['version'],'role':'generation'})
    assert r.status_code==409 and not library(ai)['keys'][0]['catalogue']
    with ai['storage'].transaction() as c:
        assert c.execute('SELECT count(*) FROM ai_provider_admin_checks').fetchone()[0]==0


def test_cloudflare_account_credentials_remain_encrypted_and_derived_group_fixed(ai):
    first=add_account(ai,provider='cloudflare',purpose='decision',secret='synthetic-token-one',account_id='a'*32)
    second=add_account(ai,provider='cloudflare',purpose='decision',secret='synthetic-token-two',account_id='a'*32,quota_group='pretend-new-account')
    assert first['quota_group']==second['quota_group']
    r=ai['admin'].put('/api/v1/ai/keys/'+first['id'],json={'version':library(ai)['version'],'key_version':first['version'],'label':'Renamed','quota_group':'circumvent-cooldown','enabled':True})
    assert r.status_code==200 and r.json()['keys'][0]['quota_group']==first['quota_group']
    assert 'a'*32 not in json.dumps(r.json()) and 'synthetic-token' not in json.dumps(r.json())


def test_shared_decision_models_and_removal_stay_fail_closed(ai_store):
    s,p,pid,other=ai_store
    lib=s.ai_account_key_add(p,0,'cloudflare','decision','Clef','Account','encrypted-decision')
    key=lib['keys'][0];kid=key['id']
    for project,model in ((pid,'@cf/cloudflare/clef'),(other,'@cf/cloudflare/clef-flash')):
        topic=s.ai_topic_settings(project,p)
        topic=s.ai_topic_configure(project,p,topic['version'],topic['policy']|{'provider':'cloudflare'})
        topic=s.ai_project_key_selection(project,p,topic['version'],s.ai_account_keys(p)['version'],'decision',[kid])
        topic=s.ai_decision_catalogue(project,p,topic['version'],kid,key['version'],['@cf/cloudflare/clef','@cf/cloudflare/clef-flash'])
        topic=s.ai_topic_configure(project,p,topic['version'],topic['policy']|{'model':model,'scope':'Acorn support','public_summary':'Acorn'})
        check=s.ai_decision_check_begin(project,p,topic['version'],kid,key['version'])
        s.ai_decision_check_finish(check['id'],DecisionResult(.99,model,Usage(10,0,10)),probe=True)
        s.ai_topic_configure(project,p,topic['version'],topic['policy']|{'enabled':True})
    assert all(s.ai_topic_settings(project,p)['keys'][0]['verified'] for project in (pid,other))
    topic=s.ai_topic_settings(pid,p)
    s.ai_decision_key_remove(pid,p,topic['version'],kid,key['version'])
    assert s.ai_topic_settings(pid,p)['policy']['enabled'] and not s.ai_topic_settings(pid,p)['keys']
    assert s.ai_topic_settings(other,p)['keys'][0]['verified']
    lib=s.ai_account_keys(p)
    s.ai_account_key_action(p,lib['version'],kid,key['version'],'delete')
    assert s.ai_topic_settings(other,p)['policy']['enabled'] and not s.ai_topic_settings(other,p)['keys']


def test_shared_generation_lease_cooldown_and_global_pause(ready_ai_jobs):
    env=ready_ai_jobs;s,p,pid=env['store'],env['principal'],env['project']
    other=s.create_project({'identifier':'second','name':'Second','description':''},p['user_id'])['id']
    key=s.ai_account_keys(p)['keys'][0]
    s.ai_project_key_selection(other,p,0,s.ai_account_keys(p)['version'],'generation',[key['id']])
    cfg=s.ai_settings(other,p);s.ai_configure(other,p,cfg['version'],cfg['settings']|{'model':'test-model'})
    publish_source(s,p,other);cfg=s.ai_settings(other,p);s.ai_configure(other,p,cfg['version'],cfg['settings']|{'enabled':True})
    stamp=time.time()+1;job,selected=claim(env,stamp);dispatch(s,job,stamp)
    other_env={'store':s,'principal':p,'project':other}
    second,_=claim(other_env,stamp)
    assert not s.ai_provider_claim(second['id'],second['lease_token'],stamp)['available']
    snap=s.ai_provider_snapshot(other,p,s.ai_settings(other,p)['version'])
    with pytest.raises(Error,match='in use'):s.ai_admin_check_begin(p,snap)
    outcome=s.ai_provider_outcome(job['id'],job['lease_token'],0,'rate_limited',retry_after=600,current_time=stamp)
    assert outcome['accepted']
    assert s.ai_provider_keys(other,p)['keys'][0]['status']=='cooling_down'
    lib=s.ai_account_keys(p)
    s.ai_account_key_update(p,lib['version'],key['id'],key['version'],label='Shared',quota_group=key['quota_group'],enabled=False)
    assert not s.ai_settings(pid,p)['settings']['enabled'] and not s.ai_settings(other,p)['settings']['enabled']
    assert not s.ai_job_before_dispatch(second['id'],second['lease_token'],stamp)
    assert s.ai_commit_reply(job['id'],job['lease_token'],'late answer',[],0) is None


def test_shared_admin_check_lease_expires_and_fences_competing_roles(ai_store):
    s,p,pid,other=ai_store
    cfg=s.ai_configure(pid,p,0,default_ai_settings(),protected_key='fixture-encrypted')
    snap=s.ai_provider_snapshot(pid,p,cfg['version']);lease=s.ai_admin_check_begin(p,snap)
    with pytest.raises(Error,match='in use'):s.ai_admin_check_begin(p,snap)
    with s.transaction(True) as c:c.execute('UPDATE ai_provider_admin_checks SET lease_until=?',(time.time()-1,))
    replacement=s.ai_admin_check_begin(p,snap)
    s.ai_admin_check_end(lease)
    with pytest.raises(Error,match='in use'):s.ai_admin_check_begin(p,snap)
    s.ai_admin_check_end(replacement)


def test_pre016_upgrade_preserves_ids_both_roles_history_and_backups(tmp_path,monkeypatch):
    old=tmp_path/'legacy'/'migrations';old.mkdir(parents=True)
    for file in Path(adapter.__file__).with_name('migrations').glob('*.sql'):
        if file.name<'016':shutil.copyfile(file,old/file.name)
    path=tmp_path/'legacy.sqlite';cipher=Vault(Fernet.generate_key().decode())
    with migration_directory(monkeypatch,old):
        s=SQLiteStorage(path,'https://box.example')
    user=s.create_native_account('admin@example.test','hash','Admin','admin',True)
    s.create_session(user['id'],'session','csrf',time.time()+3600)
    p={'kind':'admin','user_id':user['id'],'session_id':s.session('session')['session_id']}
    pid=s.create_project({'identifier':'legacy','name':'Legacy','description':''},user['id'])['id']
    stamp='2026-10-04T00:00:00Z';protected=cipher.protect('migration-secret')
    with s.transaction(True) as c:
        c.execute("INSERT INTO ai_provider_quota_groups(id,provider,label) VALUES('group','kodekey','Account')")
        c.execute("INSERT INTO ai_provider_keys(id,project_id,provider,label,quota_group_id,is_default,protected_key,catalogue,capabilities,capabilities_model,capabilities_key_version,created_at,updated_at,purpose) VALUES('legacy-key',?,'kodekey','Preserved','group',1,?,?,?,'answer',1,?,?,'both')",(pid,protected,json.dumps(['answer']),json.dumps(CAPS),stamp,stamp))
        c.execute("INSERT INTO ai_settings(project_id,settings,version,credential_version,protected_key,catalogue,capabilities,updated_at) VALUES(?,?,7,3,?,'[]','{}',?)",(pid,json.dumps(default_ai_settings()|{'model':'answer'}),protected,stamp))
        c.execute("INSERT INTO ai_decision_keys VALUES('legacy-key',?,?,'decision',1,1)",(pid,json.dumps(['decision'])))
        c.execute('INSERT INTO ai_topic_policies VALUES(?,4,?,?)',(pid,json.dumps(default_topic_policy()|{'model':'decision'}),stamp))
        c.execute("INSERT INTO ai_jobs(id,project_id,kind,resource_id,dedupe_key,config_version,authority,state,attempt_count,next_attempt,created_at,updated_at) VALUES('old-job',?,'preview',?,'old',7,?,'succeeded',1,0,?,?)",(pid,pid,json.dumps({'principal':p}),stamp,stamp))
        c.execute("INSERT INTO ai_provider_attempts VALUES('old-job',1,'legacy-key',1,0,'group',0,'old-token')")
        c.execute("INSERT INTO ai_decision_attempts VALUES('old-job',1,'old-token','legacy-key',1,0,0)")
    # FK violations at the end of reconstruction must roll back all schema/data.
    broken=tmp_path/'broken'/'migrations'
    shutil.copytree(Path(adapter.__file__).with_name('migrations'),broken)
    (broken/'999_dangling_reference.sql').write_text("INSERT INTO ai_generation_keys VALUES('missing-project','legacy-key',0);\n")
    with migration_directory(monkeypatch,broken):
        with pytest.raises(RuntimeError,match='foreign-key verification failed'):
            SQLiteStorage(path,s.base_url)
    with s.transaction() as c:
        assert not c.execute("SELECT 1 FROM schema_migrations WHERE version='016_ai_account_keys.sql'").fetchone()
        assert c.execute("SELECT protected_key FROM ai_provider_keys WHERE id='legacy-key'").fetchone()[0]==protected
    with ThreadPoolExecutor(max_workers=4) as pool:stores=list(pool.map(lambda _:SQLiteStorage(path,s.base_url),range(4)))
    for handle in stores:
        assert handle.ai_settings(pid,p)['version']==7 and handle.ai_settings(pid,p)['capabilities']==CAPS
        assert handle.ai_provider_keys(pid,p)['keys'][0]['id']=='legacy-key'
        assert handle.ai_topic_settings(pid,p)['keys'][0]['verified']
        assert cipher.reveal(handle.ai_provider_snapshot(pid,p,7)['protected_key'])=='migration-secret'
        with handle.transaction() as c:
            assert not c.execute('PRAGMA foreign_key_check').fetchall()
            assert c.execute('SELECT key_id FROM ai_provider_attempts').fetchone()[0]=='legacy-key'
            assert c.execute('SELECT protected_key FROM ai_settings').fetchone()[0] is None
    backup=tmp_path/'restored.sqlite';stores[0].backup(backup);restored=SQLiteStorage(backup,s.base_url)
    assert restored.ai_account_keys(p)==stores[0].ai_account_keys(p)
    assert {cipher.reveal(value) for value in restored.signing_secrets()}=={'migration-secret'}


def test_role_edits_detach_revoked_bindings_without_implicit_regrant(ai_store):
    s,p,pid,other=ai_store
    lib=s.ai_account_key_add(p,0,'kodekey','both','Both roles','Account','encrypted')
    key=lib['keys'][0]
    s.ai_project_key_selection(pid,p,0,lib['version'],'generation',[key['id']])
    s.ai_project_key_selection(other,p,0,s.ai_account_keys(p)['version'],'decision',[key['id']])
    before=s.ai_topic_settings(other,p)['version']
    lib=s.ai_account_key_update(p,s.ai_account_keys(p)['version'],key['id'],key['version'],label=key['label'],quota_group=key['quota_group'],enabled=True,purpose='generation')
    assert not s.ai_topic_settings(other,p)['keys'] and s.ai_topic_settings(other,p)['version']>before
    key=lib['keys'][0]
    s.ai_account_key_update(p,lib['version'],key['id'],key['version'],label=key['label'],quota_group=key['quota_group'],enabled=True,purpose='both')
    assert not s.ai_topic_settings(other,p)['keys']
    assert s.ai_provider_keys(pid,p)['keys'][0]['id']==key['id']


def test_model_denial_keeps_other_model_and_role_evidence(ai_store):
    s,p,pid,other=ai_store
    s.ai_configure(pid,p,0,default_ai_settings()|{'model':'first'},protected_key='encrypted')
    first={'store':s,'principal':p,'project':pid}
    evidence(first,catalogue=['first','second'])
    key=s.ai_account_keys(p)['keys'][0]
    s.ai_project_key_selection(other,p,0,s.ai_account_keys(p)['version'],'generation',[key['id']])
    cfg=s.ai_settings(other,p);s.ai_configure(other,p,cfg['version'],cfg['settings']|{'model':'second'})
    evidence({'store':s,'principal':p,'project':other},catalogue=['first','second'])
    ready_runtime(s,p)
    for project in (pid,other):
        publish_source(s,p,project);cfg=s.ai_settings(project,p)
        s.ai_configure(project,p,cfg['version'],cfg['settings']|{'enabled':True})
    snap=s.ai_provider_snapshot(pid,p,s.ai_settings(pid,p)['version'])
    s.ai_provider_check_outcome(pid,p,snap['version'],key['id'],key['version'],snap['health_epoch'],snap['group_epoch'],'access_denied')
    assert not s.ai_settings(pid,p)['settings']['enabled']
    assert s.ai_settings(other,p)['settings']['enabled'] and s.ai_provider_keys(other,p)['keys'][0]['eligible']
    lib=s.ai_account_keys(p)
    snap=s.ai_account_key_snapshot(p,lib['version'],key['id'],key['version'])
    s.ai_account_key_failure(p,snap,'access_denied',role='decision')
    assert s.ai_provider_keys(other,p)['keys'][0]['eligible']


def test_admin_generation_and_decision_checks_share_key_and_account_lease(ai_store):
    s,p,pid,other=ai_store
    lib=s.ai_account_key_add(p,0,'kodekey','both','Both roles','Account','encrypted')
    key=lib['keys'][0];kid=key['id']
    s.ai_project_key_selection(pid,p,0,lib['version'],'generation',[kid])
    topic=s.ai_project_key_selection(other,p,0,s.ai_account_keys(p)['version'],'decision',[kid])
    topic=s.ai_decision_catalogue(other,p,topic['version'],kid,key['version'],['decision'])
    topic=s.ai_topic_configure(other,p,topic['version'],topic['policy']|{'model':'decision'})
    snap=s.ai_provider_snapshot(pid,p,s.ai_settings(pid,p)['version']);lease=s.ai_admin_check_begin(p,snap)
    with pytest.raises(Error,match='not available'):s.ai_decision_check_begin(other,p,topic['version'],kid,key['version'])
    s.ai_admin_check_end(lease)
    decision=s.ai_decision_check_begin(other,p,topic['version'],kid,key['version'])
    with pytest.raises(Error,match='in use'):s.ai_admin_check_begin(p,snap)
    s.ai_decision_check_finish(decision['id'],error='unavailable')


def test_limit_is_per_account_provider_and_revoked_session_cannot_manage(ai_store):
    s,p,pid,other=ai_store
    for index in range(99):
        lib=s.ai_account_key_add(p,s.ai_account_keys(p)['version'],'groq','generation',str(index),'Account','encrypted')
    with pytest.raises(Error,match='99 provider keys'):
        s.ai_account_key_add(p,lib['version'],'groq','generation','Overflow','Account','encrypted')
    s.ai_account_key_add(p,lib['version'],'gemini','generation','Another provider','Account','encrypted')
    s.revoke_session(p['session_id'])
    with pytest.raises(Error):s.ai_account_keys(p)
    with pytest.raises(Error):s.ai_account_key_action(p,lib['version'],lib['keys'][0]['id'],1,'retry')


def test_successful_text_recheck_preserves_tool_evidence_for_other_projects(ai):
    selected=select_model(ai)
    ai['admin'].post(ai['url']+'/model/probe',json={'version':selected['version'],'tools':True}).raise_for_status()
    ai['admin'].post(ai['url']+'/model/probe',json={'version':selected['version'],'tools':False}).raise_for_status()
    assert ai['admin'].get(ai['url']).json()['capabilities']['tool_calling']


def test_answer_setup_can_browse_unbound_models_then_switch_bind_save_and_verify(ai):
    """The browser's combined action uses existing fenced APIs in this order."""
    select_model(ai)
    old_key = library(ai)['keys'][0]['id']
    ramp = add_account(ai, provider='ramp', secret='fixture-ramp-secret')
    before = ai['admin'].get(ai['url']).json()
    lib = library(ai)
    refreshed = ai['admin'].post('/api/v1/ai/keys/'+ramp['id']+'/models/refresh', json={
        'version':lib['version'], 'key_version':ramp['version'], 'role':'generation'})
    assert refreshed.status_code == 200
    lib = refreshed.json()
    assert next(k for k in lib['keys'] if k['id']==ramp['id'])['catalogue']
    assert ai['admin'].get(ai['url']).json() == before  # Browsing never switches providers.
    current = ai['admin'].put(ai['url'], json={'version':before['version'],
        'settings':before['settings'] | {'provider':'ramp', 'model':None, 'enabled':False}})
    assert current.status_code == 200
    current = current.json()
    pool = ai['admin'].put(ai['url']+'/keys/selection', json={'version':current['version'],
        'library_version':lib['version']+1, 'key_ids':[ramp['id']]})
    assert pool.status_code == 200
    current = ai['admin'].put(ai['url'], json={'version':pool.json()['version'],
        'settings':current['settings'] | {'model':'Exact/Model:ID', 'enabled':False}})
    assert current.status_code == 200
    verified = ai['admin'].post(ai['url']+'/keys/'+ramp['id']+'/model/probe', json={
        'version':current.json()['version'], 'key_version':ramp['version'], 'tools':True})
    assert verified.status_code == 200 and verified.json()['keys'][0]['eligible']
    assert ai['fake'].calls[-1] == ('probe','fixture-ramp-secret','Exact/Model:ID',True)
    assert not ai['admin'].get(ai['url']).json()['settings']['enabled']
    assert any(k['id']==old_key for k in library(ai)['keys'])  # Old credential preserved.
