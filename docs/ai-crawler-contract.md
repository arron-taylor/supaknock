# Public website knowledge imports

Website crawling is an explicit native-administrator action. It can prepare knowledge while customer AI is off; it never enables AI, tools, guest reporting, a provider, or an embedding model. Reading a source or opening settings performs no network request. A successful crawl queues normal local ingestion; it does not claim that the source is indexed or approved.

## Administration API

All routes are under `/api/v1/projects/{project}/ai/knowledge`, require a live native administrator session, and use the existing cookie CSRF/origin checks for mutations. Project API keys and chat credentials do not grant access. Transport filters worker leases, authority snapshots and session IDs from responses.

| Method and suffix | Result |
| --- | --- |
| `POST /websites` | Create an unapproved website source and queue a crawl; return `{source, run}`. |
| `PUT /sources/{source_id}/website` | Versioned refresh; return `{source, run}`. |
| `GET /sources/{source_id}/crawls` | Paginated run metadata without source text. |
| `GET /sources/{source_id}/crawls/{run_id}` | Run metadata and the current attempt's page outcomes, including private extracted text. |
| `POST /sources/{source_id}/crawls/{run_id}/cancel` | Cancel with `{version}`; return the run and its retained outcomes. |

Create and refresh accept a UUID `client_import_id`, display `name`, and `config`. Refresh also requires the current source `version`. The configuration is:

```json
{
  "seed_url": "https://docs.example/docs",
  "approved_origins": ["https://docs.example"],
  "include_paths": ["/docs"],
  "exclude_paths": ["/docs/private"],
  "max_pages": 20,
  "max_depth": 3
}
```

Origins are explicit: another scheme or subdomain is a separate origin. Include/exclude paths are segment prefixes (`/docs` includes `/docs/setup`, not `/docstring`). Exclusion takes precedence. The seed must be included. Origins are limited to five; each path list to twenty. HTTP is allowed for public documentation, but only the normal HTTP/HTTPS ports are accepted. Canonicalization applies only to crawl URLs and never to opaque customer identities.

Exact retries return the same work and current state, including cancellation; they never restart a run. Reusing an import ID for different inputs conflicts. A new refresh supersedes older unfinished work. Versions remain explicit, and cancelling a finished successful/failed run conflicts. Cancel with the returned version for an idempotent cancellation retry.

## Fetching and extraction

The fetcher accepts public HTTP(S) only. It rejects embedded credentials, queries, unsafe/ambiguous encoded paths and nonstandard ports. Fragments deduplicate. Query links are skipped rather than rewritten to a different resource. Every connection resolves again, rejects the destination if any DNS answer is private, loopback, link-local, multicast or a known metadata endpoint, and pins the socket to the validated address. TLS verifies the original hostname. Internal-webhook development exceptions and proxy environment variables are not used. Requests carry only fixed crawler/accept headers; there are no cookies, authentication, JavaScript, browser rendering or paid fallbacks.

