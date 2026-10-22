"""Real PostgreSQL contracts; opt in with TEST_DATABASE_URL or test-postgres.py.

Each test uses a new random database. Shared API tests also run against this
provider through conftest's installation fixture; this file targets database
boundaries that a successful HTTP response alone cannot establish.
"""
from concurrent.futures import ThreadPoolExecutor
import threading
import time

import pytest

from conftest import submission
from feedback.domain import Actor
from feedback.errors import Error

pytestmark = pytest.mark.usefixtures('postgres_url')


def storage(url):
    from feedback.storage.postgres import PostgreSQLStorage
    return PostgreSQLStorage(url, 'http://localhost:8000')


def test_postgres_concurrent_first_migration_and_restart(postgres_url):
    gate = threading.Barrier(4)
    def start(_):
        gate.wait()
        return storage(postgres_url)
    with ThreadPoolExecutor(max_workers=4) as pool:
        stores = list(pool.map(start, range(4)))
    assert all(s.ready() for s in stores)
    with stores[0].transaction() as connection:
        versions = [row[0] for row in connection.execute('SELECT version FROM schema_migrations')]
    assert versions and len(versions) == len(set(versions))
    assert storage(postgres_url).ready()


def test_postgres_future_migration_fails_without_changing_data(workflow, postgres_url):
    store = workflow['app'].state.storage
    request = submission(workflow['user'], workflow['b'])
    with store.transaction(True) as connection:
        connection.execute("INSERT INTO schema_migrations(version,checksum,applied_at) VALUES('999_future.sql',?,'today')", ('0' * 64,))
    with pytest.raises(RuntimeError, match='newer'):
        storage(postgres_url)
    assert store.request(request['id'])['title'] == request['title']
    with store.transaction() as connection:
        assert connection.execute("SELECT count(*) FROM schema_migrations WHERE version='999_future.sql'").fetchone()[0] == 1


def test_postgres_migration_checksum_change_fails_closed(workflow, postgres_url):
    store = workflow['app'].state.storage
    request = submission(workflow['user'], workflow['b'])
    with store.transaction(True) as connection:
        connection.execute('UPDATE schema_migrations SET checksum=?', ('0' * 64,))
    with pytest.raises(RuntimeError, match='(?i)(checksum|changed|modified|match)'):
        storage(postgres_url)
    assert store.request(request['id'])['title'] == request['title']


def test_postgres_failed_migration_rolls_back_schema_and_ledger(postgres_url, tmp_path, monkeypatch):
    import psycopg
    import feedback.storage.postgres as module
    migrations = tmp_path / 'migrations'
    migrations.mkdir()
    (migrations / '001_invalid.sql').write_text('CREATE TABLE partial(id TEXT);\nTHIS IS INVALID;\n')
    monkeypatch.setattr(module, '_MIGRATIONS', migrations)
    with pytest.raises(RuntimeError, match='migration failed'):
        storage(postgres_url)
    with psycopg.connect(postgres_url) as connection:
        assert connection.execute("SELECT to_regnamespace('supaknock')").fetchone()[0] is None


def test_postgres_unrecognized_private_schema_fails_closed(postgres_url):
    import psycopg
    with psycopg.connect(postgres_url) as connection:
        connection.execute('CREATE SCHEMA supaknock')
        connection.execute('CREATE TABLE supaknock.unrelated(content text)')
        connection.execute("INSERT INTO supaknock.unrelated VALUES('preserve')")
    with pytest.raises(RuntimeError, match='Unrecognized'):
        storage(postgres_url)
    with psycopg.connect(postgres_url) as connection:
        assert connection.execute('SELECT content FROM supaknock.unrelated').fetchall() == [('preserve',)]


def test_postgres_constraints_protect_accepted_timestamp_and_recover_transaction(workflow):
    w = workflow; store = w['app'].state.storage
    request = submission(w['user'], w['b'])
    admin = Actor(user_id=store.credentials('admin@example.com')['id'], role='admin')
    accepted = store.moderate_request(request['id'], 'accept', None, 1, admin)
    with pytest.raises(Error) as rejected:
        with store.transaction(True) as connection:
            connection.execute('UPDATE requests SET accepted_at=? WHERE id=?', ('changed', request['id']))
    assert rejected.value.status == 409
    assert store.request(request['id'])['accepted_at'] == accepted['accepted_at']
    # A connection error/aborted transaction must never poison the next write.
    next_request = submission(w['user'], w['b'])
    assert next_request['version'] == 1


