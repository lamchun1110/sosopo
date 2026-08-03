"""Sosopo: a small, self-hosted social publishing dashboard."""

from __future__ import annotations

import base64
import binascii
import hashlib
import ipaddress
import io
import json
import logging
import mimetypes
import os
import re
import secrets
import sqlite3
import threading
import time
import uuid
from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, unquote, urlencode, urlparse
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from cryptography.fernet import Fernet, InvalidToken
import jwt
from jwt import PyJWKClient
from PIL import Image, UnidentifiedImageError


def environment_value(name: str) -> str:
    """Read an environment value or a Docker/Kubernetes secret file reference."""
    file_name = os.environ.get(f"{name}_FILE", "").strip()
    if file_name:
        try:
            return Path(file_name).read_text(encoding="utf-8").strip()
        except OSError as error:
            raise RuntimeError(f"Could not read {name}_FILE.") from error
    return os.environ.get(name, "").strip()


APP_DIR = Path(__file__).parent
DATA_DIR = Path(os.environ.get("SOSOPO_DATA_DIR", os.environ.get("SOCIAL_DESK_DATA_DIR", APP_DIR.parent / "data")))
DB_PATH = DATA_DIR / "sosopo.sqlite3"
LEGACY_DB_PATH = DATA_DIR / "social-desk.sqlite3"
DATABASE_URL = environment_value("DATABASE_URL")
UPLOADS_DIR = DATA_DIR / "uploads"
MAX_POST_LENGTH = 5_000
MAX_UPLOAD_BYTES = 5 * 1024 * 1024
MAX_IMAGE_PIXELS = 25_000_000
MAX_POST_MEDIA = 10
MAX_ATTEMPTS = 3
POLL_SECONDS = 15
RETRY_BASE_SECONDS = 30
RETRY_MAX_SECONDS = 60 * 60
WORKER_HEARTBEAT_SECONDS = POLL_SECONDS * 3
PUBLISHING_LEASE_SECONDS = 5 * 60
DEFAULT_AUDIT_RETENTION_DAYS = 365
SESSION_SECONDS = 60 * 60 * 24 * 14
OIDC_STATE_SECONDS = 10 * 60
SOCIAL_OAUTH_STATE_SECONDS = 10 * 60
AUTH_REQUESTS_PER_MINUTE = 10
WRITE_REQUESTS_PER_MINUTE = 60
CHANNELS = ("Facebook", "Instagram", "Threads", "X", "Telegram", "Discord", "LinkedIn")
IMAGE_TYPES = {"image/gif": ".gif", "image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}
CHANNEL_CHARACTER_LIMITS = {"Facebook": 5_000, "Instagram": 2_200, "Threads": 500, "X": 280, "Telegram": 4_096, "Discord": 2_000, "LinkedIn": 3_000}
CHANNEL_MEDIA_LIMITS = {"Facebook": 10, "Instagram": 10, "Threads": 10, "X": 4, "Telegram": 10, "Discord": 10, "LinkedIn": 0}
LOGGER = logging.getLogger("sosopo")
RATE_LIMITS: dict[tuple[str, str], list[float]] = {}
RATE_LIMIT_LOCK = threading.Lock()


class ProviderError(Exception):
    """A safe-to-display provider delivery error with retry guidance."""

    def __init__(self, message: str, *, retryable: bool = True, retry_after: int | None = None) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.retry_after = retry_after


def now() -> str:
    return datetime.now(UTC).isoformat()


def detected_image_type(content: bytes) -> str | None:
    """Recognize only the four image encodings accepted by the upload API."""
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if content.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if content.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if len(content) >= 12 and content.startswith(b"RIFF") and content[8:12] == b"WEBP":
        return "image/webp"
    return None


def inspect_image(content: bytes, declared_type: str) -> tuple[int, int]:
    """Decode uploaded media before persisting it, avoiding disguised/corrupt images."""
    Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS
    try:
        with Image.open(io.BytesIO(content)) as image:
            image.verify()
        with Image.open(io.BytesIO(content)) as image:
            width, height = image.size
            actual = Image.MIME.get(image.format or "")
    except (Image.DecompressionBombError, UnidentifiedImageError, OSError, ValueError) as error:
        raise ValueError("Image content is corrupt, unsafe, or cannot be decoded.") from error
    if actual != declared_type or width < 1 or height < 1:
        raise ValueError("Image content does not match the declared media type.")
    return width, height


def token_is_expired(value: object) -> bool:
    if not value:
        return False
    try:
        expiry = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if expiry.tzinfo is None:
            return True
        return expiry <= datetime.now(UTC)
    except ValueError:
        return True


def validate_post(channel: str, body: str, image_url: str | None, image_count: int = 0) -> None:
    if channel not in CHANNELS:
        raise ValueError("Choose a supported provider.")
    if len(body) > CHANNEL_CHARACTER_LIMITS[channel]:
        raise ValueError(f"{channel} posts must be {CHANNEL_CHARACTER_LIMITS[channel]} characters or fewer.")
    if channel == "Instagram" and not image_url:
        raise ValueError("Instagram publishing requires an image.")
    if image_count > CHANNEL_MEDIA_LIMITS[channel]:
        raise ValueError(f"{channel} supports up to {CHANNEL_MEDIA_LIMITS[channel]} images per post.")


def allowed_request(client: str, scope: str, limit: int) -> bool:
    """Simple process-local abuse guard for endpoints that accept credentials or writes."""
    cutoff = time.monotonic() - 60
    with RATE_LIMIT_LOCK:
        requests = [item for item in RATE_LIMITS.get((client, scope), []) if item >= cutoff]
        if len(requests) >= limit:
            RATE_LIMITS[(client, scope)] = requests
            return False
        requests.append(time.monotonic())
        RATE_LIMITS[(client, scope)] = requests
        return True


def source_ip(peer: str, forwarded_for: str, trusted_proxy_cidrs: str) -> str:
    """Use X-Forwarded-For only when the direct peer is explicitly trusted."""
    try:
        peer_address = ipaddress.ip_address(peer)
        trusted = [ipaddress.ip_network(item.strip(), strict=False) for item in trusted_proxy_cidrs.split(",") if item.strip()]
        if not any(peer_address in network for network in trusted) or not forwarded_for:
            return peer
        candidate = forwarded_for.split(",")[0].strip()
        ipaddress.ip_address(candidate)
        return candidate
    except ValueError:
        return peer


class Record(dict[str, Any]):
    def __getitem__(self, key: str | int) -> Any:
        return list(self.values())[key] if isinstance(key, int) else super().__getitem__(key)


class Result:
    def __init__(self, cursor: Any) -> None:
        self.cursor = cursor
        self.rowcount = cursor.rowcount
        self.lastrowid = getattr(cursor, "lastrowid", None)
        self.names = [item[0] for item in cursor.description] if cursor.description else []

    def _record(self, row: Any) -> Record | None:
        if row is None:
            return None
        return Record(row) if isinstance(row, dict) else Record(zip(self.names, row))

    def fetchone(self) -> Record | None:
        return self._record(self.cursor.fetchone())

    def fetchall(self) -> list[Record]:
        return [self._record(row) for row in self.cursor.fetchall()]


class Database:
    def __init__(self) -> None:
        self.kind = "sqlite" if not DATABASE_URL or DATABASE_URL.startswith("sqlite:") else "postgres" if DATABASE_URL.startswith(("postgres://", "postgresql://")) else "mariadb" if DATABASE_URL.startswith(("mysql://", "mariadb://")) else ""
        if not self.kind:
            raise RuntimeError("DATABASE_URL must start with sqlite:, postgresql:, mysql:, or mariadb:.")
        if self.kind == "sqlite":
            path = DB_PATH if not DATABASE_URL else Path(unquote(urlparse(DATABASE_URL).path))
            self.raw = sqlite3.connect(path, timeout=10)
        elif self.kind == "postgres":
            import psycopg
            from psycopg.rows import dict_row
            self.raw = psycopg.connect(DATABASE_URL, row_factory=dict_row)
        else:
            import pymysql
            parsed = urlparse(DATABASE_URL)
            self.raw = pymysql.connect(host=parsed.hostname, port=parsed.port or 3306, user=unquote(parsed.username or ""), password=unquote(parsed.password or ""), database=parsed.path.lstrip("/"), charset="utf8mb4", cursorclass=pymysql.cursors.DictCursor)

    def __enter__(self) -> "Database":
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        (self.raw.rollback if exc_type else self.raw.commit)()
        self.raw.close()

    def execute(self, statement: str, params: tuple[Any, ...] = ()) -> Result:
        if self.kind != "sqlite":
            statement = statement.replace("?", "%s")
        cursor = self.raw.cursor()
        cursor.execute(statement, params)
        return Result(cursor)


def db() -> Database:
    return Database()


def insert_id(connection: Database, statement: str, params: tuple[Any, ...]) -> int:
    if connection.kind == "postgres":
        return int(connection.execute(f"{statement} RETURNING id", params).fetchone()["id"])
    result = connection.execute(statement, params)
    if result.lastrowid is None:
        raise RuntimeError("Database did not return an inserted ID.")
    return int(result.lastrowid)


def columns(connection: Database) -> set[str]:
    if connection.kind == "sqlite":
        return {row["name"] for row in connection.execute("PRAGMA table_info(posts)").fetchall()}
    if connection.kind == "postgres":
        return {row["name"] for row in connection.execute("SELECT column_name AS name FROM information_schema.columns WHERE table_schema = current_schema() AND table_name = 'posts'").fetchall()}
    return {row["name"] for row in connection.execute("SELECT column_name AS name FROM information_schema.columns WHERE table_schema = DATABASE() AND table_name = 'posts'").fetchall()}


def table_columns(connection: Database, table: str) -> set[str]:
    if connection.kind == "sqlite":
        return {row["name"] for row in connection.execute(f"PRAGMA table_info({table})").fetchall()}
    if connection.kind == "postgres":
        return {row["name"] for row in connection.execute("SELECT column_name AS name FROM information_schema.columns WHERE table_schema = current_schema() AND table_name = ?", (table,)).fetchall()}
    return {row["name"] for row in connection.execute("SELECT column_name AS name FROM information_schema.columns WHERE table_schema = DATABASE() AND table_name = ?", (table,)).fetchall()}


def add_column(connection: Database, name: str, definition: str) -> None:
    if name not in table_columns(connection, "posts"):
        connection.execute(f"ALTER TABLE posts ADD COLUMN {name} {definition}")


def add_table_column(connection: Database, table: str, name: str, definition: str) -> None:
    if name not in table_columns(connection, table):
        connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


def setup_database() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    if (not DATABASE_URL or DATABASE_URL.startswith("sqlite:")) and LEGACY_DB_PATH.is_file() and not DB_PATH.exists():
        LEGACY_DB_PATH.replace(DB_PATH)
    with db() as connection:
        id_column = "INTEGER PRIMARY KEY AUTOINCREMENT" if connection.kind == "sqlite" else "BIGSERIAL PRIMARY KEY" if connection.kind == "postgres" else "BIGINT AUTO_INCREMENT PRIMARY KEY"
        connection.execute(
            """CREATE TABLE IF NOT EXISTS posts (
                id %s,
                body TEXT NOT NULL,
                channel TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'draft',
                scheduled_for TEXT,
                image_url TEXT,
                created_at TEXT NOT NULL,
                published_at TEXT,
                external_id TEXT,
                attempts INTEGER NOT NULL DEFAULT 0,
                last_error TEXT
            )""" % id_column
        )
        connection.execute(
            """CREATE TABLE IF NOT EXISTS users (
                id %s,
                username TEXT NOT NULL UNIQUE,
                password_salt TEXT NOT NULL,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'user',
                is_active INTEGER NOT NULL DEFAULT 1,
                timezone TEXT NOT NULL DEFAULT 'UTC',
                signature TEXT NOT NULL DEFAULT '',
                oidc_issuer TEXT,
                oidc_subject TEXT,
                created_at TEXT NOT NULL
            )""" % id_column
        )
        connection.execute(
            """CREATE TABLE IF NOT EXISTS sessions (
                id %s,
                token_hash TEXT NOT NULL UNIQUE,
                csrf_token TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                expires_at TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(id)
            )""" % id_column
        )
        connection.execute(
            """CREATE TABLE IF NOT EXISTS connections (
                id %s,
                user_id INTEGER NOT NULL,
                provider TEXT NOT NULL,
                external_account_id TEXT NOT NULL,
                display_name TEXT NOT NULL,
                encrypted_secrets TEXT NOT NULL,
                settings_json TEXT NOT NULL DEFAULT '{}',
                token_expires_at TEXT,
                is_active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                UNIQUE(user_id, provider, external_account_id),
                FOREIGN KEY(user_id) REFERENCES users(id)
            )""" % id_column
        )
        connection.execute(
            """CREATE TABLE IF NOT EXISTS post_targets (
                post_id INTEGER NOT NULL,
                connection_id INTEGER NOT NULL,
                state TEXT NOT NULL DEFAULT 'pending',
                external_id TEXT,
                last_error TEXT,
                PRIMARY KEY(post_id, connection_id),
                FOREIGN KEY(post_id) REFERENCES posts(id),
                FOREIGN KEY(connection_id) REFERENCES connections(id)
            )"""
        )
        connection.execute(
            """CREATE TABLE IF NOT EXISTS post_media (
                id %s,
                post_id INTEGER NOT NULL,
                media_url TEXT NOT NULL,
                alt_text TEXT,
                position INTEGER NOT NULL,
                FOREIGN KEY(post_id) REFERENCES posts(id),
                UNIQUE(post_id, position)
            )""" % id_column
        )
        connection.execute(
            """CREATE TABLE IF NOT EXISTS oidc_states (
                state TEXT PRIMARY KEY,
                nonce TEXT NOT NULL,
                code_verifier TEXT NOT NULL,
                expires_at TEXT NOT NULL
            )"""
        )
        connection.execute(
            """CREATE TABLE IF NOT EXISTS social_oauth_states (
                state TEXT PRIMARY KEY,
                provider TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                code_verifier TEXT,
                expires_at TEXT NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(id)
            )"""
        )
        for name, definition in (
            ("image_url", "TEXT"), ("published_at", "TEXT"), ("external_id", "TEXT"),
            ("attempts", "INTEGER NOT NULL DEFAULT 0"), ("last_error", "TEXT"), ("user_id", "INTEGER"),
            ("scheduled_timezone", "TEXT"), ("publishing_started_at", "TEXT"),
        ):
            add_column(connection, name, definition)
        for name, definition in (("role", "TEXT NOT NULL DEFAULT 'user'"), ("is_active", "INTEGER NOT NULL DEFAULT 1"), ("timezone", "TEXT NOT NULL DEFAULT 'UTC'"), ("signature", "TEXT NOT NULL DEFAULT ''"), ("oidc_issuer", "TEXT"), ("oidc_subject", "TEXT")):
            add_table_column(connection, "users", name, definition)
        add_table_column(connection, "connections", "is_active", "INTEGER NOT NULL DEFAULT 1")
        connection.execute("CREATE UNIQUE INDEX IF NOT EXISTS users_oidc_identity ON users(oidc_issuer, oidc_subject)")
        connection.execute(
            """CREATE TABLE IF NOT EXISTS deliveries (
                id %s,
                post_id INTEGER NOT NULL,
                provider TEXT NOT NULL,
                status TEXT NOT NULL,
                detail TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY(post_id) REFERENCES posts(id)
        )""" % id_column
        )
        connection.execute(
            """CREATE TABLE IF NOT EXISTS audit_events (
                id %s,
                user_id INTEGER,
                action TEXT NOT NULL,
                subject_type TEXT NOT NULL,
                subject_id TEXT,
                detail TEXT,
                source_ip TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(id)
        )""" % id_column
        )
        connection.execute(
            """CREATE TABLE IF NOT EXISTS worker_heartbeats (
                name TEXT PRIMARY KEY,
                checked_at TEXT NOT NULL
        )"""
        )
        connection.execute(
            """CREATE TABLE IF NOT EXISTS instance_settings (
                name TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )"""
        )
        connection.execute("CREATE INDEX IF NOT EXISTS posts_due_delivery ON posts(state, scheduled_for)")
        connection.execute("CREATE INDEX IF NOT EXISTS post_media_post ON post_media(post_id, position)")
        connection.execute("CREATE INDEX IF NOT EXISTS sessions_expiry ON sessions(expires_at)")
        connection.execute("CREATE INDEX IF NOT EXISTS social_oauth_states_expiry ON social_oauth_states(expires_at)")
        connection.execute("CREATE INDEX IF NOT EXISTS deliveries_post ON deliveries(post_id, created_at)")
        if connection.execute("SELECT COUNT(*) FROM posts").fetchone()[0] == 0:
            connection.execute(
                "INSERT INTO posts (body, channel, state, created_at) VALUES (?, ?, 'draft', ?)",
                ("Welcome to Sosopo. Configure a provider when you are ready to publish.", "Facebook", now()),
            )


def config(name: str) -> str:
    return environment_value(name)


AI_PROVIDERS = {
    "OpenAI": ("openai", "SOSOPO_AI_OPENAI", "https://api.openai.com/v1"),
    "OpenRouter": ("openrouter", "SOSOPO_AI_OPENROUTER", "https://openrouter.ai/api/v1"),
    "Kimi": ("kimi", "SOSOPO_AI_KIMI", "https://api.moonshot.ai/v1"),
    "MiniMax": ("minimax", "SOSOPO_AI_MINIMAX", "https://api.minimax.io/v1"),
    "Z.AI GLM": ("zai", "SOSOPO_AI_ZAI", "https://api.z.ai/api/paas/v4"),
}

# Provider-owned defaults keep endpoint details out of the administrator UI.
# A refreshed provider catalog supersedes these choices when available.
AI_PROVIDER_MODELS = {
    "OpenAI": ["gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna", "gpt-5.5", "gpt-5.2"],
    "OpenRouter": ["openai/gpt-5.6-sol", "openai/gpt-5.5", "anthropic/claude-sonnet-4.6"],
    "Kimi": ["kimi-k3", "kimi-k2.7-code", "kimi-k2.7-code-highspeed", "kimi-k2.6"],
    "MiniMax": ["MiniMax-M2.7", "MiniMax-M2.7-highspeed", "MiniMax-M2.5", "MiniMax-M2.5-highspeed", "M2-her"],
    "Z.AI GLM": ["glm-5.2", "glm-5.1", "glm-5"],
}


def stored_ai_provider_settings(provider: str) -> dict:
    definition = AI_PROVIDERS.get(provider)
    if definition is None:
        raise ProviderError("Choose a supported AI provider.", retryable=False)
    with db() as connection:
        row = connection.execute("SELECT value FROM instance_settings WHERE name = ?", (f"ai_provider_{definition[0]}",)).fetchone()
    return decrypt_secrets(row["value"]) if row else {}


def save_ai_provider_settings(provider: str, settings: dict) -> None:
    """Save a provider configuration and its locally cached, reviewed model catalog."""
    definition = AI_PROVIDERS[provider]
    setting_name = f"ai_provider_{definition[0]}"
    with db() as connection:
        exists = connection.execute("SELECT 1 FROM instance_settings WHERE name = ?", (setting_name,)).fetchone()
        if exists:
            connection.execute("UPDATE instance_settings SET value = ? WHERE name = ?", (encrypt_secrets(settings), setting_name))
        else:
            connection.execute("INSERT INTO instance_settings (name, value) VALUES (?, ?)", (setting_name, encrypt_secrets(settings)))


def remove_ai_provider_settings(provider: str) -> bool:
    """Remove the UI-saved credential and local catalog for one provider."""
    definition = AI_PROVIDERS.get(provider)
    if definition is None:
        raise ProviderError("Choose a supported AI provider.", retryable=False)
    with db() as connection:
        return connection.execute("DELETE FROM instance_settings WHERE name = ?", (f"ai_provider_{definition[0]}",)).rowcount == 1


def ai_model_catalog(stored: dict) -> list[str]:
    raw = stored.get("models", "[]")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return []
    return [model for model in raw if isinstance(model, str) and model] if isinstance(raw, list) else []


def ai_provider_settings(provider: str) -> dict[str, str]:
    """Return only a configured, OpenAI-compatible text-generation provider."""
    definition = AI_PROVIDERS.get(provider)
    if definition is None:
        raise ProviderError("Choose a supported AI provider.", retryable=False)
    _, prefix, default_base = definition
    stored = stored_ai_provider_settings(provider)
    api_key = stored.get("api_key") or config(f"{prefix}_API_KEY")
    base_url = stored.get("base_url") or config(f"{prefix}_BASE_URL") or default_base
    model = stored.get("model") or config(f"{prefix}_MODEL") or AI_PROVIDER_MODELS[provider][0]
    if not api_key or not base_url.startswith("https://") or not model:
        raise ProviderError(f"{provider} AI is not configured by this Sosopo administrator.", retryable=False)
    return {"name": provider, "api_key": api_key, "base_url": base_url.rstrip("/"), "model": model}


def available_ai_providers() -> list[dict]:
    providers: list[dict] = []
    for name in AI_PROVIDERS:
        try:
            settings = ai_provider_settings(name)
            stored = stored_ai_provider_settings(name)
            models = ai_model_catalog(stored) or AI_PROVIDER_MODELS[name]
            providers.append({"name": settings["name"], "model": settings["model"], "models": models})
        except ProviderError:
            pass
    return providers


def ai_provider_models(provider: str) -> list[str]:
    """Refresh the provider's model catalog using its configured discovery endpoint."""
    stored = stored_ai_provider_settings(provider)
    # OpenRouter publishes its complete catalog without authentication, so make
    # it available in the selector before an administrator has saved a key.
    if provider == "OpenRouter":
        definition = AI_PROVIDERS[provider]
        settings = {"name": provider, "api_key": stored.get("api_key") or config(f"{definition[1]}_API_KEY"), "base_url": definition[2]}
    else:
        settings = ai_provider_settings(provider)
    # MiniMax documents this exact endpoint for Token Plan keys.  In
    # particular, do not append cache-busting query parameters: some MiniMax
    # API gateways reject an otherwise valid signed/bearer request when the
    # request URI differs from the documented endpoint.
    model_list_url = f"{settings['base_url']}/models"
    if provider != "MiniMax":
        # A unique query string bypasses intermediary caches for providers
        # which accept it. Sosopo itself never caches this response.
        model_list_url = f"{model_list_url}?refresh={int(time.time())}"
    headers = {"Authorization": f"Bearer {settings['api_key']}"} if settings.get("api_key") else None
    result = request_get_json(model_list_url, headers)
    entries = result.get("data") or result.get("models") or []
    if not isinstance(entries, list):
        raise ProviderError("The AI provider returned an invalid model list.")
    models = []
    for entry in entries:
        identifier = (entry.get("id") or entry.get("model") or entry.get("name")) if isinstance(entry, dict) else entry if isinstance(entry, str) else None
        if isinstance(identifier, str) and identifier and len(identifier) <= 200:
            models.append(identifier)
    if not models:
        raise ProviderError("The AI provider did not return any selectable models.")
    models = sorted(set(models), key=str.casefold)[:1_000]
    # Keep a known-good catalog locally. The composer uses this catalog rather than
    # contacting the provider on every page load, and rejects unknown model IDs.
    stored.update({"models": json.dumps(models), "models_checked_at": now()})
    save_ai_provider_settings(provider, stored)
    return models


def generate_post_copy(provider: str, model: str, instruction: str, draft: str, channels: list[str]) -> str:
    settings = ai_provider_settings(provider)
    selected_model = model.strip() or settings["model"]
    stored = stored_ai_provider_settings(provider)
    catalog = ai_model_catalog(stored)
    if catalog and selected_model not in catalog:
        raise ProviderError("Choose a model from the provider's refreshed model catalog.", retryable=False)
    if len(selected_model) > 200 or len(instruction) > 2_000 or len(draft) > 5_000:
        raise ProviderError("AI request is too long.", retryable=False)
    prompt = f"Write one ready-to-publish social media post. Platforms: {', '.join(channels) or 'general social media'}. Brief: {instruction.strip() or 'Improve the draft below.'}\nDraft to improve (may be empty):\n{draft.strip()}"
    messages = [{"role": "system", "content": "You are Sosopo's concise social-media copywriter. Return only the finished post copy; do not add a title, explanation, markdown fence, or quotation marks."}, {"role": "user", "content": prompt}]
    endpoint = f"{settings['base_url']}/text/chatcompletion_v2" if provider == "MiniMax" else f"{settings['base_url']}/chat/completions"
    result = request_json(endpoint, {"model": selected_model, "messages": messages, "temperature": 0.7, "max_tokens": 700}, {"Authorization": f"Bearer {settings['api_key']}"})
    choices = result.get("choices", [])
    content = choices[0].get("message", {}).get("content", "") if isinstance(choices, list) and choices and isinstance(choices[0], dict) else ""
    if not isinstance(content, str) or not content.strip():
        raise ProviderError("The AI provider did not return post copy.")
    return content.strip()


def audit(user_id: int | None, action: str, subject_type: str, subject_id: object | None, detail: str, source_ip: str) -> None:
    with db() as connection:
        connection.execute(
            "INSERT INTO audit_events (user_id, action, subject_type, subject_id, detail, source_ip, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (user_id, action, subject_type, str(subject_id) if subject_id is not None else None, detail[:500], source_ip[:100], now()),
        )


def worker_heartbeat() -> None:
    with db() as connection:
        connection.execute("DELETE FROM worker_heartbeats WHERE name = 'delivery'")
        connection.execute("INSERT INTO worker_heartbeats (name, checked_at) VALUES ('delivery', ?)", (now(),))


def worker_healthy() -> bool:
    try:
        with db() as connection:
            row = connection.execute("SELECT checked_at FROM worker_heartbeats WHERE name = 'delivery'").fetchone()
        return row is not None and datetime.fromisoformat(row["checked_at"]) >= datetime.now(UTC) - timedelta(seconds=WORKER_HEARTBEAT_SECONDS)
    except Exception:
        return False


def recover_stale_deliveries() -> int:
    stale_before = (datetime.now(UTC) - timedelta(seconds=PUBLISHING_LEASE_SECONDS)).isoformat()
    with db() as connection:
        result = connection.execute("UPDATE posts SET state = 'scheduled', scheduled_for = ?, publishing_started_at = NULL, last_error = 'Delivery worker lease expired; retrying.' WHERE state = 'publishing' AND publishing_started_at < ?", (now(), stale_before))
    return result.rowcount


def cleanup_expired_records() -> None:
    try:
        retention_days = max(1, int(config("SOSOPO_AUDIT_RETENTION_DAYS") or DEFAULT_AUDIT_RETENTION_DAYS))
    except ValueError:
        retention_days = DEFAULT_AUDIT_RETENTION_DAYS
    with db() as connection:
        connection.execute("DELETE FROM sessions WHERE expires_at <= ?", (now(),))
        connection.execute("DELETE FROM oidc_states WHERE expires_at <= ?", (now(),))
        connection.execute("DELETE FROM social_oauth_states WHERE expires_at <= ?", (now(),))
        connection.execute("DELETE FROM audit_events WHERE created_at < ?", ((datetime.now(UTC) - timedelta(days=retention_days)).isoformat(),))


def public_url() -> str:
    return (config("SOSOPO_PUBLIC_URL") or config("SOSOPO_PUBLIC_BASE_URL")).rstrip("/")


def timezone_name(value: object) -> str:
    name = str(value or "UTC").strip()
    try:
        ZoneInfo(name)
    except ZoneInfoNotFoundError as error:
        raise ValueError("Use an IANA timezone such as Asia/Hong_Kong or Europe/London.") from error
    return name


def encryption() -> Fernet:
    key = config("SOSOPO_ENCRYPTION_KEY")
    if not key:
        raise ProviderError("Set SOSOPO_ENCRYPTION_KEY before saving account credentials.")
    try:
        return Fernet(key.encode())
    except (ValueError, TypeError) as error:
        raise ProviderError("SOSOPO_ENCRYPTION_KEY must be a valid Fernet key.") from error


def encrypt_secrets(value: dict[str, str]) -> str:
    return encryption().encrypt(json.dumps(value).encode()).decode()


def decrypt_secrets(value: str) -> dict[str, str]:
    try:
        decoded = encryption().decrypt(value.encode())
        result = json.loads(decoded)
    except (InvalidToken, ValueError, TypeError, json.JSONDecodeError) as error:
        raise ProviderError("Could not decrypt the saved provider credentials.") from error
    if not isinstance(result, dict):
        raise ProviderError("Saved provider credentials are invalid.")
    return {str(key): str(item) for key, item in result.items()}


def hash_password(password: str, salt: bytes) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 600_000).hex()


