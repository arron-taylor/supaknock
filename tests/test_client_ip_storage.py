"""Real SQLite guarantees for private, transport-observed network metadata."""
from dataclasses import replace
import json
from pathlib import Path
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
def context(tmp_path):
    store = SQLiteStorage(tmp_path / 'feedback.sqlite', 'https://feedback.example')
    admin = store.create_native_account('admin@example.com', 'test-hash', 'Owner', 'admin', True)
    user = store.create_native_account('user@example.com', 'test-hash', 'Reader')
    store.create_session(admin['id'], 'admin-token-hash', 'csrf-hash', time.time() + 3600)
    principal = {'kind': 'admin', 'user_id': admin['id'],
                 'session_id': store.session('admin-token-hash')['session_id'], 'ip_address': '2001:db8::3'}
    project = store.create_project({'identifier': 'app', 'name': 'App', 'description': ''}, admin['id'])
    board = store.create_board(project['id'], {'identifier': 'ideas', 'name': 'Ideas', 'description': ''})
    store.chat_configure(project['id'], principal, True, [ORIGIN], 0)
    return store, admin, user, principal, project, board


def anonymous(context, ip='192.0.2.1'):
    store, _, _, _, project, _ = context
    token = digest(str(uuid4()))
    conversation = store.chat_create_session(project['id'], ORIGIN, token, time.time() + 3600, ip_address=ip)
    principal = dict(store.chat_visitor(token, ORIGIN), ip_address=ip)
    return conversation, principal


def test_request_ip_is_immutable_and_separate_from_event_and_delivery_payloads(context):
    store, admin, user, _, project, board = context
    store.update_webhook(project['id'], 'https://receiver.example', True,
                         ['request.created', 'request.updated', 'request.deleted'], 'protected-test-secret', 1)
    actor = Actor(user_id=user['id'], role='participant', ip_address='192.0.2.10')
    request = store.create_request(board['id'], 'A request', 'Description', actor)
    assert request['submitted_ip_address'] == '192.0.2.10'
    updated = store.update_creator_request(request['id'], 'Changed', 'Description', 1,
                                           replace(actor, ip_address='2001:db8::1'))
    assert updated['submitted_ip_address'] == '192.0.2.10'
    history = store.history(request['id'], 100, 0)
    assert {item['ip_address'] for item in history} == {'192.0.2.10', '2001:db8::1'}
    assert 'ip_address' not in actor.record()
    store.update_creator_request(request['id'], None, None, 2, actor, delete=True)
    assert len(store.history(request['id'], 100, 0)) == 3
    with store.transaction() as db:
        payloads = [row[0] for row in db.execute('SELECT payload FROM request_events')]
        assert db.execute('SELECT count(*) FROM webhook_deliveries').fetchone()[0] == 3
    assert all('ip_address' not in body and '192.0.2.' not in body and '2001:db8' not in body for body in payloads)
    job = store.claim_delivery(time.time() + 1)
    assert 'ip_address' not in job['payload']


def test_public_history_has_no_network_metadata(context):
    store, admin, user, _, _, board = context
    actor = Actor(user_id=user['id'], role='participant', ip_address='192.0.2.1')
    request = store.create_request(board['id'], 'Request', '', actor)
    store.moderate_request(request['id'], 'accept', None, 1,
                           Actor(user_id=admin['id'], role='admin', ip_address='2001:db8::3'))
    history = store.history(request['id'], 100, 0, public=True)
    assert len(history) == 1 and history[0]['type'] == 'request.accepted'
    assert 'ip_address' not in history[0]


def test_chat_ip_privacy_current_customer_and_idempotent_retry(context):
    store, _, _, admin, _, _ = context
    conversation, visitor = anonymous(context)
    cid = conversation['id']
    assert 'initial_ip_address' not in conversation and 'last_customer_ip_address' not in conversation
    client_id = str(uuid4())
    sent = store.chat_send(cid, visitor, client_id, 'Hello')
    assert 'ip_address' not in sent
    later = dict(visitor, ip_address='2001:db8::20')
    store.chat_send(cid, later, str(uuid4()), 'A second message')
    store.chat_send(cid, dict(visitor, ip_address='192.0.2.99'), client_id, 'Hello')
    reply = store.chat_send(cid, admin, str(uuid4()), 'Reply')
    assert reply['ip_address'] == admin['ip_address']
    view = store.chat_conversation(cid, admin)
    assert view['initial_ip_address'] == '192.0.2.1'
    assert view['last_customer_ip_address'] == '2001:db8::20'
    rows = store.chat_messages(cid, admin, 0, 100)
    assert [row['ip_address'] for row in rows] == ['192.0.2.1', '2001:db8::20', '2001:db8::3']
    assert all('ip_address' not in row for row in store.chat_messages(cid, visitor, 0, 100))
    with store.transaction() as db:
        events = db.execute('SELECT ip_address,payload FROM request_events ORDER BY occurred_at,id').fetchall()
        assert {row['ip_address'] for row in events} == {'192.0.2.1', '2001:db8::20', '2001:db8::3'}
        assert all('ip_address' not in row['payload'] for row in events)
        assert all('ip_address' not in json.dumps(dict(row)) for row in db.execute('SELECT * FROM admin_notifications'))


