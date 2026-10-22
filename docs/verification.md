# Application verification

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
