# Archive and restore projects and boards

Native administrators can archive a whole project or an individual board. Archiving hides it from participants and stops new activity while preserving its data. Permanent project/board deletion is not implemented.

## Web and iPhone

On the web, open **Project settings → your project → Project details → Archive project**. Individual boards have **Archive board** under the project's **Boards** tab. Both actions explain their effects before confirmation.

Find archived projects using **Show projects → Archived projects** in administration. Open one to restore it or browse its preserved boards. For individual boards, use **Boards → Show boards → Archived boards** and choose **Restore board**.

On iPhone, use **Projects → your project → Project settings → Archive & restore**, or open a board and choose **Archive & restore**. The project and board lists have Active/Archived filters. Administrators can still open archived requests and conversations; mutation controls are disabled.

## What is preserved and paused

- **Board archive:** retains requests, acceptance dates, votes and immutable history. It blocks new submissions, votes, moderation and creator/admin content changes. Pending AI report proposals targeting that board are cancelled. Other boards, chat and unrelated AI jobs continue normally.
- **Project archive:** hides the project and all its boards from non-administrators, removes them from normal lists and moderation queues, stops feedback/chat activity, cancels queued/running project AI jobs and pending report proposals, and blocks connected-app data reads. Administrators retain feedback and chat history access. Knowledge, generated drafts, source originals, credential bindings and settings remain stored; restore the project to access its AI administration.
- **Restoration:** keeps the same IDs and URLs. Saved chat, AI and connected-app settings become effective again. Restoring a project does not restore a board that was archived individually. Restore the project before restoring an individually archived board. Interrupted AI jobs must be started again.
- **Chat credentials:** project archiving revokes existing anonymous and authenticated customer chat sessions. They stay revoked after restoration; clients must establish a fresh session. Durable authenticated customer ownership/history is preserved. Anonymous credentials remain browser-scoped and do not gain account rights.
- **Existing deliveries:** committed webhook/push jobs retain their original delivery contract and may still deliver. Archiving does not erase events, cancel delivery history or rewrite snapshots. Shared account-level AI keys are never deleted or disabled by a project archive.

Archiving is reversible organization, not credential revocation or data erasure. An administrator can restore the resource; use dedicated key/session controls when access itself must be revoked permanently.

## API contract

Both operations require a current native administrator session (browser session plus CSRF, or native bearer session). Project integration credentials and project managers cannot archive or restore resources.

```http
PUT /api/v1/projects/{project}/archive
Content-Type: application/json

{"archived": true, "version": 3}
```

```http
PUT /api/v1/boards/{board}/archive
Content-Type: application/json

{"archived": false, "version": 4}
```

Use the resource's latest version. `archived` is a strict boolean. A state transition increments `version`, sets/clears `archived_at` and records an immutable local audit entry atomically. A repeated request with the current version and unchanged state is a no-op; an old version returns `409 conflict`. Reload before retrying rather than silently overwriting a concurrent edit.

Project and board list endpoints accept `archive=active` (default), `archive=archived`, or `archive=all`. The latter two require native-admin access. Filtering happens before pagination. Board archive filters describe the board's own state; `project_archived_at` tells an administrator whether an otherwise active board is paused by its parent.

Native admins can read archived requests via their board using `GET /api/v1/boards/{board}/requests?moderation=all`; the optional moderation filter also accepts `accepted`, `pending`, or `rejected`. `all`, `pending`, and `rejected` require native-admin access. `Request.archived` indicates inherited or direct archival and disables write controls. Archived resources are concealed as `404` from non-admin reads. Atomic writes rejected due to archival return `409 resource_archived`; revoked chat credentials retain their normal authentication errors.

## Storage and upgrade

Migration 021 adds nullable archive timestamps and an immutable local `resource_archive_events` audit table. Existing installations migrate with every project and board active. Parent archival never overwrites child archival timestamps. Session rechecks, optimistic versions and activity gates run inside SQLite write transactions. Project AI/connection generation changes fence late work even if archive and restore happen before a response returns.

The existing single-volume backup/recovery operations preserve archive states, relationships and audit records. No external store, background cleanup or automatic deletion is added. The [runtime OpenAPI export](openapi.json) is authoritative for request and response shapes.
