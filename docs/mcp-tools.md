# Connect AI chat to MCP

SupaKnock can call an application's public read-only MCP tools from its existing AI chat. This is opt-in per installation connection, per project and per tool. Existing projects continue to use support knowledge. This guide describes the implemented SupaKnock side; your application still needs to implement and host its MCP endpoint.

## Set up a connection

1. As a native administrator, open **Connected AI tools** in account administration (also available natively on iPhone).
2. Add a connection name, an exact public HTTPS MCP endpoint and, if required, a dedicated bearer token. Enable its availability for project approval. Tokens are encrypted and never returned to the browser or phone after saving.
3. Add the exact HTTPS origins permitted for citation links, such as `https://podcasts.example.com`. Paths do not belong in this list. Links outside these origins are omitted; their text can still be evidence.
4. Select **Discover tools**. Review descriptions and input schemas. Discovery does not call tools or enable them for visitors. Unsupported schemas/tools are shown with a reason.
5. Run a read-only test using public synthetic arguments. This calls the service using the saved credential and displays the result only to the administrator. Do not put secrets in tool names, descriptions, schemas, test inputs or returned data.
6. Open a project's **AI support → Connected tools**. Choose individual tools and an answer source mode, then save. Only approve tools that the remote server actually restricts to visitor-safe reads; annotations are not an enforcement mechanism.
7. Select the project's generation provider/model and account keys in **Models & keys**. Verify **tool access** for the exact keys/model. Explicitly enable project AI after its readiness checks pass.

Source modes:

| Mode | Behavior |
| --- | --- |
| Support knowledge | Default. Uses approved indexed knowledge; remote tools are not advertised or called. |
| Support knowledge and connected tools | Uses whichever approved evidence paths are ready. A source outage is not evidence of a business fact. |
| Connected tools only | Does not require SupaKnock embeddings or indexed knowledge. Local knowledge tools are hidden. |

Knowledge authoring/FAQ generation still requires approved indexed originals and does not call MCP. Human support, business hours, manual follow-up email and reviewed report submission continue independently. The web project tab shows each evidence path's readiness; the saved discovery date is not a promise that an upstream service is currently available.

Connection edits invalidate discovery and project approvals; rediscover and explicitly save the desired selections again. Removal deletes the saved secret and bindings but retains existing chat citations. Changes, archived projects, expired credentials, human takeover and topic rejection fence pending work. No automatic endpoint/provider fallback occurs.

## Transport and schema compatibility

Implement **MCP 2025-11-25 Streamable HTTP** at one public HTTPS URL. SupaKnock performs `initialize`, `notifications/initialized`, bounded `tools/list`, then `tools/call`. JSON and bounded SSE responses are supported. Optional `Mcp-Session-Id` is scoped to the attempt; cleanup uses best-effort DELETE. No SSE reconnect/replay, persistent subscriptions, OAuth, sampling, roots, elicitation, tasks, audio/image/resource blocks, remote writes or user delegation are supported.

The endpoint must not redirect, include URL credentials/query parameters, or resolve to private, loopback, link-local or metadata networks. DNS is checked and pinned for each request with TLS hostname verification. Visitor cookies, IP addresses, follow-up email and chat credentials are never attached to requests. A service bearer token is **not** a customer login.

Tool names use ASCII letters, digits, `_`, `.`, `-` (1–128 characters). Descriptions are at most 2,000 characters. Input schemas have an object root. The supported subset is explicit `type` (`object`, `array`, `string`, `integer`, `number`, `boolean`, `null`), `properties`, `required`, `additionalProperties: false`, `items`, `enum`, `title`, `description`, length/item/numeric bounds. References, type unions, compositions, regex patterns and unknown keywords are rejected, with a schema path. Omit optional unavailable properties instead of using nullable type unions. Object schemas are closed, including when `additionalProperties` is omitted; always specify it explicitly for interoperability.

Declare `readOnlyHint: true` and `destructiveHint: false`. Contradictory declarations cannot be approved. Changing a reviewed tool's definition requires administrator review again. Newly discovered tools remain unselected. Live discovery validates approved digests before a connection's first call in each answer attempt.

Return text blocks and/or a `structuredContent` object. When both are present, the text must be the JSON serialization of that same object. If `outputSchema` is advertised, structured content must validate against it. `isError: true`, conflicting representations, oversized responses and unknown content blocks yield unavailable results, never evidence. Generic tools produce bounded text evidence with the administrator's connection label and no inferred hyperlinks. Podcast tools use the richer contract below.