def expires_at() -> str:
    return datetime.fromtimestamp(datetime.now(UTC).timestamp() + SESSION_SECONDS, UTC).isoformat()


def oidc_settings() -> dict[str, str]:
    issuer, client_id = config("OIDC_ISSUER_URL").rstrip("/"), config("OIDC_CLIENT_ID")
    if not issuer or not client_id:
        raise ProviderError("SSO is not configured. Set OIDC_ISSUER_URL and OIDC_CLIENT_ID.")
    try:
        with urlopen(f"{issuer}/.well-known/openid-configuration", timeout=15) as response:
            discovery = json.loads(response.read())
    except (URLError, HTTPError, json.JSONDecodeError) as error:
        raise ProviderError("Could not load the OIDC provider configuration.") from error
    if discovery.get("issuer") != issuer or not discovery.get("jwks_uri"):
        raise ProviderError("OIDC discovery issuer or JWKS endpoint does not match the configured issuer.")
    return {**discovery, "issuer": issuer, "client_id": client_id, "client_secret": config("OIDC_CLIENT_SECRET")}


def oidc_redirect_uri() -> str:
    base = public_url()
    if not base.startswith("https://"):
        raise ProviderError("SSO requires SOSOPO_PUBLIC_URL with a public HTTPS URL.")
    return f"{base}/api/auth/oidc/callback"


