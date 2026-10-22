"""Shared administrator acknowledgements are durable installation state."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from feedback.api import create_app
from feedback.maintenance import backup
from conftest import PASSWORD, submission
from test_chat import enable, visitor, send

BASE = '/api/v1/admin/notifications'


def second_admin(w, native=False):
    email = 'second-admin@example.com'
    response = w['admin'].post('/api/v1/admin/accounts', json={
        'email': email, 'password': PASSWORD, 'display_name': 'Second admin',
    })
    assert response.status_code == 201, response.text
    client = TestClient(w['app'])
    path = '/api/v1/auth/native/login' if native else '/api/v1/auth/login'
    result = client.post(path, json={'email': email, 'password': PASSWORD})
    assert result.status_code == 200, result.text
    if native:
        client.headers['Authorization'] = 'Bearer ' + result.json()['token']
    else:
        client.headers.update({'Origin': w['cfg'].base_url, 'X-CSRF-Token': result.json()['csrf_token']})
    return client


def items(client):
    response = client.get(BASE)
    assert response.status_code == 200, response.text
    return response.json()['items']


def test_shared_read_state_between_admins_cookie_and_native(workflow):
    w = workflow
    other = second_admin(w, native=True)
    request = submission(w['user'], w['b'])
    notification = items(w['admin'])[0]
    assert notification['type'] == 'request.created'
    assert notification['resource_id'] == request['id']
    assert notification['resource_kind'] == 'request'
    assert notification['resource_available'] is True
    assert notification['url'] == '/requests/' + request['id']
    assert notification['title'] == request['title']
    assert notification['project_name'] == w['p']['name']
    assert notification['read_at'] is None
    # Listing is not acknowledgement, on either transport.
    assert other.get(BASE + '/summary').json()['unread_count'] == 1
    assert items(other)[0]['read_at'] is None
    path = BASE + '/' + notification['id'] + '/read'
    marked = other.put(path, json={'read': True})
    assert marked.status_code == 200, marked.text
    timestamp = marked.json()['read_at']
    assert timestamp
    assert w['admin'].get(BASE + '/summary').json()['unread_count'] == 0
    assert items(w['admin'])[0]['read_at'] == timestamp
    assert w['admin'].put(path, json={'read': True}).json()['read_at'] == timestamp
    assert w['admin'].put(path, json={'read': False}).json()['read_at'] is None
    assert other.get(BASE + '/summary').json()['unread_count'] == 1
    assert other.post('/api/v1/auth/logout').status_code == 204
    assert other.put(path, json={'read': True}).status_code == 401


def test_notifications_are_admin_only_and_cookie_mutations_require_csrf(workflow):
    w = workflow
    submission(w['user'], w['b'])
    record = items(w['admin'])[0]
    path = BASE + '/' + record['id'] + '/read'
    for client in (w['anon'], w['user'], w['trusted'], w['manager'], w['other']):
        denied = 401 if client is w['anon'] else 403
        assert client.get(BASE).status_code == denied
        assert client.get(BASE + '/summary').status_code == denied
        assert client.put(path, json={'read': True}).status_code == denied
        assert client.post(BASE + '/read-all', json={'through_sequence': record['sequence']}).status_code == denied
    unsafe = TestClient(w['app'])
    unsafe.cookies.update(w['admin'].cookies)
    assert unsafe.put(path, json={'read': True}).status_code == 403
    assert unsafe.post(BASE + '/read-all', json={'through_sequence': record['sequence']}).status_code == 403
    assert w['admin'].get(BASE + '/summary').json()['unread_count'] == 1
    assert w['admin'].put(BASE + '/' + str(uuid4()) + '/read', json={'read': True}).status_code == 404
    for query in ('?limit=0', '?limit=101', '?offset=-1'):
        assert w['admin'].get(BASE + query).status_code == 400
    assert w['admin'].post(BASE + '/read-all', json={'through_sequence': -1}).status_code == 400


def test_read_all_watermark_keeps_later_arrivals_unread_and_shared(workflow):
    w = workflow
    other = second_admin(w)
    first = submission(w['user'], w['b'])
    snapshot = w['admin'].get(BASE).json()
    second = submission(w['user'], w['qb'])
    result = other.post(BASE + '/read-all', json={'through_sequence': snapshot['through_sequence']})
    assert result.status_code == 200, result.text
    assert result.json()['unread_count'] == 1
    unread = w['admin'].get(BASE + '?unread_only=true').json()
    assert [n['resource_id'] for n in unread['items']] == [second['id']]
    assert unread['through_sequence'] > snapshot['through_sequence']
    assert len(items(w['admin'])) == 2
    assert next(n for n in items(w['admin']) if n['resource_id'] == first['id'])['read_at']
    assert other.post(BASE + '/read-all', json={'through_sequence': snapshot['through_sequence']}).json()['unread_count'] == 1
    assert other.post(BASE + '/read-all', json={'through_sequence': unread['through_sequence']}).json()['unread_count'] == 0


def test_event_sources_message_retries_and_private_payloads(workflow):
    w = workflow
    request = submission(w['user'], w['b'])
    rejected = w['admin'].post('/api/v1/requests/' + request['id'] + '/reject', json={'version': request['version'], 'reason': 'Refine this idea'}).json()
    updated = w['user'].patch('/api/v1/requests/' + request['id'], json={'version': rejected['version'], 'title': 'Refined idea', 'description': 'Updated details'})
    assert updated.status_code == 200, updated.text
    assert {n['type'] for n in items(w['admin'])} == {'request.created', 'request.resubmitted'}
    enable(w)
    client, conversation, token = visitor(w)
    message_id = str(uuid4())
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: send(client, conversation, 'Private message not for notification copy', message_id), range(6)))
    assert all(r.status_code == 201 for r in results)
    assert len({r.json()['id'] for r in results}) == 1
    assert send(w['admin'], conversation, 'Private administrator reply').status_code == 201
    notifications = items(w['admin'])
    chat = [n for n in notifications if n['resource_kind'] == 'conversation']
    assert len(chat) == 1
    assert chat[0]['resource_id'] == conversation['id']
    assert chat[0]['type'] == 'chat.message_created'
    assert chat[0]['url'] == '/admin/chat?conversation=' + conversation['id']
    body = w['admin'].get(BASE).text
    for private in (token, PASSWORD, w['k']['secret'], 'Private message not for notification copy', 'Private administrator reply', 'external_user_id', 'csrf_token', 'password_hash'):
        assert private not in body
    # Team acknowledgement is separate from each administrator's chat read position.
    assert w['admin'].post('/api/v1/chat/conversations/' + conversation['id'] + '/read', json={'sequence': 2}).status_code == 200
    assert next(n for n in items(w['admin']) if n['id'] == chat[0]['id'])['read_at'] is None


def test_notification_and_business_event_commit_or_rollback_together(workflow, monkeypatch):
    w = workflow
    storage = w['app'].state.storage
    original = storage._persist_event
    def fail_after_event(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError('Injected transaction failure')
    monkeypatch.setattr(storage, '_persist_event', fail_after_event)
    with pytest.raises(RuntimeError, match='Injected transaction failure'):
        submission(w['user'], w['b'])
    assert items(w['admin']) == []
    with storage.transaction() as connection:
        assert connection.execute('SELECT count(*) FROM requests').fetchone()[0] == 0
        assert connection.execute('SELECT count(*) FROM request_events').fetchone()[0] == 0
        assert connection.execute('SELECT count(*) FROM webhook_deliveries').fetchone()[0] == 0


def test_deleted_target_keeps_notice_and_notification_pagination(workflow):
    w = workflow
    records = [submission(w['user'], w['b']) for _ in range(4)]
    first = w['admin'].get(BASE + '?limit=2').json()
    second = w['admin'].get(BASE + '?limit=2&offset=2').json()
    assert first['has_more'] is True and second['has_more'] is False
    all_items = first['items'] + second['items']
    assert len({n['id'] for n in all_items}) == 4
    assert [n['sequence'] for n in all_items] == sorted((n['sequence'] for n in all_items), reverse=True)
    request = records[-1]
    assert w['user'].delete('/api/v1/requests/' + request['id'] + '?version=' + str(request['version'])).status_code == 204
    gone = next(n for n in items(w['admin']) if n['resource_id'] == request['id'])
    assert gone['resource_available'] is False
    assert w['admin'].put(BASE + '/' + gone['id'] + '/read', json={'read': True}).status_code == 200
    assert len(items(w['admin'])) == 4


def test_shared_acknowledgements_survive_restart_and_supported_backup(workflow, tmp_path):
    w = workflow
    other = second_admin(w, native=True)
    submission(w['user'], w['b'])
    submission(w['user'], w['qb'])
    record = items(other)[0]
    assert other.put(BASE + '/' + record['id'] + '/read', json={'read': True}).status_code == 200
    expected = items(w['admin'])
    bundle = tmp_path / 'backup'
    backup(w['cfg'], bundle)
    assert (bundle / 'application.key').read_text() == w['cfg'].secret
    for config in (w['cfg'], replace(w['cfg'], database_path=bundle / 'feedback.sqlite')):
        with TestClient(create_app(config)) as restarted:
            restarted.headers['Authorization'] = other.headers['Authorization']
            assert items(restarted) == expected
            assert restarted.get(BASE + '/summary').json()['unread_count'] == 1


def test_authenticated_customer_notifications_keep_opaque_identity_private(workflow):
    from test_chat_customers import customer, conversation
    w = workflow
    enable(w)
    client, session = customer(w, external='  private Customer 001  ', display_name='Private customer name')
    chat = conversation(client)
    message = send(client, chat, 'A private authenticated message')
    assert message.status_code == 201
    response = w['admin'].get(BASE)
    assert response.json()['items'][0]['resource_id'] == chat['id']
    for private in ('  private Customer 001  ', 'Private customer name', 'A private authenticated message', session['token']):
        assert private not in response.text
    assert client.get(BASE).status_code == 401


def test_storage_rechecks_admin_session_for_notification_operations(workflow):
    from feedback.errors import Error
    from feedback.security import digest
    w = workflow
    submission(w['user'], w['b'])
    notification = items(w['admin'])[0]
    store = w['app'].state.storage
    session = store.session(digest(w['admin'].cookies['feedback_session']))
    uid, sid = session['id'], session['session_id']
    store.revoke_session(sid)
    for operation in (
        lambda: store.notifications(uid, sid, False, 25, 0),
        lambda: store.notification_summary(uid, sid),
        lambda: store.set_notification_read(uid, sid, notification['id'], True),
        lambda: store.read_all_notifications(uid, sid, notification['sequence']),
    ):
        with pytest.raises(Error) as error:
            operation()
        assert error.value.status == 401


def test_migration_backfills_outstanding_work_once_without_losing_existing_data(workflow):
    from feedback.storage.sqlite import SQLiteStorage
    w = workflow
    pending = submission(w['user'], w['b'])
    accepted = submission(w['user'], w['b'])
    assert w['admin'].post('/api/v1/requests/' + accepted['id'] + '/accept', json={'version': accepted['version']}).status_code == 200
    resubmitted = submission(w['user'], w['b'])
    rejected = w['admin'].post('/api/v1/requests/' + resubmitted['id'] + '/reject', json={'version': resubmitted['version']}).json()
    assert w['user'].patch('/api/v1/requests/' + resubmitted['id'], json={'version': rejected['version'], 'title': 'Resubmitted for migration', 'description': ''}).status_code == 200
    enable(w)
    client, open_chat, _ = visitor(w)
    first = send(client, open_chat, 'Older unanswered message').json()
    latest = send(client, open_chat, 'Latest unanswered message').json()
    answered_client, answered_chat, _ = visitor(w)
    assert send(answered_client, answered_chat).status_code == 201
    assert send(w['admin'], answered_chat, 'We answered').status_code == 201
    closed_client, closed_chat, _ = visitor(w)
    assert send(closed_client, closed_chat).status_code == 201
    assert w['admin'].patch('/api/v1/admin/chat/conversations/' + closed_chat['id'], json={'state': 'closed', 'version': closed_chat['version']}).status_code == 200
    store = w['app'].state.storage
    # Recreate exactly the schema006 database state: migration007 only adds this
    # table, indexes and trigger. Existing domain rows/events/sessions stay intact.
    with store.transaction(True) as connection:
        before = connection.execute('SELECT count(*) FROM request_events').fetchone()[0]
        connection.execute('DROP TABLE admin_notifications')
        connection.execute("DELETE FROM schema_migrations WHERE version='007_admin_notifications.sql'")
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: SQLiteStorage(w['cfg'].database_path, w['cfg'].base_url), range(4)))
    notifications = items(w['admin'])
    assert len(notifications) == 3
    assert {n['resource_id'] for n in notifications} == {pending['id'], resubmitted['id'], open_chat['id']}
    assert next(n for n in notifications if n['resource_id'] == resubmitted['id'])['type'] == 'request.resubmitted'
    assert next(n for n in notifications if n['resource_id'] == open_chat['id'])['id'] == latest['id']
    assert first['id'] not in {n['id'] for n in notifications}
    with store.transaction() as connection:
        assert connection.execute('SELECT count(*) FROM request_events').fetchone()[0] == before
    assert client.get('/api/v1/chat/conversations/' + open_chat['id'] + '/messages').json()['items'] == [first, latest]
    item = notifications[0]
    assert w['admin'].put(BASE + '/' + item['id'] + '/read', json={'read': True}).status_code == 200
    SQLiteStorage(w['cfg'].database_path, w['cfg'].base_url)
    assert next(n for n in items(w['admin']) if n['id'] == item['id'])['read_at']
