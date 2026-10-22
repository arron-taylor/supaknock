import json
from fastapi.testclient import TestClient
from conftest import PASSWORD, submission


def test_setup_control_and_roles(installation):
    app,cfg,admin,user,anon=installation
    assert anon.get('/api/v1/setup').json()['available'] is False
    assert anon.post('/api/v1/setup',json={'email':'evil@example.com','password':PASSWORD,'setup_token':cfg.setup_token}).status_code==409
    assert anon.post('/api/v1/auth/register',json={'email':'evil@example.com','password':PASSWORD,'role':'admin'}).status_code==400
    assert user.get('/api/v1/admin/dashboard').status_code==403
    assert user.post('/api/v1/projects',json={'identifier':'bad','name':'Bad'}).status_code==403
    assert admin.post('/api/v1/admin/accounts',json={'email':'second@example.com','password':PASSWORD}).status_code==201
    assert user.get('/api/v1/auth/me').json()['role']=='participant'
    assert user.get('/api/v1/auth/me').json()['display_name']=='Reader'


def test_csrf_logout_expiry_and_password(installation):
    app,cfg,admin,user,anon=installation
    session_token=user.cookies['feedback_session']
    csrf=user.headers.pop('X-CSRF-Token')
    assert user.post('/api/v1/auth/logout').status_code==403
    user.headers['X-CSRF-Token']=csrf
    user.headers['Origin']='http://evil.example'
    assert user.post('/api/v1/auth/logout').status_code==403
    user.headers['Origin']=cfg.base_url
    assert user.post('/api/v1/auth/password',json={'current_password':PASSWORD,'new_password':'changed-test-password'}).status_code==204
    anon.cookies.set('feedback_session',session_token)
    assert anon.get('/api/v1/auth/me').status_code==401
    anon.cookies.clear()
    assert anon.post('/api/v1/auth/login',json={'email':'user@example.com','password':PASSWORD}).status_code==401
    r=user.post('/api/v1/auth/login',json={'email':'USER@EXAMPLE.COM','password':'changed-test-password'})
    assert r.status_code==200
    user.headers['X-CSRF-Token']=r.json()['csrf_token']
    assert user.post('/api/v1/auth/logout').status_code==204
    assert user.get('/api/v1/auth/me').status_code==401
    with app.state.storage.transaction() as c:
        assert not c.execute('SELECT 1 FROM sessions WHERE token_hash=?',(session_token,)).fetchone()


def test_private_requests_edit_resubmit_accept_lock(workflow):
    w=workflow
    r=submission(w['user'],w['b'])
    rid=r['id']
    assert r['moderation']=='pending' and r['status'] is None
    assert w['anon'].get('/api/v1/requests/'+rid).status_code==404
    assert w['anon'].get('/api/v1/boards/'+w['b']['id']+'/requests').json()['items']==[]
    assert w['user'].get('/api/v1/requests/'+rid).status_code==200
    assert w['user'].put('/api/v1/requests/'+rid+'/vote',json={'present':True}).status_code==404
    r=w['admin'].post('/api/v1/requests/'+rid+'/reject',json={'version':1,'reason':'Clarify the use case'}).json()
    assert r['moderation']=='rejected'
    r=w['user'].patch('/api/v1/requests/'+rid,json={'title':'Improved idea','description':'New details','version':r['version']}).json()
    assert r['moderation']=='pending' and r['rejection_reason'] is None
    history=w['user'].get('/api/v1/requests/'+rid+'/history').json()['items']
    assert history[1]['request']['rejection_reason']=='Clarify the use case'
    assert history[2]['type']=='request.resubmitted'
    r=w['admin'].post('/api/v1/requests/'+rid+'/accept',json={'version':r['version']}).json()
    assert r['status']=='under_consideration' and r['vote_count']==0
    accepted=r['accepted_at']
    assert w['user'].patch('/api/v1/requests/'+rid,json={'title':'No','description':'','version':r['version']}).status_code==409
    assert w['user'].delete('/api/v1/requests/'+rid+'?version='+str(r['version'])).status_code==409
    assert w['admin'].post('/api/v1/requests/'+rid+'/reject',json={'version':r['version']}).status_code==409
    for state in ['planned','in_progress','shipped','under_consideration']:
        r=w['admin'].put('/api/v1/requests/'+rid+'/status',json={'status':state,'version':r['version']}).json()
        assert r['accepted_at']==accepted
    before=len(w['admin'].get('/api/v1/requests/'+rid+'/history').json()['items'])
    again=w['admin'].put('/api/v1/requests/'+rid+'/status',json={'status':r['status'],'version':r['version']}).json()
    assert again['version']==r['version']
    assert len(w['admin'].get('/api/v1/requests/'+rid+'/history').json()['items'])==before
    public=w['anon'].get('/api/v1/requests/'+rid).json()
    assert 'creator_id' not in public and 'accepted_by' not in public and 'rejection_reason' not in public
    history=w['anon'].get('/api/v1/requests/'+rid+'/history').json()['items']
    assert all('creator' not in x and 'actor' not in x for x in history)
    assert {e['type'] for e in history}=={'request.accepted','request.status_changed'}


