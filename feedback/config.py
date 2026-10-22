from dataclasses import dataclass, field
import ipaddress
from pathlib import Path
import os
import secrets
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from cryptography.fernet import Fernet


def persistent_secret(path: Path, generate):
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return path.read_text().strip()
    value = generate()
    with os.fdopen(fd, "w") as f:
        f.write(value)
        f.flush()
        os.fsync(f.fileno())
    return value


def postgres_url(value: str | None, provider: str = 'postgres') -> str:
    """Validate connection options without including credentials in failures.

    A small URI-only surface prevents libpq query options from overriding the
    host on which TLS checks were based. The returned URI remains a secret.
    """
    message = 'DATABASE_URL must be a PostgreSQL URI with a host, username and database name'
    if not value or any(ord(char) < 32 for char in value):
        raise RuntimeError(message)
    try:
        parsed = urlsplit(value)
        port = parsed.port
        if (parsed.scheme not in ('postgres', 'postgresql') or not parsed.hostname
                or not parsed.username or parsed.path in ('', '/') or parsed.fragment
                or ',' in parsed.netloc or '/' in parsed.path[1:] or port == 0):
            raise ValueError()
        options = parse_qsl(parsed.query, keep_blank_values=True, strict_parsing=True)
    except (ValueError, TypeError):
        raise RuntimeError(message) from None
    allowed = {'sslmode', 'sslrootcert', 'sslcert', 'sslkey',
               'connect_timeout', 'channel_binding', 'application_name'}
    if len({name for name, _ in options}) != len(options) or any(name not in allowed for name, _ in options):
        raise RuntimeError('DATABASE_URL has duplicate or unsupported connection options')
    settings = dict(options)
    mode = settings.get('sslmode', 'require')
    try:
        loopback = ipaddress.ip_address(parsed.hostname).is_loopback
    except ValueError:
        loopback = parsed.hostname.lower() == 'localhost'
    if mode not in ('require', 'verify-ca', 'verify-full') and not (provider != 'supabase' and loopback and mode == 'disable'):
        raise RuntimeError('Remote databases require TLS: use sslmode=require or verify-full')
    supabase_host = parsed.hostname.rstrip('.').endswith(('.supabase.com', '.supabase.co'))
    if (provider == 'supabase' or supabase_host) and port == 6543:
        raise RuntimeError('Use a Supabase direct connection or session pooler on port 5432; transaction pooling is unsupported')
    if 'connect_timeout' in settings:
        try:
            if not 1 <= int(settings['connect_timeout']) <= 30:
                raise ValueError()
        except ValueError:
            raise RuntimeError('DATABASE_URL connect_timeout must be between 1 and 30 seconds') from None
    settings.setdefault('sslmode', mode)
    settings.setdefault('connect_timeout', '10')
    return urlunsplit(parsed._replace(query=urlencode(settings)))


@dataclass(frozen=True)
class Config:
    database_path: Path
    secret: str = field(repr=False)
    setup_token: str = field(repr=False)
    base_url: str = "http://localhost:8000"
    cookie_secure: bool = False
    registration: bool = True
    allow_internal_webhooks: bool = False
    session_seconds: int = 604800
    rate_limit: int = 60
    auth_rate_limit: int = 10
    worker: bool = True
    allow_internal_relay: bool = False
    allow_internal_ai_endpoints: bool = False
    database_provider: str = 'sqlite'
    database_url: str | None = field(default=None, repr=False)
    data_dir: Path | None = None

    @property
    def local_data_dir(self) -> Path:
        return self.data_dir if self.data_dir is not None else self.database_path.parent

    @classmethod
    def from_env(cls):
        provider = os.getenv("DATABASE_PROVIDER", "sqlite").strip().lower()
        if provider == 'postgresql':
            provider = 'postgres'
        if provider not in ('sqlite', 'postgres', 'supabase'):
            raise RuntimeError('Unsupported database provider; choose sqlite, postgres or supabase')
        if provider == 'sqlite':
            url = os.getenv("DATABASE_URL", "sqlite:///data/feedback.sqlite")
            if not url.startswith("sqlite:///") or not url[10:] or "?" in url or '#' in url:
                raise RuntimeError("DATABASE_URL must be sqlite:///absolute/path (application convention)")
            path = Path("/" + url[10:].lstrip("/"))
            data_dir = path.parent
            connection_url = None
        else:
            connection_url = postgres_url(os.getenv('DATABASE_URL'), provider)
            data_dir = Path(os.getenv('DATA_DIR', '/data'))
            if not data_dir.is_absolute():
                raise RuntimeError('DATA_DIR must be an absolute local path')
            path = data_dir / 'feedback.sqlite'  # Compatibility field; never opened remotely.
            if not os.getenv('APPLICATION_SECRET'):
                raise RuntimeError('PostgreSQL and Supabase require an explicit APPLICATION_SECRET; preserve the same key across restarts and restores')
        base = os.getenv("PUBLIC_BASE_URL", "http://localhost:8000").rstrip("/")
        parsed = urlsplit(base)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.path or parsed.query or parsed.fragment or parsed.username:
            raise RuntimeError("PUBLIC_BASE_URL must be an http(s) origin")
        if provider == 'sqlite' and path.is_file() and not os.getenv("APPLICATION_SECRET") and not (data_dir / "application.key").is_file():
            raise RuntimeError("Missing persistent application.key for existing database; restore it before starting")
        secret = os.getenv("APPLICATION_SECRET") or persistent_secret(data_dir / "application.key", lambda: Fernet.generate_key().decode())
        Fernet(secret.encode())  # Fail before startup if the encryption key is invalid.
        data_dir.mkdir(parents=True, exist_ok=True)
        token = os.getenv("SETUP_TOKEN") or persistent_secret(data_dir / "setup.token", lambda: secrets.token_urlsafe(32))
        if len(token) < 24:
            raise RuntimeError("SETUP_TOKEN must contain at least 24 characters")
        secure = os.getenv("COOKIE_SECURE", str(parsed.scheme == "https")).lower() == "true"
        if parsed.scheme == "https" and not secure:
            raise RuntimeError("HTTPS deployments require COOKIE_SECURE=true")
        return cls(path, secret, token, base, secure,
                   os.getenv("REGISTRATION_ENABLED", "true").lower() == "true",
                   os.getenv("ALLOW_INTERNAL_WEBHOOKS", "false").lower() == "true",
                   rate_limit=int(os.getenv("RATE_LIMIT_PER_MINUTE", "60")),
                   auth_rate_limit=int(os.getenv("AUTH_RATE_LIMIT_PER_MINUTE", "10")),
                   allow_internal_relay=os.getenv("ALLOW_INTERNAL_RELAY","false").lower()=="true",
                   database_provider=provider, database_url=connection_url,
                   allow_internal_ai_endpoints=os.getenv('ALLOW_INTERNAL_AI_ENDPOINTS', 'false').lower() == 'true',
                   data_dir=data_dir if provider != 'sqlite' else None)
