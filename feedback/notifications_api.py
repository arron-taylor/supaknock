"""Shared notification inbox transport. Cookie CSRF and native sessions use the common actor dependency."""
from typing import Annotated, Literal
from uuid import UUID
from fastapi import Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from feedback.domain import Actor
from feedback.notifications import NotificationService


class NotificationOutput(BaseModel):
    id: str
    sequence: int
    type: Literal['request.created', 'request.resubmitted', 'chat.message_created', 'chat.support_mode_changed', 'form.submission_created']
    project_id: str
    project_name: str
    title: str
    created_at: str
    read_at: str | None
    resource_kind: Literal['request', 'conversation', 'form_submission']
    resource_id: str
    url: str
    resource_available: bool


class NotificationSummaryOutput(BaseModel):
    unread_count: int
    through_sequence: int


class NotificationPageOutput(NotificationSummaryOutput):
    items: list[NotificationOutput]
    limit: int
    offset: int
    has_more: bool


class NotificationReadInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    read: bool


class NotificationReadAllInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    through_sequence: int = Field(ge=0, le=9223372036854775807,
                                 description='Use the through_sequence returned by the displayed inbox or summary. Later arrivals stay unread.')


def install_notifications(app, storage, actor):
    service = NotificationService(storage)
    app.state.notifications = service
    Current = Annotated[Actor, Depends(actor)]

    def session_id(request):
        return getattr(request.state, 'session', {}).get('session_id')

    @app.get('/api/v1/admin/notifications', response_model=NotificationPageOutput, tags=['Notifications'])
    def notifications(request: Request, a: Current,
                      unread_only: bool = False,
                      limit: Annotated[int, Query(ge=1, le=100)] = 25,
                      offset: Annotated[int, Query(ge=0, le=1000000)] = 0):
        """List the shared administrator inbox newest first, with a consistent unread count and sequence watermark.

        Every administrator sees the same read state across all projects. Opening
        this list does not mark anything read. Request titles are immutable event
        snapshots; customer message bodies and identities are never included.
        Deleted resources keep their notifications with resource_available=false.
        Pagination is by offset; newly arriving notifications can move items.
        """
        return service.list(a, session_id(request), unread_only, limit, offset)

    @app.get('/api/v1/admin/notifications/summary', response_model=NotificationSummaryOutput, tags=['Notifications'])
    def summary(request: Request, a: Current):
        """Read the installation-wide unread count and latest committed notification sequence."""
        return service.summary(a, session_id(request))

    @app.put('/api/v1/admin/notifications/{notification_id}/read', response_model=NotificationOutput, tags=['Notifications'])
    def set_read(notification_id: UUID, body: NotificationReadInput, request: Request, a: Current):
        """Set read or unread for every administrator. Repeating the same write is idempotent.

        Explicit concurrent read/unread changes use the last committed write.
        Conversation read positions and push preferences are independent.
        """
        return service.set_read(a, session_id(request), str(notification_id), body.read)

    @app.post('/api/v1/admin/notifications/read-all', response_model=NotificationSummaryOutput, tags=['Notifications'])
    def read_all(body: NotificationReadAllInput, request: Request, a: Current):
        """Mark all notifications at or below the displayed sequence watermark read for every administrator.

        This operation is idempotent. Notifications committed above that watermark
        remain unread, including arrivals after the displayed page was fetched.
        """
        return service.read_all(a, session_id(request), body.through_sequence)
