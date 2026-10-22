# Build support into your app

Use the same `/api/v1` API as the SupaKnock website and iPhone app. You can show a feedback board, submit requests for your users, add customer chat, or build your own support interface. Your app keeps its own accounts and sign-in flow.

The [developer reference](https://supaknock.com/developers) provides setup instructions and a searchable endpoint/schema reference. Human documentation and marketing assets are not shipped in installation images. The running `/api/v1/openapi.json` lists every HTTP operation and its schemas; [the checked-in reference](openapi.json) describes this checkout. Match your client to the specification from the server you actually run.

## Set up a project

1. Start a SupaKnock server and open `/setup`. Use its setup token to create the first administrator. Normal registration only creates participants.
2. In **Overview**, create a project for your app. Give it a stable identifier, such as `glean`.
3. In that project's settings, create a board with an identifier such as `features`.
4. Create a project API credential with **read** and **participate**. Save the secret when shown; it cannot be displayed again. Add other permissions only when your backend needs them.
5. Put the server URL, project identifier and secret in your backend's private configuration. Never put the project secret in browser JavaScript, an iPhone build, or a public repository.

Use one project per app or environment when their users and support data should stay separate. A project contains boards; each board contains feedback requests. Chat and its website-origin settings belong to the project.

| Value | Example | Where to use it |
| --- | --- | --- |
| Server URL | `https://box.example.com` | Base origin for the API and widget |
| Project identifier | `glean` | `{project}` paths accept this stable identifier or the project's UUID |
| Board identifier | `features` | Find the right board in the project's board list |
| Board ID | Returned `id` UUID | Required by `/boards/{board}/...`; a board identifier is not a substitute |
| Request ID | Returned `id` UUID | Feedback detail, edit, vote, history and moderation paths |
| External user ID | Your existing account ID, kept as a string | Identifies your already-authenticated user within this project |

Names are labels and can change. Keep returned IDs; do not derive them from names. External IDs are exact opaque strings of 1–512 characters: case, spaces and leading zeros matter. They are not normalized, matched by email, or linked across projects. An external ID is never a credential.

## Submit your first feedback request

This Python example runs on **your backend**. Set `SUPAKNOCK_URL`, `SUPAKNOCK_PROJECT`, `SUPAKNOCK_BOARD` and `SUPAKNOCK_API_KEY` in its environment. The project and board values are identifiers such as `glean` and `features`.

Call `submit_feedback` only after your app authenticates the user. `authenticated_user.id` below is the existing string ID from that verified account, not a user ID supplied in an untrusted request body.

```python
import os
import httpx


def submit_feedback(authenticated_user, title, description):
    with httpx.Client(
        base_url=os.environ['SUPAKNOCK_URL'].rstrip('/') + '/api/v1/',
        headers={'Authorization': 'Bearer ' + os.environ['SUPAKNOCK_API_KEY']},
        timeout=15,
    ) as client:
        project = os.environ['SUPAKNOCK_PROJECT']
        board_identifier = os.environ['SUPAKNOCK_BOARD']
        offset = 0
        board_id = None
        while board_id is None:
            response = client.get(f'projects/{project}/boards', params={
                'limit': 100, 'offset': offset,
            })
            response.raise_for_status()
            page = response.json()
            board_id = next((b['id'] for b in page['items']
                             if b['identifier'] == board_identifier), None)
            if board_id is not None:
                break
            if not page['has_more']:
                raise ValueError('The configured feedback board was not found')
            offset += page['limit']

        response = client.post(f'boards/{board_id}/requests', json={
            'title': title,
            'description': description,
            'external_user_id': authenticated_user.id,
        })
        response.raise_for_status()
        return response.json()
```

A successful submission returns **201** with the request's `id`, `version` and current state. It starts pending and is visible only to its owner and authorized managers. An administrator must accept it before it appears publicly. Submitting does not add a vote. You can retain the board UUID in your backend configuration after discovery.

Feedback submission has **no idempotency key** in v1. If a send times out, check that user's recent submissions before deciding to resend; blindly retrying can create a duplicate. The same caution applies to creating projects, boards and credentials.

## Choose the right authentication

Use exactly one authentication transport per request. Combining a cookie, Bearer credential or chat token is rejected where those transports overlap.

| Caller | Credential | What it can do |
| --- | --- | --- |
| Public reader | None | Read active projects, boards, accepted requests and their public history |
| Your trusted backend | `Authorization: Bearer fb_…` | Use the issued permissions in one project; pass a verified external ID for participant actions |
| SupaKnock website account | `feedback_session` cookie | Act as that account; authenticated writes need the CSRF token and configured Origin |
| Native administrator client | `Authorization: Bearer ns_…` | Act as that administrator, without cookie CSRF; expires and can be revoked |
| Authenticated chat customer | `X-Chat-Token: cc_…` | Access that customer's own project conversations; no administration |
| Anonymous widget visitor | `X-Chat-Token: cv_…` | Access the original visitor conversation; no account or feedback-board login |

### Project permissions

- **read:** read the project's public resources. **manage** also allows those reads.
- **participate:** submit, view the acting user's own submissions, make eligible creator edits/deletes, set votes, and exchange a verified identity for customer chat access.
- **audience:read:** read a request's current creator/voter identities for notifications sent by your backend.
- **manage:** manage that project's boards, feedback moderation/content/lifecycle and webhooks.

Permissions are independent: **manage** does not grant **participate** or **audience:read**. A normal feedback integration needs **read + participate**. A credential with only **participate** can use `/me/requests`, but cannot use general public-resource endpoints as an authenticated project reader.

Project credentials never grant installation administration: account management, new projects, key administration, archiving, AI settings, connected-app data and the administrator chat inbox require an administrator account. A project key cannot reach another project, even when an external ID happens to match.

### Website accounts and CSRF

`POST /auth/login` takes `{ "email": "…", "password": "…" }` and returns `user` and `csrf_token` while setting cookies. `/setup` and `/auth/register` also establish cookie sessions after successful account creation. `feedback_session` is HttpOnly; `feedback_csrf` is readable by same-origin JavaScript.

On authenticated POST, PUT, PATCH and DELETE calls, send `X-CSRF-Token` from the login response or CSRF cookie, plus the actual `Origin` equal to `PUBLIC_BASE_URL`. This includes read-only connected-app queries that use POST. Browsers supply Origin automatically. GET requests do not require a CSRF token. Do not forward a browser's Origin to a server-to-server project-key request.

`GET /auth/me` reads the signed-in account. `POST /auth/logout` revokes the current session. `POST /auth/password` requires the current and new passwords and revokes every session, including the current one. Email is an unverified login identifier; no email-reset or social-login flow is provided.

### Native administrator sessions

`POST /auth/native/login` takes the administrator's email and password **without existing cookies or Authorization**. It returns `token`, `expires_at`, `installation_id` and `user`. Send that token as a Bearer credential on later calls. This endpoint only signs in administrators; it is not a login endpoint for your app's customers.

Keep the token in secure client storage, use HTTPS and sign in again after expiry. Never combine it with cookies. Native Bearer requests do not need a CSRF token; if a client sends Origin, it must be the configured installation origin. The same logout/password endpoints revoke native sessions.

### Cross-origin websites

Public feedback reads allow cross-origin browser fetches with `credentials: 'omit'` and no Authorization header. General API CORS does not allow credentialed reads or writes from another website. Put those operations behind your own authenticated backend.

Chat has its own allowed-origin transport with `X-Chat-Token`. Enable the exact website origin in **Project settings → Website chat**. Do not use a project secret as a workaround for browser CORS.

## Read, edit and vote

All paths below are relative to `/api/v1`. For project-key participant operations, send `external_user_id` in the query string for GET/DELETE and in the JSON body for writes. Use an HTTP client's query encoder so spaces and special characters are preserved. SupaKnock account sessions omit the external ID.

| Operation | Method and path | Inputs |
| --- | --- | --- |
| Public board requests | `GET /boards/{board}/requests` | Optional `order=newest` or `popular`, lifecycle `status`, page values; external ID adds the acting user's vote state |
| Own submissions | `GET /me/requests` | External ID for a project credential; includes pending and rejected requests |
| Request detail | `GET /requests/{id}` | External ID when reading as its owner |
| Creator edit | `PATCH /requests/{id}` | `title`, `description`, current `version`, external ID |
| Creator delete | `DELETE /requests/{id}` | Current `version` and external ID in the query; success is 204 with no body |
| Set or remove vote | `PUT /requests/{id}/vote` | `present: true` or `false`, external ID; returns `vote_count` and `voted` |
| Own vote state | `GET /requests/{id}/vote` | External ID for a project credential |
| History | `GET /requests/{id}/history` | External ID for private owner history; public callers see only public events |

Only accepted requests can receive votes. Repeating the same vote PUT is safe: it sets the desired state rather than toggling it.

Moderation is `pending`, `accepted` or `rejected`. Accepted requests separately have a lifecycle: `under_consideration`, `planned`, `in_progress` or `shipped`. First acceptance records a permanent `accepted_at`; the creator can never edit or delete that request afterward. There is no unaccept operation. Editing a rejected request returns it to pending, clears the current rejection reason and preserves the old history.

## Moderate from a management backend

An explicitly issued **manage** credential can use:

- `POST /projects/{project}/boards` and `PATCH /boards/{id}`.
- `GET /moderation`, restricted to its own project; `state` defaults to `pending`.
- `POST /requests/{id}/accept` with `{ "version": 1 }`.
- `POST /requests/{id}/reject` with the current `version` and optional `reason`.
- `PUT /requests/{id}/status` with the current `version` and a lifecycle such as `shipped`.
- `PATCH /requests/{id}/content` with `title`, `description` and current `version`.
- The project's webhook configuration, test, delivery inspection and failed-delivery retry operations.

Use the returned version for your next edit. On **409**, refetch the resource and ask the user to review the newer state before retrying. A conflict can also mean a forbidden state change, such as editing after acceptance; a newer version does not bypass that rule. Supported no-op transitions do not create extra history events.

## Add chat

For an anonymous website widget, enable chat, allow your site's exact origin and copy the project's installation snippet. No project key goes in the snippet. The widget creates a visitor session only when the visitor deliberately sends a message. See the [widget setup and appearance guide](chat.md#add-the-widget).

For your own signed-in customer interface:

1. Your backend authenticates the user, then posts to `/chat/projects/{project}/customer-sessions` with its **participate** project key and `{ "external_user_id": "your-user-id", "client_type": "browser", "origin": "https://www.example.com" }`. The backend request itself omits Origin. For a native customer app, use `client_type: "native"` and omit the JSON `origin` field.
2. Return only the resulting `cc_` token and expiry to that client. It sends `X-Chat-Token` with no cookie or Bearer credential. Browser requests must use the bound Origin; native customer requests omit Origin.
3. List `/chat/conversations`, or POST there with a new `client_conversation_id` UUID to start a conversation. Keep that ID unchanged if you retry creation.
4. POST to `/chat/conversations/{id}/messages` with `{ "client_message_id": "a-UUID", "body": "Hello" }`. Keep the same ID and body when retrying an unacknowledged send. Reusing the ID for different content returns 409.
5. Read `/chat/conversations/{id}/messages?after=0&limit=25`. Keep `next_sequence` and send it as `after` to catch up. Mark viewed messages with `POST /chat/conversations/{id}/read` and `{ "sequence": 1 }`.

Customer tokens last one hour. Renew through the authenticated backend exchange; history and message retry identity survive renewal. `DELETE /chat/session` ends the current customer session without deleting history. Your app's logout must explicitly revoke its chat access; it does not automatically sign out of SupaKnock.

For live updates, `POST /chat/tickets` with `conversation_id` returns a one-use ticket valid for 60 seconds. Connect to `/api/v1/chat/ws` and send the ticket in the first frame, never in the URL. The exact frames, reconnect cursor and revocation rules are in [the chat contract](chat.md#http-contract) and the OpenAPI document's `x-websockets` section. HTTP message history remains available when sockets disconnect.

The built-in widget uses anonymous visitor sessions; it does not automatically adopt your app's login. Anonymous history cannot be recovered with a new credential or merged into an account. Authenticated apps use their own customer UI. Business hours set expectations and optional email supports manual follow-up; neither blocks messages nor sends email automatically.

## Receive webhooks and notify your users

Configure the project's destination and subscriptions in its settings or `/projects/{project}/webhook`. Save the signing secret when created or rotated. The **Send test webhook** action creates a separate test delivery, including when normal delivery is disabled.

Verify `X-Feedback-Signature` against HMAC-SHA256 of `X-Feedback-Timestamp + "." + raw_request_body`, before parsing JSON. Use a constant-time comparison, a five-minute timestamp tolerance and synchronized clocks. Persist and deduplicate `X-Feedback-Event-ID`; delivery is at least once and may arrive out of order. Return 2xx only after safely recording or processing the event. See the [complete verification example and retry rules](webhooks.md).

Request events cover creation, edits, resubmission, acceptance, rejection, status changes and deletion. Shipping is `request.status_changed` with `status: "shipped"`. To notify supporters, use **audience:read** on `GET /requests/{id}/audience` and paginate through all items. Creator and voters are deduplicated; map non-null external IDs back to your own accounts. This is the current audience, not an event-time snapshot. SupaKnock does not send those emails for you.

`chat.message_created` includes customer messages, administrator replies and optional AI replies. It contains identifiers and sequence data, never message text. Filter `message.sender_role` for the reply types your notification flow needs. Use authenticated chat history to read messages.

Inspect `/projects/{project}/deliveries` and `/projects/{project}/deliveries/{id}`. `POST /projects/{project}/deliveries/{id}/retry` retries failed jobs. Existing jobs retain their original destination, raw payload and signing secret after settings change, so keep old signing secrets accepted until those jobs finish.

## Connect your app's account data

[Connected apps](connected-apps.md) let administrators open summaries, user lists, profiles and activity from your app while handling support. These are on-demand, read-only views. Your app remains the source of its accounts and events; nothing is synchronized into SupaKnock accounts and this does not add signup alerts or marketing actions.

Have your app implement the separate [remote protocol](protocol/v1/README.md), using a permanent app-instance ID and a dedicated read-only token for SupaKnock to call it. The [implementation brief](connected-apps-agent-brief.md) explains what to build. This token is different from a SupaKnock project credential.

In **Project settings → App connection**, save the public HTTPS endpoint and token, test the saved connection, review the discovered identity and approved link origins, then enable it. Confirm the support identity namespace only when returned user IDs exactly match this project's feedback/chat external IDs. Optional signup filters and recent activity depend on the capabilities and history your app supplies. There is no name/email-based linking.

The administrator API uses `/projects/{project}/app-data/connection` and its test/enable/disable/remove actions. Read POSTs at `/app-data/{summary|users|profile|activity}/query` carry connection `version` and a typed `query`; paged users/activity reads use an opaque `continuation`. `/app-data/related/query` uses offset paging for related local support records. Cookie clients still need CSRF on these read POSTs. Project and customer credentials cannot use them.

The installation library and timezone settings live under `/admin/connected-apps`. Restarting the app or changing connection settings invalidates old continuation/test receipts. Refresh the view; do not reuse an old receipt. Remote app responses are not persistently cached or added to AI knowledge, notifications or backups.

## Pagination, errors and limits

Most lists use `limit` (default 25, maximum 100) and `offset` (default 0, maximum 1,000,000). Responses contain `items`, `limit`, `offset` and `has_more`. Add the returned limit to your offset until `has_more` is false. Request lists use newest-first order by default; projects/boards use oldest-first order with stable ID tie breakers. Popularity and active chat lists can move while you page; offset pagination is not a snapshot.

Chat message history uses `after`/`next_sequence`. Connected-app user/activity reads use `continuation`. Some administrative collections are simple `{ "items": [...] }` lists without pagination. Follow the specific operation's schema rather than assuming one pagination scheme everywhere.

```json
{
  "error": {
    "code": "invalid_input",
    "message": "Check the submitted fields",
    "fields": [{ "field": "body.title", "message": "Field required" }]
  }
}
```

`fields` is present on input-validation errors, not every failure. Use the stable error `code` to choose behavior; show the message without assuming it is a permanent API identifier.

| Status | What to do |
| --- | --- |
| 400 | Correct the named input fields or incompatible authentication |
| 401 | Supply valid credentials or renew/sign in again |
| 403 | Check permissions, enabled origins and cookie CSRF |
| 404 | Treat the resource as unavailable; private or archived records may be deliberately concealed |
| 409 | Refresh the version/state, then decide whether the action is still valid |
| 413 | Reduce the request body; check that operation's limits |
| 429 | Wait before retrying; honor `Retry-After` when supplied |
| 500/502/503/504 | Surface a temporary failure; retry only when the operation's idempotence rules allow it |

Feedback titles are at most 200 characters; descriptions at most 20,000. Chat messages are at most 4,000 characters. Default JSON bodies are limited to 96 KiB; ordinary chat bodies to 32 KiB. The defined Markdown, personality and report-edit routes allow up to 400 KiB for JSON escaping overhead while retaining their own decoded-text limits. Unknown input fields are rejected. These limits also apply to chunked requests.

Ordinary mutations default to 60 per minute per connection IP, and authentication attempts to 10; operators can configure those limits. Connected-app queries have separate budgets. Do not rely on a rate limit as a substitute for your own backend authentication or abuse controls.

Timestamps are UTC ISO-8601 strings. Webhook signature timestamps use Unix seconds. Successful DELETE/logout responses commonly return 204 with no body; do not try to parse JSON from an empty response.

## Databases and installation setup

SQLite is the default. PostgreSQL and Supabase are optional installation storage choices configured with `DATABASE_PROVIDER` and `DATABASE_URL`; remote providers also require a stable `APPLICATION_SECRET`. Supabase uses a direct or session-pooler PostgreSQL connection, not a public API key. See [database setup](database-providers.md) for TLS, key handling and connection examples.

The API and project credentials stay the same with every provider. Your clients always call SupaKnock; they never need database credentials or access to its tables. Switching providers does not transfer existing data. Keep one app instance and the local volume for setup state and optional model weights. Use the [supported backup and restore procedure](operations.md).

## Other administrator features

The full API also covers the [shared notification inbox](notifications.md), [optional push and device registration](push.md), [business hours and manual follow-up](chat-business-hours.md), and [AI configuration and knowledge](ai-web-admin.md). AI, individual assistant tools and guest reporting start off; configuration or a successful Preview does not activate customer AI.

[Archiving projects and boards](archiving.md) is administrator-only. Archived resources disappear from participant/project-key reads and reject new writes. Restoration preserves resource IDs but does not revive old revoked chat sessions; renew access as needed.

Administrators can see private connection metadata and successful account activity at `/admin/account-activity`. Other callers—including creators, project credentials and chat customers—do not receive IP metadata. The address observed for a backend integration is that backend's connection, not the person named by an external ID. There is no client-supplied IP override. See [proxy configuration and retention](operations.md#private-connection-metadata).

Use the running `/api/v1/openapi.json` for every route, request and response model, permission and supported error. [The documentation index](README.md) links the feature contracts; [the verification record](verification.md) distinguishes implemented source from tested builds and deployment.

## Board visibility

Boards can be `public` or `private`; existing boards remain public. Private boards are hidden from public browsing, but backend `read`/`participate` permissions and enabled chat intake still work. A trusted backend must enforce its own customer-facing access policy. See [board visibility](board-visibility.md) for ownership, direct-link and API details.
