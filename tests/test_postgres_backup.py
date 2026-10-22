"""Real pg_dump/pg_restore round trip into an isolated, empty application schema."""
from dataclasses import replace
import json
import os
from pathlib import Path
import shutil
import subprocess
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from conftest import submission
from feedback.api import create_app
from feedback.domain import Actor
from feedback.maintenance import backup
from feedback.security import Vault, digest
from feedback.storage import open_storage

pytestmark = pytest.mark.usefixtures('postgres_url')


@pytest.fixture
def postgres_restore_url(postgres_url):
    import psycopg
    from psycopg import sql
    name = 'supaknock_restore_' + uuid4().hex
    connection = psycopg.connect(postgres_url, autocommit=True)
    created = False
    try:
        connection.execute(sql.SQL('CREATE DATABASE {} TEMPLATE template0').format(sql.Identifier(name)))
        created = True
        parts = urlsplit(postgres_url)
        # Preserve the fixture's redacted repr even if pytest reports a failure.
        yield type(postgres_url)(urlunsplit(parts._replace(path='/' + name)))
    finally:
        if created:
            connection.execute(sql.SQL('DROP DATABASE {} WITH (FORCE)').format(sql.Identifier(name)))
        connection.close()


def restore_archive(url, archive):
    from psycopg.conninfo import conninfo_to_dict
    values = conninfo_to_dict(url)
    env = {key: value for key, value in os.environ.items() if not key.startswith('PG')}
    for key in ('host', 'port', 'user', 'password', 'dbname', 'sslmode', 'connect_timeout'):
        if key in values:
            env['PGDATABASE' if key == 'dbname' else 'PG' + key.upper()] = values[key]
    result = subprocess.run(['pg_restore', '--dbname=' + values['dbname'], '--single-transaction',
                             '--exit-on-error', '--no-owner', '--no-acl', '--no-password', str(archive)],
                            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)
    assert result.returncode == 0, 'pg_restore failed against the disposable test database'


def test_postgres_backup_restores_records_secrets_history_and_new_writes(workflow, postgres_url, postgres_restore_url, tmp_path):
    if not shutil.which('pg_dump') or not shutil.which('pg_restore'):
        pytest.fail('Real PostgreSQL backup verification requires pg_dump and pg_restore')
    import psycopg
    from test_chat import enable, visitor, send, ORIGIN
    from test_webhooks import configured

    w = workflow
    store = w['app'].state.storage
    installation_id = store.installation_id()
    signing_secret = configured(w)
    external = '0001 Account\x00 '
    request = submission(w['trusted'], w['b'], external)
    administrator = Actor(user_id=store.credentials('admin@example.com')['id'], role='admin')
    accepted = store.moderate_request(request['id'], 'accept', 'Reviewed before backup', 1, administrator)
    enable(w)
    guest, conversation, chat_token = visitor(w)
    message = send(guest, conversation, 'A message retained across restore').json()
    with psycopg.connect(postgres_url) as connection:
        connection.execute('CREATE TABLE public.backup_outside_scope(content text)')
        connection.execute("INSERT INTO public.backup_outside_scope VALUES('source only')")
    with store.transaction() as connection:
        events = [dict(row) for row in connection.execute('SELECT * FROM request_events ORDER BY id')]
        deliveries = [dict(row) for row in connection.execute('SELECT * FROM webhook_deliveries ORDER BY id')]
        notifications = [dict(row) for row in connection.execute('SELECT * FROM admin_notifications ORDER BY sequence')]
    assert events and deliveries and notifications

    bundle = tmp_path / 'bundle'
    backup(w['cfg'], bundle)
    assert (bundle / 'feedback.dump').read_bytes().startswith(b'PGDMP')
    assert json.loads((bundle / 'backup.json').read_text())['database_file'] == 'feedback.dump'
    assert (bundle / 'application.key').read_text() == w['cfg'].secret
    assert (bundle / 'setup.token').read_text() == w['cfg'].setup_token
    assert all(item.stat().st_mode & 0o777 == 0o600 for item in bundle.iterdir())
    assert not (bundle / 'feedback.sqlite').exists()
    with pytest.raises(FileExistsError):
        backup(w['cfg'], bundle)

    with psycopg.connect(postgres_restore_url) as connection:
        assert connection.execute("SELECT to_regnamespace('supaknock')").fetchone()[0] is None
        connection.execute('CREATE TABLE public.restore_unrelated(content text)')
        connection.execute("INSERT INTO public.restore_unrelated VALUES('destination preserved')")
    restore_archive(postgres_restore_url, bundle / 'feedback.dump')
    restored_cfg = replace(w['cfg'], database_url=postgres_restore_url,
                           database_path=tmp_path / 'restored' / 'feedback.sqlite',
                           secret=(bundle / 'application.key').read_text())
    restored = open_storage(restored_cfg, initialize=False)
    assert restored.installation_id() == installation_id == store.installation_id()
    assert restored.request(request['id']) == store.request(request['id'])
    assert restored.request(request['id'])['accepted_at'] == accepted['accepted_at']
    assert restored.resolve_external_identity(w['p']['id'], external) == store.request(request['id'])['creator_id']
    assert restored.api_key(digest(w['k']['secret']))['id'] == w['k']['id']
    assert restored.session(digest(w['admin'].cookies['feedback_session']))
    with restored.transaction() as connection:
        assert [dict(row) for row in connection.execute('SELECT * FROM request_events ORDER BY id')] == events
        assert [dict(row) for row in connection.execute('SELECT * FROM webhook_deliveries ORDER BY id')] == deliveries
        assert [dict(row) for row in connection.execute('SELECT * FROM admin_notifications ORDER BY sequence')] == notifications
    assert Vault(restored_cfg.secret).reveal(deliveries[0]['signing_secret']) == signing_secret
    with psycopg.connect(postgres_restore_url) as connection:
        assert connection.execute("SELECT to_regclass('public.backup_outside_scope')").fetchone()[0] is None
        assert connection.execute('SELECT content FROM public.restore_unrelated').fetchall() == [('destination preserved',)]

    app = create_app(restored_cfg)
    with TestClient(app, cookies=dict(w['admin'].cookies), headers=dict(w['admin'].headers)) as admin:
        assert admin.get('/readyz').status_code == 200
        assert admin.get('/api/v1/projects').status_code == 200
        reply = send(admin, conversation, 'A reply after restoration')
        assert reply.status_code == 201
        assert reply.json()['sequence'] == message['sequence'] + 1
        new_request = submission(admin, w['b'])
        assert new_request['id'] != request['id']
    with TestClient(app, headers={'Origin': ORIGIN, 'X-Chat-Token': chat_token}) as client:
        messages = client.get('/api/v1/chat/conversations/' + conversation['id'] + '/messages')
        assert messages.status_code == 200
        assert [item['body'] for item in messages.json()['items']] == [
            'A message retained across restore', 'A reply after restoration']
    # The original installation is unchanged by restoring or writing elsewhere.
    with store.transaction() as connection:
        assert connection.execute('SELECT count(*) FROM chat_messages WHERE conversation_id=?', (conversation['id'],)).fetchone()[0] == 1
        assert connection.execute('SELECT count(*) FROM requests').fetchone()[0] == 1


