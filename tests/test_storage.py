from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
import threading
import time
import pytest
from feedback.domain import Actor
from feedback.errors import Error
from feedback.storage.sqlite import SQLiteStorage
from feedback.security import hasher, digest
from feedback.config import Config
from conftest import submission, PASSWORD


def test_concurrent_identity_and_votes(workflow):
    w=workflow;s=w['app'].state.storage
    with ThreadPoolExecutor(max_workers=8) as pool:
        ids=list(pool.map(lambda _:s.resolve_external_identity(w['p']['id'],'0001 Case '),range(20)))
    assert len(set(ids))==1
    with s.transaction() as c:
        assert c.execute('SELECT count(*) FROM external_identities').fetchone()[0]==1
        assert c.execute('SELECT count(*) FROM users').fetchone()[0]==3
    actor=Actor(user_id=ids[0],project_id=w['p']['id'],key_id=w['k']['id'],role='integration',permissions=('participate',))
    r=s.create_request(w['b']['id'],'Concurrent vote','',actor)
    admin=Actor(user_id=s.credentials('admin@example.com')['id'],role='admin')
    s.moderate_request(r['id'],'accept',None,1,admin)
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _:s.set_vote(r['id'],True,actor),range(20)))
    assert s.request(r['id'])['vote_count']==1
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _:s.set_vote(r['id'],False,actor),range(20)))
    assert s.request(r['id'])['vote_count']==0


@pytest.mark.parametrize('delete',[False,True])
def test_acceptance_races_creator_update(workflow,delete):
    w=workflow;s=w['app'].state.storage
    owner=Actor(user_id=s.credentials('user@example.com')['id'],role='participant')
    admin=Actor(user_id=s.credentials('admin@example.com')['id'],role='admin')
    for _ in range(6):
        r=s.create_request(w['b']['id'],'Race','Original',owner)
        barrier=threading.Barrier(2)
        def creator():
            barrier.wait()
            try:return s.update_creator_request(r['id'],'Edited','Edited',1,owner,delete)
            except Error as e:return e.status
        def accept():
            barrier.wait()
            try:return s.moderate_request(r['id'],'accept',None,1,admin)
            except Error as e:return e.status
        with ThreadPoolExecutor(max_workers=2) as pool:
            a=pool.submit(accept);b=pool.submit(creator)
            accepted,edited=a.result(),b.result()
        if isinstance(accepted,dict):
            assert edited==409
            current=s.request(r['id'])
            assert current['title']=='Race' and current['accepted_at']
        elif delete:
            assert edited is None and accepted==404
        else:
            assert accepted==409 and edited['title']=='Edited' and edited['accepted_at'] is None


def test_atomic_rollback_state_event_job(workflow):
    w=workflow;s=w['app'].state.storage
    owner=Actor(user_id=s.credentials('user@example.com')['id'],role='participant')
    original=s._persist_event
    def fail(*args,**kwargs):
        original(*args,**kwargs)
        raise RuntimeError('simulated commit failure')
    s._persist_event=fail
    with pytest.raises(RuntimeError):s.create_request(w['b']['id'],'Atomic','',owner)
    s._persist_event=original
    with s.transaction() as c:
        assert c.execute('SELECT count(*) FROM requests').fetchone()[0]==0
        assert c.execute('SELECT count(*) FROM request_events').fetchone()[0]==0
        assert c.execute('SELECT count(*) FROM webhook_deliveries').fetchone()[0]==0


def test_setup_concurrency_and_bad_token(tmp_path):
    from feedback.api import create_app
    from fastapi.testclient import TestClient
    from cryptography.fernet import Fernet
    cfg=Config(tmp_path/'feedback.sqlite',Fernet.generate_key().decode(),'s'*32,worker=False)
    app=create_app(cfg)
    c=TestClient(app)
    assert c.post('/api/v1/setup',json={'email':'admin@ex.com','password':PASSWORD,'setup_token':'bad-'*8}).status_code==403
    s=app.state.storage
    def first(i):
        try:return s.create_native_account(f'admin{i}@ex.com',hasher.hash(PASSWORD),None,'admin',True)
        except Error as e:return e.status
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(first,range(2)))
    assert sum(isinstance(r,dict) for r in results)==1
    assert 409 in results
    assert not s.setup_available()


def test_restart_migrations_backup_and_recovery(workflow,tmp_path):
    w=workflow;s=w['app'].state.storage
    r=submission(w['user'],w['b'])
    db=SQLiteStorage(w['cfg'].database_path,w['cfg'].base_url)
    assert db.request(r['id'])['title']==r['title']
    assert db.session(digest(w['user'].cookies['feedback_session']))
    with db.transaction() as c:
        assert {row[0] for row in c.execute('SELECT version FROM schema_migrations')} == {p.name for p in Path('feedback/storage/migrations').glob('*.sql')}
    backup=tmp_path/'backup.sqlite'
    db.backup(backup)
    restored=SQLiteStorage(backup,w['cfg'].base_url)
    assert restored.request(r['id'])['creator_id']==db.request(r['id'])['creator_id']
    assert restored.api_key(digest(w['k']['secret']))
    assert restored.boards(w['p']['id'])
    restored.recover_account('user@example.com',hasher.hash('local-recovery-password'))
    assert restored.session(digest(w['user'].cookies['feedback_session'])) is None
    with pytest.raises(RuntimeError):db.backup(backup)
    with db.transaction(True) as c:
        c.execute("INSERT INTO schema_migrations VALUES('999_future.sql','today')")
    with pytest.raises(RuntimeError,match='newer'):SQLiteStorage(w['cfg'].database_path,w['cfg'].base_url)


