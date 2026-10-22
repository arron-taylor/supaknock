"""Account library and explicit project bindings, native-admin transport only."""
from typing import Annotated, Literal
from fastapi import Depends
from pydantic import BaseModel, Field, SecretStr, StrictBool
from feedback.ai_http import admin_dependency
from feedback.ai_account_keys import AccountKeysService
from feedback.ai_settings_api import AIInput, AIKeyPoolOutput
from feedback.ai_topics_api import TopicOutput


class AccountKeyCreate(AIInput):
    version: int = Field(ge=0)
    provider: str = Field(min_length=1,max_length=64)
    purpose: Literal['generation','decision','both']
    label: str = Field(min_length=1,max_length=80)
    quota_group: str = Field(default='Default account',min_length=1,max_length=80)
    key: SecretStr = Field(min_length=0,max_length=4096)
    account_id: str | None = Field(default=None,pattern=r'^[0-9a-f]{32}$')
    base_url: str | None = Field(default=None, max_length=1024)
    models: list[str] | None = Field(default=None, max_length=100)


class AccountKeyAction(AIInput):
    version: int = Field(ge=0)
    key_version: int = Field(ge=1)


class AccountKeyUpdate(AccountKeyAction):
    purpose: Literal['generation','decision','both'] | None = None
    label: str = Field(min_length=1,max_length=80)
    quota_group: str = Field(min_length=1,max_length=80)
    enabled: StrictBool
    key: SecretStr | None = Field(default=None,min_length=0,max_length=4096)
    account_id: str | None = Field(default=None,pattern=r'^[0-9a-f]{32}$')
    base_url: str | None = Field(default=None, max_length=1024)
    models: list[str] | None = Field(default=None, max_length=100)


class AccountKeyModels(AccountKeyAction):
    role: Literal['generation','decision']


class ProjectKeySelection(AIInput):
    version: int = Field(ge=0)
    library_version: int = Field(ge=0)
    key_ids: list[str] = Field(max_length=99)


class BoundProject(BaseModel):
    id: str
    name: str
    role: Literal['generation','decision']


class VerifiedModel(BaseModel):
    role: Literal['generation','decision']
    model: str
    capabilities: dict[str,bool]


class AccountKeyOutput(BaseModel):
    id: str
    provider: str
    label: str
    version: int
    purpose: Literal['generation','decision','both']
    created_at: str
    updated_at: str
    quota_group: str
    enabled: bool
    has_key: bool
    projects: list[BoundProject]
    catalogue: list[str]
    decision_catalogue: list[str]
    verified_models: list[VerifiedModel]
    status: str
    reason: str | None
    retry_at: float | None


class AccountKeysOutput(BaseModel):
    version: int
    maximum_keys: int
    keys: list[AccountKeyOutput]


def install_account_keys(app,storage,vault,actor):
    service=AccountKeysService(storage,vault,app.state.ai_settings,app.state.ai_topics,allow_internal=app.state.config.allow_internal_ai_endpoints)
    app.state.ai_account_keys=service
    Admin=Annotated[dict,Depends(admin_dependency(actor))]

    @app.get('/api/v1/ai/keys',tags=['AI administration'],response_model=AccountKeysOutput,
             description='Installation-wide native-admin credential library. Safe metadata and affected projects only; secrets never returned.')
    def read(principal: Admin):
        return service.read(principal)

    @app.post('/api/v1/ai/keys',tags=['AI administration'],response_model=AccountKeysOutput,
              description='Add an encrypted account credential with explicit provider roles. No project binding or opt-in is created.')
    def add(data: AccountKeyCreate,principal: Admin):
        return service.add(principal,data.version,data.provider,data.purpose,data.label,data.quota_group,data.key.get_secret_value(),data.account_id,data.base_url,data.models)

    @app.put('/api/v1/ai/keys/{key_id}',tags=['AI administration'],response_model=AccountKeysOutput,
             description='Edit, pause/resume or replace a shared key. Fences every bound project. Replacement clears all model/role evidence.')
    def update(key_id: str,data: AccountKeyUpdate,principal: Admin):
        extra={'key':data.key.get_secret_value() if data.key is not None else None} if 'key' in data.model_fields_set else {}
        return service.update(principal,data.version,key_id,data.key_version,data.label,data.quota_group,data.enabled,account_id=data.account_id,purpose=data.purpose,base_url=data.base_url,models=data.models,**extra)

    @app.delete('/api/v1/ai/keys/{key_id}',tags=['AI administration'],response_model=AccountKeysOutput)
    def delete(key_id: str,data: AccountKeyAction,principal: Admin):
        return service.action(principal,data.version,key_id,data.key_version,'delete')

    @app.post('/api/v1/ai/keys/{key_id}/retry',tags=['AI administration'],response_model=AccountKeysOutput,
              description='Explicitly reset the shared account cooldown. Capability and permanent blocks remain; recovery stays fenced.')
    def retry(key_id: str,data: AccountKeyAction,principal: Admin):
        return service.action(principal,data.version,key_id,data.key_version,'retry')

    @app.post('/api/v1/ai/keys/{key_id}/models/refresh',tags=['AI administration'],response_model=AccountKeysOutput,
              description='Explicitly refresh this credential’s role-specific models. Does not verify capabilities or enable any project.')
    async def refresh(key_id: str,data: AccountKeyModels,principal: Admin):
        return await service.refresh(principal,data.version,key_id,data.key_version,data.role)

    @app.put('/api/v1/projects/{project}/ai/keys/selection',tags=['AI administration'],response_model=AIKeyPoolOutput,
             description='Explicit generation key bindings. Detachment never destroys account credentials. Exact-model verification still required.')
    def generation(project: str,data: ProjectKeySelection,principal: Admin):
        return service.select(project,principal,data.version,data.library_version,'generation',data.key_ids)

    @app.put('/api/v1/projects/{project}/ai/topics/keys/selection',tags=['AI administration'],response_model=TopicOutput,
             description='Explicit decision key bindings. Unavailable credentials never switch an enabled topic gate off.')
    def decisions(project: str,data: ProjectKeySelection,principal: Admin):
        return service.select(project,principal,data.version,data.library_version,'decision',data.key_ids)