def test_native_ownership_and_spoofing(workflow):
    w=workflow
    r=submission(w['user'],w['b'])
    assert w['admin'].patch('/api/v1/requests/'+r['id'],json={'title':'Bad','version':1}).status_code==404
    assert w['user'].post('/api/v1/boards/'+w['b']['id']+'/requests',json={'title':'Bad','user_id':'someone-else'}).status_code==400
    assert w['user'].post('/api/v1/boards/'+w['b']['id']+'/requests',json={'title':'Bad','external_user_id':'spoof'}).status_code==400
    assert w['user'].delete('/api/v1/requests/'+r['id']+'?version=1').status_code==204
    assert w['user'].get('/api/v1/requests/'+r['id']).status_code==404
    with w['app'].state.storage.transaction() as c:
        assert c.execute('SELECT count(*) FROM request_events WHERE request_id=?',(r['id'],)).fetchone()[0]==2


def test_external_identity_scope_and_permissions(workflow):
    w=workflow
    r=submission(w['trusted'],w['b'],' 123 AbC ')
    q=submission(w['other'],w['qb'],' 123 AbC ')
    s=w['app'].state.storage
    with s.transaction() as c:
        ids=c.execute('SELECT * FROM external_identities').fetchall()
        assert len(ids)==2 and ids[0]['user_id']!=ids[1]['user_id']
        assert all(s._decode_external_id(x['external_user_id'])==' 123 AbC ' for x in ids)
        assert c.execute('SELECT count(*) FROM local_credentials').fetchone()[0]==2
    assert w['trusted'].get('/api/v1/projects/'+w['q']['id']).status_code==404
    assert w['trusted'].post('/api/v1/boards/'+w['qb']['id']+'/requests',json={'title':'No','external_user_id':'123'}).status_code==404
    assert w['trusted'].get('/api/v1/requests/'+q['id'],params={'external_user_id':' 123 AbC '}).status_code==404
    assert w['trusted'].get('/api/v1/requests/'+r['id']).status_code==404
    assert w['trusted'].get('/api/v1/requests/'+r['id'],params={'external_user_id':' 123 AbC '}).status_code==200
    assert w['trusted'].get('/api/v1/me/requests',params={'external_user_id':' 123 AbC '}).json()['items'][0]['id']==r['id']
    assert w['trusted'].get('/api/v1/me/requests',params={'external_user_id':'123 abc'}).json()['items']==[]
    assert w['trusted'].post('/api/v1/requests/'+r['id']+'/accept',json={'version':1}).status_code==403
    assert w['manager'].post('/api/v1/requests/'+r['id']+'/accept',json={'version':1}).status_code==200
    assert w['manager'].post('/api/v1/projects/'+w['p']['id']+'/keys',json={'name':'No','permissions':['manage']}).status_code==403
    assert w['trusted'].get('/api/v1/projects').json()['items'][0]['id']==w['p']['id']
    readonly,_=w['key'](w['p'],['read'])
    assert readonly.post('/api/v1/boards/'+w['b']['id']+'/requests',json={'title':'No','external_user_id':'x'}).status_code==403
    assert readonly.get('/api/v1/requests/'+r['id']+'/audience').status_code==403
    assert w['admin'].delete('/api/v1/projects/'+w['p']['id']+'/keys/'+w['k']['id']).status_code==204
    assert w['trusted'].get('/api/v1/projects').status_code==401


