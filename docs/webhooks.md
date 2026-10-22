# Webhook contract and verification

Each project configures one URL, an enabled flag, subscriptions, and an encrypted signing secret. Events are immutable. Request and human/customer chat payloads use schema version 1; optional AI replies use version 2. A business change, its event, and a subscribed delivery job commit together. Delivery is asynchronous and resumes after restart.

## Set up a receiver

1. Add a receiver in your backend that verifies the raw request body as described below, saves the event durably and returns a 2xx response.
2. In **Project settings → Webhook destination**, enter its URL, enable delivery and select the events you need.
3. Save the signing secret when it is first shown. Store it in your receiver's secret configuration; it is not an API credential.
4. Choose **Send test webhook**, then check **Delivery history** and confirm your receiver accepted the signature.

For API setup, use a native administrator session or a project key with `manage`. Browser administrator mutations also require the usual exact Origin and CSRF token. Routes are under `/api/v1/projects/{project}`:

| Method and path | Use |
| --- | --- |
| `GET /webhook` | Read settings and their current `version`; the secret is not returned. |
| `PUT /webhook` | Send `{url,enabled,events,version,rotate_secret?}`. Use the version just read. `signing_secret` is returned only when created or rotated; save it securely. |
| `POST /webhook/test` | Queue a test; no body. A `202` response means queued, not delivered. |
| `GET /deliveries` | Inspect jobs; optional `failed`, `limit` (1–100, default 25) and `offset` (0–1,000,000). Returns `{items,has_more,limit,offset}`. |
| `GET /deliveries/{delivery_id}` | Inspect one delivery. |
| `POST /deliveries/{delivery_id}/retry` | Retry an eligible failed delivery; no body. Returns `202` when queued. |

Treat `409` as a settings conflict: reload the current version before saving again. Keep project credentials on your backend; customers and anonymous clients cannot manage deliveries. Full request and response schemas are in the [OpenAPI document](openapi.json).

## Events

Available subscriptions: `request.created`, `request.updated`, `request.resubmitted`, `request.accepted`, `request.rejected`, `request.status_changed`, `request.deleted`, `chat.message_created`. Shipping is a status-change event whose request snapshot has `status="shipped"`; no redundant shipped event is emitted. A rejected creator edit emits `request.resubmitted`, carrying both content and moderation changes in one event. Other content changes emit `request.updated`. Setting the same lifecycle/content again produces no event. A manual test uses `type="webhook.test"` and `test=true`; it is clearly separate from request events and sends even if subscriptions/delivery are disabled, provided a destination and secret are configured.

`chat.message_created` covers newly committed customer messages, administrator replies and optional AI replies. Human/customer events retain schema version 1 with `message.sender_role` `customer` or `admin`; AI events use schema version 2 with `sender_role` `ai` and omit customer identity. Filter for `admin` for human-only reply notifications. Human/customer payloads include project, conversation, message sequence and durable customer identity (with exact project-scoped external ID when present), but no message body. AI payloads include the project, conversation and message sequence without customer identity or citation text. It is saved atomically with the message and uses the same signing, retry, rotation and inspection contract below. Retrying the message itself produces no second event; HTTP delivery remains at least once. Deduplicate by event ID and do not assume delivery order—use the message sequence and authenticated history API to catch up. See [chat payloads and integration examples](chat.md).

Request events contain `id`, `type`, `schema_version`, `occurred_at`, project and board `{id,identifier}`, `request_id`, canonical `url`, a plain-text `request` snapshot, relevant `previous` values, `actor`, and `creator`. `creator` has `user_id` and nullable project-scoped `external_user_id`. An integration actor identifies its credential and, for participant operations, the resolved user. Native actors identify the native user. Account email fields, passwords, session tokens, API key secrets and signing secrets are not included. Treat request titles and descriptions as user-supplied content; users can put personal information in that text.

Explicitly confirmed website guest reports use actor `kind="guest"`, with their credentialless internal feedback owner and no API credential. Their creator has no external identity. Draft preparation emits no request event and creates no feedback owner. Guest chat credentials, private conversation content, and connection addresses are absent from request webhook payloads. A webhook's canonical request URL does not grant access to an unpublished request; customer-facing report receipts separately honor the configured feedback-page destination.

