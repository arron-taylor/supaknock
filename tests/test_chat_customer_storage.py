"""Durable chat identity and transaction contracts against real SQLite."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import json
import sqlite3
import time
from uuid import uuid4

import pytest

from feedback.domain import Actor, now
from feedback.errors import Error
from feedback.security import digest
from feedback.storage.sqlite import SQLiteStorage

ORIGIN = 'https://website.example'


@pytest.fixture
def chat_store(tmp_path):
    store = SQLiteStorage(tmp_path/'chat.sqlite', 'https://feedback.example')
    admin = store.create_native_account('admin@example.com', 'hash', 'Owner', 'admin', True)
    store.create_session(admin['id'], 'admin-session-hash', 'csrf', time.time()+86400)
    session = store.session('admin-session-hash')
    principal = {'kind':'admin','user_id':admin['id'],'session_id':session['session_id']}
    projects = []
    actors = []
    for identifier in ('one','two'):
        p = store.create_project({'identifier':identifier,'name':identifier.title(),'description':''}, admin['id'])
        store.chat_configure(p['id'],principal,True,[ORIGIN],0)
        kid = store.create_api_key(p['id'],'Backend',identifier+'-hash','sb_test',['participate'],admin['id'])
        projects.append(p)
        actors.append(Actor(project_id=p['id'],key_id=kid,permissions=('participate',)))
    return store, projects, actors, principal


def customer(store, project, actor, external=' Account 0001 ', origin=None, parent=None, name=None):
    token = 'cc_'+str(uuid4())
    result = store.chat_create_customer_session(project['id'],actor,external,digest(token),time.time()+3600,origin,parent,name)
    principal = store.chat_customer_session(digest(token),origin)
    return result, principal, token


def test_customer_concurrent_resolution_is_project_scoped_and_reuses_feedback_identity(chat_store):
    store,projects,actors,admin = chat_store
    uid = store.resolve_external_identity(projects[0]['id'],' Account 0001 ')
    def issue(_):return customer(store,projects[0],actors[0],name='Acme reader')[0]
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(issue, range(16)))
    assert {r['customer']['user_id'] for r in results} == {uid}
    assert len({r['customer']['id'] for r in results}) == 1
    assert len({r['session_id'] for r in results}) == 16
    other,_,_ = customer(store,projects[1],actors[1])
    normalized,_,_ = customer(store,projects[0],actors[0],external='account 0001')
    assert other['customer']['id'] != results[0]['customer']['id']
    assert other['customer']['user_id'] != uid
    assert normalized['customer']['id'] != results[0]['customer']['id']
    assert results[0]['customer'] == {'id':results[0]['customer']['id'],'kind':'external','display_name':'Acme reader','user_id':uid,'external_user_id':' Account 0001 '}
    with store.transaction() as c:
        assert c.execute('SELECT display_name FROM users WHERE id=?',(uid,)).fetchone()[0] is None
        assert not c.execute('SELECT 1 FROM local_credentials WHERE user_id=?',(uid,)).fetchone()
        assert c.execute('SELECT count(*) FROM external_identities WHERE project_id=? AND external_user_id=?',(projects[0]['id'],' Account 0001 ')).fetchone()[0] == 1
    with pytest.raises(Error) as e:customer(store,projects[1],actors[0])
    assert e.value.status == 404


def test_history_dedup_and_reads_survive_renewal_devices_restart_and_backup(chat_store,tmp_path):
    store,projects,actors,admin = chat_store
    _,first,token = customer(store,projects[0],actors[0])
    _,second,_ = customer(store,projects[0],actors[0])
    client_id = str(uuid4())
    with ThreadPoolExecutor(max_workers=6) as pool:
        chats = list(pool.map(lambda n:store.chat_create_conversation(first if n%2 else second,client_id),range(8)))
    cid = chats[0]['id']
    assert len({x['id'] for x in chats}) == 1
    message_id = str(uuid4())
    with ThreadPoolExecutor(max_workers=6) as pool:
        messages = list(pool.map(lambda n:store.chat_send(cid,first if n%2 else second,message_id,'Hello'),range(8)))
    assert len({x['id'] for x in messages}) == 1
    assert messages[0]['sender'] == 'visitor' and messages[0]['sender_role'] == 'customer'
    with pytest.raises(Error) as e:store.chat_send(cid,second,message_id,'Changed')
    assert e.value.status == 409
    store.chat_send(cid,admin,str(uuid4()),'Reply')
    assert store.chat_conversation(cid,second)['unread_count'] == 1
    assert store.chat_read(cid,first,2) == {'read_sequence':2}
    assert store.chat_conversation(cid,second)['unread_count'] == 0
    assert store.chat_read(cid,second,0) == {'read_sequence':2}
    with store.transaction(True) as c:c.execute('UPDATE chat_customer_sessions SET expires_at=0 WHERE id=?',(first['session_id'],))
    _,renewed,new_token = customer(store,projects[0],actors[0])
    with pytest.raises(Error):store.chat_customer_session(digest(token),None)
    assert store.chat_customer_conversations(renewed,None,25,0)[0]['id'] == cid
    assert store.chat_send(cid,renewed,message_id,'Hello')['id'] == messages[0]['id']
    _,stranger,_ = customer(store,projects[0],actors[0],external='different')
    for operation in (lambda:store.chat_conversation(cid,stranger),lambda:store.chat_send(cid,stranger,str(uuid4()),'Impersonation'),lambda:store.chat_read(cid,stranger,2)):
        with pytest.raises(Error) as e:operation()
        assert e.value.status == 404
    target = tmp_path/'restored.sqlite'
    store.backup(target)
    restored = SQLiteStorage(target,store.base_url)
    restored_principal = restored.chat_customer_session(digest(new_token),None)
    assert restored.chat_conversation(cid,restored_principal)['unread_count'] == 0
    assert len(restored.chat_messages(cid,restored_principal,0,100)) == 2
    restarted = SQLiteStorage(store.path,store.base_url)
    assert restarted.chat_customer_conversations(renewed,None,25,0)[0]['id'] == cid


def test_customer_origin_binding_parent_permissions_and_revocation_are_rechecked(chat_store):
    store,projects,actors,admin = chat_store
    _,principal,token = customer(store,projects[0],actors[0],origin=ORIGIN)
    cv = store.chat_create_conversation(principal,str(uuid4()))
    for origin in (None,'https://other.example',store.base_url):
        with pytest.raises(Error) as e:store.chat_customer_session(digest(token),origin)
        assert e.value.status == 403
    store.chat_ticket(cv['id'],principal,'ticket-hash',time.time()+60)
    _,socket = store.chat_consume_ticket('ticket-hash',ORIGIN)
    assert socket['customer_id'] == principal['customer_id']
    with pytest.raises(Error):store.chat_consume_ticket('ticket-hash',ORIGIN)
    store.chat_ticket(cv['id'],principal,'pending-ticket',time.time()+60)
    with store.transaction(True) as c:c.execute("UPDATE api_keys SET permissions='[]' WHERE id=?",(actors[0].key_id,))
    operations = (lambda:store.chat_customer_session(digest(token),ORIGIN),
                  lambda:store.chat_customer_conversations(socket,None,25,0),
                  lambda:store.chat_messages(cv['id'],socket,0,100),
                  lambda:store.chat_send(cv['id'],socket,str(uuid4()),'Denied'),
                  lambda:store.chat_read(cv['id'],socket,0),
                  lambda:store.chat_ticket(cv['id'],socket,'other-ticket',time.time()+60),
                  lambda:store.chat_consume_ticket('pending-ticket',ORIGIN))
    for operation in operations:
        with pytest.raises(Error) as e:operation()
        assert e.value.status == 401
    with store.transaction(True) as c:c.execute('UPDATE api_keys SET permissions=? WHERE id=?',(json.dumps(['participate']),actors[0].key_id))
    _,second,_ = customer(store,projects[0],actors[0],origin=ORIGIN)
    store.chat_revoke_customer_session(principal)
    with pytest.raises(Error):store.chat_conversation(cv['id'],principal)
    assert store.chat_conversation(cv['id'],second)['id'] == cv['id']
    store.chat_revoke_customer_sessions(projects[0]['id'],actors[0],' Account 0001 ')
    with pytest.raises(Error):store.chat_conversation(cv['id'],second)
    _,renewed,_ = customer(store,projects[0],actors[0],origin=ORIGIN)
    assert store.chat_conversation(cv['id'],renewed)['id'] == cv['id']
    store.revoke_api_key(projects[0]['id'],actors[0].key_id)
    with pytest.raises(Error):store.chat_conversation(cv['id'],renewed)


def test_native_customer_has_no_external_identity_and_follows_parent_logout(chat_store):
    store,projects,actors,admin = chat_store
    native = store.create_native_account('reader@example.com','hash','Native reader')
    store.create_session(native['id'],'native-hash','csrf',time.time()+86400,'native')
    parent = store.session('native-hash')['session_id']
    actor = Actor(user_id=native['id'],role='participant')
    result,principal,_ = customer(store,projects[0],actor,None,parent=parent)
    browser,bp,_ = customer(store,projects[0],actor,None,origin=store.base_url,parent=parent)
    assert result['customer'] == browser['customer']
    assert result['customer']['kind'] == 'native'
    assert result['customer']['external_user_id'] is None
    assert result['customer']['display_name'] == 'Native reader'
    with pytest.raises(Error):customer(store,projects[0],actor,'forged-id',parent=parent)
    with pytest.raises(Error):customer(store,projects[0],actor,None,parent=admin['session_id'])
    with pytest.raises(Error):customer(store,projects[0],actors[0],origin=store.base_url)
    cv = store.chat_create_conversation(principal,str(uuid4()))
    assert store.chat_conversation(cv['id'],bp)['id'] == cv['id']
    store.revoke_session(parent)
    with pytest.raises(Error):store.chat_conversation(cv['id'],principal)
    store.create_session(native['id'],'renew-native','csrf',time.time()+86400,'native')
    _,renewed,_ = customer(store,projects[0],actor,None,parent=store.session('renew-native')['session_id'])
    assert store.chat_conversation(cv['id'],renewed)['id'] == cv['id']
    with store.transaction() as c:assert not c.execute('SELECT 1 FROM external_identities WHERE user_id=?',(native['id'],)).fetchone()


def test_message_event_and_delivery_commit_together_and_contain_no_body(chat_store,monkeypatch):
    store,projects,actors,admin = chat_store
    p = projects[0]
    store.update_webhook(p['id'],'https://receiver.example/chat',True,['chat.message_created'],'protected-secret',p['version'])
    _,principal,token = customer(store,p,actors[0])
    cv = store.chat_create_conversation(principal,str(uuid4()))
    original = store._persist_event
    def fail_after_persistence(*args,**kwargs):
        original(*args,**kwargs)
        raise RuntimeError('injected post-persistence failure')
    with monkeypatch.context() as m:
        m.setattr(store,'_persist_event',fail_after_persistence)
        with pytest.raises(RuntimeError):store.chat_send(cv['id'],principal,str(uuid4()),'Never committed')
    assert store.chat_messages(cv['id'],principal,0,100) == []
    assert store.chat_conversation(cv['id'],principal)['last_sequence'] == 0
    with store.transaction() as c:
        assert c.execute('SELECT count(*) FROM request_events').fetchone()[0] == 0
        assert c.execute('SELECT count(*) FROM webhook_deliveries').fetchone()[0] == 0
    cmid = str(uuid4())
    message = store.chat_send(cv['id'],principal,cmid,'Private body must not be in the event')
    store.chat_send(cv['id'],principal,cmid,'Private body must not be in the event')
    store.chat_send(cv['id'],admin,str(uuid4()),'Private admin reply')
    with store.transaction() as c:
        events = [json.loads(r[0]) for r in c.execute('SELECT payload FROM request_events ORDER BY occurred_at,id')]
        assert len(events) == 2
        payload = next(e for e in events if e['id']==message['id'])
        assert payload['conversation'] == {'id':cv['id']}
        assert payload['message'] == {'id':message['id'],'sequence':1,'sender_role':'customer'}
        assert payload['customer']['external_user_id'] == ' Account 0001 '
        assert next(e for e in events if e['id']!=message['id'])['message']['sender_role'] == 'admin'
        assert all(secret not in json.dumps(events) for secret in (message['body'],'Private admin reply','protected-secret',token,'token_hash','password_hash','email'))
        assert c.execute('SELECT count(*) FROM webhook_deliveries').fetchone()[0] == 2
    job = store.claim_delivery(time.time()+1)
    event_id = job['event_id']
    store.finish_delivery(job['id'],job['lease_token'],'queued',503,'temporary',0)
    retried = store.claim_delivery(time.time()+2)
    assert retried['event_id'] == event_id and retried['payload'] == job['payload']


def test_migration_backfills_anonymous_without_resetting_tokens_reads_or_dedup(tmp_path):
    path = tmp_path/'old.sqlite'
    c = sqlite3.connect(path)
    migrations = Path(__file__).parents[1]/'feedback/storage/migrations'
    c.execute('CREATE TABLE schema_migrations(version TEXT PRIMARY KEY,applied_at TEXT NOT NULL)')
    for file in sorted(migrations.glob('*.sql')):
        if file.name.startswith('006_'):break
        c.executescript(file.read_text())
        c.execute('INSERT INTO schema_migrations VALUES(?,?)',(file.name,now()))
        c.commit()
    uid,pid,cid,sid,mid = [str(uuid4()) for _ in range(5)]
    client_id = str(uuid4());ts = now();token = 'cv_previous-release'
    c.execute("INSERT INTO users VALUES(?,NULL,'admin',?,?)",(uid,ts,ts))
    c.execute("INSERT INTO projects(id,identifier,name,description,created_by,created_at,updated_at) VALUES(?,'old','Old','',?,?,?)",(pid,uid,ts,ts))
    c.execute('INSERT INTO chat_settings VALUES(?,1,?,1)',(pid,json.dumps([ORIGIN])))
    c.execute('INSERT INTO chat_conversations(id,project_id,created_at,updated_at,last_sequence,first_message_at) VALUES(?,?,?,?,1,?)',(cid,pid,ts,ts,ts))
    c.execute('INSERT INTO chat_sessions(id,conversation_id,token_hash,origin,created_at,expires_at,read_sequence) VALUES(?,?,?,?,?,?,1)',(sid,cid,digest(token),ORIGIN,ts,time.time()+86400))
    c.execute("INSERT INTO chat_messages VALUES(?,?,?,?, 'visitor',?,?,?)",(mid,cid,1,client_id,sid,'Old message',ts))
    c.execute("INSERT INTO chat_tickets VALUES(?,?,'visitor',?,NULL,?,?)",('old-ticket',cid,sid,ORIGIN,time.time()+60))
    c.commit();c.close()
    store = SQLiteStorage(path,'https://feedback.example')
    principal = store.chat_visitor(digest(token),ORIGIN)
    cv = store.chat_conversation(cid,principal)
    assert cv['customer'] == {'id':cid,'kind':'anonymous','display_name':'Website visitor','user_id':None,'external_user_id':None}
    assert store.chat_send(cid,principal,client_id,'Old message')['id'] == mid
    assert store.chat_read(cid,principal,0)['read_sequence'] == 1
    assert store.chat_customer_conversations(principal,None,25,0)[0]['id'] == cid
    assert store.chat_consume_ticket('old-ticket',ORIGIN)[0] == cid
    with store.transaction() as c:
        assert c.execute('PRAGMA foreign_key_check').fetchall() == []
        assert c.execute('SELECT count(*) FROM request_events').fetchone()[0] == 0
    with pytest.raises(Error):store.chat_create_conversation(principal,str(uuid4()))
    store.chat_revoke(cid,principal)
    with pytest.raises(Error):store.chat_visitor(digest(token),ORIGIN)
