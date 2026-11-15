# Parallel transcript searches — November 14, 2026

Independent adjacent searches of an established approved transcript snapshot now run in pairs. Generation joins each pair and consumes results in original request order; the next model call waits for all requested tools. Initial discovery/resolution/evidence reads, duplicate reads, other tools and actions remain sequential. Revision/authority failures cancel and drain siblings. Aggregate remote time remains capped at 25 seconds, with dynamic shared deadlines; the original call/source/rate limits and answer checker remain intact.

Validation:
- 232 SQLite generation, chat isolation, tools, MCP protocol/storage and parallel-search checks passed.
- 64 PostgreSQL checks passed; one SQLite-only bundle-restoration check skipped as designed.
- Six new concurrency checks also passed inside the published Python 3.14 container.
- Barrier-based tests prove simultaneous reads, ordered joins, duplicate cache reuse, unchanged checking and committed citations. Revision/revocation/cancellation tests prove sibling cleanup. Shared-budget tests cover aggregate expiry and unused time redistribution.
- A real endpoint comparison of four fixed searches measured 9.020 seconds sequentially and 5.569 seconds in pairs (38% less search wall time), with identical complete returned results. This excludes discovery/model time and is one sample, not a general latency guarantee.
- Live follow-up answered with one citation in 37.100 seconds. That model turn issued its searches in separate rounds, so they could not overlap; seven model calls and five remote reads remain an end-to-end latency limitation. No whole-turn speedup is claimed from that case.

Release: Docker Hub `taylorarronj/supaknock:2026.11.14-parallel-search` and `latest`, AMD64/ARM64, manifest `sha256:cadb580908bf9a7fa6cae107b2abdcc58ff0db56d0c6a622384b5c6081dd3e6c`. All 131 public runtime files match the prior Git revision plus the three reviewed implementation files. No API/schema changes.

The installation received only those three files over its existing image, preserving unrelated installed work. All 133 runtime files, settings/keys, data identifiers, environment/mounts, readiness and unchanged other services were verified. An isolated offline backup/restore rehearsal passed. Deployment evidence and screenshot are retained privately.
