# Shared administrator notifications

The bell in the web header and iPhone app opens one notification inbox for the selected installation, spanning all projects. Every native administrator sees the same read/unread state. Marking an item read, unread, or marking all read changes it for the whole team. Opening the inbox alone does not acknowledge anything; opening a notification acknowledges it and navigates to its request or conversation.

The inbox includes new requests, resubmitted requests, and incoming customer chat messages (anonymous or authenticated). Administrator replies and routine moderation/status edits do not generate inbox items. A retried chat message produces one notification. Request titles are event snapshots; chat notifications use generic text and never include message bodies, customer identifiers, or credentials. Only administrators can read or acknowledge notifications; project API credentials and customer sessions cannot access them.

Use All or Unread to filter, and the read/unread action on each row to acknowledge or revisit work. Each item shows its project and timestamp. Deleted requests retain an acknowledgement record with an unavailable destination. The shared bell is separate from approving requests, per-admin chat read positions, push preferences and previously delivered Apple notifications. It works without enabling the push relay.

## API

Use a native administrator bearer session, or the administrator website session with its usual CSRF protection for mutations.

| Operation | Endpoint | Input |
| --- | --- | --- |
| List | `GET /api/v1/admin/notifications` | `unread_only=false`, `limit=25` (max100), `offset=0` |
| Badge | `GET /api/v1/admin/notifications/summary` | — |
| Set acknowledgement | `PUT /api/v1/admin/notifications/{id}/read` | `{"read":true}` or `{"read":false}` |
| Acknowledge displayed snapshot | `POST /api/v1/admin/notifications/read-all` | `{"through_sequence":42}` |

The list returns `items`, pagination fields, `unread_count`, and `through_sequence`. The summary returns the last two fields. Items contain a stable UUID, sequence, type, project ID/name, title, creation/read timestamps, typed resource kind/ID, relative page URL, and `resource_available`. Sequences order notifications newest first and support bounded acknowledgements; resource IDs remain UUIDs.

For “Mark all read,” send the `through_sequence` from the displayed list. Notifications arriving after that snapshot remain unread. Repeating a read operation is idempotent. Explicit concurrent read/unread choices use the last committed write. Offset pages can move when new items arrive; clients should deduplicate by notification UUID and refresh their list. The web bell refreshes while visible, so another administrator's changes appear without a full page reload. The iPhone inbox refreshes in the foreground and scopes all requests and cached state to the selected server and signed-in session.

The runtime-generated [OpenAPI specification](openapi.json) describes the complete response models.

## Persistence and upgrade

SQLite migration `007_admin_notifications.sql` adds the shared inbox and indexes. Every new notification commits in the same transaction as its business event; a rolled-back submission/message leaves no notification. The adapter rechecks the administrator's live session inside read and acknowledgement operations. Existing supported backups include the inbox and shared read timestamps automatically.

On the first upgrade, the migration adds the most recent created/resubmitted event for each pending request and the latest customer-message event for each open conversation whose latest message is from a customer. It does not replay all historical activity. Earlier chat messages that predate recorded chat events cannot be backfilled. The migration runs once and preserves acknowledgements on later starts. Take the usual [backup before upgrading](operations.md).

## App activity is separate

The optional [connected-app timeline](connected-apps.md) fetches app-owned events only on demand. It does not create notification inbox items, alter shared read state, schedule polling or send push notifications. Installation Account activity continues to record successful local account/signup/login history.
