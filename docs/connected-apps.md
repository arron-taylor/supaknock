# Connected apps

Connect your app to SupaKnock to see its newest accounts, look up a customer, or read recent activity alongside their feedback and chats. Your app supplies the data through a small read-only API. You choose which views to support; a user directory alone is a useful place to start.

These views are private to your installation's administrators, on the web and iPhone. Your application keeps its users and events. Browsing does not import them, create SupaKnock accounts, or change anything in your app. The connection works with any of SupaKnock's [database providers](database-providers.md); your app can keep using its own database.

## Connect an app

1. Have your app implement [protocol v1](protocol/v1/README.md), or give its developer the [implementation brief](connected-apps-agent-brief.md). It needs authenticated discovery plus the operations it advertises. Give each app/environment a permanent `app_instance_id` and a dedicated read-only bearer token. This token is issued by **your app**, separately from SupaKnock project API keys.
2. Open **Project settings → App connection**. Enter the public HTTPS protocol address and its token. An address may include a simple prefix such as `/suggestion-box`; fixed `/v1/...` paths are appended to it.
3. Save, then **Test saved connection**. Testing reads authenticated discovery only. It does not silently turn anything on or prove every optional operation works.
4. Review the reported app identity, capabilities and allowed link origins. Only approve links you recognize. Check the support identity box only if app user IDs exactly match this project's existing feedback/chat `external_user_id` namespace. Leave it off if unsure; browsing still works.
5. Choose **Enable this connection**, then **Open app data**. Unconnected projects and existing boards/chat are unaffected.

Use your SupaKnock administrator account for setup and browsing. A project API key, participant account or customer chat credential cannot open these views. The read token stays encrypted on your SupaKnock server; it does not go into your website's widget or iPhone app.

One app instance can bind to only one project. Removing a connection deletes its token/address but preserves that nonsecret identity binding. A different app or environment belongs in a different project. Changing the address requires a new deliberately supplied token. Saving configuration disables the connection until it is tested and enabled again. To change approved link origins or the namespace confirmation, test the saved connection and review it again.

Public HTTPS destinations only: local/private networks, metadata addresses, mixed public/private DNS answers, redirects and credentialed URLs are refused. The webhook internal-network option does not apply. Never expose your database directly. The app controls which records and fields it returns.

## Read the data

- **Overview:** independent summary cards and compatible account totals. The period is shown explicitly. “Current records” new-account counts exclude deleted accounts; “retained signup events” counts require complete history. Different bases are never combined into one number. An unavailable app is not zero. These are accounts, not deduplicated people across apps.
- **Project → App data → Users:** accounts appear newest signup first. Open this view to see recent signups; refresh to fetch newer accounts. Search and signup-date filtering happen in the app when it supports those filters. This is a directory of current accounts, so a deleted account will no longer appear. Lists use live keyset pagination: later deletions, backfills and edited names can affect membership. Up to 500 visible users are retained in the active view.
- **Profile:** fixed identity/account fields, including an optional app-supplied phone number, at most twelve typed details, approved app links, and existing related local feedback/chat when the identity namespace was explicitly confirmed. Phone numbers are unverified display information and are not used to identify or link people. Missing profile/history is shown as unavailable or absent, never fabricated or matched by email or phone.
- **App activity:** choose up to twenty apps. Events are merged by exact UTC time and stable IDs. The feed pauses when a participating app cannot be read. Retry preserves the current browsing position; continuing without an app explicitly restarts the feed. Refresh also starts new snapshots. No globally simultaneous snapshot across independent apps is promised.

Activity displays twenty-five events at a time with a five-hundred-event view limit. Narrow the date range or restart when the limit is reached. Events recorded or backfilled after a snapshot appear on Refresh. Incomplete retained history is labeled. This is not a compliance audit trail, live monitoring or an analytics pipeline.

Recent accounts and signup events answer different questions. The Users view shows accounts that still exist, ordered by their original signup time. A summary can instead count retained signup events, including accounts later deleted, but only when the app can report complete history for the requested period. Signup events appear in Activity only if your app supplies them. SupaKnock does not generate signup notifications from these reads.

Web date range controls use UTC and an exclusive end date. Display timestamps use the installation timezone selected under **App activity → Display timezone** (default UTC); native date pickers use that timezone. The iPhone app exposes the read-only views under **Projects** and native connection setup under **Projects → your project → Project settings → App connection**.

### Add phone numbers to an existing connection

Have the app include optional `phone_number` in its profile response's `user` object. It may also include the field in directory `items[]`. Use nullable text up to 64 characters and preserve formatting and extensions. The value is private display information; SupaKnock does not verify it, send SMS, or use it to link identities. Omitted values show “Not provided.” Read the [phone contract](protocol/v1/README.md#optional-user-phone-number) or give your implementation agent the [Glean upgrade handoff](connected-apps-agent-brief.md).

Use a SupaKnock server and iPhone build containing phone support. Then deploy the app's adapter change and reopen the profile to make a fresh on-demand read. No new protocol version, capability, token, connection or data backfill is required when the endpoint and identity are unchanged. Image publication alone does not update a running installation.

## Refresh, privacy and failures

There is no background app polling, event ingestion, persistent data cache or remote-data notification stream. The server stores connection configuration, encrypted tokens, identity/link approvals, timezone and configuration audit metadata only. Remote responses and opaque continuation receipts live in bounded request/view memory. They never enter AI knowledge, notification payloads, browser storage or backups.

Backgrounding/closing a view cancels its work and clears remote rows; returning reads on demand. Server/account switches clear native state. A view expires after at most thirty minutes. Restart/restore invalidates old test and continuation receipts. Configuration changes/logout are checked again before returning a response. Data already delivered to another open client cannot be instantly erased without that client's next interaction.

Common states:

| State | What to do |
| --- | --- |
| Connection off/missing | Configure, test and enable it in project settings. |
| Unsupported view/filter | Implement that capability in the app, or use its other available views. |
| App authorization failed | Check/rotate the dedicated app token, save, test and enable again. |
| Invalid response/app identity changed | Check the adapter against the schemas and ensure this is the approved environment. |
| Snapshot expired/connection changed | Refresh the view; do not reuse an old continuation. |
| Busy/rate limited/timeout | Retry deliberately later. There is no hidden retry queue. |
| No linked support history | There may be no existing exact mapping, or namespace linking may be off. No account is created to fill the gap. |

Requests have a ten-second whole-read deadline; at most four outbound calls run installation-wide and two per connection, with a bounded queue. Read POSTs have a dedicated pre-authentication budget of 480 requests per minute per IP. Each administrator also has a budget of 240 read units per minute, shared across their sessions. Each outbound discovery or data call consumes one unit, so a normal view read uses two; a related-support lookup uses one. Unrelated mutation limits are unchanged.

Back up and restore using the [fixed maintenance procedure](operations.md) for your configured SQLite, PostgreSQL or Supabase database. Preserve the same application encryption key with the backup so saved app tokens can be decrypted. Remote users and events remain in your app and are not part of a SupaKnock backup. Startup does not probe apps. Restored enabled connections check identity and capabilities on their first explicit read. Local image verification and production deployment are recorded separately in [verification](verification.md).
