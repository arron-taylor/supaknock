"""Board privacy preserves intake, ownership and trusted backend integrations."""
import pytest
from fastapi.testclient import TestClient
from conftest import PASSWORD, submission, reopen_storage
from feedback.domain import Actor
from feedback.errors import Error
from test_ai_reports_storage import reports, draft, confirm
from test_ai_chat_isolation import ai_chat


def private(w):
    b = w['admin'].get('/api/v1/boards/' + w['b']['id']).json()
    response = w['admin'].patch('/api/v1/boards/' + b['id'], json={
        'version': b['version'], 'name': b['name'], 'description': b['description'], 'visibility': 'private'})
    assert response.status_code == 200, response.text
    return response.json()


def accept(w, r):
    response = w['admin'].post('/api/v1/requests/' + r['id'] + '/accept', json={'version': r['version']})
    assert response.status_code == 200, response.text
    return response.json()


def test_defaults_validation_legacy_edits_and_versions(workflow):
    w = workflow; b = w['b']; path = '/api/v1/boards/' + b['id']
    assert b['visibility'] == 'public'
    for bad in (None, True, 'unlisted'):
        assert w['admin'].patch(path, json={'name':'Features','version':1,'visibility':bad}).status_code == 400
    for role in ('anon', 'user', 'trusted', 'other'):
        assert w[role].patch(path, json={'name':'Features','version':1,'visibility':'private'}).status_code in (401,403,404)
    saved = private(w)
    assert w['admin'].patch(path,json={'name':'Old','version':1,'visibility':'public'}).status_code == 409
    legacy = w['admin'].patch(path,json={'name':'Renamed','version':saved['version']})
    assert legacy.status_code == 200 and legacy.json()['visibility'] == 'private'
    created = w['admin'].post('/api/v1/projects/'+w['p']['id']+'/boards', json={'identifier':'secret','name':'Secret','visibility':'private'})
    assert created.status_code == 201 and created.json()['visibility'] == 'private'
    assert reopen_storage(w['app'].state.storage).board(b['id'])['visibility'] == 'private'


def test_private_board_discovery_filters_before_pagination(workflow):
    w = workflow; private(w)
    path = '/api/v1/projects/' + w['p']['id'] + '/boards'
    public = w['admin'].post(path,json={'identifier':'public','name':'Public'}).json()
    for role in ('anon','user'):
        page = w[role].get(path+'?limit=1').json()
        assert [b['id'] for b in page['items']] == [public['id']]
        assert page['has_more'] is False
    for role in ('admin','trusted','manager'):
        assert len(w[role].get(path).json()['items']) == 2
    assert w['other'].get(path).status_code == 404
    assert w['anon'].get('/api/v1/boards/'+w['b']['id']).status_code == 404


def test_owner_reads_and_intake_survive_while_other_people_cannot_read_or_vote(workflow):
    w = workflow; b = private(w); base='/api/v1/boards/'+b['id']
    assert w['user'].get(base).status_code == 200
    mine = accept(w, submission(w['user'],b))
    other = accept(w, submission(w['trusted'],b,'external-person'))
    pending = submission(w['user'],b)
    assert w['user'].get(base+'/requests').json()['items'][0]['id'] == mine['id']
    assert len(w['user'].get(base+'/requests').json()['items']) == 1
    assert len(w['admin'].get(base+'/requests').json()['items']) == 2
    assert len(w['user'].get('/api/v1/me/requests').json()['items']) == 2
    assert w['user'].get('/api/v1/requests/'+mine['id']).status_code == 200
    for role, r in [('anon',mine),('user',other)]:
        path='/api/v1/requests/'+r['id']
        for suffix in ('','/history','/vote','/audience'):
            assert w[role].get(path+suffix).status_code in (401,404)
        assert w[role].put(path+'/vote',json={'present':True}).status_code in (401,404)
    assert w['user'].get('/api/v1/requests/'+mine['id']+'/history').status_code == 200
    # Storage repeats privacy checks, even if a caller observed the old public state.
    uid=w['user'].get('/api/v1/auth/me').json()['id']
    with pytest.raises(Error) as exc:
        w['app'].state.storage.set_vote(other['id'],True,Actor(user_id=uid,role='participant'))
    assert exc.value.status == 404
    edited=w['user'].patch('/api/v1/requests/'+pending['id'],json={'title':'Updated','version':pending['version']})
    assert edited.status_code == 200


