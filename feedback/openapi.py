"""Document the live HTTP contract without changing request handling."""
from copy import deepcopy

from fastapi.openapi.utils import get_openapi


# Explicit summaries keep the reference useful when several routers use names
# such as ``read``, ``save`` or ``run`` for unrelated operations.
_SUMMARY_ROWS = '''
GET /api/v1/public/forms/{form_id} | Read an enabled public form
POST /api/v1/public/forms/{form_id}/submissions | Submit the hosted public form
GET /api/v1/embed/forms/{form_id} | Read a form from an allowed website
POST /api/v1/embed/forms/{form_id}/submissions | Submit an embedded form
GET /api/v1/admin/forms | List private forms across projects
POST /api/v1/admin/projects/{project}/forms | Create a form
GET /api/v1/admin/forms/{form_id} | Read the current form definition
PUT /api/v1/admin/forms/{form_id} | Save the current form definition
POST /api/v1/forms/{form_id}/submissions | Submit answers from a trusted backend
GET /api/v1/admin/forms/{form_id}/submissions | List private form submissions
GET /api/v1/admin/form-submissions/{submission_id} | Read a private form submission
PUT /api/v1/admin/form-submissions/{submission_id}/state | Set the submission review status
DELETE /api/v1/admin/form-submissions/{submission_id} | Permanently remove submitted answers
GET /healthz | Check that the HTTP process is running
GET /readyz | Check the installation database connection
GET /api/v1/setup | Check whether initial setup is available
POST /api/v1/setup | Create the first administrator
POST /api/v1/auth/register | Create a participant account
POST /api/v1/auth/login | Sign in with a browser session
GET /api/v1/auth/me | Read the signed-in account
POST /api/v1/auth/native/login | Sign in as an administrator with a bearer session
POST /api/v1/auth/logout | Revoke the current account session
POST /api/v1/auth/password | Change a password and revoke all account sessions
POST /api/v1/admin/accounts | Create another administrator
GET /api/v1/admin/account-activity | List successful account creation and sign-in activity
GET /api/v1/admin/push | Read push relay settings
PUT /api/v1/admin/push | Configure the optional push relay
POST /api/v1/admin/push/rotate | Rotate the installation's push relay credential
GET /api/v1/admin/push/deliveries | List push delivery attempts
POST /api/v1/admin/push/deliveries/{delivery_id}/retry | Retry a push delivery
GET /api/v1/admin/devices | List registered push devices
PUT /api/v1/admin/devices/{device_id} | Register or update a push device
DELETE /api/v1/admin/devices/{device_id} | Remove a push device
GET /api/v1/admin/dashboard | Read administrator dashboard counts
GET /api/v1/projects | List projects
POST /api/v1/projects | Create a project
GET /api/v1/projects/{project} | Read a project
PATCH /api/v1/projects/{project} | Edit a project
PUT /api/v1/projects/{project}/archive | Archive or restore a project
GET /api/v1/projects/{project}/boards | List a project's boards
POST /api/v1/projects/{project}/boards | Create a board
GET /api/v1/boards/{board} | Read a board
PATCH /api/v1/boards/{board} | Edit a board
PUT /api/v1/boards/{board}/archive | Archive or restore a board
GET /api/v1/boards/{board}/requests | List a board's visible requests
POST /api/v1/boards/{board}/requests | Submit a request for moderation
GET /api/v1/me/requests | List the acting participant's requests
GET /api/v1/moderation | List requests awaiting moderation
GET /api/v1/requests/{request_id} | Read a visible request
PATCH /api/v1/requests/{request_id} | Edit or resubmit your request
DELETE /api/v1/requests/{request_id} | Delete your request before acceptance
PATCH /api/v1/requests/{request_id}/content | Edit request content as a manager
POST /api/v1/requests/{request_id}/accept | Accept a request
POST /api/v1/requests/{request_id}/reject | Reject a request
PUT /api/v1/requests/{request_id}/status | Set a request's lifecycle status
PUT /api/v1/requests/{request_id}/vote | Set or remove your vote
GET /api/v1/requests/{request_id}/vote | Read your current vote
GET /api/v1/requests/{request_id}/history | List visible request history
GET /api/v1/requests/{request_id}/audience | List the request's creator and current voters
GET /api/v1/projects/{project}/keys | List project credentials without their secrets
POST /api/v1/projects/{project}/keys | Create a project credential
DELETE /api/v1/projects/{project}/keys/{key_id} | Revoke a project credential
GET /api/v1/projects/{project}/webhook | Read webhook settings
PUT /api/v1/projects/{project}/webhook | Configure a webhook or rotate its signing secret
POST /api/v1/projects/{project}/webhook/test | Queue a test webhook delivery
GET /api/v1/projects/{project}/deliveries | List webhook deliveries
GET /api/v1/projects/{project}/deliveries/{delivery_id} | Inspect a webhook delivery and its saved payload
POST /api/v1/projects/{project}/deliveries/{delivery_id}/retry | Retry a webhook delivery with its saved payload
GET /api/v1/chat/projects/{project}/config | Read public chat availability and display settings
POST /api/v1/chat/projects/{project}/sessions | Start an anonymous website conversation
POST /api/v1/chat/projects/{project}/customer-sessions | Issue or renew customer chat access
POST /api/v1/chat/projects/{project}/customer-sessions/revoke | Revoke all chat credentials for a customer
GET /api/v1/chat/conversations | List your conversations
POST /api/v1/chat/conversations | Start a customer conversation
DELETE /api/v1/chat/session | Revoke the current customer chat credential
GET /api/v1/chat/conversations/{conversation_id} | Read a conversation you can access
PUT /api/v1/chat/conversations/{conversation_id}/support-mode | Change a conversation's AI or human support mode
GET /api/v1/chat/conversations/{conversation_id}/messages | Read messages after a conversation sequence
POST /api/v1/chat/conversations/{conversation_id}/messages | Send a message with an idempotent client ID
POST /api/v1/chat/conversations/{conversation_id}/read | Advance your conversation read position
DELETE /api/v1/chat/conversations/{conversation_id}/session | Revoke an anonymous visitor's chat credential
POST /api/v1/chat/tickets | Issue a single-use WebSocket ticket
GET /api/v1/admin/chat/business-hours | Read shared business hours
PUT /api/v1/admin/chat/business-hours | Save shared business hours
GET /api/v1/admin/chat/projects/{project}/business-hours | Read a project's business hours override
PUT /api/v1/admin/chat/projects/{project}/business-hours | Save or clear a project's business hours override
POST /api/v1/chat/conversations/{conversation_id}/contact/remove | Remove a conversation's private follow-up email
GET /api/v1/admin/chat/projects/{project} | Read a project's chat settings
PUT /api/v1/admin/chat/projects/{project} | Enable chat and configure allowed website origins
GET /api/v1/admin/chat/conversations | List the administrator chat inbox
GET /api/v1/admin/chat/conversations/{conversation_id} | Read a conversation as an administrator
PATCH /api/v1/admin/chat/conversations/{conversation_id} | Open or close a conversation
GET /api/v1/chat/conversations/{conversation_id}/proposals | List your feedback drafts from this conversation
GET /api/v1/chat/conversations/{conversation_id}/proposals/{proposal_id} | Read a feedback draft before submission
PUT /api/v1/chat/conversations/{conversation_id}/proposals/{proposal_id} | Edit a feedback draft
POST /api/v1/chat/conversations/{conversation_id}/proposals/{proposal_id}/cancel | Cancel a feedback draft
POST /api/v1/chat/conversations/{conversation_id}/proposals/{proposal_id}/confirm | Confirm the exact feedback draft for submission
GET /api/v1/chat/conversations/{conversation_id}/reports | List feedback submitted from your conversation
GET /api/v1/chat/conversations/{conversation_id}/reports/{report_id} | Read a submitted feedback receipt
PUT /api/v1/chat/conversations/{conversation_id}/reports/{report_id} | Edit submitted feedback before acceptance
DELETE /api/v1/chat/conversations/{conversation_id}/reports/{report_id} | Delete submitted feedback before acceptance
GET /api/v1/admin/notifications | List the shared administrator notification inbox
GET /api/v1/admin/notifications/summary | Read the shared unread count and sequence watermark
PUT /api/v1/admin/notifications/{notification_id}/read | Mark a notification read or unread for all administrators
POST /api/v1/admin/notifications/read-all | Mark notifications read through a displayed watermark
GET /api/v1/ai/decision-providers | List supported topic-decision providers
GET /api/v1/projects/{project}/ai/topics | Read topic-decision settings
PUT /api/v1/projects/{project}/ai/topics | Save topic-decision settings
GET /api/v1/projects/{project}/ai/topics/decisions | List topic-decision results
POST /api/v1/projects/{project}/ai/topics/keys | Add an AI key for topic decisions
POST /api/v1/projects/{project}/ai/topics/keys/{key_id}/remove | Remove a project's topic-decision key binding
POST /api/v1/projects/{project}/ai/topics/keys/{key_id}/models/refresh | Refresh a topic-decision key's available models
POST /api/v1/projects/{project}/ai/topics/keys/{key_id}/probe | Probe a key and model for topic decisions
POST /api/v1/projects/{project}/ai/topics/keys/{key_id}/test | Test a topic decision with supplied context
GET /api/v1/ai/keys | List the installation's AI credential library
POST /api/v1/ai/keys | Add an AI credential to the installation library
PUT /api/v1/ai/keys/{key_id} | Edit an installation AI credential and its allowed roles
DELETE /api/v1/ai/keys/{key_id} | Delete an AI credential and detach every project binding
POST /api/v1/ai/keys/{key_id}/retry | Retry an installation AI credential
POST /api/v1/ai/keys/{key_id}/models/refresh | Refresh an installation AI credential's model catalogue
PUT /api/v1/projects/{project}/ai/keys/selection | Select account keys for a project's AI replies
PUT /api/v1/projects/{project}/ai/topics/keys/selection | Select account keys for a project's topic decisions
GET /api/v1/ai/providers | List supported AI reply providers
GET /api/v1/ai/tool-connections | List the installation's public MCP connections
POST /api/v1/ai/tool-connections | Add an encrypted public MCP connection
PATCH /api/v1/ai/tool-connections/{cid} | Edit a connection and invalidate its project approvals
DELETE /api/v1/ai/tool-connections/{cid} | Remove a connection and detach its project approvals
POST /api/v1/ai/tool-connections/{cid}/discover | Discover and validate a connection's tool catalogue
POST /api/v1/ai/tool-connections/{cid}/test | Run an explicit bounded read-only tool test
GET /api/v1/projects/{project}/ai/tool-connections | Read project answer sources and approved MCP tools
PUT /api/v1/projects/{project}/ai/tool-connections | Select project answer sources and exact reviewed MCP tools
GET /api/v1/projects/{project}/ai | Read a project's AI support settings
PUT /api/v1/projects/{project}/ai | Save a project's AI support settings
GET /api/v1/projects/{project}/ai/usage | Read a project's AI usage and quota status
POST /api/v1/projects/{project}/ai/models/refresh | Refresh available models for the configured provider
POST /api/v1/projects/{project}/ai/model/probe | Probe the configured AI reply model
GET /api/v1/projects/{project}/ai/keys | List AI reply keys available to a project
POST /api/v1/projects/{project}/ai/keys | Add an AI reply key and bind it to this project
PUT /api/v1/projects/{project}/ai/keys/{key_id} | Edit an AI reply key through a project binding
DELETE /api/v1/projects/{project}/ai/keys/{key_id} | Delete a shared AI reply credential through a project binding
POST /api/v1/projects/{project}/ai/keys/{key_id}/retry | Retry a project's AI reply key
POST /api/v1/projects/{project}/ai/keys/{key_id}/models/refresh | Refresh models for one AI reply key
POST /api/v1/projects/{project}/ai/keys/{key_id}/model/probe | Probe one AI reply key and model
GET /api/v1/projects/{project}/ai/feedback-destinations | List boards available to AI feedback tools
GET /api/v1/ai/embeddings | Read the installation embedding provider and index profile
PUT /api/v1/ai/embeddings | Verify and change the embedding provider, pausing AI for reindexing
POST /api/v1/ai/embeddings/reindex | Queue remaining approved sources for the current embedding profile
GET /api/v1/ai/runtime | Read local embedding model installation status
POST /api/v1/ai/runtime/download | Start downloading the pinned embedding model
POST /api/v1/ai/runtime/cancel | Cancel the embedding model download
POST /api/v1/ai/runtime/remove | Remove downloaded embedding model files
GET /api/v1/projects/{project}/ai/knowledge/library | List original and generated knowledge
GET /api/v1/projects/{project}/ai/knowledge/library/{item_id} | Read one knowledge library item
GET /api/v1/projects/{project}/ai/knowledge/sources | List knowledge sources
GET /api/v1/projects/{project}/ai/knowledge/sources/{source_id} | Read a knowledge source
PATCH /api/v1/projects/{project}/ai/knowledge/sources/{source_id} | Change a knowledge source's retrieval approval
DELETE /api/v1/projects/{project}/ai/knowledge/sources/{source_id} | Delete a knowledge source
POST /api/v1/projects/{project}/ai/knowledge/markdown | Import Markdown as a knowledge source
PUT /api/v1/projects/{project}/ai/knowledge/sources/{source_id}/markdown | Replace a Markdown knowledge source
GET /api/v1/projects/{project}/ai/knowledge/sources/{source_id}/documents | List a source's original documents
GET /api/v1/projects/{project}/ai/knowledge/sources/{source_id}/documents/{document_id} | Read an original knowledge document
POST /api/v1/projects/{project}/ai/knowledge/websites | Start importing a website as a knowledge source
PUT /api/v1/projects/{project}/ai/knowledge/sources/{source_id}/website | Refresh a website knowledge source
GET /api/v1/projects/{project}/ai/knowledge/sources/{source_id}/crawls | List website import runs
GET /api/v1/projects/{project}/ai/knowledge/sources/{source_id}/crawls/{run_id} | Read a website import run
POST /api/v1/projects/{project}/ai/knowledge/sources/{source_id}/crawls/{run_id}/cancel | Cancel a website import run
POST /api/v1/projects/{project}/ai/previews | Preview an AI reply with the current project settings
POST /api/v1/projects/{project}/ai/knowledge/sources/{source_id}/sections/generate | Start generating knowledge sections from a source
GET /api/v1/projects/{project}/ai/authoring-runs | List AI authoring runs
GET /api/v1/projects/{project}/ai/authoring-runs/{run_id} | Read an AI authoring run
POST /api/v1/projects/{project}/ai/authoring-runs/{run_id}/cancel | Cancel an AI authoring run
POST /api/v1/projects/{project}/ai/authoring-runs/{run_id}/retry | Retry an AI authoring run
GET /api/v1/projects/{project}/ai/knowledge/sections | List generated knowledge sections
POST /api/v1/projects/{project}/ai/knowledge/sections/{section_id}/approve | Approve a generated section for retrieval
PUT /api/v1/projects/{project}/ai/knowledge/sections/{section_id} | Edit a generated knowledge section
DELETE /api/v1/projects/{project}/ai/knowledge/sections/{section_id} | Delete a generated knowledge section
GET /api/v1/projects/{project}/ai/personality | Read the project's AI personality instructions
PUT /api/v1/projects/{project}/ai/personality | Save the project's AI personality instructions
GET /api/v1/admin/connected-apps | List connected-app configuration for active projects
GET /api/v1/admin/connected-apps/preferences | Read the connected-app display timezone
PUT /api/v1/admin/connected-apps/preferences | Save the connected-app display timezone
GET /api/v1/projects/{project}/app-data/connection | Read a project's connected-app configuration
PUT /api/v1/projects/{project}/app-data/connection | Save a connected-app URL and encrypted credential
POST /api/v1/projects/{project}/app-data/connection/test | Test a connection and return its review receipt
POST /api/v1/projects/{project}/app-data/connection/enable | Enable a tested connection after review
POST /api/v1/projects/{project}/app-data/connection/disable | Disable a connected app
POST /api/v1/projects/{project}/app-data/connection/remove | Remove connection credentials while retaining app identity
POST /api/v1/projects/{project}/app-data/summary/query | Read current account counts from a connected app
POST /api/v1/projects/{project}/app-data/users/query | Read a page of users from a connected app
POST /api/v1/projects/{project}/app-data/profile/query | Read one connected-app user's profile
POST /api/v1/projects/{project}/app-data/activity/query | Read a page of activity from a connected app
POST /api/v1/projects/{project}/app-data/related/query | Find local feedback and chat for an exact connected-app user ID
'''
SUMMARIES = {tuple(key.lower().split(' ', 1)): value for key, value in
             (line.split(' | ', 1) for line in _SUMMARY_ROWS.strip().splitlines())}

