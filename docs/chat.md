# Customer and website chat

Public read-only MCP tools and per-message podcast context are implemented as an optional extension. See [Connected AI tools and episode chat](mcp-tools.md) for setup, compatibility, source snapshots and widget examples. Customer-specific MCP grants remain deferred.

Chat lives on your SupaKnock installation. Your configured SQLite, PostgreSQL or Supabase database stores durable customers, conversations, messages, session hashes and unread cursors. Customers can be anonymous website visitors, users authenticated by your backend, or SupaKnock account holders. The optional push relay only delivers generic admin alerts; chat works while push is disabled or unavailable.

## Add the widget

1. Sign in as an installation admin, open the project's settings, and choose **Website chat**.
2. Enable chat, add exact website origins, such as `https://www.example.com`, and save. Origins have no trailing slash or path. HTTP is accepted only for localhost development.
3. Copy that project's installation snippet and paste it into your website:

```html
<div id="suggestion-box-chat"></div>
<script defer src="https://box.example.com/widget.js"
  data-server="https://box.example.com" data-project="your-project"
  data-target="#suggestion-box-chat"></script>
```

Each project has its own enabled setting, allowed origins and installation snippet. Changing one project's settings does not change another's. The **Website chat** tab also shows the project's measured response time when enough conversation history is available.

The widget mounts inside the div using Shadow DOM. Its default presentation is a floating bubble; an optional inline launcher stays in the page at that div and opens an overlay. It has keyboard labels, Escape-to-close, plain-text rendering, small-screen sizing, loading/error feedback and a disabled submit button while sending. No API credential belongs in the snippet. Press Enter in the message box to send, or Shift+Enter to add a new line. Confirming text with an input method does not send a message.

### Match your website

Keep appearance in your website's embed code. It is not saved to SupaKnock. Chat enablement and allowed origins still belong in the project's **Website chat** settings; conversations and messages are stored normally.

For the automatic embed, put this JSON before the widget script. `data-config` is the JSON element's **ID**, without `#`:

```html
<div id="suggestion-box-chat"></div>
<script type="application/json" id="suggestion-box-appearance">
{
  "launcherText": "Talk to the Glean team",
  "title": "Glean support",
  "theme": {
    "mode": "auto",
    "light": { "accent": "#37306B", "onAccent": "#FFFFFF" },
    "dark": { "accent": "#E7E1FF", "onAccent": "#221C43" }
  }
}
</script>
<script defer src="https://box.example.com/widget.js"
  data-server="https://box.example.com" data-project="your-project"
  data-target="#suggestion-box-chat"
  data-config="suggestion-box-appearance"></script>
```

`launcherText` changes the button label (up to 80 characters); `title` changes the panel heading (up to 120). Both render as plain text. Omit them to keep the layout's default launcher label and the project's name. The JSON contains appearance only: `data-server`, `data-project`, and `data-target` select the installation, project, and mount element.

`theme.mode` accepts `auto` (the default, following the operating system), `light`, or `dark`. Each palette can override `accent`, `onAccent`, `background`, `surface`, `text`, `muted`, and `border`. Use `#RGB` or `#RRGGBB` colors; omitted or invalid values use the built-in defaults. Choose readable foreground/background pairs, especially `onAccent` on `accent`.

If your website has its own light/dark toggle, update the mounted widget when that toggle changes:

```javascript
// Run after widget.js has loaded; pass your website's selected mode.
SuggestionBoxChat.get('#suggestion-box-chat').setTheme('dark');
// 'light' selects light mode; 'auto' resumes following the operating system.
```

This updates the appearance without resetting the conversation or draft. The panel uses a gentle opening/closing animation and respects the visitor's reduced-motion preference automatically.

Alternatively, load `widget.js` without any `data-*` attributes and mount it from your JavaScript after the script and target element are ready:

```javascript
const chat = SuggestionBoxChat.mount('#suggestion-box-chat', {
  server: 'https://box.example.com',
  project: 'your-project',
  launcherText: 'Talk to the Glean team',
  theme: {
    mode: 'auto',
    light: { accent: '#37306B', onAccent: '#FFFFFF' },
    dark: { accent: '#E7E1FF', onAccent: '#221C43' },
  },
});
// In your website's theme-change handler:
chat.setTheme('light');
```

