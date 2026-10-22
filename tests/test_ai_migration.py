"""Upgrade the real pre-AI schema without rewriting existing identities/history."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
import shutil
import time
from uuid import uuid4

import pytest

from feedback.domain import Actor
from feedback.errors import Error
from feedback.storage.sqlite import SQLiteStorage
import feedback.storage.sqlite as adapter

ORIGIN = 'https://customer.example'
BASE = 'https://feedback.example'


@contextmanager
def migration_directory(monkeypatch, directory):
    with monkeypatch.context() as patch:
        patch.setattr(adapter, '__file__', str(directory.parent / 'sqlite.py'))
        yield


def pre_ai_database(tmp_path, monkeypatch):
    old = tmp_path / 'old-adapter' / 'migrations'
    old.mkdir(parents=True)
    source = Path(adapter.__file__).with_name('migrations')
    for migration in sorted(source.glob('*.sql')):
        if migration.name < '009':
            shutil.copyfile(migration, old / migration.name)
    path = tmp_path / 'old.sqlite'
    with migration_directory(monkeypatch, old):
        store = SQLiteStorage(path, BASE)
        # The current adapter checks archive columns while seeding. Remove these
        # fixture-only columns before upgrade so the actual old schema is tested.
        with store.transaction(True) as c:
            c.execute('ALTER TABLE projects ADD COLUMN archived_at TEXT')
            c.execute('ALTER TABLE boards ADD COLUMN archived_at TEXT')
            c.execute("ALTER TABLE boards ADD COLUMN visibility TEXT NOT NULL DEFAULT 'public'")
        user = store.create_native_account('owner@example.com', 'test-hash', 'Owner', 'admin', True,
                                           ip_address='192.0.2.10')
        store.create_session(user['id'], 'native-hash', 'csrf-hash', time.time() + 3600)
        admin = {'kind': 'admin', 'user_id': user['id'],
                 'session_id': store.session('native-hash')['session_id']}
        project = store.create_project({'identifier':'old', 'name':'Old', 'description':''}, user['id'])
        board = store.create_board(project['id'], {'identifier':'features','name':'Features','description':''})
        actor = Actor(user_id=user['id'], role='admin')
        request = store.create_request(board['id'], 'Existing accepted request', 'Original evidence', actor)
        accepted = store.moderate_request(request['id'], 'accept', None, request['version'], actor)
        store.chat_configure(project['id'], admin, True, [ORIGIN], 0)
        conversation = store.chat_create_session(project['id'], ORIGIN, 'visitor-hash', time.time() + 3600,
                                                 ip_address='192.0.2.11')
        visitor = {**store.chat_visitor('visitor-hash', ORIGIN), 'ip_address': '192.0.2.12'}
        client_id = str(uuid4())
        message = store.chat_send(conversation['id'], visitor, client_id, 'Original customer message')
        reply = store.chat_send(conversation['id'], admin, str(uuid4()), 'Human reply')
        store.chat_read(conversation['id'], visitor, reply['sequence'])
        with store.transaction(True) as c:
            c.execute('ALTER TABLE boards DROP COLUMN visibility')
            c.execute('ALTER TABLE boards DROP COLUMN archived_at')
            c.execute('ALTER TABLE projects DROP COLUMN archived_at')
    return dict(path=path, project=project, board=board, admin=admin, actor=actor,
                request=accepted, conversation=conversation, visitor=visitor, client_id=client_id,
                message=message, reply=reply)


def assert_legacy_preserved(store, data):
    cid = data['conversation']['id']
    visitor = store.chat_visitor('visitor-hash', ORIGIN)
    messages = store.chat_messages(cid, data['admin'], 0, 100)
    assert [m['id'] for m in messages] == [data['message']['id'], data['reply']['id']]
    assert [m['sequence'] for m in messages] == [1, 2]
    assert messages[0]['ip_address'] == '192.0.2.12'
    assert store.chat_send(cid, visitor, data['client_id'], 'Original customer message')['id'] == data['message']['id']
    view = store.chat_conversation(cid, visitor)
    assert view['unread_count'] == 0
    assert view['customer']['kind'] == 'anonymous' and view['customer']['user_id'] is None
    assert store.chat_conversation(cid, data['admin'])['initial_ip_address'] == '192.0.2.11'
    request = store.request(data['request']['id'])
    assert request['accepted_at'] == data['request']['accepted_at']
    with pytest.raises(Error):
        store.update_creator_request(request['id'], 'Changed', None, request['version'], data['actor'])
    assert store.request(request['id'])['title'] == 'Existing accepted request'


def test_ai_upgrade_preserves_chat_and_accepted_feedback(tmp_path, monkeypatch):
    data = pre_ai_database(tmp_path, monkeypatch)
    with ThreadPoolExecutor(max_workers=4) as pool:
        stores = list(pool.map(lambda _: SQLiteStorage(data['path'], BASE), range(4)))
    for store in stores:
        assert store.ready()
        assert_legacy_preserved(store, data)
    target = tmp_path / 'restored.sqlite'
    stores[0].backup(target)
    assert_legacy_preserved(SQLiteStorage(target, BASE), data)


def test_failed_ai_upgrade_keeps_legacy_database_recoverable(tmp_path, monkeypatch):
    data = pre_ai_database(tmp_path, monkeypatch)
    original = Path(adapter.__file__).with_name('migrations')
    broken = tmp_path / 'broken-adapter' / 'migrations'
    shutil.copytree(original, broken)
    (broken / '999_test_failure.sql').write_text('THIS IS INTENTIONALLY INVALID;\n')
    with migration_directory(monkeypatch, broken):
        with pytest.raises(Exception):
            SQLiteStorage(data['path'], BASE)
    # Normal startup repairs nothing destructively: it applies the entire pending
    # migration transaction and preserves all existing opaque ids and history.
    store = SQLiteStorage(data['path'], BASE)
    assert_legacy_preserved(store, data)


def test_ai_sources_and_encrypted_credentials_survive_supported_backup(tmp_path, monkeypatch):
    from dataclasses import replace
    from cryptography.fernet import Fernet
    from feedback.ai_domain import default_ai_settings
    from feedback.api import create_app
    from feedback.config import Config
    from feedback.maintenance import backup
    from feedback.security import Vault

    data = pre_ai_database(tmp_path, monkeypatch)
    store = SQLiteStorage(data['path'], BASE)
    pid, admin = data['project']['id'], data['admin']
    key = Fernet.generate_key().decode()
    vault = Vault(key)
    cfg = Config(data['path'], key, 's' * 32, base_url=BASE, worker=False)
    settings = default_ai_settings()
    settings.update(model='gpt-6-luna', soul='Be concise and kind.', feedback_url='https://customer.example/feedback')
    saved = store.ai_configure(pid, admin, 0, settings, vault.protect('test-only-provider-secret'))
    assert saved['has_key'] and saved['settings']['enabled'] is False
    assert not any(saved['settings']['tools'].values()) and saved['settings']['allow_guest_reports'] is False
    assert 'protected_key' not in saved
    store.ai_runtime_begin(admin, 0, 'download', vault.protect('test-only-hub-secret'))
    assert 'protected_token' not in store.ai_runtime(admin)

    source = store.ai_source_create(pid, admin, 'markdown', 'Support guide')
    source = store.ai_source_update(pid, admin, source['id'], source['version'], active=True, approved=True)
    revision = store.ai_revision_stage(pid, admin, source['id'], source['version'], 'Support guide', '# Support\nOriginal source content.', 1)
    store.ai_revision_chunks(pid, admin, revision['id'], [{'ordinal':0, 'title':'Support guide',
        'content':'Original source content.', 'location':None, 'vector':[1.] + [0.] * 767}])
    store.ai_revision_publish(pid, admin, revision['id'])
    expected_chunks = store.ai_knowledge_chunks(pid, admin, query='Original')
    assert len(expected_chunks) == 1
    # Weights are replaceable cache; supported backup includes only authoritative
    # SQLite data and effective secrets, even when a model directory exists.
    model_cache = data['path'].parent / 'models'
    model_cache.mkdir()
    (model_cache / 'test-cache-marker').write_text('not authoritative')
    # Readiness is a current diagnostic, not a stored configuration field: this
    # source became indexed after the earlier settings write response.
    saved_at_backup = store.ai_settings(pid, admin)
    assert saved_at_backup['settings'] == saved['settings']
    assert 'knowledge_not_ready' in saved['readiness']['issues']
    assert 'knowledge_not_ready' not in saved_at_backup['readiness']['issues']
    bundle = tmp_path / 'bundle'
    backup(cfg, bundle)
    assert {p.name for p in bundle.iterdir()} == {'feedback.sqlite','application.key','setup.token','README.txt'}
    restored = SQLiteStorage(bundle / 'feedback.sqlite', BASE)
    assert restored.ai_settings(pid, admin) == saved_at_backup
    assert restored.ai_knowledge_chunks(pid, admin, query='Original') == expected_chunks
    assert set(Vault((bundle / 'application.key').read_text()).reveal(x) for x in restored.signing_secrets()) == {
        'test-only-provider-secret', 'test-only-hub-secret'}
    restored_cfg = replace(cfg, database_path=bundle / 'feedback.sqlite')
    # A restored database without its excluded model cache never resumes a
    # download, even if the backup captured an authorized pending operation.
    app = create_app(restored_cfg)
    assert app.state.storage.ai_runtime(admin)['state'] == 'not_downloaded'
    assert not (bundle / 'models').exists()
    with pytest.raises(RuntimeError, match='cannot decrypt stored installation secrets'):
        create_app(replace(restored_cfg, secret=Fernet.generate_key().decode()))
    assert_legacy_preserved(restored, data)


def test_default_ai_permissions_and_personality_persist_across_restart(tmp_path, monkeypatch):
    from feedback.ai_domain import default_ai_settings
    data = pre_ai_database(tmp_path, monkeypatch)
    store = SQLiteStorage(data['path'], BASE)
    pid, admin = data['project']['id'], data['admin']
    defaults = store.ai_settings(pid, admin)
    assert defaults['version'] == 0 and defaults['settings'] == default_ai_settings()
    setting = {**defaults['settings'], 'soul': 'Friendly support guide.'}
    saved = store.ai_configure(pid, admin, 0, setting)
    reopened = SQLiteStorage(data['path'], BASE)
    assert reopened.ai_settings(pid, admin) == saved
    assert reopened.ai_configure(pid, admin, saved['version'], setting) == saved
    # Stale clients cannot overwrite the surviving version after restart.
    with pytest.raises(Error) as error:
        reopened.ai_configure(pid, admin, 0, default_ai_settings())
    assert error.value.status == 409
    # Missing tool flags never become an implicit permission grant.
    incomplete = {**setting, 'tools': {}}
    with pytest.raises(Error):
        reopened.ai_configure(pid, admin, saved['version'], incomplete)
    assert reopened.ai_settings(pid, admin) == saved
