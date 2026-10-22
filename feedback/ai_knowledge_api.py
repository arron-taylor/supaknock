"""Native-administrator Markdown knowledge transport; content stays private."""
from typing import Annotated, Literal
from uuid import UUID

from fastapi import Depends, Query
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt

from feedback.ai_http import admin_dependency
from feedback.ai_knowledge import KnowledgeService


class KnowledgeInput(BaseModel):
    model_config = ConfigDict(extra='forbid')


class MarkdownInput(KnowledgeInput):
    client_import_id: UUID
    filename: str = Field(min_length=1, max_length=255, description='A display filename ending in .md or .markdown, without directories.')
    name: str = Field(min_length=1, max_length=300)
    content: str = Field(min_length=1, max_length=65536, description='Exact UTF-8 Markdown, limited to 64 KiB after encoding. Imported as untrusted source content, never as agent instructions.')


class MarkdownReplacementInput(MarkdownInput):
    version: StrictInt = Field(ge=1)


class SourceStateInput(KnowledgeInput):
    version: StrictInt = Field(ge=1)
    active: StrictBool
    approved: StrictBool


class DocumentOutput(BaseModel):
    id: str
    source_id: str
    project_id: str
    source_version: int
    revision_number: int
    filename: str
    title: str
    content_hash: str
    byte_count: int
    created_at: str


class DocumentContentOutput(DocumentOutput):
    content: str


class KnowledgeJobOutput(BaseModel):
    id: str
    state: Literal['queued', 'running', 'succeeded', 'failed', 'cancelled']
    attempt_count: int
    error: str | None
    created_at: str
    updated_at: str


class KnowledgeSourceOutput(BaseModel):
    index_ready: bool
    id: str
    project_id: str
    kind: Literal['markdown', 'website']
    name: str
    location: str | None
    version: int
    active: bool
    approved: bool
    active_revision_id: str | None
    latest_document: DocumentOutput | None
    index_status: Literal['empty', 'pending', 'indexing', 'indexed', 'failed', 'cancelled', 'inactive', 'deleted']
    index_stale: bool
    job: KnowledgeJobOutput | None
    created_at: str
    updated_at: str
    deleted_at: str | None


class KnowledgeSourcePageOutput(BaseModel):
    items: list[KnowledgeSourceOutput]
    limit: int
    offset: int
    has_more: bool


class LibraryItemOutput(BaseModel):
    id: str
    source_id: str
    source_name: str
    kind: Literal['markdown', 'website']
    title: str
    url: str | None
    created_at: str
    ready: bool
    status: str
    excerpt: str


class LibraryContentOutput(LibraryItemOutput):
    content: str


class LibraryPageOutput(BaseModel):
    items: list[LibraryItemOutput]
    has_more: bool
    limit: int
    offset: int


class DocumentPageOutput(BaseModel):
    items: list[DocumentOutput]
    limit: int
    offset: int
    has_more: bool


class MarkdownImportOutput(BaseModel):
    source: KnowledgeSourceOutput
    document: DocumentOutput


def install_ai_knowledge(app, storage, vault, config, actor):
    service = KnowledgeService(storage)
    app.state.ai_knowledge = service
    Admin = Annotated[dict, Depends(admin_dependency(actor))]
    prefix = '/api/v1/projects/{project}/ai/knowledge'

    @app.get(prefix + '/library', response_model=LibraryPageOutput, tags=['AI knowledge'])
    def library(project: str, principal: Admin, query: str = Query(default='', max_length=200), source_id: UUID | None = None, limit: int = Query(default=25,ge=1,le=100), offset: int = Query(default=0,ge=0,le=1000000)):
        """Search original articles and website pages. Only native administrators may read this private library."""
        return service.library(project,principal,query,str(source_id) if source_id else None,limit,offset)

    @app.get(prefix + '/library/{item_id}', response_model=LibraryContentOutput, tags=['AI knowledge'])
    def library_item(project: str, item_id: UUID, principal: Admin):
        return service.library(project,principal,item_id=str(item_id))

    @app.get(prefix + '/sources', response_model=KnowledgeSourcePageOutput, tags=['AI knowledge'])
    def sources(project: str, principal: Admin, limit: Annotated[int, Query(ge=1, le=100)] = 25, offset: Annotated[int, Query(ge=0, le=1000000)] = 0):
        """Inspect project knowledge, explicit approval and actual indexing state. No source text or secrets are listed."""
        return service.sources(project, principal, limit, offset)

    @app.get(prefix + '/sources/{source_id}', response_model=KnowledgeSourceOutput, tags=['AI knowledge'])
    def source(project: str, source_id: UUID, principal: Admin):
        return service.source(project, principal, str(source_id))

    @app.post(prefix + '/markdown', status_code=201, response_model=MarkdownImportOutput, tags=['AI knowledge'])
    def import_markdown(project: str, body: MarkdownInput, principal: Admin):
        """Save one Markdown original and its indexing job atomically. New sources are unapproved. Importing never downloads a model or claims that indexing has completed; a client import ID makes retries idempotent."""
        return service.import_markdown(project, principal, str(body.client_import_id), body.filename, body.name, body.content)

    @app.put(prefix + '/sources/{source_id}/markdown', response_model=MarkdownImportOutput, tags=['AI knowledge'])
    def replace_markdown(project: str, source_id: UUID, body: MarkdownReplacementInput, principal: Admin):
        """Versioned replacement preserves original history and the last completed index. Older unfinished imports are cancelled, and a retry cannot revive them."""
        return service.import_markdown(project, principal, str(body.client_import_id), body.filename, body.name, body.content, source_id=str(source_id), version=body.version)

    @app.patch(prefix + '/sources/{source_id}', response_model=KnowledgeSourceOutput, tags=['AI knowledge'])
    def change_source(project: str, source_id: UUID, body: SourceStateInput, principal: Admin):
        """Set explicit active and approval flags. Only active, approved and fully indexed content is eligible for customer answers. A changed flag invalidates prior worker authority."""
        return service.change(project, principal, str(source_id), body.version, body.active, body.approved)

    @app.delete(prefix + '/sources/{source_id}', response_model=KnowledgeSourceOutput, tags=['AI knowledge'])
    def delete_source(project: str, source_id: UUID, principal: Admin, version: Annotated[int, Query(ge=1)]):
        """Remove this source from answers immediately and cancel its unfinished work. Preserve private original history; stale workers cannot resurrect it."""
        return service.delete(project, principal, str(source_id), version)

    @app.get(prefix + '/sources/{source_id}/documents', response_model=DocumentPageOutput, tags=['AI knowledge'])
    def documents(project: str, source_id: UUID, principal: Admin, limit: Annotated[int, Query(ge=1, le=100)] = 25, offset: Annotated[int, Query(ge=0, le=1000000)] = 0):
        """Read original revision metadata without document bodies, newest first."""
        return service.documents(project, principal, str(source_id), limit, offset)

    @app.get(prefix + '/sources/{source_id}/documents/{document_id}', response_model=DocumentContentOutput, tags=['AI knowledge'])
    def document(project: str, source_id: UUID, document_id: UUID, principal: Admin):
        """Read the exact private original as a JSON string. The content is untrusted Markdown and is never executed or rendered as HTML by this API."""
        return service.document(project, principal, str(source_id), str(document_id))
