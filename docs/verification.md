## Embedded chat height and writing surface — October 28, 2026

Floating desktop chat now allows up to 820px height while retaining viewport bounds and phone layouts. The composer is white in light mode and uses the themed surface in dark mode. Nine existing widget checks passed; browser validation covered desktop, tablet, 390px/320px phones and landscape, plus real disposable SQLite message delivery. The built ARM64 image passed restart, recreation and backup-restoration smoke. No API/schema changes.

## App visual polish — October 27, 2026

Release: `2026.10.27-app-polish` and `latest` published for AMD64/ARM64 (index `sha256:468ecbf630651beec9404f913107ac2a6b05540b44331d396f4c004fbd02a97c`). Live app deployment passed health, source parity and data/settings preservation checks, with an isolated restoration rehearsal.

Shared controls and labels are more readable, cards and inputs use softer corners, settings share an 880 px reading width, and AI settings have distinct secondary tabs and fewer nested borders. Project headers stay consistent between tabs. AI readiness copy reflects the saved enabled state and retains unsaved-change notices. Provider-library actions have a clearer primary/secondary hierarchy. No API, permission, credential, storage or widget behavior changes.

Verified 34 existing JavaScript checks, 13 real SQLite integration tests, JavaScript syntax and whitespace checks. Runtime OpenAPI matches all 152 documented paths. Browser inspection covered light/dark desktop settings and shared pages, 390/320 px answer setup without page overflow, keyboard project-tab navigation, unsaved permission state, provider-library actions, inbox empty state, and participant project/board/form pages including dark 390 px. A local ARM64 image matches all 129 runtime files and passed the full disposable Docker restart/recreation and backup/restoration smoke. External providers were simulated in browser fixtures; no live provider checks were needed.

## Embedded chat design release — October 27, 2026

Published commits `8c73f82` and `95fa533` and deployed the updated widget.
Docker Hub `taylorarronj/supaknock:latest` and `2026.10.27-chat-design.2`
resolve to the AMD64/ARM64 index
`sha256:12e572482248cd8a0911262df6c8c6234770325a304bfba60aeb83df4183521b`.

The final packaged ARM64 image passed the full disposable Docker smoke,
including restart/recreation and backup restoration. The AMD64 production
update passed an isolated backup/restore rehearsal. All 129 running source
files match the checkout, and the public widget bytes and readiness checks
passed. Existing records, volume, environment, effective secrets, AI settings,
key bindings, relay configuration and Compose were preserved; other containers
were unchanged.

Live browser verification on an embedding website caught its custom 570px
height constraint. The initial composer now stays hidden until the explicit
AI/person choice, keeping the choice and provider disclosure fully visible.
The final live check verified that clearance and the subsequent composer reveal.
Only an empty support session was opened; no production AI question was sent.
Local browser and focused test evidence is recorded below.

## Embedded chat visual refinement — October 27, 2026

The widget now has a spacious branded welcome, a quiet header, larger message
text and a rounded multiline composer with a circular send control. The initial
AI/person choice sits in the welcome; it returns above the conversation after
selection. Existing site colors, light/dark themes, bubble/bar/inline embeds,
episode context, citations, support handoff and chat identity remain intact.
No API, database or authentication changes.

Validation: nine JavaScript checks and 13 real-SQLite chat/recovery checks passed.
Browser checks covered desktop, 390px mobile, a compact 320px viewport, dark mode,
AI choice and progress, cited replies, expanded sources, multiline drafts,
human handoff, end-chat confirmation and fresh welcome, Enter-to-send, and
conversation recovery in an inline embed. No horizontal widget overflow was
observed at 390px. The final local ARM64 image passed the full disposable Docker
restart, recreation, persistence and backup/restoration smoke. AI/MCP responses
were simulated; no live provider calls were made. Publication and rollout evidence
will be recorded separately.

## AI setup release published and deployed — October 27, 2026

Published application commit `7df64b4`, including the unified answer setup,
Ramp verification timeout handling and the previously committed authorization
fixes. Docker Hub `taylorarronj/supaknock:latest` and
`2026.10.27-ai-setup` resolve to the same AMD64/ARM64 index:
`sha256:affe6c35c085d359c577582dcb0d77e440b922e8f6b317635fd1fa4b51d05e7a`.

The final AMD64 image passed the full disposable container smoke, including
restart, recreation, backup/restore, authorization and private state checks;
the final ARM64 candidate passed the same smoke before publication. The 29
additional SQLite security tests passed. Earlier focused UI/API and provider
results, including known broader-suite failures, are recorded below.

Owner-authorized application deployment passed an isolated, network-disabled
rehearsal against a private backup. All 129 deployed runtime source files match
the published checkout. Existing records, volume, environment, installation
identity, effective secrets, relay configuration, AI settings and key bindings
were preserved. Compose was unchanged and no unrelated containers were replaced.
The live container is healthy; public readiness, widget, AI assets and OpenAPI
match the release. A signed-in browser verified the combined setup card with
the saved Ramp Router / gpt-6-luna selection, verified status and AI still enabled.
No new live AI inference or general answer-quality benchmark was performed.

