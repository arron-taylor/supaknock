"""Native-administrator AI configuration; provider I/O stays outside transactions."""
from contextlib import contextmanager
from dataclasses import asdict
import asyncio
import hmac
import threading
from urllib.parse import quote

from feedback.ai_domain import validate_ai_settings
from feedback.ai_key_pool import provider_outcome
from feedback.ai_providers import ModelCapabilities, PROVIDERS, ProviderError, provider
from feedback.errors import Error


_UNCHANGED = object()
_PROVIDER_ERRORS = {
    'invalid_credentials': (409, 'The provider key was rejected. Update the saved key and retry.'),
    'access_denied': (409, 'This key cannot access the selected provider operation or model.'),
    'model_or_endpoint_unavailable': (409, 'The selected model is unavailable to this key.'),
    'rate_limited': (429, 'The provider rate limit was reached. Try again later.'),
    'quota_exhausted': (429, 'This provider account has exhausted its usage allowance. Its keys are cooling down.'),
    'payment_required': (409, 'This provider account needs a billing update. Repair it, then test the key again.'),
    'timeout': (504, 'The provider did not respond in time. Try again later.'),
    'capability_unverified': (409, 'Text generation could not be verified for this model.'),
    'tool_capability_unverified': (409, 'Tool calling could not be verified for this model.'),
    'unsupported_provider': (400, 'Choose an available provider.'),
}


def provider_error(error):
    # Never serialize upstream messages, HTTP bodies, or arbitrary adapter codes.
    status, message = _PROVIDER_ERRORS.get(error.code, (502, 'The provider operation could not be completed. Try again later.'))
    code = error.code if error.code in _PROVIDER_ERRORS else 'unavailable'
    return Error(status, 'ai_provider_' + code, message)


def resolve_feedback_destination(settings, kind, base_url):
    """Resolve a saved page link. No fetching or private/draft query parameters."""
    if kind not in ('feature', 'bug'):
        raise Error(400, 'invalid_input', 'Choose a feature or bug destination')
    validate_ai_settings(settings)
    custom = settings[kind + '_feedback_url'] or settings['feedback_url']
    if custom:
        return custom
    board = settings[kind + '_board_id']
    return base_url.rstrip('/') + '/boards/' + quote(board, safe='') if board else None