Limits: 50 account connections, 4/project, 100 discovered tools across at most 10 pages, 8 selected tools total including built-ins, 8 remote reads/attempt and 16/durable turn across retries. Metadata reads count. Discovery is bounded to 10 seconds; individual RPCs to 10 seconds; cumulative remote work to 25 seconds/attempt. Results are capped at 256 KiB wire and 96 KiB normalized data; input arguments at 16 KiB, schema depth 8, JSON depth 12. Evidence uses one twenty-source limit across retrieval, answer checking, correction and saved citations, including mixed local and remote sources. Each complete serialized tool source is bounded to 4 KiB (excerpts up to 2,000 characters); combined tool evidence is bounded to 96 KiB. Duplicate evidence within the same approved connection and attempt reuses its source ID. Identical reads reuse authorized attempt-local evidence without another network call; revocation and version checks still apply. A turn that fills its source allowance answers from the selected complete excerpts and explicitly marks omitted tool evidence as partial. It never clips an excerpt or treats a bounded selection as full coverage.

Admission is shared across native tests and chat work in this single-instance process: 4 active operations, 2/connection and 16 waiting; 30 reads/conversation/minute, 120/project/minute and 240/connection/minute. These transient rate counters reset on restart; durable call receipts still bound retries. A bounded upstream `Retry-After` sets a shared connection cooldown. A failed remote read never quarantines a model-provider key.

## Optional example: podcast contract, version 1

Select **Episode on the visitor's page** for the project binding and approve exactly these three tools. SupaKnock removes `episode_id` and `transcript_revision` from the model-facing inputs, supplies them itself, and refuses another episode/revision in the result.

- `get_episode(episode_id: string)`: resolve canonical public identity and metadata.
- `search_transcript(episode_id: string, transcript_revision: string, query: string, limit?: integer)`: search within that episode. Bound query to 1,000 characters and limit to 1–6.
- `get_transcript_window(episode_id: string, transcript_revision: string, start_seconds: number, end_seconds: number)`: return a contiguous window, at most 120 seconds. Times are seconds, never milliseconds.

Every result includes `contract_version: 1`. Use safe statuses `ok`, `unavailable`, `transcript_unavailable`, `timing_unavailable`, or `revision_changed`. Missing and inaccessible episodes both return `unavailable`. Never start transcription through these reads. Check public visibility and bearer-token scope on every operation, including canonical aliases.

Example `get_episode` structured content:

```json
{
  "contract_version": 1,
  "status": "ok",
  "episode_id": "canonical-episode-id",
  "requested_episode_id": "alias-from-page",
  "title": "Small habits, lasting change",
  "show_title": "Everyday Ideas",
  "url": "https://podcasts.example.com/episode/canonical-episode-id",
  "duration_seconds": 1800,
  "transcript": {"status": "ready", "revision": "immutable-revision-1", "coverage": "complete", "timing_kind": "aligned"}
}
```

`requested_episode_id` must exactly equal the submitted ID when canonical identity differs. IDs remain opaque and case-sensitive. Titles are at most 200 characters. The revision must identify the transcript snapshot, not a mutable pointer. Metadata can be answered without a transcript; speech claims cannot be grounded in a generated description/summary. V1 cites title/show/duration/publication metadata; chapter outlines and generated summaries are not promoted into speech evidence.

Example search result:

```json
{
  "contract_version": 1,
  "status": "ok",
  "episode_id": "canonical-episode-id",
  "transcript_revision": "immutable-revision-1",
  "coverage": "partial",
  "matches": [{
    "segment_ids": ["segment-17", "segment-18"],
    "text": "The host recommends taking a walk every morning.",
    "start_seconds": 42,
    "end_seconds": 49,
    "timing_kind": "aligned",
    "url": "https://podcasts.example.com/episode/canonical-episode-id?t=42"
  }]
}
```

Return at most twenty exact, meaningful excerpts of at most 1,600 UTF-8 bytes each. Each has 1–100 revision-scoped segment IDs. SupaKnock defaults searches to six excerpts when no limit is supplied; the remote tool’s reviewed schema may impose a smaller limit than twenty. Use `segments` instead of `matches` for a window and include truthful `window_complete` and optional `next_start_seconds`. Coverage is `complete | partial | unknown`; search hits never establish completeness of an episode summary. Omit both times when `timing_kind` is `unavailable`; approximate times are visibly labelled. Do not invent speakers. Empty matches are a successful search, not proof that a topic was never mentioned.

A revision change permits one fresh metadata/read cycle before transcript evidence has been accepted. If the revision changes after earlier transcript evidence, the turn is stopped instead of mixing snapshots. The visitor can ask again. Repeated revision changes or exhausted limits are unavailable, not empty factual results.

