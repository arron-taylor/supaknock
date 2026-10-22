"""Provider selection, secret handling and provider-aware backup boundaries."""
from dataclasses import replace
import json
import sys
from types import ModuleType
from urllib.parse import parse_qs, urlsplit

from cryptography.fernet import Fernet
import pytest

from feedback.config import Config, postgres_url
from feedback.maintenance import backup
from feedback.storage import open_storage


@pytest.fixture
def database_env(monkeypatch, tmp_path):
    for name in ('DATABASE_PROVIDER', 'DATABASE_URL', 'APPLICATION_SECRET', 'SETUP_TOKEN',
                 'DATA_DIR', 'PUBLIC_BASE_URL', 'COOKIE_SECURE'):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv('DATA_DIR', str(tmp_path / 'local'))
    monkeypatch.setenv('APPLICATION_SECRET', Fernet.generate_key().decode())
    monkeypatch.setenv('DATABASE_URL', 'postgresql://app:private-password@db.example.com/supaknock')
    return monkeypatch


@pytest.mark.parametrize('provider,expected', [('postgres', 'postgres'), ('postgresql', 'postgres'), ('supabase', 'supabase')])
def test_remote_configuration_uses_local_data_and_explicit_secret(database_env, tmp_path, provider, expected):
    database_env.setenv('DATABASE_PROVIDER', provider)
    cfg = Config.from_env()
    assert cfg.database_provider == expected
    assert cfg.local_data_dir == tmp_path / 'local'
    assert not cfg.database_path.exists()
    assert not (cfg.local_data_dir / 'application.key').exists()
    assert (cfg.local_data_dir / 'setup.token').stat().st_mode & 0o777 == 0o600
    assert parse_qs(urlsplit(cfg.database_url).query) == {'sslmode': ['require'], 'connect_timeout': ['10']}
    assert Config.from_env().setup_token == cfg.setup_token
    assert 'private-password' not in repr(cfg)
    assert cfg.secret not in repr(cfg)
    assert cfg.setup_token not in repr(cfg)


def test_remote_never_generates_or_reuses_a_local_encryption_key(database_env, tmp_path):
    database_env.setenv('DATABASE_PROVIDER', 'postgres')
    database_env.delenv('APPLICATION_SECRET')
    local = tmp_path / 'local'
    local.mkdir()
    key = Fernet.generate_key().decode()
    (local / 'application.key').write_text(key)
    with pytest.raises(RuntimeError, match='explicit APPLICATION_SECRET'):
        Config.from_env()
    assert (local / 'application.key').read_text() == key
    assert not (local / 'setup.token').exists()


@pytest.mark.parametrize('url', [
    '', 'sqlite:///data/feedback.sqlite', 'postgresql://app:private-password@host/',
    'postgresql://app:private-password@host:bad/db',
    'postgresql://app:private-password@host/db?sslmode=disable',
    'postgresql://app:private-password@host/db?sslmode=prefer',
    'postgresql://app:private-password@localhost/db?host=remote&sslmode=disable',
    'postgresql://app:private-password@localhost/db?hostaddr=8.8.8.8&sslmode=disable',
    'postgresql://app:private-password@host/db?options=-csearch_path%3Dpublic',
    'postgresql://app:private-password@host/db?sslpassword=private-password',
    'postgresql://app:private-password@host/db?sslmode=require&sslmode=disable',
    'postgresql://app:private-password@host/db?connect_timeout=0',
    'postgresql://app:private-password@host/db?connect_timeout=999',
    'postgresql://app:private-password@host/db?connect_timeout=bad',
    'postgresql://app:private-password@host/db#fragment',
])
def test_invalid_remote_configuration_does_not_disclose_url(url):
    with pytest.raises(RuntimeError) as exc:
        postgres_url(url)
    assert 'private-password' not in str(exc.value)


@pytest.mark.parametrize('host', ['localhost', '127.0.0.1', '[::1]'])
def test_plaintext_requires_explicit_loopback_test_connection(host):
    url = f'postgresql://app:password@{host}/db?sslmode=disable'
    assert parse_qs(urlsplit(postgres_url(url)).query)['sslmode'] == ['disable']
    with pytest.raises(RuntimeError, match='require TLS'):
        postgres_url(url, 'supabase')