COOKIE = {'APIKeyCookie': []}
NATIVE = {'NativeBearer': []}
PROJECT = {'ProjectBearer': []}
CUSTOMER = {'ChatCustomer': []}
VISITOR = {'ChatVisitor': []}
ADMIN = 'Administrator account required (browser session or native bearer token). Use an administrator feedback_session cookie or ns_ bearer session. Participant accounts cannot use this operation.'
ACCOUNT = 'A local participant or administrator feedback_session cookie, or an administrator ns_ bearer session.'
PROJECT_IDENTITY = 'A trusted project backend may instead use an fb_ bearer credential with participate permission and the verified external_user_id. The ID is opaque, project-scoped and case-sensitive: preserve it exactly, including whitespace and Unicode. It is never a credential and does not link accounts across projects.'
CHAT_ORIGIN = 'Browser chat credentials require their exact bound Origin; same-origin HTTP requests may use Sec-Fetch-Site: same-origin to select the installation origin. Native customer credentials omit Origin. Never combine X-Chat-Token with Authorization or a feedback_session cookie.'


def _authorization(path, method):
    if '/customer-sessions' in path:
        auth = 'Use a local account session, or a trusted backend project key with participate permission and a verified external_user_id. Preserve the project-scoped ID exactly; it is never a credential. Project-key requests must omit Origin. Account sessions must omit external_user_id. Chat credentials cannot establish identity.'
        if path.endswith('/revoke'):
            auth += ' Revokes every chat credential for this customer in this project; durable conversations and read positions remain.'
        else:
            auth += ' For browser clients, send an allowed origin in the JSON body; local accounts may also use PUBLIC_BASE_URL. Native clients omit that field. Only project keys may supply display_name. The returned cc_ credential lasts one hour. Renew with the same verified identity to retain conversations and read positions. Revoking the issuing key or account session invalidates its derived credentials.'
        return auth, [COOKIE, NATIVE, PROJECT]
    if path.startswith('/api/v1/chat/projects/'):
        return 'Public website chat requires chat enabled and an exact allowed Origin. Same-origin configuration reads may use Sec-Fetch-Site: same-origin. Session creation accepts no account or project credentials; its cv_ visitor token is secret and scoped to one conversation and website origin.', []
    if path in ('/api/v1/chat/conversations', '/api/v1/chat/session'):
        auth = 'An owning cc_ customer credential is required.'
        if method == 'get':
            auth += ' Listing also accepts a cv_ visitor credential for its single conversation.'
        if path.endswith('/session'):
            auth += ' Revokes this credential and its tickets/sockets. Other device credentials and durable history remain intact.'
        return auth + ' ' + CHAT_ORIGIN, [CUSTOMER, VISITOR] if method == 'get' else [CUSTOMER]
    if path == '/api/v1/chat/conversations/{conversation_id}/session':
        return 'Owning cv_ visitor credential or native administrator. Authenticated customers revoke their current credential with DELETE /api/v1/chat/session. History is retained. ' + CHAT_ORIGIN, [VISITOR, COOKIE, NATIVE]
    if path.startswith('/api/v1/chat/'):
        if '/proposals' in path or '/reports' in path:
            return 'Owning cc_ customer or cv_ visitor credential only; administrators cannot confirm feedback on this customer surface. New drafts and confirmation recheck AI, the specific tool and support mode. Guest submission also requires explicit guest-report permission. Existing report ownership remains usable after creation settings change while the owning credential is valid. Acceptance permanently locks creator edits and deletion. ' + CHAT_ORIGIN, [CUSTOMER, VISITOR]
        return 'Owning cc_ customer, cv_ visitor, or native administrator. Direct project credentials and participant account cookies must first exchange for customer chat access. Each operation, ticket and reconnect rechecks ownership, project, expiry, revocation and issuing authority. ' + CHAT_ORIGIN, [CUSTOMER, VISITOR, COOKIE, NATIVE]
    if path.startswith('/api/v1/ai/') or '/ai' in path:
        return ADMIN + ' Project and chat credentials and participant accounts cannot administer AI. Reading settings does not download models or generate content. Provider requests, model downloads and generation require explicit administrator actions.', [COOKIE, NATIVE]
    if '/app-data/' in path or path.startswith('/api/v1/admin/connected-apps'):
        return ADMIN + ' Project credentials, participant accounts and chat credentials cannot access connection settings or data. Query POSTs are read-only, but cookie clients still require Origin and X-CSRF-Token. Remote data is fetched on demand without a persistent cache. App-issued credentials never reach clients.', [COOKIE, NATIVE]
    if path == '/api/v1/projects' and method == 'get':
        return 'Native administrators can list the installation directory. A project credential with read or manage permission sees only its own project. Visitors and regular accounts cannot list projects; use a creator-supplied direct project or board link.', [COOKIE, NATIVE, PROJECT]
    if path.startswith('/api/v1/embed/forms/'):
        return 'Credential-free website embed: send the browser Origin of an explicitly allowed website and omit cookies and Authorization. Embedding and submissions must be enabled. POST requires a signed two-hour ticket bound to the form, origin and current definition, client_request_id, answers and an empty website honeypot. Retry the same ticket, ID and answers. Only returns ok; answers remain administrator-only. Public-page and embed spam limits are shared.', []
    if path.startswith('/api/v1/public/forms/'):
        return 'Anonymous, explicitly enabled public form only. No listing or answer access. Submissions require the installation Origin, current signed ticket, validation and rate limits. Turning off public access closes this route immediately.', []
    if path.startswith('/api/v1/forms/'):
        return 'Backend only: an fb_ project token with forms:submit permission for this form’s project. Browser Origin and Sec-Fetch-Site requests are rejected. A token cannot read submissions or administer forms. Keep it on your server; your backend authenticates or rate-limits visitors. Reuse client_request_id and exactly the same answers on retries.', [PROJECT]
    if path.startswith('/api/v1/admin') or '/keys' in path or path.endswith('/archive') or (path in ('/api/v1/projects', '/api/v1/projects/{project}') and method in ('post', 'patch')):
        return ADMIN + ' Project credentials cannot create projects, administer accounts, archive resources or manage credentials.', [COOKIE, NATIVE]
    if path in ('/api/v1/auth/me', '/api/v1/auth/logout', '/api/v1/auth/password'):
        return ACCOUNT + ' Project and chat credentials do not authenticate a local account.', [COOKIE, NATIVE]
    if '/webhook' in path or '/deliveries' in path or path == '/api/v1/moderation' or any(path.endswith('/' + x) for x in ('accept', 'reject', 'status', 'content')) or (method in ('post', 'patch') and 'boards' in path and not path.endswith('/requests')):
        return 'Native administrator, or an fb_ project credential with manage permission. The resource must belong to that credential’s project.', [COOKIE, NATIVE, PROJECT]
    if path == '/api/v1/me/requests' or path.endswith('/vote') or (method in ('post', 'patch', 'delete') and '/requests' in path):
        return ACCOUNT + ' ' + PROJECT_IDENTITY + ' Internal user IDs supplied by a client cannot select an acting participant.', [COOKIE, NATIVE, PROJECT]
    if path.endswith('/audience'):
        return 'Native administrator, or an fb_ project credential with audience:read permission for this project. This private list deduplicates the creator and current voters; it is not the audience at the time of an earlier event.', [COOKIE, NATIVE, PROJECT]
    if path == '/api/v1/auth/native/login':
        return 'Administrator email and password required. Returns a revocable ns_ bearer session without a browser cookie. Use expires_at; the duration is installation-configurable (seven days by default). Omit existing Authorization and feedback_session credentials. Participant accounts cannot use this login.', []
    if path.startswith('/api/v1/auth/') or path == '/api/v1/setup':
        return 'No existing session is required. Setup requires the deployment setup token and is available only before the first administrator exists. Registration creates participants only and is unavailable before setup or when registration is disabled. Browser sign-in returns feedback_session and feedback_csrf cookies plus the csrf_token in JSON.', []
    return 'Anonymous public read. A supplied project credential needs read or manage permission for the resource’s project. Unpublished requests are visible only to their creator or a manager; unauthorized detail reads return 404. Archived resources and archive filters other than active require administrator access.', [{}, COOKIE, NATIVE, PROJECT]


