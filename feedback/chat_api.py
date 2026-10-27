"""Versioned chat HTTP and single-instance WebSocket transport."""
import asyncio
import json
import time
from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID
from fastapi import Depends, Header, Query, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict, Field
from feedback.client_ip import client_ip
from feedback.chat import ChatService
from feedback.business_hours import HoursEdit, WeeklyHours
from feedback.domain import Actor
from feedback.errors import Error
from feedback.security import digest


class ChatInput(BaseModel):
    model_config = ConfigDict(extra='forbid')


class ChatSettingsInput(ChatInput):
    enabled: bool
    origins: list[str] = Field(max_length=20)
    version: int = Field(ge=0)


class EpisodeResource(ChatInput):
    kind: Literal['podcast_episode']
    id: str = Field(min_length=1,max_length=200)


class MessageContext(ChatInput):
    model_config = ConfigDict(extra="forbid", strict=True)
    version: Literal[1]
    resource: EpisodeResource
    position_seconds: float | None = Field(default=None,ge=0,le=86400)


class MessageInput(ChatInput):
    context: MessageContext | None = None
    client_message_id: UUID
    body: str = Field(min_length=1, max_length=4000)
    follow_up_email: str | None = Field(default=None, max_length=254, description="Optional unverified address for manual human follow-up. Stored separately from messages; never sent to AI or notification payloads.")


class ContactClearInput(ChatInput):
    version: int = Field(ge=0)


class ReadInput(ChatInput):
    sequence: int = Field(ge=0, le=2147483647)


class StateInput(ChatInput):
    state: Literal['open', 'closed']
    version: int = Field(ge=1)


class SupportModeInput(ChatInput):
    mode: Literal['ai', 'waiting_human', 'human']
    mode_version: int = Field(ge=1)


class TicketInput(ChatInput):
    conversation_id: UUID


class CustomerSessionInput(ChatInput):
    external_user_id: str | None = Field(default=None, min_length=1, max_length=512)
    client_type: Literal['browser', 'native']
    origin: str | None = Field(default=None, min_length=1, max_length=2048)
    display_name: str | None = Field(default=None, min_length=1, max_length=100)


class CustomerRevokeInput(ChatInput):
    external_user_id: str | None = Field(default=None, min_length=1, max_length=512)


class ConversationInput(ChatInput):
    client_conversation_id: UUID


class CustomerOutput(BaseModel):
    id: str
    kind: Literal['anonymous', 'external', 'native']
    display_name: str
    user_id: str | None = None
    external_user_id: str | None = None


class CustomerSessionOutput(BaseModel):
    token: str
    expires_at: str
    customer: CustomerOutput


class ChatReportSummary(BaseModel):
    request_id: str
    title: str
    kind: Literal['feature', 'bug']
    moderation: Literal['pending', 'accepted', 'rejected'] | None
    status: Literal['under_consideration', 'planned', 'in_progress', 'shipped'] | None
    deleted: bool
    guest: bool


class ConversationOutput(BaseModel):
    project_archived_at: str | None = None
    id: str
    project_id: str
    project_identifier: str
    project_name: str
    state: Literal['open','closed']
    version: int
    created_at: str
    updated_at: str
    last_sequence: int
    support_mode: Literal['ai', 'waiting_human', 'human'] = 'human'
    mode_version: int = 1
    ai_status: Literal['queued', 'running', 'succeeded', 'failed', 'cancelled'] | None = None
    ai_progress: Literal['thinking', 'reading', 'writing', 'checking', 'refining'] | None = Field(default=None, description='Live stage for the current authorized AI attempt. No draft text; null when inactive. Available through polling and WebSocket state frames.')
    ai_trigger_sequence: int | None = Field(default=None, description='Customer message sequence for the latest AI turn; use to discard progress once its reply arrives.')
    unread_count: int
    follow_up_email: str | None = Field(default=None, description="Unverified follow-up contact; conversation owner and native administrators only.")
    contact_version: int = 0
    customer: CustomerOutput
    initial_ip_address: str | None = Field(default=None, description='First connection address; native administrators only.')
    last_customer_ip_address: str | None = Field(default=None, description='Latest new customer message connection address; native administrators only. Admin replies and idempotent retries do not change it.')
    ai_available: bool | None = Field(default=None, description='Current explicit AI readiness; native administrators only.')
    reports: list[ChatReportSummary] | None = Field(default=None, description='Newest 20 submitted feedback summaries for this conversation; native administrators only. No customer credential or guest owner identity.')
    report_count: int | None = Field(default=None, description='Total durable submitted feedback receipts, including deleted requests; native administrators only.')


