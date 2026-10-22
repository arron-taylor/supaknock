"""Public API output schemas also prevent accidental secret fields from escaping."""
from typing import Generic, TypeVar, Literal
from pydantic import BaseModel, Field

T = TypeVar('T')


class PageOutput(BaseModel, Generic[T]):
    items: list[T]
    limit: int
    offset: int
    has_more: bool


class UserOutput(BaseModel):
    id: str
    display_name: str | None = None
    role: Literal['participant', 'admin']


class SessionOutput(BaseModel):
    user: UserOutput
    csrf_token: str


class NativeSessionOutput(BaseModel):
    user: UserOutput
    token: str
    expires_at: str
    installation_id: str


class SetupOutput(BaseModel):
    available: bool
    registration_enabled: bool


class ProjectOutput(BaseModel):
    id: str
    identifier: str
    name: str
    description: str
    created_at: str
    updated_at: str
    version: int
    archived_at: str | None = None


class BoardOutput(ProjectOutput):
    visibility: Literal['public', 'private'] = 'public'
    project_id: str
    project_archived_at: str | None = None


class RequestOutput(BaseModel):
    id: str
    board_id: str
    project_id: str
    project_identifier: str
    project_name: str
    board_name: str
    board_identifier: str
    title: str
    description: str
    status: Literal['under_consideration', 'planned', 'in_progress', 'shipped'] | None
    created_at: str
    updated_at: str
    version: int
    vote_count: int
    voted: bool
    creator_name: str | None
    owned: bool
    can_edit: bool
    archived: bool = False
    moderation: Literal['pending', 'accepted', 'rejected'] | None = None
    rejection_reason: str | None = None
    accepted_at: str | None = None
    submitted_ip_address: str | None = Field(default=None, description='Connection address recorded at submission. Native administrators only; omitted for all other callers. Older records may be null.')


class VoteOutput(BaseModel):
    vote_count: int
    voted: bool


class IdentityOutput(BaseModel):
    user_id: str
    external_user_id: str | None
    creator: bool


class KeyOutput(BaseModel):
    id: str
    name: str
    prefix: str
    permissions: list[Literal['read', 'participate', 'audience:read', 'manage', 'forms:submit']]
    created_at: str
    revoked_at: str | None


class NewKeyOutput(BaseModel):
    id: str
    secret: str
    prefix: str
    permissions: list[Literal['read', 'participate', 'audience:read', 'manage', 'forms:submit']]


class WebhookOutput(BaseModel):
    url: str | None
    enabled: bool
    events: list[str]
    has_secret: bool
    version: int
    signing_secret: str | None = None


class EventOutput(BaseModel):
    id: str
    type: str
    occurred_at: str
    schema_version: int | None = None
    project: dict | None = None
    board: dict | None = None
    request_id: str | None = None
    url: str | None = None
    request: dict | None = None
    previous: dict | None = None
    actor: dict | None = None
    creator: dict | None = None
    status: str | None = None
    previous_status: str | None = None
    ip_address: str | None = Field(default=None, description='Connection address for this action. Native administrators only; never included in webhook payloads.')


class AccountActivityOutput(BaseModel):
    id: str
    user_id: str
    event_type: Literal['signup', 'login', 'account_created', 'setup']
    transport: Literal['cookie', 'native'] | None
    ip_address: str | None
    occurred_at: str
    display_name: str | None
    email: str
    role: Literal['participant', 'admin']


class DeliveryOutput(BaseModel):
    id: str
    event_id: str
    destination: str
    state: Literal['queued', 'delivering', 'succeeded', 'failed']
    attempt_count: int
    next_attempt_at: str
    last_attempt_at: str | None
    last_http_status: int | None
    error: str | None
    delivered_at: str | None
    type: str | None = None
    payload: dict | None = None


class DashboardOutput(BaseModel):
    pending_requests: int
    failed_deliveries: int


class TestOutput(BaseModel):
    event_id: str


class RetryOutput(BaseModel):
    queued: bool


class DeviceOutput(BaseModel):
    id: str
    name: str
    environment: Literal['sandbox','production']
    enabled: bool
    request_notifications: bool
    chat_notifications: bool
    form_notifications: bool
    generation: int
    updated_at: str


class DevicesOutput(BaseModel):
    items: list[DeviceOutput]


class PushOutput(BaseModel):
    installation_id: str
    enabled: bool
    connected: bool
    relay_url: str
    last_error: str | None


class PushDeliveryOutput(BaseModel):
    id: str
    event_id: str
    kind: Literal['request.created','chat.message','form.submission_created']
    resource_id: str
    device_id: str
    state: Literal['queued','sending','awaiting','succeeded','failed','cancelled','expired']
    attempt_count: int
    retry_count: int
    error: str | None
    created_at: str
    expires_at: str