def test_idempotent_votes_audience_and_versions(workflow):
    w=workflow
    r=submission(w['trusted'],w['b'],'creator')
    rid=r['id']
    assert w['manager'].put('/api/v1/requests/'+rid+'/status',json={'version':1,'status':'planned'}).status_code==409
    assert w['manager'].post('/api/v1/requests/'+rid+'/accept',json={'version':1}).status_code==200
    for _ in range(3):
        r=w['trusted'].put('/api/v1/requests/'+rid+'/vote',json={'present':True,'external_user_id':'creator'})
        assert r.json()=={'vote_count':1,'voted':True}
    w['trusted'].put('/api/v1/requests/'+rid+'/vote',json={'present':True,'external_user_id':'voter'})
    w['user'].put('/api/v1/requests/'+rid+'/vote',json={'present':True})
    audience=w['trusted'].get('/api/v1/requests/'+rid+'/audience').json()['items']
    assert len(audience)==3 and sum(x['creator'] for x in audience)==1
    assert {x['external_user_id'] for x in audience}=={'creator','voter',None}
    for _ in range(3):
        r=w['trusted'].put('/api/v1/requests/'+rid+'/vote',json={'present':False,'external_user_id':'voter'})
        assert r.json()=={'vote_count':2,'voted':False}
    assert w['anon'].get('/api/v1/requests/'+rid+'/audience').status_code==403
    assert w['user'].put('/api/v1/requests/'+rid+'/status',json={'version':2,'status':'shipped'}).status_code==403
    assert w['manager'].patch('/api/v1/requests/'+rid+'/content',json={'title':'Admin title','description':'','version':1}).status_code==409
    assert w['manager'].patch('/api/v1/requests/'+rid+'/content',json={'title':'Admin title','description':'','version':2}).status_code==200


def test_validation_secrets_and_pagination(workflow):
    w=workflow
    assert w['user'].post('/api/v1/boards/'+w['b']['id']+'/requests',json={'title':' '*3}).status_code==400
    assert w['user'].post('/api/v1/boards/'+w['b']['id']+'/requests',json={'title':'x'*201}).status_code==400
    assert w['anon'].get('/api/v1/projects?limit=101').status_code==400
    page=w['admin'].get('/api/v1/projects?limit=1').json()
    assert len(page['items'])==1 and page['has_more']
    assert w['admin'].get('/api/v1/projects?limit=1&offset=1').json()['has_more'] is False
    public=w['anon'].get('/api/v1/projects/'+w['p']['id']).text
    assert 'webhook' not in public and 'secret' not in public
    keys=w['admin'].get('/api/v1/projects/'+w['p']['id']+'/keys').text
    assert w['k']['secret'] not in keys and 'secret_hash' not in keys
    invalid=w['anon'].post('/api/v1/auth/register',json={'email':'abc','password':'TOPSECRET'}).text
    assert 'TOPSECRET' not in invalid
    r=w['anon'].post('/api/v1/auth/login',content=b'x'*100000)
    assert r.status_code==413