class CitationOutput(BaseModel):
    id: str
    source_id: str
    revision_id: str
    kind: Literal['markdown', 'website']
    title: str
    excerpt: str
    url: str | None


class ToolSourceOutput(BaseModel):
    id: str
    title: str
    excerpt: str
    url: str | None = None
    observed_at: str
    resource_kind: str | None = None
    resource_id: str | None = None
    revision: str | None = None
    segment_ids: list[str] | None = None
    start_seconds: float | None = None
    end_seconds: float | None = None
    timing_kind: Literal['aligned','approximate','unavailable'] | None = None


class MessageOutput(BaseModel):
    id: str
    conversation_id: str
    sequence: int
    client_message_id: str
    sender: Literal['visitor','admin','ai']
    sender_role: Literal['customer', 'admin', 'ai']
    body: str
    created_at: str
    citations: list[CitationOutput] = Field(default_factory=list)
    tool_sources: list[ToolSourceOutput] = Field(default_factory=list)
    context: MessageContext | None = None
    ip_address: str | None = Field(default=None, description='Sender connection address; native administrators only. Never included in notifications or webhooks.')


class MessagePageOutput(BaseModel):
    items: list[MessageOutput]
    has_more: bool
    next_sequence: int


class ConversationPageOutput(BaseModel):
    items: list[ConversationOutput]
    limit: int
    offset: int
    has_more: bool


class VisitorSessionOutput(BaseModel):
    token: str
    expires_at: str
    conversation: ConversationOutput


class TicketOutput(BaseModel):
    ticket: str
    expires_at: str


class ReadOutput(BaseModel):
    read_sequence: int


class ResponseLabelOutput(BaseModel):
    label: str | None


class ResponseMetricOutput(ResponseLabelOutput):
    median_seconds: float | None
    answered_count: int
    unanswered_count: int
    window_days: int
    minimum_answered: int
    recent_reply: bool


class HoursAvailability(BaseModel):
    enabled: bool
    source: Literal['account','project']
    timezone: str
    days: list[list[dict[str,str]]]
    is_open: bool | None
    next_open_at: str | None
    collect_email: bool
    observed_at: str


class HoursSettingsOutput(BaseModel):
    version: int
    mode: Literal['inherit','override','off']
    schedule: WeeklyHours
    effective: WeeklyHours
    source: Literal['account','project']


class ChatConfigOutput(BaseModel):
    project: str
    name: str
    enabled: bool
    ai_available: bool = False
    response_time: ResponseLabelOutput
    business_hours: HoursAvailability


class ChatSettingsOutput(BaseModel):
    project_id: str
    project_identifier: str
    project_name: str
    enabled: bool
    origins: list[str]
    version: int
    response_time: ResponseMetricOutput | None = None