@pytest.mark.parametrize('provider,host', [('supabase', 'custom.example.com'), ('postgres', 'aws-0-us-east-1.pooler.supabase.com'), ('postgres', 'db.project.supabase.co.')])
def test_supabase_transaction_pooler_is_rejected(provider, host):
    with pytest.raises(RuntimeError, match='session pooler'):
        postgres_url(f'postgresql://postgres.project:password@{host}:6543/postgres', provider)


def test_supabase_direct_and_session_pooler_uris_preserve_credentials():
    for host, user in [('db.project.supabase.co', 'postgres'), ('aws-0-us-east-1.pooler.supabase.com', 'postgres.project')]:
        parsed = urlsplit(postgres_url(f'postgresql://{user}:pass%40word@{host}:5432/postgres?sslmode=verify-full&sslrootcert=%2Fcerts%2Froot.crt', 'supabase'))
        assert parsed.username == user
        assert parsed.password == 'pass%40word'
        assert parse_qs(parsed.query)['sslmode'] == ['verify-full']
        assert parse_qs(parsed.query)['sslrootcert'] == ['/certs/root.crt']


def test_remote_data_dir_must_be_absolute(database_env):
    database_env.setenv('DATABASE_PROVIDER', 'postgres')
    database_env.setenv('DATA_DIR', 'relative')
    with pytest.raises(RuntimeError, match='absolute local path'):
        Config.from_env()


def test_sqlite_default_retains_path_and_generated_secrets(database_env, tmp_path):
    database_env.delenv('APPLICATION_SECRET')
    database_env.setenv('DATABASE_URL', 'sqlite:///' + str(tmp_path / 'sqlite' / 'feedback.sqlite'))
    cfg = Config.from_env()
    assert cfg.database_provider == 'sqlite'
    assert cfg.database_url is None
    assert cfg.local_data_dir == tmp_path / 'sqlite'
    assert (cfg.local_data_dir / 'application.key').read_text() == cfg.secret
    assert not (tmp_path / 'local').exists()
    assert replace(cfg, database_path=tmp_path / 'restore' / 'feedback.sqlite').local_data_dir == tmp_path / 'restore'


def test_factory_routes_supabase_to_postgres_without_sqlite_file(database_env, monkeypatch):
    database_env.setenv('DATABASE_PROVIDER', 'supabase')
    cfg = Config.from_env()
    module = ModuleType('feedback.storage.postgres')
    calls = []
    module.PostgreSQLStorage = lambda url, base, *, initialize: calls.append((url, base, initialize)) or 'postgres-adapter'
    monkeypatch.setitem(sys.modules, module.__name__, module)
    assert open_storage(cfg) == 'postgres-adapter'
    assert calls == [(cfg.database_url, cfg.base_url, True)]
    assert not cfg.database_path.exists()


def test_postgres_bundle_keeps_secrets_and_records_format_without_connection_password(database_env, monkeypatch, tmp_path):
    database_env.setenv('DATABASE_PROVIDER', 'postgres')
    cfg = Config.from_env()

    class Adapter:
        def backup(self, path):
            assert path.name == 'feedback.dump'
            path.write_bytes(b'PGDMP fixture')

    def existing_adapter(config, *, initialize):
        assert initialize is False
        return Adapter()
    monkeypatch.setattr('feedback.maintenance.open_storage', existing_adapter)
    destination = tmp_path / 'bundle'
    backup(cfg, destination)
    assert (destination / 'application.key').read_text() == cfg.secret
    assert (destination / 'setup.token').read_text() == cfg.setup_token
    assert json.loads((destination / 'backup.json').read_text()) == {
        'format_version': 1, 'database_provider': 'postgres', 'database_file': 'feedback.dump'}
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in destination.iterdir())
    assert all(b'private-password' not in path.read_bytes() for path in destination.iterdir())
    with pytest.raises(FileExistsError):
        backup(cfg, destination)
