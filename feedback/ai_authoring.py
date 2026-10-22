"""Private reviewable knowledge drafts and tools-disabled administrator previews."""
import asyncio
import threading
import time

from feedback.ai_providers import ProviderError
from feedback.ai_key_pool import ProviderPoolWait
from feedback.ai_retrieval import RetrievalService, _passage
from feedback.errors import Error


class _Stopped(Exception):
    pass


class AuthoringWorker:
    """One durable administrative generation at a time, separate from chat turns."""
    def __init__(self, storage, generation):
        self.storage, self.generation = storage, generation
        self._stop, self._wake = threading.Event(), threading.Event()
        self._lock, self._thread = threading.Lock(), None

    async def _generate(self, job, material, *, passages=None, batch_index=None):
        mode = job['kind']
        question = material['run']['query'] if mode == 'preview' else 'Organize the supplied original support passages into useful sections and frequently asked questions.'
        if mode == 'sections':
            question += ' This is one small batch. Return at most three concise entries, combining related facts where useful.'
        task = asyncio.create_task(self.generation.generate(job['id'], job['lease_token'], question, mode=mode,
            passages=passages, call_index=batch_index or 0))
        try:
            while not task.done():
                if self._stop.is_set():
                    raise _Stopped()
                await asyncio.wait({task}, timeout=.1)
            if self._stop.is_set():
                raise _Stopped()
            return await task
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    def run_once(self):
        if self._stop.is_set() or not self._lock.acquire(blocking=False):
            return False
        job = None
        try:
            job = self.storage.ai_claim_job(time.time(), lease_seconds=300, kinds=('preview', 'sections'))
            if not job:
                return False
            material = self.storage.ai_job_material(job['id'], job['lease_token'])
            if not material or not material.get('run'):
                if material:
                    self.storage.ai_finish_job(job['id'], job['lease_token'], 'failed', 'source_failed')
                return True
            if job['kind'] == 'sections' and material['run'].get('coverage', {}).get('batch_size'):
                coverage = material['run']['coverage']
                for index in range(coverage['completed_batches'], coverage['total_batches']):
                    passages = [_passage(row) for row in material['source_passages'][index * coverage['batch_size']:(index + 1) * coverage['batch_size']]]
                    result = asyncio.run(self._generate(job, material, passages=passages, batch_index=index))
                    ids = list(dict.fromkeys(cid for section in result['sections'] for cid in section['citation_ids']))
                    if not self.storage.ai_authoring_finish(job['id'], job['lease_token'], result, ids, batch_index=index):
                        break
                    if (index - coverage['completed_batches'] + 1) % 4 == 0 and index + 1 < coverage['total_batches']:
                        self.storage.ai_authoring_continue(job['id'], job['lease_token'])
                        break
                return True
            passages = [_passage(row) for row in material['source_passages']] if job['kind'] == 'sections' else None
            result = asyncio.run(self._generate(job, material, passages=passages))
            ids = result['citation_ids'] if job['kind'] == 'preview' else list(dict.fromkeys(cid for section in result['sections'] for cid in section['citation_ids']))
            self.storage.ai_authoring_finish(job['id'], job['lease_token'], result, ids)
            return True
        except _Stopped:
            # Keep the recoverable lease. No provider billing or rollback claim
            # is made for an interrupted dispatched request.
            return False
        except ProviderPoolWait as wait:
            if job:
                self.storage.ai_provider_defer(job['id'], job['lease_token'], wait.retry_at)
            return bool(job)
        except ProviderError as error:
            safe = {'rate_limited': 'rate_limited', 'timeout': 'timeout', 'invalid_response': 'invalid_response',
                    'incomplete_response': 'incomplete_response', 'request_rejected': 'request_rejected',
                    'tool_not_enabled': 'invalid_response'}.get(error.code, 'unavailable')
            retry = error.retryable and job['attempt_count'] < 3
            delay = min(86400, max(1, error.retry_after if error.retry_after is not None else 10 * job['attempt_count']))
            if retry:
                self.storage.ai_provider_defer(job['id'], job['lease_token'], time.time() + delay, error=safe)
            else:
                self.storage.ai_finish_job(job['id'], job['lease_token'], 'failed', safe)
            return True
        except Error as error:
            if job:
                safe = 'budget_exhausted' if error.code in ('budget_exhausted', 'ai_budget_exhausted') else 'source_failed'
                retry = error.status == 503 and job['attempt_count'] < 3
                self.storage.ai_finish_job(job['id'], job['lease_token'], 'queued' if retry else 'failed', 'unavailable' if retry else safe, retry_at=time.time() + 10 if retry else None)
            return bool(job)
        except Exception:
            if job:
                self.storage.ai_finish_job(job['id'], job['lease_token'], 'failed', 'invalid_response')
            return bool(job)
        finally:
            self._lock.release()

    def wake(self):
        self._wake.set()

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        def loop():
            while not self._stop.is_set():
                try:
                    busy = self.run_once()
                except Exception:
                    busy = False  # No source, prompt or provider exception logs.
                if not busy:
                    self._wake.wait(1)
                    self._wake.clear()
        self._thread = threading.Thread(target=loop, name='knowledge-authoring', daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=5)


