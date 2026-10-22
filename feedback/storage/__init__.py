from feedback.storage.base import Storage
from feedback.storage.sqlite import SQLiteStorage
from feedback.config import postgres_url


def open_storage(config, *, initialize=True) -> Storage:
    if config.database_provider == 'sqlite':
        if not initialize and not config.database_path.is_file():
            raise RuntimeError('Database does not exist; check DATABASE_URL')
        return SQLiteStorage(config.database_path, config.base_url)
    if config.database_provider in ('postgres', 'postgresql', 'supabase'):
        from feedback.storage.postgres import PostgreSQLStorage
        return PostgreSQLStorage(postgres_url(config.database_url, config.database_provider), config.base_url,
                                 initialize=initialize)
    raise RuntimeError('Unsupported database provider; choose sqlite, postgres or supabase')
