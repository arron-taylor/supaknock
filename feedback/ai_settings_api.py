"""Versioned native-administrator transport for optional AI configuration."""
from typing import Annotated

from fastapi import Depends
from pydantic import BaseModel, ConfigDict, Field, SecretStr, StrictBool

from feedback.ai_http import admin_dependency
from feedback.ai_providers import PROVIDERS
from feedback.ai_settings import AISettingsService


class AIInput(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


class AIToolsInput(AIInput):
    search_knowledge: StrictBool
    support_link: StrictBool
    request_human: StrictBool
    create_feature_request: StrictBool
    create_bug_report: StrictBool


class AISettingsInput(AIInput):
    enabled: StrictBool
    allow_guest_reports: StrictBool
    provider: str = Field(min_length=1, max_length=64,
        json_schema_extra={'enum': list(PROVIDERS)},
        description='An application-reviewed provider adapter. Runtime validation uses the same registry.')
    model: str | None = Field(max_length=256)
    tools: AIToolsInput
    soul: str = Field(max_length=20000)
    feature_board_id: str | None = Field(max_length=100)
    bug_board_id: str | None = Field(max_length=100)
    feedback_url: str | None = Field(max_length=2048)
    feature_feedback_url: str | None = Field(max_length=2048)
    bug_feedback_url: str | None = Field(max_length=2048)
    daily_request_limit: int = Field(ge=1, le=100000)
    daily_token_limit: int = Field(ge=1, le=100000000)
    max_output_tokens: int = Field(ge=1, le=8192)
    max_tool_rounds: int = Field(ge=1, le=8)


class AISettingsWrite(AIInput):
    version: int = Field(ge=0)
    settings: AISettingsInput
    key: SecretStr | None = Field(default=None, min_length=1, max_length=4096,
        description='Omit to preserve the saved key only when the provider is unchanged. Changing provider clears an omitted key and the selected model. Use null to remove the key or supply a replacement explicitly. Never returned.')


class AIProviderAction(AIInput):
    version: int = Field(ge=0)


class AIProbeInput(AIProviderAction):
    tools: StrictBool = False


class AIKeyCreate(AIProviderAction):
    label: str = Field(min_length=1, max_length=80)
    quota_group: str = Field(default='Default account', min_length=1, max_length=80,
        description='Exact provider-account label shared across this installation. Keys in one account share limits.')
    key: SecretStr = Field(min_length=1, max_length=4096)


class AIKeyAction(AIProviderAction):
    key_version: int = Field(ge=0)


class AIKeyUpdate(AIKeyAction):
    label: str = Field(min_length=1, max_length=80)
    quota_group: str = Field(min_length=1, max_length=80)
    enabled: StrictBool
    key: SecretStr | None = Field(default=None, min_length=1, max_length=4096,
        description='Omit to preserve this key. A supplied non-null value replaces it and clears its verification.')


class AIKeyProbe(AIKeyAction):
    tools: StrictBool = False


class AIKeyOutput(BaseModel):
    id: str
    label: str
    quota_group: str
    quota_group_id: str
    version: int
    enabled: bool
    is_default: bool
    status: str
    reason: str | None
    retry_at: float | None
    has_key: bool
    catalogue_count: int
    model_available: bool
    capabilities: dict[str, bool]
    capabilities_model: str | None
    eligible: bool
    created_at: str
    updated_at: str


class AIQuotaGroupOutput(BaseModel):
    id: str
    label: str
    status: str
    reason: str | None
    retry_at: float | None
    health_epoch: int


class AIKeyPoolOutput(BaseModel):
    project_id: str
    provider: str
    version: int
    credential_version: int
    maximum_keys: int
    selected_model: str | None
    catalogue: list[str]
    verified_key_count: int
    available_key_count: int
    keys: list[AIKeyOutput]
    groups: list[AIQuotaGroupOutput]


class AIReadinessOutput(BaseModel):
    knowledge_ready: bool = False
    tools_ready: bool = False
    source_mode: str = "knowledge"
    ready: bool
    issues: list[str]


class AISettingsOutput(BaseModel):
    project_id: str
    version: int
    credential_version: int
    settings: AISettingsInput
    has_key: bool
    catalogue: list[str]
    capabilities: dict[str, bool]
    updated_at: str | None
    readiness: AIReadinessOutput


class AIProviderOutput(BaseModel):
    id: str
    name: str


class AIFeedbackDestinationsOutput(BaseModel):
    feature: str | None
    bug: str | None


class AIUsageOutput(BaseModel):
    project_id: str
    day: str
    reserved_requests: int
    reserved_tokens: int
    reported_input_tokens: int
    reported_output_tokens: int


def install_ai_settings(app, storage, vault, config, actor):
    from feedback.ai_topics_api import install_ai_topics
    install_ai_topics(app, storage, vault, actor)
    from functools import partial
    from feedback.ai_providers import provider
    service = AISettingsService(storage, vault, config.base_url, provider_factory=partial(provider, allow_internal=config.allow_internal_ai_endpoints))
    app.state.ai_settings = service
    from feedback.ai_account_keys_api import install_account_keys
    install_account_keys(app, storage, vault, actor)
    Admin = Annotated[dict, Depends(admin_dependency(actor))]

    @app.get('/api/v1/ai/providers', tags=['AI administration'], response_model=list[AIProviderOutput])
    def providers(principal: Admin):
        return service.providers(principal)

    @app.get('/api/v1/projects/{project}/ai', tags=['AI administration'], response_model=AISettingsOutput)
    def settings(project: str, principal: Admin):
        return service.read(project, principal)

    @app.get('/api/v1/projects/{project}/ai/usage', tags=['AI administration'], response_model=AIUsageOutput,
             description='Read today’s UTC usage without generation. Reserved tokens are conservative allowances, not billed cost. Reported counts include only completed usage reports; uncertain calls remain reserved.')
    def usage(project: str, principal: Admin):
        return service.usage(project, principal)

    @app.put('/api/v1/projects/{project}/ai', tags=['AI administration'], response_model=AISettingsOutput,
             description='Save the complete versioned configuration. Changing provider clears the model, catalogue and capability evidence, and clears the old key unless a replacement is explicitly supplied. Disable AI before changing provider, then refresh its models and verify the new exact selection. Every permission is explicit; saving does not contact the provider or enable permissions implicitly.')
    def save(project: str, data: AISettingsWrite, principal: Admin):
        key = {'key': data.key.get_secret_value() if data.key is not None else None} if 'key' in data.model_fields_set else {}
        return service.save(project, principal, data.version, data.settings.model_dump(), **key)

    @app.post('/api/v1/projects/{project}/ai/models/refresh', tags=['AI administration'], response_model=AISettingsOutput,
              description='Explicitly contact the saved provider to refresh exact model IDs. Catalogue membership does not verify access or capabilities.')
    async def refresh(project: str, data: AIProviderAction, principal: Admin):
        return await service.refresh_models(project, principal, data.version)

    @app.post('/api/v1/projects/{project}/ai/model/probe', tags=['AI administration'], response_model=AISettingsOutput,
              description='Explicitly run one short synthetic text request, or three requests when tools=true, using the saved key and selected model. May incur provider usage. Sends no customer content and executes no application tools.')
    async def probe(project: str, data: AIProbeInput, principal: Admin):
        return await service.probe(project, principal, data.version, tools=data.tools)

    @app.get('/api/v1/projects/{project}/ai/keys', tags=['AI administration'], response_model=AIKeyPoolOutput,
             description='Read up to99 named provider keys and safe health metadata. No secret, prefix or suffix is returned; no network I/O occurs.')
    def keys(project: str, principal: Admin):
        return service.keys(project, principal)

    @app.post('/api/v1/projects/{project}/ai/keys', tags=['AI administration'], response_model=AIKeyPoolOutput,
              description='Add an encrypted key for the selected provider. Exact-model capability probes remain explicit; a new key never resets its account cooldown.')
    def add_key(project: str, data: AIKeyCreate, principal: Admin):
        return service.add_key(project, principal, data.version, data.label, data.quota_group, data.key.get_secret_value())

    @app.put('/api/v1/projects/{project}/ai/keys/{key_id}', tags=['AI administration'], response_model=AIKeyPoolOutput,
             description='Edit metadata, pause/resume or replace a key. Rechecks settings/key versions and fences existing AI work. Replacement requires new model verification.')
    def update_key(project: str, key_id: str, data: AIKeyUpdate, principal: Admin):
        extra = {'key': data.key.get_secret_value() if data.key is not None else None} if 'key' in data.model_fields_set else {}
        return service.update_key(project, principal, data.version, key_id, data.key_version,
            label=data.label, quota_group=data.quota_group, enabled=data.enabled, **extra)

    @app.delete('/api/v1/projects/{project}/ai/keys/{key_id}', tags=['AI administration'], response_model=AIKeyPoolOutput)
    def delete_key(project: str, key_id: str, data: AIKeyAction, principal: Admin):
        return service.delete_key(project, principal, data.version, key_id, data.key_version)

    @app.post('/api/v1/projects/{project}/ai/keys/{key_id}/retry', tags=['AI administration'], response_model=AIKeyPoolOutput,
              description='Clear this provider-account cooldown across its shared keys. No network call or capability grant occurs; invalid/payment-blocked keys still need repair and a successful explicit probe.')
    def retry_key(project: str, key_id: str, data: AIKeyAction, principal: Admin):
        return service.retry_key(project, principal, data.version, key_id, data.key_version)

    @app.post('/api/v1/projects/{project}/ai/keys/{key_id}/models/refresh', tags=['AI administration'], response_model=AIKeyPoolOutput)
    async def key_models(project: str, key_id: str, data: AIKeyAction, principal: Admin):
        return await service.refresh_models(project, principal, data.version, key_id=key_id, key_version=data.key_version)

    @app.post('/api/v1/projects/{project}/ai/keys/{key_id}/model/probe', tags=['AI administration'], response_model=AIKeyPoolOutput,
              description='Explicitly probe this key on the saved exact model: one text call or three text/tool/continuation calls. No support tool executes. May consume provider quota; all current credential/configuration and health fences are rechecked.')
    async def key_probe(project: str, key_id: str, data: AIKeyProbe, principal: Admin):
        return await service.probe(project, principal, data.version, tools=data.tools, key_id=key_id, key_version=data.key_version)

    @app.get('/api/v1/projects/{project}/ai/feedback-destinations', tags=['AI administration'], response_model=AIFeedbackDestinationsOutput)
    def destinations(project: str, principal: Admin):
        return service.destinations(project, principal)
