"""Native administrator previews, internal knowledge drafts and personality text."""
from typing import Annotated, Literal
from uuid import UUID

from fastapi import Depends, Query
from feedback.ai_limits import MAX_SOURCES
from pydantic import BaseModel, ConfigDict, Field, StrictInt

from feedback.ai_authoring import AuthoringService, AuthoringWorker
from feedback.ai_http import admin_dependency


class Input(BaseModel):
    model_config = ConfigDict(extra='forbid')


class PreviewInput(Input):
    client_request_id: UUID
    version: StrictInt = Field(ge=0, description='Current project AI configuration version.')
    question: str = Field(min_length=1, max_length=1000)


class SectionsInput(Input):
    client_request_id: UUID
    version: StrictInt = Field(ge=0)
    source_version: StrictInt = Field(ge=1)
    page_ids: list[UUID] | None = Field(default=None, min_length=1, max_length=50)


class VersionInput(Input):
    version: StrictInt = Field(ge=1)


class RetryInput(VersionInput):
    client_request_id: UUID


class SectionInput(VersionInput):
    title: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=4000)
    category: str = Field(min_length=1, max_length=100)
    citation_ids: list[UUID] = Field(min_length=1, max_length=MAX_SOURCES)


class PersonalityInput(Input):
    version: StrictInt = Field(ge=0)
    filename: Literal['soul.md'] = 'soul.md'
    content: str = Field(max_length=20000, description='UTF-8 personality Markdown. This controls presentation only, never evidence requirements or tool permissions.')


class PersonalityOutput(BaseModel):
    version: int
    filename: Literal['soul.md']
    content: str


class CitationOutput(BaseModel):
    id: str
    source_id: str
    revision_id: str
    kind: Literal['markdown', 'website']
    title: str
    excerpt: str
    url: str | None


class DraftSectionOutput(BaseModel):
    kind: Literal['section', 'faq']
    title: str
    body: str
    category: str
    citation_ids: list[str]


class SectionOutput(DraftSectionOutput):
    id: str
    run_id: str
    version: int
    stale: bool
    deleted_at: str | None
    published_source_id: str | None = None
    citations: list[CitationOutput]


class SectionPageOutput(BaseModel):
    items: list[SectionOutput]
    limit: int
    offset: int
    has_more: bool


class PreviewResultOutput(BaseModel):
    body: str
    citation_ids: list[str]
    knowledge_version: int
    status: Literal['answered', 'insufficient', 'refuse', 'clarify', 'greeting', 'unavailable']


class SectionsResultOutput(BaseModel):
    sections: list[DraftSectionOutput]
    knowledge_version: int


class CoverageOutput(BaseModel):
    scope: str | None = None
    page_ids: list[str] | None = None
    selected_passages: int
    total_passages: int
    batch_size: int | None = None
    total_batches: int | None = None
    completed_batches: int | None = None
    completed_passages: int | None = None


class AuthoringRunOutput(BaseModel):
    id: str
    project_id: str
    kind: Literal['preview', 'sections']
    config_version: int
    knowledge_version: int
    source_id: str | None
    source_version: int | None
    query: str | None
    status: Literal['queued', 'running', 'succeeded', 'partial', 'failed', 'cancelled']
    error: str | None
    version: int
    created_at: str
    finished_at: str | None
    result: PreviewResultOutput | SectionsResultOutput | None
    sections: list[SectionOutput]
    section_count: int = 0
    citations: list[CitationOutput]
    coverage: CoverageOutput | None
    stale: bool


class AuthoringRunPageOutput(BaseModel):
    items: list[AuthoringRunOutput]
    has_more: bool
    limit: int
    offset: int


