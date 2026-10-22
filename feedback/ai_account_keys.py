"""Shared native-admin credential library; projects bind IDs, never secrets."""
import asyncio
import hashlib
import hmac

from feedback.ai_decision_providers import cloudflare_credentials
from feedback.ai_key_pool import provider_outcome
from feedback.ai_providers import ProviderError
from feedback.ai_settings import AISettingsService, _UNCHANGED, provider_error
from feedback.errors import Error


class AccountKeysService:
    def __init__(self, storage, vault, generation, decisions, *, allow_internal=False):
        self.storage, self.vault = storage, vault
        self.allow_internal = allow_internal
        self.generation, self.decisions = generation, decisions

    def read(self, principal):
        AISettingsService._admin(principal)
        return self.storage.ai_account_keys(principal)

    def _secret(self, provider, key, account_id, group, base_url=None, models=None):
        if provider == 'custom':
            from feedback.ai_endpoints import custom_credentials
            if account_id is not None:
                raise Error(400, 'invalid_input', 'Account ID is only supported for Cloudflare')
            try:
                return custom_credentials(key, base_url, models, allow_internal=self.allow_internal), group
            except ProviderError:
                raise Error(400, 'invalid_input', 'Supply an allowed API base URL and exact model IDs for the custom endpoint') from None
        if base_url is not None or models is not None:
            raise Error(400, 'invalid_input', 'Endpoint configuration is only supported for custom providers')
        if provider == 'cloudflare':
            try:
                secret = cloudflare_credentials(account_id, key)
            except ProviderError:
                raise Error(400,'invalid_input','Use a Cloudflare account ID and Workers AI API token') from None
            return secret, 'Cloudflare account ' + hashlib.sha256(account_id.encode('ascii')).hexdigest()[:24]
        if account_id is not None:
            raise Error(400,'invalid_input','Account ID is only supported for Cloudflare')
        AISettingsService._valid_key(key)
        return key, group

    def _unique(self, principal, version, provider, secret, except_id=None):
        for row in self.storage.ai_account_key_snapshot(principal,version,provider=provider):
            if row['id']==except_id:
                continue
            try:
                duplicate=hmac.compare_digest(secret,self.vault.reveal(row['protected_key']))
            except Exception:
                raise Error(409,'ai_key_unavailable','An existing provider key could not be read. Replace or remove it first.') from None
            if duplicate:
                raise Error(409,'ai_duplicate_key','This key is already saved. Select the existing account key in your project.')

    def add(self, principal, version, provider, purpose, label, group, key, account_id=None, base_url=None, models=None):
        AISettingsService._admin(principal)
        secret,group=self._secret(provider,key,account_id,group,base_url,models)
        self._unique(principal,version,provider,secret)
        return self.storage.ai_account_key_add(principal,version,provider,purpose,label,group,self.vault.protect(secret))

    def update(self, principal, version, kid, key_version, label, group, enabled, key=_UNCHANGED, account_id=None, purpose=None, base_url=None, models=None):
        AISettingsService._admin(principal)
        snapshot=self.storage.ai_account_key_snapshot(principal,version,kid,key_version)
        extra={}
        if key is not _UNCHANGED:
            secret,group=self._secret(snapshot['key']['provider'],key,account_id,group,base_url,models)
            self._unique(principal,version,snapshot['key']['provider'],secret,except_id=kid)
            extra['protected_key']=self.vault.protect(secret)
        elif base_url is not None or models is not None:
            raise Error(400, 'invalid_input', 'Replace the endpoint, model IDs and key together')
        elif account_id is not None:
            raise Error(400,'invalid_input','Replace the Cloudflare token and account ID together')
        elif snapshot['key']['provider']=='cloudflare':
            # Account-derived group cannot be changed independently of credentials.
            group=snapshot['group']['label']
        return self.storage.ai_account_key_update(principal,version,kid,key_version,label=label,quota_group=group,enabled=enabled,purpose=purpose,**extra)

    def action(self, principal, version, kid, key_version, action):
        AISettingsService._admin(principal)
        return self.storage.ai_account_key_action(principal,version,kid,key_version,action)

    async def refresh(self, principal, version, kid, key_version, role):
        AISettingsService._admin(principal)
        snapshot=self.storage.ai_account_key_snapshot(principal,version,kid,key_version)
        saved=snapshot['key']
        if saved['purpose'] not in (role,'both'):
            raise Error(400,'invalid_input','Choose an allowed key role')
        check_snapshot={'version':version,'key_id':kid,'key_version':key_version,'health_epoch':saved['health_epoch'],'group_epoch':snapshot['group']['health_epoch']}
        with self.generation._operation('account-key:'+kid), self.generation._check(principal,check_snapshot):
            try:
                key=self.vault.reveal(saved['protected_key'])
            except Exception:
                raise Error(409,'ai_key_unavailable','The saved provider key could not be read') from None
            try:
                factory=self.generation.provider_factory if role=='generation' else self.decisions.provider_factory
                models=await asyncio.wait_for(factory(saved['provider']).list_models(key),30)
            except (ProviderError,TimeoutError) as exc:
                error=exc if isinstance(exc,ProviderError) else ProviderError('timeout',retryable=True)
                self.storage.ai_account_key_failure(principal,snapshot,provider_outcome(error),error.retry_after,role=role)
                raise provider_error(error) from None
            except Exception:
                raise provider_error(ProviderError('unavailable')) from None
            return self.storage.ai_account_key_models(principal,snapshot,role,list(models))

    def select(self, project, principal, version, library_version, role, keys):
        AISettingsService._admin(principal)
        return self.storage.ai_project_key_selection(project,principal,version,library_version,role,keys)