Choose **automatic embed or manual mount**, so the same target is mounted once.

### Choose a layout and scroll reveal

The same `data-config` JSON and manual `mount()` options accept:

| Option | Default | Behavior |
| --- | --- | --- |
| `presentation` | `"bubble"` | `"bubble"`: floating bottom-right launcher. `"bar"`: centered floating bottom bar. `"inline"`: full-width bar at your target div, opening a floating conversation panel. Unknown values use `"bubble"`. |
| `revealAfterScroll` | `0` | For floating layouts, reveal after scrolling this many pixels down the document. Use a finite positive number; zero, omitted, or invalid values show immediately. Inline ignores this option. |

For a floating bar that slides up after 600 pixels, use this as your appearance JSON (alongside any existing title, label and theme):

```json
{
  "presentation": "bar",
  "revealAfterScroll": 600,
  "launcherText": "Ask us anything…"
}
```

For inline chat, place your target `<div id="suggestion-box-chat"></div>` exactly where the bar should appear—inside a help section, article or sidebar—and set `"presentation": "inline"`. Only the launcher follows the container's width. It stays in place when opened, so surrounding content and the page's scroll position do not shift. The conversation opens above the website: bottom-right on desktop and almost full-screen on mobile. Closing returns focus to the launcher and preserves the draft. Modern browsers use the top layer to prevent the overlay being clipped by the launcher's ancestors; browsers without Popover support use a fixed-position panel. Give the parent a suitable width; do not give the empty div a fixed zero height or hide it with CSS.

The floating bar uses a message-field appearance with a muted prompt and an inset accent-colored send arrow. The entire bar is an accessible **button** that opens and focuses the conversation's message field. It is not a second message input. Its default label is **Ask a question…**; the bubble keeps **Chat with us**. `launcherText` overrides either.

Scroll reveal happens once per page mount, respects an already-restored scroll position, and never steals focus. Once visible, the launcher stays visible when scrolling back up or closing chat. Hidden launchers are not keyboard-focusable. Scrolling inside a nested page container does not count: the threshold uses the main document's scroll position. Choose a reachable threshold for short pages; otherwise the floating launcher stays hidden. `SuggestionBoxChat.get('#suggestion-box-chat').open()` opens immediately even before the threshold, so your own support button can always open chat. Closing preserves the draft and conversation. Reduced-motion preferences disable the slide animation.

All layouts use the same chat session and support features. Changing presentation does not create a different visitor identity. Mount one widget per page; use `get(...).open()` for additional support buttons. Layout options are read at mount time, not changed by `setTheme()`.

### Sessions and website security

The script first fetches public configuration. A visitor session is created on their first deliberate **Send**, not on a page view or when they open the panel. Opening/typing does not alert admins. The visitor secret is stored in this website's localStorage, scoped by installation, project and website origin. It expires after 30 days and is never a cookie shared between sites. Reloading resumes that conversation until expiry/revocation. **End this chat** revokes access and clears the local reference. If storage is unavailable, the current page still works but cannot resume after reload. An expired session requires starting a new conversation; there is no unauthenticated history recovery or identity linking.

Treat the snippet as trusted code on your website; Shadow DOM isolates styles, not security. Website scripts can read the site's localStorage. Disclose chat storage/retention in your own privacy information and initialize the widget after any consent your site requires. Chats remain in your installation database and its backups; this release does not automatically delete conversations.

A website with CSP must allow the installation origin in `script-src` and `connect-src`, and its `wss://` origin in `connect-src`. The widget includes one static Shadow DOM stylesheet; a strict `style-src` (or `style-src-elem`) policy must permit its hash generated from your pinned widget version. Theme colors are validated hex values applied through the DOM style API; they do not add dynamic stylesheet text. Your theme-toggle or manual-mount code must also follow your website's existing script policy. Do not enable arbitrary script execution or broadly allow inline styles to accommodate chat. No iframe or third-party cookie permission is needed. Recalculate the stylesheet hash when upgrading the widget.

An anonymous API client follows the same flow. Keep the returned `cv_` credential private and use the returned conversation ID:

```sh
curl -X POST https://box.example.com/api/v1/chat/projects/your-project/sessions \
  -H 'Origin: https://www.example.com'
# Subsequent requests: X-Chat-Token: cv_… and the same Origin.
```