def social_oauth_redirect_uri() -> str:
    base = public_url()
    if not base.startswith("https://"):
        raise ProviderError("Social account connection requires SOSOPO_PUBLIC_URL with a public HTTPS URL.")
    return f"{base}/api/social-oauth/callback"


def social_oauth_settings(provider: str) -> dict[str, str]:
    # Instagram professional accounts are authorized and discovered through the
    # same Meta Page grant as Facebook; the dashboard exposes both entry points.
    if provider == "Instagram":
        provider = "Facebook"
    settings = {
        "Facebook": {"client_id": config("FACEBOOK_OAUTH_CLIENT_ID"), "client_secret": config("FACEBOOK_OAUTH_CLIENT_SECRET"), "authorize": config("FACEBOOK_OAUTH_AUTHORIZE_URL") or "https://www.facebook.com/v24.0/dialog/oauth", "token": config("FACEBOOK_OAUTH_TOKEN_URL") or "https://graph.facebook.com/v24.0/oauth/access_token", "scopes": "pages_show_list,pages_read_engagement,pages_manage_posts,instagram_basic,instagram_content_publish"},
        "Threads": {"client_id": config("THREADS_OAUTH_CLIENT_ID"), "client_secret": config("THREADS_OAUTH_CLIENT_SECRET"), "authorize": config("THREADS_OAUTH_AUTHORIZE_URL") or "https://threads.net/oauth/authorize", "token": config("THREADS_OAUTH_TOKEN_URL") or "https://graph.threads.net/oauth/access_token", "scopes": "threads_basic,threads_content_publish"},
        "X": {"client_id": config("X_OAUTH_CLIENT_ID"), "client_secret": config("X_OAUTH_CLIENT_SECRET"), "authorize": config("X_OAUTH_AUTHORIZE_URL") or "https://x.com/i/oauth2/authorize", "token": config("X_OAUTH_TOKEN_URL") or "https://api.x.com/2/oauth2/token", "scopes": "tweet.read,tweet.write,users.read,offline.access"},
        "LinkedIn": {"client_id": config("LINKEDIN_OAUTH_CLIENT_ID"), "client_secret": config("LINKEDIN_OAUTH_CLIENT_SECRET"), "authorize": config("LINKEDIN_OAUTH_AUTHORIZE_URL") or "https://www.linkedin.com/oauth/v2/authorization", "token": config("LINKEDIN_OAUTH_TOKEN_URL") or "https://www.linkedin.com/oauth/v2/accessToken", "scopes": "openid profile w_member_social"},
        "Discord": {"client_id": config("DISCORD_OAUTH_CLIENT_ID"), "client_secret": config("DISCORD_OAUTH_CLIENT_SECRET"), "authorize": config("DISCORD_OAUTH_AUTHORIZE_URL") or "https://discord.com/oauth2/authorize", "token": config("DISCORD_OAUTH_TOKEN_URL") or "https://discord.com/api/oauth2/token", "scopes": "webhook.incoming"},
    }.get(provider)
    if not settings or not settings["client_id"] or not settings["client_secret"]:
        raise ProviderError(f"{provider} OAuth is not configured by this Sosopo administrator.")
    return settings


def social_oauth_enabled(provider: str) -> bool:
    try:
        social_oauth_settings(provider)
        return True
    except ProviderError:
        return False


def social_token_expiry(token: dict[str, Any]) -> str | None:
    try:
        seconds = int(token.get("expires_in", 0))
        return (datetime.now(UTC) + timedelta(seconds=seconds)).isoformat() if seconds > 0 else None
    except (TypeError, ValueError):
        return None