class AISettingsService:
    def __init__(self, storage, vault, base_url, *, provider_factory=provider):
        self.storage, self.vault, self.base_url = storage, vault, base_url
        self.provider_factory = provider_factory
        self._lock, self._operations = threading.Lock(), set()

    @staticmethod
    def _admin(principal):
        if not isinstance(principal, dict) or principal.get('kind') != 'admin' or not principal.get('user_id') or not principal.get('session_id'):
            raise Error(403, 'forbidden', 'Native administrator access required')

    def read(self, project, principal):
        self._admin(principal)
        return self.storage.ai_settings(project, principal)

    def usage(self, project, principal):
        self._admin(principal)
        return self.storage.ai_usage(project, principal)

    def jobs(self, project, principal, limit=20):
        self._admin(principal)
        return self.storage.ai_jobs(project, principal, limit=limit)

    def save(self, project, principal, version, settings, *, key=_UNCHANGED):
        """An omitted key survives only within the same saved provider.

        Storage decides this atomically against the versioned current selection;
        a provider switch clears its old key/model and all verification evidence.
        """
        self._admin(principal)
        validate_ai_settings(settings)
        if key is _UNCHANGED:
            return self.storage.ai_configure(project, principal, version, settings)
        library_version=self.storage.ai_account_keys(principal)['version']
        if key is not None:
            self._valid_key(key)
            current = self.read(project, principal)
            same_provider=current['settings']['provider']==settings['provider']
            pool=self.keys(project,principal) if same_provider else {'keys':[]}
            default=next((row['id'] for row in pool['keys'] if row['is_default']),None)
            self._unique_key(project,principal,version,key,except_id=default,
                             provider=settings['provider'],library_version=library_version)
        return self.storage.ai_configure(project, principal, version, settings,
                                         protected_key=self.vault.protect(key) if key is not None else None, library_version=library_version)

    @staticmethod
    def _valid_key(key):
        if not isinstance(key, str) or not 1 <= len(key) <= 4096 or any(not 32 < ord(c) < 127 for c in key):
            raise Error(400, 'invalid_input', 'Use a provider key without whitespace or control characters')

    def _unique_key(self, project, principal, version, key, *, except_id=None, provider=None, library_version=None):
        # The mutation rechecks this same settings version, fencing concurrent
        # additions. Never expose a credential or stable secret fingerprint.
        current=self.read(project,principal)
        if current['version']!=version:
            raise Error(409,'conflict','Project settings changed')
        rows=self.storage.ai_account_key_snapshot(principal,library_version,provider=provider) if provider else self.storage.ai_provider_key_secrets(project,principal,version)
        for row in rows:
            if row['id'] == except_id:
                continue
            try:
                duplicate = hmac.compare_digest(key, self.vault.reveal(row['protected_key']))
            except Exception:
                raise Error(409, 'ai_key_unavailable', 'An existing provider key could not be read. Remove or replace it first.') from None
            if duplicate:
                raise Error(409, 'ai_duplicate_key', 'This key is already saved in the account library; explicitly select it for the project')

    def keys(self, project, principal):
        self._admin(principal)
        return self.storage.ai_provider_keys(project, principal)

    def add_key(self, project, principal, version, label, quota_group, key):
        self._admin(principal)
        self._valid_key(key)
        library_version=self.storage.ai_account_keys(principal)['version']
        self._unique_key(project, principal, version, key)
        return self.storage.ai_provider_key_add(project, principal, version, label, quota_group, self.vault.protect(key), library_version=library_version)

    def update_key(self, project, principal, version, key_id, key_version, *, label, quota_group, enabled, key=_UNCHANGED):
        self._admin(principal)
        extra = {'library_version':self.storage.ai_account_keys(principal)['version']}
        if key is not _UNCHANGED:
            self._valid_key(key)
            self._unique_key(project, principal, version, key, except_id=key_id)
            extra['protected_key'] = self.vault.protect(key)
        return self.storage.ai_provider_key_update(project, principal, version, key_id, key_version,
            label=label, quota_group=quota_group, enabled=enabled, **extra)

    def delete_key(self, project, principal, version, key_id, key_version):
        self._admin(principal)
        return self.storage.ai_provider_key_delete(project, principal, version, key_id, key_version)

    def retry_key(self, project, principal, version, key_id, key_version):
        self._admin(principal)
        return self.storage.ai_provider_key_retry(project, principal, version, key_id, key_version)

    def providers(self, principal):
        self._admin(principal)
        self.storage.ai_runtime(principal)  # Recheck the native session transactionally.
        # No user-controlled modules or endpoints.
        return [{'id': item.id, 'name': item.name} for item in PROVIDERS.values()]

    def destinations(self, project, principal):
        settings = self.read(project, principal)['settings']
        return {kind: resolve_feedback_destination(settings, kind, self.base_url) for kind in ('feature', 'bug')}

    @contextmanager
    def _operation(self, project_id):
        # Explicit probes are small but may incur cost. Bound outstanding admin
        # operations separately from the durable customer generation queue.
        with self._lock:
            if project_id in self._operations or len(self._operations) >= 4:
                raise Error(409, 'ai_operation_running', 'A provider check is already running. Wait and retry.')
            self._operations.add(project_id)
        try:
            yield
        finally:
            with self._lock:
                self._operations.remove(project_id)

    @contextmanager
    def _check(self, principal, snapshot):
        check=self.storage.ai_admin_check_begin(principal,snapshot)
        try:
            yield
        finally:
            self.storage.ai_admin_check_end(check)

    def _snapshot(self, project, principal, version, *, key_id=None, key_version=None):
        snapshot = self.storage.ai_provider_snapshot(project, principal, version, key_id=key_id)
        if key_version is not None and snapshot.get('key_version') != key_version:
            raise Error(409, 'conflict', 'This provider key changed. Reload and retry.')
        if not snapshot['protected_key']:
            raise Error(409, 'ai_key_required', 'Save a provider key before checking available models')
        try:
            key = self.vault.reveal(snapshot['protected_key'])
        except Exception:
            raise Error(409, 'ai_key_unavailable', 'The saved provider key could not be read') from None
        return snapshot, key

    def _evidence(self, principal, snapshot, **evidence):
        return self.storage.ai_store_provider_evidence(snapshot['project_id'], principal,
            snapshot['version'], snapshot['credential_version'], snapshot['settings']['model'],
            key_id=snapshot['key_id'], key_version=snapshot['key_version'],
            health_epoch=snapshot['health_epoch'], group_epoch=snapshot['group_epoch'], **evidence)

    def _failed_check(self, principal, snapshot, error, *, probe=False):
        outcome = provider_outcome(error)
        if probe and outcome == 'unchanged':
            outcome = 'capability_unverified'
        return self.storage.ai_provider_check_outcome(snapshot['project_id'], principal,
            snapshot['version'], snapshot['key_id'], snapshot['key_version'],
            snapshot['health_epoch'], snapshot['group_epoch'], outcome, retry_after=error.retry_after,catalogue_failure=not probe)

    async def refresh_models(self, project, principal, version, *, key_id=None, key_version=None):
        current = self.read(project, principal)
        with self._operation(current['project_id']):
            snapshot, key = self._snapshot(project, principal, version, key_id=key_id, key_version=key_version)
            with self._check(principal,snapshot):
                try:
                    models = await asyncio.wait_for(self.provider_factory(snapshot['settings']['provider']).list_models(key),30)
                except ProviderError as exc:
                    self._failed_check(principal, snapshot, exc)
                    raise provider_error(exc) from None
                except Exception:
                    self._failed_check(principal, snapshot, ProviderError('unavailable'))
                    raise provider_error(ProviderError('unavailable')) from None
                result = self._evidence(principal, snapshot, catalogue=list(models))
                return self.keys(project, principal) if key_id is not None else result

    async def probe(self, project, principal, version, *, tools=False, key_id=None, key_version=None):
        if type(tools) is not bool:
            raise Error(400, 'invalid_input', 'Choose whether to verify tool calling')
        current = self.read(project, principal)
        with self._operation(current['project_id']):
            current = self.read(project, principal)
            snapshot, key = self._snapshot(project, principal, version, key_id=key_id, key_version=key_version)
            model = snapshot['settings']['model']
            if not model or model not in snapshot['catalogue']:
                raise Error(409, 'ai_model_required', 'Refresh available models and select an exact model first')
            with self._check(principal,snapshot):
                try:
                    capabilities = await asyncio.wait_for(self.provider_factory(snapshot['settings']['provider']).probe_model(key, model, tools=tools),90)
                    if not isinstance(capabilities, ModelCapabilities) or not capabilities.text_generation or (tools and not capabilities.tool_calling):
                        raise ProviderError('capability_unverified')
                except ProviderError as exc:
                    self._failed_check(principal, snapshot, exc, probe=True)
                    raise provider_error(exc) from None
                except Exception:
                    self._failed_check(principal, snapshot, ProviderError('unavailable'), probe=True)
                    raise provider_error(ProviderError('unavailable')) from None
                verified=asdict(capabilities)
                if not tools and snapshot['capabilities'].get('tool_calling') is True:
                    verified['tool_calling']=True
                result = self._evidence(principal, snapshot, capabilities=verified)
                return self.keys(project, principal) if key_id is not None else result