def _body_limit(path, method):
    if method == 'put' and path.startswith('/api/v1/chat/conversations/{conversation_id}/') and any('/' + kind + '/' in path for kind in ('proposals', 'reports')):
        return 400
    if path.startswith('/api/v1/chat/'):
        return 32
    if (method == 'post' and path == '/api/v1/projects/{project}/ai/knowledge/markdown') or (method == 'put' and path in ('/api/v1/projects/{project}/ai', '/api/v1/projects/{project}/ai/personality', '/api/v1/projects/{project}/ai/knowledge/sources/{source_id}/markdown')):
        return 400
    return 96


def _error(description):
    return {'description': description, 'content': {'application/json': {'schema': {'$ref': '#/components/schemas/ApiError'}}}}


def _header(name, description, required=False):
    return {'in': 'header', 'name': name, 'required': required, 'schema': {'type': 'string'}, 'description': description}


def _describe_operation(path, method, op):
    op['summary'] = SUMMARIES.get((method, path), op.get('summary', ''))
    op['responses'].pop('422', None)
    if path in ('/healthz', '/readyz'):
        op['tags'] = ['Health']
        op.update(security=[], description='Unauthenticated process liveness check; this does not check the database.' if path == '/healthz' else 'Unauthenticated database connectivity check. A successful query returns ok=true; a database error uses the standard error envelope.')
        op['responses']['200']['content']['application/json']['schema'] = {'$ref': '#/components/schemas/HealthOutput'}
        if path == '/readyz':
            op['responses'].update({'500': _error('The database check could not complete'), '503': _error('The PostgreSQL connection is unavailable')})
        return
    for code, text in {
        400: 'Invalid input, malformed JSON or conflicting authentication transports; validation details may appear in error.fields',
        401: 'Required credentials are missing, invalid, expired or revoked',
        403: 'Insufficient permission, an invalid Origin or a missing/invalid cookie CSRF token',
        404: 'The resource is missing or concealed from this caller',
        409: 'The supplied version is stale or the requested action conflicts with current state; refresh before retrying',
        413: f'The JSON body exceeds {_body_limit(path, method)} KiB; individual field limits also apply',
        429: 'The request rate or operation quota was exceeded; some rate limits return Retry-After',
        500: 'The operation could not be completed; internal details and credentials are not returned',
        503: 'Storage or a required service is temporarily unavailable; retry idempotent operations after recovery',
    }.items():
        op['responses'][str(code)] = _error(text)
    op['responses']['429']['headers'] = {'Retry-After': {'description': 'When present, seconds to wait before another attempt.', 'schema': {'type': 'integer', 'minimum': 1}}}
    auth, security = _authorization(path, method)
    op['security'] = deepcopy(security)
    original = op.get('description', '')
    # These route docstrings are fully covered below. Keep other docstrings:
    # authoring and moderation routes carry useful operation-specific behavior.
    if ('/customer-sessions' in path or path.endswith('/audience')
            or path == '/api/v1/chat/tickets' or path == '/api/v1/chat/session'
            or path.endswith('/vote') or (path == '/api/v1/chat/conversations' and method == 'post')):
        original = ''
    if path.endswith('/messages') and method == 'post':
        original = 'Returns after the message, immutable event and applicable webhook/push jobs commit together. Notification payloads exclude the message body.'
    details = [original, auth]
    if path == '/api/v1/admin/push':
        details.append('SupaKnock’s optional iPhone push relay is https://relay.supaknock.com. Call this settings endpoint on your own installation, not on the relay. Feedback and chat also use your installation. The server enrolls with the relay and keeps its credential encrypted; clients never receive it.')
        if method == 'put':
            details.append('To enable push, send {"enabled":true,"relay_url":"https://relay.supaknock.com"}. Use the HTTPS origin without /api/v1. Push is off by default. You may choose a compatible self-hosted relay before enrollment; the enrolled origin cannot be changed through this setting.')
    mutating = method not in ('get', 'head', 'options')
    if mutating and COOKIE in security:
        details.append('Cookie-authenticated requests require Origin equal to PUBLIC_BASE_URL and X-CSRF-Token from sign-in. Bearer sessions and project credentials do not use CSRF tokens.')
        op.setdefault('parameters', []).append(_header('X-CSRF-Token', 'Required only with a feedback_session cookie. Send the csrf_token returned at sign-in (also available in feedback_csrf).'))
    admin_origin = path.startswith(('/api/v1/ai/', '/api/v1/admin/chat/', '/api/v1/admin/connected-apps')) or '/ai' in path or '/app-data/' in path
    if path.startswith('/api/v1/chat/') or mutating or admin_origin:
        if path.startswith('/api/v1/chat/'):
            origin = 'Required for visitor-session creation and cross-origin browser chat; must match the configured and credential-bound website origin. Same-origin chat HTTP reads/writes may infer PUBLIC_BASE_URL from Sec-Fetch-Site: same-origin. Native customer tokens and project-key customer exchanges must omit Origin. Administrator/account cookies require an explicit PUBLIC_BASE_URL Origin on mutations.'
        elif not mutating:
            origin = 'Optional on this administrator read. If present, it must equal the installation PUBLIC_BASE_URL.'
        else:
            origin = 'Required with cookie authentication on mutations and must equal PUBLIC_BASE_URL. If sent on any non-chat mutation, it must equal PUBLIC_BASE_URL, even with a bearer credential.'
        op.setdefault('parameters', []).append(_header('Origin', origin))
    names = {p.get('name') for p in op.get('parameters', [])}
    if 'limit' in names and 'offset' in names:
        details.append('Offset pagination returns items, limit, offset and has_more. Keep filters unchanged and advance offset by the number of returned items while has_more is true. Concurrent inserts or edits can move rows between pages; this is not a snapshot cursor.')
    if 'after' in names and path.endswith('/messages'):
        details.append('Returns messages in increasing sequence after the supplied value. Continue with next_sequence while has_more is true. Sequence numbers belong to one conversation; deduplicate by message ID when reconnecting.')
    for parameter in op.get('parameters', []):
        name = parameter.get('name')
        if name == 'project' and parameter.get('in') == 'path':
            parameter['description'] = 'Project UUID or its unique identifier, such as my-app.'
        elif name == 'board' and parameter.get('in') == 'path':
            parameter['description'] = 'Board UUID, returned when listing or creating boards. A board identifier alone is not a global reference.'
        elif name == 'limit':
            parameter['description'] = 'Maximum number of items to return. Use the documented default and bounds below.'
        elif name == 'offset':
            parameter['description'] = 'Number of matching items to skip; offset pagination does not freeze the result set.'
        elif name == 'external_user_id':
            parameter['description'] = 'Exact opaque ID verified by your backend, with a participate project credential. Preserve case, whitespace and Unicode; the ID alone grants no access.'
    if '/app-data/' in path:
        op['responses'].update({str(code): _error(message) for code, message in {
            502: 'The connected app failed or returned an invalid protocol response',
            503: 'The connected app or local read capacity is busy, or storage is unavailable',
            504: 'The connected app did not respond before the bounded timeout',
        }.items()})
        if path.endswith('/query') and not path.endswith('/related/query'):
            op['responses']['499'] = _error('The client disconnected before the read completed')
        if any(path.endswith('/' + kind + '/query') for kind in ('users', 'activity')):
            details.append('Start without continuation. For the next page, keep version and query unchanged, leave query.cursor unset, and send the returned outer continuation. Do not forward data.next_cursor yourself. An expired or changed browse session returns 409; restart from the first page. User rows sort by joined_at then exact ID bytes, descending; activity rows sort by occurred_at then exact ID bytes, descending.')
        if path.endswith('/related/query'):
            details.append('Reads local support records only. The connection must be enabled at the supplied version, and support_linked must be enabled for results. user_id must exactly equal the existing project external ID. limit and offset apply independently to the requests and conversations arrays; has_more is true if either has another page. This operation never creates or links an identity.')
        if path.endswith('/connection/test'):
            details.append('Fetches the remote manifest without enabling the connection. The signed review receipt expires after five minutes and is bound to the administrator session, project and connection version.')
        if path.endswith('/connection/enable'):
            details.append('Send a fresh test receipt, the same connection version, and only reviewed link_origins from its manifest. support_linked explicitly permits exact-ID lookups of existing local support records. One remote app identity can bind to only one project.')
        if path.endswith('/connection') and method == 'put':
            details.append('Use version=0 for an unconfigured connection. Saving disables the connection until it is tested and enabled again. Changing the URL requires a new app-issued token. Saved tokens are encrypted and never returned.')
        if path.endswith('/connection/remove'):
            details.append('Removes the URL and encrypted token, but preserves the established app identity binding. It does not allow this project to be silently rebound to another app.')
    if path.startswith('/api/v1/ai/') or '/ai' in path:
        op['responses']['502'] = _error('The AI provider returned an invalid model catalogue or decision response')
    if path.endswith('/messages') and method == 'post':
        details.append('Generate client_message_id once for each new message and reuse it on retries. An identical retry returns the committed message; reusing the ID with changed content returns 409. Customer idempotence survives chat-token renewal. Optional follow_up_email is private, unverified and saved separately; it is excluded from AI context, events and notifications.')
    if path == '/api/v1/chat/conversations' and method == 'post':
        details.append('Generate client_conversation_id once and reuse it if the response is lost. The customer owns the returned conversation across renewed credentials.')
    if path.endswith('/support-mode'):
        details.append('Use the current mode_version, which is separate from conversation version. Customers can choose ai or waiting_human; only administrators can select human. Current AI readiness is rechecked before AI resumes.')
    if path.endswith('/read') and path.startswith('/api/v1/chat/'):
        details.append('Read positions only advance and are bounded to committed messages. Each administrator has a separate conversation read position; customer positions survive credential renewal. This does not change the shared notification inbox.')
    if path.endswith('/contact/remove'):
        details.append('Supply the conversation contact_version as version. Removes the current optional contact, without changing messages or deleting historical backups.')
    if path == '/api/v1/chat/tickets':
        details.append('The ct_ ticket expires after 60 seconds and can be used once. Connect to /api/v1/chat/ws and send it in the first JSON frame within five seconds. Never put the ticket or a chat token in the URL. The WebSocket contract is documented in x-websockets.')
    if path.endswith('/vote') and method == 'put':
        details.append('Only accepted requests can be voted on. PUT sets present=true or present=false; it is not a toggle. Repeating the same value does not add votes or duplicate effects.')
    if path == '/api/v1/requests/{request_id}' and method in ('patch', 'delete'):
        details.append('Creator changes are allowed only before the request has ever been accepted. Acceptance remains permanent even if moderation changes later. Editing a rejected request resubmits it for moderation.')
    if '/business-hours' in path:
        details.append('Business hours describe expected availability, not live presence or a promised reply time. They never prevent sending a message. Project overrides take precedence over the shared schedule.')
    if path.endswith('/webhook') and method == 'put':
        details.append('The events enum is the subscription allowlist. chat.support_mode_changed exists as an internal event but cannot currently be selected through these v1 settings. Existing queued jobs retain their saved destination, payload and signing secret after settings change.')
    if path == '/api/v1/projects/{project}/ai/keys/{key_id}' and method == 'delete':
        details.append('Deletes the shared credential and its secret, affecting every bound project. To detach it only from this project, use PUT /api/v1/projects/{project}/ai/keys/selection with the remaining key IDs instead.')
    if path == '/api/v1/projects/{project}/ai' and method == 'put':
        details.append('A provider change detaches the old project selections; it does not delete credentials from the installation library. Add or select keys for the new provider explicitly.')
    op['description'] = '\n\n'.join(part for part in details if part).replace('up to99', 'up to 99')