## Optional example: podcast widget integration

Use the existing widget and project; support and episode questions share one conversation. No new browser secret is required.

```html
<div id="podcast-chat"></div>
<script src="https://YOUR-SUPAKNOCK/widget.js"></script>
<script>
const chat = SupaKnockChat.mount('#podcast-chat', {
  server: 'https://YOUR-SUPAKNOCK',
  project: 'YOUR_PROJECT',
  presentation: 'bar',
  context: {version: 1, resource: {kind: 'podcast_episode', id: 'EPISODE_ID'}},
  onSourceActivate(source) {
    // Optional. Return true only after your app validates and handles the seek.
    // Validate source.kind, source.episodeId and source.seconds against your player.
    return false; // SupaKnock opens the validated source URL instead.
  }
});
chat.setContextProvider(() => ({
  version: 1,
  resource: {kind: 'podcast_episode', id: pageEpisode.id},
  ...(player.episodeId === pageEpisode.id
    ? {position_seconds: player.currentTime} : {})
}));
// Your existing episode button can call this; opening sends no question.
chat.open({intent: 'episode'});
// A support entry point opens the same conversation without changing permissions.
// chat.open({intent: 'support'});
// On routes without an episode, clear the callback and context:
// chat.setContext(null);
</script>
```

`setContext(value)` replaces the callback. `setContextProvider(callback)` supersedes static context for future sends; return null for no episode. The callback is synchronous and evaluated once per new deliberate send. Invalid context or callback errors stop sending with an actionable error; retrying the same pending message keeps its original context. Changing page, timestamp or another browser tab never retargets an in-flight message. IDs are 1–200 characters; position is finite 0–86400 seconds; the entire context is at most 2 KiB. No arbitrary instructions, URLs, titles, identities or authorization fields are accepted. Older clients omit context and remain unchanged.

A bubbling `supaknock:ready` event exposes `{controller}` on mount. A user-initiated `supaknock:seek` event exposes `{episodeId, seconds, url}`; `preventDefault()` signals that the host handled the action. Prefer the callback above for simple integration. Neither citations nor model output execute JavaScript or seek automatically. Without a host handler, canonical timestamp links open in a separate tab. Mobile/native chat uses the same validated links.

REST message sends and WebSocket message frames accept the same optional `context`. HTTP message ID/body/email/context retries are idempotent; WebSocket retries bind ID/body/context and continue to save follow-up email only through HTTP. Changing context under the same message ID conflicts. Replies keep the trigger's context. `tool_sources` is additive alongside existing knowledge `citations`, and contains server-validated excerpts, URLs, timestamps, canonical identity/revision/segment IDs and observation time.

## Storage, backups and future authentication

Only snippets actually cited in a committed answer are saved with the message. Unused/failed excerpts stay in bounded attempt memory and are discarded by the worker. No transcript import, embedding, remote-data cache or playback telemetry is created. Body-free diagnostic call receipts retain counts/fencing and are pruned after 30 days when new calls are reserved. Existing chat source snapshots survive backups even after the connection is removed. Protocol sessions and transient test outputs are never durable; a restored installation must live-discover before any new remote call. Keep the application encryption key with your ordinary backup.

Public v1 deliberately cannot access subscriber-only/user-specific data. A later authenticated extension must exchange a real application login for a short-lived scoped grant, rechecked for project, customer, session, connection and revocation. Browser episode IDs, shared MCP bearer credentials and MCP session IDs provide no customer identity. Keep the administrator connected-app summaries/users protocol and its secrets separate.

Implement the dedicated podcast MCP service in the application that owns the episode data. The frontend should update the singleton widget on route changes and use the player's matching-episode position. Convert millisecond offsets to seconds when necessary. Persist stable transcript revisions and segment IDs, enforce current public visibility, and generate canonical episode timestamp links. Issue a dedicated read-only service credential; do not reuse broader administrative credentials.

## Public integration resources

The [website guide](https://supaknock.com/developers/mcp) includes a readable setup walkthrough, [podcast JSON examples](https://supaknock.com/guides/podcast-mcp-examples.json) and an [agent handoff checklist](https://supaknock.com/guides/podcast-mcp-checklist.md). The public Markdown copy excludes repository-specific deployment paths. Check the installed server version before enabling the feature.

General integrations can use the [MCP agent checklist](https://supaknock.com/guides/mcp-checklist.md); the podcast resources are optional examples, not required tool names or browser context for other applications.
