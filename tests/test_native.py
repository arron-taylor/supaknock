import time
from fastapi.testclient import TestClient
from conftest import PASSWORD
from feedback.security import digest


def native(app):
    client = TestClient(app)
    response = client.post('/api/v1/auth/native/login', json={'email':'admin@example.com','password':PASSWORD})
    assert response.status_code == 200, response.text
    assert not response.cookies
    client.headers['Authorization'] = 'Bearer ' + response.json()['token']
    return client, response.json()


def test_native_admin_session_transport_and_isolation(workflow):
    w = workflow
    c, data = native(w['app'])
    assert data['token'].startswith('ns_')
    assert c.get('/api/v1/auth/me').json()['role'] == 'admin'
    assert c.get('/api/v1/admin/dashboard').status_code == 200
    assert c.post('/api/v1/projects',json={'identifier':'native','name':'Native'}).status_code == 201
    assert w['manager'].get('/api/v1/admin/dashboard').status_code == 403
    assert w['anon'].post('/api/v1/auth/native/login',json={'email':'user@example.com','password':PASSWORD}).status_code == 401
    assert w['anon'].post('/api/v1/auth/native/login',json={'email':'missing@example.com','password':PASSWORD}).status_code == 401
    raw = data['token']
    with w['app'].state.storage.transaction() as db:
        assert not db.execute('SELECT 1 FROM sessions WHERE token_hash=?',(raw,)).fetchone()
        assert db.execute('SELECT transport FROM sessions WHERE token_hash=?',(digest(raw),)).fetchone()[0] == 'native'
    c.cookies.set('feedback_session',w['admin'].cookies['feedback_session'])
    assert c.get('/api/v1/auth/me').status_code == 400
    c.cookies.clear()
    forged = TestClient(w['app'])
    forged.cookies.set('feedback_session',raw)
    assert forged.get('/api/v1/auth/me').status_code == 401
    assert c.post('/api/v1/auth/logout').status_code == 204
    assert c.get('/api/v1/auth/me').status_code == 401


def test_native_expiration_password_and_instance_persistence(installation):
    app,cfg,admin,user,anon = installation
    c, first = native(app)
    other, second = native(app)
    assert first['installation_id'] == second['installation_id']
    with app.state.storage.transaction(True) as db:
        db.execute('UPDATE sessions SET expires_at=? WHERE token_hash=?',(time.time()-1,digest(first['token'])))
    assert c.get('/api/v1/auth/me').status_code == 401
    assert other.post('/api/v1/auth/password',json={'current_password':PASSWORD,'new_password':'another-long-password'}).status_code == 204
    assert other.get('/api/v1/auth/me').status_code == 401
    assert admin.get('/api/v1/auth/me').status_code == 401
    from feedback.storage import open_storage
    assert open_storage(cfg).installation_id() == first['installation_id']