def _object(properties):
    return {'type': 'object', 'required': list(properties), 'properties': properties}


def _response_shapes(spec):
    schemas = spec['components']['schemas']
    schemas['HealthOutput'] = _object({'ok': {'type': 'boolean'}})
    nullable = {'type': ['string', 'null']}
    schemas['ConnectedConnectionOutput'] = _object({
        'project_id': {'type': 'string'}, 'project_name': {'type': 'string'},
        'version': {'type': 'integer', 'minimum': 0, 'description': 'Optimistic concurrency version; zero before configuration.'},
        'connection_id': deepcopy(nullable), 'base_url': deepcopy(nullable),
        'has_token': {'type': 'boolean'}, 'enabled': {'type': 'boolean'},
        'app_instance_id': deepcopy(nullable), 'link_origins': {'type': 'array', 'items': {'type': 'string'}},
        'support_linked': {'type': 'boolean'},
    })
    schemas['ConnectedConnectionListOutput'] = _object({'items': {'type': 'array', 'items': {'$ref': '#/components/schemas/ConnectedConnectionOutput'}}})
    schemas['ConnectedPreferencesOutput'] = _object({'version': {'type': 'integer', 'minimum': 0}, 'timezone': {'type': 'string', 'description': 'IANA display timezone, such as America/Chicago.'}})
    from feedback.connected_apps_protocol import Manifest
    manifest = Manifest.model_json_schema(ref_template='#/components/schemas/{model}')
    for name, definition in manifest.pop('$defs', {}).items():
        schemas.setdefault(name, definition)
    schemas['ConnectedManifest'] = manifest
    schemas['ConnectedTestOutput'] = _object({'manifest': {'$ref': '#/components/schemas/ConnectedManifest'}, 'receipt': {'type': 'string', 'description': 'Opaque review receipt; expires in five minutes. Never edit it.'}})
    brief = {'id': {'type': 'string'}, 'created_at': {'type': 'string'}}
    schemas['ConnectedRelatedOutput'] = _object({
        'linked': {'type': 'boolean'},
        'requests': {'type': 'array', 'items': _object({**brief, 'title': {'type': 'string'}})},
        'conversations': {'type': 'array', 'items': _object(deepcopy(brief))},
        'has_more': {'type': 'boolean'},
    })
    for path, methods in spec['paths'].items():
        name = None
        if path == '/api/v1/admin/connected-apps': name = 'ConnectedConnectionListOutput'
        elif path == '/api/v1/admin/connected-apps/preferences': name = 'ConnectedPreferencesOutput'
        elif '/app-data/connection' in path: name = 'ConnectedTestOutput' if path.endswith('/test') else 'ConnectedConnectionOutput'
        elif path.endswith('/app-data/related/query'): name = 'ConnectedRelatedOutput'
        if name:
            for op in methods.values():
                op['responses']['200']['content']['application/json']['schema'] = {'$ref': '#/components/schemas/' + name}



