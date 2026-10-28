"""Recoverable AI reply worker. The shared generator cannot commit messages."""
import asyncio
import threading
import time

from feedback.ai_providers import ProviderError
from feedback.ai_key_pool import ProviderPoolWait
from feedback.errors import Error


class AIChatWorker:
    def __init__(self, storage, generator):
        self.storage, self.generator = storage, generator
        self._stop, self._wake = threading.Event(), threading.Event()
        self._work_lock, self._state_lock = threading.Lock(), threading.Lock()
        self._thread = self._loop = self._task = None

    async def _generate(self, job, material):
        with self._state_lock:
            self._loop, self._task = asyncio.get_running_loop(), asyncio.current_task()
            if self._stop.is_set():
                self._task.cancel()
        try:
            return await self.generator.generate(job['id'], job['lease_token'], material['question'],
                                                 history=material['messages'], mode='answer')
        finally:
            with self._state_lock:
                self._loop = self._task = None

    def _failure(self, job, code, retryable, retry_after=None):
        state = 'queued' if retryable and job['attempt_count'] < job['max_attempts'] else 'failed'
        delay = min(60, 5 * 2 ** job['attempt_count'])
        if type(retry_after) in (int, float) and 0 <= retry_after <= 86400:
            delay = max(delay, retry_after)
        if state == 'queued':
            self.storage.ai_provider_defer(job['id'], job['lease_token'], time.time() + delay, error=code)
        else:
            self.storage.ai_finish_job(job['id'], job['lease_token'], state, code)

    def run_once(self):
        if self._stop.is_set() or not self._work_lock.acquire(blocking=False):
            return False
        job = None
        try:
            job = self.storage.ai_claim_job(time.time(), 120, kinds=('turn',))
            if not job:
                return False
            material = self.storage.ai_job_material(job['id'], job['lease_token'])
            if not material:
                return False
            result = asyncio.run(self._generate(job, material))
            if self._stop.is_set():
                raise asyncio.CancelledError()
            policy = result.get('status') if result.get('status') in ('refuse', 'clarify', 'greeting', 'unavailable') else None
            return self.storage.ai_commit_reply(job['id'], job['lease_token'], result['body'],
                result['citation_ids'], result['knowledge_version'], policy_outcome=policy, answer_outcome=result.get('outcome', result.get('status', 'answered'))) is not None
        except asyncio.CancelledError:
            # A possibly dispatched request remains charged. The bounded retry
            # retains its original authority and can never resurrect on off/on.
            if job:
                self._failure(job, 'unavailable', True)
            return False
        except ProviderPoolWait as wait:
            if job:
                self.storage.ai_provider_defer(job['id'], job['lease_token'], wait.retry_at)
            return False
        except ProviderError as error:
            if job:
                code = error.code if error.code in ('invalid_request', 'invalid_response', 'incomplete_response', 'request_rejected', 'rate_limited', 'timeout') else 'unavailable'
                self._failure(job, code, error.retryable, error.retry_after)
            return False
        except Error as error:
            if job:
                if error.code == 'budget_exhausted':
                    self._failure(job, 'budget_exhausted', False)
                elif error.status == 409:
                    self.storage.ai_finish_job(job['id'], job['lease_token'], 'cancelled', 'cancelled')
                else:
                    self._failure(job, 'unavailable' if error.status >= 500 else 'invalid_response', error.status >= 500)
            return False
        except Exception:
            if job:
                self._failure(job, 'unavailable', True)
            return False
        finally:
            if job:self.storage.mcp_clear_attempt(job['id'],job['lease_token'])
            self._work_lock.release()

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        def loop():
            while not self._stop.is_set():
                self._wake.clear()
                try:
                    if self.run_once():
                        continue  # Drain ready replies without an artificial pause.
                except Exception:
                    pass  # Durable work recovers; never log source or provider data.
                self._wake.wait(1)
        self._thread = threading.Thread(target=loop, name='ai-chat-replies', daemon=True)
        self._thread.start()

    def wake(self):
        self._wake.set()

    def stop(self):
        self._stop.set()
        self._wake.set()
        with self._state_lock:
            if self._loop and self._task:
                self._loop.call_soon_threadsafe(self._task.cancel)
        if self._thread:
            self._thread.join(timeout=5)
