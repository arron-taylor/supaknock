# Choose a database

SQLite remains the default. PostgreSQL and Supabase use the same PostgreSQL adapter; Supabase is a hosted PostgreSQL connection, not a Supabase API integration. Your web app, widgets, native client, accounts and API keep working through SupaKnock.

These options are implemented in this checkout. Check [verification](verification.md) for the databases and environments actually tested. An available configuration is not a claim that this build has been published or deployed.

| Provider | What you configure | Persistent storage |
| --- | --- | --- |
| `sqlite` | Nothing extra for the default Compose setup | Database, generated secrets and optional model weights in `/data` |
| `postgres` | PostgreSQL URI and an explicit application encryption key | Application records in PostgreSQL; setup token and optional model weights in `/data` |
| `supabase` | Supabase direct/session connection URI and an explicit application encryption key | Application records in the private PostgreSQL schema; setup token and optional model weights in `/data` |

Use one application instance with every provider. Choosing PostgreSQL does not enable multiple app replicas, serverless hosting, Supabase Auth or direct browser access to the database. Keep the local volume even with a remote database. There is no automatic transfer between databases: changing the URL points the app at a different installation.

## Keep SQLite

The existing configuration stays valid:

```dotenv
DATABASE_PROVIDER=sqlite
DATABASE_URL=sqlite:///data/feedback.sqlite
```

SQLite generates its encryption key and setup token beside the database unless you supply them. Keep that directory on a local persistent volume; do not put a SQLite WAL database on a network filesystem. `DATA_DIR` does not relocate the SQLite database or its secrets.

## Connect PostgreSQL

Create a dedicated database and a login that owns its `supaknock` schema. The app needs permission to create that schema and to create, alter, read and write its own objects. It does not need a database superuser. PostgreSQL 15–17 are the intended compatibility range; see verification for the versions exercised by this checkout.

Set these values in your private deployment environment:

```dotenv
DATABASE_PROVIDER=postgres
DATABASE_URL=postgresql://supaknock:URL_ENCODED_PASSWORD@db.example.com:5432/supaknock?sslmode=require
APPLICATION_SECRET=YOUR_FERNET_KEY
DATA_DIR=/data
```

Generate the key once, before the first remote-provider startup, then save it as `APPLICATION_SECRET` in your private `.env` or deployment secrets. With Docker Compose, no host Python dependencies are needed:

```sh
docker compose build
docker compose run --rm --no-deps --entrypoint python supaknock -c \
  'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'
```

If you installed SupaKnock directly with Python, use that environment:

```sh
python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'
```

Keep that exact key for this database through every restart, upgrade and restore. Remote providers require `APPLICATION_SECRET` explicitly; they never generate an encryption key from an empty local volume. A lost or changed key makes saved provider credentials, webhook secrets and other encrypted settings unreadable. Do not rotate it by replacing the environment value.

Use a `postgresql://` or `postgres://` URI. Percent-encode reserved characters in the username, password and database name. `DATABASE_PROVIDER=postgresql` is also accepted as an alias for `postgres`. The URL stays server-side and must not be put into a browser, mobile configuration, support log or Git.

Remote connections require TLS. SupaKnock adds `sslmode=require` when it is absent; use `sslmode=verify-full` with your provider's root certificate when you want hostname and certificate verification. Supply the certificate through `sslrootcert` and mount it read-only into the container. Plaintext is accepted only for explicit `postgres` loopback development connections using `sslmode=disable`. Private network addresses still require TLS. See the [PostgreSQL connection options](https://www.postgresql.org/docs/current/libpq-connect.html).

Supported URI query options are `sslmode`, `sslrootcert`, `sslcert`, `sslkey`, `connect_timeout`, `channel_binding` and `application_name`. Duplicate options, query overrides for host/user/database, custom search paths and multihost URLs are rejected. Connection timeout defaults to 10 seconds and may be set from 1 to 30 seconds. The adapter owns the schema and search path.

## Connect Supabase

In your Supabase project's **Connect** dialog, copy either its **Direct connection** URI or its **Session pooler** URI. Both use port 5432. Direct connections may require IPv6; the shared session pooler is the usual option for an IPv4-only server. The transaction pooler on port 6543 is rejected because migrations and connection-level behavior need a stable session. See [Supabase's connection guide](https://supabase.com/docs/guides/database/connecting-to-postgres).

```dotenv
DATABASE_PROVIDER=supabase
DATABASE_URL=postgresql://postgres.PROJECT_REF:URL_ENCODED_PASSWORD@aws-0-REGION.pooler.supabase.com:5432/postgres?sslmode=require
APPLICATION_SECRET=YOUR_FERNET_KEY
DATA_DIR=/data
```

Copy the actual host and username from your project; those placeholders are not a working connection. Use the database password, not an anonymous key or a service-role API key. For a direct connection the username and host differ from the session-pooler example.

SupaKnock creates its tables in `supaknock`, outside the usual exposed `public` schema. Keep `supaknock` out of Supabase's exposed API schemas, and do not grant Supabase's anonymous or authenticated client roles access. All reads and writes go through the SupaKnock API and its existing authorization checks. A dedicated Supabase project keeps its data and access settings separate from other apps.

## Start and verify

For a fresh installation, set the environment before the first start and run the usual command:

```sh
docker compose up -d --build
```

Compose keeps `/data` mounted for local state. For a direct Python installation, `DATA_DIR` must be an absolute writable local directory. Set `PUBLIC_BASE_URL`, HTTPS cookies and trusted proxies as described in the [README](../README.md).

Startup applies this provider's migrations before serving traffic. A failed connection or migration fails startup; there is no SQLite fallback. Check `/readyz`, complete setup, and verify a project, feedback submission and chat message before exposing a new installation.

Switching the provider or URI requires a restart. It does not move accounts, projects, sessions or messages. Back up the existing installation first and leave it intact. This release has no SQLite-to-PostgreSQL importer. To restore an existing PostgreSQL installation into another PostgreSQL or Supabase destination, use the [backup and restore procedure](operations.md#postgresql-and-supabase-restore).

## Backups

The same maintenance command creates a private bundle for every provider:

```sh
docker compose exec supaknock python -m feedback.maintenance backup /data/backup-new
docker compose cp supaknock:/data/backup-new ./backup-new
```

SQLite produces `feedback.sqlite`; PostgreSQL and Supabase produce `feedback.dump`, a custom-format archive containing only the `supaknock` schema. Both include the effective `application.key` and setup token. PostgreSQL bundles also include a nonsecret `backup.json` format description. Model weights and database connection credentials are excluded. The remote database's own backup service cannot preserve an encryption key stored only in your app deployment.

Remote backup requires `pg_dump` from the **same PostgreSQL major version** as the server. The app image includes versions 15, 16 and 17, and selects the matching binary automatically. Direct Python installations must install the matching client on PATH. A newer client is not an interchangeable replacement: its archive or restore commands may contain settings an older server does not support. The command fails before creating a backup if the matching tool is unavailable; it never substitutes a partial SQLite backup. Use matching-major `pg_restore` when restoring, too; the image includes those binaries alongside `pg_dump`. See [PostgreSQL's pg_dump documentation](https://www.postgresql.org/docs/current/app-pgdump.html) and the full [operations guide](operations.md).