## Unified answer-provider setup — October 27, 2026

Replaced three separate answer-provider forms with one local draft for provider,
explicit account-key selections and exact model. Account catalogues can be read
or refreshed before project bindings change. One Save & verify action performs
the existing version-checked writes and per-key probes, with progress and errors
in the same card. Shared keys are not implicitly bound or deleted; changes keep
AI off until explicit enablement. Interrupted operations stop and require review
of saved state. A separate disconnect action preserves the account credentials.
Late tab refreshes cannot overwrite a newly edited draft. Topic-decision setup
is unchanged. The workflow uses existing APIs and does not change the schema.

Validation: 729 real-SQLite account-key/provider API tests passed; 23 JavaScript
UI logic tests passed. Browser checks covered a KodeKey-to-Ramp switch, two-key
verification, model loading before save, failed tool verification and recovery,
missing-key guidance, local discard/cancel, disconnect/reconnect, direct Settings
navigation, dark/light themes and 390px layout without horizontal overflow.
All provider calls in browser tests were simulated. Runtime OpenAPI generated
with temporary Config/create_app matches docs/openapi.json; diff check passed.

Final local ARM64 image `supaknock:ai-setup-local`, config
`sha256:13565c973da2fcdf407958be60f1f62b6bca8d324b7fc39b3d83bf94a94c81a0`,
built successfully and passed the disposable Docker restart, recreation,
persistence and backup/restoration smoke. Owned test containers/volumes were
cleaned up. No live credentials, publication or production deployment were used.

## Ramp Router timeout and verification failures — October 27, 2026

Local ARM64 image `supaknock:ramp-timeout-local` built successfully (image
config `sha256:db6544a6497f271a3f52a64a6986f0418bfbe7fe6f3b932513d7258d11aaee99`).
The disposable Docker smoke passed HTTP flows, restart/recreation persistence,
and backup/restoration. Smoke containers and volumes were cleaned up. Local
browser fixtures were stopped and their tabs closed. No publication/deployment.

Ramp inference POST requests now allow up to 45 seconds for a response read,
within the existing 45-second total request deadline. Catalogue requests and
other providers retain their 15-second read limits. Connection/pool limits,
redirect refusal, response bounds, cancellation and credential fences remain.
A failed answer-check request or exhausted checking allowance now says the check
could not complete, rather than suggesting missing support evidence. Actual
unsupported answers retain the existing evidence fallback; unchecked drafts
remain private. Cooldowns and bounded retries are unchanged.

Validation: 152 focused provider/evidence/strict-output/chat/progress tests passed,
plus the added real-SQLite timeout/next-turn-cooldown regression. Real loopback
HTTP tests cover delayed first bytes, read timeout and the total deadline.
Browser QA with disposable SQLite and simulated providers verified the new
message in the customer widget and administrator inbox. Runtime OpenAPI from a
temporary Config exactly matches the committed schema.

Broader provider/continuation/MCP/key-pool checks: 143 passed, four failed.
Three continuation tests also fail on a clean HEAD archive (d800105), without
these changes. The fourth key-pool timing test passed on isolated rerun and on
clean HEAD. This is focused verification, not a claim of a green full suite.
No live inference, publication or production deployment was performed.

# Application verification

## README developer guide — October 21, 2026

- Extracted the README's exact HTML into a disposable local site on port 3000.
  Used a separate temporary SQLite application on port 8000 with a demo project,
  the documented allowed origin, anonymous chat enabled, and AI off.
- Browser verification sent "Hello from my app!" through the widget, signed in
  to the test administrator workspace, opened Live chat, replied "Hi! How can we
  help?", and confirmed that reply appeared in the customer widget.
- Runtime OpenAPI and protocol schema parity passed. README file links and
  Markdown heading anchors resolve. The embedded Compose configuration still
  exactly matches `compose.image.yaml`; edited files pass whitespace checks.
- Replaced a stalled GitHub Mermaid rendering with a self-contained SVG diagram;
  the SVG parses successfully and includes accessible title/description text.
- The three existing product-website screenshot URLs returned HTTP 200 with
  the expected image content types. Images stay hosted with the product website.

No production data, external AI, or live customer conversations were used.
This documentation change did not publish a container or deploy an application.

## Docker README quickstart — October 21, 2026

Extracted the embedded YAML into a temporary directory and validated it with
Docker Compose. Its normalized configuration exactly matches
`compose.image.yaml`, including the image, volume and localhost binding.
README local file links resolve, and edited files pass whitespace checks.
The setup-token path and environment override were checked against runtime
configuration; the documented health status uses the existing image healthcheck.
No container was started, image published, or deployment performed for this
documentation-only update.

## Initial source and README review — October 21, 2026

