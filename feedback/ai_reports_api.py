"""Customer-only review, confirmation and ownership APIs for assistant drafts."""
from typing import Annotated, Literal
from uuid import UUID

from fastapi import Depends, Query
from pydantic import BaseModel, ConfigDict, Field, StrictInt

from feedback.ai_tools import ReportService


class Input(BaseModel):
    model_config = ConfigDict(extra='forbid')


class VersionInput(Input):
    version: StrictInt = Field(ge=1)


class DraftInput(VersionInput):
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(max_length=20000)


class ConfirmationInput(VersionInput):
    draft_hash: str = Field(pattern=r'^[0-9a-f]{64}$')
    client_request_id: UUID


class BoardOutput(BaseModel):
    id: str
    name: str


class ProposalOutput(BaseModel):
    id: str
    conversation_id: str
    message_sequence: int | None = Field(default=None, description='Owning chat message position for inline review; never a credential.')
    kind: Literal['feature', 'bug']
    board: BoardOutput
    title: str
    description: str
    version: int
    draft_hash: str
    state: Literal['pending', 'confirmed', 'cancelled']
    expires_at: str | None
    can_confirm: bool
    can_edit: bool
    guest: bool
    feedback_url: str
    tracking_notice: str | None
    report_id: str | None
    copy_text: str


class ProposalPageOutput(BaseModel):
    items: list[ProposalOutput]
    limit: int
    offset: int
    has_more: bool


class ReportOutput(BaseModel):
    id: str
    conversation_id: str
    message_sequence: int | None = Field(default=None, description='Original chat position, retained after submission and status changes.')
    request_id: str
    kind: Literal['feature', 'bug']
    title: str
    description: str
    moderation: Literal['pending', 'accepted', 'rejected'] | None
    status: Literal['under_consideration', 'planned', 'in_progress', 'shipped'] | None
    accepted_at: str | None
    version: int
    deleted: bool
    guest: bool
    can_edit: bool
    can_delete: bool
    feedback_url: str
    public_url: str | None
    tracking_notice: str | None
    created_at: str


class ReportPageOutput(BaseModel):
    items: list[ReportOutput]
    limit: int
    offset: int
    has_more: bool


def install_ai_reports(app, storage):
    service = ReportService(storage)
    app.state.ai_reports = service
    Principal = Annotated[dict, Depends(app.state.chat_principal)]
    prefix = '/api/v1/chat/conversations/{conversation_id}'

    @app.get(prefix + '/proposals', response_model=ProposalPageOutput, tags=['Chat reports'])
    def proposals(conversation_id: UUID, principal: Principal,
                  limit: Annotated[int, Query(ge=1, le=100)] = 25,
                  offset: Annotated[int, Query(ge=0, le=1000000)] = 0):
        """Review private drafts owned by this chat customer. Reading never submits a request."""
        return service.proposals(str(conversation_id), principal, limit, offset)

    @app.get(prefix + '/proposals/{proposal_id}', response_model=ProposalOutput, tags=['Chat reports'])
    def proposal(conversation_id: UUID, proposal_id: UUID, principal: Principal):
        return service.proposal(str(conversation_id), principal, str(proposal_id))

    @app.put(prefix + '/proposals/{proposal_id}', response_model=ProposalOutput, tags=['Chat reports'])
    def edit_proposal(conversation_id: UUID, proposal_id: UUID, body: DraftInput, principal: Principal):
        """Edit the exact private draft. Changes advance its version and invalidate the previous confirmation hash."""
        return service.edit_proposal(str(conversation_id), principal, str(proposal_id), body.version, body.title, body.description)

    @app.post(prefix + '/proposals/{proposal_id}/cancel', response_model=ProposalOutput, tags=['Chat reports'])
    def cancel_proposal(conversation_id: UUID, proposal_id: UUID, body: VersionInput, principal: Principal):
        return service.cancel_proposal(str(conversation_id), principal, str(proposal_id), body.version)

    @app.post(prefix + '/proposals/{proposal_id}/confirm', response_model=ReportOutput, tags=['Chat reports'])
    def confirm(conversation_id: UUID, proposal_id: UUID, body: ConfirmationInput, principal: Principal):
        """Explicitly confirm the displayed draft version and hash. The customer must see its destination, visibility and tracking notice first. This creates one pending request atomically; repeating the same client request ID returns its receipt. Guest creation requires a separately enabled policy and the original website chat credential."""
        return service.confirm(str(conversation_id), principal, str(proposal_id), body.version, body.draft_hash, str(body.client_request_id))

    @app.get(prefix + '/reports', response_model=ReportPageOutput, tags=['Chat reports'])
    def reports(conversation_id: UUID, principal: Principal,
                limit: Annotated[int, Query(ge=1, le=100)] = 25,
                offset: Annotated[int, Query(ge=0, le=1000000)] = 0):
        """Private receipts remain available to the owning current chat credential after new report creation is disabled."""
        return service.reports(str(conversation_id), principal, limit, offset)

    @app.get(prefix + '/reports/{report_id}', response_model=ReportOutput, tags=['Chat reports'])
    def report(conversation_id: UUID, report_id: UUID, principal: Principal):
        return service.report(str(conversation_id), principal, str(report_id))

    @app.put(prefix + '/reports/{report_id}', response_model=ReportOutput, tags=['Chat reports'])
    def edit_report(conversation_id: UUID, report_id: UUID, body: DraftInput, principal: Principal):
        """Edit an owned report before its first acceptance. Prior acceptance permanently locks creator edits even if moderation later changes."""
        return service.edit_report(str(conversation_id), principal, str(report_id), body.version, body.title, body.description)

    @app.delete(prefix + '/reports/{report_id}', response_model=ReportOutput, tags=['Chat reports'])
    def delete_report(conversation_id: UUID, report_id: UUID, principal: Principal, version: Annotated[int, Query(ge=1)]):
        """Delete an owned report only before its first acceptance, retaining its private durable receipt."""
        return service.delete_report(str(conversation_id), principal, str(report_id), version)
