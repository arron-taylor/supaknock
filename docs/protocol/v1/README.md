# Connected App Protocol 1.0

This is the read-only API **your app implements** so SupaKnock administrators can browse its accounts and activity. Your app keeps its own database. Start with a user directory to show recent signups, then add summaries, profiles or activity when useful. No SupaKnock database access or client SDK is required.

The standalone `*-request.json` and `*-response.json` JSON Schemas are the wire contract. [examples.json](examples.json) contains valid requests and responses for all five operations. Generate schemas with `python scripts/export-connected-protocol.py`; behavioral validation lives in `feedback/connected_apps_protocol.py` and `tests/test_connected_protocol.py`.

## Routes and authentication

| Route | When required | Purpose |
| --- | --- | --- |
| `GET /v1/manifest` | Always | Stable app identity, capabilities, supported user filters and candidate link origins. |
| `POST /v1/summary/query` | `summary: true` | Current account total and new-account count for the requested period. |
| `POST /v1/users/query` | `users: true` | Current accounts ordered by signup time, newest first. |
| `POST /v1/users/lookup` | `profiles: true` | One current profile for an exact `user_id`. |
| `POST /v1/activity/query` | `activity: true` | Retained events in the requested period, newest first. |

Authenticate **every route and every page**, including discovery, with `Authorization: Bearer <app-read-token>`. Issue a dedicated token in your app, restricted to these read operations. Never reuse a SupaKnock project key, administrator session or customer chat credential. POST operations are read-only. Return `200` with `Content-Type: application/json` and `Cache-Control: no-store` on success. All response envelopes require `protocol`, `app_instance_id` and `observed_at`.

Serve these routes at a public HTTPS address, optionally under a simple prefix. For example, the configured address `https://app.example/support` produces `https://app.example/support/v1/manifest`. Do not include `/v1` in the configured address unless it is genuinely part of that prefix. URLs cannot contain credentials, queries or fragments. Private networks and redirects are refused. Keep a permanent `app_instance_id` per app/environment; staging and production need different IDs.

Implement discovery plus at least one supported operation. Capability flags default to false. `user_activity` requires `activity` and support for its exact user filter. Only `query` (name/email) and `joined_range` are supported user filters; advertise only those your app implements. An unfiltered directory is valid.

Give the administrator the base address and token through a secure channel. They save it under **Project settings → App connection**, test it, review the returned identity/link origins, and enable it. Testing checks discovery, so validate the data operations before handing over the adapter. See the [implementation brief](../../connected-apps-agent-brief.md) for a synthetic reference service and staging probe.

## Query and data rules

Users and activity default to 50 records per page and accept `limit` from 1 to 100. A users query accepts optional `query` text up to 200 characters and an optional `joined` range. Summary and activity require `from` and `until`. Ranges are half-open: `from` is included and `until` is excluded. Use UTC timestamps ending in `Z`, with at most six fractional digits. For the newest current accounts, the initial users request can simply be:

```json
{"limit": 50}
```

Request unknown fields are rejected. Bounded additive response fields are ignored; required fields and values are strict. Version major must be 1; minor additions cannot change existing semantics. Details have six fixed primitive types: text, number, boolean, timestamp, badge and link. Numeric detail values are safe integers in v1.

Summary counts use integers from 0 to 2^53−1, or null with a reason (`unsupported` or `history_incomplete`). The new-account count declares its basis: `current_accounts` excludes deleted accounts; `signup_events` requires complete retained signup history for the interval. A recent-users list is a current directory, not a historical signup ledger. Activity shows signup events only if your app retains and returns them. Counts represent accounts, not deduplicated people across apps.

IDs contain 1–512 characters of valid UTF-8 and preserve exact bytes; never trim, case-fold, normalize or match by email. Sort users by `(joined_at DESC, id UTF-8 bytes DESC)` and activity by `(occurred_at DESC, id UTF-8 bytes DESC)`. Sort time as integer seconds plus microseconds, not rounded JavaScript milliseconds. App links require HTTPS and administrator-approved origins. No script/HTML interpretation or automatic link fetch.