def test_customer_conversation_ip_and_renewed_session_do_not_overwrite_history(context):
    store, admin, _, admin_principal, project, _ = context
    key_id = store.create_api_key(project['id'], 'App', 'key-hash', 'key-prefix', ['participate'], admin['id'])
    actor = Actor(project_id=project['id'], key_id=key_id, role='integration', permissions=('participate',))
    store.chat_create_customer_session(project['id'], actor, ' opaque customer ', 'customer-hash', time.time() + 3600, None)
    principal = dict(store.chat_customer_session('customer-hash', None), ip_address='192.0.2.15')
    client_conversation_id = str(uuid4())
    conversation = store.chat_create_conversation(principal, client_conversation_id)
    cid, client_message_id = conversation['id'], str(uuid4())
    message = store.chat_send(cid, principal, client_message_id, 'Original')
    store.chat_create_customer_session(project['id'], actor, ' opaque customer ', 'renewed-hash', time.time() + 3600, None)
    renewed = dict(store.chat_customer_session('renewed-hash', None), ip_address='2001:db8::15')
    assert store.chat_create_conversation(renewed, client_conversation_id)['id'] == cid
    assert store.chat_send(cid, renewed, client_message_id, 'Original')['id'] == message['id']
    view = store.chat_conversation(cid, admin_principal)
    assert view['initial_ip_address'] == view['last_customer_ip_address'] == '192.0.2.15'
    store.chat_send(cid, renewed, str(uuid4()), 'New')
    assert store.chat_conversation(cid, admin_principal)['last_customer_ip_address'] == '2001:db8::15'
    assert 'initial_ip_address' not in store.chat_conversation(cid, renewed)


