"""PostgreSQL error privacy and binding checks without a database server."""
import traceback

import psycopg
import pytest

from feedback.errors import Error
from feedback.storage import postgres


PRIVATE_MARKER = 'synthetic-private-value-not-for-error-output'


class BrokenCleanupConnection:
    """A driver connection that also fails while cleaning up its first error."""
    def __init__(self, failure=None, *, fail_at='query'):
        self.failure = failure
        self.fail_at = fail_at
        self.rolled_back = self.closed = False

    def execute(self, sql):
        if self.fail_at == 'begin' or sql == 'FAILING OPERATION':
            raise self.failure

    def commit(self):
        if self.fail_at == 'commit':
            raise self.failure

    def rollback(self):
        self.rolled_back = True
        raise psycopg.OperationalError(PRIVATE_MARKER)

    def close(self):
        self.closed = True
        raise psycopg.OperationalError(PRIVATE_MARKER)


def store_with_raw(raw):
    storage = postgres.PostgreSQLStorage.__new__(postgres.PostgreSQLStorage)
    storage.connect = lambda: postgres.Connection(raw)
    return storage


def assert_private(error):
    assert PRIVATE_MARKER not in str(error)
    assert PRIVATE_MARKER not in ''.join(traceback.format_exception(error))


@pytest.mark.parametrize('failure_type,status,code', [
    (psycopg.IntegrityError, 409, 'conflict'),
    (psycopg.OperationalError, 503, 'storage_unavailable'),
    (psycopg.ProgrammingError, 503, 'storage_unavailable'),
])
@pytest.mark.parametrize('fail_at', ['query', 'commit'])
def test_transaction_failure_stays_private_when_rollback_and_close_also_fail(failure_type, status, code, fail_at):
    raw = BrokenCleanupConnection(failure_type(PRIVATE_MARKER), fail_at=fail_at)
    storage = store_with_raw(raw)
    with pytest.raises(Error) as caught:
        with storage.transaction() as connection:
            if fail_at == 'query':
                connection.execute('FAILING OPERATION')
    assert (caught.value.status, caught.value.code) == (status, code)
    assert raw.rolled_back and raw.closed
    assert_private(caught.value)


def test_migration_failure_stays_private_when_rollback_and_close_also_fail():
    raw = BrokenCleanupConnection(psycopg.ProgrammingError(PRIVATE_MARKER), fail_at='begin')
    storage = store_with_raw(raw)
    with pytest.raises(RuntimeError, match='no schema changes were committed') as caught:
        storage.migrate()
    assert raw.rolled_back and raw.closed
    assert_private(caught.value)


def test_cleanup_failure_does_not_replace_an_existing_domain_error():
    raw = BrokenCleanupConnection()
    storage = store_with_raw(raw)
    expected = Error(409, 'conflict', 'The resource changed')
    with pytest.raises(Error) as caught:
        with storage.transaction():
            raise expected
    assert caught.value is expected
    assert raw.rolled_back and raw.closed
    assert_private(caught.value)


@pytest.mark.parametrize('operation', ['connect', 'transaction'])
def test_connection_failure_is_private_and_runtime_outage_is_retryable(monkeypatch, operation):
    storage = postgres.PostgreSQLStorage.__new__(postgres.PostgreSQLStorage)
    storage._database_url = 'postgresql://fixture:fixture-password@localhost/fixture'

    def unavailable(**_parameters):
        raise psycopg.OperationalError(PRIVATE_MARKER)

    monkeypatch.setattr(postgres.psycopg, 'connect', unavailable)
    with pytest.raises(RuntimeError if operation == 'connect' else Error) as caught:
        if operation == 'connect':
            storage.connect()
        else:
            with storage.transaction():
                pytest.fail('A failed connection cannot yield a transaction')
    if operation == 'transaction':
        assert (caught.value.status, caught.value.code) == (503, 'storage_unavailable')
    assert_private(caught.value)
    assert 'fixture-password' not in ''.join(traceback.format_exception(caught.value))


@pytest.mark.parametrize('driver_error', [ValueError, UnicodeError, psycopg.DataError])
def test_binding_failure_is_private_even_when_cleanup_fails(monkeypatch, driver_error):
    raw = BrokenCleanupConnection()
    storage = store_with_raw(raw)

    class InvalidBindingCursor:
        def __init__(self, connection, **_kwargs):
            assert connection is raw

        def execute(self, _query, _values):
            raise driver_error(PRIVATE_MARKER)

    monkeypatch.setattr(postgres.psycopg, 'ClientCursor', InvalidBindingCursor)
    with pytest.raises(Error) as caught:
        with storage.transaction() as connection:
            connection.execute('SELECT ?', (PRIVATE_MARKER,))
    assert (caught.value.status, caught.value.code) == (400, 'invalid_input')
    assert raw.rolled_back and raw.closed
    assert_private(caught.value)


def test_qmark_translation_preserves_quotes_comments_and_literal_percent():
    query = '''SELECT ?, '?', 'it''s ? 50%', "q?%", 10 % 3 -- ? %
/* question ? and percentage % */ WHERE a=? AND b=?'''
    expected = '''SELECT %s, '?', 'it''s ? 50%%', "q?%%", 10 %% 3 -- ? %%
/* question ? and percentage %% */ WHERE a=%s AND b=%s'''
    assert postgres._parameters(query) == expected


def test_values_are_passed_separately_to_the_driver_without_interpolation(monkeypatch):
    captured = {}
    raw = object()

    class RecordingCursor:
        def __init__(self, connection, *, row_factory):
            assert connection is raw and row_factory is postgres._row_factory

        def execute(self, query, values):
            captured.update(query=query, values=values)
            return self

    monkeypatch.setattr(postgres.psycopg, 'ClientCursor', RecordingCursor)
    hostile = "'); DROP TABLE users; -- ? %s \\\nstill a value"
    result = postgres.Connection(raw).execute(
        "SELECT ?, ?, ?, ?, ?, '?', '%s'", (hostile, '100% literal ?', True, False, None))
    assert isinstance(result, RecordingCursor)
    assert captured['query'] == "SELECT %s, %s, %s, %s, %s, '?', '%%s'"
    assert captured['values'] == (hostile, '100% literal ?', 1, 0, None)
    assert hostile not in captured['query']


@pytest.mark.parametrize('value', ['\ud800', '\udfff', 'prefix\ud800suffix'])
def test_lone_surrogate_external_ids_fail_with_a_generic_input_error(value):
    with pytest.raises(Error) as caught:
        postgres.PostgreSQLStorage._encode_external_id(value)
    assert (caught.value.status, caught.value.code) == (400, 'invalid_input')
    assert caught.value.message == 'External user ID must contain valid Unicode'
    assert value not in str(caught.value)


def test_valid_opaque_ids_round_trip_without_normalization():
    values = ['zero\0byte', ' ID ', 'id', 'ID', '\u00e9', 'e\u0301', '\U0001f426']
    encoded = [postgres.PostgreSQLStorage._encode_external_id(value) for value in values]
    assert len(set(encoded)) == len(values)
    assert [postgres.PostgreSQLStorage._decode_external_id(value) for value in encoded] == values