def test_postgres_acceptance_races_creator_edits_and_deletes(workflow):
    from test_storage import test_acceptance_races_creator_update
    test_acceptance_races_creator_update(workflow, False)
    test_acceptance_races_creator_update(workflow, True)


def test_postgres_concurrent_opaque_identities_and_idempotent_votes(workflow):
    from test_storage import test_concurrent_identity_and_votes
    test_concurrent_identity_and_votes(workflow)


def test_postgres_opaque_ids_preserve_nul_unicode_case_and_spaces(workflow):
    w = workflow; store = w['app'].state.storage
    values = ('A\x00B', 'A B', 'A', 'a', 'A ', ' A', '\u00e9', 'e\u0301')
    first = [submission(w['trusted'], w['b'], value) for value in values]
    other = [submission(w['other'], w['qb'], value) for value in values]
    ids = [store.request(item['id'])['creator_id'] for item in first + other]
    assert len(set(ids)) == 2 * len(values)
    for index, value in enumerate(values):
        assert store.resolve_external_identity(w['p']['id'], value) == ids[index]
        own = w['trusted'].get('/api/v1/requests/' + first[index]['id'],
                               params={'external_user_id': value})
        assert own.status_code == 200
        history = w['admin'].get('/api/v1/requests/' + first[index]['id'] + '/history').json()
        assert history['items'][0]['creator']['external_user_id'] == value
        assert w['anon'].get('/api/v1/requests/' + first[index]['id']).status_code == 404


def test_postgres_event_and_webhook_jobs_roll_back_together(workflow):
    from test_webhooks import test_claim_is_exclusive_and_event_job_rollback
    test_claim_is_exclusive_and_event_job_rollback(workflow)


def test_postgres_acceptance_event_failure_rolls_back(workflow):
    from test_storage import test_acceptance_event_failure_rolls_back
    test_acceptance_event_failure_rolls_back(workflow)


def test_postgres_expired_delivery_lease_fences_previous_worker(workflow, postgres_url):
    from test_webhooks import configured
    w = workflow; configured(w)
    submission(w['user'], w['b'])
    first_process, second_process = storage(postgres_url), storage(postgres_url)
    instant = time.time()
    first = first_process.claim_delivery(instant)
    assert first and second_process.claim_delivery(instant) is None
    second = second_process.claim_delivery(instant + 61)
    assert second['id'] == first['id'] and second['lease_token'] != first['lease_token']
    first_process.finish_delivery(first['id'], first['lease_token'], 'succeeded', 200, None, instant)
    assert second_process.delivery(w['p']['id'], first['id'])['state'] == 'delivering'
    second_process.finish_delivery(second['id'], second['lease_token'], 'succeeded', 200, None, instant)
    assert first_process.delivery(w['p']['id'], first['id'])['state'] == 'succeeded'


def test_postgres_chat_retry_is_atomic_and_body_free(workflow, monkeypatch):
    from test_chat_customers import test_chat_event_is_body_free_atomic_signed_and_immutable_on_retry
    test_chat_event_is_body_free_atomic_signed_and_immutable_on_retry(workflow, monkeypatch)


def test_postgres_concurrent_chat_message_sequence(workflow):
    from test_chat import test_chat_idempotence_cursor_unread_and_close
    test_chat_idempotence_cursor_unread_and_close(workflow)


def test_postgres_shared_notifications_rollback(workflow, monkeypatch):
    from test_notifications import test_notification_and_business_event_commit_or_rollback_together
    test_notification_and_business_event_commit_or_rollback_together(workflow, monkeypatch)


def test_postgres_unrelated_public_schema_is_not_modified(postgres_url):
    import psycopg
    with psycopg.connect(postgres_url) as connection:
        connection.execute('CREATE TABLE public.unrelated (content text)')
        connection.execute("INSERT INTO public.unrelated VALUES('keep me')")
    assert storage(postgres_url).ready()
    with psycopg.connect(postgres_url) as connection:
        assert connection.execute('SELECT content FROM public.unrelated').fetchall() == [('keep me',)]
        assert connection.execute("SELECT count(*) FROM information_schema.tables WHERE table_schema='public'").fetchone()[0] == 1


