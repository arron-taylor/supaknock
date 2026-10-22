from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Literal
from dataclasses import replace
import hmac
import json
import re
import secrets
import time
import threading
from uuid import UUID
from fastapi import FastAPI, Depends, Query, Request, Response, Security
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, FileResponse, RedirectResponse
from fastapi.security import APIKeyCookie, APIKeyHeader
from starlette.exceptions import HTTPException as RoutingError
from pydantic import BaseModel, ConfigDict, Field, field_validator
from feedback.outputs import (PageOutput, UserOutput, SessionOutput, SetupOutput, ProjectOutput, BoardOutput, RequestOutput, VoteOutput, IdentityOutput, KeyOutput, NewKeyOutput, WebhookOutput, EventOutput, DeliveryOutput, DashboardOutput, TestOutput, RetryOutput)
from feedback.client_ip import client_ip
from feedback.config import Config
from feedback.domain import Actor, EVENTS, PERMISSIONS
from feedback.domain import utc_timestamp
from feedback.outputs import AccountActivityOutput, NativeSessionOutput, DeviceOutput, DevicesOutput, PushOutput, PushDeliveryOutput
from feedback.errors import Error
from feedback.security import digest, hasher, verify_password, Vault
from feedback.services import FeedbackService, public_project, request_view, delivery_view, require_admin, require_manage
from feedback.storage import open_storage
from feedback.webhooks import WebhookWorker, destination


class Input(BaseModel):
    model_config = ConfigDict(extra='forbid')


class AccountInput(Input):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=12, max_length=128)
    display_name: str | None = Field(default=None, max_length=100)

    @field_validator('email')
    @classmethod
    def email_format(cls, value):
        value = value.strip().casefold()
        if value.count('@') != 1 or any(c.isspace() for c in value) or not all(value.split('@')):
            raise ValueError('Use a valid email address')
        return value


class SetupInput(AccountInput):
    setup_token: str = Field(min_length=24, max_length=256)


class LoginInput(Input):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=1, max_length=128)


class PasswordInput(Input):
    current_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=12, max_length=128)


class ResourceInput(Input):
    identifier: str = Field(pattern=r'^[a-z][a-z0-9-]{0,59}$')
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(default='', max_length=20000)

    @field_validator('name')
    @classmethod
    def nonempty(cls, value):
        if not value.strip():
            raise ValueError('Name cannot be blank')
        return value


class ResourceEdit(Input):
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(default='', max_length=20000)
    version: int = Field(ge=1)


class BoardInput(ResourceInput):
    visibility: Literal['public', 'private'] = 'public'


class BoardEdit(ResourceEdit):
    # Omission preserves visibility for older clients; explicit null is invalid.
    visibility: Literal['public', 'private'] = 'public'


class ArchiveInput(Input):
    archived: bool = Field(strict=True)
    version: int = Field(ge=1)


class Content(Input):
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(default='', max_length=20000)

    @field_validator('title')
    @classmethod
    def nonempty(cls, value):
        if not value.strip():
            raise ValueError('Title cannot be blank')
        return value


class Submission(Content):
    external_user_id: str | None = Field(default=None, min_length=1, max_length=512)


class CreatorEdit(Submission):
    version: int = Field(ge=1)


class AdminEdit(Content):
    version: int = Field(ge=1)


class VersionInput(Input):
    version: int = Field(ge=1)


class Rejection(VersionInput):
    reason: str | None = Field(default=None, max_length=2000)


class Lifecycle(VersionInput):
    status: Literal['under_consideration', 'planned', 'in_progress', 'shipped']


class Vote(Input):
    present: bool
    external_user_id: str | None = Field(default=None, min_length=1, max_length=512)


class KeyInput(Input):
    name: str = Field(min_length=1, max_length=100)
    permissions: list[Literal['read', 'participate', 'audience:read', 'manage', 'forms:submit']] = Field(min_length=1, max_length=5)


class DeviceInput(Input):
    token: str = Field(pattern=r'^[0-9a-f]{64,200}$')
    environment: Literal['sandbox','production']
    name: str = Field(default='iPhone',min_length=1,max_length=100)
    enabled: bool = True
    request_notifications: bool = True
    chat_notifications: bool = True
    form_notifications: bool = False


class PushInput(Input):
    enabled: bool
    relay_url: str = Field(default='https://relay.supaknock.com', max_length=2048, description='HTTPS origin of the optional push relay, without /api/v1. SupaKnock provides https://relay.supaknock.com. Choose the origin before enrollment; enrolled installations cannot switch relays through this setting.')


class WebhookInput(VersionInput):
    url: str | None = Field(default=None, max_length=2048)
    enabled: bool
    events: list[Literal['request.created', 'request.updated', 'request.resubmitted', 'request.accepted', 'request.rejected', 'request.status_changed', 'request.deleted', 'chat.message_created']] = Field(max_length=8)
    rotate_secret: bool = False


class Pagination:
    def __init__(self, limit: Annotated[int, Query(ge=1, le=100)] = 25, offset: Annotated[int, Query(ge=0, le=1000000)] = 0):
        self.limit, self.offset = limit, offset

    def page(self, items):
        return {'items': items[:self.limit], 'limit': self.limit, 'offset': self.offset, 'has_more': len(items) > self.limit}


Page = Annotated[Pagination, Depends()]
External = Annotated[str | None, Query(min_length=1, max_length=512, description='Opaque project-scoped participant ID. Requires a participate credential; never a login credential.')]
LifecycleFilter = Literal['under_consideration', 'planned', 'in_progress', 'shipped']
api_secret = APIKeyHeader(name='Authorization', auto_error=False, description='Bearer fb_… project credential, or ns_… native administrator session')
cookie_secret = APIKeyCookie(name='feedback_session', auto_error=False)


