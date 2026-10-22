"""Bounded local knowledge indexing, hybrid retrieval and original citations."""
import re
import threading
import time

from feedback.ai_runtime import EmbeddingRuntimeError, validate_vectors
from feedback.errors import Error

from feedback.ai_chunking import MAX_CHUNKS, MAX_PROMPT_BYTES, chunk_markdown, split_chunk, plain_label

MAX_INDEX_SECONDS = 600
MAX_RETRIEVAL_CANDIDATES = 20


def _website_excerpt_keys(row):
    """Comparison keys only: never rewrite an original excerpt or its citation.

    Markdown uploads and code-shaped text keep their existing ranking. Website
    extraction can repeat a guide card on many pages, sometimes carrying the
    next card's label. Only the same heading and complete summary are grouped;
    differing sentences remain independent evidence.
    """
    content = row['content']
    if row['kind'] != 'website' or re.search(r'(?m)^(?: {4}|\t)|`|~{3}', content):
        return None, None
    scope = (row['source_id'], row['revision_id'])
    lines = [line.strip() for line in content.splitlines() if line.strip()]
    normalized = [re.sub(r'^#{1,6}[ \t]+', '', line) for line in lines]
    exact = scope + (row['title'], ' '.join(' '.join(normalized).split()))
    introduction = None
    if len(lines) == 3 and re.match(r'^#{1,6}[ \t]+', lines[0]):
        summary, label = lines[1:]
        if (len(summary) <= 280 and summary.endswith(('.', '!', '?', '。', '！', '？'))
                and len(label) <= 80 and len(label.split()) <= 12
                and all(character.isalnum() or character.isspace() or character in '&/-'
                        for character in label)):
            introduction = scope + (normalized[0], ' '.join(summary.split()))
    return exact, introduction


def _select_passages(rows, limit):
    """Stable diversity within one bounded, already-authorized candidate pool.

    Exact website duplicates have one representative. Repeated introductions
    are deferred, not discarded: an unfamiliar trailing label could be useful
    evidence, so it remains available when distinct passages leave room.
    """
    selected, deferred, exact_seen, introductions_seen = [], [], set(), set()
    for row in rows:
        exact, introduction = _website_excerpt_keys(row)
        if exact is not None:
            if exact in exact_seen:
                continue
            exact_seen.add(exact)
        if introduction is not None and introduction in introductions_seen:
            deferred.append(row)
        else:
            selected.append(row)
            if introduction is not None:
                introductions_seen.add(introduction)
    return (selected + deferred)[:limit]


def _passage(row):
    url = None
    if row['kind'] == 'website':
        from feedback.ai_crawler import CrawlError, canonical_url
        try:
            url = canonical_url(row['page_url'])
            if url != row['page_url']:
                raise CrawlError('invalid_url')
        except CrawlError:
            raise Error(409, 'invalid_citation', 'A source link is no longer valid') from None
    result = {k: row[k] for k in ('id', 'source_id', 'revision_id', 'kind')}
    result.update(title=plain_label(row['title'] or row['source_name']), excerpt=row['content'], url=url)
    if 'score' in row:
        result['score'] = row['score']
    return result


class RetrievalService:
    def __init__(self, storage, installation):
        self.storage, self.installation = storage, installation

    @staticmethod
    def _query(query, limit):
        if not isinstance(query, str) or not query.strip() or len(query) > 1000 or type(limit) is not int or not 1 <= limit <= 20:
            raise Error(400, 'invalid_input', 'Use a nonempty question of at most 1,000 characters and at most 20 passages')
        try:
            if len(('task: search result | query: ' + query).encode('utf-8')) > MAX_PROMPT_BYTES:
                raise ValueError()
        except (UnicodeError, ValueError):
            raise Error(400, 'invalid_input', 'The question is too large for knowledge search') from None

    def _embed(self, query):
        try:
            return getattr(self.installation, 'validate', validate_vectors)(self.installation.embed_queries([query]), 1)[0]
        except EmbeddingRuntimeError as error:
            if error.code in ('input_too_long', 'invalid_input'):
                raise Error(400, 'invalid_input', 'Shorten the question to fit the embedding model input limit') from None
            raise Error(503, 'ai_search_unavailable', 'The embedding provider is unavailable or busy') from None

    def retrieve(self, project, principal, query, limit=6):
        self._query(query, limit)
        context = self.storage.ai_retrieval_context(project, principal)
        vector = self._embed(query)
        result = self.storage.ai_retrieve(context['project_id'], principal, query, vector, context['knowledge_version'], MAX_RETRIEVAL_CANDIDATES)
        result['passages'] = [_passage(row) for row in _select_passages(result['passages'], limit)]
        return result

    def retrieve_job(self, job_id, token, query, limit=6):
        self._query(query, limit)
        material = self.storage.ai_job_material(job_id, token)
        if not material or material['job']['kind'] not in ('turn', 'preview', 'sections'):
            raise Error(409, 'conflict', 'This answer is no longer authorized')
        self.storage.ai_topic_require_allow(job_id, token)
        result = self.storage.ai_retrieve_job(job_id, token, query, self._embed(query), MAX_RETRIEVAL_CANDIDATES)
        result['passages'] = [_passage(row) for row in _select_passages(result['passages'], limit)]
        return result

    def validate_citations(self, project, principal, knowledge_version, chunk_ids):
        return [_passage(row) for row in self.storage.ai_validate_citations(project, principal, knowledge_version, chunk_ids)]