def social_oauth_connections(provider: str, settings: dict[str, str], code: str, verifier: str | None) -> list[dict[str, str]]:
    redirect_uri = social_oauth_redirect_uri()
    payload = {"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri, "client_id": settings["client_id"], "client_secret": settings["client_secret"]}
    if provider == "X":
        payload["code_verifier"] = verifier or ""
    token = request_form(settings["token"], payload)
    access_token = str(token.get("access_token") or "")
    if not access_token:
        raise ProviderError("The provider did not return an access token.")
    expiry = social_token_expiry(token)
    if provider == "Facebook":
        base = config("META_GRAPH_BASE_URL") or "https://graph.facebook.com/v24.0"
        pages = request_get_json(f"{base}/me/accounts?{urlencode({'fields': 'id,name,access_token,instagram_business_account{id,username}', 'access_token': access_token})}").get("data", [])
        records: list[dict[str, str]] = []
        for page in pages if isinstance(pages, list) else []:
            if not isinstance(page, dict) or not page.get("id") or not page.get("access_token"):
                continue
            records.append({"provider": "Facebook", "external_account_id": str(page["id"]), "display_name": str(page.get("name") or page["id"]), "access_token": str(page["access_token"]), "token_expires_at": expiry or ""})
            instagram = page.get("instagram_business_account")
            if isinstance(instagram, dict) and instagram.get("id"):
                records.append({"provider": "Instagram", "external_account_id": str(instagram["id"]), "display_name": str(instagram.get("username") or page.get("name") or instagram["id"]), "access_token": str(page["access_token"]), "token_expires_at": expiry or ""})
        if not records:
            raise ProviderError("No managed Facebook Pages were returned. Confirm that the account manages a Page and approved page permissions.")
        return records
    if provider == "Threads":
        base = config("THREADS_API_BASE_URL") or "https://graph.threads.net/v1.0"
        profile = request_get_json(f"{base}/me?{urlencode({'fields': 'id,username', 'access_token': access_token})}")
        if not profile.get("id"):
            raise ProviderError("Threads did not return a profile.")
        return [{"provider": "Threads", "external_account_id": str(profile["id"]), "display_name": str(profile.get("username") or profile["id"]), "access_token": access_token, "token_expires_at": expiry or ""}]
    if provider == "LinkedIn":
        profile = request_get_json("https://api.linkedin.com/v2/userinfo", {"Authorization": f"Bearer {access_token}"})
        subject = str(profile.get("sub") or "")
        if not subject:
            raise ProviderError("LinkedIn did not return a member profile.")
        author = subject if subject.startswith("urn:li:") else f"urn:li:person:{subject}"
        return [{"provider": "LinkedIn", "external_account_id": author, "display_name": str(profile.get("name") or profile.get("given_name") or subject), "access_token": access_token, "token_expires_at": expiry or ""}]
    if provider == "Discord":
        webhook = token.get("webhook")
        if not isinstance(webhook, dict) or not webhook.get("id") or not webhook.get("token"):
            raise ProviderError("Discord did not return an approved channel webhook.")
        webhook_id, webhook_token = str(webhook["id"]), str(webhook["token"])
        return [{"provider": "Discord", "external_account_id": webhook_id, "display_name": str(webhook.get("name") or f"Discord channel {webhook.get('channel_id') or webhook_id}"), "access_token": f"https://discord.com/api/webhooks/{webhook_id}/{webhook_token}", "secret_name": "webhook_url", "token_expires_at": ""}]
    profile = request_get_json("https://api.x.com/2/users/me", {"Authorization": f"Bearer {access_token}"}).get("data", {})
    if not isinstance(profile, dict) or not profile.get("id"):
        raise ProviderError("X did not return a user profile.")
    return [{"provider": "X", "external_account_id": str(profile["id"]), "display_name": str(profile.get("username") or profile.get("name") or profile["id"]), "access_token": access_token, "token_expires_at": expiry or ""}]


def save_social_connections(user_id: int, records: list[dict[str, str]]) -> int:
    saved = 0
    with db() as connection:
        for record in records:
            existing = connection.execute("SELECT id FROM connections WHERE user_id = ? AND provider = ? AND external_account_id = ?", (user_id, record["provider"], record["external_account_id"])).fetchone()
            encrypted = encrypt_secrets({str(record.get("secret_name") or "access_token"): record["access_token"]})
            expiry = record["token_expires_at"] or None
            if existing:
                connection.execute("UPDATE connections SET display_name = ?, encrypted_secrets = ?, token_expires_at = ?, is_active = 1 WHERE id = ?", (record["display_name"], encrypted, expiry, existing["id"]))
            else:
                connection.execute("INSERT INTO connections (user_id, provider, external_account_id, display_name, encrypted_secrets, settings_json, token_expires_at, created_at) VALUES (?, ?, ?, ?, ?, '{}', ?, ?)", (user_id, record["provider"], record["external_account_id"], record["display_name"], encrypted, expiry, now()))
            saved += 1
    return saved


def verify_oidc_id_token(token: object, settings: dict[str, str], nonce: str) -> dict[str, Any]:
    if not isinstance(token, str):
        raise ProviderError("OIDC provider did not return an ID token.")
    try:
        header = jwt.get_unverified_header(token)
        algorithm = str(header.get("alg", ""))
        allowed = settings.get("id_token_signing_alg_values_supported", [])
        if algorithm in {"none", "HS256", "HS384", "HS512"} or algorithm not in allowed:
            raise ProviderError("OIDC provider returned an unsupported ID-token algorithm.")
        key = PyJWKClient(settings["jwks_uri"]).get_signing_key_from_jwt(token).key
        claims = jwt.decode(token, key, algorithms=[algorithm], audience=settings["client_id"], issuer=settings["issuer"], options={"require": ["exp", "iat", "iss", "aud", "sub"]})
    except (jwt.PyJWTError, KeyError) as error:
        raise ProviderError("OIDC ID-token validation failed.") from error
    if not secrets.compare_digest(str(claims.get("nonce", "")), nonce):
        raise ProviderError("OIDC ID-token nonce validation failed.")
    return claims


def public_image_url(image_url: str) -> str:
    if image_url.startswith(("https://", "http://")):
        return image_url
    base_url = public_url()
    if not base_url.startswith("https://"):
        raise ProviderError("Image publishing needs SOSOPO_PUBLIC_URL set to a public HTTPS URL.")
    return f"{base_url}{image_url}"


def storage_backend() -> str:
    backend = config("SOSOPO_STORAGE_BACKEND") or "local"
    if backend not in {"local", "s3"}:
        raise ProviderError("SOSOPO_STORAGE_BACKEND must be local or s3.")
    return backend


def media_key(filename: str) -> str:
    return f"{config('S3_MEDIA_PREFIX').strip('/') or 'uploads'}/{filename}"


def media_client() -> Any:
    import boto3
    return boto3.client("s3", endpoint_url=config("S3_ENDPOINT_URL") or None, aws_access_key_id=config("AWS_ACCESS_KEY_ID") or None, aws_secret_access_key=config("AWS_SECRET_ACCESS_KEY") or None)


def media_url(filename: str) -> str:
    if storage_backend() == "local":
        return f"/uploads/{filename}"
    base = config("SOSOPO_MEDIA_PUBLIC_URL").rstrip("/")
    if not base.startswith("https://"):
        raise ProviderError("S3 media storage requires SOSOPO_MEDIA_PUBLIC_URL with a public HTTPS URL.")
    return f"{base}/{media_key(filename)}"


def store_media(filename: str, content_type: str, content: bytes) -> str:
    if storage_backend() == "local":
        (UPLOADS_DIR / filename).write_bytes(content)
    else:
        bucket = config("S3_MEDIA_BUCKET")
        if not bucket:
            raise ProviderError("S3 media storage requires S3_MEDIA_BUCKET.")
        media_client().put_object(Bucket=bucket, Key=media_key(filename), Body=content, ContentType=content_type)
    return media_url(filename)


def media_exists(image_url: str) -> bool:
    if storage_backend() == "local":
        return image_url.startswith("/uploads/") and (UPLOADS_DIR / Path(image_url).name).is_file()
    return image_url.startswith(config("SOSOPO_MEDIA_PUBLIC_URL").rstrip("/") + "/")


def media_bytes(image_url: str) -> bytes:
    if storage_backend() == "local":
        return (UPLOADS_DIR / Path(image_url).name).read_bytes()
    bucket = config("S3_MEDIA_BUCKET")
    if not bucket:
        raise ProviderError("S3 media storage requires S3_MEDIA_BUCKET.")
    return media_client().get_object(Bucket=bucket, Key=media_key(Path(urlparse(image_url).path).name))["Body"].read()


def post_media_urls(post: dict[str, Any]) -> list[str]:
    """Return ordered attachments while retaining compatibility with old single-image posts."""
    urls = post.get("media_urls")
    if isinstance(urls, list):
        return [str(url) for url in urls]
    if "id" not in post:
        return [post["image_url"]] if post.get("image_url") else []
    with db() as connection:
        rows = connection.execute("SELECT media_url FROM post_media WHERE post_id = ? ORDER BY position", (post["id"],)).fetchall()
    return [row["media_url"] for row in rows] or ([post["image_url"]] if post.get("image_url") else [])


def request_json(url: str, payload: dict[str, Any], headers: dict[str, str] | None = None) -> dict[str, Any]:
    data = json.dumps(payload).encode()
    request = Request(url, data=data, method="POST", headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urlopen(request, timeout=30) as response:
            return json.loads(response.read() or b"{}")
    except HTTPError as error:
        body = error.read().decode(errors="replace")[:500]
        retry_after = parse_retry_after(error.headers.get("Retry-After", ""))
        retryable = error.code == HTTPStatus.TOO_MANY_REQUESTS or error.code >= HTTPStatus.INTERNAL_SERVER_ERROR
        raise ProviderError(f"Provider rejected the post ({error.code}): {body}", retryable=retryable, retry_after=retry_after) from error
    except URLError as error:
        raise ProviderError(f"Provider could not be reached: {error.reason}", retryable=True) from error


def request_form(url: str, payload: dict[str, str], headers: dict[str, str] | None = None) -> dict[str, Any]:
    request = Request(url, data=urlencode(payload).encode(), method="POST", headers={"Content-Type": "application/x-www-form-urlencoded", **(headers or {})})
    try:
        with urlopen(request, timeout=30) as response:
            return json.loads(response.read() or b"{}")
    except HTTPError as error:
        retry_after = parse_retry_after(error.headers.get("Retry-After", ""))
        retryable = error.code == HTTPStatus.TOO_MANY_REQUESTS or error.code >= HTTPStatus.INTERNAL_SERVER_ERROR
        raise ProviderError(f"Provider rejected the post ({error.code}): {error.read().decode(errors='replace')[:500]}", retryable=retryable, retry_after=retry_after) from error
    except URLError as error:
        raise ProviderError(f"Provider could not be reached: {error.reason}", retryable=True) from error


def request_get_json(url: str, headers: dict[str, str] | None = None) -> dict[str, Any]:
    request = Request(url, headers=headers or {})
    try:
        with urlopen(request, timeout=30) as response:
            result = json.loads(response.read() or b"{}")
    except HTTPError as error:
        raise ProviderError(f"Provider rejected account discovery ({error.code}): {error.read().decode(errors='replace')[:500]}", retryable=error.code == HTTPStatus.TOO_MANY_REQUESTS or error.code >= HTTPStatus.INTERNAL_SERVER_ERROR) from error
    except (URLError, json.JSONDecodeError) as error:
        raise ProviderError("Provider account discovery could not be completed.", retryable=True) from error
    if not isinstance(result, dict):
        raise ProviderError("Provider account discovery returned an invalid response.")
    return result


def request_delete(url: str, headers: dict[str, str] | None = None) -> dict[str, Any]:
    """Delete a remote resource and normalize providers that return no body."""
    request = Request(url, method="DELETE", headers=headers or {})
    try:
        with urlopen(request, timeout=30) as response:
            body = response.read() or b"{}"
            result = json.loads(body)
    except HTTPError as error:
        body = error.read().decode(errors="replace")[:500]
        retry_after = parse_retry_after(error.headers.get("Retry-After", ""))
        retryable = error.code == HTTPStatus.TOO_MANY_REQUESTS or error.code >= HTTPStatus.INTERNAL_SERVER_ERROR
        raise ProviderError(f"Provider rejected deletion ({error.code}): {body}", retryable=retryable, retry_after=retry_after) from error
    except (URLError, json.JSONDecodeError) as error:
        raise ProviderError(f"Provider deletion could not be completed: {error}", retryable=True) from error
    return result if isinstance(result, dict) else {}


def parse_retry_after(value: str) -> int | None:
    """Accept only bounded Retry-After delay seconds from a provider response."""
    try:
        return min(max(int(value), 1), RETRY_MAX_SECONDS)
    except (TypeError, ValueError):
        return None


def telegram_request(token: str, method: str, fields: dict[str, str], image: Path | None = None) -> dict[str, Any]:
    url = f"https://api.telegram.org/bot{token}/{method}"
    if image is None:
        response = request_form(url, fields)
    else:
        boundary = f"----sosopo{uuid.uuid4().hex}"
        chunks: list[bytes] = []
        for key, value in fields.items():
            chunks.extend((f"--{boundary}\r\n".encode(), f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode(), value.encode(), b"\r\n"))
        content_type = mimetypes.guess_type(image.name)[0] or "application/octet-stream"
        chunks.extend((f"--{boundary}\r\n".encode(), f'Content-Disposition: form-data; name="photo"; filename="{image.name}"\r\n'.encode(), f"Content-Type: {content_type}\r\n\r\n".encode(), image.read_bytes(), b"\r\n", f"--{boundary}--\r\n".encode()))
        request = Request(url, data=b"".join(chunks), method="POST", headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
        try:
            with urlopen(request, timeout=30) as result:
                response = json.loads(result.read() or b"{}")
        except (HTTPError, URLError) as error:
            raise ProviderError(f"Telegram could not deliver the post: {error}") from error
    if not response.get("ok"):
        raise ProviderError(f"Telegram rejected the post: {response.get('description', 'unknown error')}")
    return response


def publish(post: dict[str, Any], account: dict[str, Any] | None = None) -> str:
    channel = str(account.get("provider") or post["channel"]) if account else post["channel"]
    body, image_urls = post["body"], post_media_urls(post)
    image_url = image_urls[0] if image_urls else None
    if account and token_is_expired(account.get("token_expires_at")):
        raise ProviderError("This provider account token has expired. Reconnect or rotate it before publishing.")
    secrets_for_account = decrypt_secrets(account["encrypted_secrets"]) if account else {}
    account_id = str(account["external_account_id"]) if account else ""
    def credential(name: str, environment: str) -> str:
        return secrets_for_account.get(name, "") or config(environment)
    if channel == "Discord":
        webhook_url = credential("webhook_url", "DISCORD_WEBHOOK_URL")
        if not webhook_url.startswith("https://discord.com/api/webhooks/") and not webhook_url.startswith("https://discordapp.com/api/webhooks/"):
            raise ProviderError("Discord needs a valid incoming webhook URL.", retryable=False)
        embeds = [{"image": {"url": public_image_url(url)}} for url in image_urls]
        result = request_json(f"{webhook_url}?wait=true", {"content": body, "embeds": embeds, "allowed_mentions": {"parse": []}})
        return str(result.get("id") or "")
    if channel == "LinkedIn":
        author, token = account_id or credential("author_urn", "LINKEDIN_AUTHOR_URN"), credential("access_token", "LINKEDIN_ACCESS_TOKEN")
        version = config("LINKEDIN_API_VERSION")
        if not author or not token or not version:
            raise ProviderError("LinkedIn needs LINKEDIN_AUTHOR_URN, LINKEDIN_ACCESS_TOKEN, and LINKEDIN_API_VERSION.")
        if not author.startswith("urn:li:"):
            raise ProviderError("LinkedIn author must be a member or organization URN.", retryable=False)
        if image_urls:
            raise ProviderError("LinkedIn image publishing is not available yet; publish text only.", retryable=False)
        result = request_json("https://api.linkedin.com/rest/posts", {
            "author": author,
            "commentary": body,
            "visibility": "PUBLIC",
            "distribution": {"feedDistribution": "MAIN_FEED", "targetEntities": [], "thirdPartyDistributionChannels": []},
            "lifecycleState": "PUBLISHED",
            "isReshareDisabledByAuthor": False,
        }, {"Authorization": f"Bearer {token}", "LinkedIn-Version": version, "X-Restli-Protocol-Version": "2.0.0"})
        return str(result.get("id") or "linkedin-posted")
    if channel == "Telegram":
        token, chat_id = credential("bot_token", "TELEGRAM_BOT_TOKEN"), account_id or credential("chat_id", "TELEGRAM_CHAT_ID")
        if not token or not chat_id:
            raise ProviderError("Telegram needs TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID.")
        if not image_urls:
            result = telegram_request(token, "sendMessage", {"chat_id": chat_id, "text": body})
            return str(result["result"]["message_id"])
        message_ids: list[str] = []
        for index, url in enumerate(image_urls):
            fields = {"chat_id": chat_id, "caption": body if index == 0 else ""}
            image = UPLOADS_DIR / Path(url).name if storage_backend() == "local" else None
            if storage_backend() == "s3":
                fields["photo"] = public_image_url(url)
            result = telegram_request(token, "sendPhoto", fields, image)
            message_ids.append(str(result["result"]["message_id"]))
        return ",".join(message_ids)
    if channel == "X":
        token = credential("access_token", "X_ACCESS_TOKEN")
        if not token:
            raise ProviderError("X needs X_ACCESS_TOKEN with post.write permission.")
        media_ids: list[str] = []
        for url in image_urls:
            image_name = Path(urlparse(url).path).name
            result = request_json("https://api.x.com/2/media/upload", {"media": base64.b64encode(media_bytes(url)).decode(), "media_category": "tweet_image", "media_type": mimetypes.guess_type(image_name)[0] or "image/png"}, {"Authorization": f"Bearer {token}"})
            media_ids.append(str(result.get("data", {}).get("id") or result.get("data", {}).get("media_id") or ""))
            if not media_ids[-1]:
                raise ProviderError("X did not return a media ID.")
        result = request_json("https://api.x.com/2/tweets", {"text": body, **({"media": {"media_ids": media_ids}} if media_ids else {})}, {"Authorization": f"Bearer {token}"})
        return str(result.get("data", {}).get("id") or "")
    if channel == "Facebook":
        page_id, token = account_id or credential("page_id", "FACEBOOK_PAGE_ID"), credential("access_token", "FACEBOOK_PAGE_ACCESS_TOKEN")
        if not page_id or not token:
            raise ProviderError("Facebook needs FACEBOOK_PAGE_ID and FACEBOOK_PAGE_ACCESS_TOKEN.")
        base = config('META_GRAPH_BASE_URL') or 'https://graph.facebook.com/v24.0'
        if len(image_urls) > 1:
            fields = {"access_token": token, "message": body}
            for index, url in enumerate(image_urls):
                photo = request_form(f"{base}/{page_id}/photos", {"access_token": token, "url": public_image_url(url), "published": "false"})
                media_id = str(photo.get("id") or "")
                if not media_id:
                    raise ProviderError("Facebook did not upload a carousel image.")
                fields[f"attached_media[{index}]"] = json.dumps({"media_fbid": media_id})
            result = request_form(f"{base}/{page_id}/feed", fields)
        else:
            endpoint = f"{base}/{page_id}/{'photos' if image_url else 'feed'}"
            fields = {"access_token": token, "caption" if image_url else "message": body}
            if image_url:
                fields["url"] = public_image_url(image_url)
            result = request_form(endpoint, fields)
        return str(result.get("post_id") or result.get("id") or "")
    if channel == "Instagram":
        target_id, token = account_id or credential("account_id", "INSTAGRAM_ACCOUNT_ID"), credential("access_token", "INSTAGRAM_ACCESS_TOKEN")
        if not target_id or not token:
            raise ProviderError("Instagram needs INSTAGRAM_ACCOUNT_ID and INSTAGRAM_ACCESS_TOKEN.")
        if not image_url:
            raise ProviderError("Instagram publishing requires an image in this first release.")
        base = config("META_GRAPH_BASE_URL") or "https://graph.facebook.com/v24.0"
        if len(image_urls) > 1:
            children: list[str] = []
            for url in image_urls:
                child = request_form(f"{base}/{target_id}/media", {"access_token": token, "image_url": public_image_url(url), "is_carousel_item": "true"})
                if not child.get("id"):
                    raise ProviderError("Instagram did not create a carousel item.")
                children.append(str(child["id"]))
            container = request_form(f"{base}/{target_id}/media", {"access_token": token, "media_type": "CAROUSEL", "children": ",".join(children), "caption": body})
        else:
            container = request_form(f"{base}/{target_id}/media", {"access_token": token, "image_url": public_image_url(image_url), "caption": body})
        creation_id = str(container.get("id") or "")
        if not creation_id:
            raise ProviderError("Instagram did not create a media container.")
        result = request_form(f"{base}/{target_id}/media_publish", {"access_token": token, "creation_id": creation_id})
        return str(result.get("id") or "")
    if channel == "Threads":
        user_id, token = account_id or credential("user_id", "THREADS_USER_ID"), credential("access_token", "THREADS_ACCESS_TOKEN")
        if not user_id or not token:
            raise ProviderError("Threads needs THREADS_USER_ID and THREADS_ACCESS_TOKEN.")
        base = config("THREADS_API_BASE_URL") or "https://graph.threads.net/v1.0"
        if len(image_urls) > 1:
            children: list[str] = []
            for url in image_urls:
                child = request_form(f"{base}/{user_id}/threads", {"access_token": token, "media_type": "IMAGE", "image_url": public_image_url(url), "is_carousel_item": "true"})
                if not child.get("id"):
                    raise ProviderError("Threads did not create a carousel item.")
                children.append(str(child["id"]))
            fields = {"access_token": token, "media_type": "CAROUSEL", "children": ",".join(children), "text": body}
        else:
            fields = {"access_token": token, "media_type": "IMAGE" if image_url else "TEXT", "text": body}
            if image_url:
                fields["image_url"] = public_image_url(image_url)
        container = request_form(f"{base}/{user_id}/threads", fields)
        creation_id = str(container.get("id") or "")
        if not creation_id:
            raise ProviderError("Threads did not create a media container.")
        result = request_form(f"{base}/{user_id}/threads_publish", {"access_token": token, "creation_id": creation_id})
        return str(result.get("id") or "")
    raise ProviderError("Unsupported provider.")


def delete_published_content(post: dict[str, Any], external_id: str, account: dict[str, Any] | None = None) -> None:
    """Delete one delivered item using the credential that originally published it."""
    channel = str(account.get("provider") or post["channel"]) if account else post["channel"]
    if not external_id:
        raise ProviderError("This delivery has no remote post ID and cannot be deleted.", retryable=False)
    secrets_for_account = decrypt_secrets(account["encrypted_secrets"]) if account else {}
    account_id = str(account["external_account_id"]) if account else ""
    def credential(name: str, environment: str) -> str:
        return secrets_for_account.get(name, "") or config(environment)
    if channel == "Discord":
        webhook_url = credential("webhook_url", "DISCORD_WEBHOOK_URL")
        request_delete(f"{webhook_url.rstrip('/')}/messages/{quote(external_id, safe='')}")
        return
    if channel == "Telegram":
        token, chat_id = credential("bot_token", "TELEGRAM_BOT_TOKEN"), account_id or credential("chat_id", "TELEGRAM_CHAT_ID")
        for message_id in external_id.split(","):
            telegram_request(token, "deleteMessage", {"chat_id": chat_id, "message_id": message_id})
        return
    if channel == "X":
        token = credential("access_token", "X_ACCESS_TOKEN")
        request_delete(f"https://api.x.com/2/tweets/{quote(external_id, safe='')}", {"Authorization": f"Bearer {token}"})
        return
    if channel == "LinkedIn":
        token, version = credential("access_token", "LINKEDIN_ACCESS_TOKEN"), config("LINKEDIN_API_VERSION")
        request_delete(f"https://api.linkedin.com/rest/posts/{quote(external_id, safe='')}", {"Authorization": f"Bearer {token}", "LinkedIn-Version": version, "X-Restli-Protocol-Version": "2.0.0"})
        return
    if channel in {"Facebook", "Instagram"}:
        token = credential("access_token", "FACEBOOK_PAGE_ACCESS_TOKEN" if channel == "Facebook" else "INSTAGRAM_ACCESS_TOKEN")
        base = config("META_GRAPH_BASE_URL") or "https://graph.facebook.com/v24.0"
        request_delete(f"{base}/{quote(external_id, safe='')}?{urlencode({'access_token': token})}")
        return
    if channel == "Threads":
        token = credential("access_token", "THREADS_ACCESS_TOKEN")
        base = config("THREADS_API_BASE_URL") or "https://graph.threads.net/v1.0"
        request_delete(f"{base}/{quote(external_id, safe='')}?{urlencode({'access_token': token})}")
        return
    raise ProviderError("This provider does not support deleting delivered content.", retryable=False)


def provider_status(channel: str) -> str:
    required = {
        "Facebook": ("FACEBOOK_PAGE_ID", "FACEBOOK_PAGE_ACCESS_TOKEN"),
        "Instagram": ("INSTAGRAM_ACCOUNT_ID", "INSTAGRAM_ACCESS_TOKEN"),
        "Threads": ("THREADS_USER_ID", "THREADS_ACCESS_TOKEN"),
        "X": ("X_ACCESS_TOKEN",),
        "Telegram": ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"),
        "Discord": ("DISCORD_WEBHOOK_URL",),
        "LinkedIn": ("LINKEDIN_AUTHOR_URN", "LINKEDIN_ACCESS_TOKEN", "LINKEDIN_API_VERSION"),
    }[channel]
    return "ready" if all(config(item) for item in required) else "needs configuration"


def claim_post(post_id: int) -> bool:
    with db() as connection:
        result = connection.execute("UPDATE posts SET state = 'publishing', publishing_started_at = ?, attempts = attempts + 1, last_error = NULL WHERE id = ? AND state = 'scheduled'", (now(), post_id))
    return result.rowcount == 1


def deliver(post_id: int) -> None:
    with db() as connection:
        row = connection.execute("SELECT * FROM posts WHERE id = ? AND state = 'publishing'", (post_id,)).fetchone()
    if row is None:
        return
    post = dict(row)
    with db() as connection:
        targets = [dict(target) for target in connection.execute(
            "SELECT post_targets.connection_id, connections.* FROM post_targets JOIN connections ON connections.id = post_targets.connection_id WHERE post_targets.post_id = ? AND post_targets.state != 'published'",
            (post_id,),
        ).fetchall()]
    if not targets:
        targets = [None]
    failures: list[ProviderError] = []
    delivered: list[str] = []
    for target in targets:
        try:
            if target and not target.get("is_active"):
                raise ProviderError("This provider account has been disabled.")
            external_id = publish(post, target)
            delivered.append(external_id)
            if target:
                with db() as connection:
                    connection.execute("UPDATE post_targets SET state = 'published', external_id = ?, last_error = NULL WHERE post_id = ? AND connection_id = ?", (external_id, post_id, target["connection_id"]))
        except ProviderError as error:
            failures.append(error)
            if target:
                with db() as connection:
                    connection.execute("UPDATE post_targets SET state = 'failed', last_error = ? WHERE post_id = ? AND connection_id = ?", (str(error)[:500], post_id, target["connection_id"]))
    if failures:
        with db() as connection:
            attempts = connection.execute("SELECT attempts FROM posts WHERE id = ?", (post_id,)).fetchone()[0]
            retryable = any(error.retryable for error in failures)
            state = "failed" if attempts >= MAX_ATTEMPTS or not retryable else "scheduled"
            detail = "; ".join(str(error) for error in failures)[:500]
            retry_delay = max([RETRY_BASE_SECONDS * (2 ** max(attempts - 1, 0)), *(error.retry_after or 0 for error in failures)])
            retry_at = (datetime.now(UTC) + timedelta(seconds=min(retry_delay, RETRY_MAX_SECONDS))).isoformat()
            connection.execute("UPDATE posts SET state = ?, publishing_started_at = NULL, scheduled_for = CASE WHEN ? = 'scheduled' THEN ? ELSE scheduled_for END, last_error = ? WHERE id = ?", (state, state, retry_at, detail, post_id))
            connection.execute("INSERT INTO deliveries (post_id, provider, status, detail, created_at) VALUES (?, ?, 'failed', ?, ?)", (post_id, post["channel"], detail, now()))
        return
    with db() as connection:
        external_id = ",".join(delivered)
        connection.execute("UPDATE posts SET state = 'published', publishing_started_at = NULL, published_at = ?, external_id = ?, last_error = NULL WHERE id = ?", (now(), external_id, post_id))
        connection.execute("INSERT INTO deliveries (post_id, provider, status, detail, created_at) VALUES (?, ?, 'published', ?, ?)", (post_id, post["channel"], external_id, now()))


def scheduler() -> None:
    while True:
        try:
            worker_heartbeat()
            cleanup_expired_records()
            recover_stale_deliveries()
            with db() as connection:
                rows = connection.execute("SELECT id FROM posts WHERE state = 'scheduled' AND scheduled_for <= ? ORDER BY scheduled_for LIMIT 10", (now(),)).fetchall()
            for row in rows:
                if claim_post(row["id"]):
                    deliver(row["id"])
        except Exception:
            LOGGER.exception("Scheduled-delivery poll failed")
        time.sleep(POLL_SECONDS)


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, directory=str(APP_DIR), **kwargs)

    def _json(self, payload: object, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def end_headers(self) -> None:
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "strict-origin-when-cross-origin")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        self.send_header("Content-Security-Policy", "default-src 'self'; base-uri 'self'; frame-ancestors 'none'; form-action 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'")
        super().end_headers()

    def _session(self) -> sqlite3.Row | None:
        cookie = SimpleCookie(self.headers.get("Cookie"))
        token = cookie.get("sosopo_session")
        if token is None:
            return None
        token_hash = hashlib.sha256(token.value.encode()).hexdigest()
        with db() as connection:
            return connection.execute(
                "SELECT sessions.*, users.username, users.role, users.timezone, users.signature FROM sessions JOIN users ON users.id = sessions.user_id WHERE token_hash = ? AND expires_at > ? AND users.is_active = 1",
                (token_hash, now()),
            ).fetchone()

    def _require_auth(self, csrf: bool = False) -> sqlite3.Row | None:
        session = self._session()
        if session is None:
            self._json({"error": "Authentication required."}, HTTPStatus.UNAUTHORIZED)
            return None
        if csrf and not secrets.compare_digest(self.headers.get("X-CSRF-Token", ""), session["csrf_token"]):
            self._json({"error": "Invalid CSRF token."}, HTTPStatus.FORBIDDEN)
            return None
        return session

    def _allow(self, scope: str, limit: int) -> bool:
        if allowed_request(self._source_ip(), scope, limit):
            return True
        self._json({"error": "Too many requests. Try again in a minute."}, HTTPStatus.TOO_MANY_REQUESTS)
        return False

    def _source_ip(self) -> str:
        return source_ip(self.client_address[0], self.headers.get("X-Forwarded-For", ""), config("SOSOPO_TRUSTED_PROXY_CIDRS"))

    def _create_session(self, user_id: int) -> dict[str, str]:
        token, csrf_token = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        with db() as connection:
            connection.execute("DELETE FROM sessions WHERE expires_at <= ?", (now(),))
            connection.execute(
                "INSERT INTO sessions (token_hash, csrf_token, user_id, expires_at, created_at) VALUES (?, ?, ?, ?, ?)",
                (hashlib.sha256(token.encode()).hexdigest(), csrf_token, user_id, expires_at(), now()),
            )
        return {"token": token, "csrf_token": csrf_token}

    def _json_session(self, payload: dict[str, Any], session: dict[str, str], status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps({**payload, "csrf_token": session["csrf_token"]}).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        secure = "; Secure" if urlparse(public_url()).scheme == "https" else ""
        self.send_header("Set-Cookie", f"sosopo_session={session['token']}; HttpOnly; SameSite=Strict; Path=/; Max-Age={SESSION_SECONDS}{secure}")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _redirect_session(self, location: str, session: dict[str, str]) -> None:
        secure = "; Secure" if urlparse(public_url()).scheme == "https" else ""
        self.send_response(HTTPStatus.FOUND)
        self.send_header("Location", location)
        self.send_header("Set-Cookie", f"sosopo_session={session['token']}; HttpOnly; SameSite=Strict; Path=/; Max-Age={SESSION_SECONDS}{secure}")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def _read_json(self) -> dict[str, Any]:
        size = int(self.headers.get("Content-Length", "0"))
        if size < 0 or size > MAX_UPLOAD_BYTES * 2:
            raise ValueError("Request body is too large.")
        payload = json.loads(self.rfile.read(size) or b"{}")
        if not isinstance(payload, dict):
            raise ValueError("JSON object required.")
        return payload

    @staticmethod
    def _schedule_time(value: object, timezone: object = "UTC") -> str:
        try:
            scheduled = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError as error:
            raise ValueError("Use an ISO 8601 schedule time.") from error
        zone = timezone_name(timezone)
        if scheduled.tzinfo is None:
            scheduled = scheduled.replace(tzinfo=ZoneInfo(zone))
        scheduled = scheduled.astimezone(UTC)
        if scheduled <= datetime.now(UTC):
            raise ValueError("Schedule time must be in the future.")
        return scheduled.isoformat()

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == "/metrics":
            token = config("SOSOPO_METRICS_TOKEN")
            authorization = self.headers.get("Authorization", "")
            if not token or not secrets.compare_digest(authorization, f"Bearer {token}"):
                self._json({"error": "Not found."}, HTTPStatus.NOT_FOUND); return
            with db() as connection:
                states = {row["state"]: row["count"] for row in connection.execute("SELECT state, COUNT(*) AS count FROM posts GROUP BY state").fetchall()}
                deliveries = {row["status"]: row["count"] for row in connection.execute("SELECT status, COUNT(*) AS count FROM deliveries GROUP BY status").fetchall()}
            lines = ["# HELP sosopo_posts Number of posts by state.", "# TYPE sosopo_posts gauge"]
            lines.extend(f'sosopo_posts{{state="{state}"}} {count}' for state, count in sorted(states.items()))
            lines.extend(["# HELP sosopo_deliveries_total Delivery attempts by result.", "# TYPE sosopo_deliveries_total counter"])
            lines.extend(f'sosopo_deliveries_total{{status="{status}"}} {count}' for status, count in sorted(deliveries.items()))
            lines.extend(["# HELP sosopo_worker_healthy Worker heartbeat health (1 healthy, 0 unhealthy).", "# TYPE sosopo_worker_healthy gauge", f"sosopo_worker_healthy {int(worker_healthy())}"])
            body = ("\n".join(lines) + "\n").encode()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/api/setup-status":
            with db() as connection:
                has_user = connection.execute("SELECT 1 FROM users LIMIT 1").fetchone() is not None
            self._json({"setup_required": not has_user})
            return
        if path == "/api/session":
            session = self._require_auth()
            if session:
                self._json({"username": session["username"], "role": session["role"], "timezone": session["timezone"], "signature": session["signature"], "csrf_token": session["csrf_token"]})
            return
        if path == "/api/auth/oidc/login":
            try:
                settings, state, nonce, verifier = oidc_settings(), secrets.token_urlsafe(32), secrets.token_urlsafe(32), secrets.token_urlsafe(64)
                challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
                with db() as connection:
                    connection.execute("DELETE FROM oidc_states WHERE expires_at <= ?", (now(),))
                    connection.execute("INSERT INTO oidc_states (state, nonce, code_verifier, expires_at) VALUES (?, ?, ?, ?)", (state, nonce, verifier, (datetime.now(UTC) + timedelta(seconds=OIDC_STATE_SECONDS)).isoformat()))
                query = urlencode({"response_type": "code", "client_id": settings["client_id"], "redirect_uri": oidc_redirect_uri(), "scope": "openid profile email", "state": state, "nonce": nonce, "code_challenge": challenge, "code_challenge_method": "S256"})
                self.send_response(HTTPStatus.FOUND); self.send_header("Location", f"{settings['authorization_endpoint']}?{query}"); self.end_headers()
            except ProviderError as error:
                self._json({"error": str(error)}, HTTPStatus.SERVICE_UNAVAILABLE)
            return
        if path == "/api/auth/oidc/callback":
            values = parse_qs(urlparse(self.path).query)
            state, code = values.get("state", [""])[0], values.get("code", [""])[0]
            if not state or not code:
                self._json({"error": "Invalid SSO callback."}, HTTPStatus.BAD_REQUEST); return
            with db() as connection:
                stored = connection.execute("SELECT * FROM oidc_states WHERE state = ? AND expires_at > ?", (state, now())).fetchone()
                connection.execute("DELETE FROM oidc_states WHERE state = ?", (state,))
            if stored is None:
                self._json({"error": "Expired or invalid SSO state."}, HTTPStatus.BAD_REQUEST); return
            try:
                settings = oidc_settings()
                token = request_form(settings["token_endpoint"], {"grant_type": "authorization_code", "code": code, "redirect_uri": oidc_redirect_uri(), "client_id": settings["client_id"], "client_secret": settings["client_secret"], "code_verifier": stored["code_verifier"]})
                identity = verify_oidc_id_token(token.get("id_token"), settings, stored["nonce"])
                subject = str(identity.get("sub", ""))
                username = str(identity.get("preferred_username") or identity.get("email") or subject).strip()
                if not subject or not username:
                    raise ProviderError("SSO provider did not return a usable identity.")
            except (ProviderError, HTTPError, URLError, json.JSONDecodeError) as error:
                self._json({"error": str(error) or "SSO sign-in failed."}, HTTPStatus.UNAUTHORIZED); return
            with db() as connection:
                user = connection.execute("SELECT * FROM users WHERE oidc_issuer = ? AND oidc_subject = ?", (settings["issuer"], subject)).fetchone()
                if user is None:
                    if config("OIDC_ALLOW_SIGNUP").lower() not in {"1", "true", "yes"}:
                        self._json({"error": "Your SSO account has not been provisioned."}, HTTPStatus.FORBIDDEN); return
                    salt = secrets.token_bytes(16)
                    user_id = insert_id(connection, "INSERT INTO users (username, password_salt, password_hash, role, timezone, oidc_issuer, oidc_subject, created_at) VALUES (?, ?, ?, 'user', 'UTC', ?, ?, ?)", (username, salt.hex(), hash_password(secrets.token_urlsafe(32), salt), settings["issuer"], subject, now()))
                    user = connection.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
                elif not user["is_active"]:
                    self._json({"error": "This user account is disabled."}, HTTPStatus.FORBIDDEN); return
            self._redirect_session("/", self._create_session(user["id"])); return
        if path == "/api/social-oauth/callback":
            values = parse_qs(urlparse(self.path).query)
            provider, state, code = values.get("provider", [""])[0], values.get("state", [""])[0], values.get("code", [""])[0]
            if provider not in {"Facebook", "Threads", "X", "LinkedIn", "Discord"} or not state or not code:
                self._json({"error": "Invalid social account callback."}, HTTPStatus.BAD_REQUEST); return
            with db() as connection:
                stored = connection.execute("SELECT * FROM social_oauth_states WHERE state = ? AND provider = ? AND expires_at > ?", (state, provider, now())).fetchone()
                connection.execute("DELETE FROM social_oauth_states WHERE state = ?", (state,))
            if stored is None:
                self._json({"error": "Expired or invalid social account connection state."}, HTTPStatus.BAD_REQUEST); return
            try:
                records = social_oauth_connections(provider, social_oauth_settings(provider), code, stored["code_verifier"])
                saved = save_social_connections(stored["user_id"], records)
                audit(stored["user_id"], "connection.oauth_connected", "connection", None, f"Connected {saved} account(s) through {provider} OAuth", self._source_ip())
                self.send_response(HTTPStatus.FOUND); self.send_header("Location", "/?connected=" + urlencode({"provider": provider, "accounts": saved})); self.end_headers()
            except ProviderError as error:
                self._json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
            return
        if path == "/api/health":
            try:
                with db() as connection:
                    connection.execute("SELECT 1")
                self._json({"status": "ok"})
            except Exception:
                self._json({"status": "unhealthy"}, HTTPStatus.SERVICE_UNAVAILABLE)
            return
        if path.startswith("/api/") and self._require_auth() is None:
            return
        if path.startswith("/api/social-oauth/") and path.endswith("/start"):
            provider = path.split("/")[3]
            if provider == "Instagram":
                provider = "Facebook"
            if provider not in {"Facebook", "Threads", "X", "LinkedIn", "Discord"}:
                self._json({"error": "Unsupported social OAuth provider."}, HTTPStatus.NOT_FOUND); return
            try:
                session, settings = self._session(), social_oauth_settings(provider)
                state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(64)
                with db() as connection:
                    connection.execute("DELETE FROM social_oauth_states WHERE expires_at <= ?", (now(),))
                    connection.execute("INSERT INTO social_oauth_states (state, provider, user_id, code_verifier, expires_at) VALUES (?, ?, ?, ?, ?)", (state, provider, session["user_id"], verifier if provider == "X" else None, (datetime.now(UTC) + timedelta(seconds=SOCIAL_OAUTH_STATE_SECONDS)).isoformat()))
                query = {"client_id": settings["client_id"], "redirect_uri": social_oauth_redirect_uri(), "response_type": "code", "scope": settings["scopes"], "state": state}
                if provider == "X":
                    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
                    query.update({"code_challenge": challenge, "code_challenge_method": "S256"})
                self.send_response(HTTPStatus.FOUND); self.send_header("Location", f"{settings['authorize']}?{urlencode(query)}"); self.end_headers()
            except ProviderError as error:
                self._json({"error": str(error)}, HTTPStatus.SERVICE_UNAVAILABLE)
            return
        if path == "/api/dashboard":
            session = self._session()
            with db() as connection:
                posts = [dict(row) for row in connection.execute("SELECT * FROM posts WHERE user_id = ? ORDER BY CASE state WHEN 'scheduled' THEN 0 WHEN 'failed' THEN 1 ELSE 2 END, scheduled_for, id DESC", (session["user_id"],)).fetchall()]
                for post in posts:
                    post["media_urls"] = [row["media_url"] for row in connection.execute("SELECT media_url FROM post_media WHERE post_id = ? ORDER BY position", (post["id"],)).fetchall()] or ([post["image_url"]] if post.get("image_url") else [])
            self._json({"posts": posts, "providers": [{"name": channel, "status": provider_status(channel), "oauth_available": social_oauth_enabled(channel)} for channel in CHANNELS]})
            return
        if path == "/api/ai/providers":
            self._session()
            self._json({"providers": available_ai_providers()})
            return
        if path == "/api/admin/users":
            session = self._session()
            if session["role"] != "admin":
                self._json({"error": "Administrator access required."}, HTTPStatus.FORBIDDEN); return
            with db() as connection:
                users = [dict(row) for row in connection.execute("SELECT id, username, role, is_active, timezone, oidc_issuer, created_at FROM users ORDER BY id").fetchall()]
            self._json({"users": users}); return
        if path == "/api/admin/ai-providers":
            session = self._session()
            if session["role"] != "admin":
                self._json({"error": "Administrator access required."}, HTTPStatus.FORBIDDEN); return
            providers = []
            for name, (slug, _, default_base) in AI_PROVIDERS.items():
                stored = stored_ai_provider_settings(name)
                catalog = ai_model_catalog(stored)
                providers.append({"name": name, "model": stored.get("model", AI_PROVIDER_MODELS[name][0]), "models": catalog or AI_PROVIDER_MODELS[name], "models_count": len(catalog), "models_checked_at": stored.get("models_checked_at"), "has_api_key": bool(stored.get("api_key"))})
            self._json({"providers": providers}); return
        if path.startswith("/api/admin/ai-providers/") and path.endswith("/models"):
            session = self._session()
            if session["role"] != "admin":
                self._json({"error": "Administrator access required."}, HTTPStatus.FORBIDDEN); return
            provider = unquote(path.split("/")[4])
            try:
                self._json({"models": ai_provider_models(provider)})
            except ProviderError as error:
                self._json({"error": str(error)}, HTTPStatus.BAD_GATEWAY)
            return
        if path == "/api/admin/audit-events":
            session = self._session()
            if session["role"] != "admin":
                self._json({"error": "Administrator access required."}, HTTPStatus.FORBIDDEN); return
            with db() as connection:
                events = [dict(row) for row in connection.execute("SELECT audit_events.*, users.username FROM audit_events LEFT JOIN users ON users.id = audit_events.user_id ORDER BY audit_events.id DESC LIMIT 200").fetchall()]
            self._json({"events": events}); return
        if path == "/api/admin/status":
            session = self._session()
            if session["role"] != "admin":
                self._json({"error": "Administrator access required."}, HTTPStatus.FORBIDDEN); return
            with db() as connection:
                states = {row["state"]: row["count"] for row in connection.execute("SELECT state, COUNT(*) AS count FROM posts GROUP BY state").fetchall()}
                heartbeat = connection.execute("SELECT checked_at FROM worker_heartbeats WHERE name = 'delivery'").fetchone()
                latest_failure = connection.execute("SELECT created_at, detail FROM deliveries WHERE status = 'failed' ORDER BY id DESC LIMIT 1").fetchone()
            self._json({"posts": states, "worker_healthy": worker_healthy(), "worker_checked_at": heartbeat["checked_at"] if heartbeat else None, "latest_delivery_failure": dict(latest_failure) if latest_failure else None})
            return
        if path == "/api/connections":
            session = self._session()
            with db() as connection:
                records = [dict(row) for row in connection.execute("SELECT id, provider, external_account_id, display_name, token_expires_at, is_active, created_at FROM connections WHERE user_id = ? ORDER BY provider, display_name", (session["user_id"],)).fetchall()]
            self._json({"connections": records})
            return
        if path.startswith("/api/posts/") and path.endswith("/deliveries"):
            try:
                post_id = int(path.split("/")[3])
            except ValueError:
                self._json({"error": "Invalid post ID."}, HTTPStatus.BAD_REQUEST); return
            session = self._session()
            with db() as connection:
                owner = connection.execute("SELECT id FROM posts WHERE id = ? AND user_id = ?", (post_id, session["user_id"])).fetchone()
                if owner is None:
                    self._json({"error": "Post not found."}, HTTPStatus.NOT_FOUND); return
                deliveries = [dict(row) for row in connection.execute("SELECT provider, status, detail, created_at FROM deliveries WHERE post_id = ? ORDER BY id DESC", (post_id,)).fetchall()]
                targets = [dict(row) for row in connection.execute("SELECT post_targets.connection_id, post_targets.state, post_targets.external_id, post_targets.last_error, connections.provider, connections.display_name, connections.external_account_id FROM post_targets JOIN connections ON connections.id = post_targets.connection_id WHERE post_targets.post_id = ? ORDER BY connections.display_name", (post_id,)).fetchall()]
            self._json({"deliveries": deliveries, "targets": targets})
            return
        if path.startswith("/uploads/"):
            filename = Path(path).name
            if filename != path.removeprefix("/uploads/") or not (UPLOADS_DIR / filename).is_file():
                self._json({"error": "Not found."}, HTTPStatus.NOT_FOUND)
                return
            upload = UPLOADS_DIR / filename
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", next((kind for kind, suffix in IMAGE_TYPES.items() if suffix == upload.suffix), "application/octet-stream"))
            self.send_header("Content-Length", str(upload.stat().st_size))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            with upload.open("rb") as file:
                self.copyfile(file, self.wfile)
            return
        if self.path == "/":
            self.path = "/index.html"
        return super().do_GET()

    def do_HEAD(self) -> None:  # noqa: N802
        if urlparse(self.path).path == "/api/health":
            try:
                with db() as connection:
                    connection.execute("SELECT 1")
                self.send_response(HTTPStatus.OK)
            except Exception:
                self.send_response(HTTPStatus.SERVICE_UNAVAILABLE)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            return
        return super().do_HEAD()

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        try:
            payload = self._read_json()
            if path == "/api/setup":
                if not self._allow("auth", AUTH_REQUESTS_PER_MINUTE): return
                username, password = str(payload.get("username", "")).strip(), str(payload.get("password", ""))
                if len(username) < 3 or len(password) < 12:
                    self._json({"error": "Use a username of at least 3 characters and a password of at least 12 characters."}, HTTPStatus.BAD_REQUEST); return
                with db() as connection:
                    if connection.execute("SELECT 1 FROM users LIMIT 1").fetchone():
                        self._json({"error": "Setup has already been completed."}, HTTPStatus.CONFLICT); return
                    try:
                        connection.execute("INSERT INTO instance_settings (name, value) VALUES ('initial_setup', ?)", (now(),))
                    except Exception:
                        self._json({"error": "Setup has already been completed."}, HTTPStatus.CONFLICT); return
                    salt = secrets.token_bytes(16)
                    user_id = insert_id(connection, "INSERT INTO users (username, password_salt, password_hash, role, timezone, created_at) VALUES (?, ?, ?, ?, 'UTC', ?)", (username, salt.hex(), hash_password(password, salt), "admin", now()))
                    connection.execute("UPDATE posts SET user_id = ? WHERE user_id IS NULL", (user_id,))
                audit(user_id, "setup.completed", "user", user_id, "Created initial administrator", self._source_ip())
                self._json_session({"username": username}, self._create_session(user_id), HTTPStatus.CREATED); return
            if path == "/api/login":
                if not self._allow("auth", AUTH_REQUESTS_PER_MINUTE): return
                username, password = str(payload.get("username", "")).strip(), str(payload.get("password", ""))
                with db() as connection:
                    user = connection.execute("SELECT * FROM users WHERE username = ? AND is_active = 1", (username,)).fetchone()
                if user is None or not secrets.compare_digest(hash_password(password, bytes.fromhex(user["password_salt"])), user["password_hash"]):
                    self._json({"error": "Invalid username or password."}, HTTPStatus.UNAUTHORIZED); return
                self._json_session({"username": user["username"]}, self._create_session(user["id"])); return
            if not self._allow("write", WRITE_REQUESTS_PER_MINUTE):
                return
            if path == "/api/logout":
                session = self._require_auth(csrf=True)
                if session is None: return
                with db() as connection:
                    connection.execute("DELETE FROM sessions WHERE id = ?", (session["id"],))
                self._json({"status": "logged out"}); return
            session = self._require_auth(csrf=True)
            if session is None:
                return
            if path == "/api/admin/ai-providers":
                if session["role"] != "admin":
                    self._json({"error": "Administrator access required."}, HTTPStatus.FORBIDDEN); return
                provider = str(payload.get("provider", "")).strip()
                definition = AI_PROVIDERS.get(provider)
                if definition is None:
                    self._json({"error": "Choose a supported AI provider."}, HTTPStatus.BAD_REQUEST); return
                current = stored_ai_provider_settings(provider)
                model = str(payload.get("model", "")).strip() or current.get("model") or AI_PROVIDER_MODELS[provider][0]
                api_key = str(payload.get("api_key", "")).strip()
                if len(model) > 200:
                    self._json({"error": "Choose a model."}, HTTPStatus.BAD_REQUEST); return
                if not api_key and not current.get("api_key"):
                    self._json({"error": "Provide an API key for this provider."}, HTTPStatus.BAD_REQUEST); return
                catalog = ai_model_catalog(current) or AI_PROVIDER_MODELS[provider]
                # The browser normally supplies a model from this catalog. Do
                # not make saving a credential depend on a prior live catalog
                # refresh, though: provider catalogs can be temporarily
                # unavailable, and the selected default may be newly released.
                if model not in catalog:
                    catalog = [model, *catalog]
                stored = {"api_key": api_key or current["api_key"], "base_url": definition[2], "model": model, "models": json.dumps(catalog)}
                if current.get("models_checked_at"):
                    stored["models_checked_at"] = current["models_checked_at"]
                save_ai_provider_settings(provider, stored)
                audit(session["user_id"], "ai_provider.saved", "instance", provider, f"Configured {provider} AI provider", self._source_ip())
                self._json({"name": provider, "model": model, "has_api_key": True}); return
            if path.startswith("/api/admin/ai-providers/") and path.endswith("/remove"):
                if session["role"] != "admin":
                    self._json({"error": "Administrator access required."}, HTTPStatus.FORBIDDEN); return
                provider = unquote(path.split("/")[4])
                if provider not in AI_PROVIDERS:
                    self._json({"error": "Choose a supported AI provider."}, HTTPStatus.BAD_REQUEST); return
                removed = remove_ai_provider_settings(provider)
                audit(session["user_id"], "ai_provider.removed", "instance", provider, f"Removed {provider} UI-saved API key", self._source_ip())
                self._json({"status": "removed" if removed else "not configured", "name": provider}); return
            if path == "/api/ai/generate":
                provider = str(payload.get("provider", ""))
                model = str(payload.get("model", ""))
                instruction = str(payload.get("instruction", ""))
                draft = str(payload.get("draft", ""))
                channels = payload.get("channels", [])
                if not isinstance(channels, list) or any(str(channel) not in CHANNELS for channel in channels):
                    self._json({"error": "Choose valid post platforms for AI generation."}, HTTPStatus.BAD_REQUEST); return
                try:
                    copy = generate_post_copy(provider, model, instruction, draft, [str(channel) for channel in channels])
                except ProviderError as error:
                    self._json({"error": str(error)}, HTTPStatus.BAD_GATEWAY); return
                audit(session["user_id"], "post.ai_generated", "user", session["user_id"], f"Generated post copy with {provider}", self._source_ip())
                self._json({"copy": copy}); return
            if path == "/api/me/timezone":
                zone = timezone_name(payload.get("timezone"))
                with db() as connection:
                    connection.execute("UPDATE users SET timezone = ? WHERE id = ?", (zone, session["user_id"]))
                self._json({"timezone": zone}); return
            if path == "/api/me/settings":
                zone = timezone_name(payload.get("timezone") or session["timezone"])
                signature = str(payload.get("signature", "")).strip()
                if len(signature) > 1_000:
                    self._json({"error": "Signature must be 1,000 characters or fewer."}, HTTPStatus.BAD_REQUEST); return
                with db() as connection:
                    connection.execute("UPDATE users SET timezone = ?, signature = ? WHERE id = ?", (zone, signature, session["user_id"]))
                audit(session["user_id"], "user.settings_changed", "user", session["user_id"], "Updated timezone or signature", self._source_ip())
                self._json({"timezone": zone, "signature": signature}); return
            if path == "/api/me/password":
                current_password = str(payload.get("current_password", ""))
                new_password = str(payload.get("new_password", ""))
                if len(new_password) < 12:
                    self._json({"error": "Use a new password of at least 12 characters."}, HTTPStatus.BAD_REQUEST); return
                with db() as connection:
                    user = connection.execute("SELECT password_salt, password_hash FROM users WHERE id = ?", (session["user_id"],)).fetchone()
                    if user is None or not secrets.compare_digest(hash_password(current_password, bytes.fromhex(user["password_salt"])), user["password_hash"]):
                        self._json({"error": "Current password is incorrect."}, HTTPStatus.UNAUTHORIZED); return
                    salt = secrets.token_bytes(16)
                    connection.execute("UPDATE users SET password_salt = ?, password_hash = ? WHERE id = ?", (salt.hex(), hash_password(new_password, salt), session["user_id"]))
                    connection.execute("DELETE FROM sessions WHERE user_id = ?", (session["user_id"],))
                audit(session["user_id"], "user.password_changed", "user", session["user_id"], "Changed password and revoked sessions", self._source_ip())
                self._json_session({"status": "password changed"}, self._create_session(session["user_id"])); return
            if path == "/api/admin/users":
                if session["role"] != "admin":
                    self._json({"error": "Administrator access required."}, HTTPStatus.FORBIDDEN); return
                username, password = str(payload.get("username", "")).strip(), str(payload.get("password", ""))
                role, zone = str(payload.get("role", "user")), timezone_name(payload.get("timezone", "UTC"))
                if len(username) < 3 or len(password) < 12 or role not in {"admin", "user"}:
                    self._json({"error": "Use a username/password of at least 3/12 characters and a valid role."}, HTTPStatus.BAD_REQUEST); return
                salt = secrets.token_bytes(16)
                with db() as connection:
                    user_id = insert_id(connection, "INSERT INTO users (username, password_salt, password_hash, role, timezone, created_at) VALUES (?, ?, ?, ?, ?, ?)", (username, salt.hex(), hash_password(password, salt), role, zone, now()))
                audit(session["user_id"], "user.created", "user", user_id, f"Created {role} user {username}", self._source_ip())
                self._json({"id": user_id, "username": username, "role": role, "timezone": zone}, HTTPStatus.CREATED); return
            if path.startswith("/api/admin/users/") and path.endswith("/disable"):
                if session["role"] != "admin":
                    self._json({"error": "Administrator access required."}, HTTPStatus.FORBIDDEN); return
                user_id = int(path.split("/")[4])
                if user_id == session["user_id"]:
                    self._json({"error": "An administrator cannot disable their own account."}, HTTPStatus.BAD_REQUEST); return
                with db() as connection:
                    changed = connection.execute("UPDATE users SET is_active = 0 WHERE id = ?", (user_id,))
                    if changed.rowcount != 1:
                        self._json({"error": "User not found."}, HTTPStatus.NOT_FOUND); return
                    connection.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
                audit(session["user_id"], "user.disabled", "user", user_id, "Disabled user and revoked sessions", self._source_ip())
                self._json({"status": "disabled"}); return
            if path == "/api/connections":
                provider = str(payload.get("provider", "")).strip()
                account_id = str(payload.get("external_account_id", "")).strip()
                display_name = str(payload.get("display_name", "")).strip()
                secret_values = payload.get("secrets", {})
                settings = payload.get("settings", {})
                if provider not in CHANNELS or not account_id or not display_name or not isinstance(secret_values, dict) or not isinstance(settings, dict):
                    self._json({"error": "Provider, account ID, display name, secrets, and settings are required."}, HTTPStatus.BAD_REQUEST); return
                secrets_to_store = {str(key): str(value) for key, value in secret_values.items() if value}
                if provider == "Discord":
                    webhook_url = secrets_to_store.get("webhook_url") or account_id
                    match = re.fullmatch(r"https://(?:discord(?:app)?\.com)/api/webhooks/(\d+)/[^/?#]+/?", webhook_url)
                    if not match:
                        self._json({"error": "Enter a valid Discord incoming webhook URL."}, HTTPStatus.BAD_REQUEST); return
                    account_id = match.group(1)
                    secrets_to_store = {"webhook_url": webhook_url}
                token_expiry = str(payload.get("token_expires_at", "")).strip() or None
                if token_expiry and token_is_expired(token_expiry):
                    self._json({"error": "token_expires_at must be a future ISO 8601 timestamp with timezone."}, HTTPStatus.BAD_REQUEST); return
                if not secrets_to_store:
                    self._json({"error": "At least one credential is required."}, HTTPStatus.BAD_REQUEST); return
                with db() as connection:
                    connection_id = insert_id(connection,
                        "INSERT INTO connections (user_id, provider, external_account_id, display_name, encrypted_secrets, settings_json, token_expires_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (session["user_id"], provider, account_id, display_name, encrypt_secrets(secrets_to_store), json.dumps(settings), token_expiry, now()),
                    )
                audit(session["user_id"], "connection.created", "connection", connection_id, f"Created {provider} connection {display_name}", self._source_ip())
                self._json({"id": connection_id, "provider": provider, "external_account_id": account_id, "display_name": display_name}, HTTPStatus.CREATED); return
            if path.startswith("/api/connections/") and path.endswith("/disable"):
                connection_id = int(path.split("/")[3])
                with db() as connection:
                    changed = connection.execute("UPDATE connections SET is_active = 0 WHERE id = ? AND user_id = ?", (connection_id, session["user_id"]))
                    if changed.rowcount != 1:
                        self._json({"error": "Connection not found."}, HTTPStatus.NOT_FOUND); return
                audit(session["user_id"], "connection.disabled", "connection", connection_id, "Disabled provider connection", self._source_ip())
                self._json({"status": "disabled"}); return
            if path.startswith("/api/connections/") and path.endswith("/rotate"):
                connection_id = int(path.split("/")[3])
                secret_values = payload.get("secrets", {})
                token_expiry = str(payload.get("token_expires_at", "")).strip() or None
                if not isinstance(secret_values, dict):
                    self._json({"error": "secrets must be an object."}, HTTPStatus.BAD_REQUEST); return
                secrets_to_store = {str(key): str(value) for key, value in secret_values.items() if value}
                if not secrets_to_store:
                    self._json({"error": "Provide at least one replacement credential."}, HTTPStatus.BAD_REQUEST); return
                if token_expiry and token_is_expired(token_expiry):
                    self._json({"error": "token_expires_at must be a future ISO 8601 timestamp with timezone."}, HTTPStatus.BAD_REQUEST); return
                with db() as connection:
                    current = connection.execute("SELECT encrypted_secrets FROM connections WHERE id = ? AND user_id = ?", (connection_id, session["user_id"])).fetchone()
                    if current is None:
                        self._json({"error": "Connection not found."}, HTTPStatus.NOT_FOUND); return
                    merged = {**decrypt_secrets(current["encrypted_secrets"]), **secrets_to_store}
                    connection.execute("UPDATE connections SET encrypted_secrets = ?, token_expires_at = ?, is_active = 1 WHERE id = ?", (encrypt_secrets(merged), token_expiry, connection_id))
                audit(session["user_id"], "connection.rotated", "connection", connection_id, "Rotated provider credentials", self._source_ip())
                self._json({"status": "rotated", "token_expires_at": token_expiry}); return
            if path == "/api/uploads":
                content_type, encoded = str(payload.get("content_type", "")).lower(), payload.get("data", "")
                if content_type not in IMAGE_TYPES or not isinstance(encoded, str):
                    self._json({"error": "Upload a PNG, JPEG, GIF, or WebP image."}, HTTPStatus.BAD_REQUEST); return
                try:
                    image = base64.b64decode(encoded, validate=True)
                except (binascii.Error, ValueError):
                    self._json({"error": "Invalid image data."}, HTTPStatus.BAD_REQUEST); return
                if not image or len(image) > MAX_UPLOAD_BYTES:
                    self._json({"error": "Images must be between 1 byte and 5 MB."}, HTTPStatus.BAD_REQUEST); return
                actual_type = detected_image_type(image)
                if actual_type != content_type:
                    self._json({"error": "Image bytes do not match the declared content type."}, HTTPStatus.BAD_REQUEST); return
                inspect_image(image, actual_type)
                filename = f"{uuid.uuid4().hex}{IMAGE_TYPES[actual_type]}"
                self._json({"url": store_media(filename, actual_type, image)}, HTTPStatus.CREATED); return
            if path == "/api/posts":
                body, legacy_channel = str(payload.get("body", "")).strip(), str(payload.get("channel", "")).strip()
                requested_channels = payload.get("channels", [legacy_channel])
                if not isinstance(requested_channels, list) or not requested_channels:
                    self._json({"error": "Select at least one platform."}, HTTPStatus.BAD_REQUEST); return
                channels = list(dict.fromkeys(str(channel).strip() for channel in requested_channels))
                image_urls = payload.get("image_urls")
                if image_urls is None:
                    image_urls = [str(payload.get("image_url", "")).strip()] if payload.get("image_url") else []
                if not isinstance(image_urls, list) or any(not isinstance(url, str) or not url.strip() for url in image_urls):
                    self._json({"error": "image_urls must be an array of uploaded image URLs."}, HTTPStatus.BAD_REQUEST); return
                image_urls = list(dict.fromkeys(url.strip() for url in image_urls))
                image_url = image_urls[0] if image_urls else None
                target_ids = payload.get("connection_ids", [])
                if not body or any(channel not in CHANNELS for channel in channels):
                    self._json({"error": "A post and at least one supported platform are required."}, HTTPStatus.BAD_REQUEST); return
                if len(image_urls) > MAX_POST_MEDIA or any(not media_exists(url) for url in image_urls):
                    self._json({"error": "Unknown image upload."}, HTTPStatus.BAD_REQUEST); return
                if not isinstance(target_ids, list) or any(not isinstance(item, int) for item in target_ids):
                    self._json({"error": "connection_ids must be an array of numeric account IDs."}, HTTPStatus.BAD_REQUEST); return
                if payload.get("apply_signature", True) and session["signature"]:
                    body = f"{body.rstrip()}\n\n{str(session['signature']).strip()}"
                schedule_zone = timezone_name(payload.get("scheduled_timezone") or session["timezone"])
                schedule = self._schedule_time(payload["scheduled_for"], schedule_zone) if payload.get("scheduled_for") else None
                with db() as connection:
                    if target_ids:
                        placeholders = ",".join("?" for _ in target_ids)
                        selected = connection.execute(f"SELECT id, provider FROM connections WHERE user_id = ? AND is_active = 1 AND (token_expires_at IS NULL OR token_expires_at > ?) AND id IN ({placeholders})", (session["user_id"], now(), *target_ids)).fetchall()
                        if len(selected) != len(set(target_ids)) or {row["provider"] for row in selected} != set(channels):
                            self._json({"error": "Select active accounts for every chosen platform."}, HTTPStatus.BAD_REQUEST); return
                    elif len(channels) != 1:
                        self._json({"error": "Connect an account for each platform when publishing to more than one platform."}, HTTPStatus.BAD_REQUEST); return
                    for channel in channels:
                        validate_post(channel, body, image_url, len(image_urls))
                    post_id = insert_id(connection, "INSERT INTO posts (user_id, body, channel, state, scheduled_for, scheduled_timezone, image_url, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (session["user_id"], body, channels[0], "scheduled" if schedule else "draft", schedule, schedule_zone if schedule else None, image_url, now()))
                    for target_id in dict.fromkeys(target_ids):
                        connection.execute("INSERT INTO post_targets (post_id, connection_id) VALUES (?, ?)", (post_id, target_id))
                    for position, url in enumerate(image_urls):
                        connection.execute("INSERT INTO post_media (post_id, media_url, position) VALUES (?, ?, ?)", (post_id, url, position))
                    row = dict(connection.execute("SELECT * FROM posts WHERE id = ?", (post_id,)).fetchone())
                row["media_urls"] = image_urls
                audit(session["user_id"], "post.created", "post", post_id, f"Created {'/'.join(channels)} post", self._source_ip())
                self._json(row, HTTPStatus.CREATED); return
            if path.startswith("/api/posts/") and path.endswith("/remove"):
                post_id = int(path.split("/")[3])
                with db() as connection:
                    post = connection.execute("SELECT state FROM posts WHERE id = ? AND user_id = ?", (post_id, session["user_id"])).fetchone()
                    if post is None or post["state"] not in {"draft", "scheduled", "failed"}:
                        self._json({"error": "Only your drafts, scheduled posts, or failed posts can be removed from the queue."}, HTTPStatus.CONFLICT); return
                    connection.execute("DELETE FROM deliveries WHERE post_id = ?", (post_id,))
                    connection.execute("DELETE FROM post_media WHERE post_id = ?", (post_id,))
                    connection.execute("DELETE FROM post_targets WHERE post_id = ?", (post_id,))
                    connection.execute("DELETE FROM posts WHERE id = ? AND user_id = ?", (post_id, session["user_id"]))
                audit(session["user_id"], "post.removed", "post", post_id, "Removed unpublished post from queue", self._source_ip())
                self._json({"status": "removed"}); return
            if path.startswith("/api/posts/") and path.endswith("/delete-from-channels"):
                post_id = int(path.split("/")[3])
                with db() as connection:
                    row = connection.execute("SELECT * FROM posts WHERE id = ? AND user_id = ? AND state = 'published'", (post_id, session["user_id"])).fetchone()
                    targets = [dict(item) for item in connection.execute("SELECT post_targets.connection_id, post_targets.external_id, connections.* FROM post_targets JOIN connections ON connections.id = post_targets.connection_id WHERE post_targets.post_id = ? AND post_targets.state = 'published'", (post_id,)).fetchall()]
                if row is None:
                    self._json({"error": "Only one of your published posts can be deleted from channels."}, HTTPStatus.NOT_FOUND); return
                post, deleted, failed = dict(row), [], []
                # Legacy one-provider records do not have a connection target.
                pending = targets or [{"connection_id": None, "external_id": post.get("external_id", "")}]
                for target in pending:
                    try:
                        delete_published_content(post, str(target.get("external_id") or ""), target if target.get("connection_id") is not None else None)
                        deleted.append(str(target.get("provider") or post["channel"]))
                        if target.get("connection_id") is not None:
                            with db() as connection:
                                connection.execute("UPDATE post_targets SET state = 'deleted', last_error = NULL WHERE post_id = ? AND connection_id = ?", (post_id, target["connection_id"]))
                    except ProviderError as error:
                        failed.append({"provider": str(target.get("provider") or post["channel"]), "error": str(error)})
                        if target.get("connection_id") is not None:
                            with db() as connection:
                                connection.execute("UPDATE post_targets SET last_error = ? WHERE post_id = ? AND connection_id = ?", (str(error)[:500], post_id, target["connection_id"]))
                if deleted and not failed:
                    with db() as connection:
                        connection.execute("UPDATE posts SET state = 'deleted', last_error = NULL WHERE id = ?", (post_id,))
                elif failed:
                    with db() as connection:
                        connection.execute("UPDATE posts SET last_error = ? WHERE id = ?", ("Some channels could not delete the post.", post_id))
                audit(session["user_id"], "post.remote_delete", "post", post_id, f"Deleted from {len(deleted)} channel(s), failed on {len(failed)}", self._source_ip())
                self._json({"deleted": deleted, "failed": failed}, HTTPStatus.OK if deleted else HTTPStatus.BAD_GATEWAY); return
            if path.startswith("/api/posts/") and path.endswith("/schedule"):
                post_id, schedule_zone = int(path.split("/")[3]), timezone_name(payload.get("scheduled_timezone") or session["timezone"])
                schedule = self._schedule_time(payload.get("scheduled_for", ""), schedule_zone)
                with db() as connection:
                    cursor = connection.execute("UPDATE posts SET state = 'scheduled', scheduled_for = ?, scheduled_timezone = ?, attempts = 0, last_error = NULL WHERE id = ? AND user_id = ? AND state != 'published'", (schedule, schedule_zone, post_id, session["user_id"]))
                    connection.execute("UPDATE post_targets SET state = 'pending', last_error = NULL WHERE post_id = ?", (post_id,))
                if cursor.rowcount != 1:
                    self._json({"error": "Post not found or already published."}, HTTPStatus.NOT_FOUND); return
                audit(session["user_id"], "post.scheduled", "post", post_id, f"Scheduled for {schedule}", self._source_ip())
                self._json({"status": "scheduled"}); return
            if path.startswith("/api/posts/") and path.endswith("/publish"):
                post_id = int(path.split("/")[3])
                with db() as connection:
                    found = connection.execute("SELECT id FROM posts WHERE id = ? AND user_id = ? AND state != 'published'", (post_id, session["user_id"])).fetchone()
                    if found is None:
                        self._json({"error": "Post not found or already published."}, HTTPStatus.NOT_FOUND); return
                    connection.execute("UPDATE posts SET state = 'scheduled', scheduled_for = ? WHERE id = ? AND user_id = ?", (now(), post_id, session["user_id"]))
                audit(session["user_id"], "post.queued", "post", post_id, "Queued for immediate delivery", self._source_ip())
                self._json({"status": "queued"}, HTTPStatus.ACCEPTED); return
            if path.startswith("/api/posts/") and path.endswith("/retry"):
                post_id = int(path.split("/")[3])
                with db() as connection:
                    cursor = connection.execute("UPDATE posts SET state = 'scheduled', scheduled_for = ?, scheduled_timezone = 'UTC', attempts = 0, publishing_started_at = NULL, last_error = NULL WHERE id = ? AND user_id = ? AND state = 'failed'", (now(), post_id, session["user_id"]))
                    if cursor.rowcount == 1:
                        connection.execute("UPDATE post_targets SET state = 'pending', last_error = NULL WHERE post_id = ? AND state != 'published'", (post_id,))
                if cursor.rowcount != 1:
                    self._json({"error": "Only one of your failed posts can be retried."}, HTTPStatus.NOT_FOUND); return
                audit(session["user_id"], "post.retried", "post", post_id, "Manually retried failed delivery", self._source_ip())
                self._json({"status": "queued"}, HTTPStatus.ACCEPTED); return
            self._json({"error": "Not found."}, HTTPStatus.NOT_FOUND)
        except (json.JSONDecodeError, ValueError) as error:
            self._json({"error": str(error) or "Invalid request."}, HTTPStatus.BAD_REQUEST)
        except ProviderError as error:
            self._json({"error": str(error) or "Configuration error."}, HTTPStatus.BAD_REQUEST)
        except Exception:
            LOGGER.exception("Unhandled API write failure")
            self._json({"error": "The request could not be completed."}, HTTPStatus.INTERNAL_SERVER_ERROR)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    setup_database()
    ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