def install_chat(app, storage, vault, config, actor):
    service = ChatService(storage, config.base_url)
    app.state.chat_service = service
    Current = Annotated[Actor, Depends(actor)]

    def principal(request: Request, a: Current, token: Annotated[str | None, Header(alias='X-Chat-Token', max_length=128)] = None):
        origin = request.headers.get('origin') or (config.base_url if request.headers.get('sec-fetch-site') == 'same-origin' else None)
        if token:
            if request.headers.get('authorization') or request.cookies.get('feedback_session'):
                raise Error(400, 'ambiguous_auth', 'Use one authentication transport')
            return {**service.visitor(token, origin), 'ip_address': client_ip(request)}
        if not a.user_id or a.role != 'admin' or a.key_id:
            raise Error(403, 'forbidden', 'Administrator or visitor chat session required')
        if origin and origin != config.base_url:
            raise Error(403, 'origin_denied', 'Use the configured administrator origin')
        return {'kind':'admin', 'user_id':a.user_id, 'session_id':request.state.session['session_id'], 'origin':origin, 'ip_address': client_ip(request)}

    # Extensions use the same credential, origin, and connection metadata boundary.
    app.state.chat_principal = principal

    def admin(p: Annotated[dict, Depends(principal)]):
        if p['kind'] != 'admin':
            raise Error(403, 'forbidden', 'Administrator access required')
        return p

    Principal = Annotated[dict, Depends(principal)]
    Admin = Annotated[dict, Depends(admin)]

    @app.get('/api/v1/chat/projects/{project}/config', tags=['Chat'], response_model=ChatConfigOutput, description='Public widget configuration. Requires an exact enabled website Origin. No credentials or identities returned.', response_model_exclude_unset=True)
    def public_config(project: str, request: Request):
        return service.config(project, request.headers.get('origin') or (config.base_url if request.headers.get('sec-fetch-site') == 'same-origin' else None))

    @app.post('/api/v1/chat/projects/{project}/sessions', status_code=201, tags=['Chat'], response_model=VisitorSessionOutput, description='Create a new 30-day visitor session and conversation. X-Chat-Token authorizes subsequent operations. Call only on deliberate chat use.', response_model_exclude_unset=True)
    def create_session(project: str, request: Request):
        if request.headers.get('authorization') or request.cookies.get('feedback_session'):
            raise Error(400, 'ambiguous_auth', 'Create visitor sessions without account credentials')
        return service.create_session(project, request.headers.get('origin'), client_ip(request))

    def issuer_session(request, a):
        if request.headers.get('x-chat-token'):
            raise Error(400, 'ambiguous_auth', 'Use the account or project credential to establish chat access')
        if a.key_id and request.headers.get('origin'):
            raise Error(403, 'forbidden', 'Project credentials must be used by trusted backends')
        return getattr(request.state, 'session', {}).get('session_id')

    @app.post('/api/v1/chat/projects/{project}/customer-sessions', status_code=201, tags=['Chat'], response_model=CustomerSessionOutput, description='Exchange a trusted participate project credential plus a verified external_user_id, or a native account session, for a one-hour customer credential. Browser credentials bind an allowed origin; native credentials require no Origin. Repeating the exchange preserves customer history and read positions. Revoking the issuing key or native session invalidates its derived credentials.', response_model_exclude_unset=True)
    def customer_session(project: str, data: CustomerSessionInput, request: Request, a: Current):
        parent = issuer_session(request, a)
        return service.create_customer_session(project, a, data.external_user_id, data.client_type, data.origin, parent, data.display_name)

    @app.post('/api/v1/chat/projects/{project}/customer-sessions/revoke', status_code=204, tags=['Chat'], description='Revoke all chat credentials for this verified customer in the project without deleting history. Requires the same native account or a project credential with participate and external_user_id.')
    def revoke_customer_sessions(project: str, data: CustomerRevokeInput, request: Request, a: Current):
        parent = issuer_session(request, a)
        service.revoke_customer_sessions(project, a, data.external_user_id, parent)
        return Response(status_code=204)

    @app.get('/api/v1/chat/conversations', tags=['Chat'], response_model=ConversationPageOutput, description='List only the acting customer’s conversations in their credential’s project, including empty conversations. Newest activity first, then stable ID; results can move when messages arrive.', response_model_exclude_unset=True)
    def customer_conversations(p: Principal, state: Literal['open','closed'] | None = None, limit: Annotated[int, Query(ge=1,le=100)] = 25, offset: Annotated[int, Query(ge=0,le=1000000)] = 0):
        items = service.conversations(p, state, limit + 1, offset)
        return {'items':items[:limit], 'limit':limit, 'offset':offset, 'has_more':len(items)>limit}

    @app.post('/api/v1/chat/conversations', status_code=201, tags=['Chat'], response_model=ConversationOutput, description='Start a conversation for an authenticated customer. Reuse client_conversation_id on retries to obtain the same conversation; anonymous widget sessions continue to create their conversation at session issuance.', response_model_exclude_unset=True)
    def create_conversation(data: ConversationInput, p: Principal):
        return service.create_conversation(p, str(data.client_conversation_id))

    @app.delete('/api/v1/chat/session', status_code=204, tags=['Chat'], description='Revoke the current cc_ customer credential and its sockets/tickets. Other device sessions and durable history remain intact.')
    def revoke_customer_session(p: Principal):
        service.revoke_customer_session(p)
        return Response(status_code=204)

    @app.get('/api/v1/chat/conversations/{conversation_id}', tags=['Chat'], response_model=ConversationOutput, response_model_exclude_unset=True)
    def conversation(conversation_id: UUID, p: Principal):
        return storage.chat_conversation(str(conversation_id), p)

    @app.put('/api/v1/chat/conversations/{conversation_id}/support-mode', tags=['Chat'], response_model=ConversationOutput, response_model_exclude_unset=True, description='Explicit versioned AI or human-support choice. Customers can request AI or wait for a human; only native administrators can take over. Changing mode fences unfinished AI replies.')
    def support_mode(conversation_id: UUID, data: SupportModeInput, p: Principal):
        return service.support_mode(str(conversation_id), p, data.mode, data.mode_version)

    @app.get('/api/v1/chat/conversations/{conversation_id}/messages', tags=['Chat'], response_model=MessagePageOutput, response_model_exclude_unset=True)
    def messages(conversation_id: UUID, p: Principal, after: Annotated[int, Query(ge=0, le=2147483647)] = 0, limit: Annotated[int, Query(ge=1, le=100)] = 25):
        rows = storage.chat_messages(str(conversation_id), p, after, limit + 1)
        items = rows[:limit]
        return {'items': items, 'has_more': len(rows) > limit, 'next_sequence': items[-1]['sequence'] if items else after}

    @app.post('/api/v1/chat/conversations/{conversation_id}/messages', status_code=201, tags=['Chat'], response_model=MessageOutput, description='Idempotent per durable sender and client_message_id, including after customer credential renewal. Returns only after the message, immutable body-free event, subscribed webhook job and eligible push jobs commit. Conflicting ID reuse returns 409.', response_model_exclude_unset=True)
    def send(conversation_id: UUID, data: MessageInput, p: Principal):
        message = service.message(str(conversation_id), p, str(data.client_message_id), data.body, data.follow_up_email, data.context.model_dump(exclude_none=True) if data.context else None)
        app.state.ai_chat_worker.wake()
        return message

    @app.post('/api/v1/chat/conversations/{conversation_id}/read', tags=['Chat'], response_model=ReadOutput, response_model_exclude_unset=True)
    def read(conversation_id: UUID, data: ReadInput, p: Principal):
        return storage.chat_read(str(conversation_id), p, data.sequence)

    @app.delete('/api/v1/chat/conversations/{conversation_id}/session', status_code=204, tags=['Chat'])
    def revoke(conversation_id: UUID, p: Principal):
        if p['kind'] == 'customer':
            raise Error(403, 'forbidden', 'Use DELETE /api/v1/chat/session to end this customer session')
        storage.chat_revoke(str(conversation_id), p)
        return Response(status_code=204)

    @app.post('/api/v1/chat/tickets', tags=['Chat'], response_model=TicketOutput, description='Single-use connection ticket, valid for 60 seconds and bound to the current session and Origin. Send as the first WebSocket frame, never in its URL.', response_model_exclude_unset=True)
    def ticket(data: TicketInput, p: Principal):
        return service.ticket(str(data.conversation_id), p)

    @app.get('/api/v1/admin/chat/business-hours', tags=['Chat administration'], response_model=HoursSettingsOutput)
    def account_hours(p: Admin):
        return storage.chat_business_hours(None,p)

    @app.put('/api/v1/admin/chat/business-hours', tags=['Chat administration'], response_model=HoursSettingsOutput)
    def save_account_hours(data: HoursEdit, p: Admin):
        return storage.chat_business_hours_save(None,p,data.model_dump())

    @app.get('/api/v1/admin/chat/projects/{project}/business-hours', tags=['Chat administration'], response_model=HoursSettingsOutput)
    def project_hours(project: str, p: Admin):
        return storage.chat_business_hours(project,p)

    @app.put('/api/v1/admin/chat/projects/{project}/business-hours', tags=['Chat administration'], response_model=HoursSettingsOutput)
    def save_project_hours(project: str, data: HoursEdit, p: Admin):
        return storage.chat_business_hours_save(project,p,data.model_dump())

    @app.post('/api/v1/chat/conversations/{conversation_id}/contact/remove', tags=['Chat'], response_model=ConversationOutput, response_model_exclude_unset=True, description='Owner or native administrator removes the separate follow-up email using its current version. Does not remove messages or historical backups.')
    def clear_contact(conversation_id: UUID, data: ContactClearInput, p: Principal):
        return storage.chat_contact_clear(str(conversation_id),p,data.version)

    @app.get('/api/v1/admin/chat/projects/{project}', tags=['Chat administration'], response_model=ChatSettingsOutput, response_model_exclude_unset=True)
    def settings(project: str, p: Admin):
        result = service.settings(project)
        result['response_time'] = service.response_time(result['project_id'])
        return result

    @app.put('/api/v1/admin/chat/projects/{project}', tags=['Chat administration'], response_model=ChatSettingsOutput, response_model_exclude_unset=True)
    def configure(project: str, data: ChatSettingsInput, p: Admin):
        return service.configure(project, p, data.enabled, data.origins, data.version)

    @app.get('/api/v1/admin/chat/conversations', tags=['Chat administration'], response_model=ConversationPageOutput, response_model_exclude_unset=True)
    def inbox(p: Admin, state: Literal['open','closed'] | None = None, project: str | None = None, limit: Annotated[int, Query(ge=1,le=100)] = 25, offset: Annotated[int, Query(ge=0,le=1000000)] = 0):
        items = storage.chat_conversations(p, state, project, limit+1, offset)
        return {'items':items[:limit], 'limit':limit, 'offset':offset, 'has_more':len(items)>limit}

    @app.get('/api/v1/admin/chat/conversations/{conversation_id}', tags=['Chat administration'], response_model=ConversationOutput, response_model_exclude_unset=True)
    def admin_conversation(conversation_id: UUID, p: Admin):
        return storage.chat_conversation(str(conversation_id), p)

    @app.patch('/api/v1/admin/chat/conversations/{conversation_id}', tags=['Chat administration'], response_model=ConversationOutput, response_model_exclude_unset=True)
    def state(conversation_id: UUID, data: StateInput, p: Admin):
        return storage.chat_change_state(str(conversation_id), p, data.state, data.version)

    @app.get('/widget.js', include_in_schema=False)
    def widget():
        return FileResponse(Path(__file__).with_name('web') / 'widget.js', media_type='application/javascript', headers={'Access-Control-Allow-Origin':'*', 'Cache-Control':'no-cache'})

    @app.get('/admin/chat', include_in_schema=False)
    def admin_page(request: Request):
        token = request.cookies.get('feedback_session')
        session = storage.session(digest(token)) if token else None
        if not session or session.get('transport') != 'cookie':
            from urllib.parse import urlencode
            target = request.url.path + (('?' + request.url.query) if request.url.query else '')
            return RedirectResponse('/login?' + urlencode({'return': target}), status_code=303)
        return FileResponse(Path(__file__).with_name('web') / 'chat-admin.html', headers={'Cache-Control':'no-cache'})

    @app.get('/chat-admin.js', include_in_schema=False)
    def admin_script():
        return FileResponse(Path(__file__).with_name('web') / 'chat-admin.js', media_type='application/javascript', headers={'Cache-Control':'no-cache'})

    # The durable log is the source of truth. Polling from its cursor avoids lost
    # fan-out races and naturally restores delivery after reconnect/restart.
    connections = {}
    pending_connections = {}

    @app.websocket('/api/v1/chat/ws')
    async def socket(ws: WebSocket):
        peer = (ws.scope.get('client') or ('unknown',))[0]
        if len(connections) + len(pending_connections) >= 128 or sum(1 for x in pending_connections.values() if x == peer) >= 8:
            await ws.close(code=1013)
            return
        origin = ws.headers.get('origin')
        if ws.query_params or (origin and origin != config.base_url and not storage.chat_origin_allowed(origin)):
            await ws.close(code=4403)
            return
        pending_connections[ws] = peer
        await ws.accept()
        task = None
        try:
            text = await asyncio.wait_for(ws.receive_text(), 5)
            if len(text.encode()) > 32768:
                raise Error(400,'invalid_input','Frame exceeds 32 KiB')
            hello = json.loads(text)
            token = hello.get('ticket','')
            after = hello.get('after',0)
            if not isinstance(token,str) or not token.startswith('ct_') or len(token)>128 or not isinstance(after,int) or not 0 <= after <= 2147483647:
                raise Error(401,'unauthorized','A connection ticket and valid cursor are required')
            cid, p = await asyncio.to_thread(storage.chat_consume_ticket,digest(token),ws.headers.get('origin'))
            p = {**p, 'ip_address': client_ip(ws)}
            if sum(1 for x in connections.values() if x['session_id']==p['session_id'])>=8:
                raise Error(429,'rate_limited','Too many connections')
            connections[ws] = {**p,'conversation_id':cid}
            pending_connections.pop(ws, None)
            async def emit(data):
                await asyncio.wait_for(ws.send_json(data),5)
            await emit({'type':'ready','conversation_id':cid})
            last_ping, last_typing, frame_start, frames = time.monotonic(), 0, time.monotonic(), 0
            state_revision = None
            task = asyncio.create_task(ws.receive_text())
            while True:
                conversation = await asyncio.to_thread(storage.chat_conversation,cid,p)
                current_revision = (conversation['version'], conversation['mode_version'], conversation['last_sequence'], conversation['ai_status'], conversation['ai_progress'], conversation['ai_trigger_sequence'], conversation['contact_version'])
                if state_revision != current_revision:
                    await emit({'type':'state','conversation':conversation})
                    state_revision = current_revision
                rows = await asyncio.to_thread(storage.chat_messages,cid,p,after,100)
                for message in rows:
                    await emit({'type':'message','message':message})
                    after=message['sequence']
                done, _ = await asyncio.wait([task],timeout=0.25 if len(rows)==100 else 1)
                if task in done:
                    raw=task.result()
                    if len(raw.encode())>32768:
                        raise Error(400,'invalid_input','Frame exceeds 32 KiB')
                    if time.monotonic()-frame_start>=60:
                        frames,frame_start=0,time.monotonic()
                    frames+=1
                    if frames>120:
                        raise Error(429,'rate_limited','Too many frames')
                    frame=json.loads(raw)
                    # Recheck membership/revocation before every client operation.
                    await asyncio.to_thread(storage.chat_conversation,cid,p)
                    if frame.get('type')=='message':
                        data=MessageInput.model_validate(frame.get('message',{}))
                        message=await asyncio.to_thread(service.message,cid,p,str(data.client_message_id),data.body,None,data.context.model_dump(exclude_none=True) if data.context else None)
                        app.state.ai_chat_worker.wake()
                        await emit({'type':'ack','message':message})
                    elif frame.get('type')=='read':
                        data=ReadInput.model_validate({'sequence':frame.get('sequence')})
                        await asyncio.to_thread(storage.chat_read,cid,p,data.sequence)
                    elif frame.get('type')=='typing' and time.monotonic()-last_typing>=2:
                        last_typing=time.monotonic()
                        for peer, other in list(connections.items()):
                            if peer is not ws and other['conversation_id']==cid:
                                try:
                                    await asyncio.to_thread(storage.chat_conversation,cid,other)
                                    await asyncio.wait_for(peer.send_json({'type':'typing','sender':'admin' if p['kind']=='admin' else 'visitor','sender_role':'admin' if p['kind']=='admin' else 'customer'}),1)
                                except (Error,RuntimeError,asyncio.TimeoutError):
                                    pass
                    elif frame.get('type') not in ('typing','pong','ping'):
                        raise Error(400,'invalid_input','Unknown frame type')
                    task=asyncio.create_task(ws.receive_text())
                if time.monotonic()-last_ping>=20:
                    await emit({'type':'ping'})
                    last_ping=time.monotonic()
        except Error as error:
            try:
                await ws.send_json({'type':'error','error':{'code':error.code,'message':error.message}})
                await ws.close(code=4401 if error.status in (401,403,404) else 4400)
            except (RuntimeError,WebSocketDisconnect):
                pass
        except (WebSocketDisconnect,asyncio.TimeoutError,ValueError,TypeError,AttributeError,KeyError,RuntimeError):
            try:
                await ws.close(code=4400)
            except (RuntimeError,WebSocketDisconnect):
                pass
        finally:
            connections.pop(ws,None)
            pending_connections.pop(ws,None)
            if task:
                task.cancel()
                await asyncio.gather(task,return_exceptions=True)