class IngestionWorker:
    def __init__(self, storage, installation):
        self.storage, self.installation = storage, installation
        self._stop, self._wake = threading.Event(), threading.Event()
        self._thread = None
        self._work_lock = threading.Lock()

    def _checkpoint(self, job, deadline):
        if time.monotonic() >= deadline:
            raise EmbeddingRuntimeError('timeout')
        if self._stop.is_set():
            raise EmbeddingRuntimeError('closed')
        if not self.storage.ai_renew_job(job['id'], job['lease_token'], lease_seconds=300):
            raise Error(409, 'conflict', 'The knowledge worker no longer owns this import')
        if not self.installation.available():
            raise EmbeddingRuntimeError('unavailable')

    def _embed_batch(self, job, rows, deadline):
        self._checkpoint(job, deadline)
        # Recursive descendants share their original ordinal. Use temporary
        # unique lookup positions so each exact span can reuse its own vector.
        lookup = [row | {'ordinal': index} for index, row in enumerate(rows)]
        reused = self.storage.ai_reusable_chunks(job['id'], job['lease_token'], lookup)
        missing = [(index, row) for index, row in enumerate(rows) if index not in reused]
        if not missing:
            return [row | {'vector': reused[index]} for index, row in enumerate(rows)]
        try:
            vectors = getattr(self.installation, 'validate', validate_vectors)(self.installation.embed_documents([row['content'] for _, row in missing], titles=[row['title'] for _, row in missing]), len(missing))
            reused.update((index, vector) for (index, _), vector in zip(missing, vectors))
            return [row | {'vector': reused[index]} for index, row in enumerate(rows)]
        except EmbeddingRuntimeError as error:
            if error.code != 'input_too_long':
                raise
            # The real tokenizer is authoritative. Normalization can expand some
            # Unicode text beyond a byte heuristic, so split exact original spans
            # until the runtime accepts every complete formatted prompt.
            if len(rows) > 1:
                middle = len(rows) // 2
                pieces = self._embed_batch(job, rows[:middle], deadline) + self._embed_batch(job, rows[middle:], deadline)
            else:
                split = list(split_chunk(rows[0]))
                for part in split:
                    part['ordinal'] = rows[0]['ordinal']
                pieces = self._embed_batch(job, split, deadline)
            if len(pieces) > MAX_CHUNKS:
                raise Error(400, 'knowledge_limit', 'This source has too many passages to index')
            return pieces

    def run_once(self):
        if not self.installation.available() or not self._work_lock.acquire(blocking=False):
            return False
        job = None
        deadline = time.monotonic() + MAX_INDEX_SECONDS
        try:
            job = self.storage.ai_claim_job(time.time(), 300, kinds=('ingest',))
            if not job:
                return False
            token = job['lease_token']
            material = self.storage.ai_job_material(job['id'], token)
            if not material:
                return False
            document = material.get('document')
            if not document:
                self.storage.ai_finish_job(job['id'], token, 'failed', 'source_failed')
                return True
            chunks = []
            for page in material.get('pages') or [{'title': document['title'], 'content': document['content'], 'url': None}]:
                for item in chunk_markdown(page['content'], page['title'], page['url']):
                    item['ordinal'] = len(chunks)
                    chunks.append(item)
            if len(chunks) > MAX_CHUNKS:
                raise Error(400, 'knowledge_limit', 'This source has too many passages to index')
            prepared = []
            for offset in range(0, len(chunks), 4):
                batch = chunks[offset:offset + 4]
                prepared.extend(self._embed_batch(job, batch, deadline))
                if len(prepared) > MAX_CHUNKS:
                    raise Error(400, 'knowledge_limit', 'This source has too many passages to index')
            self._checkpoint(job, deadline)
            for ordinal, row in enumerate(prepared):
                row['ordinal'] = ordinal
            principal = job['authority']['principal']
            revision = self.storage.ai_revision_stage(job['project_id'], principal, job['resource_id'], job['authority']['source_version'], document['title'], document['content'], len(prepared), job_id=job['id'], lease_token=token)
            for offset in range(0, len(prepared), 100):
                self._checkpoint(job, deadline)
                values = [{k: row[k] for k in ('ordinal', 'title', 'content', 'location', 'vector')} for row in prepared[offset:offset + 100]]
                self.storage.ai_revision_chunks(job['project_id'], principal, revision['id'], values, lease_token=token)
            self.storage.ai_revision_publish(job['project_id'], principal, revision['id'], lease_token=token)
            return True
        except EmbeddingRuntimeError as error:
            if job:
                if error.code in ('busy', 'unavailable', 'closed'):
                    self.storage.ai_defer_job(job['id'], job['lease_token'], time.time() + 5)
                else:
                    self.storage.ai_finish_job(job['id'], job['lease_token'], 'queued' if job['attempt_count'] < 3 else 'failed', 'embedding_failed', retry_at=time.time() + 10)
            return False
        except Error:
            if job:
                self.storage.ai_finish_job(job['id'], job['lease_token'], 'failed', 'source_failed')
            return False
        finally:
            self._work_lock.release()

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        def loop():
            while not self._stop.is_set():
                try:
                    self.run_once()
                except Exception:
                    pass  # Never log source text, provider exceptions or secrets.
                self._wake.wait(1)
                self._wake.clear()
        self._thread = threading.Thread(target=loop, name='knowledge-ingestion', daemon=True)
        self._thread.start()

    def wake(self):
        self._wake.set()

    def stop(self):
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=5)
