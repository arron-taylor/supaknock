# Administrator previews and derived knowledge

AI authoring is private native-administrator work. All routes below use `/api/v1/projects/{project}/ai`, existing native-session checks, and CSRF/origin protection for cookie mutations. Project API keys, participant sessions and chat credentials cannot configure personality, start a preview, inspect its private results or edit derived sections. A project AI configuration and compatible approved knowledge must be ready, but customer AI may remain off. Starting authoring never changes any opt-in.

## API

| Method and suffix | Request and behavior |
| --- | --- |
| `POST /previews` | `{client_request_id, version, question}`; queue a tools-disabled grounded preview and return 202 with its run. |
| `POST /knowledge/sources/{source_id}/sections/generate` | `{client_request_id, version, source_version, page_ids?}`; queue sections/FAQs from all selected eligible passages, optionally limited to selected website pages. |
| `GET /authoring-runs` | Paginated private history; optional `kind=preview` or `kind=sections`. |
| `GET /authoring-runs/{run_id}` | Inspect progress, fixed safe errors, result, citations, coverage, editable drafts and staleness. |
| `POST /authoring-runs/{run_id}/cancel` | `{version}`; fence unfinished work. Completed work retains its actual terminal state. |
| `POST /authoring-runs/{run_id}/retry` | `{version, client_request_id}`; explicitly resume eligible unfinished batches without duplicating saved drafts. |
| `GET /knowledge/sections` | Paginated/searchable drafts; use this beyond the bounded run preview. |
| `POST /knowledge/sections/{section_id}/approve` | `{version}`; adopt the saved reviewed draft as an independent approved Markdown article and queue indexing. |
| `PUT /knowledge/sections/{section_id}` | `{version, title, body, category, citation_ids}`; save a versioned internal edit. |
| `DELETE /knowledge/sections/{section_id}?version=…` | Tombstone an internal draft while preserving its edit history. |
| `GET /personality` | Read `{version, filename:"soul.md", content}` without generation. |
| `PUT /personality` | `{version, filename:"soul.md", content}`; save versioned personality text without generation. Filename defaults to `soul.md`. |

`client_request_id` is a UUID. `version` on a generation request is the current AI settings version; `source_version` is the selected source version. Identical retries return the original run and its current state, without another provider request. A reused ID with different inputs conflicts. Run and section versions are separate from AI settings versions.

Preview questions are nonempty, at most 1,000 characters, and must fit the local retrieval byte limit. A completed preview has `{body, citation_ids, knowledge_version, status}`, where status is `answered`, `insufficient`, `refuse`, `clarify`, `greeting` or `unavailable`. Citation details contain only validated original chunk/source/revision IDs, a plain label, an original excerpt and a validated website URL when available. Markdown citations do not publish the full original file. Previews create no customer conversation, chat message, report, handoff or notification.

## Sections, FAQs and provenance

New generation runs select eligible original passages from the whole approved source, or from explicitly selected website `page_ids` (1–50 distinct page IDs). Current material selection has a defensive 10,000-passage ceiling; coverage reports selected and total counts rather than implying unlimited processing. The earlier first-20-passage behavior applies only to legacy runs without the new full-source scope. New runs use batches of five passages. The worker requests at most three concise entries per batch, saves successful batches atomically, and yields after four calls so remaining work can be recovered.

Coverage includes scope/page selection, selected and total passages, batch size, completed and total batches, and completed passages. A later failure after saved batches presents `partial`; successful drafts remain available. An explicit versioned, idempotent retry resumes eligible unfinished work, while changed generation settings/evidence require a new run. A run response previews at most 100 sections and exposes `section_count`; use the paginated sections endpoint for the complete draft collection.

Each editable section has its own ID and version, kind (`section` or `faq`), title, body, category and up to six original citation IDs. Edits have limits of 200, 4,000 and 100 characters for title, body and category. Immutable snapshots retain original generation and edits. Deletion uses a versioned tombstone. Supporting references are attached to each draft and can be inspected without manually assembling ordinary citation sets.

Draft generation/editing alone never changes the search corpus. **Explicit approval** of a saved draft creates a separate approved Markdown original, queues local indexing, and records immutable publication provenance (migration 018). Approval is idempotent per saved draft version; current source/citation eligibility and administrator authority are rechecked. The original website/source remains; later refreshes do not overwrite the independent article. `published_source_id` identifies its adopted source. The article is usable for retrieval only after successful indexing.

Run-level configuration/knowledge staleness remains inspectable, but a completed draft's edit/approval eligibility follows its own current cited evidence. Unrelated settings changes alone do not invalidate that evidence. Revoked, deleted or changed supporting evidence still blocks use. Private history retains original citation details while current retrieval immediately excludes ineligible sources.
## Personality

`soul.md` is UTF-8 text with at most 20,000 characters (80 KiB), without unsupported control characters. The targeted endpoint and full settings update accept a bounded JSON body that also accommodates escaped Unicode. Empty content resets the personality. No executable files, uploaded code or filesystem paths are accepted.

Personality uses the same optimistic configuration version as provider/model/tool settings, preserving unrelated fields and the encrypted key. A changed personality cancels older generation work. A no-op write preserves the current version. Tone, terminology and brevity may change; access control, citations, honesty and tool permissions remain fixed application rules. The subsequent administration UI can read a local `soul.md` file as text and send this API payload.

## Durable execution

The authoring worker claims only `preview` and `sections` jobs and uses the shared [grounded generation service](ai-generation-contract.md). Each operation has a fenced lease, current native administrator authority, configuration and knowledge versions, and a selected-source version for sections. Embedding/provider calls occur outside SQLite transactions. Each successful batch commits its draft rows, initial history, coverage checkpoint and result under the fenced lease; the final batch completes the job. Legacy single-result runs retain their original atomic completion path.

Every preview and section generation advertises no assistant tools, regardless of configured tool switches. Model output cannot choose another provider, enable a tool, fabricate a citation ID, publish a report or invent a support URL. Source passages and personality are untrusted prompt data. Citation validation establishes provenance, not proof that every generated sentence is correct; administrators must review derived drafts, and release quality evaluation remains separate.

Provider allowance is reserved before dispatch. The shared service records reported usage separately; uncertain calls retain their reservation. Recoverable retryable failures use bounded delays and at most three job attempts. Retries can incur another provider request, but cannot duplicate a committed result. Cancellation and session/configuration/source changes prevent late publication. Shutdown cancels the local wait and leaves a recoverable lease without claiming that remote billing was cancelled.

## Verification

`tests/test_ai_authoring.py` uses real SQLite with fixture embeddings/providers. It covers native-admin and project isolation, CSRF, explicit/idempotent work, independent opt-ins, original citations, immutable generation/edit provenance, bounded source coverage, Unicode personality, cancellation, in-flight session/source/configuration changes, quota reservation, uncertain retries, rejected tool/citation/link output and local shutdown. No live generation or credential reads are required by these tests. See [verification](verification.md) for checks performed on the current application checkout.
