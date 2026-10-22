"""Installation-wide embedding selection; local remains the default.

External APIs receive approved source passages and retrieval questions. Vectors
stay in the installation database, scoped to an immutable embedding profile.
"""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import hashlib
import math
import threading

from feedback.ai_endpoints import endpoint_url, endpoint_request
from feedback.ai_providers import ProviderError, _identifier, _json_bytes
from feedback.ai_runtime import EmbeddingRuntimeError, validate_vectors
from feedback.ai_settings import AISettingsService
from feedback.errors import Error


def embedding_config(data, *, allow_internal=False):
    if not isinstance(data, dict) or set(data) != {'base_url', 'model', 'dimensions', 'send_dimensions', 'query_prefix', 'document_prefix'}:
        raise ProviderError('invalid_request')
    result = dict(data, base_url=endpoint_url(data['base_url'], allow_internal=allow_internal))
    if (not _identifier(data['model']) or type(data['dimensions']) is not int or not 1 <= data['dimensions'] <= 4096
            or type(data['send_dimensions']) is not bool or any(not isinstance(data[k], str) or len(data[k]) > 256 for k in ('query_prefix', 'document_prefix'))):
        raise ProviderError('invalid_request')
    _json_bytes(result, 8192)
    return result


class EmbeddingService:
    def __init__(self, storage, vault, local, *, allow_internal=False, transport=None):
        self.storage, self.vault, self.local = storage, vault, local
        self.allow_internal, self.transport = allow_internal, transport
        self._slots = threading.BoundedSemaphore(2)
        self._executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix='external-embedding')

    def status(self, principal):
        AISettingsService._admin(principal)
        return self.storage.ai_embedding_settings(principal)

    async def _external(self, config, key, texts, kind):
        if not isinstance(texts, (list, tuple)) or not 1 <= len(texts) <= 4:
            raise ProviderError('invalid_request')
        prefix = config['query_prefix' if kind == 'query' else 'document_prefix']
        if any(not isinstance(text, str) or not text.strip() or len((prefix + text).encode('utf-8')) > 32768 for text in texts):
            raise ProviderError('invalid_request')
        body = {'model': config['model'], 'input': [prefix + text for text in texts], 'encoding_format': 'float'}
        if config['send_dimensions']:
            body['dimensions'] = config['dimensions']
        value = await endpoint_request(self.transport, 45, 'POST', config['base_url'], '/embeddings', key, body, allow_internal=self.allow_internal)
        if not isinstance(value, dict) or not isinstance(value.get('data'), list) or len(value['data']) != len(texts):
            raise ProviderError('invalid_response')
        ordered = {}
        for row in value['data']:
            if not isinstance(row, dict) or type(row.get('index')) is not int or not 0 <= row['index'] < len(texts) or row['index'] in ordered:
                raise ProviderError('invalid_response')
            vector = row.get('embedding')
            if not isinstance(vector, list) or len(vector) != config['dimensions'] or any(type(x) not in (int, float) or not math.isfinite(x) for x in vector):
                raise ProviderError('invalid_response')
            try:
                norm = math.sqrt(math.fsum(x*x for x in vector))
            except (ValueError, OverflowError):
                raise ProviderError('invalid_response') from None
            if not math.isfinite(norm) or norm <= 0:
                raise ProviderError('invalid_response')
            ordered[row['index']] = [x / norm for x in vector]
        return validate_vectors([ordered[i] for i in range(len(texts))], len(texts), dimensions=config['dimensions'])

    async def configure(self, principal, version, mode, config=None, key=None):
        before = self.status(principal)
        if before['version'] != version:
            raise Error(409, 'conflict', 'Embedding settings changed; reload before saving')
        protected = profile = None
        if mode == 'external':
            try:
                config = embedding_config(config, allow_internal=self.allow_internal)
                if not isinstance(key, str) or (key and not _identifier(key, 4096)):
                    raise ProviderError('invalid_credentials')
                # Verify both request shapes using synthetic text only. Never
                # acquire weights or transmit knowledge as part of a probe.
                if not self._slots.acquire(blocking=False):
                    raise Error(409, 'embedding_busy', 'Embedding requests are busy; try saving again shortly')
                try:
                    await self._external(config, key, ['How do I contact support?'], 'query')
                    await self._external(config, key, ['Contact support through the chat button.'], 'document')
                finally:
                    self._slots.release()
                profile = {'model_id': config['model'], 'dimensions': config['dimensions'], 'normalized': True,
                    'precision': 'float32', 'chunking': 'headings-v1', 'preprocessing': 'external-plain-v1',
                    'revision': hashlib.sha256(_json_bytes(config, 8192)).hexdigest()}
                protected = self.vault.protect(key)
            except (ProviderError, EmbeddingRuntimeError):
                raise Error(400, 'embedding_check_failed', 'The embedding check failed. Check the endpoint, key, model and dimensions. No settings were changed.') from None
        elif mode != 'local' or config is not None or key is not None:
            raise Error(400, 'invalid_input', 'Choose local embeddings or supply complete external settings')
        result = self.storage.ai_embedding_configure(principal, version, mode, config, profile, protected)
        return self.status(principal) | {'reindex': result}

    def reindex(self, principal, version):
        AISettingsService._admin(principal)
        return self.storage.ai_embedding_reindex(principal, version)

    def available(self):
        saved = self.storage.ai_embedding_settings(internal=True)
        return saved['mode'] == 'external' or self.local.available()

    def _embed(self, texts, kind, titles=None):
        saved = self.storage.ai_embedding_settings(internal=True)
        if saved['mode'] == 'local':
            vectors = self.local.embed_queries(texts) if kind == 'query' else self.local.embed_documents(texts, titles=titles)
        else:
            if not self._slots.acquire(blocking=False):
                raise EmbeddingRuntimeError('busy')
            try:
                key = self.vault.reveal(saved['protected_key'])
                vectors = self._executor.submit(lambda: asyncio.run(self._external(saved['config'], key, texts, kind))).result(timeout=55)
            except Exception:
                raise EmbeddingRuntimeError('external_failed') from None
            finally:
                self._slots.release()
        if self.storage.ai_embedding_settings(internal=True)['version'] != saved['version']:
            raise EmbeddingRuntimeError('unavailable')
        return vectors

    def stop(self):
        self._executor.shutdown(wait=False, cancel_futures=True)

    def embed_queries(self, texts):
        return self._embed(texts, 'query')

    def embed_documents(self, texts, *, titles=None):
        return self._embed(texts, 'document', titles)

    def validate(self, vectors, count):
        profile = self.storage.ai_embedding_settings(internal=True)['profile']
        return validate_vectors(vectors, count, dimensions=profile['dimensions'])
