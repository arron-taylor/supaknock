import time
import pytest
from feedback.storage.sqlite import SQLiteStorage
from feedback.errors import Error
from feedback.security import Vault
from cryptography.fernet import Fernet

@pytest.fixture
def store(tmp_path):
    s=SQLiteStorage(tmp_path/'app.sqlite','https://box.example')
    a=s.create_native_account('a@example.com','hash','Owner','admin',True)
    s.create_session(a['id'],'token','csrf',time.time()+3600)
    p={'kind':'admin','user_id':a['id'],'session_id':s.session('token')['session_id']}
    projects=[s.create_project({'identifier':x,'name':x,'description':''},a['id'])['id'] for x in ('one','two')]
    return s,p,projects

def saved(s,p,pid):
    v=Vault(Fernet.generate_key().decode())
    encrypted=v.protect('sentinel-remote-token')
    r=s.connected_change(pid,p,0,'save',{'base_url':'https://app.example/connect','protected_token':encrypted})
    assert not r['enabled'] and r['has_token'] and 'protected_token' not in r
    assert encrypted in s.signing_secrets()
    return r

def test_lifecycle_and_identity_binding(store):
    s,p,(one,two)=store
    a=saved(s,p,one)
    a=s.connected_change(one,p,a['version'],'enable',dict(app_instance_id=' Production ',link_origins=[],support_linked=True))
    assert a['enabled']
    s.connected_snapshot(one,p,a['version'])
    a=s.connected_change(one,p,a['version'],'remove')
    assert not a['has_token'] and a['app_instance_id']==' Production '
    with pytest.raises(Error):s.connected_snapshot(one,p)
    b=saved(s,p,two)
    with pytest.raises(Error):s.connected_change(two,p,b['version'],'enable',dict(app_instance_id=' Production ',link_origins=[],support_linked=False))
    with pytest.raises(Error):s.connected_change(one,p,0,'save',{})

def test_revocation_and_late_work(store):
    s,p,(one,_)=store
    r=saved(s,p,one)
    snap=s.connected_snapshot(one,p,enabled=False)
    r=s.connected_change(one,p,r['version'],'save',{'base_url':'https://app.example/connect'})
    with pytest.raises(Error):s.connected_snapshot(one,p,snap['version'],enabled=False)
    with pytest.raises(Error):s.connected_change(one,p,r['version'],'save',{'base_url':'https://different.example'})
    s.revoke_session(p['session_id'])
    for call in (lambda:s.connected_list(p),lambda:s.connected_read(one,p),lambda:s.connected_change(one,p,r['version'],'remove')):
        with pytest.raises(Error):call()

def test_empty_relationship_is_read_only_and_opt_in(store):
    s,p,(one,_)=store
    r=saved(s,p,one)
    r=s.connected_change(one,p,r['version'],'enable',dict(app_instance_id='one',link_origins=[],support_linked=False))
    assert not s.connected_related(one,p,' Person ',r['version'],25,0)['linked']
    r=s.connected_change(one,p,r['version'],'enable',dict(app_instance_id='one',link_origins=[],support_linked=True))
    assert s.connected_related(one,p,' Person ',r['version'],25,0)==dict(linked=True,requests=[],conversations=[],has_more=False)
    assert s.connected_preferences(p)['timezone']=='UTC'
    assert s.connected_preferences(p,1,'America/Chicago')['version']==2
    with pytest.raises(Error):s.connected_preferences(p,1,'UTC')
