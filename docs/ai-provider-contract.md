# AI generation provider contract

October 12 extension: [additional chat providers and external embeddings](ai-providers.md). Local EmbeddingGemma remains the default; external embeddings are now an explicit administrator option.

Implemented October 3, 2026 and connected to native-administrator project
settings, knowledge authoring, previews and customer chat. KodeKey, direct
Google Gemini, Mistral and Groq are registered reviewed adapters. Initial fixtures establish protocol
handling, not account entitlement. A later owner-authorized test of **GPT 6 Luna only** verified text
and tool-call access for the supplied account, as recorded below. Account quotas,
production suitability and integrated release acceptance remain gates in
[the AI support plan](ai-web-admin.md).

## Application boundary

`feedback/ai_providers.py` exposes an asynchronous `Provider` protocol and the
reviewed `provider('kodekey')`, `provider('gemini')`, `provider('mistral')` and `provider('groq')` registry entries. Adding another provider requires a
reviewed module and registration; it cannot come from a URL, uploaded code or
user-supplied import path. The registry exposes only these four providers in this release.

- `list_models(key)` returns bounded, deduplicated, sorted **exact model IDs**.
  It does not return or infer account entitlement or supported capabilities.
- `probe_model(key, model, tools=False)` performs an explicit synthetic text
  probe. With `tools=True`, it additionally checks a forced synthetic tool call
  and a follow-up using a synthetic tool result. It executes no application tool
  and sends no customer content. One probe uses one or three provider calls.
- `generate(key, model, messages, capabilities=..., tools=(),
  max_output_tokens=1024, required_tool=None)` makes one request and returns a
  `GenerationResult` with text, proposed `ToolCall` values, `Usage` and a finish
  reason. Only the supplied enabled `ToolSpec` schemas are advertised. An empty
  tool list sends no tool fields. Unknown or disabled proposed tools fail closed.

`Message` supports system/user/assistant/tool roles, with typed tool-call and
tool-result references. `GenerationResult.message()` creates the corresponding
assistant history message. No application callable, credential lookup, database,
retrieval implementation or authorization principal is available to the adapter.
The caller validates tool arguments against each application's operation; a
parsed JSON object is not approval or permission to execute it.

`ModelCapabilities` defaults every capability to false. Text-only generation is
valid once text support has been verified; tools additionally require verified
tool support. The application must store probe evidence under the exact project,
provider, credential/configuration version and model. Key/provider/model changes
invalidate that evidence. The dataclass is trusted internal input, never a
customer-supplied assertion. Catalogue metadata cannot change it.

Streaming and structured output remain explicitly unsupported/unverified in the
adapters. KodeKey, Mistral and Groq request `stream: false`; Gemini uses synchronous
`generateContent`. No partial response is published.
The reviewed KodeKey request uses `max_tokens`. Models that reject this bounded
request shape cannot pass the live probe and are not silently retried with a
different model or parameter contract.

The application retains all project/identity isolation, grounding and citations,
prompt construction, configuration fencing, live tool opt-ins, confirmation,
budgets, durable jobs, leases, retries and transactional commits. It must recheck
the current configuration before each provider dispatch and before committing a
reply or action. The adapter never executes tools or continues a tool loop on its
own. Passing its probe is necessary but not sufficient to enable customer AI.

## Transport and diagnostics

KodeKey requests use only the application-owned
`https://api.ai.kodekloud.com/v1/models` and `/chat/completions` endpoints. No
configurable base URL, redirect following, environment proxy or cross-provider
fallback is supported. Bearer credentials travel in an Authorization header,
never a URL. Each call owns its client and does not retain a project's key.

Limits are deliberately conservative and can be lowered by callers:

| Bound | Implemented limit |
| --- | --- |
| Whole transport operation | 45 seconds; internal test override must be 0–60 seconds |
| Connect/pool wait | 5 seconds |
| Read/write inactivity | 15 seconds, also bounded by the whole-operation deadline |
| Serialized request | 256 KiB |
| Raw successful response | 1 MiB, checked while reading |
| Catalogue | 1,000 entries; model IDs at most 256 printable non-space ASCII characters |
| History | 128 messages |
| Tools/calls per response | 8 |
| Tool parameter schema/serialized argument string | 16 KiB |
| Output budget | 1–8,192 tokens; normal default 1,024 |
| Probe output budgets | KodeKey: 32 text, 128 tool, 32 follow-up; Gemini/Mistral/Groq: 1,024 per call |

