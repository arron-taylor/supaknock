"""Explicit, recoverable installation of the one curated local embedding model.

The database owns authorization and leases; this worker owns only UUID-named
directories below the installation's model cache. No network runs on startup.
"""
from concurrent.futures import TimeoutError as FutureTimeout
import os
from pathlib import Path
import re
import shutil
import threading
import time
from uuid import UUID, uuid4

from feedback.ai_domain import EMBEDDING_PROFILE
from feedback.ai_download import ModelDownloadError, model_manifest, supervised_download, verify_model
from feedback.ai_runtime import EmbeddingRuntimeError, LocalEmbeddingRuntime, validate_vectors
from feedback.errors import Error


_OWNED = re.compile(r'(?:embeddinggemma-[0-9a-f-]{36}|stage-[0-9a-f-]{36}-[0-9a-f-]{36})\Z')


def _sync_directory(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class EmbeddingInstallation:
    def __init__(self, storage, vault, config, *, downloader=supervised_download,
                 verifier=verify_model, runtime_factory=LocalEmbeddingRuntime):
        self.storage, self.vault = storage, vault
        self.root = config.local_data_dir.resolve() / 'models'
        self.downloader, self.verifier, self.runtime_factory = downloader, verifier, runtime_factory
        self.total_bytes = sum(item['size'] for item in model_manifest()['files'])
        self._stop, self._wake = threading.Event(), threading.Event()
        self._verifying = False
        self._work_lock, self._runtime_lock = threading.Lock(), threading.RLock()
        self._thread = self._runtime = self._cache_id = None
        self._verified = None
        self.reconcile()

    def _path(self, cache_id):
        try:
            if str(UUID(cache_id)) != cache_id:
                raise ValueError()
        except (ValueError, TypeError, AttributeError):
            raise EmbeddingRuntimeError('unavailable') from None
        if self.root.is_symlink():
            raise EmbeddingRuntimeError('unavailable')
        return self.root / ('embeddinggemma-' + cache_id)

    def _close_runtime(self):
        with self._runtime_lock:
            if self._runtime is not None:
                self._runtime.close()
            self._runtime = self._cache_id = None

    def reconcile(self):
        """Restore without a cache never resumes a pending external download."""
        row = self.storage.ai_runtime_local()
        if row['action'] == 'download' and row['operation_id']:
            marker = self.root / ('.download-' + row['operation_id'])
            if marker.is_symlink() or not marker.is_file():
                self.storage.ai_runtime_abandon_download(row['version'])
                row = self.storage.ai_runtime_local()
        if not self.root.is_dir() or self.root.is_symlink():
            if row['active_manifest'] or row['operation_id']:
                self.storage.ai_runtime_unavailable(row['version'], cancel_operation=True)
            self._verified = None
            return
        if row['active_manifest']:
            try:
                if row['active_manifest'] != EMBEDDING_PROFILE:
                    raise EmbeddingRuntimeError('unavailable')
                self.verifier(self._path(row['active_cache_id']))
                self._verified = row['active_cache_id']
            except Exception:
                self.storage.ai_runtime_unavailable(row['version'], cancel_operation=True)
                self._verified = None

    def status(self, principal):
        row = self.storage.ai_runtime(principal)
        if row['active_cache_id']:
            try:
                path = self._path(row['active_cache_id'])
                # Full hashes are checked at startup and before activation.
                if not path.is_dir() or path.is_symlink():
                    raise EmbeddingRuntimeError('unavailable')
            except EmbeddingRuntimeError:
                self.storage.ai_runtime_unavailable(row['version'], cancel_operation=True)
                self._close_runtime()
                self._verified = None
                row = self.storage.ai_runtime(principal)
        return row | {'model_id': EMBEDDING_PROFILE['model_id'], 'download_bytes': self.total_bytes,
                      'license_url': 'https://huggingface.co/google/embeddinggemma-300m'}

    def download(self, principal, version, token):
        self.storage.ai_runtime(principal)
        if not isinstance(token, str) or not 1 <= len(token) <= 2048 or any(not 32 < ord(c) < 127 for c in token):
            raise Error(400, 'invalid_input', 'Supply a Hugging Face token with access to EmbeddingGemma')
        if self.root.is_symlink():
            raise Error(409, 'ai_cache_unavailable', 'The model cache is unavailable')
        try:
            if shutil.disk_usage(self.root.parent).free < self.total_bytes + max(self.total_bytes // 10, 64 * 1024**2):
                raise Error(409, 'ai_disk_space', 'Free additional disk space before downloading the model')
            # This durable directory distinguishes interrupted explicit work from
            # a database-only restore. Reads/startup never create it.
            self.root.mkdir(mode=0o700, exist_ok=True)
            _sync_directory(self.root.parent)
        except OSError:
            raise Error(409, 'ai_cache_unavailable', 'The model cache is unavailable') from None
        if not self._work_lock.acquire(blocking=False):
            raise Error(409, 'ai_operation_running', 'Wait for the current model operation to finish')
        try:
            result = self.storage.ai_runtime_begin(principal, version, 'download', self.vault.protect(token))
            try:
                marker = self.root / ('.download-' + result['operation_id'])
                with marker.open('x') as file:
                    file.write('Explicit model download requested on this installation.\n')
                    file.flush()
                    os.fsync(file.fileno())
                _sync_directory(self.root)
            except Exception:
                self.storage.ai_runtime_cancel(principal, result['version'])
                raise Error(409, 'ai_cache_unavailable', 'The model cache is unavailable') from None
        finally:
            self._work_lock.release()
        self._wake.set()
        return result

    def cancel(self, principal, version):
        before = self.storage.ai_runtime(principal)
        result = self.storage.ai_runtime_cancel(principal, version)
        if before['operation_id']:
            self._clear_marker(before['operation_id'])
        self._wake.set()
        return result

    def remove(self, principal, version):
        result = self.storage.ai_runtime_begin(principal, version, 'remove')
        self._close_runtime()
        self._verified = None
        self._wake.set()
        return result

    def _delete(self, path):
        if path.parent != self.root or not _OWNED.fullmatch(path.name):
            raise EmbeddingRuntimeError('unavailable')
        if self.root.is_symlink():
            raise EmbeddingRuntimeError('unavailable')
        if path.is_symlink():
            path.unlink()
        elif path.exists():
            shutil.rmtree(path)

    def _clear_marker(self, operation):
        if not self.root.is_symlink():
            (self.root / ('.download-' + str(UUID(operation)))).unlink(missing_ok=True)

    def run_once(self):
        if not self._work_lock.acquire(blocking=False):
            return False
        try:
            job = self.storage.ai_runtime_claim(time.time())
            if job is None:
                return False
            self._perform(job)
            return True
        finally:
            self._work_lock.release()

    def _perform(self, job):
        operation, lease = job['operation_id'], job['lease_token']
        completed, total, verifying = job['progress_bytes'], max(job['total_bytes'], self.total_bytes), False
        renewed = 0.0
        stage = self.root / f'stage-{operation}-{lease}'
        candidate = self.root / ('embeddinggemma-' + str(uuid4()))
        runtime, committed = None, False

        def checkpoint(force=False):
            nonlocal renewed
            if self._stop.is_set():
                return True
            if force or time.monotonic() - renewed >= 1:
                if not self.storage.ai_runtime_advance(operation, lease, completed, total, verifying=verifying):
                    return True
                renewed = time.monotonic()
            return False

        def progress(value, size):
            nonlocal completed, total
            # Recovery begins a fresh download; durable progress never regresses.
            completed, total = max(completed, value), max(total, size)
            if checkpoint():
                raise ModelDownloadError('cancelled')

        try:
            if checkpoint(True):
                return
            if job['action'] == 'remove':
                self._close_runtime()
                if self.root.is_symlink():
                    raise EmbeddingRuntimeError('unavailable')
                if self.root.exists():
                    for path in self.root.iterdir():
                        if checkpoint(True):
                            return
                        if _OWNED.fullmatch(path.name):
                            self._delete(path)
                    _sync_directory(self.root)
                self.storage.ai_runtime_finish(operation, lease)
                return
            token = self.vault.reveal(job['protected_token'])
            self.downloader(stage, token, progress=progress, cancelled=checkpoint)
            token = None
            verifying = True
            if checkpoint(True):
                return
            self.verifier(stage, cancelled=checkpoint)
            with self._runtime_lock:
                self._verifying = True
                self._close_runtime()
                runtime = self.runtime_factory(stage)
            future = runtime.submit('query', ['How can I contact support?'])
            while True:
                if checkpoint():
                    raise ModelDownloadError('cancelled')
                try:
                    validate_vectors(future.result(timeout=.2), 1)
                    break
                except FutureTimeout:
                    continue
            runtime.close()
            runtime = None
            if checkpoint(True):
                return
            stage.rename(candidate)
            _sync_directory(self.root)
            cache_id = candidate.name.removeprefix('embeddinggemma-')
            committed = self.storage.ai_runtime_finish(operation, lease, manifest=EMBEDDING_PROFILE, cache_id=cache_id)
            if committed:
                self._close_runtime()
                self._verified = cache_id
                # Safe after commit: a crash may leave an unreferenced cache,
                # but can never publish a database reference to missing files.
                for path in self.root.iterdir():
                    if path != candidate and _OWNED.fullmatch(path.name):
                        self._delete(path)
        except Exception as exc:
            code = 'cancelled' if self._stop.is_set() or getattr(exc, 'code', None) == 'cancelled' else ('verification_failed' if verifying else 'download_failed')
            if not verifying and getattr(exc, 'code', None) == 'model_access_required':
                code = 'model_access_required'
            # Stale/revoked leases cannot change the durable result.
            if not self._stop.is_set():
                self.storage.ai_runtime_finish(operation, lease, error=code)
        finally:
            if runtime is not None:
                runtime.close()
            with self._runtime_lock:
                self._verifying = False
            for path in (stage, candidate if not committed else stage):
                try:
                    self._delete(path)
                except Exception:
                    pass  # A later explicit operation can clean owned remnants.
            if not self._stop.is_set() and self.storage.ai_runtime_local()['operation_id'] != operation:
                self._clear_marker(operation)

    def _embedding_runtime(self):
        row = self.storage.ai_runtime_local()
        cache_id = row['active_cache_id']
        if not cache_id or row['active_manifest'] != EMBEDDING_PROFILE or self._verified != cache_id:
            raise EmbeddingRuntimeError('unavailable')
        path = self._path(cache_id)
        if not path.is_dir() or path.is_symlink():
            self.storage.ai_runtime_unavailable(row['version'], cancel_operation=True)
            raise EmbeddingRuntimeError('unavailable')
        with self._runtime_lock:
            if self._verifying:
                raise EmbeddingRuntimeError('busy')
            if self._cache_id != cache_id:
                self._close_runtime()
                self._runtime = self.runtime_factory(path)
                self._cache_id = cache_id
            return self._runtime

    def embed_queries(self, texts):
        return self._embedding_runtime().embed_queries(texts)

    def embed_documents(self, texts, *, titles=None):
        return self._embedding_runtime().embed_documents(texts, titles=titles)

    def available(self):
        """Internal readiness check; never loads a model or starts acquisition."""
        row = self.storage.ai_runtime_local()
        if (self._verifying or row['state'] != 'ready' or not row['active_cache_id']
                or row['active_manifest'] != EMBEDDING_PROFILE or self._verified != row['active_cache_id']):
            return False
        try:
            path = self._path(row['active_cache_id'])
            return path.is_dir() and not path.is_symlink()
        except EmbeddingRuntimeError:
            return False

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        def loop():
            while not self._stop.is_set():
                try:
                    self.run_once()
                except Exception:
                    pass  # Never log secrets, paths from remote data or exceptions.
                self._wake.wait(1)
                self._wake.clear()
        self._thread = threading.Thread(target=loop, name='embedding-installation', daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=5)
        self._close_runtime()
