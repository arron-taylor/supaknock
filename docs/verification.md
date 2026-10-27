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
