# Optional admin push

Push is off by default. Normal feedback, webhooks, and live chat work without the relay.

SupaKnock’s relay service is available at **https://relay.supaknock.com**. Your installation uses it to send iPhone admin notifications. Feedback, chat and your application’s API calls still go to your own server.

1. Open **Admin → Push notifications** on your installation.
2. Set the relay URL to `https://relay.supaknock.com` (without `/api/v1`) and enable push.
3. Sign in to the [iPhone app](https://supaknock.com/iphone), enable notifications, and save device preferences.

For API setup, send `PUT /api/v1/admin/push` to **your installation** with an administrator session and this JSON body:

```json
{"enabled": true, "relay_url": "https://relay.supaknock.com"}
```

You can instead deploy a compatible relay with its own Supabase schema and APNs configuration. Choose that relay’s HTTPS origin before enrollment. Existing connections to `suggestion-box-relay.doomo.dev` remain supported; it is an alias of the same service.

The installation generates an identity and enrollment proof locally. Only explicit enablement contacts the relay. The relay issues a unique credential; the installation encrypts it using its existing persistent application key. The phone receives only its native admin session, never the relay credential, Supabase key, or APNs provider key. Automatic enrollment identifies a caller; it cannot prove that the caller runs official software.

The relay receives a device token, environment, and installation/event/recipient/resource identifiers for each job. It generates fixed generic notification text, with no request title, description, chat body, password, or email. Local device registrations belong to the authenticated administrator's current session. Logout, password change, session expiry, token replacement, opt-out and account changes suppress subsequent sending. An invalid-token result can disable only the exact registration generation that produced it.

## Delivery and recovery

Request creation and eligible device jobs commit in the same database transaction. Committed visitor messages enqueue chat alerts, coalesced to one per conversation per five minutes. Opening a widget and typing do not send push. Local jobs survive restart and use 60-second fenced leases, ten-second HTTP timeouts, exponential backoff with jitter and a 24-hour send window. Temporary failures retry up to eight consecutive times; successful relay responses reset that failure count. Manual retries are bounded to three and keep the same event and recipient IDs. Relay acceptance and Apple acceptance are separate from visible delivery on a phone.

**Admin → Push notifications → Delivery history** shows bounded, redacted states and allows eligible failed jobs to be retried. `succeeded` means APNs accepted the notification, not that the device displayed it. After fixing enrollment/credentials, retry eligible failures. The relay can independently pause enrollment or sending and revoke abusive installations.

Disabling push marks outstanding jobs for cancellation immediately; re-enabling does not revive those jobs. Cancellation by event/recipient key also covers a lost HTTP receipt. The relay stores a temporary cancellation record so late or retried sends remain suppressed. Work already accepted by APNs cannot be recalled. Cancellation failures remain inspectable; up to three manual cancellation retries are permitted within 48 hours of local job creation. Session revocation is checked when a due job is claimed, immediately before sending, and after its result returns.

Local target snapshots are encrypted, cleared on successful/cancelled/expired completion, and cleared after the 24-hour send window on the next worker cycle. Diagnostic jobs are retained 30 days. The relay also erases terminal target ciphertext and does not maintain a permanent device directory. Backups can retain encrypted historical data according to the operator's retention policy.

## Settings and deployment

Relay calls require an HTTPS origin, use DNS validation and address pinning, and never follow redirects. `ALLOW_INTERNAL_RELAY=true` is an explicit development-only option for intentional private HTTP/HTTPS endpoints; metadata/link-local destinations stay blocked. The relay origin is fixed once enrollment is prepared. Use the correct origin initially; do not reuse a proof/credential with another relay. Rotation recovers using the stored enrollment proof if the response was lost. Rotation does not reset relay quotas.

Keep `/data` and its `application.key` together. The supported backup operation preserves the installation identity, proof, credential, sessions, registrations and jobs. A cloned volume has the **same** installation identity: use restoration as a replacement, never run both copies simultaneously. Separate installations start with separate empty volumes. Losing the encryption key requires restoring the matching secret; startup never silently replaces it.

Native API sessions use `POST /api/v1/auth/native/login` and `Authorization: Bearer ns_…`, expire after seven days, and are administrator-only. Cookie sessions still require CSRF. Project API keys cannot administer push or impersonate native admins. See [OpenAPI](openapi.json) for the implemented operations and [contracts](push.md) for relay details.

## Connected-app data

[Connected-app summaries and activity](connected-apps.md) are manual/on-navigation reads. Remote app events and profiles are not sent to the relay, APNs or the shared notification inbox. No relay credentials are reused for app connections.