def test_postgres_database_enforces_customer_and_notification_immutability(workflow):
    from test_chat import enable, visitor, send
    w = workflow; enable(w)
    first, conversation, _ = visitor(w)
    assert send(first, conversation).status_code == 201
    store = w['app'].state.storage
    with store.transaction() as connection:
        customer_id = connection.execute('SELECT customer_id FROM chat_conversations WHERE id=?',
                                         (conversation['id'],)).fetchone()[0]
    prohibited = [
        ('UPDATE chat_customers SET project_id=? WHERE id=?', (w['q']['id'], customer_id)),
        ('UPDATE chat_conversations SET project_id=? WHERE id=?', (w['q']['id'], conversation['id'])),
        ('UPDATE admin_notifications SET title=?', ('Rewritten event',)),
    ]
    for statement, parameters in prohibited:
        with pytest.raises(Error) as rejected:
            with store.transaction(True) as connection:
                connection.execute(statement, parameters)
        assert rejected.value.status == 409
    assert first.get('/api/v1/chat/conversations/' + conversation['id']).status_code == 200


def test_postgres_schema_preserves_relational_constraints(postgres_url, tmp_path):
    """The provider change must not silently drop an existing FK/unique/NOT NULL guard."""
    import psycopg
    from feedback.storage.sqlite import SQLiteStorage
    baseline = SQLiteStorage(tmp_path / 'parity.sqlite', 'http://localhost:8000')
    storage(postgres_url)
    with baseline.transaction() as sqlite:
        tables = {row[0] for row in sqlite.execute("SELECT name FROM sqlite_master WHERE type='table'")
                  if row[0] != 'schema_migrations' and not row[0].startswith(('sqlite_', 'ai_chunks_fts'))}
        required, foreign_keys, unique_keys = set(), set(), set()
        for table in tables:
            required.update((table, row['name']) for row in sqlite.execute(f'PRAGMA table_info("{table}")')
                            if row['notnull'] or row['pk'])
            groups = {}
            for row in sqlite.execute(f'PRAGMA foreign_key_list("{table}")'):
                groups.setdefault(row['id'], []).append(row)
            for rows in groups.values():
                rows.sort(key=lambda row: row['seq'])
                foreign_keys.add((table, tuple(row['from'] for row in rows), rows[0]['table'],
                                  tuple(row['to'] for row in rows), rows[0]['on_delete'], rows[0]['on_update']))
            for row in sqlite.execute(f'PRAGMA index_list("{table}")'):
                if row['unique'] and not row['partial']:
                    cols = tuple(item['name'] for item in sqlite.execute(f'PRAGMA index_info("{row["name"]}")'))
                    unique_keys.add((table, cols))
        triggers = {row[0] for row in sqlite.execute("SELECT name FROM sqlite_master WHERE type='trigger'")
                    if row[0] not in ('ai_chunks_insert', 'ai_chunks_delete')}
    with psycopg.connect(postgres_url) as pg:
        pg_tables = {row[0] for row in pg.execute("SELECT tablename FROM pg_tables WHERE schemaname='supaknock'")}
        assert pg_tables - {'schema_migrations'} == tables
        pg_required = set(pg.execute("SELECT table_name,column_name FROM information_schema.columns WHERE table_schema='supaknock' AND is_nullable='NO'"))
        assert required <= pg_required
        rows = pg.execute("""
            SELECT c.oid,t.relname,c.confrelid::regclass::text,c.confdeltype,c.confupdtype,
                   array_agg(a.attname ORDER BY u.i),array_agg(b.attname ORDER BY u.i)
            FROM pg_constraint c JOIN pg_class t ON t.oid=c.conrelid
            JOIN pg_namespace n ON n.oid=t.relnamespace
            CROSS JOIN LATERAL generate_subscripts(c.conkey,1) u(i)
            JOIN pg_attribute a ON a.attrelid=c.conrelid AND a.attnum=c.conkey[u.i]
            JOIN pg_attribute b ON b.attrelid=c.confrelid AND b.attnum=c.confkey[u.i]
            WHERE c.contype='f' AND n.nspname='supaknock'
            GROUP BY c.oid,t.relname,c.confrelid,c.confdeltype,c.confupdtype
        """)
        actions = {'a': 'NO ACTION', 'r': 'RESTRICT', 'c': 'CASCADE', 'n': 'SET NULL', 'd': 'SET DEFAULT'}
        pg_keys = {(table, tuple(source), target.removeprefix('supaknock.'), tuple(destination),
                    actions[delete], actions[update])
                   for _, table, target, delete, update, source, destination in rows}
        assert foreign_keys == pg_keys
        pg_unique = {(table, tuple(cols)) for table, cols in pg.execute("""
            SELECT t.relname,array_agg(a.attname ORDER BY u.ord)
            FROM pg_index x JOIN pg_class t ON t.oid=x.indrelid
            JOIN pg_namespace n ON n.oid=t.relnamespace
            CROSS JOIN LATERAL unnest(x.indkey) WITH ORDINALITY u(num,ord)
            JOIN pg_attribute a ON a.attrelid=t.oid AND a.attnum=u.num
            WHERE x.indisunique AND x.indpred IS NULL AND n.nspname='supaknock'
            GROUP BY x.indexrelid,t.relname
        """)}
        assert unique_keys <= pg_unique
        pg_triggers = {row[0] for row in pg.execute("""
            SELECT t.tgname FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid
            JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE n.nspname='supaknock' AND NOT t.tgisinternal
        """)}
        assert triggers == pg_triggers