def test_rate_limits(tmp_path):
    from feedback.config import Config
    from feedback.api import create_app
    from cryptography.fernet import Fernet
    cfg=Config(tmp_path/'db.sqlite',Fernet.generate_key().decode(),'s'*32,worker=False,auth_rate_limit=2)
    c=TestClient(create_app(cfg))
    for _ in range(2):
        assert c.post('/api/v1/auth/login',json={'email':'absent@ex.com','password':'bad'}).status_code==401
    assert c.post('/api/v1/auth/login',json={'email':'absent@ex.com','password':'bad'}).status_code==429


def test_public_history_pagination_and_expired_cookies(workflow):
    import time
    w=workflow;s=w['app'].state.storage
    r=submission(w['user'],w['b'])
    for i in range(3):
        r=w['user'].patch('/api/v1/requests/'+r['id'],json={'title':f'Idea {i}','description':'','version':r['version']}).json()
    r=w['admin'].post('/api/v1/requests/'+r['id']+'/accept',json={'version':r['version']}).json()
    r=w['admin'].put('/api/v1/requests/'+r['id']+'/status',json={'status':'shipped','version':r['version']}).json()
    history=w['anon'].get('/api/v1/requests/'+r['id']+'/history?limit=1').json()
    assert history['items'][0]['type']=='request.accepted' and history['has_more']
    assert w['anon'].get('/api/v1/requests/'+r['id']+'/history?limit=1&offset=1').json()['items'][0]['type']=='request.status_changed'
    with s.transaction(True) as c:c.execute('UPDATE sessions SET expires_at=?',(time.time()-1,))
    assert w['user'].get('/api/v1/auth/me').status_code==401
    assert w['user'].get('/api/v1/requests/'+r['id']).status_code==200
    assert w['user'].put('/api/v1/requests/'+r['id']+'/vote',json={'present':True}).status_code==401


def test_openapi_matches_actual_errors_and_private_filtering(workflow):
    w=workflow
    spec=w['anon'].get('/api/v1/openapi.json').json()
    op=spec['paths']['/api/v1/boards/{board}/requests']['post']
    assert '422' not in op['responses'] and '400' in op['responses']
    assert op['responses']['201']['content']['application/json']['schema']['$ref'].endswith('RequestOutput')
    assert 'participate' in op['description']
    assert w['anon'].get('/api/v1/missing').json()['error']['code']=='not_found'
    assert w['anon'].post('/api/v1/projects/absent').json()['error']['code']=='method_not_allowed'
    # Unversioned UI entrypoints/assets must pick up container upgrades without
    # relying on a hard reload; API data still uses its stricter no-store policy.
    for path in ('/login', '/register', '/setup', '/chat-admin.js', '/widget.js',
                 '/assets/app.js', '/assets/shell.js', '/assets/style.css', '/assets/chat.css',
                 '/projects/' + w['p']['id'], '/boards/' + w['b']['id'], '/requests/request-id'):
        response = w['anon'].get(path)
        assert response.status_code == 200
        assert response.headers['Cache-Control'] == 'no-cache'
    for path in ('/workspace', '/my-requests', '/account', '/admin', '/admin/ai', '/admin/ai/providers', '/admin/push',
                 '/admin/account-activity', '/admin/app-activity', '/admin/projects/' + w['p']['id'], '/admin/chat'):
        response = w['anon'].get(path, follow_redirects=False)
        assert response.status_code == 303
        assert response.headers['location'] == '/login?return=' + path.replace('/', '%2F')
    assert w['admin'].get('/workspace', follow_redirects=False).status_code == 200
    member_home = w['user'].get('/workspace', follow_redirects=False)
    assert member_home.status_code == 303 and member_home.headers['location'] == '/my-requests'
    assert w['user'].get('/admin/chat', follow_redirects=False).status_code == 200
    for path in ('/welcome', '/iphone', '/examples', '/api-docs', '/assets/landing.css',
                 '/assets/api-docs.js', '/assets/showcase/joypass-chat.jpg', '/assets/config.py'):
        assert w['anon'].get(path).status_code == 404
    entry = w['anon'].get('/', follow_redirects=False)
    assert entry.status_code == 303 and entry.headers['location'] == '/login'
    assert '/assets/app.js' in w['anon'].get('/login').text
    assert w['anon'].get('/widget.js').headers['Access-Control-Allow-Origin'] == '*'
    assert w['anon'].get('/api/v1/projects').headers['Cache-Control'] == 'no-store'


