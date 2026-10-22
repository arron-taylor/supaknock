"""Topic policy orchestration. Decision adapters never choose replies or tools."""
import asyncio
import hashlib
import hmac
import json

from feedback.ai_decision_providers import cloudflare_credentials, decision_provider, validate_decision
from feedback.ai_key_pool import ProviderPoolWait, provider_outcome
from feedback.ai_providers import ProviderError
from feedback.ai_settings import AISettingsService, provider_error
from feedback.ai_topics import is_greeting, topic_context
from feedback.errors import Error


class TopicSettingsService:
    def __init__(self, storage, vault, *, provider_factory=decision_provider):
        self.storage, self.vault, self.provider_factory = storage, vault, provider_factory

    def read(self, project, principal):
        AISettingsService._admin(principal)
        return self.storage.ai_topic_settings(project, principal)

    def save(self, project, principal, version, policy):
        AISettingsService._admin(principal)
        return self.storage.ai_topic_configure(project, principal, version, policy)

    def add_key(self, project, principal, version, label, group, *, key=None, reuse_key_id=None, account_id=None):
        AISettingsService._admin(principal)
        if (key is None) == (reuse_key_id is None):
            raise Error(400, 'invalid_input', 'Supply a new key or explicitly reference a saved generation key')
        library_version=self.storage.ai_account_keys(principal)['version']
        snapshot = self.storage.ai_decision_key_secrets(project, principal, version)
        if snapshot['provider'] == 'cloudflare':
            if reuse_key_id is not None:
                raise Error(400, 'invalid_input', 'Cloudflare requires a new account ID and API token')
            try:
                key = cloudflare_credentials(account_id, key)
            except ProviderError:
                raise Error(400, 'invalid_input', 'Use a 32-character lowercase hexadecimal Cloudflare account ID and an API token without whitespace') from None
            # Tokens for one Cloudflare account must share cooldowns even when
            # an administrator supplies different labels or project settings.
            group = 'Cloudflare account ' + hashlib.sha256(account_id.encode('ascii')).hexdigest()[:24]
        elif account_id is not None:
            raise Error(400, 'invalid_input', 'Account ID is only supported for Cloudflare decision credentials')
        if key is not None:
            if snapshot['provider'] != 'cloudflare':
                AISettingsService._valid_key(key)
            for row in snapshot['keys']:
                try:
                    duplicate = hmac.compare_digest(key, self.vault.reveal(row['protected_key']))
                except Exception:
                    raise Error(409, 'ai_key_unavailable', 'An existing provider key could not be read. Remove or replace it first.') from None
                if duplicate:
                    raise Error(409, 'ai_duplicate_key', 'This key is already saved. Explicitly share the saved key instead.')
        return self.storage.ai_decision_key_add(project, principal, version, label, group,
            self.vault.protect(key) if key is not None else None, reuse_key_id=reuse_key_id,
            configuration_version=snapshot['configuration_version'], library_version=library_version)

    def remove_key(self, project, principal, version, key_id, key_version):
        AISettingsService._admin(principal)
        return self.storage.ai_decision_key_remove(project, principal, version, key_id, key_version)

    def _adapter(self, snapshot):
        return self.provider_factory(snapshot['topic']['policy']['provider'])

    async def refresh(self, project, principal, version, key_id, key_version):
        AISettingsService._admin(principal)
        snap = self.storage.ai_decision_snapshot(project, principal, version, key_id, key_version)
        try:
            models = await self._adapter(snap).list_models(self.vault.reveal(snap['key']['protected_key']))
        except ProviderError as error:
            raise provider_error(error) from None
        return self.storage.ai_decision_catalogue(project, principal, version, key_id, key_version, models, health_epoch=snap['key']['health_epoch'], group_epoch=snap['group']['health_epoch'])

    async def test(self, project, principal, version, key_id, key_version, question, history=(), *, probe=False):
        AISettingsService._admin(principal)
        snap = self.storage.ai_decision_snapshot(project, principal, version, key_id, key_version)
        adapter = self._adapter(snap)
        if adapter.transport_verified is not True:
            raise Error(409, 'ai_decision_transport_unavailable', 'This provider has no verified typed decision API. Choose a supported provider before enabling allowed topics.')
        context, limited = topic_context(history)
        if limited:
            raise Error(400, 'invalid_input', 'Recent context exceeds the 8 KiB decision limit')
        if probe:
            question, context = 'How do I export notes from the fictional Acorn notes app?', []
            policy = snap['topic']['policy'] | {'scope': 'Support for the fictional Acorn notes app', 'allowed_examples': [], 'disallowed_examples': []}
        else:
            policy = snap['topic']['policy']
            if not policy['scope'].strip() or not policy['public_summary'].strip():
                raise Error(409, 'ai_decision_scope_required', 'Save a scope and public summary first')
        operation = self.storage.ai_decision_check_begin(project, principal, version, key_id, key_version)
        try:
            result = validate_decision(await asyncio.wait_for(adapter.classify(
                self.vault.reveal(operation['protected_key']), operation['model'],
                policy={key: policy[key] for key in ('scope', 'allowed_examples', 'disallowed_examples')},
                question=question, context=context), timeout=30), operation['model'])
        except (ProviderError, TimeoutError) as error:
            error = error if isinstance(error, ProviderError) else ProviderError('timeout', retryable=True)
            return self.storage.ai_decision_check_finish(operation['id'], error='unavailable',
                outcome=provider_outcome(error), retry_after=error.retry_after)
        except Exception:
            return self.storage.ai_decision_check_finish(operation['id'], error='unavailable')
        return self.storage.ai_decision_check_finish(operation['id'], result, probe=probe)


