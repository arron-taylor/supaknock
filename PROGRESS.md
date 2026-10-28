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

# Application progress

## Developer introduction and first chat — October 21, 2026

Added a concrete product example, project explanation, integration diagram,
three starting paths, an expandable public screenshot gallery, and common
integration questions to the README. The exact local HTML walkthrough was
verified in a browser through a customer message and administrator reply.
Anonymous widget usage is distinguished from authenticated backend integration;
project secrets stay server-side. The iPhone gallery is labeled as an upcoming
companion preview. Runtime source and deployment configuration are unchanged.

## Docker README quickstart — October 21, 2026

The README identifies Docker Hub and the full published image address, embeds
the complete published-image Compose configuration, and walks through pulling,
startup health, reading `/data/setup.token`, first-administrator setup and later
sign-in. No application behavior or deployment configuration changed.

## Initial repository README — October 21, 2026

Prepared the standalone application for the private `arron-taylor/supaknock`
repository. The README now provides product navigation, a feature overview,
Docker setup, integration entry points, and clear links to supaknock.com.
Detailed environment settings live in `docs/configuration.md`. The iPhone
companion is described as coming soon. No runtime behavior changed.

Documentation checks and the initial source review are recorded in
[verification](docs/verification.md). Public visibility and production
rollout are separate from this private source upload.

## Independent application repository — October 21, 2026

Application source, migrations, tests, dependency locks, Docker files and current
public documentation now live together in this repository. Website and private
supporting work are separate. Runtime application code is unchanged by this move.
Local verification is recorded in [docs/verification.md](docs/verification.md).
No GitHub publication or production deployment is part of this reorganization.

Known limitation carried forward: optional podcast AI can return an invalid
answer format after successful tool reads. Repair remains separate work.