Errors use `{ "error": { "code": "invalid_input", "message": "Check the input" } }` with the status/code mapping in the semantics guide. Error text is diagnostic only; SupaKnock supplies its own safe messages. Do not echo input, credentials or database exceptions. JSON is bounded to 1 MiB per response. No redirects or compressed responses. No pagination URLs. Token authorization applies on every page; cursors are not credentials.

The installation-facing `/api/v1` API is separate and described by [SupaKnock's generated OpenAPI](../../openapi.json). Its administrator routes wrap these reads and enforce session/project permissions. Implement the routes above in your app; do not copy SupaKnock's `/api/v1/projects/.../app-data` routes into it.

## Optional user phone number

User records in both `/v1/users/query` (`items[]`) and `/v1/users/lookup` (`user`) may include `phone_number`, a nullable string of at most 64 characters. Omit it or return null when unavailable. This is a backward-compatible v1 addition: existing 1.0 adapters need no version or capability change, and older clients can ignore the field.

Return a string, never a JSON number, so a leading `+`, zeroes and extensions survive. For example, `"phone_number": "+1 (312) 555-0142 ext. 9"`. SupaKnock preserves the supplied formatting without guessing a country or normalizing it. Use the same value in list and profile responses when both supply it; it is also valid to expose it only in profiles.

The field is private, on-demand profile information for native administrators. It is displayed as plain text, not a verified contact method, login identifier, support-identity link, calling/SMS action or marketing consent. It is not persisted by SupaKnock or added to AI context. User search remains name/email only. Do not place phone numbers in public activity titles or logs to implement this field.

## Continuation and failure semantics

Every successful response carries `protocol`, `app_instance_id` and `observed_at`. All operations require the dedicated bearer token. Unknown additive fields are ignored within byte/depth bounds; unsupported major versions fail closed. Unknown optional profile detail types are skipped, with the original maximum twelve-entry bound still enforced. UTC timestamps support up to six fractional digits. IDs remain exact UTF-8 bytes. Counts and numeric details are safe integers, never floating-point or boolean substitutes.

Initial users/activity queries omit `cursor` or set null. The app returns an opaque signed/encrypted cursor or null at the end. An empty page with a non-null cursor is invalid. Subsequent queries repeat the exact filter/limit and pass the cursor. Keep scan bound or activity snapshot and expiry fixed; the next page's first order tuple must be strictly less than the prior page's last. Cursor values are not URLs. The app should return 400 for invalid queries, 401/403 for authorization, 404 for absent profiles, 409/410 for expired/invalid continuation, 429 for temporary limits and 503 for unavailable reads. Error bodies contain generic codes, not private data. SupaKnock never displays upstream error bodies.

Bind user cursors to the exact filters, limit, app instance, `scan_until`, last ordering tuple and expiry. Current rows and name/email matches remain live, so edits or deletions can change membership while paging. Activity additionally needs a fixed snapshot and committed visibility watermark: an event that commits after the first page must not enter that snapshot, even with an older timestamp. Keep expiry fixed across pages and within thirty minutes of first issue. Refresh starts over to include newer or backfilled events. See the [implementation brief](../../connected-apps-agent-brief.md) for the late-commit case and tests an adapter should pass.

SupaKnock's own API uses a separate encrypted `continuation` field bound to its native session, project, connection/version, filters and process epoch. Its returned `data.next_cursor` is always null; no raw app cursor reaches clients. This wrapper does not replace app cursor validation or live authorization.

Response ceilings: 1 MiB JSON, nesting twelve, page limit 100 (clients normally request 50), at most twelve typed profile details, two non-null profile links total, eight candidate link origins, 8 KiB app cursor UTF-8 and 16 KiB wrapped continuation. Profiles/events render text only. Link origins need explicit administrator approval; an adapter must not use those links as open redirects or put secrets in them.

Implementers: [agent brief](../../connected-apps-agent-brief.md). Administrators: [setup guide](../../connected-apps.md). The earlier [design draft](README.md) explains rationale; these schemas and the executable fixtures define the shipped v1 shapes.