class RequestGuard:
    """Bound request bodies before routing; single-instance, bounded in-memory rate limits."""
    def __init__(self, app, config, storage=None):
        self.app, self.config, self.storage = app, config, storage
        self.buckets, self.lock, self.last_cleanup = {}, threading.Lock(), 0

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        headers = dict(scope['headers'])
        path, method = scope['path'], scope['method']
        error = None
        chat = path.startswith('/api/v1/chat/')
        chat_origin = headers.get(b'origin', b'').decode()
        chat_allowed = bool(chat and chat_origin and self.storage and self.storage.chat_origin_allowed(chat_origin))
        form_embed = re.fullmatch(r'/api/v1/embed/forms/([0-9a-fA-F-]{36})(/submissions)?', path)
        form_embed_path = path.startswith('/api/v1/embed/forms/')
        form_allowed = bool(form_embed and chat_origin and self.storage and self.storage.forms_embed_origin_allowed(form_embed[1].lower(),chat_origin))
        form_cors = {'Access-Control-Allow-Origin':chat_origin,'Vary':'Origin','Cache-Control':'no-store'} if form_allowed else {}
        # Cross-origin public reads are credential-free. Never allow browser
        # credentials or Authorization headers through this CORS transport.
        if method == 'OPTIONS' and path.startswith('/api/v1/') and b'origin' in headers and b'access-control-request-method' in headers:
            requested_method = headers[b'access-control-request-method'].decode().upper()
            requested_headers = {x.strip().lower() for x in headers.get(b'access-control-request-headers', b'').decode().split(',') if x.strip()}
            if form_embed_path:
                allowed_method = 'POST' if form_embed and form_embed[2] else 'GET'
                if not form_allowed or requested_method != allowed_method or requested_headers - {'accept','content-type'}:
                    return await JSONResponse({'error':{'code':'cors_denied','message':'This website is not enabled for this form'}},status_code=403)(scope,receive,send)
                return await Response(status_code=204,headers={**form_cors,'Access-Control-Allow-Methods':allowed_method+', OPTIONS','Access-Control-Allow-Headers':'Accept, Content-Type','Access-Control-Max-Age':'600'})(scope,receive,send)
            if chat:
                can_put = re.fullmatch(r'/api/v1/chat/conversations/[^/]+/(?:support-mode|(?:proposals|reports)/[^/]+)', path)
                methods = ('GET', 'POST', 'DELETE', 'PUT') if can_put else ('GET', 'POST', 'DELETE')
                if not chat_allowed or requested_method not in methods or requested_headers - {'accept','content-type','x-chat-token'}:
                    return await JSONResponse({'error':{'code':'cors_denied','message':'This website is not enabled for chat'}},status_code=403)(scope,receive,send)
                return await Response(status_code=204,headers={'Access-Control-Allow-Origin':chat_origin,'Access-Control-Allow-Methods':', '.join((*methods, 'OPTIONS')),'Access-Control-Allow-Headers':'Accept, Content-Type, X-Chat-Token','Access-Control-Max-Age':'600','Vary':'Origin'})(scope,receive,send)
            if requested_method not in ('GET', 'HEAD') or requested_headers - {'accept', 'content-type'}:
                return await JSONResponse({'error': {'code': 'cors_denied', 'message': 'Only credential-free public reads support cross-origin browser access'}}, status_code=400)(scope, receive, send)
            return await Response(status_code=204, headers={
                'Access-Control-Allow-Origin': '*',
                'Access-Control-Allow-Methods': 'GET, HEAD, OPTIONS',
                'Access-Control-Allow-Headers': 'Accept, Content-Type',
                'Access-Control-Max-Age': '600', 'Cache-Control': 'no-store'
            })(scope, receive, send)
        if method not in ('GET', 'HEAD', 'OPTIONS') and path.startswith('/api/'):
            origin = headers.get(b'origin')
            if origin and origin.decode() != self.config.base_url and not chat and not form_allowed:
                error = (403, 'csrf_failed', 'Use the configured site origin')
            is_auth = path.startswith('/api/v1/auth/') or path == '/api/v1/setup'
            minute = int(time.time() / 60)
            client = (scope.get('client') or ('unknown',))[0]
            app_read = method == 'POST' and bool(re.fullmatch(r'/api/v1/projects/[^/]+/app-data/(?:summary|users|profile|activity|related)/query', path))
            key = (client, 'auth' if is_auth else ('app_read' if app_read else 'mutation'), minute)
            with self.lock:
                if minute != self.last_cleanup:
                    self.buckets = {k:v for k,v in self.buckets.items() if k[2] >= minute - 1}
                    self.last_cleanup = minute
                cap = self.config.auth_rate_limit if is_auth else (480 if app_read else self.config.rate_limit)
                count = self.buckets.get(key, 0)
                if count >= cap or (key not in self.buckets and len(self.buckets) >= 10000):
                    error = (429, 'rate_limited', 'Too many attempts; try again in one minute')
                else:
                    self.buckets[key] = count + 1
        if error:
            return await JSONResponse({'error': {'code': error[1], 'message': error[2]}}, status_code=error[0], headers={**form_cors,**({'Retry-After': '60'} if error[0] == 429 else {})})(scope, receive, send)
        # Long originals, personality and report drafts have separate decoded
        # text limits. Permit JSON escaping overhead only on these write routes.
        markdown_upload = bool((method == 'POST' and re.fullmatch(r'/api/v1/projects/[^/]+/ai/knowledge/markdown', path)) or
            (method == 'PUT' and re.fullmatch(r'/api/v1/projects/[^/]+/ai/knowledge/sources/[^/]+/markdown', path)))
        personality_upload = method == 'PUT' and bool(re.fullmatch(r'/api/v1/projects/[^/]+/ai(?:/personality)?', path))
        report_edit = method == 'PUT' and bool(re.fullmatch(r'/api/v1/chat/conversations/[^/]+/(?:proposals|reports)/[^/]+', path))
        body_limit = 400 * 1024 if markdown_upload or personality_upload or report_edit else (32768 if chat else 98304)
        # Also covers chunked bodies and dishonest Content-Length.
        body = bytearray()
        while True:
            message = await receive()
            if message['type'] == 'http.disconnect':
                return
            body.extend(message.get('body', b''))
            if len(body) > body_limit:
                return await JSONResponse({'error': {'code': 'body_too_large', 'message': f'Maximum request body is {body_limit // 1024} KiB'}}, status_code=413,headers=form_cors)(scope, receive, send)
            if not message.get('more_body'):
                break
        sent = False
        async def buffered_receive():
            nonlocal sent
            if not sent:
                sent = True
                return {'type': 'http.request', 'body': bytes(body), 'more_body': False}
            return await receive()
        async def secure_send(message):
            if message['type'] == 'http.response.start':
                message['headers'] += [(b'x-content-type-options', b'nosniff'), (b'referrer-policy', b'no-referrer'),
                    (b'content-security-policy', b"default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")]
                if path.startswith('/api/'):
                    message['headers'].append((b'cache-control', b'no-store'))
                    if form_embed_path:
                        message['headers'].append((b'vary',b'Origin'))
                        if form_allowed: message['headers'].append((b'access-control-allow-origin',chat_origin.encode()))
                    elif chat_allowed:
                        message['headers'] += [(b'access-control-allow-origin',chat_origin.encode()),(b'vary',b'Origin')]
                    elif not chat and method in ('GET', 'HEAD') and b'authorization' not in headers and b'cookie' not in headers:
                        message['headers'].append((b'access-control-allow-origin', b'*'))
            await send(message)
        await self.app(scope, buffered_receive, secure_send)