class AuthoringService:
    def __init__(self, storage, worker, settings):
        self.storage, self.worker, self.settings = storage, worker, settings

    @staticmethod
    def _admin(principal):
        if not isinstance(principal, dict) or principal.get('kind') != 'admin' or not principal.get('user_id') or not principal.get('session_id'):
            raise Error(403, 'forbidden', 'Native administrator access required')

    @staticmethod
    def _section(value):
        return value | {'citations': [_passage(row) for row in value.get('citations', [])]}

    @classmethod
    def _run(cls, value):
        return value | {'citations': [_passage(row) for row in value.get('citations', [])],
                        'sections': [cls._section(section) for section in value.get('sections', [])]}

    def preview(self, project, principal, client_id, version, question):
        self._admin(principal)
        RetrievalService._query(question, 6)
        result = self.storage.ai_authoring_begin(project, principal, client_id, 'preview', version, query=question)
        self.worker.wake()
        return self._run(result)

    def sections(self, project, principal, client_id, version, source_id, source_version, page_ids=None):
        self._admin(principal)
        result = self.storage.ai_authoring_begin(project, principal, client_id, 'sections', version, source_id=source_id, source_version=source_version, page_ids=page_ids)
        self.worker.wake()
        return self._run(result)

    def runs(self, project, principal, kind=None, limit=25, offset=0):
        self._admin(principal)
        result = self.storage.ai_authoring_runs(project, principal, kind, limit, offset)
        return result | {'items': [self._run(row) for row in result['items']]}

    def run(self, project, principal, run_id):
        self._admin(principal)
        return self._run(self.storage.ai_authoring_run(project, principal, run_id))

    def cancel(self, project, principal, run_id, version):
        self._admin(principal)
        result = self.storage.ai_authoring_cancel(project, principal, run_id, version)
        self.worker.wake()
        return self._run(result)

    def retry(self, project, principal, run_id, version, client_id):
        self._admin(principal)
        result = self.storage.ai_authoring_retry(project, principal, run_id, version, client_id)
        self.worker.wake()
        return self._run(result)

    def edit(self, project, principal, section_id, version, data):
        self._admin(principal)
        return self._section(self.storage.ai_section_update(project, principal, section_id, version, data))

    def drafts(self, project, principal, query='', limit=25, offset=0):
        self._admin(principal)
        result = self.storage.ai_sections_list(project, principal, query, limit, offset)
        return result | {'items': [self._section(row) for row in result['items']]}

    def publish(self, project, principal, section_id, version):
        self._admin(principal)
        return self.storage.ai_section_publish(project, principal, section_id, version)

    def delete(self, project, principal, section_id, version):
        self._admin(principal)
        return self._section(self.storage.ai_section_delete(project, principal, section_id, version))

    def personality(self, project, principal):
        self._admin(principal)
        value = self.settings.read(project, principal)
        return {'version': value['version'], 'filename': 'soul.md', 'content': value['settings']['soul']}

    def save_personality(self, project, principal, version, content):
        self._admin(principal)
        try:
            if not isinstance(content, str) or len(content) > 20000 or len(content.encode('utf-8')) > 80000 or any(ord(c) < 32 and c not in '\n\r\t' for c in content):
                raise ValueError()
        except (ValueError, UnicodeError):
            raise Error(400, 'invalid_input', 'Use UTF-8 personality Markdown of at most 20,000 characters without control characters') from None
        current = self.settings.read(project, principal)
        if type(version) is not int or version != current['version']:
            raise Error(409, 'conflict', 'AI settings changed; reload before editing the personality')
        value = self.settings.save(project, principal, version, current['settings'] | {'soul': content})
        return {'version': value['version'], 'filename': 'soul.md', 'content': value['settings']['soul']}