## Integrate authenticated customers

Enable chat for the project first. Your backend authenticates its own user, then exchanges a project API credential with `participate` permission and that verified user's external ID for a temporary `cc_` chat credential. External IDs are exact opaque strings, scoped to the project; case and spaces are preserved. The service automatically reuses or creates the existing feedback identity. No email, separate user provisioning, or SupaKnock login is required.

Never accept an arbitrary customer ID from a browser as proof of identity. Derive it from your authenticated application session. Keep the `fb_` project credential on your backend; give the browser or native client only the returned `cc_` credential. Choose the client type and website origin in your backend's configuration rather than forwarding arbitrary client choices.

For example, after authenticating a browser request, your backend can run:

```javascript
// Runs on your server, after authenticating the application's own session.
async function browserChatSession(authenticatedUser) {
  const response = await fetch(
    'https://box.example.com/api/v1/chat/projects/your-project/customer-sessions',
    {
      method: 'POST',
      headers: {
        Authorization: `Bearer ${process.env.SUGGESTION_BOX_API_KEY}`,
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({
        external_user_id: authenticatedUser.id, // Existing opaque string; do not normalize.
        client_type: 'browser',
        origin: 'https://www.example.com', // Exact enabled origin.
        display_name: authenticatedUser.displayName,
      }),
    },
  );
  if (!response.ok) throw new Error('Unable to establish customer chat');
  return response.json();
}
```

The 201 response is:

```json
{
  "token": "cc_…",
  "expires_at": "2026-10-02T13:00:00.000Z",
  "customer": {
    "id": "customer-uuid",
    "kind": "external",
    "display_name": "Alex",
    "user_id": "user-uuid",
    "external_user_id": "your-user-123"
  }
}
```

On the website, use that credential alone. `credentials: 'omit'` prevents a SupaKnock account cookie from conflicting with customer authentication. Browsers supply their actual Origin automatically:

```javascript
const chatToken = session.token; // Received from your authenticated backend.
const base = 'https://box.example.com/api/v1/chat';
const options = {
  credentials: 'omit',
  headers: { 'X-Chat-Token': chatToken, 'Content-Type': 'application/json' },
};
async function chatRequest(path, init = {}) {
  const response = await fetch(`${base}${path}`, { ...options, ...init });
  if (response.status === 401) throw new Error('Renew the customer credential');
  if (!response.ok) throw new Error(`Chat request failed (${response.status})`);
  return response.json();
}
const history = await chatRequest('/conversations');
const clientConversationId = crypto.randomUUID(); // Retain for retries.
const conversation = await chatRequest('/conversations', {
  method: 'POST',
  body: JSON.stringify({ client_conversation_id: clientConversationId }),
});
const clientMessageId = crypto.randomUUID(); // Retain until this send is acknowledged.
const sent = await chatRequest(`/conversations/${conversation.id}/messages`, {
  method: 'POST',
  body: JSON.stringify({ client_message_id: clientMessageId, body: 'Hello!' }),
});
```

Production clients should check every response and handle 401 by obtaining a new credential from their authenticated backend. Retry a timed-out send with the same client message ID and body. The supplied widget continues to use anonymous sessions; authenticated applications build their own customer UI against this API.

For a native app, the app authenticates with your backend as usual. Your backend makes the same exchange with `client_type: "native"` and **omits `origin`**. Return the `cc_` credential to the app, which sends it in `X-Chat-Token` without an Origin header. Native clients do not need a fabricated website origin. Use secure client storage and HTTPS; never include credentials in URLs or logs.

SupaKnock account holders can instead exchange their own cookie session plus CSRF protection using `{ "client_type": "browser", "origin": "https://box.example.com" }`, or `{ "client_type": "native" }`. Omit `external_user_id` and `display_name`; their existing account supplies their identity and label. The configured installation base URL is allowed for this browser flow. Existing `ns_` administrator sessions can also perform the exchange. The resulting credential always grants customer access only, even when issued from an admin account. Subsequent customer requests must omit the account cookie or bearer credential.

### Expiry, renewal and revocation