def test_postgres_maintenance_does_not_initialize_an_empty_destination(postgres_url, tmp_path):
    import psycopg
    from cryptography.fernet import Fernet
    from feedback.config import Config
    cfg = Config(tmp_path / 'local.sqlite', Fernet.generate_key().decode(), 's' * 32,
                 database_provider='postgres', database_url=postgres_url)
    with pytest.raises(RuntimeError, match='No SupaKnock database'):
        backup(cfg, tmp_path / 'empty-target-backup')
    with psycopg.connect(postgres_url) as connection:
        assert connection.execute("SELECT to_regnamespace('supaknock')").fetchone()[0] is None
    assert not (tmp_path / 'empty-target-backup' / 'feedback.dump').exists()


@pytest.mark.parametrize('failure', ['nonzero', 'timeout', 'missing'])
def test_postgres_failed_backup_removes_only_its_private_partial_file(postgres_url, tmp_path, monkeypatch, failure):
    from types import SimpleNamespace
    from feedback.storage.postgres import PostgreSQLStorage
    store = PostgreSQLStorage(postgres_url, 'http://localhost:8000')
    path = tmp_path / 'failed.dump'
    secret_error = 'upstream-error-with-private-credential'
    with store.transaction() as connection:
        major = int(connection.execute('SHOW server_version_num').fetchone()[0]) // 10000

    def fail(command, **kwargs):
        assert Path(command[0]).name == 'pg_dump'
        assert not any('postgresql://' in argument or 'postgres://' in argument for argument in command)
        if '--version' in command:
            return SimpleNamespace(returncode=0, stdout=f'pg_dump (PostgreSQL) {major}.0\n'.encode())
        assert path.stat().st_mode & 0o777 == 0o600
        kwargs['stdout'].write(b'PGDMP incomplete archive')
        if failure == 'timeout':
            raise subprocess.TimeoutExpired(command, 300, stderr=secret_error.encode())
        if failure == 'missing':
            raise FileNotFoundError(secret_error)
        return SimpleNamespace(returncode=1, stderr=secret_error.encode())

    monkeypatch.setattr('feedback.storage.postgres.subprocess.run', fail)
    with pytest.raises(RuntimeError) as rejected:
        store.backup(path)
    assert secret_error not in str(rejected.value)
    assert not path.exists()
    assert store.ready()


def test_postgres_backup_never_overwrites_an_existing_destination(postgres_url, tmp_path, monkeypatch):
    from feedback.storage.postgres import PostgreSQLStorage
    store = PostgreSQLStorage(postgres_url, 'http://localhost:8000')
    path = tmp_path / 'existing.dump'
    original = b'existing backup stays unchanged'
    path.write_bytes(original)

    def forbidden(*args, **kwargs):
        pytest.fail('A preexisting destination must be rejected before pg_dump starts')

    monkeypatch.setattr('feedback.storage.postgres.subprocess.run', forbidden)
    with pytest.raises(RuntimeError, match='new file'):
        store.backup(path)
    assert path.read_bytes() == original


def test_postgres_backup_rejects_mismatched_client_before_creating_archive(postgres_url, tmp_path, monkeypatch):
    from types import SimpleNamespace
    from feedback.storage.postgres import PostgreSQLStorage
    store = PostgreSQLStorage(postgres_url, 'http://localhost:8000')
    path = tmp_path / 'wrong-version.dump'

    def wrong_version(command, **kwargs):
        assert command[1:] == ['--version'], 'A mismatched client must never run the dump'
        return SimpleNamespace(returncode=0, stdout=b'pg_dump (PostgreSQL) 99.0\n')

    monkeypatch.setattr('feedback.storage.postgres.subprocess.run', wrong_version)
    with pytest.raises(RuntimeError, match='same major version'):
        store.backup(path)
    assert not path.exists()
    assert store.ready()