def test_chat_metadata_rolls_back_with_failed_event(context, monkeypatch):
    store, _, _, admin, _, _ = context
    conversation, visitor = anonymous(context)
    original = store._persist_event
    def fail(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError('Simulated transaction failure')
    monkeypatch.setattr(store, '_persist_event', fail)
    with pytest.raises(RuntimeError):
        store.chat_send(conversation['id'], dict(visitor, ip_address='192.0.2.99'), str(uuid4()), 'Rollback')
    view = store.chat_conversation(conversation['id'], admin)
    assert view['last_customer_ip_address'] == '192.0.2.1' and view['last_sequence'] == 0
    assert store.chat_messages(conversation['id'], admin, 0, 100) == []
    with store.transaction() as db:
        assert db.execute('SELECT count(*) FROM request_events').fetchone()[0] == 0
        assert db.execute('SELECT count(*) FROM admin_notifications').fetchone()[0] == 0


def test_account_activity_survives_session_expiry_and_requires_live_admin(context):
    store, admin, _, principal, _, _ = context
    new_user = store.create_native_account('new@example.com', 'password-hash', 'New', ip_address='192.0.2.44', activity_type='signup')
    uid = new_user['id']
    store.create_session(uid, 'old-login-hash', 'csrf-hash', time.time() - 1, 'native', ip_address='2001:db8::44')
    store.create_session(uid, 'new-login-hash', 'csrf-hash', time.time() + 3600, ip_address='192.0.2.45')
    assert store.session('old-login-hash') is None
    rows = store.account_activity(admin['id'], principal['session_id'], 100, 0, user_id=uid)
    assert len(rows) == 3
    assert {(row['event_type'], row['transport'], row['ip_address']) for row in rows} == {
        ('signup', None, '192.0.2.44'), ('login', 'native', '2001:db8::44'), ('login', 'cookie', '192.0.2.45')}
    assert all(row['display_name'] == 'New' and row['email'] == 'new@example.com' and row['role'] == 'participant' for row in rows)
    assert rows == sorted(rows, key=lambda row: (row['occurred_at'], row['id']), reverse=True)
    assert store.account_activity(admin['id'], principal['session_id'], 1, 1, user_id=uid) == rows[1:2]
    with pytest.raises(Error):
        store.account_activity(uid, store.session('new-login-hash')['session_id'], 100, 0)
    store.revoke_session(principal['session_id'])
    with pytest.raises(Error):
        store.account_activity(admin['id'], principal['session_id'], 100, 0)


def test_account_activity_failure_rolls_back_account_and_login(context, monkeypatch, tmp_path):
    store, admin, _, principal, _, _ = context
    def fail(*args):
        raise RuntimeError('Activity persistence unavailable')
    monkeypatch.setattr(store, '_account_activity', fail)
    with pytest.raises(RuntimeError):
        store.create_native_account('rollback@example.com', 'hash', None, ip_address='192.0.2.1', activity_type='signup')
    assert store.credentials('rollback@example.com') is None
    with pytest.raises(RuntimeError):
        store.create_session(admin['id'], 'rollback-login', 'csrf', time.time() + 3600, ip_address='192.0.2.2')
    assert store.session('rollback-login') is None
    fresh = SQLiteStorage(tmp_path / 'fresh.sqlite', 'https://feedback.example')
    monkeypatch.setattr(fresh, '_account_activity', fail)
    with pytest.raises(RuntimeError):
        fresh.create_native_account('setup@example.com', 'hash', None, 'admin', True,
                                    ip_address='192.0.2.3', activity_type='setup')
    assert fresh.setup_available() and fresh.credentials('setup@example.com') is None


def test_migration_unknown_metadata_and_backup_restore(tmp_path):
    path = tmp_path / 'old.sqlite'
    migration_dir = Path(__file__).parents[1] / 'feedback/storage/migrations'
    db = sqlite3.connect(path)
    db.execute('CREATE TABLE schema_migrations(version TEXT PRIMARY KEY,applied_at TEXT NOT NULL)')
    for file in sorted(migration_dir.glob('*.sql')):
        if file.name.startswith('008_'):
            break
        db.executescript(file.read_text())
        db.execute('INSERT INTO schema_migrations VALUES(?,?)', (file.name, now()))
        db.commit()
    uid, pid, bid, rid, customer_id, cid, sid, mid = [str(uuid4()) for _ in range(8)]
    ts = now()
    db.execute("INSERT INTO users VALUES(?,'Owner','admin',?,?)", (uid, ts, ts))
    db.execute('INSERT INTO local_credentials VALUES(?,?,?,?)', (uid, 'owner@example.com', 'hash', ts))
    db.execute("INSERT INTO projects(id,identifier,name,description,created_by,created_at,updated_at) VALUES(?,'old','Old','',?,?,?)", (pid, uid, ts, ts))
    db.execute("INSERT INTO boards(id,project_id,identifier,name,description,created_at,updated_at) VALUES(?,?,'ideas','Ideas','',?,?)", (bid, pid, ts, ts))
    db.execute("INSERT INTO requests(id,board_id,creator_id,title,description,moderation,created_at,updated_at) VALUES(?,?,?,'Old request','','pending',?,?)", (rid, bid, uid, ts, ts))
    db.execute('INSERT INTO chat_settings VALUES(?,1,?,1)', (pid, json.dumps([ORIGIN])))
    db.execute("INSERT INTO chat_customers(id,project_id,kind,created_at,updated_at) VALUES(?,?,'anonymous',?,?)", (customer_id, pid, ts, ts))
    db.execute('INSERT INTO chat_conversations(id,project_id,customer_id,created_at,updated_at,last_sequence,first_message_at) VALUES(?,?,?,?,?,1,?)', (cid, pid, customer_id, ts, ts, ts))
    db.execute('INSERT INTO chat_sessions(id,conversation_id,token_hash,origin,created_at,expires_at) VALUES(?,?,?,?,?,?)', (sid, cid, 'visitor-hash', ORIGIN, ts, time.time() + 3600))
    db.execute("INSERT INTO chat_messages VALUES(?,?,1,?,'visitor',?,'Legacy message',?)", (mid, cid, str(uuid4()), customer_id, ts))
    db.commit(); db.close()
    store = SQLiteStorage(path, 'https://feedback.example')
    store.create_session(uid, 'admin-hash', 'csrf', time.time() + 3600, ip_address='192.0.2.3')
    admin = {'kind': 'admin', 'user_id': uid, 'session_id': store.session('admin-hash')['session_id']}
    assert store.request(rid)['submitted_ip_address'] is None
    assert store.chat_conversation(cid, admin)['initial_ip_address'] is None
    assert store.chat_messages(cid, admin, 0, 100)[0]['ip_address'] is None
    visitor = dict(store.chat_visitor('visitor-hash', ORIGIN), ip_address='192.0.2.5')
    store.chat_send(cid, visitor, str(uuid4()), 'After upgrade')
    backup = tmp_path / 'backup.sqlite'
    store.backup(backup)
    restored = SQLiteStorage(backup, 'https://feedback.example')
    view = restored.chat_conversation(cid, admin)
    assert view['initial_ip_address'] is None and view['last_customer_ip_address'] == '192.0.2.5'
    assert restored.chat_messages(cid, admin, 0, 100)[1]['ip_address'] == '192.0.2.5'
    assert restored.account_activity(uid, admin['session_id'], 100, 0)[0]['ip_address'] == '192.0.2.3'