def test_backend_intake_moderation_and_webhook_snapshots_still_work(workflow):
    w=workflow; b=private(w); p=w['p']
    hook=w['admin'].put('/api/v1/projects/'+p['id']+'/webhook',json={'version':p['version'],'url':'https://example.com/hooks','enabled':True,'events':['request.created','request.accepted']})
    assert hook.status_code == 200, hook.text
    r=submission(w['trusted'],b,' exact id ')
    assert w['trusted'].get('/api/v1/requests/'+r['id'],params={'external_user_id':' exact id '}).status_code == 200
    assert any(x['id']==r['id'] for x in w['manager'].get('/api/v1/moderation').json()['items'])
    accepted=accept(w,r)
    assert w['trusted'].get('/api/v1/requests/'+r['id']).status_code == 200
    aud_only,_=w['key'](p,['audience:read'])
    assert aud_only.get('/api/v1/requests/'+r['id']+'/audience').status_code == 200
    assert aud_only.get('/api/v1/requests/'+r['id']).status_code == 403
    read_only,_=w['key'](p,['read'])
    assert read_only.get('/api/v1/requests/'+r['id']+'/audience').status_code == 403
    other_aud,_=w['key'](w['q'],['audience:read'])
    assert other_aud.get('/api/v1/requests/'+r['id']+'/audience').status_code == 404
    assert w['other'].get('/api/v1/requests/'+r['id']).status_code == 404
    history=w['admin'].get('/api/v1/requests/'+r['id']+'/history').json()
    current=w['admin'].get('/api/v1/boards/'+b['id']).json()
    public=w['admin'].patch('/api/v1/boards/'+b['id'],json={'name':b['name'],'version':current['version'],'visibility':'public'})
    assert public.status_code == 200
    assert w['anon'].get('/api/v1/requests/'+r['id']).status_code == 200
    assert w['admin'].get('/api/v1/requests/'+r['id']+'/history').json() == history
    assert w['admin'].get('/api/v1/requests/'+r['id']).json()['accepted_at'] == accepted['accepted_at']
    with w['app'].state.storage.transaction() as c:
        assert c.execute('SELECT count(*) FROM webhook_deliveries WHERE event_id IN (SELECT id FROM request_events WHERE request_id=?)',(r['id'],)).fetchone()[0] == 2


@pytest.mark.parametrize('kind', ['visitor','external','native'])
def test_private_board_chat_reports_keep_confirmation_and_owner_tracking(reports,kind):
    env=reports; s=env['store']; b=env['board']
    s.update_board(b['id'],{'name':b['name'],'description':'','version':b['version'],'visibility':'private'})
    customer,_,_,proposal=draft(env,kind)
    result=confirm(env,customer,proposal)
    r=s.request(result['request_id'])
    s.moderate_request(r['id'],'accept',None,r['version'],Actor(user_id=env['admin']['user_id'],role='admin'))
    again=confirm(env,customer,proposal)
    assert again['moderation']=='accepted' and again['public_url'] is None


def test_visibility_mutation_rechecks_session_key_and_version(workflow):
    from feedback.security import digest
    from concurrent.futures import ThreadPoolExecutor
    w=workflow; s=w['app'].state.storage; b=w['b']
    session=s.session(digest(w['admin'].cookies['feedback_session']))
    actor=Actor(user_id=session['id'],role='admin')
    data={'name':b['name'],'version':b['version'],'visibility':'private'}
    def update(_):
        try: return s.update_board(b['id'],data,actor=actor,session_id=session['session_id'])['version']
        except Error as e: return e.status
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(update,range(2))) == [2,409]
    s.revoke_session(session['session_id'])
    with pytest.raises(Error) as exc:
        s.update_board(b['id'],dict(data,version=2,visibility='public'),actor=actor,session_id=session['session_id'])
    assert exc.value.status in (401,403)
    key=s.api_key(digest(w['manager'].headers['Authorization'][7:])); s.revoke_api_key(w['p']['id'],key['id'])
    with pytest.raises(Error) as exc:
        s.update_board(b['id'],dict(data,version=2),actor=Actor(key_id=key['id'],project_id=w['p']['id'],permissions=('manage',)))
    assert exc.value.status == 403
    assert s.board(b['id'])['visibility']=='private'


def test_existing_board_upgrade_preserves_content_and_defaults_public(workflow):
    w=workflow; s=w['app'].state.storage; r=submission(w['user'],w['b'])
    migration='026_board_visibility.sql' if w['cfg'].database_provider=='sqlite' else '006_board_visibility.sql'
    with s.transaction(True) as c:
        c.execute('ALTER TABLE boards DROP COLUMN visibility')
        c.execute('DELETE FROM schema_migrations WHERE version=?',(migration,))
    restored=reopen_storage(s)
    assert restored.board(w['b']['id'])['visibility']=='public'
    assert restored.request(r['id'])['description']==r['description']
    assert len(restored.history(r['id'],10,0))==1


def test_private_board_archive_restore_keeps_visibility(workflow):
    w=workflow; b=private(w); path='/api/v1/boards/'+b['id']+'/archive'
    archived=w['admin'].put(path,json={'version':b['version'],'archived':True}).json()
    restored=w['admin'].put(path,json={'version':archived['version'],'archived':False}).json()
    assert restored['visibility']=='private'
    assert w['anon'].get('/api/v1/boards/'+b['id']).status_code==404
    assert submission(w['trusted'],b,'after-restore')['moderation']=='pending'
