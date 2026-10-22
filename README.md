<p align="center">
  <a href="https://supaknock.com"><img src="feedback/web/brand.svg" width="76" height="85" alt="SupaKnock"></a>
</p>

<h1 align="center">SupaKnock</h1>

<p align="center"><strong>Support for your app. On your server.</strong></p>
<p align="center">Customer chat, feedback boards, and forms for app makers and small teams.</p>

<p align="center">
  <a href="https://supaknock.com">Website</a> ·
  <a href="#quick-start">Quick start</a> ·
  <a href="https://supaknock.com/api-docs">API guide</a> ·
  <a href="docs/README.md">Documentation</a> ·
  <a href="https://supaknock.com/iphone">iPhone preview</a>
</p>

<p align="center">
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-3ecf8e?style=flat-square" alt="MIT licensed"></a>
  <a href="https://hub.docker.com/r/taylorarronj/supaknock"><img src="https://img.shields.io/badge/Docker-amd64%20%7C%20arm64-2496ed?style=flat-square&logo=docker&logoColor=white" alt="Docker: AMD64 and ARM64"></a>
  <a href="docs/database-providers.md"><img src="https://img.shields.io/badge/database-SQLite%20%7C%20PostgreSQL-4169e1?style=flat-square" alt="SQLite and PostgreSQL"></a>
</p>

---

SupaKnock gives your team one place to reply to customers, review ideas, and collect submissions. Add chat to your website, share a feedback board, or connect your own app through the API.

Run it in a single Docker container. SQLite works out of the box, with PostgreSQL and Supabase available when you need them. The core application needs no hosted SupaKnock account, email provider, Redis, or AI subscription.

