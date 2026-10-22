# Assistant tools and customer reports

Public read-only MCP tools and per-message podcast context are implemented as an optional extension. See [Connected AI tools and episode chat](mcp-tools.md) for setup, compatibility, source snapshots and widget examples. Customer-specific MCP grants remain deferred.

AI support exposes five fixed operations, each disabled until a native administrator explicitly enables it. There are no arbitrary commands, model-supplied destinations, or client-supplied identities. Customer chat uses the configured provider and model; administrator preview and section generation never use tools.

| Tool | Exact arguments | Effect |
| --- | --- | --- |
| `search_knowledge` | `query` | Retrieve up to six approved, active, compatible passages from this project. |
| `support_link` | `passage_id` | Resolve one currently eligible original passage and its verified citation; Markdown may have no URL. |
| `request_human` | None | Atomically switch to waiting for a human, cancel pending AI work, and stop generation. |
| `create_feature_request` | `title`, `description` | Prepare a private feature draft for the configured project board. |
| `create_bug_report` | `title`, `description` | Prepare a private bug draft for the configured project board. |

Every operation rechecks the job lease, customer credential, project, conversation mode, configuration, and its individual flag. Enabling AI does not enable tools or guest submission. Direct human controls remain available independently of AI and tool flags. Search results and source text are data, never authorization. Ordinary answers continue to require verified source citations with all tools off.

Tool arguments are exact objects with no extra fields and at most 16 KiB of serialized UTF-8 JSON. Search accepts at most 1,000 characters within the retrieval byte bound; passage IDs are at most 100 characters. A report title is nonblank and at most 200 characters; descriptions are at most 20,000 characters, subject to the smaller tool-argument byte limit during model preparation. Invalid Unicode and control characters are rejected. Provider call IDs must be unique within an attempt. Each batch is validated completely before execution, with at most one action and that action last. The generation loop permits at most eight tool calls and the configured 1–8 tool rounds plus one final call with tools disabled. Every provider round reserves usage separately; [generation bounds](ai-generation-contract.md) also apply.

## Review and explicit submission

A report tool creates no request, account, or guest owner. It prepares one draft per originating turn and report kind, then returns a server-owned acknowledgment that nothing has been submitted. The customer reviews the exact title, description, board, pending moderation visibility, and any guest tracking notice. Only a separate explicit confirmation creates the request. Customer text that asks for submission and model arguments claiming confirmation have no authority to submit.

Drafts carry an opaque ID, content version, SHA-256 confirmation hash, expiry, destination board name/ID, `can_edit`, `can_confirm`, and `copy_text`. The hash binds the exact content, board, kind, and configuration/mode versions. A draft expires after at most 30 minutes and never outlives its original credential. Pending edits and confirmations require the original authority to remain valid as well as the caller's current ownership. Configuration changes, human handoff, new customer messages, expiry, or revocation can invalidate a draft. The flags returned by reads are presentation hints; writes recheck authority atomically.

Material customer edits advance the version/hash and fence the originating worker while leaving the edited draft confirmable. Explicit cancellation and successful confirmation also fence that worker. Its late acknowledgment or retry cannot overwrite a customer edit or claim nothing was submitted after confirmation. A no-op edit preserves the version. New model attempts with different provider call IDs reuse the same pending draft only for identical text; changed model text conflicts instead of overwriting it.

## HTTP contract

All paths below begin with `/api/v1/chat/conversations/{conversation_id}`. IDs are UUIDs. These APIs require the owning current customer chat credential; native administrator sessions and project keys do not substitute for that credential. Website credentials also require their approved origin. Response fields exclude customer/session identifiers, external IDs, credentials, IP addresses, and private history.