def create_app(config=None):
    config = config or Config.from_env()
    storage = open_storage(config)
    service, vault = FeedbackService(storage), Vault(config.secret)
    try:
        for protected in storage.signing_secrets():
            vault.reveal(protected)
    except Exception as exc:
        raise RuntimeError('Application key cannot decrypt stored installation secrets; restore the original key') from None
    worker = WebhookWorker(storage, vault, config.allow_internal_webhooks)
    from feedback.push import PushService, PushWorker, delivery_view as push_delivery_view
    push = PushService(storage,vault,config.allow_internal_relay)
    push_worker = PushWorker(storage,vault,config.allow_internal_relay)
    from feedback.ai_installation import EmbeddingInstallation
    ai_runtime = EmbeddingInstallation(storage, vault, config)
    from feedback.ai_retrieval import IngestionWorker, RetrievalService
    from feedback.ai_embeddings import EmbeddingService
    ai_embeddings = EmbeddingService(storage, vault, ai_runtime, allow_internal=config.allow_internal_ai_endpoints)
    ai_ingestion = IngestionWorker(storage, ai_embeddings)
    ai_retrieval = RetrievalService(storage, ai_embeddings)
    from feedback.ai_generation import AIGeneration
    from feedback.ai_tools import AIToolDispatcher
    from feedback.ai_mcp import MCPTools
    ai_mcp_tools = MCPTools(storage,vault)
    ai_tools = AIToolDispatcher(storage, ai_retrieval, ai_mcp_tools)
    from functools import partial
    from feedback.ai_providers import provider
    ai_generation = AIGeneration(storage, vault, ai_retrieval, tool_dispatcher=ai_tools, provider_factory=partial(provider, allow_internal=config.allow_internal_ai_endpoints))
    from feedback.ai_chat import AIChatWorker
    ai_chat_worker = AIChatWorker(storage, ai_generation)

    @asynccontextmanager
    async def lifespan(app):
        try:
            if config.worker:
                worker.start()
                push_worker.start()
                ai_runtime.start()
                app.state.ai_crawl_worker.start()
                ai_ingestion.start()
                app.state.ai_authoring_worker.start()
                ai_chat_worker.start()
            yield
        finally:
            ai_chat_worker.stop()
            app.state.ai_authoring_worker.stop()
            app.state.ai_crawl_worker.stop()
            ai_ingestion.stop()
            ai_runtime.stop()
            ai_embeddings.stop()
            worker.stop()
            push_worker.stop()

    app = FastAPI(title='SupaKnock API', version='1.0.0', description='Public reads are anonymous. Native sessions require CSRF on mutations. Project Bearer credentials are scoped and intended only for trusted servers.', docs_url=None, redoc_url=None, openapi_url='/api/v1/openapi.json', lifespan=lifespan)
    app.state.ai_mcp_tools = ai_mcp_tools
    app.state.storage, app.state.config, app.state.worker = storage, config, worker
    app.state.ai_embeddings = ai_embeddings
    app.state.push = push
    app.state.push_worker = push_worker
    app.state.ai_runtime = ai_runtime
    app.state.ai_ingestion = ai_ingestion
    app.state.ai_retrieval = ai_retrieval
    app.state.ai_generation = ai_generation
    app.state.ai_tools = ai_tools
    app.state.ai_chat_worker = ai_chat_worker
    app.add_middleware(RequestGuard, config=config, storage=storage)

    @app.exception_handler(Error)
    async def domain_error(request, exc):
        return JSONResponse({'error': {'code': exc.code, 'message': exc.message}}, status_code=exc.status)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        # Do not echo submitted passwords or other raw inputs.
        fields = [{'field': '.'.join(str(x) for x in e['loc']), 'message': e['msg']} for e in exc.errors()]
        return JSONResponse({'error': {'code': 'invalid_input', 'message': 'Check the submitted fields', 'fields': fields}}, status_code=400)

    @app.exception_handler(RoutingError)
    async def routing_error(request, exc):
        codes = {404: 'not_found', 405: 'method_not_allowed'}
        return JSONResponse({'error': {'code': codes.get(exc.status_code, 'http_error'), 'message': str(exc.detail)}}, status_code=exc.status_code)

    @app.exception_handler(Exception)
    async def unexpected_error(request, exc):
        return JSONResponse({'error': {'code': 'internal_error', 'message': 'The operation could not be completed'}}, status_code=500)

    def actor(request: Request, bearer=Security(api_secret), token=Security(cookie_secret)):
        if bearer:
            if token:
                raise Error(400, 'ambiguous_auth', 'Use one authentication method')
            if not bearer.startswith('Bearer '):
                raise Error(401, 'unauthorized', 'Invalid API credential')
            secret = bearer[7:]
            if secret.startswith('ns_'):
                session = storage.session(digest(secret))
                if not session or session['transport'] != 'native' or session['role'] != 'admin':
                    raise Error(401, 'unauthorized', 'Admin session expired; sign in again')
                request.state.session = session
                return Actor(user_id=session['id'], role=session['role'], ip_address=client_ip(request))
            key = storage.api_key(digest(secret))
            if not key:
                raise Error(401, 'unauthorized', 'Invalid API credential')
            return Actor(role='integration', project_id=key['project_id'], key_id=key['id'], permissions=tuple(json.loads(key['permissions'])), ip_address=client_ip(request))
        if token:
            session = storage.session(digest(token))
            if not session or session['transport'] != 'cookie':
                if request.method in ('GET', 'HEAD', 'OPTIONS'):
                    return Actor(ip_address=client_ip(request))
                raise Error(401, 'unauthorized', 'Session expired; sign in again')
            if request.method not in ('GET', 'HEAD', 'OPTIONS'):
                csrf = request.headers.get('X-CSRF-Token', '')
                if request.headers.get('Origin') != config.base_url or not hmac.compare_digest(digest(csrf), session['csrf_hash']):
                    raise Error(403, 'csrf_failed', 'Refresh the page and retry')
            request.state.session = session
            return Actor(user_id=session['id'], role=session['role'], ip_address=client_ip(request))
        return Actor(ip_address=client_ip(request))

    Current = Annotated[Actor, Depends(actor)]
    from feedback.ai_http import admin_dependency
    app.state.ai_admin = admin_dependency(actor)

    def read_scope(a, pid):
        service.scope(a, pid)
        if a.key_id and not (a.can(pid, 'read') or a.manages(pid)):
            raise Error(403, 'forbidden', 'Read permission required')

    def login_session(response, user, ip_address):
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        storage.create_session(user['id'], digest(token), digest(csrf), time.time() + config.session_seconds, ip_address=ip_address)
        response.set_cookie('feedback_session', token, max_age=config.session_seconds, httponly=True, secure=config.cookie_secure, samesite='strict', path='/')
        response.set_cookie('feedback_csrf', csrf, max_age=config.session_seconds, httponly=False, secure=config.cookie_secure, samesite='strict', path='/')
        return {'user': {k: user[k] for k in ('id', 'display_name', 'role')}, 'csrf_token': csrf}

    def clear_session(response):
        response.delete_cookie('feedback_session', path='/', secure=config.cookie_secure, httponly=True, samesite='strict')
        response.delete_cookie('feedback_csrf', path='/', secure=config.cookie_secure, samesite='strict')

    @app.get('/healthz', tags=['Operations'])
    def health():
        return {'ok': True}

    @app.get('/readyz', tags=['Operations'])
    def ready():
        return {'ok': storage.ready()}

    @app.get('/api/v1/setup', tags=['Authentication'], response_model=SetupOutput, response_model_exclude_unset=True)
    def setup_state():
        return {'available': storage.setup_available(), 'registration_enabled': config.registration}

    @app.post('/api/v1/setup', status_code=201, tags=['Authentication'], description='Requires the deployment setup token. Atomically creates the first administrator and permanently disables setup.', response_model=SessionOutput, response_model_exclude_unset=True)
    def setup(data: SetupInput, response: Response, request: Request):
        if not hmac.compare_digest(data.setup_token, config.setup_token):
            raise Error(403, 'invalid_setup_token', 'Invalid setup token')
        user = storage.create_native_account(data.email, hasher.hash(data.password), data.display_name, 'admin', setup=True, ip_address=client_ip(request), activity_type='setup')
        return login_session(response, user, client_ip(request))

    @app.post('/api/v1/auth/register', status_code=201, tags=['Authentication'], description='Creates a participant. Email is a login identifier, not verified.', response_model=SessionOutput, response_model_exclude_unset=True)
    def register(data: AccountInput, response: Response, request: Request):
        if not config.registration or storage.setup_available():
            raise Error(403, 'registration_disabled', 'Registration is unavailable')
        user = storage.create_native_account(data.email, hasher.hash(data.password), data.display_name, ip_address=client_ip(request), activity_type='signup')
        return login_session(response, user, client_ip(request))

    @app.post('/api/v1/auth/login', tags=['Authentication'], response_model=SessionOutput, response_model_exclude_unset=True)
    def login(data: LoginInput, response: Response, request: Request):
        user = storage.credentials(data.email.strip().casefold())
        if not verify_password(user['password_hash'] if user else None, data.password):
            raise Error(401, 'invalid_login', 'Email or password is incorrect')
        return login_session(response, user, client_ip(request))

    @app.get('/api/v1/auth/me', tags=['Authentication'], response_model=UserOutput, response_model_exclude_unset=True)
    def me(request: Request, a: Current):
        if not a.user_id:
            raise Error(401, 'unauthorized', 'Sign in first')
        return {'id': a.user_id, 'role': a.role, 'display_name': request.state.session['display_name']}

    @app.post('/api/v1/auth/native/login', tags=['Authentication'], response_model=NativeSessionOutput)
    def native_login(data: LoginInput, request: Request):
        if request.headers.get('Authorization') or request.cookies.get('feedback_session'):
            raise Error(400, 'ambiguous_auth', 'Sign in without existing credentials')
        user = storage.credentials(data.email.strip().casefold())
        if not verify_password(user['password_hash'] if user else None, data.password) or user['role'] != 'admin':
            raise Error(401, 'invalid_login', 'Administrator email or password is incorrect')
        token, expires = 'ns_' + secrets.token_urlsafe(32), time.time() + config.session_seconds
        storage.create_session(user['id'], digest(token), digest(secrets.token_urlsafe(32)), expires, 'native', ip_address=client_ip(request))
        return {'user':user, 'token':token, 'expires_at':utc_timestamp(expires), 'installation_id':storage.installation_id()}

    @app.post('/api/v1/auth/logout', status_code=204, tags=['Authentication'])
    def logout(request: Request, response: Response, a: Current):
        if not a.user_id:
            raise Error(401, 'unauthorized', 'Native login required')
        storage.revoke_session(request.state.session['session_id'])
        clear_session(response)

    @app.post('/api/v1/auth/password', status_code=204, tags=['Authentication'], description='Changes the password and revokes every existing session, including this one.')
    def password(data: PasswordInput, response: Response, a: Current):
        if not a.user_id or a.key_id:
            raise Error(401, 'unauthorized', 'Native login required')
        credential = storage.native_credential_for_user(a.user_id)
        if not verify_password(credential['password_hash'], data.current_password):
            raise Error(403, 'invalid_password', 'Current password is incorrect')
        storage.change_password(a.user_id, hasher.hash(data.new_password), credential['password_hash'])
        clear_session(response)

    @app.post('/api/v1/admin/accounts', status_code=201, tags=['Administration'], response_model=UserOutput, response_model_exclude_unset=True)
    def add_admin(data: AccountInput, a: Current):
        require_admin(a)
        return storage.create_native_account(data.email, hasher.hash(data.password), data.display_name, 'admin', ip_address=a.ip_address)

    @app.get('/api/v1/admin/account-activity', tags=['Administration'], response_model=PageOutput[AccountActivityOutput], description='Native administrators only. Durable successful signup/setup, account creation and login history, newest first with stable ID ordering. IPs are observed connection addresses, not identity proof. Session expiry does not erase history. Optional user_id scopes the list to one account.')
    def account_activity(request: Request, page: Page, a: Current, user_id: UUID | None = None):
        require_admin(a)
        return page.page(storage.account_activity(a.user_id, request.state.session['session_id'], page.limit + 1, page.offset, str(user_id) if user_id else None))

    @app.get('/api/v1/admin/push', tags=['Push'],response_model=PushOutput)
    def push_settings(a: Current):
        require_admin(a)
        return push.settings()

    @app.put('/api/v1/admin/push', tags=['Push'],response_model=PushOutput)
    def push_configure(data: PushInput,request: Request,a: Current):
        require_admin(a)
        return push.configure(a.user_id,request.state.session['session_id'],data.enabled,data.relay_url)

    @app.post('/api/v1/admin/push/rotate', tags=['Push'],response_model=PushOutput)
    def push_rotate(request: Request,a: Current):
        require_admin(a)
        return push.rotate(a.user_id,request.state.session['session_id'])

    @app.get('/api/v1/admin/push/deliveries', tags=['Push'],response_model=PageOutput[PushDeliveryOutput])
    def push_deliveries(page: Page,a: Current):
        require_admin(a)
        return page.page([push_delivery_view(x) for x in storage.push_deliveries(page.limit+1,page.offset)])

    @app.post('/api/v1/admin/push/deliveries/{delivery_id}/retry',status_code=202,tags=['Push'],response_model=RetryOutput)
    def push_retry(delivery_id: UUID,request: Request,a: Current):
        require_admin(a)
        storage.retry_push(a.user_id,request.state.session['session_id'],str(delivery_id))
        return {'queued':True}

    @app.get('/api/v1/admin/devices', tags=['Push'],response_model=DevicesOutput)
    def devices(a: Current):
        return {'items': service.devices(a)}

    @app.put('/api/v1/admin/devices/{device_id}', tags=['Push'],response_model=DeviceOutput)
    def register_device(device_id: UUID, data: DeviceInput, request: Request, a: Current):
        require_admin(a)
        return service.register_device(a,request.state.session['session_id'],str(device_id),data.model_dump(),vault)

    @app.delete('/api/v1/admin/devices/{device_id}',status_code=204,tags=['Push'])
    def remove_device(device_id: UUID, request: Request, a: Current):
        require_admin(a)
        service.remove_device(a,request.state.session['session_id'],str(device_id))

    @app.get('/api/v1/admin/dashboard', tags=['Administration'], response_model=DashboardOutput, response_model_exclude_unset=True)
    def dashboard(a: Current):
        require_admin(a)
        return storage.dashboard()

    @app.get('/api/v1/projects', tags=['Projects'], response_model=PageOutput[ProjectOutput], response_model_exclude_unset=True)
    def projects(page: Page, a: Current, archive: Literal['active', 'archived', 'all'] = 'active'):
        return page.page(service.projects(a, page.limit + 1, page.offset, archive))

    @app.post('/api/v1/projects', status_code=201, tags=['Projects'], response_model=ProjectOutput, response_model_exclude_unset=True)
    def create_project(data: ResourceInput, a: Current):
        require_admin(a)
        return public_project(storage.create_project(data.model_dump(), a.user_id))

    @app.get('/api/v1/projects/{project}', tags=['Projects'], response_model=ProjectOutput, response_model_exclude_unset=True)
    def project(project: str, a: Current):
        p = service.project(project, a)
        read_scope(a, p['id'])
        return public_project(p)

    @app.patch('/api/v1/projects/{project}', tags=['Projects'], response_model=ProjectOutput, response_model_exclude_unset=True)
    def update_project(project: str, data: ResourceEdit, a: Current):
        require_admin(a)
        p = service.project(project, a)
        return public_project(storage.update_project(p['id'], data.model_dump()))

    @app.put('/api/v1/projects/{project}/archive', tags=['Projects'], response_model=ProjectOutput,
             description='Native administrators only. Versioned archive/restore; preserves history and pauses new activity.')
    def archive_project(project: str, data: ArchiveInput, request: Request, a: Current):
        require_admin(a)
        p = service.project(project, a)
        principal = {'kind':'admin','user_id':a.user_id,'session_id':request.state.session['session_id']}
        return public_project(storage.resource_archive('project',p['id'],principal,data.archived,data.version))

    @app.get('/api/v1/projects/{project}/boards', tags=['Boards'], response_model=PageOutput[BoardOutput], response_model_exclude_unset=True)
    def boards(project: str, page: Page, a: Current, archive: Literal['active', 'archived', 'all'] = 'active'):
        if archive != 'active': require_admin(a)
        p = service.project(project, a)
        read_scope(a, p['id'])
        return page.page(storage.boards(p['id'], page.limit + 1, page.offset, archive, include_private=service.trusted_board_reader(a, p['id'])))

    @app.post('/api/v1/projects/{project}/boards', status_code=201, tags=['Boards'], response_model=BoardOutput, response_model_exclude_unset=True)
    def create_board(project: str, data: BoardInput, request: Request, a: Current):
        p = service.project(project, a)
        require_manage(a, p['id'])
        return storage.create_board(p['id'], data.model_dump(), actor=a, session_id=request.state.session['session_id'] if not a.key_id else None)

    @app.get('/api/v1/boards/{board}', tags=['Boards'], response_model=BoardOutput, response_model_exclude_unset=True)
    def board(board: str, a: Current):
        b = service.board(board, a)
        read_scope(a, b['project_id'])
        return b

    @app.patch('/api/v1/boards/{board}', tags=['Boards'], response_model=BoardOutput, response_model_exclude_unset=True)
    def update_board(board: str, data: BoardEdit, request: Request, a: Current):
        b = service.board(board, a)
        service.scope(a, b['project_id'])
        require_manage(a, b['project_id'])
        return storage.update_board(board, data.model_dump(exclude_unset=True), actor=a, session_id=request.state.session['session_id'] if not a.key_id else None)

    @app.put('/api/v1/boards/{board}/archive', tags=['Boards'], response_model=BoardOutput,
             description='Native administrators only. Versioned archive/restore. Restore the parent project before restoring a board.')
    def archive_board(board: str, data: ArchiveInput, request: Request, a: Current):
        require_admin(a)
        principal = {'kind':'admin','user_id':a.user_id,'session_id':request.state.session['session_id']}
        return storage.resource_archive('board',board,principal,data.archived,data.version)

    @app.get('/api/v1/boards/{board}/requests', tags=['Requests'], response_model=PageOutput[RequestOutput], response_model_exclude_unset=True)
    def board_requests(board: str, page: Page, a: Current, status: LifecycleFilter | None = None, order: Literal['newest', 'popular'] = 'newest', external_user_id: External = None, moderation: Literal['accepted','pending','rejected','all'] = 'accepted'):
        if moderation != 'accepted': require_admin(a)
        b = service.board(board, a)
        read_scope(a, b['project_id'])
        if external_user_id is not None:
            a = service.participant(a, b['project_id'], external_user_id)
        return page.page([request_view(x, a) for x in storage.list_requests(board_id=board, moderation=None if moderation == 'all' else moderation, status=status, order=order, limit=page.limit + 1, offset=page.offset, user_id=a.user_id, include_archived=a.role == "admin" and not a.key_id, include_private=service.trusted_board_reader(a, b["project_id"]))])

    @app.post('/api/v1/boards/{board}/requests', status_code=201, tags=['Requests'], response_model=RequestOutput, response_model_exclude_unset=True)
    def submit(board: str, data: Submission, a: Current):
        return service.submit(board, data.title, data.description, a, data.external_user_id)

    @app.get('/api/v1/me/requests', tags=['Requests'], description='Native account submissions across projects. Project credentials require external_user_id and participate; resolution is automatic.', response_model=PageOutput[RequestOutput], response_model_exclude_unset=True)
    def own_requests(page: Page, a: Current, external_user_id: External = None):
        if a.key_id:
            a = service.participant(a, a.project_id, external_user_id)
        elif not a.user_id or external_user_id is not None:
            raise Error(401 if not a.user_id else 400, 'unauthorized', 'Native login or a project credential with external_user_id required')
        return page.page([request_view(x, a) for x in storage.list_requests(creator_id=a.user_id, project_id=a.project_id, moderation=None, limit=page.limit + 1, offset=page.offset, user_id=a.user_id)])

    @app.get('/api/v1/moderation', tags=['Moderation'], description='Installation queue for admins; project queue for manage credentials. Pending by default.', response_model=PageOutput[RequestOutput], response_model_exclude_unset=True)
    def moderation(page: Page, a: Current, project_id: str | None = None, state: Literal['pending', 'rejected', 'accepted'] = 'pending'):
        if a.key_id:
            service.scope(a, project_id or a.project_id)
            project_id = a.project_id
            require_manage(a, project_id)
        else:
            require_admin(a)
        return page.page([request_view(x, a) for x in storage.list_requests(project_id=project_id, moderation=state, limit=page.limit + 1, offset=page.offset, user_id=a.user_id, include_private=True)])

    @app.get('/api/v1/requests/{request_id}', tags=['Requests'], response_model=RequestOutput, response_model_exclude_unset=True)
    def detail(request_id: str, a: Current, external_user_id: External = None):
        r = storage.request(request_id)
        read_scope(a, r['project_id'])
        if external_user_id is not None:
            a = service.participant(a, r['project_id'], external_user_id)
        return request_view(service.readable_request(request_id, a), a)

    @app.patch('/api/v1/requests/{request_id}', tags=['Requests'], response_model=RequestOutput, response_model_exclude_unset=True)
    def edit(request_id: str, data: CreatorEdit, a: Current):
        return service.edit(request_id, data.model_dump(), a, data.external_user_id)

    @app.delete('/api/v1/requests/{request_id}', status_code=204, tags=['Requests'])
    def delete(request_id: str, version: Annotated[int, Query(ge=1)], a: Current, external_user_id: External = None):
        service.edit(request_id, {'version': version}, a, external_user_id, delete=True)

    @app.patch('/api/v1/requests/{request_id}/content', tags=['Moderation'], response_model=RequestOutput, response_model_exclude_unset=True)
    def admin_edit(request_id: str, data: AdminEdit, a: Current):
        return service.moderate(request_id, 'content', (data.title, data.description), data.version, a)

    @app.post('/api/v1/requests/{request_id}/accept', tags=['Moderation'], response_model=RequestOutput, response_model_exclude_unset=True)
    def accept(request_id: str, data: VersionInput, a: Current):
        return service.moderate(request_id, 'accept', None, data.version, a)

    @app.post('/api/v1/requests/{request_id}/reject', tags=['Moderation'], response_model=RequestOutput, response_model_exclude_unset=True)
    def reject(request_id: str, data: Rejection, a: Current):
        return service.moderate(request_id, 'reject', data.reason, data.version, a)

    @app.put('/api/v1/requests/{request_id}/status', tags=['Moderation'], response_model=RequestOutput, response_model_exclude_unset=True)
    def status(request_id: str, data: Lifecycle, a: Current):
        return service.moderate(request_id, 'status', data.status, data.version, a)

    @app.put('/api/v1/requests/{request_id}/vote', tags=['Voting'], description='Idempotently sets the acting participant vote; only accepted requests are voteable.', response_model=VoteOutput, response_model_exclude_unset=True)
    def vote(request_id: str, data: Vote, a: Current):
        return service.vote(request_id, data.present, a, data.external_user_id)

    @app.get('/api/v1/requests/{request_id}/vote', tags=['Voting'], response_model=VoteOutput, response_model_exclude_unset=True)
    def vote_state(request_id: str, a: Current, external_user_id: External = None):
        r, a = service.acting_request(request_id, a, external_user_id)
        r = service.readable_request(request_id, a)
        return {'vote_count': r['vote_count'], 'voted': bool(r['voted'])}

    @app.get('/api/v1/requests/{request_id}/history', tags=['Requests'], description='Public reads expose acceptance and status history only. Owners/managers see private history.', response_model=PageOutput[EventOutput], response_model_exclude_unset=True)
    def history(request_id: str, page: Page, a: Current, external_user_id: External = None):
        r = storage.request(request_id)
        read_scope(a, r['project_id'])
        if external_user_id is not None:
            a = service.participant(a, r['project_id'], external_user_id)
        return page.page(service.history(request_id, a, page.limit + 1, page.offset))

    @app.get('/api/v1/requests/{request_id}/audience', tags=['Voting'], description='Requires audience:read or admin. Current creator and voters, deduplicated; not an event-time snapshot.', response_model=PageOutput[IdentityOutput], response_model_exclude_unset=True)
    def audience(request_id: str, page: Page, a: Current):
        return page.page(service.audience(request_id, a, page.limit + 1, page.offset))

    @app.get('/api/v1/projects/{project}/keys', tags=['Credentials'], response_model=PageOutput[KeyOutput], response_model_exclude_unset=True)
    def keys(project: str, page: Page, a: Current):
        require_admin(a)
        p = service.project(project, a)
        return page.page([dict(x, permissions=json.loads(x['permissions'])) for x in storage.api_keys(p['id'], page.limit + 1, page.offset)])

    @app.post('/api/v1/projects/{project}/keys', status_code=201, tags=['Credentials'], description='Admin only. Secret is shown once; store on a trusted backend.', response_model=NewKeyOutput, response_model_exclude_unset=True)
    def create_key(project: str, data: KeyInput, a: Current):
        require_admin(a)
        p = service.project(project, a)
        secret = 'fb_' + secrets.token_urlsafe(32)
        kid = storage.create_api_key(p['id'], data.name, digest(secret), secret[:11], sorted(set(data.permissions)), a.user_id)
        return {'id': kid, 'secret': secret, 'prefix': secret[:11], 'permissions': sorted(set(data.permissions))}

    @app.delete('/api/v1/projects/{project}/keys/{key_id}', status_code=204, tags=['Credentials'])
    def revoke_key(project: str, key_id: str, a: Current):
        require_admin(a)
        p = service.project(project, a)
        storage.revoke_api_key(p['id'], key_id)

    @app.get('/api/v1/projects/{project}/webhook', tags=['Webhooks'], response_model=WebhookOutput, response_model_exclude_unset=True)
    def webhook(project: str, a: Current):
        p = service.project(project, a)
        require_manage(a, p['id'])
        return storage.webhook_settings(p['id'])

    @app.put('/api/v1/projects/{project}/webhook', tags=['Webhooks'], description='Creates or rotates a secret, returned only on creation/rotation. Existing jobs retain their encrypted signing snapshot.', response_model=WebhookOutput, response_model_exclude_unset=True)
    def update_webhook(project: str, data: WebhookInput, a: Current):
        p = service.project(project, a)
        require_manage(a, p['id'])
        if data.url:
            destination(data.url, config.allow_internal_webhooks)
        if data.enabled and not data.url:
            raise Error(400, 'invalid_input', 'Enabled webhooks require a URL')
        secret = secrets.token_urlsafe(32) if data.rotate_secret or not p['webhook_secret'] else None
        result = storage.update_webhook(p['id'], data.url, data.enabled, sorted(set(data.events)), vault.protect(secret) if secret else None, data.version)
        if secret:
            result['signing_secret'] = secret
        return result

    @app.post('/api/v1/projects/{project}/webhook/test', status_code=202, tags=['Webhooks'], response_model=TestOutput, response_model_exclude_unset=True)
    def test_webhook(project: str, a: Current):
        p = service.project(project, a)
        require_manage(a, p['id'])
        return storage.test_webhook(p['id'], a)

    @app.get('/api/v1/projects/{project}/deliveries', tags=['Webhooks'], response_model=PageOutput[DeliveryOutput], response_model_exclude_unset=True)
    def deliveries(project: str, page: Page, a: Current, failed: bool = False):
        p = service.project(project, a)
        require_manage(a, p['id'])
        return page.page([delivery_view(x) for x in storage.deliveries(p['id'], page.limit + 1, page.offset, failed)])

    @app.get('/api/v1/projects/{project}/deliveries/{delivery_id}', tags=['Webhooks'], response_model=DeliveryOutput, response_model_exclude_unset=True)
    def delivery(project: str, delivery_id: str, a: Current):
        p = service.project(project, a)
        require_manage(a, p['id'])
        return delivery_view(storage.delivery(p['id'], delivery_id))

    @app.post('/api/v1/projects/{project}/deliveries/{delivery_id}/retry', status_code=202, tags=['Webhooks'], response_model=RetryOutput, response_model_exclude_unset=True)
    def retry_delivery(project: str, delivery_id: str, a: Current):
        p = service.project(project, a)
        require_manage(a, p['id'])
        storage.retry_delivery(p['id'], delivery_id)
        return {'queued': True}

    web = Path(__file__).with_name('web')
    @app.get('/assets/{filename}', include_in_schema=False)
    def asset(filename: str):
        if filename not in ('app.js', 'forms.js', 'public-form.js', 'public-form.css', 'style.css', 'shell.js', 'chat.css', 'chat-presentation.js', 'ai-admin.js', 'ai-admin.css', 'connected-apps.css', 'connected-apps.js', 'connected-feed.js', 'business-hours.js', 'brand.svg', 'favicon.svg'):
            raise Error(404, 'not_found', 'Resource not found')
        # Asset URLs are stable across upgrades, so never reuse them without
        # checking the running installation for a newer version.
        return FileResponse(web / filename, headers={'Cache-Control': 'no-cache'})

    @app.get('/', include_in_schema=False)
    def home_redirect():
        return RedirectResponse('/login', status_code=303, headers={'Cache-Control': 'no-store'})

    @app.get('/workspace', include_in_schema=False)
    @app.get('/setup', include_in_schema=False)
    @app.get('/login', include_in_schema=False)
    @app.get('/register', include_in_schema=False)
    @app.get('/account', include_in_schema=False)
    @app.get('/my-requests', include_in_schema=False)
    @app.get('/admin/forms', include_in_schema=False)
    @app.get('/admin/forms/{form_id}', include_in_schema=False)
    @app.get('/admin/form-submissions/{submission_id}', include_in_schema=False)
    @app.get('/admin', include_in_schema=False)
    @app.get('/admin/ai', include_in_schema=False)
    @app.get('/admin/ai/tools', include_in_schema=False)
    @app.get('/admin/ai/providers', include_in_schema=False)
    @app.get('/admin/push', include_in_schema=False)
    @app.get('/admin/account-activity', include_in_schema=False)
    @app.get('/admin/app-activity', include_in_schema=False)
    @app.get('/admin/projects/{project}/app-data', include_in_schema=False)
    @app.get('/admin/projects/{project}', include_in_schema=False)
    @app.get('/admin/projects/{project}/ai', include_in_schema=False)
    @app.get('/projects/{project}', include_in_schema=False)
    @app.get('/boards/{board}', include_in_schema=False)
    @app.get('/requests/{request_id}', include_in_schema=False)
    def website(request: Request):
        # Direct-link feedback pages can be read publicly. Account and admin
        # surfaces require a cookie session; APIs enforce resource permissions.
        path = request.url.path
        protected = path == '/workspace' or path in {
            '/account', '/my-requests', '/admin', '/admin/ai',
            '/admin/ai/providers', '/admin/ai/tools', '/admin/push', '/admin/account-activity',
            '/admin/app-activity',
        } or path.startswith(('/admin/projects/', '/admin/forms', '/admin/form-submissions/'))
        if protected:
            token = request.cookies.get('feedback_session')
            session = storage.session(digest(token)) if token else None
            if not session or session.get('transport') != 'cookie':
                from urllib.parse import urlencode
                target = path + (('?' + request.url.query) if request.url.query else '')
                return RedirectResponse('/login?' + urlencode({'return': target}), status_code=303)
            if path == '/workspace' and session.get('role') != 'admin':
                return RedirectResponse('/my-requests', status_code=303)
        return FileResponse(web / 'index.html', headers={'Cache-Control': 'no-cache'})

    from feedback.chat_api import install_chat
    install_chat(app, storage, vault, config, actor)
    from feedback.ai_reports_api import install_ai_reports
    install_ai_reports(app, storage)
    from feedback.forms_api import install_forms
    install_forms(app, storage, actor, config)
    from feedback.notifications_api import install_notifications
    install_notifications(app, storage, actor)
    from feedback.ai_settings_api import install_ai_settings
    install_ai_settings(app, storage, vault, config, actor)
    from feedback.ai_mcp_api import install_mcp
    install_mcp(app,storage,vault,actor)
    app.state.ai_mcp_admin.admission = ai_mcp_tools.admission
    from feedback.ai_embeddings_api import install_embeddings
    install_embeddings(app, ai_embeddings, actor)
    from feedback.ai_runtime_api import install_ai_runtime
    install_ai_runtime(app, ai_runtime, actor)
    from feedback.ai_knowledge_api import install_ai_knowledge
    install_ai_knowledge(app, storage, vault, config, actor)
    from feedback.ai_crawler_api import install_ai_crawler
    install_ai_crawler(app, storage, vault, config, actor)
    from feedback.ai_authoring_api import install_ai_authoring
    install_ai_authoring(app, storage, vault, config, actor)
    from feedback.connected_apps_api import install_connected_apps
    install_connected_apps(app, storage, vault, actor)
    from feedback.openapi import install_openapi
    install_openapi(app)
    return app