class TopicGate:
    def __init__(self, storage, vault, *, provider_factory=decision_provider):
        self.storage, self.vault, self.provider_factory = storage, vault, provider_factory

    async def evaluate(self, job_id, token, question, material):
        snapshot = self.storage.ai_topic_job_snapshot(job_id, token)
        policy = snapshot['topic']['policy']
        if not policy['enabled']:
            return None  # Zero decision calls when disabled.
        history = material.get('topic_messages', [])
        context, limited = topic_context(history)
        digest = hashlib.sha256(json.dumps([question, history], ensure_ascii=False,
            separators=(',', ':')).encode()).hexdigest()
        receipt = snapshot['receipt']
        if receipt:
            if receipt['context_hash'] != digest:
                raise Error(409, 'conflict', 'Decision context changed')
            return receipt
        if is_greeting(question) or limited:
            return self.storage.ai_topic_record(job_id, token, digest,
                local_outcome='clarify' if limited else 'greeting')
        adapter = self.provider_factory(policy['provider'])
        if adapter.transport_verified is not True:
            return self.storage.ai_topic_record(job_id, token, digest,
                local_outcome='unavailable', error='unavailable')
        claim = self.storage.ai_topic_claim(job_id, token)
        if not claim['available']:
            if claim['retry_at'] is not None:
                raise ProviderPoolWait(claim['retry_at'])
            return self.storage.ai_topic_record(job_id, token, digest,
                local_outcome='unavailable', error='unavailable')
        tokens = len(json.dumps([policy['scope'], policy['allowed_examples'], policy['disallowed_examples'], question, context], ensure_ascii=False).encode()) + 1024
        index = material['settings']['max_tool_rounds'] + 1
        if not self.storage.ai_reserve_usage(job_id, token, tokens, call_index=index):
            raise Error(429, 'budget_exhausted', 'The project AI usage allowance is exhausted')
        if not self.storage.ai_topic_before_dispatch(job_id, token, tokens):
            raise Error(409, 'conflict', 'This decision is no longer authorized')
        try:
            result = validate_decision(await asyncio.wait_for(adapter.classify(
                self.vault.reveal(claim['protected_key']), claim['model'],
                policy={key: policy[key] for key in ('scope', 'allowed_examples', 'disallowed_examples')},
                question=question, context=context), timeout=30), claim['model'])
        except (ProviderError, TimeoutError) as error:
            error = error if isinstance(error, ProviderError) else ProviderError('timeout', retryable=True)
            safe = error.code if error.code in ('invalid_response', 'rate_limited', 'timeout') else 'unavailable'
            return self.storage.ai_topic_record(job_id, token, digest, error=safe,
                outcome=provider_outcome(error), retry_after=error.retry_after)
        except Exception:
            return self.storage.ai_topic_record(job_id, token, digest, error='unavailable')
        return self.storage.ai_topic_record(job_id, token, digest, result=result)
