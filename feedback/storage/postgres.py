"""PostgreSQL storage, also used for Supabase database connections.

The application keeps one writer at a time, like SQLite BEGIN IMMEDIATE. A
transaction-scoped advisory lock is acquired before authorization reads. This
preserves the existing atomic contracts without pretending this is a distributed
or horizontally scaled installation. All objects live in a private schema.
"""
from contextlib import contextmanager
from pathlib import Path
import hashlib
import math
import os
import re
import struct
import subprocess
import shutil
import unicodedata

import psycopg
from psycopg.conninfo import conninfo_to_dict

from feedback.domain import now
from feedback.errors import Error
from feedback.storage.relational import RelationalStorage

_LOCK = 0x535550414B4E4F43  # SupaKnock, scoped by PostgreSQL to this database.
_MIGRATIONS = Path(__file__).with_name('postgres_migrations')


class Row:
    """The small row interface used by shared operations: keys and index access."""
    def __init__(self, names, values):
        self._names, self._values = names, values

    def keys(self):
        return self._names

    def __getitem__(self, key):
        return self._values[self._names.index(key)] if isinstance(key, str) else self._values[key]

    def __iter__(self):
        return iter(self._values)

    def __len__(self):
        return len(self._values)


def _row_factory(cursor):
    names = [column.name for column in cursor.description] if cursor.description else []
    return lambda values: Row(names, values)


def _parameters(sql):
    """Translate parameter markers, never values or SQL syntax.

    Shared statements use qmarks. Quoted strings/identifiers and comments are
    preserved, and literal percent signs are escaped for psycopg binding.
    """
    parts = re.split(r"('(?:''|[^'])*'|\"(?:\"\"|[^\"])*\"|--[^\n]*|/\*.*?\*/)", sql, flags=re.S)
    return ''.join(part.replace('%', '%%') if index % 2 else part.replace('%', '%%').replace('?', '%s')
                   for index, part in enumerate(parts))


class Connection:
    def __init__(self, raw):
        self.raw = raw

    def execute(self, sql, parameters=None):
        if parameters is None:
            return self.raw.execute(sql)
        values = tuple(int(value) if isinstance(value, bool) else value for value in parameters)
        # Client-side binding gives PostgreSQL context for nullable/repeated
        # parameters (e.g. ? IS NULL) without string interpolation by our code.
        cursor = psycopg.ClientCursor(self.raw, row_factory=_row_factory)
        try:
            return cursor.execute(_parameters(sql), values)
        except (ValueError, UnicodeError, psycopg.DataError):
            raise Error(400, 'invalid_input', 'Text contains characters unsupported by this database') from None

    def commit(self):
        self.raw.commit()

    def rollback(self):
        # Preserve the sanitized error even if a dropped connection also makes
        # rollback fail. Driver exception context can contain private values.
        try:
            self.raw.rollback()
        except psycopg.Error:
            pass

    def close(self):
        try:
            self.raw.close()
        except psycopg.Error:
            pass


