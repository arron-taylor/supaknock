"""Explicit, native-administrator website import transport."""
from typing import Annotated, Literal
from uuid import UUID

from fastapi import Depends, Query
from pydantic import BaseModel, ConfigDict, Field, StrictInt

from feedback.ai_crawler import CrawlService, CrawlWorker
from feedback.ai_http import admin_dependency
from feedback.ai_knowledge_api import KnowledgeSourceOutput


class CrawlConfigInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    seed_url: str = Field(min_length=1, max_length=2048)
    approved_origins: list[str] = Field(min_length=1, max_length=5, description='Explicit HTTP(S) origins. Subdomains and changed schemes are separate origins.')
    include_paths: list[str] = Field(default_factory=lambda: ['/'], min_length=1, max_length=20)
    exclude_paths: list[str] = Field(default_factory=list, max_length=20)
    max_pages: StrictInt = Field(default=20, ge=1, le=50)
    max_depth: StrictInt = Field(default=3, ge=0, le=5)


class WebsiteInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    client_import_id: UUID
    name: str = Field(min_length=1, max_length=300)
    config: CrawlConfigInput


class WebsiteReplacementInput(WebsiteInput):
    version: StrictInt = Field(ge=1)


class CrawlCancelInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    version: StrictInt = Field(ge=1)


class CrawlPageOutput(BaseModel):
    id: str
    attempt: int
    url: str
    title: str
    content: str
    content_hash: str
    status: Literal['fetched', 'skipped', 'failed']
    error: str | None
    created_at: str


class CrawlRunOutput(BaseModel):
    id: str
    project_id: str
    source_id: str
    source_version: int
    version: int
    config: CrawlConfigInput
    created_at: str
    finished_at: str | None
    document_id: str | None
    status: Literal['queued', 'running', 'succeeded', 'failed', 'cancelled']
    error: str | None
    attempt_count: int
    stop_reason: Literal['page_limit'] | None
    fetched_count: int
    skipped_count: int
    failed_count: int


class CrawlRunDetailOutput(CrawlRunOutput):
    pages: list[CrawlPageOutput]


class CrawlRunPageOutput(BaseModel):
    items: list[CrawlRunOutput]
    limit: int
    offset: int
    has_more: bool


class WebsiteImportOutput(BaseModel):
    source: KnowledgeSourceOutput
    run: CrawlRunDetailOutput


def install_ai_crawler(app, storage, vault, config, actor):
    worker = CrawlWorker(storage)
    service = CrawlService(storage, worker)
    app.state.ai_crawl_worker = worker
    app.state.ai_crawler = service
    Admin = Annotated[dict, Depends(admin_dependency(actor))]
    prefix = '/api/v1/projects/{project}/ai/knowledge'

    @app.post(prefix + '/websites', status_code=201, response_model=WebsiteImportOutput, tags=['AI knowledge'])
    def import_website(project: str, body: WebsiteInput, principal: Admin):
        """Queue an explicit public website crawl. New sources are unapproved. The import ID makes retries idempotent; reading settings never starts a crawl."""
        return service.begin(project, principal, str(body.client_import_id), body.name, body.config.model_dump())

    @app.put(prefix + '/sources/{source_id}/website', response_model=WebsiteImportOutput, tags=['AI knowledge'])
    def refresh_website(project: str, source_id: UUID, body: WebsiteReplacementInput, principal: Admin):
        """Refresh a versioned website source. The configured page cap is successful bounded completion, reported as stop_reason=page_limit. Unusable individual URLs are skipped; usable pages are published. Imports with no usable pages, fatal resource failures or cancellation retain the previous original and index."""
        return service.begin(project, principal, str(body.client_import_id), body.name, body.config.model_dump(), source_id=str(source_id), version=body.version)

    @app.get(prefix + '/sources/{source_id}/crawls', response_model=CrawlRunPageOutput, tags=['AI knowledge'])
    def runs(project: str, source_id: UUID, principal: Admin, limit: Annotated[int, Query(ge=1, le=100)] = 25, offset: Annotated[int, Query(ge=0, le=1000000)] = 0):
        """Inspect bounded crawl history without source bodies."""
        return service.runs(project, principal, str(source_id), limit, offset)

    @app.get(prefix + '/sources/{source_id}/crawls/{run_id}', response_model=CrawlRunDetailOutput, tags=['AI knowledge'])
    def run(project: str, source_id: UUID, run_id: UUID, principal: Admin):
        """Inspect the current attempt's page outcomes and extracted private originals. Source content is untrusted text, never executable HTML or instructions."""
        return service.run(project, principal, str(source_id), str(run_id))

    @app.post(prefix + '/sources/{source_id}/crawls/{run_id}/cancel', response_model=CrawlRunDetailOutput, tags=['AI knowledge'])
    def cancel(project: str, source_id: UUID, run_id: UUID, body: CrawlCancelInput, principal: Admin):
        """Cancel unfinished crawl work with a version fence; a stale worker cannot publish after cancellation."""
        return service.cancel(project, principal, str(source_id), str(run_id), body.version)