Customer credentials expire after one hour. Reuse the current credential for chat operations until renewal is needed; do not issue a new session for every message or API call. Obtain a replacement by repeating the authenticated backend/account exchange; a `cc_` credential cannot renew itself. Sessions for the same customer share conversation history, message retry identity and monotonic unread positions across devices. Expiry or revocation removes access without deleting that history. A new authenticated session restores access to the same project-scoped customer.

- `DELETE /api/v1/chat/session` with `X-Chat-Token` revokes just that credential and its tickets/sockets. Other device sessions remain valid.
- Your trusted backend can revoke every session for one customer with `POST /api/v1/chat/projects/{project}/customer-sessions/revoke` and `{ "external_user_id": "your-user-123" }`. This requires `participate` for that project. Native account callers omit the external ID and revoke their own sessions.
- Revoking an issuing project key invalidates its derived chat sessions. Logging out of a SupaKnock account session, changing the account password, or expiry of that parent session also invalidates its derived credentials.
- Logging out of **your integrating application** does not automatically notify SupaKnock. Have your client revoke its current credential, or have your backend call the all-sessions endpoint as appropriate to your logout policy. Remove locally stored chat credentials afterward.

Anonymous sessions remain private to their one original conversation. Expired or lost anonymous credentials cannot recover history through a new session. There is no guest linking or automatic merging by name, email, or external ID across projects.

## Administrator experience

**Live chat** (`/admin/chat`) remains the shared inbox for conversations across projects. Its **Widget setup** action opens the selected project's **Website chat** settings; conversation details also link to that conversation's project settings.

The Swift app and `/admin/chat` use normal native administrator authentication. One inbox includes anonymous and authenticated customers, their labels and identity kind, open/closed conversations, unread counts, saved history, replies, and close/reopen actions. Browser administration includes a **Refresh inbox** control; an open conversation polls new messages. The widget and Swift app receive live messages over WebSockets. Closing prevents new messages until an admin reopens the conversation. Repeated close/open state writes require the current version; conflicts return 409. Project credentials and ordinary participant accounts cannot access the admin inbox; participant customers can access only their own conversations through customer credentials.