def install_ai_authoring(app, storage, vault, config, actor):
    worker = AuthoringWorker(storage, app.state.ai_generation)
    service = AuthoringService(storage, worker, app.state.ai_settings)
    app.state.ai_authoring, app.state.ai_authoring_worker = service, worker
    Admin = Annotated[dict, Depends(admin_dependency(actor))]
    prefix = '/api/v1/projects/{project}/ai'

    @app.post(prefix + '/previews', status_code=202, response_model=AuthoringRunOutput, tags=['AI authoring'])
    def preview(project: str, body: PreviewInput, principal: Admin):
        """Queue an explicit grounded preview with every assistant tool disabled. It may use provider quota while project AI is off; it never creates a customer message or enables a setting."""
        return service.preview(project, principal, str(body.client_request_id), body.version, body.question)

    @app.post(prefix + '/knowledge/sources/{source_id}/sections/generate', status_code=202, response_model=AuthoringRunOutput, tags=['AI authoring'])
    def sections(project: str, source_id: UUID, body: SectionsInput, principal: Admin):
        """Queue reviewable drafts from all indexed passages in the source, or selected website pages, in recoverable batches. Coverage is explicit. Drafts stay private until separately approved as an independent article; existing sources are never overwritten."""
        return service.sections(project, principal, str(body.client_request_id), body.version, str(source_id), body.source_version, [str(x) for x in body.page_ids] if body.page_ids else None)

    @app.get(prefix + '/authoring-runs', response_model=AuthoringRunPageOutput, tags=['AI authoring'])
    def runs(project: str, principal: Admin, kind: Literal['preview', 'sections'] | None = None, limit: Annotated[int, Query(ge=1, le=100)] = 25, offset: Annotated[int, Query(ge=0, le=1000000)] = 0):
        return service.runs(project, principal, kind, limit, offset)

    @app.get(prefix + '/authoring-runs/{run_id}', response_model=AuthoringRunOutput, tags=['AI authoring'])
    def run(project: str, run_id: UUID, principal: Admin):
        """Inspect durable progress, result, original citations and staleness without provider credentials or worker leases."""
        return service.run(project, principal, str(run_id))

    @app.post(prefix + '/authoring-runs/{run_id}/cancel', response_model=AuthoringRunOutput, tags=['AI authoring'])
    def cancel(project: str, run_id: UUID, body: VersionInput, principal: Admin):
        """Fence unfinished work immediately. Cancellation does not promise a refund for an already dispatched provider request."""
        return service.cancel(project, principal, str(run_id), body.version)

    @app.post(prefix + '/authoring-runs/{run_id}/retry', status_code=202, response_model=AuthoringRunOutput, tags=['AI authoring'])
    def retry(project: str, run_id: UUID, body: RetryInput, principal: Admin):
        """Explicitly resume unfinished section batches. Saved drafts and paid receipts remain; changed settings or evidence require a new run."""
        return service.retry(project, principal, str(run_id), body.version, str(body.client_request_id))

    @app.get(prefix + '/knowledge/sections', response_model=SectionPageOutput, tags=['AI authoring'])
    def drafts(project: str, principal: Admin, query: str = Query(default='', max_length=200), limit: int = Query(default=25, ge=1, le=100), offset: int = Query(default=0, ge=0, le=1000000)):
        return service.drafts(project, principal, query, limit, offset)

    from feedback.ai_knowledge_api import KnowledgeSourceOutput

    @app.post(prefix + '/knowledge/sections/{section_id}/approve', response_model=KnowledgeSourceOutput, tags=['AI authoring'])
    def publish(project: str, section_id: UUID, body: VersionInput, principal: Admin):
        """Explicitly adopt the reviewed saved draft as an approved original. Idempotent per draft version; local indexing follows. Existing sources are not overridden."""
        return service.publish(project, principal, str(section_id), body.version)

    @app.put(prefix + '/knowledge/sections/{section_id}', response_model=SectionOutput, tags=['AI authoring'])
    def edit(project: str, section_id: UUID, body: SectionInput, principal: Admin):
        """Version an internal draft, retaining source provenance and immutable edit history. Draft edits never modify originals or embeddings."""
        value = body.model_dump(exclude={'version'})
        value['citation_ids'] = [str(cid) for cid in body.citation_ids]
        return service.edit(project, principal, str(section_id), body.version, value)

    @app.delete(prefix + '/knowledge/sections/{section_id}', response_model=SectionOutput, tags=['AI authoring'])
    def delete(project: str, section_id: UUID, principal: Admin, version: Annotated[int, Query(ge=1)]):
        """Remove an internal draft with a versioned tombstone while preserving its history."""
        return service.delete(project, principal, str(section_id), version)

    @app.get(prefix + '/personality', response_model=PersonalityOutput, tags=['AI authoring'])
    def personality(project: str, principal: Admin):
        return service.personality(project, principal)

    @app.put(prefix + '/personality', response_model=PersonalityOutput, tags=['AI authoring'])
    def save_personality(project: str, body: PersonalityInput, principal: Admin):
        """Save soul.md as versioned project text. Changing personality fences old generation; saving performs no provider request or tool action."""
        return service.save_personality(project, principal, body.version, body.content)