class PostgreSQLStorage(RelationalStorage):
    _ai_chunk_order_sql = 'k.rowid'
    _ai_crawl_order_sql = 'rowid'

    def __init__(self, database_url, base_url, *, initialize=True):
        self._database_url, self.base_url = database_url, base_url
        self.migrate(initialize=initialize)

    def connect(self):
        try:
            parameters = conninfo_to_dict(self._database_url)
            parameters.setdefault('connect_timeout', '10')
            parameters.setdefault('application_name', 'supaknock')
            raw = psycopg.connect(**parameters, autocommit=True,
                                 prepare_threshold=None, row_factory=_row_factory)
            return Connection(raw)
        except psycopg.Error:
            raise RuntimeError('Cannot connect to PostgreSQL; check the database configuration') from None

    @contextmanager
    def transaction(self, write=False):
        try:
            c = self.connect()
        except RuntimeError:
            raise Error(503, 'storage_unavailable', 'Database connection failed; try again') from None
        try:
            c.execute('BEGIN' if write else 'BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY')
            c.execute("SET LOCAL search_path = supaknock, pg_catalog")
            c.execute("SET LOCAL lock_timeout = '10s'")
            c.execute("SET LOCAL statement_timeout = '30s'")
            if write:
                c.execute('SELECT pg_advisory_xact_lock(?)', (_LOCK,))
            yield c
            c.commit()
        except psycopg.IntegrityError:
            c.rollback()
            raise Error(409, 'conflict', 'A unique identifier already exists or the operation violates a constraint') from None
        except psycopg.Error:
            c.rollback()
            # Driver errors can include SQL, parameters and connection details.
            raise Error(503, 'storage_unavailable', 'Database operation failed; try again') from None
        except BaseException:
            c.rollback()
            raise
        finally:
            c.close()

    def migrate(self, *, initialize=True):
        c = self.connect()
        try:
            c.execute('BEGIN')
            c.execute("SET LOCAL lock_timeout = '10s'")
            c.execute("SET LOCAL statement_timeout = '60s'")
            c.execute('SELECT pg_advisory_xact_lock(?)', (_LOCK,))
            exists = c.execute("SELECT to_regclass('supaknock.schema_migrations')").fetchone()[0]
            if not exists:
                if not initialize:
                    raise RuntimeError('No SupaKnock database found; check DATABASE_URL')
                if c.execute("SELECT 1 FROM pg_namespace WHERE nspname='supaknock'").fetchone():
                    raise RuntimeError('Unrecognized supaknock schema; refusing to initialize it')
                c.execute('CREATE SCHEMA supaknock')
                c.execute('REVOKE ALL ON SCHEMA supaknock FROM PUBLIC')
                c.execute('SET LOCAL search_path = supaknock, pg_catalog')
                c.execute('CREATE TABLE schema_migrations(version TEXT PRIMARY KEY, checksum TEXT NOT NULL, applied_at TEXT NOT NULL)')
            else:
                c.execute('SET LOCAL search_path = supaknock, pg_catalog')
            applied = {row['version']: row['checksum'] for row in c.execute('SELECT version,checksum FROM schema_migrations')}
            files = sorted(_MIGRATIONS.glob('*.sql'))
            if not files or applied.keys() - {file.name for file in files}:
                raise RuntimeError('Database is newer than this application; refusing to start')
            for file in files:
                digest = hashlib.sha256(file.read_bytes()).hexdigest()
                if file.name in applied:
                    if applied[file.name] != digest:
                        raise RuntimeError('Database migration checksum mismatch; refusing to start')
                    continue
                if not initialize:
                    raise RuntimeError('Database requires migrations; start the application before maintenance')
                c.execute(file.read_text())
                c.execute('INSERT INTO schema_migrations VALUES(?,?,?)', (file.name, digest, now()))
            # Supabase creates default grants for API roles. Do not expose this
            # private database domain through its REST API, even if an operator
            # later includes the schema in exposed-schema configuration.
            if initialize:
                for role in ('PUBLIC', 'anon', 'authenticated', 'service_role'):
                    if role != 'PUBLIC' and not c.execute('SELECT 1 FROM pg_roles WHERE rolname=?', (role,)).fetchone():
                        continue
                    c.execute(f'REVOKE ALL ON SCHEMA supaknock FROM {role}')
                    c.execute(f'REVOKE ALL ON ALL TABLES IN SCHEMA supaknock FROM {role}')
                    c.execute(f'REVOKE ALL ON ALL SEQUENCES IN SCHEMA supaknock FROM {role}')
                    c.execute(f'REVOKE ALL ON ALL FUNCTIONS IN SCHEMA supaknock FROM {role}')
            c.commit()
        except psycopg.Error:
            c.rollback()
            raise RuntimeError('PostgreSQL migration failed; no schema changes were committed') from None
        except BaseException:
            c.rollback()
            raise
        finally:
            c.close()

    @staticmethod
    def _encode_external_id(value):
        try:
            return value.encode('utf-8')
        except UnicodeError:
            raise Error(400, 'invalid_input', 'External user ID must contain valid Unicode') from None

    @staticmethod
    def _decode_external_id(value):
        return bytes(value).decode('utf-8')

    @staticmethod
    def _text_contains_sql(column):
        return f'position(lower(?) in lower({column}))>0'

    @staticmethod
    def _text_bytes_sql(column):
        return f'octet_length({column})'

    @staticmethod
    def _has_ai_turns(c):
        return c.execute("SELECT to_regclass('supaknock.ai_turns')").fetchone()[0] is not None

    @staticmethod
    def _words(text):
        text = ''.join(ch for ch in unicodedata.normalize('NFKD', text.casefold()) if not unicodedata.combining(ch))
        return set(re.findall(r'\w+', text, re.UNICODE))

    def _ai_knowledge_chunk_rows(self, c, project_id, terms, limit):
        from feedback.ai_domain import EMBEDDING_PROFILE
        query = self._ai_knowledge_chunks_sql() + ' ORDER BY s.id,k.ordinal,k.id LIMIT ?'
        rows = c.execute(query, (project_id, self._ai_json(self._ai_embedding_profile(c)), 10001 if terms else limit)).fetchall()
        if not terms:
            return rows
        if len(rows) > 10000:
            raise Error(409, 'knowledge_limit', 'This project exceeds the active-passage search limit')
        wanted = self._words(' '.join(terms))
        return [row for row in rows if wanted & self._words(row['title'] + ' ' + row['content'])][:limit]

    def _ai_search_candidates(self, c, project_id, query, values, take):
        from feedback.ai_domain import EMBEDDING_PROFILE
        rows = c.execute(self._ai_eligible_sql() + ' ORDER BY k.id LIMIT 10001',
                         (project_id, self._ai_json(self._ai_embedding_profile(c)))).fetchall()
        if len(rows) > 10000:
            raise Error(409, 'knowledge_limit', 'This project exceeds the active-passage search limit')
        wanted = self._words(' '.join(re.findall(r'\w+', query, re.UNICODE)[:32]))
        query_norm = math.sqrt(math.fsum(v*v for v in values))
        scored, lexical = [], []
        for row in rows:
            vector = struct.unpack('<' + str(len(values)) + 'f', row['vector'])
            norm = math.sqrt(math.fsum(v*v for v in vector))
            distance = 1 - math.fsum(a*b for a,b in zip(values, vector)) / (query_norm * norm) if query_norm and norm else 1.0
            scored.append((distance, row['id'], row))
            if wanted & self._words(row['title'] + ' ' + row['content']):
                lexical.append(row)
        return [item[2] for item in sorted(scored, key=lambda item: (item[0], item[1]))[:take]], lexical

    def backup(self, destination):
        destination = Path(destination)
        if destination.exists():
            raise RuntimeError('Backup destination must be a new file')
        with self.transaction() as c:
            major = int(c.execute('SHOW server_version_num').fetchone()[0]) // 10000
        bundled = Path(f'/opt/postgresql/{major}/bin/pg_dump')
        system = Path(f'/usr/lib/postgresql/{major}/bin/pg_dump')
        executable = str(bundled if bundled.is_file() else system) if bundled.is_file() or system.is_file() else shutil.which('pg_dump')
        if not executable:
            raise RuntimeError('PostgreSQL backup requires a matching-major pg_dump client')
        try:
            version = subprocess.run([executable, '--version'], capture_output=True, timeout=10)
            match = re.search(rb'PostgreSQL\) (\d+)\.', version.stdout)
            if version.returncode or not match or int(match.group(1)) != major:
                raise RuntimeError('PostgreSQL backup requires pg_dump from the same major version as the server')
        except (OSError, subprocess.TimeoutExpired):
            raise RuntimeError('Cannot check PostgreSQL backup client version') from None
        params = conninfo_to_dict(self._database_url)
        env = {key: value for key, value in os.environ.items() if not key.startswith('PG')}
        for name in ('host', 'port', 'user', 'password', 'dbname', 'sslmode', 'sslrootcert', 'sslcert', 'sslkey', 'channel_binding', 'connect_timeout', 'application_name'):
            if name in params:
                env[{'dbname': 'PGDATABASE', 'application_name': 'PGAPPNAME'}.get(name, 'PG' + name.upper())] = params[name]
        env.setdefault('PGCONNECT_TIMEOUT', '10')
        created = False
        try:
            descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            created = True
            with os.fdopen(descriptor, 'wb') as output:
                result = subprocess.run([executable, '--format=custom', '--schema=supaknock', '--no-owner', '--no-acl'],
                                        env=env, stdout=output, stderr=subprocess.PIPE, timeout=300)
            if result.returncode:
                raise RuntimeError('PostgreSQL backup failed; check connectivity and pg_dump version')
        except (OSError, subprocess.TimeoutExpired):
            if created:
                destination.unlink(missing_ok=True)
            raise RuntimeError('PostgreSQL backup requires pg_dump and a reachable database') from None
        except BaseException:
            if created:
                destination.unlink(missing_ok=True)
            raise
