# App integration brief for an implementation agent

Implement a small authenticated, read-only adapter in **the application's backend** so an administrator can see its accounts and activity in SupaKnock. Keep the app's existing database and authentication. Browsing must not import users, change accounts, expose arbitrary queries or create SupaKnock users.

Read [protocol v1](protocol/v1/README.md), its normative JSON schemas and examples first. The installation API in `docs/openapi.json` is a different interface; it is used by SupaKnock's own web/native administrator clients. The app-issued read token is separate from a SupaKnock project API key or administrator session.

## Inspect and decide

Document the app's user table/model, exact permanent IDs, immutable signup timestamp, existing SupaKnock `external_user_id` integration, deletion behavior, event retention, timezone handling and access controls. Preserve ID UTF-8 bytes including case, spaces and composed/decomposed Unicode; never join by email. If IDs are not the existing support namespace, keep local support linking off.

Choose a permanent nonsecret `app_instance_id` per app/environment. Never reuse it across staging/production or another app. Issue a dedicated high-entropy bearer token with only these read operations. Store/rotate/revoke it using the app's existing secret management. Authenticate **every route**, including discovery, compare tokens safely, and never log headers, queries, returned records, tokens or cursors.

Start with the capabilities the app can honestly support. A directory-only or summary-only app is useful. Do not invent historical events from `updated_at`. Activity requires committed visibility history; omit it until that exists. An account ID must never be reassigned to another person.

## Implement discovery and the supported operations

`GET /v1/manifest` is required. Advertise at least one of `summary`, `users`, `profiles` or `activity`, then implement its corresponding route. All capability flags default to false. Only advertise `user_activity` if activity also supports an exact `user_id` filter. Search (`query`) and signup-date filtering (`joined_range`) are optional user filters, declared separately.

| Method and route | Responsibility |
| --- | --- |
| GET `/v1/manifest` | Protocol major/minor, stable instance ID, observed time, optional capabilities, supported user filters and approved candidate link origins. |
| POST `/v1/summary/query` | Current total accounts and new accounts in the exact half-open interval. Return explicit unknowns/reasons and counting basis. |
| POST `/v1/users/query` | Current accounts, newest signup first; optional app-side name/email search and joined interval. Deterministic live keyset pages, not offset pagination. |
| POST `/v1/users/lookup` | One current profile for the exact supplied ID; 404 if absent/deleted. Bounded typed fields and approved HTTPS links only. |
| POST `/v1/activity/query` | Optional retained immutable events, optional exact user filter, stable committed watermark and keyset pages. |

A configured public HTTPS address prefix is supported; the routes above are appended below it. Return JSON with `Cache-Control: no-store`. Every successful response includes `protocol`, `app_instance_id` and `observed_at`, not only discovery. Read POSTs never change app business state. Enforce bounds on body bytes, text, page sizes, query complexity and execution time inside the app. SupaKnock refuses private destinations, redirects and compressed responses; it accepts at most 1 MiB of JSON per response and gives a whole read, including discovery, ten seconds.

For recent signups, `users` alone is enough: return current accounts ordered by original `joined_at`, newest first. Deletions disappear from this directory. Summary counts must distinguish `current_accounts` from `signup_events`; the latter requires complete retained signup history for the interval. Return null with a reason when a count is unavailable, never a made-up zero. Do not synthesize activity events from the current users table or promise signup notifications.

Optionally map the app's existing user phone to `phone_number` in profile `user` and, if useful, directory `items[]`. Use a string of at most 64 characters or null/omission, preserving international prefixes and extensions. Do not invent a number, convert it to a numeric type, link accounts by it, or treat it as verified. This additive v1 field requires no new capability; SupaKnock shows it privately in web/iPhone profiles. Search remains name/email. See the [phone-number contract](protocol/v1/README.md#optional-user-phone-number).

For an existing Glean connection, use the focused [phone upgrade handoff](connected-apps-agent-brief.md). It scopes the work to the existing adapter and includes compatibility tests and rollout checks.

User pages fix `scan_until` on the first page and sort by `(joined_at DESC, id UTF-8 bytes DESC)`. Keep name/email matching and retained rows live; document that search membership can change. Bind a signed/encrypted opaque app cursor to filters, limit, immutable last tuple, scan bound, app instance and fixed expiry. Continuations retain their original expiry, at most thirty minutes. Use binary ID collation, not locale/case-insensitive collation.

Activity pages additionally fix a committed visibility watermark on the first page. A sequence allocated before commit is insufficient: writer A can reserve 10, writer B commit 11, reader capture 11, then writer A commit late and incorrectly appear in the old snapshot. Allocate publication position and commit visibility together under an appropriate transaction/serialization rule, use an actual consistent retained snapshot, or do not advertise activity. The synthetic fixture demonstrates atomic publication under one lock; production apps need the database-equivalent guarantee. Later backdated arrivals must appear only after refresh. Do not retain a database transaction indefinitely per scrolling browser.

Sort event timestamps with microsecond precision and IDs by UTF-8 bytes; the shared [ordering fixture](protocol/v1/fixtures/order.json) includes distinctions lost by JavaScript milliseconds or Swift canonical String equality. Handle deletion/retention truthfully; return an expired-snapshot error when a continuation cannot be honored.

## Validate before connecting

The repo includes a deliberately synthetic in-memory adapter and bounded probe. Neither is a production service or a runtime requirement:

```sh
# In a checkout with the test dependencies installed; use a disposable token.
export CONNECTED_REFERENCE_TOKEN='synthetic-only-reference-token'
.venv/bin/python scripts/connected-reference.py
# In a second terminal with the same environment variable:
.venv/bin/python scripts/check-connected-adapter.py http://127.0.0.1:8769 --synthetic-loopback
```

For your own public HTTPS **staging** adapter, use `scripts/check-connected-adapter.py https://staging.example/suggestion-box --token-env YOUR_STAGING_READ_TOKEN`. Supply only an environment-variable name on the command line, not a secret. This probe makes at most eight bounded reads and prints check names, not private records. It checks shapes and authenticated reads; it is not a full security or data-correctness certification. The fixed loopback switch belongs only to the synthetic reference tool; production SupaKnock has no private-network bypass.

Add app-owned tests for missing/wrong/revoked tokens; exact IDs and project/environment isolation; counts with deleted accounts and incomplete signup retention; pagination with tied timestamps and Unicode IDs; live search edits/deletion/backfills; activity late-commit interleaving; expired/repeated/tampered/wrong-query cursors; response limits; unsafe links/redirects; no private payload logging. Include at least 130 events to cross repeated refill boundaries.

Deliver the endpoint URL, permanent instance ID, supported capabilities, coverage meaning and token **storage location**, never the token in a report. Have the owner save/test/review/enable it in SupaKnock and verify summaries, users, one profile and activity if supported. Leave production deployment and real data access under the app owner's existing process.