def _field_descriptions(spec):
    customer = spec['components']['schemas']['CustomerSessionInput']['properties']
    customer['origin']['description'] = 'Required with client_type=browser and omitted with client_type=native. Must be an allowed website origin; local account sessions may also use PUBLIC_BASE_URL. This JSON field is distinct from the request Origin header.'
    customer['display_name']['description'] = 'Optional only for project-key exchanges. Local account sessions use the account display name and must omit this field.'
    descriptions = {
        'version': 'Version of this resource or configuration. On writes, send the current value from its latest read; a stale value returns 409.',
        'external_user_id': 'Exact project-scoped ID verified by a trusted backend. Preserve case, whitespace and Unicode. It is not a credential and never links accounts across projects.',
        'client_message_id': 'Generate one UUID per new message and reuse it on retries, including after credential renewal. Different content with the same ID returns 409.',
        'client_conversation_id': 'Generate one UUID per new conversation and reuse it if the response is lost.',
        'mode_version': 'Current support-mode version; independent of the conversation version. A stale value returns 409.',
        'contact_version': 'Current version of the separately stored follow-up contact. Use this value when clearing the contact.',
        'key_version': 'Current version of this AI credential. Refresh the key after a 409 conflict.',
        'library_version': 'Current version of the installation AI credential library; required alongside the project settings version for binding changes.',
        'accepted_at': 'Time of first acceptance. Never cleared or replaced; once set, creator editing and deletion stay locked.',
    }
    for schema in spec['components']['schemas'].values():
        for name, definition in schema.get('properties', {}).items():
            if name in descriptions and not definition.get('description'):
                definition['description'] = descriptions[name]
    schemas = spec['components']['schemas']
    schemas['WebhookInput']['properties']['events']['description'] = 'Explicit subscription allowlist. chat.support_mode_changed is an internal event but is not currently accepted by this v1 settings endpoint.'
    for name in ('UserQuery', 'ActivityQuery'):
        schemas[name]['properties']['cursor']['description'] = 'Leave unset when calling SupaKnock. Send the outer continuation from the previous response instead; raw upstream cursors are rejected.'
    for name in ('UserRead', 'ActivityRead'):
        schemas[name]['properties']['continuation']['description'] = 'Opaque outer continuation returned by the previous SupaKnock response. Keep the query and connection version unchanged; expired or changed sessions return 409.'


