# Public and private boards

In **Project settings → Boards**, choose **Visibility** when creating or editing a board. The iPhone app offers the same choice in New board and Board details. Existing boards remain public after upgrading; changing visibility uses the board's current version and rejects stale edits. Older API clients that omit visibility preserve the saved value when editing.

- **Public:** existing behavior. Anyone with the project or board link can browse accepted requests. Pending and rejected requests retain their existing owner/manager privacy.
- **Private:** hidden from public and participant board listings. Anonymous direct board/request reads return 404. A signed-in participant with a board link can submit and see their own accepted requests there, and all their own submissions under My requests. Other people's requests and history are inaccessible, including through direct links and voting endpoints.

Private does not mean archived. Authorized intake remains available: native signed-in submission, backend project credentials with `participate`, and explicitly enabled AI chat reports with customer review/confirmation. Archiving still pauses intake; restoring preserves the saved visibility. Accepting a private request never makes it public while its board is private. Changing the board back to public exposes its accepted requests again; pending/rejected requests remain private.

Administrators and project `manage` credentials retain management access. A trusted backend project credential with `read` can list/read private boards and accepted requests for its project, so existing app integrations continue to work. It must enforce its own customer-facing visibility rules and remain on the backend. An opaque external user ID alone grants no access. Private board names/descriptions can be read by signed-in users with a direct link; avoid using them for confidential report content.

Webhook delivery and administrator notifications continue normally, retaining their existing payload/privacy contracts. Guest chat report tracking remains tied to the original chat credential; private reports do not receive a public request link. There is no new guest identity, account linking, invitation flow or board membership role.

## API

`POST /api/v1/projects/{project}/boards` accepts `visibility: "public" | "private"` (default `public`). `PATCH /api/v1/boards/{board}` accepts the same optional field alongside `name`, `description` and `version`; omitting visibility preserves it. Board responses include `visibility`.

```json
{
  "name": "Bug reports",
  "description": "Send a problem to our team.",
  "visibility": "private",
  "version": 3
}
```

Existing intake endpoints and credentials are unchanged. Public/participant board filtering occurs before pagination. Private request access is enforced by the API, not merely hidden in the interface. Responses are not cacheable; already downloaded copies of formerly public content cannot be withdrawn.

SQLite migration 026 and PostgreSQL migration 006 add the setting without modifying existing requests, acceptance dates, votes or immutable delivery snapshots. Provider selection still does not transfer data.
