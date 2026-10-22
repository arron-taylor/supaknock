"""Native administrator transport for explicit local model operations."""
from typing import Annotated, Literal

from fastapi import Depends
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from feedback.ai_http import admin_dependency


class RuntimeAction(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    version: int = Field(ge=0)


class RuntimeDownload(RuntimeAction):
    token: SecretStr = Field(min_length=1, max_length=2048)


class RuntimeOutput(BaseModel):
    version: int
    state: Literal['not_downloaded', 'downloading', 'verifying', 'ready', 'removing', 'failed']
    operation_id: str | None
    action: Literal['download', 'remove'] | None
    progress_bytes: int
    total_bytes: int
    attempt_count: int
    error: str | None
    updated_at: str | None
    active_cache_id: str | None
    active_manifest: dict | None
    model_id: str
    download_bytes: int
    license_url: str


def install_ai_runtime(app, installation, actor):
    Admin = Annotated[dict, Depends(admin_dependency(actor))]

    @app.get('/api/v1/ai/runtime', tags=['AI administration'], response_model=RuntimeOutput)
    def status(principal: Admin):
        return installation.status(principal)

    @app.post('/api/v1/ai/runtime/download', tags=['AI administration'], response_model=RuntimeOutput,
              description='Explicitly download the pinned EmbeddingGemma model. Accept its license in your own Hugging Face account first. The access token is encrypted for the duration of this operation and never returned.')
    def download(data: RuntimeDownload, principal: Admin):
        installation.download(principal, data.version, data.token.get_secret_value())
        return installation.status(principal)

    @app.post('/api/v1/ai/runtime/cancel', tags=['AI administration'], response_model=RuntimeOutput)
    def cancel(data: RuntimeAction, principal: Admin):
        installation.cancel(principal, data.version)
        return installation.status(principal)

    @app.post('/api/v1/ai/runtime/remove', tags=['AI administration'], response_model=RuntimeOutput,
              description='Disable AI and remove local model files. Knowledge documents remain stored. Removal cannot be cancelled after it starts.')
    def remove(data: RuntimeAction, principal: Admin):
        installation.remove(principal, data.version)
        return installation.status(principal)