**[Explore SupaKnock →](https://supaknock.com)**

## A small support desk, with room to grow

| | What you can do |
| --- | --- |
| **Customer chat** | Embed a chat widget, reply from a shared inbox, and support anonymous visitors or authenticated customers. [Chat guide →](docs/chat.md) |
| **Feedback boards** | Review requests before publishing, collect votes, and track ideas from consideration to shipped. [Feedback guide →](docs/integration.md) |
| **Forms** | Collect submissions through your backend or an explicitly enabled public form link. Answers stay private to administrators. [Forms guide →](docs/forms.md) |
| **AI support, optional** | Answer from approved knowledge, choose your provider, and enable individual tools. AI starts off. [AI guide →](docs/ai-providers.md) |
| **Connected apps** | Read customer profiles, recent signups, and activity from your app alongside support history. [Connection guide →](docs/connected-apps.md) |
| **API and webhooks** | Build your own experience with a versioned JSON API and signed webhook deliveries with retries. [API guide →](https://supaknock.com/api-docs) |

Shared notifications and configurable business hours help your team follow up. An optional iPhone companion is **coming soon**; [preview it and request early access](https://supaknock.com/iphone). Its push relay is packaged separately.

## Quick start

### 1. Use the published Docker Hub image

The prebuilt application image is **[`taylorarronj/supaknock:latest` on Docker Hub](https://hub.docker.com/r/taylorarronj/supaknock)**. Its full image address is `docker.io/taylorarronj/supaknock:latest`, with builds for AMD64 and ARM64. You can run it without cloning this repository or building the application yourself.

Install [Docker with Compose](https://docs.docker.com/compose/install/), then create a **new installation directory**:

```sh
mkdir supaknock
cd supaknock
```

### 2. Create `compose.yaml`

Save the following as `compose.yaml` in that directory. The `image` line tells Docker Compose to pull the published image from Docker Hub. The named volume keeps your SQLite database and generated secrets across container restarts and upgrades.

```yaml
name: suggestionbox

services:
  supaknock:
    image: taylorarronj/supaknock:latest
    restart: unless-stopped
    ports:
      - "127.0.0.1:8000:8000"
    environment:
      PUBLIC_BASE_URL: http://localhost:8000
      COOKIE_SECURE: "false"
      DATABASE_PROVIDER: sqlite
      DATABASE_URL: sqlite:///data/feedback.sqlite
    volumes:
      - feedback_data:/data
    stop_grace_period: 20s
    security_opt:
      - no-new-privileges:true
    cap_drop:
      - ALL
volumes:
  feedback_data:
```

This is the same configuration as [compose.image.yaml](compose.image.yaml). It runs locally at `http://localhost:8000`; see [Put it on your domain](#put-it-on-your-domain) before exposing it publicly.

### 3. Pull the image and start SupaKnock

Run these commands from the directory containing `compose.yaml`:

```sh
docker compose pull supaknock
docker compose up -d supaknock
docker compose ps
```

The first command downloads `taylorarronj/supaknock:latest` from Docker Hub. The second starts it in the background. Wait for `docker compose ps` to show the service as **healthy** before continuing.

### 4. Read `setup.token` and create your administrator

On first startup, SupaKnock generates a private setup token at `/data/setup.token` inside the container. Read it with:

```sh
docker compose exec supaknock cat /data/setup.token
```

Copy the printed token, open **[http://localhost:8000/setup](http://localhost:8000/setup)**, and enter it with the email and password for your first administrator account. Keep the token private. If you explicitly set a `SETUP_TOKEN` environment variable, use that value instead of the generated file.

Setup closes permanently after the first administrator is created. For later visits, sign in at **[http://localhost:8000/login](http://localhost:8000/login)** with your administrator email and password; you do not need the setup token again.

From the workspace, create a project, add a feedback board, enable website chat, or create a form. Share direct board and project links so visitors can read published feedback; participants sign in to submit ideas or vote.

### Put it on your domain

Set `PUBLIC_BASE_URL` to your HTTPS origin and `COOKIE_SECURE` to `"true"`, then put an HTTPS reverse proxy in front of port 8000. Set `TRUSTED_PROXY_IPS` to the addresses of the proxies you actually trust.

Run **one application instance** and preserve its volume and encryption key. For an existing installation, retain its Compose project name and volume; changing those can select an empty volume. Never use `docker compose down -v` to upgrade.

See [configuration](docs/configuration.md), [PostgreSQL and Supabase setup](docs/database-providers.md), and [backups, recovery, and upgrades](docs/operations.md).

## Make it part of your app

Use the built-in widget and pages, or build your own interface against `/api/v1`. Backend project credentials stay on your server. SupaKnock keeps your user IDs opaque and scoped to each project; an external user ID is never a credential.

- **Website chat:** [install the widget and connect authenticated customers](docs/chat.md).
- **Feedback and votes:** [integrate with your backend](docs/integration.md).
- **Forms:** [submit from your backend or enable a hosted public page](docs/forms.md).
- **Events:** [verify signatures and handle webhook retries](docs/webhooks.md).
- **AI tools:** [connect approved public, read-only MCP services](docs/mcp-tools.md).

Read the **[online API guide](https://supaknock.com/api-docs)** or use the [OpenAPI schema](docs/openapi.json). Every running installation also serves its schema at `/api/v1/openapi.json`.

## Built to keep you in control

New feedback stays private until accepted. Form answers and administrator views are private. Public links expose published feedback, with no public installation-wide project directory.

AI support, assistant tools, and public form pages are opt-in. Local knowledge search uses EmbeddingGemma, with weights downloaded only after an administrator explicitly requests them. External AI and embedding providers receive the data needed for their enabled features; [review the setup and data flow](docs/ai-providers.md) before activating them.

Your application records stay in your installation database. Backups include the encryption key needed to restore encrypted settings. [Read the operations guide →](docs/operations.md)

## Develop locally

This repository contains the application, its API, migrations, tests, and Docker build. The product website, iPhone source, and optional push relay are maintained separately. You can build and run the application from this repository alone.

To build the container from source:

```sh
cp .env.example .env
docker compose up -d --build
```

For Python development, use Python 3.12+ and patched SQLite (3.51.3+ or a vendor backport of the WAL-reset fix):

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.lock
.venv/bin/pip install -e '.[test]'
.venv/bin/python -m pytest -q

# Use an absolute path for your local database.
DATABASE_URL=sqlite:///absolute/path/to/data/feedback.sqlite .venv/bin/python -m feedback
```

The Python server reads process environment variables; it does not automatically load `.env`. Optional local AI dependencies have [separate setup instructions](docs/ai-runtime.md).

Start with the [architecture](docs/architecture.md), [development boundaries](AGENTS.md), and [documentation index](docs/README.md). See [verification](docs/verification.md) for completed checks and known test failures, and [progress](PROGRESS.md) for current work. This checkout may contain changes newer than a published Docker image.

## License

[MIT](LICENSE). Self-host it, adapt it, and build it into your own products.

<p align="center"><a href="https://supaknock.com">supaknock.com</a></p>