```json
{
  "id": "event-uuid",
  "type": "request.status_changed",
  "schema_version": 1,
  "occurred_at": "2026-10-02T12:00:00.000Z",
  "project": {"id": "project-uuid", "identifier": "glean"},
  "board": {"id": "board-uuid", "identifier": "features"},
  "request_id": "request-uuid",
  "url": "https://feedback.example.com/requests/request-uuid",
  "request": {
    "id": "request-uuid", "title": "Offline mode", "description": "…",
    "moderation": "accepted", "status": "shipped", "accepted_at": "…",
    "rejection_reason": null, "version": 5
  },
  "previous": {"moderation": "accepted", "status": "in_progress", "rejection_reason": null},
  "actor": {"user_id": "admin-uuid", "api_key_id": null, "kind": "native"},
  "creator": {"user_id": "user-uuid", "external_user_id": "glean-user-123"}
}
```

## Signing

Headers:

- `X-Feedback-Event-ID`: stable event ID
- `X-Feedback-Timestamp`: Unix seconds for this attempt
- `X-Feedback-Signature`: `v1=` followed by lowercase hex HMAC-SHA256

The signed message is the ASCII timestamp, one literal period byte, and the exact raw UTF-8 request body: `timestamp + b'.' + body`. Verify before parsing or reserializing JSON. Use a constant-time comparison and a tolerance of 300 seconds. Ensure both servers have synchronized clocks.

```python
import hashlib
import hmac
import json
import time


def verify(headers, raw_body, accepted_secrets):
    timestamp = headers['X-Feedback-Timestamp']
    if abs(time.time() - int(timestamp)) > 300:
        raise ValueError('stale delivery')
    received = headers['X-Feedback-Signature']
    message = timestamp.encode('ascii') + b'.' + raw_body
    valid = any(hmac.compare_digest(received, 'v1=' + hmac.new(
        secret.encode(), message, hashlib.sha256).hexdigest())
        for secret in accepted_secrets)
    if not valid:
        raise ValueError('invalid signature')
    event = json.loads(raw_body)
    if event['id'] != headers['X-Feedback-Event-ID']:
        raise ValueError('event ID mismatch')
    return event
```

A Node receiver can use `createHmac('sha256', secret).update(timestamp).update('.').update(rawBody).digest('hex')`; compare equally sized signature buffers with `timingSafeEqual`. Configure your web framework to retain the raw request body.

Persist event IDs in your own database with uniqueness. Process the event and insert its deduplication record atomically when possible. A duplicate should return a successful 2xx response. Do not acknowledge before your event is safely persisted or processed. Deliveries may arrive out of order; use the request version in the snapshot when maintaining downstream state.

## Reliability and administration

Any 2xx succeeds. Network/TLS failures, 408, 425, 429, and 5xx retry. Other HTTP errors, redirects, and unsafe destinations fail for admin attention. There are eight automatic attempts, including the first, with exponential backoff from five seconds, ±25% jitter, and a one-hour cap. Socket operations have ten-second timeouts. `Retry-After` from the receiver is not honored in v1. A failed delivery may be inspected and manually retried; manual retry starts a fresh eight-attempt budget while retaining the same event ID, raw payload, destination, and signing secret snapshot.

Leases expire after 60 seconds. Abandoned jobs are reclaimed after expiration. Completion is fenced by a unique lease token to prevent stale workers from overwriting newer results. A restart/crash may redeliver a successfully received event. The contract is at-least-once, not exactly-once.

Webhook configuration changes affect new jobs. Queued jobs keep their original URL and encrypted secret, including after URL changes, disabling delivery, or secret rotation. Keep the receiver accepting old and new secrets until old jobs finish. Retry timestamps/signatures are fresh; event IDs and raw bodies remain stable. The encryption key in `/data/application.key` (or `APPLICATION_SECRET`) is required to decrypt these snapshots after restart/restore. Do not rotate that application key by simply changing the environment.

Destinations are validated at configuration and each attempt. All resolved addresses are checked; a validated IP is pinned for the connection while HTTPS still verifies the hostname and certificate. Redirects are not followed. Private/loopback destinations require `ALLOW_INTERNAL_WEBHOOKS=true`; link-local/metadata destinations remain blocked. The worker stores only bounded categorical diagnostics and HTTP status, never response bodies or raw exception messages.

For downstream notifications, use the current audience endpoint with `audience:read`. It deduplicates creator/voters but does not persist an audience snapshot at event time. Your backend decides whom to notify and how.


Support-mode changes are recorded internally as `chat.support_mode_changed` events, but the public webhook settings API does not currently accept this subscription. Its documented payload shape does not mean it can be enabled for delivery. Read the current mode through the authorized chat HTTP/WebSocket API instead. Existing customer/admin message payloads and signatures retain their contract.

## Connected apps are not webhook receivers

[Connected-app reads](connected-apps.md) use a separate dedicated outbound bearer token and refuse redirects/private destinations. They do not enqueue delivery jobs or reuse webhook signing secrets. Existing immutable event snapshots and delivery semantics remain unchanged.