def _request_webhooks(spec):
    schemas = spec['components']['schemas']
    text = {'type': 'string'}
    nullable = {'type': ['string', 'null']}
    resource = _object({'id': deepcopy(text), 'identifier': deepcopy(text)})
    kinds = ['request.created', 'request.updated', 'request.resubmitted', 'request.accepted', 'request.rejected', 'request.status_changed', 'request.deleted']
    schemas['RequestWebhookEvent'] = _object({
        'id': {'type': 'string', 'format': 'uuid'}, 'type': {'type': 'string', 'enum': kinds},
        'schema_version': {'const': 1}, 'occurred_at': {'type': 'string', 'format': 'date-time'},
        'project': deepcopy(resource), 'board': deepcopy(resource), 'request_id': deepcopy(text), 'url': deepcopy(text),
        'request': _object({
            'id': deepcopy(text), 'title': deepcopy(text), 'description': deepcopy(text),
            'moderation': {'enum': ['pending', 'accepted', 'rejected']},
            'status': {'enum': ['under_consideration', 'planned', 'in_progress', 'shipped']},
            'accepted_at': deepcopy(nullable), 'rejection_reason': deepcopy(nullable), 'version': {'type': 'integer', 'minimum': 1},
        }),
        'previous': {'type': ['object', 'null'], 'additionalProperties': True, 'description': 'Changed fields from the previous request state, or null for a new request. The field set depends on the event type.'},
        'actor': _object({'user_id': deepcopy(nullable), 'api_key_id': deepcopy(nullable), 'kind': {'enum': ['native', 'integration']}}),
        'creator': _object({'user_id': deepcopy(text), 'external_user_id': deepcopy(nullable)}),
    })
    summaries = {
        'request.created': 'A request was submitted for moderation',
        'request.updated': 'Request content was edited',
        'request.resubmitted': 'A rejected request was edited and resubmitted',
        'request.accepted': 'A request was accepted',
        'request.rejected': 'A request was rejected',
        'request.status_changed': 'A request changed lifecycle status',
        'request.deleted': 'A creator deleted a request before acceptance',
    }
    contract = 'Verify X-Feedback-Signature as v1= followed by the hexadecimal HMAC-SHA256 of X-Feedback-Timestamp + "." + the exact raw UTF-8 request body, using the webhook signing secret. X-Feedback-Timestamp is Unix seconds. Deduplicate by X-Feedback-Event-ID. Delivery is at least once and ordering is not guaranteed. Any 2xx response acknowledges delivery. Redirects are not followed; network failures, 408, 425, 429 and 5xx retry with bounded backoff (eight attempts including the first). Existing jobs retain their destination, payload and signing-secret snapshot. Manual retry uses that same saved event.'
    for kind in kinds:
        spec['webhooks'][kind] = {'post': {
            'summary': summaries[kind],
            'description': 'Opt in through project webhook settings. This private event contains the creator identity and a committed request snapshot; do not serve it publicly. Business changes, immutable events and subscribed delivery jobs commit together. ' + contract,
            'requestBody': {'required': True, 'content': {'application/json': {'schema': {'allOf': [{'$ref': '#/components/schemas/RequestWebhookEvent'}, {'type': 'object', 'properties': {'type': {'const': kind}}}]}}}},
            'responses': {'200': {'description': 'Accepted or already processed'}, '204': {'description': 'Accepted or already processed'}},
        }}
    schemas['WebhookTestEvent'] = _object({
        'id': {'type': 'string', 'format': 'uuid'}, 'type': {'const': 'webhook.test'},
        'schema_version': {'const': 1}, 'occurred_at': {'type': 'string', 'format': 'date-time'},
        'project': deepcopy(resource), 'actor': deepcopy(schemas['RequestWebhookEvent']['properties']['actor']),
        'test': {'const': True},
    })
    spec['webhooks']['webhook.test'] = {'post': {
        'summary': 'An administrator or manager requested a test delivery',
        'description': 'Sent only by POST /api/v1/projects/{project}/webhook/test, to the configured destination. It is not a subscription event. ' + contract,
        'requestBody': {'required': True, 'content': {'application/json': {'schema': {'$ref': '#/components/schemas/WebhookTestEvent'}}}},
        'responses': {'200': {'description': 'Accepted or already processed'}, '204': {'description': 'Accepted or already processed'}},
    }}
    for kind, item in spec['webhooks'].items():
        op = item['post']
        op['x-subscribable'] = kind not in ('chat.support_mode_changed', 'webhook.test')
        op['parameters'] = [
            _header('X-Feedback-Event-ID', 'Stable event UUID; deduplicate deliveries by this ID.', True),
            _header('X-Feedback-Timestamp', 'Unix seconds used to sign this delivery attempt.', True),
            _header('X-Feedback-Signature', 'v1= followed by the lowercase hexadecimal HMAC-SHA256 signature.', True),
        ]
        if kind == 'chat.message_created':
            op['description'] += ' ' + contract
        elif kind == 'chat.support_mode_changed':
            op['description'] = 'Internal event schema for a committed support-mode transition. This event is not currently selectable in the v1 webhook settings events allowlist, so new subscriptions cannot be configured through this API. Schema version 2 excludes message bodies, customer identity, credentials and IP metadata. mode_version identifies the transition order.'