- 18 API documentation/OpenAPI tests passed against disposable local state.
- The runtime OpenAPI export and connected-app protocol schemas match.
- All local Markdown file links resolve.
- Gitleaks 8.30.1 reported no leaks in the 298-file initial application source
  snapshot. This is a bounded automated check, not a complete security audit.
- The initial file list excludes private supporting records, website source,
  original Git history, local databases, credentials, and development caches.
- Whitespace checks pass for the edited documentation. The imported baseline
  retains three trailing-whitespace lines in an existing migration and test;
  the migration is preserved unchanged to retain its checksum.
- Runtime source is unchanged. The known broader-suite failures below remain
  unresolved; a passing full test suite is not claimed.

The documentation test run emitted the existing Starlette/httpx deprecation
warning. This review did not deploy an application or publish a container image.

## Workspace separation — October 21, 2026

The application is independently buildable and testable from this repository.
All 129 application runtime files, including migrations, are byte-identical to
the pre-move working checkout. No runtime behavior was intentionally changed.

Completed local checks:

- 118 application checks passed in a disposable application-only directory with
  no website or private supporting siblings. Includes real SQLite API/storage,
  documentation examples, OpenAPI parity, connected protocol and MCP checks.
- 38 checks passed against a real disposable PostgreSQL server.
- All 28 JavaScript checks passed.
- Application Docker image built successfully and all 129 packaged runtime
  files match source. Full disposable Docker smoke passed, including restart,
  recreation, fixed backup/restoration, authorization and durable private state.
- Runtime OpenAPI equals the checked-in export; protocol schemas and all public
  local Markdown links resolve.
- Compose retains the previous default project and volume name. Existing custom
  project names must still be supplied unchanged.

A broader Python run was interrupted after **729 passed and 8 failed**; the full
suite was not completed. All eight failures reproduce against the exact
pre-move working code in a separate disposable directory. Two involve citation
handle/tool-round expectations and six involve Groq integration fixture readiness.
They are pre-existing failures, remain unresolved, and are not a passing full
suite. Tests also emit an existing Starlette/httpx deprecation warning.

No hosted Supabase, fresh model download, live provider inference, physical
phone, production rollout, or GitHub publication was verified by this move.


## October 28, 2026 — AI answer reliability
153 targeted SQLite regressions passed, including real provider-adapter final-round history and complete transcript preloading/fallback. PostgreSQL MCP/usage regressions and corrected restart/chat suite passed; one explicitly SQLite-only migration test skipped on PostgreSQL. 52 API/OpenAPI checks and 9 widget checks passed. Disposable browser checks verified AI mode retained on failure, question retry and native-admin diagnostics. Docker smoke verified restart, recreation and backup/restore. Optional complete transcript input remains bounded and explicitly approved; citation/current-authority checks stay enforced. No live-model quality claim is implied by fixture tests.

Release follow-up: multi-platform image published and deployed with offline migration/backup-restoration rehearsal and all runtime source hashes verified. Two bounded live episode questions using Ramp gpt-6-luna both produced source-checked, cited answers with complete available transcript context; 27.6 and 38.7 seconds, two and four provider calls respectively. No automatic human handoff. This verifies these two cases, not general answer quality or a latency guarantee.


## Ramp interactive latency — October 28, 2026

Ramp generation requests now send `allow_flex_tier: false` to avoid automatic discounted Flex routing. Exact selected model, reasoning defaults, prompts, full-transcript bounds, capability evidence, key pinning, correction and independent source verification are unchanged. No premium tier or model fallback is requested. Standard processing can cost more than Flex; an explicitly pinned service tier remains explicit.

Bounded paired checks of two public episode questions using the same Ramp GPT 6 Luna model measured model-work totals of 7.4–11.4 seconds on standard processing versus 11.6–39.5 seconds on Flex. Provider receipts confirmed the actual tiers. Source-fetch time is excluded; samples are small and stochastic. All completed comparison answers passed the existing independent evidence check; the standard follow-up still used correction and rechecking. An initial probe incorrectly requested JSON-object mode and was corrected to the production strict-schema contract; a second probe exposed ordinary plain-text draft formatting and was corrected to include the production format-repair path. Neither probe error changed production behavior. No caching or reasoning reductions were shipped.

Validation: 174 targeted SQLite/provider tests and 111 real PostgreSQL generation/MCP tests passed. Both ARM64 and AMD64 application images built. The disposable container smoke passed restart, recreation and backup restoration, plus ownership, event, notification and persistence checks. No schema, API or UI change was required.

Release follow-up: multi-platform image `2026.10.28-ai-latency` / `latest` published and deployed. Same-question live checks completed in 13.9 and 9.3 seconds from message creation to committed, cited, source-checked answer (previously 27.6 and 38.7 seconds). Existing data, settings, selected model, keys, bindings, volumes and Compose were preserved; only the application container changed. These are two live cases, not a latency guarantee.
