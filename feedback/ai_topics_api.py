"""Versioned native-admin transport for optional topic restrictions."""
from typing import Annotated, Literal
from fastapi import Depends, Query
from pydantic import BaseModel, ConfigDict, Field, SecretStr, StrictBool
from feedback.ai_decision_providers import DECISION_PROVIDERS
from feedback.ai_http import admin_dependency
from feedback.ai_topic_service import TopicSettingsService


class TopicInput(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


class TopicPolicy(TopicInput):
    enabled: StrictBool
    provider: str = Field(min_length=1, max_length=64, json_schema_extra={'enum': list(DECISION_PROVIDERS)})
    model: str | None = Field(max_length=256)
    scope: str = Field(max_length=4000)
    public_summary: str = Field(max_length=240)
    allowed_examples: list[str] = Field(max_length=10)
    disallowed_examples: list[str] = Field(max_length=10)
    preset: Literal['balanced', 'strict']
    refusal_messages: list[str] = Field(max_length=5)


class TopicWrite(TopicInput):
    version: int = Field(ge=0)
    policy: TopicPolicy


class DecisionKeyCreate(TopicInput):
    version: int = Field(ge=0)
    label: str = Field(min_length=1, max_length=80)
    quota_group: str = Field(default='Default account', min_length=1, max_length=80)
    key: SecretStr | None = Field(default=None, min_length=1, max_length=4096)
    reuse_key_id: str | None = Field(default=None, min_length=1, max_length=100)
    account_id: str | None = Field(default=None, min_length=32, max_length=32, pattern=r'^[0-9a-f]{32}$')


class DecisionKeyAction(TopicInput):
    version: int = Field(ge=0)
    key_version: int = Field(ge=1)


class DecisionContext(TopicInput):
    sender_role: Literal['customer', 'ai', 'admin', 'visitor']
    body: str = Field(max_length=16000)


class DecisionTest(DecisionKeyAction):
    question: str = Field(min_length=1, max_length=4000)
    history: list[DecisionContext] = Field(default_factory=list, max_length=4)


class DecisionKeyOutput(BaseModel):
    id: str
    label: str
    version: int
    quota_group: str
    enabled: bool
    has_key: bool
    shared_with_generation: bool
    catalogue: list[str]
    verified: bool
    retry_at: float | None
    reason: str | None


class TopicOutput(BaseModel):
    project_id: str
    version: int
    policy: TopicPolicy
    updated_at: str | None
    keys: list[DecisionKeyOutput]
    preset_version: str
    maximum_keys: int


class DecisionTestOutput(BaseModel):
    outcome: Literal['allow', 'refuse', 'clarify', 'unavailable']
    probability: float | None
    model: str
    preset: Literal['balanced', 'strict']
    preset_version: str
    reply: str | None
    error: str | None


class DecisionProviderOutput(BaseModel):
    id: str
    name: str
    available: bool
    status: str
    requires_account_id: bool


class DecisionHistoryItem(BaseModel):
    job_id: str
    attempt: int
    policy_version: int
    preset_version: str
    outcome: str
    probability: float | None
    model: str | None
    error: str | None
    created_at: str


class DecisionHistoryOutput(BaseModel):
    items: list[DecisionHistoryItem]
    has_more: bool
    limit: int
    offset: int


def install_ai_topics(app, storage, vault, actor):
    service = TopicSettingsService(storage, vault)
    app.state.ai_topics = service
    Admin = Annotated[dict, Depends(admin_dependency(actor))]

    @app.get('/api/v1/ai/decision-providers', tags=['AI administration'], response_model=list[DecisionProviderOutput])
    def providers(principal: Admin):
        storage.ai_runtime(principal)
        return [{'id': p.id, 'name': p.name, 'available': p.transport_verified,
                 'status': 'wire_supported' if p.transport_verified else 'typed_gateway_unavailable',
                 'requires_account_id': getattr(p, 'requires_account_id', False)} for p in DECISION_PROVIDERS.values()]

    @app.get('/api/v1/projects/{project}/ai/topics', tags=['AI administration'], response_model=TopicOutput)
    def settings(project: str, principal: Admin):
        return service.read(project, principal)

    @app.get('/api/v1/projects/{project}/ai/topics/decisions', tags=['AI administration'], response_model=DecisionHistoryOutput,
             description='Native-admin-only decision metadata. Excludes messages, context, scope, credentials and key labels.')
    def decisions(project: str, principal: Admin, limit: int = Query(25, ge=1, le=100), offset: int = Query(0, ge=0)):
        return storage.ai_topic_decisions(project, principal, limit, offset)

    @app.put('/api/v1/projects/{project}/ai/topics', tags=['AI administration'], response_model=TopicOutput,
             description='Independent default-off policy. Requires exact decision capability to enable. Scope changes fence late work; personality grants no authority.')
    def save(project: str, data: TopicWrite, principal: Admin):
        return service.save(project, principal, data.version, data.policy.model_dump())

    @app.post('/api/v1/projects/{project}/ai/topics/keys', tags=['AI administration'], response_model=TopicOutput)
    def add(project: str, data: DecisionKeyCreate, principal: Admin):
        return service.add_key(project, principal, data.version, data.label, data.quota_group,
            key=data.key.get_secret_value() if data.key else None, reuse_key_id=data.reuse_key_id, account_id=data.account_id)

    @app.post('/api/v1/projects/{project}/ai/topics/keys/{key_id}/remove', tags=['AI administration'], response_model=TopicOutput)
    def remove(project: str, key_id: str, data: DecisionKeyAction, principal: Admin):
        return service.remove_key(project, principal, data.version, key_id, data.key_version)

    @app.post('/api/v1/projects/{project}/ai/topics/keys/{key_id}/models/refresh', tags=['AI administration'], response_model=TopicOutput)
    async def refresh(project: str, key_id: str, data: DecisionKeyAction, principal: Admin):
        return await service.refresh(project, principal, data.version, key_id, data.key_version)

    @app.post('/api/v1/projects/{project}/ai/topics/keys/{key_id}/probe', tags=['AI administration'], response_model=DecisionTestOutput,
              description='Explicit synthetic typed capability probe. Budgeted before dispatch. Unverified gateway transport fails before billing; catalogue membership is insufficient.')
    async def probe(project: str, key_id: str, data: DecisionKeyAction, principal: Admin):
        return await service.test(project, principal, data.version, key_id, data.key_version, '', probe=True)

    @app.post('/api/v1/projects/{project}/ai/topics/keys/{key_id}/test', tags=['AI administration'], response_model=DecisionTestOutput,
              description='Decision-only test of saved scope and synthetic context. No retrieval, generation or tools; never enables the gate.')
    async def test(project: str, key_id: str, data: DecisionTest, principal: Admin):
        return await service.test(project, principal, data.version, key_id, data.key_version, data.question,
                                  [item.model_dump() for item in data.history])