def _websocket(spec):
    schemas = spec['components']['schemas']
    def frame(kind, **properties):
        return _object({'type': {'const': kind}, **properties})
    schemas['ChatSocketHello'] = {
        'type': 'object', 'required': ['ticket'], 'properties': {
            'ticket': {'type': 'string', 'description': 'Single-use ct_ ticket from POST /api/v1/chat/tickets.'},
            'after': {'type': 'integer', 'minimum': 0, 'maximum': 2147483647, 'default': 0, 'description': 'Highest committed message sequence already received for this conversation.'},
        },
    }
    schemas['ChatSocketClientFrame'] = {'oneOf': [
        frame('message', message={'type':'object','required':['client_message_id','body'],'properties':{'client_message_id': {'type': 'string', 'format': 'uuid'}, 'body': {'type': 'string', 'minLength': 1, 'maxLength': 4000},'context':{'anyOf':[{'$ref':'#/components/schemas/MessageContext'},{'type':'null'}],'description':'Optional immutable episode snapshot. Retry with the same context; it grants no access rights.'}}}),
        frame('read', sequence={'type': 'integer', 'minimum': 0, 'maximum': 2147483647}),
        frame('typing'), frame('pong'), frame('ping'),
    ]}
    schemas['ChatSocketServerFrame'] = {'oneOf': [
        frame('ready', conversation_id={'type': 'string', 'format': 'uuid'}),
        frame('state', conversation={'$ref': '#/components/schemas/ConversationOutput'}),
        frame('message', message={'$ref': '#/components/schemas/MessageOutput'}),
        frame('ack', message={'$ref': '#/components/schemas/MessageOutput'}),
        frame('typing', sender={'enum': ['visitor', 'admin']}, sender_role={'enum': ['customer', 'admin']}),
        frame('ping'), frame('error', error=deepcopy(schemas['ApiError']['properties']['error'])),
    ]}
    spec['x-websockets'] = {'/api/v1/chat/ws': {
        'summary': 'Resume live messages for one authorized conversation',
        'description': 'Use wss with an HTTPS installation. Obtain a fresh ticket over authenticated HTTP, open the socket without URL query parameters, and send the hello frame within five seconds. Browser credentials require their bound Origin; native customer credentials omit Origin. Authentication comes from the ticket, not cookies or URL credentials. After ready, state describes the current conversation and message frames replay committed messages after your cursor. On reconnect, obtain another ticket and send your highest received sequence. Deduplicate message and ack frames by message ID. Authorization and ownership are rechecked throughout the connection. HTTP remains available for all durable operations. Save optional follow_up_email through HTTP message creation; WebSocket message frames do not save contact details.',
        'ticketOperation': 'POST /api/v1/chat/tickets',
        'hello': {'$ref': '#/components/schemas/ChatSocketHello'},
        'clientFrames': {'$ref': '#/components/schemas/ChatSocketClientFrame'},
        'serverFrames': {'$ref': '#/components/schemas/ChatSocketServerFrame'},
        'limits': {'ticketLifetimeSeconds': 60, 'helloTimeoutSeconds': 5, 'maxFrameBytes': 32768, 'maxFramesPerMinute': 120, 'maxConnectionsPerSession': 8, 'maxConnectionsPerInstallation': 128, 'typingIntervalSeconds': 2, 'serverPingIntervalSeconds': 20},
        'closeCodes': {'1013': 'Installation connection capacity reached; try again later.', '4403': 'Rejected before acceptance: URL query parameters or an invalid Origin.', '4401': 'Credentials expired, were revoked, or no longer authorize this conversation.', '4400': 'Invalid input, frame limit, per-session connection limit or another protocol error.'},
    }}


