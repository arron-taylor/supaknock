from dataclasses import replace
import os
from uuid import uuid4
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from feedback.api import create_app
from feedback.config import Config

PASSWORD = 'a-long-test-password'


class _TestDatabaseURL(str):
    def __repr__(self):
        return "'<redacted PostgreSQL test URL>'"


def make_storage(path, base_url, database_url=None):
    if database_url is not None:
        from feedback.storage.postgres import PostgreSQLStorage
        return PostgreSQLStorage(database_url, base_url)
    from feedback.storage.sqlite import SQLiteStorage
    return SQLiteStorage(path, base_url)


def reopen_storage(storage):
    from feedback.storage.sqlite import SQLiteStorage
    if isinstance(storage, SQLiteStorage):
        return SQLiteStorage(storage.path, storage.base_url)
    from feedback.storage.postgres import PostgreSQLStorage
    return PostgreSQLStorage(storage._database_url, storage.base_url)


def storage_fingerprint(config, storage):
    """Check read-only operations without assuming a provider has a local file."""
    if config.database_provider == 'sqlite':
        return config.database_path.read_bytes()
    import hashlib
    digest = hashlib.sha256()
    with storage.transaction() as connection:
        tables = [row[0] for row in connection.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname='supaknock' ORDER BY tablename")]
        for table in tables:
            digest.update(table.encode())
            rows = sorted((tuple(row) for row in connection.execute('SELECT * FROM "' + table.replace('"', '""') + '"')), key=repr)
            digest.update(repr(rows).encode())
    return digest.hexdigest()


@pytest.fixture
def database_url():
    """Opt in to real PostgreSQL; each test gets a disposable, isolated database.

    TEST_DATABASE_URL must point at a test server whose role can CREATE DATABASE.
    The referenced database is never migrated or cleared. Only the randomly named
    database created here is dropped, including when a test fails.
    """
    server = os.environ.get('TEST_DATABASE_URL')
    if not server:
        yield None
        return
    import psycopg
    from psycopg import sql
    name = 'supaknock_test_' + uuid4().hex
    connection = psycopg.connect(server, autocommit=True)
    created = False
    try:
        connection.execute(sql.SQL('CREATE DATABASE {} TEMPLATE template0').format(sql.Identifier(name)))
        created = True
        # Config accepts URLs. Quote credentials/path segments rather than
        # converting to a libpq string, which is deliberately not public config.
        from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
        parts = urlsplit(server)
        query = urlencode([(key, value) for key, value in parse_qsl(parts.query)
                           if key != 'dbname'])
        yield _TestDatabaseURL(urlunsplit((parts.scheme, parts.netloc, '/' + name, query, parts.fragment)))
    finally:
        if created:
            connection.execute(sql.SQL('DROP DATABASE {} WITH (FORCE)').format(sql.Identifier(name)))
        connection.close()


@pytest.fixture
def postgres_url(database_url):
    if database_url is None:
        pytest.skip('Set TEST_DATABASE_URL to run real PostgreSQL contracts')
    return database_url


@pytest.fixture
def installation(tmp_path, database_url):
    cfg = Config(tmp_path / 'feedback.sqlite', Fernet.generate_key().decode(), 'setup-token-' * 4, worker=False, allow_internal_webhooks=True, auth_rate_limit=10000, rate_limit=10000)
    if database_url is not None:
        cfg = replace(cfg, database_provider='postgresql', database_url=database_url)
    app = create_app(cfg)
    admin = TestClient(app)
    r = admin.post('/api/v1/setup', json={'email':'admin@example.com','password':PASSWORD,'setup_token':cfg.setup_token,'display_name':'Owner'})
    assert r.status_code == 201, r.text
    admin.headers.update({'Origin':cfg.base_url,'X-CSRF-Token':r.json()['csrf_token']})
    user = TestClient(app)
    r = user.post('/api/v1/auth/register', json={'email':'user@example.com','password':PASSWORD,'display_name':'Reader'})
    assert r.status_code == 201, r.text
    user.headers.update({'Origin':cfg.base_url,'X-CSRF-Token':r.json()['csrf_token']})
    anon = TestClient(app)
    return app, cfg, admin, user, anon


@pytest.fixture
def workflow(installation):
    app,cfg,admin,user,anon = installation
    p = admin.post('/api/v1/projects',json={'identifier':'glean','name':'Glean'}).json()
    q = admin.post('/api/v1/projects',json={'identifier':'another','name':'Another'}).json()
    b = admin.post('/api/v1/projects/'+p['id']+'/boards',json={'identifier':'features','name':'Features'}).json()
    qb = admin.post('/api/v1/projects/'+q['id']+'/boards',json={'identifier':'features','name':'Features'}).json()
    def key(project,permissions):
        r=admin.post('/api/v1/projects/'+project['id']+'/keys',json={'name':'Integration','permissions':permissions})
        assert r.status_code==201,r.text
        client=TestClient(app)
        client.headers['Authorization']='Bearer '+r.json()['secret']
        return client,r.json()
    trusted,k=key(p,['read','participate','audience:read'])
    manager,mk=key(p,['read','manage'])
    other,ok=key(q,['read','participate'])
    return dict(app=app,cfg=cfg,admin=admin,user=user,anon=anon,p=p,q=q,b=b,qb=qb,trusted=trusted,k=k,manager=manager,other=other,key=key)


def submission(client,board,external=None):
    body={'title':'Offline mode','description':'Please support offline reading.'}
    if external is not None: body['external_user_id']=external
    r=client.post('/api/v1/boards/'+board['id']+'/requests',json=body)
    assert r.status_code==201,r.text
    return r.json()