Redirects are never followed. `redirect_refused` tells the admin to provide the canonical public URL. `/robots.txt` must be a successful plain-text response or an explicit 404; authentication errors, redirects, malformed policies and network failures fail closed. Rules use the declared `SuggestionBoxCrawler` product token, case-insensitive exact group matching, combined matching groups, longest matching allow/disallow rules and allow on ties. Supported `crawl-delay` values are 0–10 seconds; unsupported delays fail closed. The core rules follow [RFC 9309](https://www.rfc-editor.org/rfc/rfc9309.html).

The crawler follows bounded in-scope links and robots-advertised sitemap XML, with `/sitemap.xml` as a fallback when none is advertised. Each sitemap stays on its advertising origin. Sitemap discovery may read metadata outside included documentation paths, but explicit exclusions still apply. Discovered document URLs must satisfy the complete approved-origin/path scope. Sitemaps require UTF-8 as specified by the [sitemap protocol](https://www.sitemaps.org/protocol.html); DTDs, entities and alternate XML encodings are rejected.

Only HTML/XHTML pages become knowledge. Extraction favors `main`/`article`, keeps headings, lists, code blocks and table text, and removes common navigation/footer/script/style/hidden content. It rejects common login forms, bot challenges, soft error pages, empty pages and `noindex` directives. This is conservative heuristic extraction, not browser rendering. Stored website originals are immutable extracted text snapshots with page URLs, titles and content hashes; raw response bodies are not stored or logged. All source text remains untrusted data, never execution authority.

## Resource limits

| Limit | Bound |
| --- | --- |
| Page attempts | 1–50; default 20 |
| Link depth | 0–5; default 3 |
| Discovered document URLs | 500 |
| Sitemap documents | 10 |
| Concurrent requests/crawls | One request in one crawl per installation |
| HTML/XML response body | 256 KiB each |
| Robots response body | 512 KiB |
| Total fetched response bodies | 5 MiB |
| Extracted page / whole crawl | 64 KiB / 512 KiB |
| Socket timeout / whole crawl | 10 seconds / 180 seconds |
| Durable outcomes per attempt | 60 |
| Website history | 100 crawls per source |

Compressed responses are refused; the client requests identity encoding. Both advertised content length and actual streamed bytes are bounded. A supervised child process enforces the whole-crawl deadline even while DNS, headers, body reads or parsing are blocked. Cancellation terminates that child, and shutdown stops the crawl worker. The worker claims only `crawl` jobs and renews its recoverable lease while running. No raw remote exceptions or response bodies enter error logs.

## Publication, cancellation and recovery

Each page records `fetched`, `failed` or `skipped`, a fixed safe error code when applicable, and an attempt number. Outcomes are immutable/idempotent within an attempt. A reclaimed lease starts a new attempt; its bundle never mixes abandoned-attempt pages, and an old lease cannot write or publish. The admin detail response shows the current attempt only while older outcomes remain durable. Both list and detail responses include current-attempt `fetched_count`, `skipped_count` and `failed_count`. Web and native crawl history show these counts and readable reasons. Skipped outcomes are not indexing inputs. Older failed runs keep their original status; retry requires a new explicit refresh.

Only a successful crawl within its configured scope creates a new immutable source document and its ingestion job, in the same SQLite transaction that completes the crawl. Reaching `max_pages` is normal completion: the run has `status: succeeded`, `error: null` and `stop_reason: page_limit`. It stops fetching pages and discovery metadata, and indexes only fetched pages; this is not a claim of full website coverage. Page attempts still count toward the cap, including skipped pages. Individual unusable pages do not prevent publication when at least one page was collected successfully. Otherwise `stop_reason` is null. Historical failed runs are not promoted or approved by the migration.

Per-page originals and hashes supply ingestion provenance. Whole-crawl discovery/download/text budget exhaustion, timeout or cancellation cannot replace the previous original or active search index. Individual login, empty, challenge, error, redirect, unavailable, blocked, non-HTML, oversized or unreadable pages are recorded as `skipped` with no document content, then traversal continues. An invalid or unavailable optional sitemap is also skipped. If every page is unusable, the run fails without replacing previous knowledge. Unverifiable robots rules prevent fetching any content from that origin; that origin is checked only once per attempt, and usable pages on other explicitly approved origins may still import. The configured depth is also an explicit scope boundary. After crawl success, the prior index remains available until normal ingestion atomically publishes the new generation. Source approval, source deletion, newer import intent and native-session validity are rechecked at durable worker boundaries; stale work cannot resurrect disabled content.

Errors are stable codes such as `robots_unavailable`, `robots_denied`, `redirect_refused`, `blocked_destination`, `http_failed`, `unexpected_type`, `unexpected_encoding`, `response_too_large`, `invalid_sitemap`, `login_page`, `challenge_page`, `error_page`, `empty_page`, `discovery_limit`, `text_limit` (per page), `byte_limit` and `total_text_limit` (whole crawl), `timeout` and `crawl_failed`. A failure is inspectable without exposing remote response bodies. Retry by starting a new versioned refresh with a new import ID.

## Section generation prerequisites

Knowledge source responses expose `index_ready` for a usable current-profile index, independently of source approval. Project AI settings expose a native-admin-only `readiness` object with `ready` and safe prerequisite codes. Source review explains missing model/key selection or verification, embedding runtime readiness, source approval/activity and successful indexing, and disables Generate until these prerequisites are met. Links lead to Models & keys or AI runtime. Website AI can remain off during authoring; generated sections remain private drafts. The API checks the same prerequisites again, returns a specific `ai_not_ready` message, and preserves explicit version conflicts. A genuine failed refresh may still leave the prior approved index usable; historical errors alone do not disable generation from that preserved index.

## Verification boundary

Crawler tests use local fake HTTP/DNS fixtures. Real SQLite API/worker tests exercise authorization, original preservation, continuation jobs, cancellation, new-import fences, revoked administrators and recovery across attempts. The supervised-process tests demonstrate deadlines and cancellation of blocked children. No live customer website was crawled for this implementation, and these fixtures do not claim browser-rendered site compatibility or production deployment validation.