def install_openapi(app):
    def generate():
        if app.openapi_schema:
            return app.openapi_schema
        spec = get_openapi(title=app.title, version=app.version, description=app.description, routes=app.routes)
        schemas = spec['components']['schemas']
        schemas['ApiError'] = _object({'error': {
            'type': 'object', 'required': ['code', 'message'], 'properties': {
                'code': {'type': 'string', 'description': 'Stable machine-readable error code.'},
                'message': {'type': 'string', 'description': 'Safe explanation for the caller; never a raw internal exception.'},
                'fields': {'type': 'array', 'description': 'Optional validation issues; rejected values are not echoed.', 'items': _object({'field': {'type': 'string'}, 'message': {'type': 'string'}})},
            }}})
        spec['info']['description'] = ('SupaKnock installation API for feedback, chat and administration. The same routes serve the web app, iPhone app and trusted backend integrations. Public feedback reads need no credentials; private data is filtered by the acting account and project. Cookie mutations require CSRF and the installation Origin. Administrator bearer sessions do not require CSRF. Project keys belong only on trusted servers. Customer and visitor chat credentials use X-Chat-Token and never grant administration.\n\nThe API contract is the same with SQLite, PostgreSQL and Supabase PostgreSQL. Configure the database on the server; clients never connect to installation tables. See /api-docs for integration examples. WebSocket chat is described separately in x-websockets because OpenAPI paths describe HTTP operations.')
        spec['info']['license'] = {'name': 'MIT', 'identifier': 'MIT'}
        spec['servers'] = [{'url': '/', 'description': 'This SupaKnock installation; use its PUBLIC_BASE_URL as your client base URL.'}]
        spec['components']['securitySchemes'] = {
            'APIKeyCookie': {'type': 'apiKey', 'in': 'cookie', 'name': 'feedback_session', 'description': 'Local account browser session. The account role still controls access: participant cookies do not satisfy administrator operations. Mutations also require the exact installation Origin and X-CSRF-Token.'},
            'NativeBearer': {'type': 'http', 'scheme': 'bearer', 'bearerFormat': 'ns_…', 'description': 'Administrator session issued by POST /api/v1/auth/native/login. Send Authorization: Bearer ns_…. Do not combine with a session cookie or chat token; no CSRF token is needed.'},
            'ProjectBearer': {'type': 'http', 'scheme': 'bearer', 'bearerFormat': 'fb_…', 'description': 'Trusted backend project credential. Send Authorization: Bearer fb_…. Permissions and project scope apply per operation. Never embed this secret in browser or mobile code; external_user_id is not an authentication mechanism.'},
            'ChatVisitor': {'type': 'apiKey', 'in': 'header', 'name': 'X-Chat-Token', 'description': 'Secret cv_ visitor credential for one conversation and its exact website Origin. Never combine with cookies or Authorization.'},
            'ChatCustomer': {'type': 'apiKey', 'in': 'header', 'name': 'X-Chat-Token', 'description': 'One-hour cc_ credential for a durable customer and project. Browser credentials require their bound Origin; native credentials omit Origin. Never combine with cookies or Authorization.'},
        }
        for path, methods in spec['paths'].items():
            for method, op in methods.items():
                _describe_operation(path, method, op)
        _response_shapes(spec)
        session_content = spec['paths']['/api/v1/chat/projects/{project}/customer-sessions']['post']['requestBody']['content']['application/json']
        session_content['examples'] = {
            'externalBrowser': {'summary':'Trusted backend verifies the signed-in website user', 'value':{'external_user_id':'app-user-123','client_type':'browser','origin':'https://app.example.com','display_name':'Jordan'}},
            'externalNative': {'summary':'Trusted backend gives its native client customer access without a browser Origin', 'value':{'external_user_id':'app-user-123','client_type':'native','display_name':'Jordan'}},
            'nativeAccount': {'summary':'SupaKnock participant exchanges its cookie session and CSRF token', 'value':{'client_type':'browser','origin':'https://box.example.com'}}
        }
        spec['components']['schemas']['ChatMessageCreatedEvent'] = {
            'type':'object', 'required':['id','type','schema_version','occurred_at','project','conversation','message','customer','actor'],
            'properties':{
                'id':{'type':'string','format':'uuid'}, 'type':{'const':'chat.message_created'}, 'schema_version':{'const':1},
                'occurred_at':{'type':'string','format':'date-time'},
                'project':{'type':'object','required':['id','identifier'],'properties':{'id':{'type':'string'},'identifier':{'type':'string'}}},
                'conversation':{'type':'object','required':['id'],'properties':{'id':{'type':'string','format':'uuid'}}},
                'message':{'type':'object','required':['id','sequence','sender_role'],'properties':{'id':{'type':'string','format':'uuid'},'sequence':{'type':'integer','minimum':1},'sender_role':{'enum':['customer','admin']}}},
                'customer':{'$ref':'#/components/schemas/CustomerOutput'},
                'actor':{'type':'object','required':['kind','user_id'],'properties':{'kind':{'enum':['customer','admin']},'user_id':{'type':['string','null']}}}
            }
        }
        schemas = spec['components']['schemas']
        schemas['ChatHumanMessageCreatedEvent'] = deepcopy(schemas['ChatMessageCreatedEvent'])
        ai_event = deepcopy(schemas['ChatMessageCreatedEvent'])
        ai_event['required'].remove('customer')
        ai_event['properties'].pop('customer')
        ai_event['properties']['schema_version'] = {'const': 2}
        ai_event['properties']['message']['properties']['sender_role'] = {'const': 'ai'}
        ai_event['properties']['actor']['properties'] = {'kind': {'const': 'ai'}, 'user_id': {'type': 'null'}}
        schemas['ChatAIMessageCreatedEvent'] = ai_event
        schemas['ChatMessageCreatedEvent'] = {'oneOf': [
            {'$ref': '#/components/schemas/ChatHumanMessageCreatedEvent'},
            {'$ref': '#/components/schemas/ChatAIMessageCreatedEvent'}]}
        schemas['ChatSupportModeChangedEvent'] = {
            'type': 'object', 'required': ['id', 'type', 'schema_version', 'occurred_at', 'project', 'conversation', 'previous_mode', 'reason', 'actor'],
            'properties': {
                'id': {'type': 'string', 'format': 'uuid'}, 'type': {'const': 'chat.support_mode_changed'}, 'schema_version': {'const': 2},
                'occurred_at': {'type': 'string', 'format': 'date-time'}, 'project': deepcopy(ai_event['properties']['project']),
                'conversation': {'type': 'object', 'required': ['id', 'support_mode', 'mode_version'], 'properties': {
                    'id': {'type': 'string', 'format': 'uuid'}, 'support_mode': {'enum': ['ai', 'waiting_human', 'human']}, 'mode_version': {'type': 'integer', 'minimum': 1}}},
                'previous_mode': {'enum': ['ai', 'waiting_human', 'human']}, 'reason': {'type': 'string'},
                'actor': {'type': 'object', 'required': ['kind', 'user_id'], 'properties': {
                    'kind': {'enum': ['admin', 'customer', 'system', 'ai']}, 'user_id': {'type': ['string', 'null']}}}}}
        spec['webhooks'] = {'chat.message_created': {'post': {
            'summary':'A customer, administrator or AI message was committed',
            'description':'Opt in through project webhook settings. Signed raw JSON using X-Feedback-Timestamp, X-Feedback-Signature and X-Feedback-Event-ID. At least once; delivery ordering is not guaranteed. Customer/admin events retain schema_version 1 and customer identity. AI events use schema_version 2, sender_role ai and no customer identity. Filter sender_role=admin for human replies; AI messages never count as human response time. Bodies and citations remain behind authenticated chat APIs. Deduplicate the stable event ID and use conversation sequence for history. See docs/chat.md and docs/webhooks.md.',
            'requestBody':{'required':True,'content':{'application/json':{'schema':{'$ref':'#/components/schemas/ChatMessageCreatedEvent'},'example':{
                'id':'d4bfa680-c7e5-4479-b4cd-e1d6a10c066e','type':'chat.message_created','schema_version':1,'occurred_at':'2026-10-03T01:00:00.000Z',
                'project':{'id':'9dc2e44e-3c3a-4d69-a55b-e57c36a839bc','identifier':'my-app'},
                'conversation':{'id':'504f92da-bbaa-4d31-b0c1-631d315a01cb'},
                'message':{'id':'d4bfa680-c7e5-4479-b4cd-e1d6a10c066e','sequence':2,'sender_role':'admin'},
                'customer':{'id':'e7e792e7-d8c1-451c-bc85-6f8a46e5d77b','kind':'external','display_name':'Jordan','user_id':'2ee560d1-1b14-4472-b5d7-c3f07edec7b3','external_user_id':'app-user-123'},
                'actor':{'kind':'admin','user_id':'9b6ad65e-93c4-4ebe-b0e5-1f3c6fa743cc'}
            }}}},
            'responses':{'200':{'description':'Accepted or already processed'},'204':{'description':'Accepted or already processed'}}
        }}}
        spec['webhooks']['chat.support_mode_changed'] = {'post': {
            'summary': 'A conversation explicitly changed between AI and human support',
            'description': 'Opt-in signed at-least-once webhook using the existing raw-body signature contract. Schema version 2 carries no message body, customer identity, credentials or IP metadata. Deduplicate the event ID; use mode_version to recognize stale transitions and authorized chat APIs to read current state.',
            'requestBody': {'required': True, 'content': {'application/json': {'schema': {'$ref': '#/components/schemas/ChatSupportModeChangedEvent'}}}},
            'responses': {'200': {'description': 'Accepted or already processed'}, '204': {'description': 'Accepted or already processed'}}}}
        _field_descriptions(spec)
        _request_webhooks(spec)
        _websocket(spec)
        app.openapi_schema = spec
        return spec
    app.openapi = generate