| Method and suffix | Input | Result |
| --- | --- | --- |
| GET `/proposals` | Optional `limit`, `offset` | Private draft page. |
| GET `/proposals/{proposal_id}` | — | Current draft and copyable text. |
| PUT `/proposals/{proposal_id}` | JSON `version`, `title`, `description` | Edited draft and current hash. |
| POST `/proposals/{proposal_id}/cancel` | JSON `version` | Cancelled draft. |
| POST `/proposals/{proposal_id}/confirm` | JSON `version`, `draft_hash`, `client_request_id` | Durable private report receipt. |
| GET `/reports` | Optional `limit`, `offset` | Private receipt page. |
| GET `/reports/{report_id}` | — | Current moderation/lifecycle and ownership controls. |
| PUT `/reports/{report_id}` | JSON `version`, `title`, `description` | Edited report receipt. |
| DELETE `/reports/{report_id}` | Query `version` | Deleted receipt with a durable tombstone. |

Versions are positive integers; JSON booleans are not versions. Confirmation hashes are 64 lowercase hexadecimal characters and client request IDs are UUIDs. Extra JSON fields are rejected. Pages default to 25 items, accept 1–100, and bound offset to 1,000,000. Customer draft/report edits accept the full 200-character title and 20,000-character description. Only those two PUT routes have a 400 KiB body allowance, including escaped JSON; ordinary chat retains its 32 KiB limit. Stale versions and changed confirmation content return explicit conflicts.

The receipt `id` and route `report_id` are the original proposal ID. The separate `request_id` identifies the created request and is returned only in this scoped receipt. Receipt versions follow the underlying request, independently of draft versions. A removed request retains a private deleted receipt; a deletion through this API preserves its last content and advances its tombstone version. Receipts expose current moderation, lifecycle status, `accepted_at`, and edit/delete availability. Acceptance permanently locks creator edits and deletion even if lifecycle or moderation later changes.

Confirmation commits the pending request, receipt, applicable immutable event/delivery work, original submission address, and any guest owner together. Repeating the exact originally confirmed draft version/hash with the same submission ID returns the existing receipt. A new unused submission ID for that same confirmation also resolves to the original receipt and becomes bound to it; at most 100 IDs may be bound per report. Reusing a bound ID for different content or a different proposal conflicts. Every replay reauthorizes current ownership and credentials. It does not create another request, change the original address, or require creation flags to remain enabled.

## Guests, destinations, and existing ownership

Native and authenticated external customers reuse their existing project-scoped identity without normalization or linking. Guest submission additionally requires `allow_guest_reports`, the original website visitor credential, and its origin. A credentialless internal guest owner is created only on successful confirmation. This owner grants no login or administrator access; the anonymous chat identity stays anonymous. Guest-to-account linking remains unavailable.

Before confirmation, the guest notice explains that account tracking requires signing in and submitting on the feedback page, and that guest reports stay with this browser's chat. After confirmation, the notice explains that private status remains available only while the chat credential is valid and will not appear in an account. A guest can review and copy a prepared draft or open the feedback page when guest submission is disabled. The API never transfers or submits that copy automatically.

`feedback_url` resolves from the current report-kind override, then the project's general feedback URL, then the native destination board. The server adds no identity or draft parameters to custom URLs. Existing receipts also use the current configured destination. `public_url` is supplied only for an accepted native request when no custom destination is configured; custom feedback destinations remain authoritative after acceptance. Clients should use these explicit fields rather than invent URLs or treat a pending receipt as publicly visible.

Turning off AI, a report tool, or guest submission prevents new creation but preserves existing owners' scoped receipt reads, edits, and deletion. Human mode changes likewise do not remove ownership. These operations still require a current valid credential, exact ownership, a current version, and the permanent acceptance lock. Guest access remains bound to its original visitor credential and origin; authenticated owners may use renewed current credentials. A replay can return an already committed receipt after creation is disabled, but cannot use an expired or revoked credential.

Draft preparation is limited to 30 per customer and 500 per project per UTC day. New guest reports are limited to five per customer and 100 per project per day; authenticated reports to 20 per customer and 1,000 per project per day. Exact receipt replays do not consume a new report allowance. Provider generation has its separate per-project request/token allowance.
