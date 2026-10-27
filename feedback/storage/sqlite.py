"""SQLite connections, migrations, backup and local search primitives."""
from contextlib import contextmanager
import os
from pathlib import Path
import re
import sqlite3
import struct
import time

from feedback.ai_domain import EMBEDDING_PROFILE
from feedback.domain import now
from feedback.errors import Error
from feedback.storage.relational import RelationalStorage


class SQLiteStorage(RelationalStorage):
    _ai_chunk_order_sql = 'k.rowid'
    _ai_crawl_order_sql = 'rowid'

    def __init__(self, path: Path, base_url: str):
        self.path, self.base_url = path, base_url
        path.parent.mkdir(parents=True, exist_ok=True)
        self.migrate()

    def connect(self):
        c = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys=ON")
        c.execute("PRAGMA busy_timeout=10000")
        c.execute("PRAGMA synchronous=FULL")
        return c

    @contextmanager
    def transaction(self, write=False):
        c = self.connect()
        try:
            c.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield c
            c.commit()
        except sqlite3.IntegrityError as exc:
            c.rollback()
            raise Error(409, "conflict", "A unique identifier already exists or the operation violates a constraint") from exc
        except BaseException:
            c.rollback()
            raise
        finally:
            c.close()

    def migrate(self):
        c = self.connect()
        try:
            # Initial WAL conversion can return SQLITE_BUSY immediately even with
            # busy_timeout set. Concurrent fresh startups must serialize this step
            # too, before the migration transaction can acquire its exclusive lock.
            deadline = time.monotonic() + 10
            while True:
                try:
                    mode = c.execute("PRAGMA journal_mode=WAL").fetchone()[0]
                    break
                except sqlite3.OperationalError as exc:
                    if (getattr(exc, 'sqlite_errorcode', 0) & 255) not in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED) or time.monotonic() >= deadline:
                        raise
                    time.sleep(.025)
            if mode != "wal":
                raise RuntimeError("SQLite WAL mode could not be enabled; use local persistent storage")
            # SQLite table reconstruction requires FK enforcement off before BEGIN.
            # This migration-only connection serializes writes and validates every
            # reference before commit; runtime connections always enforce FKs.
            c.execute("PRAGMA foreign_keys=OFF")
            c.execute("BEGIN EXCLUSIVE")
            c.execute("CREATE TABLE IF NOT EXISTS schema_migrations(version TEXT PRIMARY KEY, applied_at TEXT NOT NULL)")
            applied = {x[0] for x in c.execute("SELECT version FROM schema_migrations")}
            files = sorted(Path(__file__).with_name("migrations").glob("*.sql"))
            if applied - {x.name for x in files}:
                raise RuntimeError("Database is newer than this application; refusing to start")
            for file in files:
                if file.name in applied:
                    continue
                statement = ""
                for line in file.read_text().splitlines(keepends=True):
                    statement += line
                    if sqlite3.complete_statement(statement):
                        c.execute(statement)
                        statement = ""
                if statement.strip():
                    raise RuntimeError(f"Incomplete migration: {file.name}")
                c.execute("INSERT INTO schema_migrations VALUES (?,?)", (file.name, now()))
            if c.execute("PRAGMA foreign_key_check").fetchone():
                raise RuntimeError("Migration foreign-key verification failed; refusing to start")
            c.commit()
        except BaseException:
            c.rollback()
            raise
        finally:
            c.close()

    def backup(self, destination):
        destination = Path(destination)
        if destination.resolve() == self.path.resolve() or destination.exists():
            raise RuntimeError("Backup destination must be a new file")
        try:
            os.close(os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
        except FileExistsError:
            raise RuntimeError("Backup destination must be a new file") from None
        source = target = None
        try:
            source, target = self.connect(), sqlite3.connect(destination)
            source.backup(target)
        except BaseException:
            if target is not None:
                target.close()
                target = None
            destination.unlink(missing_ok=True)
            raise
        finally:
            if source is not None:
                source.close()
            if target is not None:
                target.close()

    @staticmethod
    def _has_ai_turns(c):
        # Existing storage methods are also used while testing upgrades from the
        # actual pre-AI schema. Never require a new table to seed that history.
        return c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='ai_turns'").fetchone() is not None

    @staticmethod
    def _text_contains_sql(column):
        return f'instr(lower({column}),lower(?))>0'

    @staticmethod
    def _text_bytes_sql(column):
        return f'length(CAST({column} AS BLOB))'

    def _ai_knowledge_chunk_rows(self, c, project_id, terms, limit):
        params = [project_id, self._ai_json(self._ai_embedding_profile(c))]
        extra = ''
        if terms is not None:
            extra = ' AND k.rowid IN (SELECT rowid FROM ai_chunks_fts WHERE ai_chunks_fts MATCH ?)'
            params.append(' OR '.join('"' + term + '"' for term in terms))
        params.append(limit)
        return c.execute(self._ai_knowledge_chunks_sql() + extra + ' ORDER BY s.id,k.ordinal,k.id LIMIT ?', params)

    def _ai_search_candidates(self, c, project_id, query, values, take):
        try:
            import sqlite_vec
            c.enable_load_extension(True)
            try:
                sqlite_vec.load(c)
            finally:
                c.enable_load_extension(False)
        except (ImportError, AttributeError, OSError, sqlite3.Error):
            raise Error(503, 'ai_search_unavailable', 'Local knowledge search is unavailable') from None
        # Materialize the authorized corpus before computing either ranking. No
        # global nearest-neighbor LIMIT can hide an eligible project passage.
        params = (project_id, self._ai_json(self._ai_embedding_profile(c)))
        semantic = c.execute('WITH eligible AS MATERIALIZED (' + self._ai_eligible_sql() + ') SELECT *,vec_distance_cosine(vector,?) distance FROM eligible ORDER BY distance,id LIMIT ?', (*params, struct.pack('<' + str(len(values)) + 'f', *values), take)).fetchall()
        terms = re.findall(r'\w+', query, re.UNICODE)[:32]
        lexical = []
        if terms:
            literal = ' OR '.join('"' + term + '"' for term in terms)
            lexical = c.execute('WITH eligible AS MATERIALIZED (' + self._ai_eligible_sql(False) + ') SELECT * FROM eligible WHERE chunk_rowid IN (SELECT rowid FROM ai_chunks_fts WHERE ai_chunks_fts MATCH ?) LIMIT 10001', (*params, literal)).fetchall()
        return semantic, lexical
