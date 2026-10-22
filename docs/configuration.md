# Configuration

| Variable | Meaning / default |
| --- | --- |
| `PUBLIC_BASE_URL` | Browser origin and canonical URLs; `http://localhost:8000` locally |
| `PORT` | HTTP listening port; `8000` (Compose fixes the container port) |
| `DATABASE_PROVIDER` | `sqlite` (default), `postgres` or `supabase`; `postgresql` is an alias for `postgres` |
| `DATABASE_URL` | `sqlite:///data/feedback.sqlite` by default; PostgreSQL providers require a private PostgreSQL connection URI |
| `DATA_DIR` | Local secrets/model directory for PostgreSQL providers; `/data`. Compose fixes it to its persistent `/data` volume |
| `APPLICATION_SECRET` | Fernet encryption key; generated beside the SQLite database if omitted. Required explicitly for PostgreSQL/Supabase |
| `SETUP_TOKEN` | At least 24 random characters; generated in `/data/setup.token` if omitted |
| `COOKIE_SECURE` | Defaults to true for HTTPS; Compose’s local example sets false |
| `REGISTRATION_ENABLED` | `true`; signup always creates participants |
| `ALLOW_INTERNAL_AI_ENDPOINTS` | `false`; explicit opt-in for private/custom HTTP chat and embedding endpoints |
| `ALLOW_INTERNAL_RELAY` | `false`; explicit development opt-in for private relay endpoints |
| `ALLOW_INTERNAL_WEBHOOKS` | `false`; explicit opt-in for private/loopback destinations |
| `TRUSTED_PROXY_IPS` | Empty; accept forwarded headers only from listed proxies |
| `RATE_LIMIT_PER_MINUTE` | `60` mutations per client IP per minute |
| `AUTH_RATE_LIMIT_PER_MINUTE` | `10` signup/login/setup/password/logout attempts per IP per minute |
| `HOST_PORT`, `BIND_ADDRESS` | Compose host binding; `8000`, `127.0.0.1` |

Changing database providers/URLs requires a restart and does not transfer data. Keep SQLite databases on local storage. PostgreSQL and Supabase use a private `supaknock` schema and require TLS for remote connections; Supabase uses a direct or session-pooler URL. Keep the local volume for the setup token and optional model weights. Preserve the same effective application key in backups and every future deployment. See [provider setup and limitations](database-providers.md).

Generate a deployment-supplied encryption key with:

```sh
python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'
```

Passwords use Argon2id. Emails are unverified login identifiers; no email verification, password-reset email, SSO, or social login is implemented. Sessions are opaque, hashed in the configured database, expire after seven days, and are revoked on logout/password change. Browser mutations require CSRF tokens and an exact Origin. HTTP local cookies are intentionally allowed for development; public deployments require HTTPS and Secure cookies.


See [installation](../README.md#quick-start), [database providers](database-providers.md), and [operations and backups](operations.md).