Administrators also see the conversation's first connection IP and latest customer-message IP, plus each message's sender IP in message details. The first address stays fixed; only a newly committed customer message changes the latest address. An idempotent retry or administrator reply cannot overwrite it. Older records show **Not recorded**. These optional API fields are `initial_ip_address`, `last_customer_ip_address` and message `ip_address`; they are omitted from customer HTTP/WebSocket responses and all webhook/push payloads. See [trusted proxies and retention](operations.md#private-connection-metadata).

The first committed customer message atomically queues notifications for eligible devices with chat alerts enabled. Later customer messages can queue at most one additional alert per conversation per five minutes. Alerts carry installation/conversation/event identifiers and generic text, never message bodies. Native app authorization is still required after tapping an alert. APNs delivery and background socket activity are not guaranteed.

## HTTP contract

All routes are under `/api/v1`. Anonymous requests use `X-Chat-Token: cv_…` plus their original permitted `Origin`. Authenticated customer requests use `X-Chat-Token: cc_…`; browser credentials require their bound Origin, and native credentials require its absence. Native admins use their existing cookie/CSRF transport or `Authorization: Bearer ns_…`. Authentication transports cannot be combined. Public project identifiers and conversation UUIDs never authorize access by themselves.

| Method/path | Request / result |
|---|---|
| `GET /chat/projects/{project}/config` | Allowed-origin public project name, response-time label, AI availability and business-hours information |
| `POST /chat/projects/{project}/sessions` | No body; returns `{token,expires_at,conversation}`; no account credentials |
| `POST /chat/projects/{project}/customer-sessions` | Trusted backend/native account exchange described above; returns `{token,expires_at,customer}` |
| `POST /chat/projects/{project}/customer-sessions/revoke` | Same issuer authentication; `{external_user_id}` for backends, `{}` for native accounts; 204 |
| `GET /chat/conversations` | Own customer conversations; optional `state`, `limit` (1–100, default 25), `offset`; `{items,has_more,limit,offset}` |
| `POST /chat/conversations` | `cc_` only; `{client_conversation_id: UUID}`; idempotent 201 conversation |
| `GET /chat/conversations/{id}` | Current authorized conversation |
| `GET /chat/conversations/{id}/messages?after=0&limit=25` | `{items,has_more,next_sequence}`; limit 1–100 |
| `POST /chat/conversations/{id}/messages` | `{client_message_id: UUID,body,follow_up_email?}`; persisted message response; optional contact is stored separately |
| `POST /chat/conversations/{id}/read` | `{sequence}`; monotonic `{read_sequence}` result |
| `PUT /chat/conversations/{id}/support-mode` | `{mode,mode_version}`; current conversation; see AI additions below |
| `POST /chat/conversations/{id}/contact/remove` | `{version}` using the conversation's `contact_version`; owner or native admin; current conversation |
| `DELETE /chat/conversations/{id}/session` | Revoke visitor access; 204 |
| `DELETE /chat/session` | Revoke the current `cc_` credential; 204 |
| `POST /chat/tickets` | `{conversation_id}`; returns `{ticket,expires_at}` |
| `GET /admin/chat/projects/{project}` | Settings, version and response metric diagnostics |
| `PUT /admin/chat/projects/{project}` | `{enabled,origins,version}`; origins max 20; initial version 0 |
| `GET /admin/chat/conversations` | Standard page; optional `state`, `project`, `limit`, `offset` |
| `GET /admin/chat/conversations/{id}` | Administrator conversation view |
| `PATCH /admin/chat/conversations/{id}` | `{state:"open"|"closed",version}` |

Business-hours settings use separate installation/project routes documented in the [business-hours guide](chat-business-hours.md). The [generated OpenAPI](openapi.json) includes the full HTTP schemas. Administrator replies, history, read markers and socket tickets use the same `/chat/conversations/...` and `/chat/tickets` routes with administrator authentication.

Message shape: `{id,conversation_id,sequence,client_message_id,sender,sender_role,body,created_at}`. Legacy human/customer `sender` values remain `visitor` and `admin`; AI replies add `sender:"ai"` and `sender_role:"ai"`. Customer roles remain `customer` for every customer type. `citations` is an additive array of server-validated source labels, original excerpts and optional support URLs (empty for existing human/customer messages). Internal sender/session identifiers are omitted. Text length is 1–4,000 characters. Message IDs supplied by clients are UUIDs, scoped to the durable sender/conversation. Identical retries return the same stored message across session renewal and even after closing; conflicting reuse returns 409. Sequence numbers monotonically order committed messages. Conversation shape includes `id`, `project_id`, `project_identifier`, `project_name`, `state`, `version`, `created_at`, `updated_at`, `last_sequence`, `unread_count`, and `customer` (the object shown above). Anonymous customer user/external IDs are null; native external IDs are null. These details are private to authorized customers and admins.

Customer history includes empty conversations; the admin inbox excludes conversations without a customer message. Lists sort by newest activity then stable ID, so messages can move results between pages. Read positions belong to the customer rather than their credential and cannot move backward. Anonymous credentials list only their original conversation and cannot call the new-conversation endpoint.

CORS is limited to enabled origins and chat endpoints, permits only `Accept`, `Content-Type`, `X-Chat-Token` headers, and never permits browser credentials. Issuing project credentials are rejected when an Origin header is present: the exchange belongs on your backend. Each operation independently checks project/origin/session access. Existing non-chat CORS policy stays restricted to anonymous reads. Chat bodies are bounded to 32 KiB; normal request IP limits also apply. Durable caps allow 30 messages per sender/conversation/minute, 1,000 new conversations per project/UTC day, 20 new conversations per authenticated customer/day, and 100 active `cc_` credentials per customer. Expired credentials are pruned during issuance. Idempotent retries do not consume another conversation/message allowance. Rate-limit failures return 429; concealed conversations return 404, invalid/expired credentials 401, and forbidden operations 403.

## WebSocket protocol

Use `wss://box.example.com/api/v1/chat/ws` (`ws://localhost` for development). Do not put credentials in URLs. First obtain a single-use ticket from the authenticated HTTP API. Tickets expire after 60 seconds and bind the session, conversation and Origin (or its absence for native clients). Each ticket authorizes exactly one conversation; ownership and current session/parent validity are checked again when it is consumed. Within five seconds of opening a socket send a **text** JSON frame:

```json
{"ticket":"ct_…","after":0}
```

Server frames:

- `{"type":"ready","conversation_id":"…"}`: authenticated connection.
- `{"type":"state","conversation":{…}}`: initial state and close/reopen changes.
- `{"type":"message","message":{…}}`: durable messages after the sequence cursor.
- `{"type":"ack","message":{…}}`: committed response to a socket send. A later message frame may repeat it; deduplicate by message ID/sequence.
- `{"type":"typing","sender":"admin","sender_role":"admin"}`: ephemeral indicator; customer typing uses legacy `sender:"visitor"` and `sender_role:"customer"`. Expire locally after a few seconds.
- `{"type":"ping"}`: respond with `{"type":"pong"}`.
- `{"type":"error","error":{"code":"…","message":"…"}}`: bounded error followed by closure.

Optional client frames: `{"type":"message","message":{"client_message_id":"UUID","body":"Hello"}}`, `{"type":"read","sequence":12}`, `{"type":"typing"}`, and `pong`. Read frames do not return an acknowledgment; use the HTTP read endpoint when you need a confirmed cursor before disconnecting. HTTP sends/history remain available while reconnecting. Both transports share the same atomic storage operation. After reconnect, request a fresh ticket and send the last **contiguous** received sequence, then deduplicate any repeated message. If the customer credential expired, exchange it through your backend/account first. The supplied widget also catches up using HTTP.

The single-instance server checks the durable log and session validity at most one second apart, rechecks access before every client operation, and closes revoked/expired connections. Limits: 128 total connections including pending handshakes, 8 per authenticated session, 8 outstanding tickets/session, 32 KiB text frames, 120 client frames/minute, typing forwarded at most once/two seconds. Five-second send deadlines bound slow consumers. Built-in Uvicorn bounds input queues and uses 20-second ping/pong health checks. Keep one application instance and its local persistent volume, including when using PostgreSQL or Supabase.

Reverse proxies must preserve WebSocket upgrades, Origin, and a timeout above the heartbeat interval. Example Nginx location:

```nginx
location /api/v1/chat/ws {
    proxy_pass http://suggestion-box:8000;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_set_header Host $host;
    proxy_read_timeout 75s;
}
```

Use HTTPS/WSS on public deployments. Do not log ticket frames, visitor tokens or message bodies. The relay is never a socket proxy.

## Administrator reply webhooks

Subscribe the project's existing webhook destination to `chat.message_created`. Every newly committed customer or administrator message records one immutable event in the same transaction as the message and applicable delivery job. Retrying an already committed message does not create a second event. Message bodies remain available only through the authorized history API.

```json
{
  "id": "message-uuid",
  "type": "chat.message_created",
  "schema_version": 1,
  "occurred_at": "2026-10-02T12:00:00.000Z",
  "project": { "id": "project-uuid", "identifier": "your-project" },
  "conversation": { "id": "conversation-uuid" },
  "message": { "id": "message-uuid", "sequence": 2, "sender_role": "admin" },
  "customer": {
    "id": "customer-uuid",
    "kind": "external",
    "display_name": "Alex",
    "user_id": "user-uuid",
    "external_user_id": "your-user-123"
  },
  "actor": { "kind": "admin", "user_id": "admin-uuid" }
}
```

Verify the raw body and timestamp using the [webhook signing contract](webhooks.md#signing), then durably deduplicate by event ID. To notify your own users, filter for `message.sender_role == "admin"`, use `customer.external_user_id` within the event's project to identify your account, and queue your notification before returning 2xx. Anonymous events have a durable customer reference but no external account ID, so do not attempt account notifications by matching display names. The same event is delivered even when push is disabled.

Deliveries use the existing persistent queue, bounded retries, inspection and manual retry controls. Delivery is at least once and may arrive out of order. Use message IDs/sequence to deduplicate and order your view; fetch authorized history after your last contiguous sequence when needed. URL, body, event ID and encrypted signing configuration are snapshotted for retries. Later webhook configuration or secret changes affect new jobs; retain old verification secrets while existing deliveries finish. No message text, passwords, session credentials or API secrets appear in these events.

## Typical response time

View this project's response-time diagnostics in **Project settings → Website chat**. The value is calculated from conversation history, rather than entered manually.

The historical label is the median interval from each conversation's first visitor message to its first human admin reply, among conversations started in the last 30 days. It requires at least five answered conversations and at least one answered conversation among the five most recently started. Unanswered conversations are excluded from the median and shown in admin diagnostics; insufficient/stale data hides the public label. Later replies do not change the sample. Labels round to whole minutes, hours or days (under one minute is explicit). This is historical performance, not an availability promise or SLA.

For existing SQLite installations, migrations `005_chat.sql` and `006_chat_customers.sql` are additive. The customer migration backfills each existing anonymous conversation with its own customer, preserves visitor credentials, history and read positions, and retains message retry identity. No accounts are linked or merged. Back up before upgrading. The [backup/restore procedure](operations.md) for each supported database includes customer identities, history, unread state and session hashes. Restoration preserves still-valid chat access, subject to parent authentication and expiry; preserve application secrets for encrypted push/webhook data too. Changing database providers does not transfer existing conversations; see [database configuration](database-providers.md).


## Optional AI protocol additions

AI remains independently off per project. Public chat configuration adds `ai_available` only when AI is explicitly enabled and ready; it exposes no model, key or knowledge details. Existing conversations start in human mode. Conversation responses add `support_mode` (`ai`, `waiting_human`, `human`), an independent `mode_version`, and nullable `ai_status`. Open/closed lifecycle and its existing version remain separate.

Use `PUT /api/v1/chat/conversations/{conversation_id}/support-mode` with `{mode,mode_version}` and the existing owning chat credential or native administrator session. Customers can choose AI or request a human; only administrators can take over as human. AI selection requires current readiness. Direct human requests remain available without an enabled assistant handoff tool. Cross-origin widget PUT is allowed only on this route with the bound Origin and X-Chat-Token; it does not grant general cross-origin administrative writes.

While a turn runs, authorized conversation responses and WebSocket `state` frames include `ai_progress`: `thinking`, `reading`, `writing`, `checking` or `refining`. These are fixed activity labels, not model text or reasoning. `ai_trigger_sequence` identifies the customer message being answered. Queued turns have no stage yet. Progress clears when the turn finishes, is cancelled, loses its lease or authority, or a person takes over. Clients should also hide progress once they receive a reply newer than the trigger sequence, even if an older state frame is still in flight. Polling returns the same fields after a reconnect. The supplied widget shows a quiet animated progress card, respects reduced motion, and displays the complete answer only after source checking and the final authorized commit. No provisional answer is streamed.

New customer messages wake the reply worker immediately. Exact connected-tool reads can be reused within the same authorized attempt, and duplicate source text is not repeated in later model prompts. These remove avoidable work; provider generation and evidence checks still determine much of the response time.

AI messages appear in HTTP and WebSocket history with explicit `ai` attribution, validated citations and the usual stable sequence. They contribute to customer unread counts, but never a human's first-response timestamp or customer connection IP. Treat new sender values deliberately when updating custom clients; older integrations filtering `admin` still mean a human reply. Reconnect reads the committed log, never provisional text. A newer customer message, mode transition, source/configuration change or revoked issuing authority fences unfinished work. No provider/model fallback occurs.

`chat.message_created` retains schema version 1 for customer and human administrator messages. AI messages use schema version 2 with an AI actor and omit the customer identity object. Their bodies/citations are available through authorized message APIs, not webhook bodies. Support-mode changes are recorded internally as schema-version-2 events, but `chat.support_mode_changed` is currently not an accepted webhook subscription in the public settings API. Use authorized conversation HTTP/WebSocket state to observe the current support mode. Signed message delivery remains at least once and unordered. Shared handoff notifications contain generic human-support copy.

See [AI administration](ai-web-admin.md) for setup, [the customer widget guide](ai-widget-contract.md) for the supplied interface, and [generation behavior](ai-generation-contract.md) for the server contract.

## Relationship to connected-app profiles

A connected-app profile can show existing conversations only after an administrator confirms its user IDs share the project's current external identity namespace. This is an exact read-only lookup; it never creates a customer, links a guest, normalizes an ID or grants chat credentials. [Connected-app setup](connected-apps.md) remains independent of website AI and human chat.


## Business hours and follow-up contact

Shared account hours and project overrides explain human reply availability without blocking messages. Optional follow-up email is unverified and used for manual replies only; it is separate from AI/chat message content. Configure hours on the website. Native administrators can see a saved email in Conversation info. See the [business-hours guide](chat-business-hours.md).