The transport requests identity encoding and rejects compressed responses, which
prevents decompression from bypassing the wire-size cap. JSON success bodies
are parsed. Duplicate JSON keys, non-finite numbers, malformed tool calls,
duplicate call IDs, unexpected roles, partial replies and unsupported response
shapes fail closed. Redirect bodies are never read. The only error-body reader is
Groq's bounded 16 KiB JSON check for the exact documented `blocked_api_access`
code on HTTP 400; it maps to `quota_exhausted`. No raw error body is retained,
logged or returned, and every other error remains status-based.

`ProviderError` exposes only a static `code`, optional HTTP status,
`retryable` flag and bounded `retry_after` seconds. Authentication/access failures
are permanent; 429, 408, 425 and server failures can be retried by the application.
Retry-After accepts seconds or an HTTP date and is bounded to 1–86,400 seconds.
The adapter performs **no automatic retries**. Network/TLS and timeout errors are
normalized without exposing exception text; caller cancellation propagates.

Normalized usage retains only reported input/output/total token counts. Missing
counts remain unknown. No monetary price or total is invented from model names,
catalogue metadata or an unqualified provider cost field. A failed/uncertain call
may still have consumed provider quota; this layer does not claim exactly-once
billing or a zero-cost failure.

## Credential pools and Groq

Each project's selected provider has up to 99 encrypted, named keys. A key must
have its own catalogue and successful evidence for the selected model and all
required capabilities. The application rotates eligible keys between durable
attempts and pins one key throughout an attempt's tool rounds. It never moves a
signed continuation to another account. Exact account-group labels share quota
health installation-wide for that provider; keys themselves remain project-scoped.
Provider changes clear the old pool. See the [pool contract](ai-key-pool-contract.md)
for atomic administration, migration, recovery, retry and usage guarantees.

Groq uses only `https://api.groq.com/openai/v1/models` and `/chat/completions`, with
Bearer authentication and `max_completion_tokens`. The catalogue intersects live
active IDs with reviewed direct chat models: GPT OSS 120B/20B, Llama 3.3 70B,
Llama 3.1 8B and Qwen 3.8 27B. The picker prefers `openai/gpt-oss-120b` only when
that exact model is returned. Discovery never enables AI or starts paid probes.
Compound, hosted tools, audio and guard models are excluded. Custom function
calls are sequential; reasoning is hidden and never shown or stored in chat.

