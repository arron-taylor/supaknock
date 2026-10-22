# Persistence, backups, restore, upgrades, and recovery

Run one container/process and keep a local persistent Docker volume with every database provider. With SQLite, that volume contains accounts, credentials, sessions, projects, boards, requests, votes, immutable events, delivery jobs and generated application/setup secrets. With PostgreSQL or Supabase, application records live in the private `supaknock` schema; the local volume still holds the setup token and optional model weights. Remote providers require an explicit `APPLICATION_SECRET` in the deployment environment. Do not place SQLite WAL storage on NFS or another network filesystem. See [provider configuration](database-providers.md). Recreating the container preserves the named `feedback_data` volume. `docker compose down` preserves it; **`down -v` deletes it**.

SQLite uses foreign keys, WAL, FULL synchronous durability, and short transactions with lock timeout handling. See the official [WAL guidance](https://www.sqlite.org/wal.html). The image builds SQLite 3.53.4 to include the upstream WAL-reset fix; direct installations should use a patched library as well.

## Backup

The supported maintenance operation uses the [SQLite online backup API](https://www.sqlite.org/backup.html) for SQLite and a PostgreSQL custom-format `pg_dump` archive for PostgreSQL/Supabase. Do not copy only a live SQLite main database file; committed data may still be in its WAL. Remote backups require a `pg_dump` client matching the server major version exactly. The app image supplies versions 15, 16 and 17 and automatically chooses the matching binary. Direct Python installations must install the matching client on PATH. A newer client can generate an archive or commands that cannot be restored to the older source version; it is deliberately rejected.

```sh
# A new directory is required for every backup. No existing backup is overwritten.
docker compose exec supaknock python -m feedback.maintenance backup /data/backup-2026-10-02
docker compose cp supaknock:/data/backup-2026-10-02 ./backup-2026-10-02
```

The resulting directory contains `feedback.sqlite` for SQLite or `feedback.dump` for PostgreSQL/Supabase, the **effective** `application.key` (including when supplied through an environment variable), and `setup.token`. PostgreSQL bundles include a nonsecret `backup.json` identifying their format. The PostgreSQL archive includes only the `supaknock` schema; it does not include Supabase platform schemas, external database roles or connection credentials. The database and key must be restored together. The directory is private; database hashes and webhook snapshots are still sensitive operational data. Store an encrypted off-host copy and restrict backup-directory permissions. The database snapshot is consistent while the service is live, but changing application secrets during backup is unsupported.

For a direct installation, set the same configuration environment and run `.venv/bin/python -m feedback.maintenance backup /absolute/new/backup-directory`. The parent directory must already exist.

## SQLite restore

Stop the service before replacing files. Preserve the current volume as a separate recovery copy first. Restore into a new volume/directory, so rollback remains possible. Do not overlay a restored database on stale `-wal`/`-shm` files.

One supported Compose procedure:

```sh
docker compose stop supaknock
# Keep the current volume as-is. Name a new volume for this restore.
docker volume create suggestion-box-restored
# Use a one-off container to populate the new volume; it initializes correct ownership.
docker compose run --no-deps -d --name suggestion-box-restore \
  -v suggestion-box-restored:/data supaknock sleep infinity
docker cp ./backup-2026-10-02/. suggestion-box-restore:/data/
docker exec --user 0 suggestion-box-restore chown -R 10001:10001 /data
docker rm -f suggestion-box-restore
```

Point the Compose `feedback_data` volume at that new volume using an override:

```yaml
# compose.restore.yaml
volumes:
  feedback_data:
    external: true
    name: suggestion-box-restored
```

```sh
docker compose -f compose.yaml -f compose.restore.yaml up -d --force-recreate supaknock
```

If `APPLICATION_SECRET` is set in `.env`, it must match the restored `application.key`; otherwise unset it so the file is used. On a Linux host, copied files must be readable by UID 10001. With ordinary Docker `cp` into a container, use `docker cp -a` only when ownership is deliberately compatible; if required use a root one-off container to `chown -R 10001:10001 /data` on the **new restore volume**. The service itself remains non-root. Test `/readyz`, login, project/board lists, sample requests/votes, and delivery inspection before directing public traffic to the restored instance.

Restored sessions/keys remain valid and delivery jobs resume, including already received events restored from an older backup. Consumers must deduplicate by event ID. A restored in-flight job waits until its recorded lease expires.

## PostgreSQL and Supabase restore

Stop the app before restoring and leave its current database and local volume intact. Provision a **new destination database or Supabase project with no `supaknock` schema**. Use a PostgreSQL server version supported by this app and compatible with the source archive. Do not start SupaKnock against that target before restoring; startup would create its initial schema. The same backup format works for PostgreSQL and Supabase, but it cannot be restored as SQLite.

Use `pg_restore` from the same major version as the destination server, and restore to a version compatible with the source backup. For a same-version restore, both tools and both databases have the same major version. The image bundles version 15 tools at `/opt/postgresql/15/bin/`, version 16 tools at `/opt/postgresql/16/bin/`, and version 17 tools at `/usr/lib/postgresql/17/bin/`. For a direct installation, put the matching tool directory first on PATH. Do not use the unqualified system `pg_restore` against an older server without checking `pg_restore --version`. Set `PGHOST`, `PGPORT`, `PGUSER` and `PGDATABASE` to the **new destination**, plus `PGSSLMODE=require` or your verified certificate settings. For Supabase use a direct or session connection. The command prompts for the destination database password so it is not placed in shell history or process arguments:

```sh
pg_restore --host="$PGHOST" --port="${PGPORT:-5432}" \
  --username="$PGUSER" --dbname="$PGDATABASE" --password \
  --single-transaction --exit-on-error --no-owner --no-acl \
  ./backup-2026-10-02/feedback.dump
```

Use a role that can create and own the `supaknock` schema. Do not add `--clean`, `--create` or commands to drop the old schema. A restore error must be investigated before starting the app; it is not a reason to erase the original database. See [the PostgreSQL restore options](https://www.postgresql.org/docs/current/app-pgrestore.html).

Prepare a new local volume using the SQLite procedure above, copying the bundle's `setup.token` and, if you preserved one, its matching complete model cache. Set `APPLICATION_SECRET` to the bundle's `application.key` value in your private deployment environment; for remote providers the local key file alone is insufficient. Set `DATABASE_PROVIDER` and `DATABASE_URL` to the restored destination. Retain the original database credentials separately because the backup bundle does not contain them.

Start one app instance with that configuration. Confirm `/readyz`, administrator login, existing projects and boards, feedback, chat, credential decryption and delivery inspection before switching public traffic. The restored installation identity and existing sessions remain the same. Delivery jobs can resume and repeat deliveries that happened after the snapshot, so receivers must deduplicate by event ID. A cacheless restore keeps ordinary support available and requires explicit model installation before AI can resume.

Keep `supaknock` outside Supabase's exposed API schemas and remove access for anonymous/authenticated API roles. The restore deliberately omits owner and ACL metadata; the destination login owns the restored objects and the app applies its private-schema protections at startup. Preserve the old installation until verification is complete, then manage its retirement separately.

## Upgrades and rollback

1. Create and copy a backup bundle before upgrading.
2. Stop the service, update the source or pinned image, rebuild, and recreate with the same volume.
3. Startup applies provider-specific migrations transactionally, serialized by SQLite locks or PostgreSQL advisory locks. Failure stops startup and does not erase/rebuild the database. A newer schema also stops an older application.
4. Check readiness, login, request and admin flows, and delivery history.
5. To roll back an incompatible migration, restore the pre-upgrade database **and key** into a new volume or PostgreSQL destination and run the previous application version. Do not just downgrade against a newer schema.

Deploy one instance and avoid parallel upgrades. SQLite, PostgreSQL and Supabase are supported configuration choices; no automatic database transfer command exists. Changing a provider or URL does not move data. AI generation providers are a separate setting. Approved knowledge and embeddings stay in the configured installation database, while model weights remain local.

## Private connection metadata

Migration008 records connection IP addresses for new feedback submissions and their activity, chat conversations/messages, and successful native-account signups/logins. Initial setup and accounts created by an administrator also have an activity record; for administrator-created accounts, the address is the creating administrator's connection. Older records remain **Not recorded**. Successful logins are retained independently of session expiry/revocation. Failed attempts, page views, vote history and location lookup are not added by this feature.

Only signed-in native administrators can read these details. The web interface shows them in request details/history and conversation information/message details, with **Manage → Account activity** for account history. The iPhone app exposes conversation/request information and **Settings → Account activity**. Participants, chat customers and project API credentials (including `manage` and `audience:read`) do not receive IP metadata. It is excluded from webhook/push payloads and notification inbox items.

The server records the validated ASGI connection address for HTTP and WebSocket sends; clients cannot provide an IP in request bodies. `TRUSTED_PROXY_IPS` defaults to empty. Behind an HTTPS proxy, set it to only the actual proxy IPs or narrowly scoped networks, and have that proxy supply `X-Forwarded-For`. Keep the application port reachable only through that proxy. Uvicorn resolves the address through its trusted-proxy handling; arbitrary `X-Forwarded-For`, `X-Real-IP` or Cloudflare headers from untrusted peers are not used. Avoid `*` on a reachable application port. If a CDN sits in front of the proxy, configure the trusted chain at the proxy as well. Verify a real request in the admin UI after deployment.

For Docker deployments, give the trusted proxy a stable address on its existing network and preserve that address and `TRUSTED_PROXY_IPS` in the deployment configuration. Avoid trusting an entire shared Docker network simply because its addresses can change. Changing proxy trust affects future activity; historical records containing the proxy address cannot reveal the original visitor address.

These addresses identify connections, not people. NAT/VPNs can share addresses, IPv6/mobile connections can change, and a trusted application's backend call records that backend's address. Direct visitor/customer chat calls record their actual client connection. No browser-supplied external ID or forwarded field changes this attribution.

IP metadata and account history remain in the installation's configured database and supported backups alongside their records. There is no automatic retention cleanup for this history in this release; session cleanup does not remove account activity. Include this collection and retention in your deployment's privacy information, and protect backups like the live database. No IP lookup or third-party transmission is introduced.

## Native account recovery

Email is not verified and no email reset flow is provided. An installation operator with local filesystem/container access can recover an **existing** native account:

```sh
docker compose exec -it supaknock python -m feedback.maintenance recover-account admin@example.com
```

Enter the new password interactively; it is not in command arguments or logs. The command preserves the role and identity, changes the Argon2id hash, and revokes all existing sessions. It cannot claim setup or create an administrator. Additional admins are created by an authenticated admin through the website/API. Use recovery only on the installation you administer.

The constrained local maintenance module supplies backup and recovery, not a general-purpose management CLI. Integration clients, future MCP/CLI, and native apps must use the API rather than mutate storage.

## Optional allowed topics

Open a project's **AI support → Allowed topics** to prepare a scope, a short public summary, allowed/disallowed examples, Balanced/Strict and up to five manually pasted refusal messages. This policy is separate from `soul.md` and starts off. `{scope}` in a refusal inserts the public summary. Plain-text messages support no other interpolation, HTML or links.

Decision keys are encrypted in the configured database. Explicitly share a same-provider answer key or add a separate KodeKey decision key; model capability evidence remains role-specific. Both roles share the 99-key provider cap, provider-account cooldowns and project AI budgets. Customer messages and bounded recent context go to the chosen decision provider only when the gate is enabled; knowledge originals and customer identity are excluded. Human support remains independent.

Jev is advertised by KodeKey, but the tested gateway routes did not expose typed decisions. Activation and ordinary decision probes therefore remain blocked, with an explanation in the interface. Catalogue listing does not verify capability. Balanced/Strict thresholds are provisional; live calibration and a verified gateway contract remain prerequisites. See [status and contracts](ai-web-admin.md) and diagnostic evidence (internal historical record). Do not change database capability flags to bypass setup. Existing chat proceeds as before while topic restrictions are off.

The fixed backup bundle includes topic policy, decision receipts, encrypted keys and role references; restore it together with its `application.key`. No additional database or model download is required for the dormant guardrail setup.

## Optional AI support

The AI implementation is present in this checkout, but customer rollout is not yet approved. Check current [progress](../PROGRESS.md) and the actual-model quality results (internal historical record) before choosing a build. Local candidate tags are verification artifacts; no publication or production deployment is implied.

Use one instance and the existing local volume. The app image contains the CPU embedding runtime, with no model weights and no startup download. The selected reviewed generation adapter calls KodeKey, Google Gemini or Mistral; conversation generation is not a local LLM. Website originals, uploaded Markdown, personality, vectors, configuration, job state, private guest ownership and report receipts all live in the configured installation database. There is no separate embedding database service; PostgreSQL does not require pgvector for the bounded knowledge corpus.

A native administrator opens **AI runtime** to explicitly download the pinned EmbeddingGemma model. Each installation owner signs in to their own Hugging Face account, accepts the license on the [EmbeddingGemma model page](https://huggingface.co/google/embeddinggemma-300m), and creates an [access token](https://huggingface.co/settings/tokens) in that account with read access to the model. They paste their token into the administration form. There is no bundled developer token or fallback credential, and the application cannot grant model access. The token is encrypted while the download is pending and removed on completion, failure or cancellation; installed inference needs no token. Files occupy approximately 1.26 GB under `/data/models`; the download checks additional free space before starting. A previous cache may temporarily coexist with its replacement. Tokens are never baked into images or placed in command lines. Download refusal, cancellation or corruption keeps AI unavailable and preserves ordinary feedback and human chat.

For resource planning, the actual local query/import benchmark passed in a container limited to two CPU cores and 3 GiB of memory. This is a tested fixture size, not a capacity guarantee for arbitrary sites or traffic. See the fixed workload and limits (internal historical record) and measured results (internal historical record). AMD64 measurements used emulation; native AMD64 capacity was not measured.

In each project's **AI support** settings, choose KodeKey, Google Gemini or Mistral, save that provider’s own API key, refresh its catalogue, select an exact model, and explicitly test the required capabilities. Obtain your own key from [Google AI Studio](https://aistudio.google.com/apikey) for Gemini or [Mistral Studio](https://console.mistral.ai/home?profile_dialog=api-keys) for Mistral. Switching providers clears the previous key and model selection unless a replacement key is explicitly supplied; refresh and test the new selection. Catalogue membership alone does not establish usable access. Import and approve the intended website/Markdown knowledge, then check supported, unsupported and conflicting questions in Preview. Configure personality and your feedback destinations. Setup, model verification and Preview never enable customer AI. AI chat, each of the five tools, and website guest reporting must each be enabled explicitly. See the [administration guide](ai-web-admin.md) and [widget behavior](ai-widget-contract.md).

Guest reporting remains browser/session scoped. Draft preparation does not create a request or an account; exact customer confirmation creates the private pending report. The widget explains that a guest who wants ongoing account-based tracking should instead use the real feedback page. Custom feature/bug links override a project-wide feedback link; the native board is the fallback. No automatic identity recognition, account claiming, transcript transfer or external custom request URL is invented. Accepted reports remain permanently locked against creator edits/deletion.

### Controlled live evaluation

After fresh model download and activation are verified, the owner can evaluate a tested candidate in a private staging installation or through native-admin Preview with customer AI still disabled. Preview uses the real provider and approved knowledge but disables assistant tools and does not create a customer conversation. Use only the authorized exact test model, set bounded usage allowances, and start with synthetic or deliberately selected nonprivate support material.

Keep customer projects, assistant tools and guest submissions off while the live answer-quality gate remains unresolved. A private widget test can use a separate test project and website origin; enable its tools individually only when testing the corresponding confirmation and handoff behavior. Take the supported backup before an upgrade and preserve the existing volume for recovery. An owner-only evaluation is not customer rollout acceptance: provider reliability, the fixed quality corpus and account production suitability/quotas still need to pass before enabling customer AI.

### AI backup and restoration

The normal backup operation above already includes authoritative AI data and effective application secrets. It intentionally excludes model weights. Restore the database and secret bundle together. A cacheless restore preserves knowledge and guest records, makes AI unavailable, and starts no implicit model acquisition. After an explicit model installation, review project settings and previews before reenabling AI. Human chat and existing authorized report ownership do not depend on AI being enabled.

To preserve warm-cache operation, stop the installation and separately preserve its complete `/data/models` directory together with a matching supported database/secret backup. Restore that directory into the new volume before the first startup, preserving its names and UID 10001 ownership. Do not rename individual cache directories, edit database cache identifiers, copy incomplete download staging as an active model, or overwrite the running installation. Startup verifies the pinned manifest and file hashes; absent, mismatched or corrupt files fail closed. The old volume remains the rollback copy. No standard backup is enlarged by weights automatically.

Changing the provider, key or exact model cancels unfinished replies/drafts and requires new capability verification. Source changes and human takeover also fence late output. Uncertain provider requests may have consumed quota even when no answer is delivered; reserved usage and reported tokens are separate. Inspect safe status codes in administration and use the direct human controls during failures. Logs and notifications omit raw keys, upstream error bodies and customer message bodies.

## Connected-app configuration and restore

Connected apps need no new service, volume, model or environment variable. Migration 019 adds encrypted connection configuration, minimal identity bindings, metadata-only configuration audit and display timezone to the installation database. Remote user/event payloads are never stored. Startup validates token ciphertext with the same effective application key and fails closed if decryption fails; it makes no app probes. Existing backups preserve configuration and identity binding. Restore invalidates in-memory continuation/test receipts and performs fresh checks on the next explicit read.

A connection can be disabled/removed in project settings. Removal clears endpoint/token but keeps its nonsecret app binding; use a new project for another app identity. Global outbound limits and public-only HTTPS protections apply independently of webhook settings. See [connected-app troubleshooting](connected-apps.md).

Repository Compose uses the `feedback` service and builds from the checkout. Operators using the published Docker Hub image should pull and recreate their own existing service while preserving its volume; this change does not edit external deployment configurations or imply a new `latest` publication.

### Connected AI tool data

Ordinary database backups include encrypted MCP connection credentials, project approvals and excerpts cited in committed chat replies. Keep the application encryption key with the backup. Unused excerpts, live MCP sessions and transient rate counters are not durable. Restored connections live-discover before their next call. See [the MCP integration guide](mcp-tools.md) for privacy and endpoint restrictions.
