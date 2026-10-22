"""Native administrator controls for optional external embedding endpoints."""
from typing import Annotated, Literal
from fastapi import Depends
from pydantic import BaseModel, ConfigDict, Field, SecretStr, StrictBool
from feedback.ai_http import admin_dependency


class EmbeddingConfig(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    base_url: str = Field(min_length=1, max_length=1024)
    model: str = Field(min_length=1, max_length=256)
    dimensions: int = Field(ge=1, le=4096)
    send_dimensions: StrictBool = False
    query_prefix: str = Field(default='', max_length=256)
    document_prefix: str = Field(default='', max_length=256)


class EmbeddingWrite(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    version: int = Field(ge=0)
    mode: Literal['local', 'external']
    config: EmbeddingConfig | None = None
    key: SecretStr | None = Field(default=None, max_length=4096)


class ReindexInput(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    version: int = Field(ge=0)


class ReindexOutput(BaseModel):
    queued: int
    remaining: int


class EmbeddingOutput(BaseModel):
    version: int
    mode: Literal['local', 'external']
    config: EmbeddingConfig | None
    profile: dict
    has_key: bool
    updated_at: str | None
    reindex: ReindexOutput | None = None


def install_embeddings(app, service, actor):
    Admin = Annotated[dict, Depends(admin_dependency(actor))]

    @app.get('/api/v1/ai/embeddings', tags=['AI administration'], response_model=EmbeddingOutput)
    def status(principal: Admin):
        return service.status(principal)

    @app.put('/api/v1/ai/embeddings', tags=['AI administration'], response_model=EmbeddingOutput,
             description='Choose local EmbeddingGemma or an OpenAI-compatible embeddings endpoint. External selection tests synthetic query/document inputs before saving. It shares approved passages and retrieval questions with that endpoint. Changes disable project AI, cancel stale work and queue reindexing of approved originals. Secrets are encrypted and never returned. Re-enable AI explicitly after indexing; no automatic fallback.')
    async def configure(data: EmbeddingWrite, principal: Admin):
        return await service.configure(principal, data.version, data.mode, data.config.model_dump() if data.config else None, data.key.get_secret_value() if data.key is not None else None)

    @app.post('/api/v1/ai/embeddings/reindex', tags=['AI administration'], response_model=ReindexOutput,
              description='Queue remaining approved sources for the current embedding profile within existing queue limits. Repeat after queued work finishes if remaining is nonzero.')
    def reindex(data: ReindexInput, principal: Admin):
        return service.reindex(principal, data.version)
