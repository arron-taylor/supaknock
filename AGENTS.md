# Application development

This repository contains the independently buildable SupaKnock application.
Keep private operational records, credentials, provider experiments, the iPhone
source and the product website outside this repository.

Preserve API compatibility, atomic authorization and event/job writes, immutable
history, idempotent retries, project-scoped opaque identities, privacy boundaries,
SSRF protection and fenced leases. SQL belongs within `feedback/storage`.
SQLite remains the default; PostgreSQL and Supabase use the PostgreSQL adapter.
Use one application instance and preserve existing data and effective secrets.

Run real database integration tests for behavioral changes. Verify relevant
pages in a browser after UI changes. Use disposable resources for Docker,
persistence and backup/restoration verification. Never erase an existing
database or claim deployment from local build evidence.

Export `docs/openapi.json` from `create_app().openapi()` with a temporary Config,
never a production database. Record public-safe checks in `docs/verification.md`
and current application work in `PROGRESS.md`. Keep MIT licensing.

The normal application build and test suite must work without sibling private
repositories. Optional relay end-to-end tests require an explicit disposable
database and may use `RELAY_SOURCE_DIR` for the separate relay checkout.
Website-specific tests belong to the website repository.