@pytest.mark.parametrize('provider',['firebase','mysql','unknown'])
def test_unsupported_provider_fails(monkeypatch,provider):
    monkeypatch.setenv('DATABASE_PROVIDER',provider)
    with pytest.raises(RuntimeError,match='Unsupported database provider'):Config.from_env()


def test_configuration_and_persistent_secrets(tmp_path,monkeypatch):
    monkeypatch.setenv('DATABASE_URL','sqlite://relative.sqlite')
    with pytest.raises(RuntimeError):Config.from_env()
    monkeypatch.setenv('DATABASE_URL','sqlite:///'+str(tmp_path/'db.sqlite'))
    cfg=Config.from_env();again=Config.from_env()
    assert cfg.database_path==tmp_path/'db.sqlite'
    assert cfg.secret==again.secret and cfg.setup_token==again.setup_token
    assert (tmp_path/'application.key').stat().st_mode & 0o777==0o600
    monkeypatch.setenv('PUBLIC_BASE_URL','https://feedback.example.com')
    monkeypatch.setenv('COOKIE_SECURE','false')
    with pytest.raises(RuntimeError,match='HTTPS'):Config.from_env()


def test_backup_bundle_restores_signing_secret_and_missing_key_fails(workflow,tmp_path,monkeypatch):
    from feedback.maintenance import backup
    from feedback.security import Vault
    from test_webhooks import configured
    w=workflow;secret=configured(w)
    submission(w['user'],w['b'])
    directory=tmp_path/'bundle'
    backup(w['cfg'],directory)
    assert (directory/'application.key').read_text()==w['cfg'].secret
    restored=SQLiteStorage(directory/'feedback.sqlite',w['cfg'].base_url)
    with restored.transaction() as c:
        encrypted=c.execute('SELECT signing_secret FROM webhook_deliveries').fetchone()[0]
    assert Vault((directory/'application.key').read_text()).reveal(encrypted)==secret
    monkeypatch.setenv('DATABASE_URL','sqlite:///'+str(directory/'feedback.sqlite'))
    monkeypatch.delenv('APPLICATION_SECRET',raising=False)
    assert Config.from_env().secret==w['cfg'].secret
    (directory/'application.key').unlink()
    with pytest.raises(RuntimeError,match='Missing persistent'):Config.from_env()


def test_failed_migration_rolls_back(tmp_path,monkeypatch):
    import feedback.storage.sqlite as module
    root=tmp_path/'adapter'
    migrations=root/'migrations';migrations.mkdir(parents=True)
    (migrations/'001_broken.sql').write_text('CREATE TABLE partial(id TEXT);\nTHIS IS INVALID;\n')
    monkeypatch.setattr(module,'__file__',str(root/'sqlite.py'))
    with pytest.raises(Exception):SQLiteStorage(tmp_path/'failed.sqlite','http://localhost')
    import sqlite3
    c=sqlite3.connect(tmp_path/'failed.sqlite')
    assert not c.execute("SELECT 1 FROM sqlite_master WHERE name='partial'").fetchone()
    c.close()


def test_concurrent_migration_startup(tmp_path):
    path=tmp_path/'concurrent.sqlite'
    with ThreadPoolExecutor(max_workers=4) as pool:
        stores=list(pool.map(lambda _:SQLiteStorage(path,'http://localhost'),range(4)))
    assert all(s.ready() for s in stores)
    with stores[0].transaction() as c:
        assert {row[0] for row in c.execute('SELECT version FROM schema_migrations')} == {p.name for p in Path('feedback/storage/migrations').glob('*.sql')}


def test_acceptance_event_failure_rolls_back(workflow):
    w=workflow;s=w['app'].state.storage
    r=submission(w['user'],w['b'])
    admin=Actor(user_id=s.credentials('admin@example.com')['id'],role='admin')
    original=s._persist_event
    def fail(*a,**kw):
        original(*a,**kw)
        raise RuntimeError('Event persistence failed')
    s._persist_event=fail
    with pytest.raises(RuntimeError):s.moderate_request(r['id'],'accept',None,1,admin)
    s._persist_event=original
    after=s.request(r['id'])
    assert after['moderation']=='pending' and after['accepted_at'] is None and after['version']==1
    assert len(s.history(r['id'],100,0))==1


def test_wrong_encryption_key_fails_startup(workflow):
    from feedback.api import create_app
    from cryptography.fernet import Fernet
    from test_webhooks import configured
    w=workflow;configured(w)
    with pytest.raises(RuntimeError,match='cannot decrypt'):
        create_app(replace(w['cfg'],secret=Fernet.generate_key().decode()))