def test_external_creator_full_workflow_and_eligible_delete(workflow):
    w=workflow
    r=submission(w['trusted'],w['b'],'backend-account-001')
    edited=w['trusted'].patch('/api/v1/requests/'+r['id'],json={
        'title':'Refined from the backend','description':'The creator can update this pending idea.',
        'version':1,'external_user_id':'backend-account-001'})
    assert edited.status_code==200
    assert w['trusted'].patch('/api/v1/requests/'+r['id'],json={
        'title':'Wrong owner','version':2,'external_user_id':'backend-account-002'}).status_code==404
    assert w['trusted'].delete('/api/v1/requests/'+r['id'],params={
        'version':2,'external_user_id':'backend-account-001'}).status_code==204
    assert w['trusted'].get('/api/v1/me/requests',params={
        'external_user_id':'backend-account-001'}).json()['items']==[]
    accepted=submission(w['trusted'],w['b'],'backend-account-001')
    accepted=w['manager'].post('/api/v1/requests/'+accepted['id']+'/accept',json={'version':1}).json()
    assert w['trusted'].patch('/api/v1/requests/'+accepted['id'],json={
        'title':'Locked','version':accepted['version'],'external_user_id':'backend-account-001'}).status_code==409


def test_credential_and_board_lists_are_bounded(workflow):
    w=workflow
    first=w['admin'].get('/api/v1/projects/'+w['p']['id']+'/keys?limit=1').json()
    assert len(first['items'])==1 and first['has_more']
    second=w['admin'].get('/api/v1/projects/'+w['p']['id']+'/keys?limit=1&offset=1').json()
    assert len(second['items'])==1 and second['items'][0]['id']!=first['items'][0]['id']
    assert w['admin'].get('/api/v1/projects/'+w['p']['id']+'/keys?limit=101').status_code==400
    assert w['admin'].post('/api/v1/projects/'+w['p']['id']+'/boards',json={'identifier':'integrations','name':'Integrations'}).status_code==201
    assert w['anon'].get('/api/v1/projects/'+w['p']['id']+'/boards?limit=1').json()['has_more']
    assert w['anon'].get('/api/v1/projects/'+w['p']['id']+'/boards?limit=1&offset=1').json()['items'][0]['identifier']=='integrations'


def test_cross_origin_anonymous_reads_without_private_credentials(workflow):
    w=workflow
    origin='https://glean.example'
    response=w['anon'].get('/api/v1/projects/'+w['p']['id'],headers={'Origin':origin})
    assert response.status_code==200
    assert response.headers['Access-Control-Allow-Origin']=='*'
    preflight={'Origin':origin,'Access-Control-Request-Method':'GET','Access-Control-Request-Headers':'Content-Type'}
    response=w['anon'].options('/api/v1/projects',headers=preflight)
    assert response.status_code==204 and response.headers['Access-Control-Allow-Origin']=='*'
    assert 'Access-Control-Allow-Credentials' not in response.headers
    preflight['Access-Control-Request-Method']='POST'
    response=w['anon'].options('/api/v1/boards/'+w['b']['id']+'/requests',headers=preflight)
    assert response.status_code==400 and response.json()['error']['code']=='cors_denied'
    preflight['Access-Control-Request-Method']='GET'
    preflight['Access-Control-Request-Headers']='Authorization'
    assert w['anon'].options('/api/v1/projects',headers=preflight).status_code==400
    response=w['user'].get('/api/v1/me/requests',headers={'Origin':origin})
    assert response.status_code==200 and 'Access-Control-Allow-Origin' not in response.headers