def test_postgres_supabase_api_default_grants_are_revoked(postgres_url):
    """Simulate Supabase default grants without contacting a hosted project."""
    import psycopg
    from psycopg import sql
    roles = ('anon', 'authenticated', 'service_role')
    with psycopg.connect(postgres_url) as connection:
        for role in roles:
            if not connection.execute('SELECT 1 FROM pg_roles WHERE rolname=%s', (role,)).fetchone():
                connection.execute(sql.SQL('CREATE ROLE {} NOLOGIN').format(sql.Identifier(role)))
            for objects in ('TABLES', 'SEQUENCES', 'FUNCTIONS'):
                connection.execute(sql.SQL('ALTER DEFAULT PRIVILEGES GRANT ALL ON {} TO {}')
                                   .format(sql.SQL(objects), sql.Identifier(role)))
    storage(postgres_url)
    with psycopg.connect(postgres_url) as connection:
        for role in roles:
            assert connection.execute("SELECT has_schema_privilege(%s,'supaknock','USAGE')", (role,)).fetchone()[0] is False
            for table in ('users', 'sessions', 'api_keys', 'chat_messages', 'ai_generation_keys'):
                assert connection.execute('SELECT has_table_privilege(%s,%s,\'SELECT\')',
                                          (role, 'supaknock.' + table)).fetchone()[0] is False
            assert connection.execute("SELECT has_function_privilege(%s,'supaknock.accepted_immutable_guard()','EXECUTE')", (role,)).fetchone()[0] is False
    for role in roles:
        with psycopg.connect(postgres_url) as connection:
            connection.execute(sql.SQL('SET ROLE {}').format(sql.Identifier(role)))
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                connection.execute('SELECT * FROM supaknock.users')


def test_postgres_driver_failures_are_sanitized_and_next_transaction_works(postgres_url):
    store = storage(postgres_url)
    with pytest.raises(Error) as failed:
        with store.transaction(True) as connection:
            connection.execute("SELECT 'private-driver-error-value' FROM not_a_real_table")
    assert failed.value.status == 503
    assert 'private-driver-error-value' not in str(failed.value)
    assert 'not_a_real_table' not in str(failed.value)
    assert failed.value.__suppress_context__ is True
    assert store.ready()


def test_postgres_nul_text_is_explicit_validation_error(workflow):
    w = workflow
    for field in ('title', 'description'):
        payload = {'title': 'Title', 'description': 'Description', field: 'bad\x00text'}
        response = w['user'].post('/api/v1/boards/' + w['b']['id'] + '/requests', json=payload)
        assert response.status_code == 400
        assert 'bad\x00text' not in response.text
    assert submission(w['trusted'], w['b'], 'allowed\x00external')['version'] == 1