Groq documents organization-wide [rate limits](https://console.groq.com/docs/rate-limits)
and [spend limits](https://console.groq.com/docs/spend-limits). Keys from the same
account share a cooldown. Rate-limit cooldowns honor Retry-After; recognized
quota exhaustion waits 24 hours. Invalid credentials and payment errors require
replacement or an explicit successful probe. Expired cooldowns permit one fenced
recovery attempt. All-unavailable work stops waiting after five minutes and uses
the existing human-support fallback; each dispatched attempt still consumes its
conservative usage reservation. No adapter performs automatic retries.

## Explicit JSON output mode

`generate(..., response_format='text'|'json_object'|'json_schema',
response_schema=None)` defaults to text. On tool-free calls the application
chooses strict JSON Schema only when both the adapter's reviewed
`native_strict_json_output` flag and the claimed key/model's stored
`structured_output` evidence are true. Otherwise it retains the existing
JSON-mode/text selection. Already selected strict requests never retry in a
weaker format. A schema without strict mode, an unverified strict request, or
strict mode combined with advertised tools fails before dispatch.

Mistral and Groq send `response_format: {"type": "json_object"}`; Gemini sends
`generationConfig.responseMimeType: "application/json"`. Adapters independently
suppress these controls while tools are advertised. Tools-off continuation keeps
native signed history intact. KodeKey keeps `native_json_output=False` for its
legacy hint, but supports strict mode via `response_format.json_schema` with
`strict: true`. Its explicit administrator connection test adds one bounded
synthetic conflicting-prompt/schema probe after text and optional tool checks.
Only the exact constrained result earns `structured_output=True`; catalogue
membership and an HTTP 200 cannot. A rejected/malformed/refused optional probe
leaves text support verified and strict support false. Authentication, quota,
network and service failures still reach ordinary key-health handling.
Existing keys need a fresh connection test to acquire this evidence; provider,
key/model changes and late results retain existing transactional fences.

KodeKey support inputs use literal labelled plain text for initial context,
checking and correction. Other providers and native tool continuations retain
their existing formats. Personality, recent history and source text remain
untrusted. Answer/Preview, section/FAQ, checker and correction schemas are
application-owned, bound required fields and use exact attempt-local citation
allowlists where applicable. Serialized schema bytes count against request and
usage limits. Application validation and original-source checking remain
mandatory: strict JSON does not establish factual accuracy, authorize tools or
replace exact issue-quote/citation validation. Unsupported adapter schema
requests fail before dispatch; no generic schema or format field is exposed to
HTTP clients. Gemini, Mistral and Groq strict support remains unverified here.

Reference: [KodeKey structured outputs](https://kodekloud.com/blog/how-to-get-reliable-json-with-structured-outputs/).

Gemini omits `candidateCount`, which Google documents as unsupported for Gemini 3
and later. The default single candidate is still verified in every response.

References: [Mistral JSON mode](https://docs.mistral.ai/studio/conversations/structured-output/json_mode),
[Groq JSON mode](https://console.groq.com/docs/structured-outputs),
[Gemini GenerateContent schema](https://ai.google.dev/api/generate-content),
[Gemini 3.8 migration](https://ai.google.dev/gemini-api/docs/generate-content/latest-model).

## Direct Google Gemini protocol

Gemini uses the native Google API at
`https://generativelanguage.googleapis.com/v1beta/models` and
`/models/{exact-model-id}:generateContent`. Each project supplies its own Google
AI Studio key, sent only in `x-goog-api-key`. It is never sent to KodeKey or put in
a URL. The same deadlines, byte bounds, safe errors, cancellation, no-redirect,
no-proxy and no-retry guarantees above apply. The catalogue follows at most ten
pages under one whole-operation deadline and includes supported `gemini-*` text
candidates that advertise `generateContent`; image, audio, live, TTS, robotics and
embedding variants are excluded. The documented resource prefix `models/` is
removed, while the remaining exact ID is preserved. Selection still requires an
explicit per-key/per-model probe; a catalogue entry is not proof of entitlement.

System instructions and conversation history are translated to native `Content`
parts. Only enabled application functions are declared using `parametersJsonSchema`.
A forced tool probe uses `ANY` with a single allowed function. A normal tool turn
uses `AUTO`. No Google built-in tools are enabled. The adapter preserves native
function-call IDs, signed parts and thought signatures exactly for continuation,
including parallel calls; missing native IDs get transient internal references.
Private thought text does not become the answer. Bounded, repr-hidden opaque state
travels only through internal `GenerationResult`/`Message` values and is counted
against the application context budget before any tool effect. It is not saved to
chat history, returned by APIs, logged, or shown to customers. Historical calls
remain replayable when the last round disables new tools, while new calls still
require the current allowlist.

Blocked, truncated, malformed and unsupported replies fail closed. Input/output
usage uses reported prompt, candidate and thinking counts; missing counts remain
unknown. Gemini's three synthetic probes each allow up to 1,024 output tokens to
accommodate thinking. This is a quota ceiling, not a usage estimate. The application
still enforces its own smaller context, output and round budgets regardless of a
model's advertised maximum context.

Google documents `gemini-3.8-flash` as a text/tool-capable model with a 1,048,576-token
input limit. It is not hardcoded into the picker or auto-selected: the saved key's
catalogue determines the options. Account quotas and live answer quality require
actual account verification.
[Model documentation](https://ai.google.dev/gemini-api/docs/models/gemini-3.8-flash),
[model discovery](https://ai.google.dev/api/models),
[native generation](https://ai.google.dev/api/generate-content),
[function calling](https://ai.google.dev/gemini-api/docs/function-calling).

## Direct Mistral protocol

Mistral uses only `https://api.mistral.ai/v1/models` and
`/chat/completions`, with the project's own Bearer key. The bounded catalogue
retains exact chat-capable IDs, including account fine-tunes, and excludes
archived/internal entries. Aliases are selectable only if returned by that
account's catalogue. The owner selected `ministral-14b-2512` (Ministral 3 14B)
for live checks; the application does not force or infer this selection.

The adapter sends `max_tokens` and validates native function calls, including
documented string/object argument forms. Historical assistant calls and ordered
tool replies retain their exact IDs and derived names. Signed thinking chunks
remain bounded private continuation state, with the same context accounting and
non-persistence guarantees as Gemini. Unknown new tools still fail closed, even
when old calls are replayed in a final turn with tools disabled. A shared narrow
HTTP helper enforces all three providers' transport limits without enabling
cross-provider fallback. No provider SDK or database migration was added.

[Ministral 3 14B](https://docs.mistral.ai/models/ministral-3-14b-25-12),
[model discovery](https://docs.mistral.ai/api/endpoint/models),
[chat API](https://docs.mistral.ai/api/endpoint/chat),
[reasoning continuation](https://docs.mistral.ai/studio/conversations/reasoning).

## Evidence and remaining checks

Official KodeKloud examples confirm the fixed endpoint and chat-completions/model
discovery shape. They explicitly distinguish the catalogue from the models a
particular account can invoke, and note that usage cost fields vary by model.
[Model discovery example](https://kodekloud.com/blog/multi-model-chat-app-streamlit/)

The documented tool pattern returns function names and JSON arguments, then
accepts an assistant tool-call message followed by a tool result. Suggestion Box
uses that protocol shape but keeps execution in its own authorized application
operations. [Tool-call example](https://kodekloud.com/blog/most-ai-agent-ideas-are-cron-jobs-in-disguise/)

Structured-output support varies by selected model; a generic gateway address
does not establish it for every catalogue entry.
[Structured-output example](https://kodekloud.com/blog/how-to-get-reliable-json-with-structured-outputs/)

KodeKloud describes KodeKey as a learning/experimentation product rather than a
production application service. Confirm the account's permitted intended use and
quotas before customer rollout. [KodeKey product description](https://kodekloud.com/ai-playgrounds/kodekey)

`tests/test_ai_providers.py` uses injected HTTP transports and a fake normalized
provider. It covers exact IDs, no-tool generation, enabled schemas, proposed
tools without execution, a complete synthetic probe round trip, malformed
responses, limits, safe errors, redirects, total deadline/cancellation and
environment-proxy exclusion. The final focused run passed **58 tests**, including
decoded exponent overflow and escaped Unicode-surrogate regressions found during
independent review. It makes no real provider calls. Application integration is now covered by the real-SQLite authorization,
idempotence, rollback and recovery checks in the integrated evidence matrix (internal historical record).
The full integrated Python suite passed 781 tests, with three optional external
relay integrations skipped; this does not establish live-provider answer quality.

Before release, run authorized tests with the intended KodeKey account to record
catalogue access, successful text/tool probes on selected exact models, revoked
or unauthorized-model behavior, quotas, the chosen output-budget parameter,
optional capabilities if enabled later, and intended-use suitability. Record
only redacted results and safe diagnostics; never save the key, raw provider
errors or customer prompts as evidence.

## Authorized GPT 6 Luna live verification

On October 3, 2026, the owner supplied credentials privately and limited live
model testing to GPT 6 Luna. Catalogue discovery returned 57 exact IDs and one
matching ID, **`gpt-6-luna`**. Only that model was invoked.

The existing adapter completed all three synthetic probe calls: exact text
response, forced tool-call arguments, and a follow-up after a synthetic tool
result. Text generation and tool calling passed. No application tool executed,
no customer data was sent, and no other model was tested. Reported total usage
was **200 tokens** across the three calls; no monetary cost is inferred.

Per-call elapsed times were 1.166, 1.489 and 1.005 seconds. These small synthetic
requests are connection/protocol evidence, not a performance or answer-quality
benchmark. Streaming and structured output were not tested and remain disabled
in this adapter. The key itself and raw responses are absent from the
sanitized evidence (internal historical record).

This establishes access for that supplied key/model at the recorded time. It
does not grant another project/key/model implicit verified capabilities, enable
customer AI, establish account quotas or permitted production use, or complete
AI18's application/release acceptance. Normal project setup still requires its
own explicit model probe before enablement.
## Native administrator configuration API

`feedback.ai_settings.AISettingsService` connects this contract to encrypted,
versioned SQLite settings. `feedback.ai_settings_api.install_ai_settings` installs
these native-administrator routes:

| Method and path | Effect |
| --- | --- |
| `GET /api/v1/ai/providers` | List KodeKey, Google Gemini and Mistral provider identities. |
| `GET /api/v1/projects/{project}/ai` | Read settings, exact cached model IDs, capability evidence and `has_key`. |
| `PUT /api/v1/projects/{project}/ai` | Save `{version, settings, key?}` without provider I/O. Every settings/tool flag is required. |
| `POST /api/v1/projects/{project}/ai/models/refresh` | Explicitly refresh the saved key's catalogue using `{version}`. |
| `POST /api/v1/projects/{project}/ai/model/probe` | Explicitly test the saved exact model using `{version, tools: false}`; `tools: true` requests the synthetic tool round trip too. |
| `GET /api/v1/projects/{project}/ai/feedback-destinations` | Resolve feature/bug page links from saved settings without fetching either page. |

Within the same provider, omitting `key` preserves the encrypted key, `null`
removes it, and a nonempty string replaces it. Switching providers clears the
previous key unless a replacement is explicitly supplied, clears the chosen
model/catalogue/probe evidence, and fences previous work atomically. The UI saves
the switch with AI off. Refresh the new catalogue, select a model and probe it
before explicitly enabling AI again. No key, ciphertext or key fragment is returned. Reads and
saves never contact the provider, download a model or enable permissions. AI,
each tool, and guest reports remain separate explicit choices. Saved model IDs
are never normalized; a model must appear exactly in the current catalogue and
pass a live probe before it can be enabled. A successful catalogue refresh alone
does not verify any capability. The existing readiness checks also require a
ready local runtime and approved compatible knowledge.

Cookie requests use the existing origin and CSRF checks. Native administrator
sessions also work; project credentials and chat credentials do not grant these
permissions. Storage rechecks the native session at every settings/evidence
operation, including after asynchronous provider calls. Changing the key or
model invalidates evidence; a late result from an earlier configuration returns
a conflict instead of updating the replacement. Provider keys remain protected
by the installation's effective encryption key and supported backup operation.

An explicit failed probe clears capability verification and disables AI if it
was enabled. A denied/rejected key during catalogue refresh clears both the
catalogue and capabilities; restored catalogue access still requires a new
probe. Transient refresh errors preserve the previous catalogue. Errors use
fixed application messages without upstream response bodies, prompts or keys.
Clients should reload settings after a failed check because invalidation can
advance the configuration version. Explicit checks are limited to one active
operation per project and four per installation. They have no automatic retries
and may incur provider usage (one synthetic request for text, three with tools);
they are separate from the durable generation queue's daily usage reservations.

`resolve_feedback_destination(settings, kind, base_url)` applies the same priority
for future guest-off guidance, pre-submit notices, copied drafts and receipts:
the flow-specific saved URL, then the project's saved URL, then its configured
native board page. With neither a URL nor a board it returns `None`. A custom
page never changes the destination board or creates report-specific routes. The
resolver preserves the administrator's validated URL exactly, adds no identity,
token or draft parameters, and performs no network request.

AI04 verification on October 3, 2026: `tests/test_ai_settings_api.py`,
`tests/test_ai_providers.py` and `tests/test_ai_storage.py` passed **133 tests**
together. These use real SQLite with fixture provider I/O and include native
admin/CSRF boundaries, secret-free responses, explicit opt-ins, stale-key and
revoked-session completion races, bounded concurrent checks, cancellation,
access-loss invalidation and feedback-page priority. No additional live provider
calls were made. Admin UI, integrated customer turns and release checks remain
in their later plan tasks.

## Integrated quality acceptance

The later fixed 36-question actual-model run did not meet release targets.
Successful small connection probes did not predict reliable integrated generation:
23 requests returned HTTP 400 and two successful responses failed strict answer
validation. The production adapter exposes these as safe request-rejected errors. Separate
bounded diagnostics confirm provider-reported content blocking on a benign
synthetic return-window request; the mechanism and trigger remain unknown. No
raw error body or credential is retained. See quality protocol and results (internal historical record)
for subsequent diagnostics and any remediation. Customer rollout remains gated
on adequate actual-provider quality, account suitability and model acquisition.


## Gemini local verification

The October 3 Gemini addition passes 131 adapter cases, four real SQLite native
continuation cases and the full 910-test Python suite (three optional relay
integrations skipped), plus 12 JavaScript checks and desktop/mobile browser QA.
Both local container architectures pass packaging and ordinary persistence/
backup checks. No Google key has been supplied, so real catalogue access, model
entitlement, text/tool probes, quotas and answer quality remain unverified.
These fixture passes do not complete the live-quality release gate.
Current Gemini evidence (internal historical record).


## Direct-provider supplied-key checks — October 3, 2026

Both own keys successfully discovered their selected exact models. Gemini returned
20 eligible models; its first `gemini-3.8-flash` text probe returned HTTP 503. No
Google tool or support-question call followed, and no cause beyond service
unavailability is inferred. Mistral returned 27 chat-capable models and
`ministral-14b-2512` passed text, forced function call and tool-result continuation.
Its support request returned successfully but failed application JSON parsing.
A single additional diagnostic retained an entirely fictional answer: the correct
return policy and exact citation enclosed in one JSON Markdown fence.

The application now accepts that exact whole-wrapper form while preserving its
original byte cap, schema and citation checks. Offline replay of the retained
answer passes the final validator and human semantic review; it is not a fresh
generation after the correction or a full quality-corpus pass. The initial failed
results remain unchanged. Six generation requests total (five Mistral, one Gemini),
no automatic retries or application tool effects. Mistral reported 2,501 tokens;
usage for the failed Gemini call is unknown. No monetary cost is inferred.

Initial direct-provider evidence (internal historical record),
synthetic formatting diagnostic (internal historical record),
final-validator replay (internal historical record). Account quotas,
production suitability and the fixed answer-quality evaluation remain release
checks. No provider setting was changed in a real installation.
