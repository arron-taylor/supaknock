"""Provider-neutral regression cases for shared relational operations."""
from contextlib import contextmanager
import sqlite3

from feedback.ai_domain import default_ai_settings
from test_ai_storage import ai_store, CAPS


def test_full_catalogue_pruning_preserves_exact_models_and_other_roles(ai_store, monkeypatch):
    storage, principal, project, _ = ai_store
    models = [f"model/{index}'%_" for index in range(1000)]
    config = storage.ai_configure(project, principal, 0,
                                  default_ai_settings() | {'model': models[0]},
                                  protected_key='synthetic-key')
    with storage.transaction(True) as connection:
        key = connection.execute('SELECT id FROM ai_provider_keys').fetchone()[0]
        for model in models:
            storage._ai_model_evidence(connection, key, 'generation', model, 1, CAPS, True)
        storage._ai_model_evidence(connection, key, 'decision', models[-1], 1, {}, True)

    original_transaction = storage.transaction

    @contextmanager
    def constrained_transaction(write=False):
        with original_transaction(write) as connection:
            if isinstance(connection, sqlite3.Connection):
                connection.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 999)
            yield connection

    monkeypatch.setattr(storage, 'transaction', constrained_transaction)
    retained = models[::3]
    storage.ai_store_provider_evidence(project, principal, config['version'],
                                       config['credential_version'], models[0], catalogue=retained)
    with storage.transaction() as connection:
        rows = connection.execute('SELECT role,model FROM ai_key_model_evidence WHERE key_id=?', (key,)).fetchall()
    assert {row['model'] for row in rows if row['role'] == 'generation'} == set(retained)
    assert {row['model'] for row in rows if row['role'] == 'decision'} == {models[-1]}

    current = storage.ai_settings(project, principal)
    storage.ai_store_provider_evidence(project, principal, current['version'],
                                       current['credential_version'], models[0], catalogue=[])
    with storage.transaction() as connection:
        rows = connection.execute('SELECT role,model FROM ai_key_model_evidence WHERE key_id=?', (key,)).fetchall()
    assert [tuple(row) for row in rows] == [('decision', models[-1])]
