"""Sosopo: a small, self-hosted social publishing dashboard."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
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
WORKSPACE_ROLES = ("owner", "admin", "editor", "viewer")
WORKSPACE_ROLE_RANK = {"viewer": 0, "editor": 1, "admin": 2, "owner": 3}
MAX_WORKSPACE_NAME_LENGTH = 80
INVITATION_SECONDS = 7 * 24 * 60 * 60
EXPIRED_INVITATION_RETENTION_DAYS = 30
EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
CONNECTION_EXPIRY_WARNING_DAYS = 7
TOKEN_REFRESH_INTERVAL_SECONDS = 15 * 60
TOKEN_REFRESH_HORIZON_HOURS = 24
# Per-plan limits for hosted deployments. self_hosted is deliberately
# unlimited so existing installations keep today's behavior. Override or add
# plans with the SOSOPO_PLAN_LIMITS JSON environment value.
PLAN_LIMITS: dict[str, dict[str, int] | None] = {
    "self_hosted": None,
    "free": {"members": 3, "connections": 3, "posts_per_month": 30, "ai_generations_per_month": 20, "ai_media_per_month": 5, "storage_mb": 200},
    "starter": {"members": 10, "connections": 10, "posts_per_month": 300, "ai_generations_per_month": 300, "ai_media_per_month": 100, "storage_mb": 2_000},
    "pro": {"members": 50, "connections": 50, "posts_per_month": 3_000, "ai_generations_per_month": 3_000, "ai_media_per_month": 1_000, "storage_mb": 20_000},
}
STRIPE_PLAN_PRICE_VARIABLES = {"starter": "STRIPE_PRICE_STARTER", "pro": "STRIPE_PRICE_PRO"}
STRIPE_WEBHOOK_TOLERANCE_SECONDS = 300
MEDIA_JOB_KINDS = ("image", "video")
MAX_MEDIA_PROMPT_LENGTH = 2_000
MAX_MEDIA_STYLE_LENGTH = 200
MAX_MEDIA_DOWNLOAD_BYTES = 100 * 1024 * 1024
MEDIA_IMAGE_SIZES = {"1:1": "1024x1024", "3:2": "1536x1024", "2:3": "1024x1536", "16:9": "1792x1024", "9:16": "1024x1792"}
MEDIA_VIDEO_SIZES = {"16:9": "1280x720", "9:16": "720x1280", "1:1": "720x720"}
VIDEO_POLL_SECONDS = 10
VIDEO_POLL_LIMIT = 90
# Media-capable models per provider. Only OpenAI-compatible media APIs are
# called; providers absent from a map cannot run that media kind.
AI_PROVIDER_IMAGE_MODELS = {
    "OpenAI": ["gpt-image-1.5", "gpt-image-1"],
    "OpenRouter": ["openai/gpt-image-1.5", "google/gemini-2.5-flash-image"],
    "Z.AI GLM": ["cogview-4.5"],
}
AI_PROVIDER_VIDEO_MODELS = {
    "OpenAI": ["sora-2.2", "sora-2"],
}
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


def connection_health(record: dict[str, Any]) -> str:
    """Summarize one connection as active, expiring_soon, expired, or disabled."""
    if not record.get("is_active"):
        return "disabled"
    expiry = record.get("token_expires_at")
    if not expiry:
        return "active"
    if token_is_expired(expiry):
        return "expired"
    try:
        remaining = datetime.fromisoformat(str(expiry).replace("Z", "+00:00")) - datetime.now(UTC)
    except ValueError:
        return "expired"
    return "expiring_soon" if remaining <= timedelta(days=CONNECTION_EXPIRY_WARNING_DAYS) else "active"


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
        connection.execute(
            """CREATE TABLE IF NOT EXISTS workspaces (
                id %s,
                name TEXT NOT NULL,
                slug TEXT NOT NULL UNIQUE,
                owner_user_id INTEGER NOT NULL,
                plan TEXT NOT NULL DEFAULT 'self_hosted',
                status TEXT NOT NULL DEFAULT 'active',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(owner_user_id) REFERENCES users(id)
            )""" % id_column
        )
        connection.execute(
            """CREATE TABLE IF NOT EXISTS workspace_memberships (
                id %s,
                workspace_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                role TEXT NOT NULL DEFAULT 'editor',
                invite_state TEXT NOT NULL DEFAULT 'active',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(workspace_id, user_id),
                FOREIGN KEY(workspace_id) REFERENCES workspaces(id),
                FOREIGN KEY(user_id) REFERENCES users(id)
            )""" % id_column
        )
        connection.execute(
            """CREATE TABLE IF NOT EXISTS workspace_invitations (
                id %s,
                workspace_id INTEGER NOT NULL,
                email TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'editor',
                token_hash TEXT NOT NULL UNIQUE,
                invited_by INTEGER NOT NULL,
                expires_at TEXT NOT NULL,
                accepted_at TEXT,
                accepted_user_id INTEGER,
                created_at TEXT NOT NULL,
                FOREIGN KEY(workspace_id) REFERENCES workspaces(id),
                FOREIGN KEY(invited_by) REFERENCES users(id)
            )""" % id_column
        )
        connection.execute(
            """CREATE TABLE IF NOT EXISTS media_jobs (
                id %s,
                workspace_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                kind TEXT NOT NULL,
                prompt TEXT NOT NULL,
                aspect_ratio TEXT NOT NULL DEFAULT '1:1',
                style TEXT NOT NULL DEFAULT '',
                provider TEXT NOT NULL,
                model TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'queued',
                progress INTEGER NOT NULL DEFAULT 0,
                error TEXT,
                result_url TEXT,
                moderation TEXT NOT NULL DEFAULT 'pending',
                reviewed_by INTEGER,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(workspace_id) REFERENCES workspaces(id),
                FOREIGN KEY(user_id) REFERENCES users(id)
            )""" % id_column
        )
        connection.execute("CREATE INDEX IF NOT EXISTS media_jobs_queue ON media_jobs(status, id)")
        connection.execute("CREATE INDEX IF NOT EXISTS media_jobs_workspace ON media_jobs(workspace_id, id)")
        connection.execute(
            """CREATE TABLE IF NOT EXISTS usage_records (
                workspace_id INTEGER NOT NULL,
                metric TEXT NOT NULL,
                period TEXT NOT NULL,
                amount INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(workspace_id, metric, period)
            )"""
        )
        connection.execute(
            """CREATE TABLE IF NOT EXISTS workspace_settings (
                workspace_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                value TEXT NOT NULL,
                PRIMARY KEY(workspace_id, name)
            )"""
        )
        add_column(connection, "workspace_id", "INTEGER")
        add_table_column(connection, "connections", "workspace_id", "INTEGER")
        add_table_column(connection, "sessions", "active_workspace_id", "INTEGER")
        add_table_column(connection, "social_oauth_states", "workspace_id", "INTEGER")
        add_table_column(connection, "audit_events", "workspace_id", "INTEGER")
        add_table_column(connection, "workspaces", "billing_customer_id", "TEXT")
        add_table_column(connection, "workspaces", "billing_subscription_id", "TEXT")
        connection.execute("CREATE INDEX IF NOT EXISTS posts_workspace ON posts(workspace_id, state)")
        connection.execute("CREATE INDEX IF NOT EXISTS connections_workspace ON connections(workspace_id, provider)")
        connection.execute("CREATE INDEX IF NOT EXISTS workspace_memberships_user ON workspace_memberships(user_id)")
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
        migrate_users_to_workspaces(connection)


def workspace_role_allows(role: object, minimum: str) -> bool:
    return WORKSPACE_ROLE_RANK.get(str(role or ""), -1) >= WORKSPACE_ROLE_RANK[minimum]


def workspace_slug(connection: Database, name: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", name.casefold()).strip("-")[:40] or "workspace"
    slug, counter = base, 1
    while connection.execute("SELECT 1 FROM workspaces WHERE slug = ?", (slug,)).fetchone():
        counter += 1
        slug = f"{base}-{counter}"
    return slug


def create_workspace(connection: Database, name: str, owner_user_id: int, plan: str | None = None) -> int:
    plan = plan or ("free" if deployment_mode() == "hosted" else "self_hosted")
    workspace_id = insert_id(
        connection,
        "INSERT INTO workspaces (name, slug, owner_user_id, plan, status, created_at, updated_at) VALUES (?, ?, ?, ?, 'active', ?, ?)",
        (name, workspace_slug(connection, name), owner_user_id, plan, now(), now()),
    )
    connection.execute(
        "INSERT INTO workspace_memberships (workspace_id, user_id, role, invite_state, created_at, updated_at) VALUES (?, ?, 'owner', 'active', ?, ?)",
        (workspace_id, owner_user_id, now(), now()),
    )
    return workspace_id


def workspace_membership(connection: Database, workspace_id: int, user_id: int) -> Record | None:
    return connection.execute(
        "SELECT workspace_memberships.*, workspaces.name AS workspace_name, workspaces.slug AS workspace_slug"
        " FROM workspace_memberships JOIN workspaces ON workspaces.id = workspace_memberships.workspace_id AND workspaces.status = 'active'"
        " WHERE workspace_memberships.workspace_id = ? AND workspace_memberships.user_id = ? AND workspace_memberships.invite_state = 'active'",
        (workspace_id, user_id),
    ).fetchone()


def user_workspaces(connection: Database, user_id: int) -> list[Record]:
    return connection.execute(
        "SELECT workspaces.id, workspaces.name, workspaces.slug, workspaces.owner_user_id, workspace_memberships.role"
        " FROM workspace_memberships JOIN workspaces ON workspaces.id = workspace_memberships.workspace_id AND workspaces.status = 'active'"
        " WHERE workspace_memberships.user_id = ? AND workspace_memberships.invite_state = 'active' ORDER BY workspace_memberships.id",
        (user_id,),
    ).fetchall()


def default_workspace_id(connection: Database, user_id: int) -> int | None:
    workspaces = user_workspaces(connection, user_id)
    return int(workspaces[0]["id"]) if workspaces else None


def ensure_personal_workspace(connection: Database, user_id: int, username: str) -> int:
    """Give a user one personal workspace and adopt their pre-workspace data into it."""
    existing = default_workspace_id(connection, user_id)
    if existing is not None:
        return existing
    workspace_id = create_workspace(connection, f"{username}'s workspace", user_id)
    connection.execute("UPDATE posts SET workspace_id = ? WHERE user_id = ? AND workspace_id IS NULL", (workspace_id, user_id))
    connection.execute("UPDATE connections SET workspace_id = ? WHERE user_id = ? AND workspace_id IS NULL", (workspace_id, user_id))
    connection.execute("UPDATE audit_events SET workspace_id = ? WHERE user_id = ? AND workspace_id IS NULL", (workspace_id, user_id))
    return workspace_id


def plan_limits(plan: str) -> dict[str, int] | None:
    """Resolve one plan's limits; None means unlimited."""
    limits: dict[str, dict[str, int] | None] = dict(PLAN_LIMITS)
    raw = config("SOSOPO_PLAN_LIMITS")
    if raw:
        try:
            override = json.loads(raw)
        except json.JSONDecodeError:
            LOGGER.warning("SOSOPO_PLAN_LIMITS is not valid JSON and was ignored")
            override = {}
        if isinstance(override, dict):
            for name, value in override.items():
                if value is None or isinstance(value, dict):
                    limits[str(name)] = value
    return limits.get(plan)


def current_period() -> str:
    return datetime.now(UTC).strftime("%Y-%m")


def record_usage(connection: Database, workspace_id: int, metric: str, amount: int = 1, period: str | None = None) -> None:
    period = period or current_period()
    updated = connection.execute(
        "UPDATE usage_records SET amount = amount + ?, updated_at = ? WHERE workspace_id = ? AND metric = ? AND period = ?",
        (amount, now(), workspace_id, metric, period),
    )
    if updated.rowcount == 0:
        connection.execute(
            "INSERT INTO usage_records (workspace_id, metric, period, amount, updated_at) VALUES (?, ?, ?, ?, ?)",
            (workspace_id, metric, period, amount, now()),
        )


def usage_amount(connection: Database, workspace_id: int, metric: str, period: str | None = None) -> int:
    row = connection.execute(
        "SELECT amount FROM usage_records WHERE workspace_id = ? AND metric = ? AND period = ?",
        (workspace_id, metric, period or current_period()),
    ).fetchone()
    return int(row["amount"]) if row else 0


def workspace_plan(connection: Database, workspace_id: int) -> str:
    row = connection.execute("SELECT plan FROM workspaces WHERE id = ?", (workspace_id,)).fetchone()
    return str(row["plan"]) if row else "self_hosted"


def workspace_setting(connection: Database, workspace_id: int, name: str) -> str | None:
    row = connection.execute("SELECT value FROM workspace_settings WHERE workspace_id = ? AND name = ?", (workspace_id, name)).fetchone()
    return str(row["value"]) if row else None


def save_workspace_setting(connection: Database, workspace_id: int, name: str, value: str | None) -> None:
    connection.execute("DELETE FROM workspace_settings WHERE workspace_id = ? AND name = ?", (workspace_id, name))
    if value is not None:
        connection.execute("INSERT INTO workspace_settings (workspace_id, name, value) VALUES (?, ?, ?)", (workspace_id, name, value))


def enforce_monthly_quota(connection: Database, workspace_id: int, metric: str, limit_name: str, label: str) -> None:
    limits = plan_limits(workspace_plan(connection, workspace_id))
    if limits is not None:
        limit = limits.get(limit_name)
        if limit is not None and usage_amount(connection, workspace_id, metric) >= int(limit):
            raise ProviderError(f"This workspace reached its monthly limit of {limit} {label}. Upgrade the plan or wait for the next month.", retryable=False)
    if metric.startswith("ai_"):
        cap = workspace_setting(connection, workspace_id, "ai_monthly_cap")
        if cap is not None:
            spent = usage_amount(connection, workspace_id, "ai_generations") + usage_amount(connection, workspace_id, "ai_media")
            if spent >= int(cap):
                raise ProviderError(f"This workspace reached its monthly AI budget cap of {cap} actions. A workspace owner can raise the cap in Team settings.", retryable=False)


def enforce_member_limit(connection: Database, workspace_id: int) -> None:
    limits = plan_limits(workspace_plan(connection, workspace_id))
    if limits is None or limits.get("members") is None:
        return
    count = int(connection.execute("SELECT COUNT(*) AS count FROM workspace_memberships WHERE workspace_id = ?", (workspace_id,)).fetchone()["count"])
    if count >= int(limits["members"]):
        raise ProviderError(f"This workspace plan allows {limits['members']} members. Upgrade the plan to add more.", retryable=False)


def enforce_connection_limit(connection: Database, workspace_id: int) -> None:
    limits = plan_limits(workspace_plan(connection, workspace_id))
    if limits is None or limits.get("connections") is None:
        return
    count = int(connection.execute("SELECT COUNT(*) AS count FROM connections WHERE workspace_id = ? AND is_active = 1", (workspace_id,)).fetchone()["count"])
    if count >= int(limits["connections"]):
        raise ProviderError(f"This workspace plan allows {limits['connections']} connected accounts. Upgrade the plan to add more.", retryable=False)


def enforce_storage_limit(connection: Database, workspace_id: int, additional_bytes: int) -> None:
    limits = plan_limits(workspace_plan(connection, workspace_id))
    if limits is None or limits.get("storage_mb") is None:
        return
    used = usage_amount(connection, workspace_id, "storage_bytes", period="total")
    if used + additional_bytes > int(limits["storage_mb"]) * 1024 * 1024:
        raise ProviderError(f"This workspace reached its {limits['storage_mb']} MB media storage limit. Upgrade the plan or remove media.", retryable=False)


def billing_enabled() -> bool:
    return deployment_mode() == "hosted" and bool(config("STRIPE_SECRET_KEY"))


def stripe_request(path: str, payload: dict[str, str]) -> dict[str, Any]:
    request = Request(
        f"https://api.stripe.com/v1/{path}",
        data=urlencode(payload).encode(),
        method="POST",
        headers={"Authorization": f"Bearer {config('STRIPE_SECRET_KEY')}", "Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urlopen(request, timeout=30) as response:
            result = json.loads(response.read() or b"{}")
    except HTTPError as error:
        LOGGER.error("Stripe request failed with status %s", error.code)
        raise ProviderError("The billing provider rejected the request. Check the billing configuration.", retryable=False) from error
    except (URLError, json.JSONDecodeError) as error:
        raise ProviderError("The billing provider could not be reached.") from error
    if not isinstance(result, dict):
        raise ProviderError("The billing provider returned an invalid response.")
    return result


def verify_stripe_signature(payload: bytes, header: str, secret: str) -> bool:
    """Verify a Stripe-Signature header (t=timestamp,v1=hmac) within tolerance."""
    timestamp, candidates = "", []
    for item in header.split(","):
        key, _, value = item.strip().partition("=")
        if key == "t":
            timestamp = value
        elif key == "v1":
            candidates.append(value)
    try:
        age = abs(datetime.now(UTC).timestamp() - int(timestamp))
    except ValueError:
        return False
    if age > STRIPE_WEBHOOK_TOLERANCE_SECONDS or not candidates:
        return False
    expected = hmac.new(secret.encode(), f"{timestamp}.".encode() + payload, hashlib.sha256).hexdigest()
    return any(hmac.compare_digest(expected, candidate) for candidate in candidates)


def apply_billing_event(event: dict[str, Any]) -> None:
    kind = str(event.get("type") or "")
    data = event.get("data", {}).get("object", {}) if isinstance(event.get("data"), dict) else {}
    if not isinstance(data, dict):
        return
    metadata = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
    try:
        workspace_id = int(metadata.get("workspace_id") or data.get("client_reference_id"))
    except (TypeError, ValueError):
        return
    with db() as connection:
        if kind == "checkout.session.completed":
            plan = str(metadata.get("plan") or "")
            if plan in STRIPE_PLAN_PRICE_VARIABLES:
                connection.execute(
                    "UPDATE workspaces SET plan = ?, billing_customer_id = ?, billing_subscription_id = ?, updated_at = ? WHERE id = ?",
                    (plan, str(data.get("customer") or ""), str(data.get("subscription") or ""), now(), workspace_id),
                )
        elif kind == "customer.subscription.deleted":
            connection.execute("UPDATE workspaces SET plan = 'free', billing_subscription_id = NULL, updated_at = ? WHERE id = ?", (now(), workspace_id))
    audit(None, f"billing.{kind}"[:100], "workspace", workspace_id, "Applied verified billing event", "stripe-webhook", workspace_id=workspace_id)


def deployment_mode() -> str:
    """self_hosted keeps today's defaults; hosted enables multi-customer behavior."""
    mode = (config("SOSOPO_DEPLOYMENT_MODE") or "self_hosted").strip().lower()
    return mode if mode in {"self_hosted", "hosted"} else "self_hosted"


def self_signup_allowed() -> bool:
    value = config("SOSOPO_ALLOW_SELF_SIGNUP").strip().lower()
    if value in {"1", "true", "yes"}:
        return True
    if value in {"0", "false", "no"}:
        return False
    return deployment_mode() == "hosted"


def create_local_user(connection: Database, username: str, password: str, role: str = "user", timezone: str = "UTC") -> int:
    salt = secrets.token_bytes(16)
    return insert_id(
        connection,
        "INSERT INTO users (username, password_salt, password_hash, role, timezone, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (username, salt.hex(), hash_password(password, salt), role, timezone, now()),
    )


def send_email(recipient: str, subject: str, body: str) -> bool:
    """Send one transactional email; return False when unconfigured or delivery fails."""
    host = config("SOSOPO_SMTP_HOST")
    if not host:
        return False
    import smtplib
    from email.message import EmailMessage
    message = EmailMessage()
    message["From"] = config("SOSOPO_SMTP_FROM") or config("SOSOPO_SMTP_USERNAME") or f"sosopo@{host}"
    message["To"] = recipient
    message["Subject"] = subject
    message.set_content(body)
    try:
        with smtplib.SMTP(host, int(config("SOSOPO_SMTP_PORT") or 587), timeout=15) as client:
            if config("SOSOPO_SMTP_STARTTLS").lower() not in {"0", "false", "no"}:
                client.starttls()
            username = config("SOSOPO_SMTP_USERNAME")
            if username:
                client.login(username, config("SOSOPO_SMTP_PASSWORD"))
            client.send_message(message)
        return True
    except (OSError, ValueError, smtplib.SMTPException):
        LOGGER.exception("Transactional email could not be sent")
        return False


def invitation_url(token: str) -> str:
    base = public_url()
    return f"{base}/invite?token={quote(token, safe='')}" if base else f"/invite?token={quote(token, safe='')}"


def invitation_by_token(connection: Database, token: str) -> Record | None:
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    return connection.execute(
        "SELECT workspace_invitations.*, workspaces.name AS workspace_name, workspaces.status AS workspace_status"
        " FROM workspace_invitations JOIN workspaces ON workspaces.id = workspace_invitations.workspace_id"
        " WHERE workspace_invitations.token_hash = ?",
        (token_hash,),
    ).fetchone()


def invitation_is_usable(invitation: Record | None) -> bool:
    return (
        invitation is not None
        and invitation["accepted_at"] is None
        and str(invitation["expires_at"]) > now()
        and invitation["workspace_status"] == "active"
    )


def migrate_users_to_workspaces(connection: Database) -> None:
    """Give every pre-workspace user an isolated personal workspace.

    Existing installations may hold several users whose posts and connections
    were private to each user. One workspace per user preserves exactly that
    isolation; merging everyone into a shared workspace would leak data.
    """
    users = connection.execute(
        "SELECT users.id, users.username FROM users LEFT JOIN workspace_memberships ON workspace_memberships.user_id = users.id"
        " WHERE workspace_memberships.id IS NULL ORDER BY users.id"
    ).fetchall()
    for user in users:
        ensure_personal_workspace(connection, int(user["id"]), str(user["username"]))


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


def stored_ai_provider_settings(provider: str, workspace_id: int | None = None) -> dict:
    definition = AI_PROVIDERS.get(provider)
    if definition is None:
        raise ProviderError("Choose a supported AI provider.", retryable=False)
    setting_name = f"ai_provider_{definition[0]}"
    with db() as connection:
        if workspace_id is None:
            row = connection.execute("SELECT value FROM instance_settings WHERE name = ?", (setting_name,)).fetchone()
        else:
            row = connection.execute("SELECT value FROM workspace_settings WHERE workspace_id = ? AND name = ?", (workspace_id, setting_name)).fetchone()
    return decrypt_secrets(row["value"]) if row else {}


def effective_ai_provider_stored(provider: str, workspace_id: int | None) -> tuple[dict, str]:
    """Prefer a workspace's own provider credential over the instance-wide one.

    A workspace configuration only takes effect once it holds its own API key;
    a cached model catalog alone must not shadow the instance credential.
    """
    if workspace_id is not None:
        workspace_stored = stored_ai_provider_settings(provider, workspace_id)
        if workspace_stored.get("api_key"):
            return workspace_stored, "workspace"
    return stored_ai_provider_settings(provider), "instance"


def save_ai_provider_settings(provider: str, settings: dict, workspace_id: int | None = None) -> None:
    """Save a provider configuration and its locally cached, reviewed model catalog."""
    definition = AI_PROVIDERS[provider]
    setting_name = f"ai_provider_{definition[0]}"
    with db() as connection:
        if workspace_id is None:
            exists = connection.execute("SELECT 1 FROM instance_settings WHERE name = ?", (setting_name,)).fetchone()
            if exists:
                connection.execute("UPDATE instance_settings SET value = ? WHERE name = ?", (encrypt_secrets(settings), setting_name))
            else:
                connection.execute("INSERT INTO instance_settings (name, value) VALUES (?, ?)", (setting_name, encrypt_secrets(settings)))
        else:
            save_workspace_setting(connection, workspace_id, setting_name, encrypt_secrets(settings))


def remove_ai_provider_settings(provider: str, workspace_id: int | None = None) -> bool:
    """Remove the UI-saved credential and local catalog for one provider."""
    definition = AI_PROVIDERS.get(provider)
    if definition is None:
        raise ProviderError("Choose a supported AI provider.", retryable=False)
    setting_name = f"ai_provider_{definition[0]}"
    with db() as connection:
        if workspace_id is None:
            return connection.execute("DELETE FROM instance_settings WHERE name = ?", (setting_name,)).rowcount == 1
        return connection.execute("DELETE FROM workspace_settings WHERE workspace_id = ? AND name = ?", (workspace_id, setting_name)).rowcount == 1


def ai_model_catalog(stored: dict) -> list[str]:
    raw = stored.get("models", "[]")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return []
    return [model for model in raw if isinstance(model, str) and model] if isinstance(raw, list) else []


def ai_provider_settings(provider: str, workspace_id: int | None = None) -> dict[str, str]:
    """Return only a configured, OpenAI-compatible text-generation provider."""
    definition = AI_PROVIDERS.get(provider)
    if definition is None:
        raise ProviderError("Choose a supported AI provider.", retryable=False)
    _, prefix, default_base = definition
    stored, source = effective_ai_provider_stored(provider, workspace_id)
    api_key = stored.get("api_key") or ("" if source == "workspace" else config(f"{prefix}_API_KEY"))
    base_url = stored.get("base_url") or config(f"{prefix}_BASE_URL") or default_base
    model = stored.get("model") or config(f"{prefix}_MODEL") or AI_PROVIDER_MODELS[provider][0]
    if not api_key or not base_url.startswith("https://") or not model:
        raise ProviderError(f"{provider} AI is not configured by this Sosopo administrator.", retryable=False)
    return {"name": provider, "api_key": api_key, "base_url": base_url.rstrip("/"), "model": model, "source": source}


def available_ai_providers(workspace_id: int | None = None) -> list[dict]:
    providers: list[dict] = []
    for name in AI_PROVIDERS:
        try:
            settings = ai_provider_settings(name, workspace_id)
            stored, _ = effective_ai_provider_stored(name, workspace_id)
            models = ai_model_catalog(stored) or AI_PROVIDER_MODELS[name]
            providers.append({"name": settings["name"], "model": settings["model"], "models": models, "source": settings["source"]})
        except ProviderError:
            pass
    return providers


def ai_provider_models(provider: str, workspace_id: int | None = None) -> list[str]:
    """Refresh one scope's model catalog using the provider's discovery endpoint."""
    stored = stored_ai_provider_settings(provider, workspace_id)
    # OpenRouter publishes its complete catalog without authentication, so make
    # it available in the selector before an administrator has saved a key.
    if provider == "OpenRouter":
        definition = AI_PROVIDERS[provider]
        settings = {"name": provider, "api_key": stored.get("api_key") or config(f"{definition[1]}_API_KEY"), "base_url": definition[2]}
    else:
        settings = ai_provider_settings(provider, workspace_id)
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
    save_ai_provider_settings(provider, stored, workspace_id)
    return models


def generate_post_copy(provider: str, model: str, instruction: str, draft: str, channels: list[str], workspace_id: int | None = None) -> str:
    settings = ai_provider_settings(provider, workspace_id)
    selected_model = model.strip() or settings["model"]
    stored, _ = effective_ai_provider_stored(provider, workspace_id)
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


def request_get_bytes(url: str, headers: dict[str, str] | None = None) -> bytes:
    """Download one generated media object with a hard size cap."""
    request = Request(url, headers=headers or {})
    try:
        with urlopen(request, timeout=120) as response:
            content = response.read(MAX_MEDIA_DOWNLOAD_BYTES + 1)
    except (HTTPError, URLError) as error:
        raise ProviderError("The generated media could not be downloaded from the provider.") from error
    if not content or len(content) > MAX_MEDIA_DOWNLOAD_BYTES:
        raise ProviderError("The generated media is empty or larger than the download limit.")
    return content


def default_media_model(provider: str, kind: str) -> str:
    models = (AI_PROVIDER_IMAGE_MODELS if kind == "image" else AI_PROVIDER_VIDEO_MODELS).get(provider) or []
    return models[0] if models else ""


def media_job_prompt(job: dict[str, Any]) -> str:
    style = str(job.get("style") or "").strip()
    prompt = str(job["prompt"]).strip()
    return f"{prompt}\n\nVisual style: {style}" if style else prompt


def store_generated_media(job: dict[str, Any], content: bytes, content_type: str, suffix: str) -> str:
    with db() as connection:
        enforce_storage_limit(connection, int(job["workspace_id"]), len(content))
        record_usage(connection, int(job["workspace_id"]), "storage_bytes", len(content), period="total")
    return store_media(f"{uuid.uuid4().hex}{suffix}", content_type, content)


def generate_image_media(job: dict[str, Any], settings: dict[str, str]) -> str:
    payload = {"model": job["model"] or default_media_model(job["provider"], "image"), "prompt": media_job_prompt(job), "size": MEDIA_IMAGE_SIZES.get(str(job["aspect_ratio"]), "1024x1024"), "n": 1}
    result = request_json(f"{settings['base_url']}/images/generations", payload, {"Authorization": f"Bearer {settings['api_key']}"})
    data = result.get("data")
    first = data[0] if isinstance(data, list) and data and isinstance(data[0], dict) else {}
    if first.get("b64_json"):
        try:
            content = base64.b64decode(str(first["b64_json"]), validate=True)
        except (binascii.Error, ValueError) as error:
            raise ProviderError("The AI provider returned unreadable image data.") from error
    elif first.get("url"):
        content = request_get_bytes(str(first["url"]))
    else:
        raise ProviderError("The AI provider did not return an image.")
    image_type = detected_image_type(content)
    if image_type is None:
        raise ProviderError("The AI provider returned an unsupported image format.")
    inspect_image(content, image_type)
    return store_generated_media(job, content, image_type, IMAGE_TYPES[image_type])


def generate_video_media(job: dict[str, Any], settings: dict[str, str]) -> str:
    """Run one provider-side asynchronous video job (OpenAI-style /videos API)."""
    headers = {"Authorization": f"Bearer {settings['api_key']}"}
    model = job["model"] or default_media_model(job["provider"], "video")
    creation = request_json(f"{settings['base_url']}/videos", {"model": model, "prompt": media_job_prompt(job), "size": MEDIA_VIDEO_SIZES.get(str(job["aspect_ratio"]), "1280x720")}, headers)
    video_id = str(creation.get("id") or "")
    if not video_id:
        raise ProviderError("The AI provider did not accept the video job.")
    for _ in range(VIDEO_POLL_LIMIT):
        time.sleep(VIDEO_POLL_SECONDS)
        remote = request_get_json(f"{settings['base_url']}/videos/{quote(video_id, safe='')}", headers)
        state = str(remote.get("status") or "")
        try:
            progress = min(max(int(remote.get("progress") or 0), 5), 99)
        except (TypeError, ValueError):
            progress = 50
        with db() as connection:
            connection.execute("UPDATE media_jobs SET progress = ?, updated_at = ? WHERE id = ?", (progress, now(), job["id"]))
        if state == "completed":
            content = request_get_bytes(f"{settings['base_url']}/videos/{quote(video_id, safe='')}/content", headers)
            return store_generated_media(job, content, "video/mp4", ".mp4")
        if state in {"failed", "cancelled", "expired"}:
            detail = remote.get("error", {}).get("message") if isinstance(remote.get("error"), dict) else ""
            raise ProviderError(f"The provider video job {state}: {detail or 'no detail returned'}"[:400])
    raise ProviderError("The provider video job did not finish in time.")


def claim_media_job() -> Record | None:
    with db() as connection:
        row = connection.execute("SELECT id FROM media_jobs WHERE status = 'queued' ORDER BY id LIMIT 1").fetchone()
        if row is None:
            return None
        claimed = connection.execute("UPDATE media_jobs SET status = 'running', progress = 5, updated_at = ? WHERE id = ? AND status = 'queued'", (now(), row["id"]))
        if claimed.rowcount != 1:
            return None
        return connection.execute("SELECT * FROM media_jobs WHERE id = ?", (row["id"],)).fetchone()


def run_media_job(job: dict[str, Any]) -> None:
    """Generate one media asset; on failure record the error and refund the credit."""
    try:
        settings = ai_provider_settings(str(job["provider"]), int(job["workspace_id"]))
        result_url = generate_image_media(job, settings) if job["kind"] == "image" else generate_video_media(job, settings)
    except ProviderError as error:
        detail = str(error)[:500]
    except Exception:
        LOGGER.exception("Media job %s failed unexpectedly", job["id"])
        detail = "The media job failed unexpectedly. Check the worker logs."
    else:
        with db() as connection:
            connection.execute("UPDATE media_jobs SET status = 'succeeded', progress = 100, result_url = ?, error = NULL, updated_at = ? WHERE id = ?", (result_url, now(), job["id"]))
        return
    with db() as connection:
        connection.execute("UPDATE media_jobs SET status = 'failed', error = ?, updated_at = ? WHERE id = ?", (detail, now(), job["id"]))
        record_usage(connection, int(job["workspace_id"]), "ai_media", -1)


def media_worker() -> None:
    """Process queued media jobs without blocking scheduled post delivery."""
    while True:
        try:
            job = claim_media_job()
            if job is not None:
                run_media_job(dict(job))
                continue
        except Exception:
            LOGGER.exception("Media job poll failed")
        time.sleep(POLL_SECONDS)


def audit(user_id: int | None, action: str, subject_type: str, subject_id: object | None, detail: str, source_ip: str, workspace_id: int | None = None) -> None:
    with db() as connection:
        connection.execute(
            "INSERT INTO audit_events (user_id, workspace_id, action, subject_type, subject_id, detail, source_ip, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (user_id, workspace_id, action, subject_type, str(subject_id) if subject_id is not None else None, detail[:500], source_ip[:100], now()),
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
        connection.execute("DELETE FROM workspace_invitations WHERE accepted_at IS NULL AND expires_at < ?", ((datetime.now(UTC) - timedelta(days=EXPIRED_INVITATION_RETENTION_DAYS)).isoformat(),))
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
    refresh_token = str(token.get("refresh_token") or "")
    if provider == "LinkedIn":
        profile = request_get_json("https://api.linkedin.com/v2/userinfo", {"Authorization": f"Bearer {access_token}"})
        subject = str(profile.get("sub") or "")
        if not subject:
            raise ProviderError("LinkedIn did not return a member profile.")
        author = subject if subject.startswith("urn:li:") else f"urn:li:person:{subject}"
        return [{"provider": "LinkedIn", "external_account_id": author, "display_name": str(profile.get("name") or profile.get("given_name") or subject), "access_token": access_token, "refresh_token": refresh_token, "token_expires_at": expiry or ""}]
    if provider == "Discord":
        webhook = token.get("webhook")
        if not isinstance(webhook, dict) or not webhook.get("id") or not webhook.get("token"):
            raise ProviderError("Discord did not return an approved channel webhook.")
        webhook_id, webhook_token = str(webhook["id"]), str(webhook["token"])
        return [{"provider": "Discord", "external_account_id": webhook_id, "display_name": str(webhook.get("name") or f"Discord channel {webhook.get('channel_id') or webhook_id}"), "access_token": f"https://discord.com/api/webhooks/{webhook_id}/{webhook_token}", "secret_name": "webhook_url", "token_expires_at": ""}]
    profile = request_get_json("https://api.x.com/2/users/me", {"Authorization": f"Bearer {access_token}"}).get("data", {})
    if not isinstance(profile, dict) or not profile.get("id"):
        raise ProviderError("X did not return a user profile.")
    return [{"provider": "X", "external_account_id": str(profile["id"]), "display_name": str(profile.get("username") or profile.get("name") or profile["id"]), "access_token": access_token, "refresh_token": refresh_token, "token_expires_at": expiry or ""}]


def save_social_connections(user_id: int, workspace_id: int, records: list[dict[str, str]]) -> int:
    saved = 0
    with db() as connection:
        if workspace_membership(connection, workspace_id, user_id) is None:
            raise ProviderError("You are no longer a member of the workspace this connection was started for.", retryable=False)
        for record in records:
            existing = connection.execute("SELECT id FROM connections WHERE workspace_id = ? AND provider = ? AND external_account_id = ?", (workspace_id, record["provider"], record["external_account_id"])).fetchone()
            secrets_map = {str(record.get("secret_name") or "access_token"): record["access_token"]}
            if record.get("refresh_token"):
                secrets_map["refresh_token"] = str(record["refresh_token"])
            encrypted = encrypt_secrets(secrets_map)
            expiry = record["token_expires_at"] or None
            if existing:
                connection.execute("UPDATE connections SET display_name = ?, encrypted_secrets = ?, token_expires_at = ?, is_active = 1 WHERE id = ?", (record["display_name"], encrypted, expiry, existing["id"]))
            else:
                conflicting = connection.execute("SELECT id FROM connections WHERE user_id = ? AND provider = ? AND external_account_id = ?", (user_id, record["provider"], record["external_account_id"])).fetchone()
                if conflicting:
                    raise ProviderError(f"The {record['provider']} account {record['display_name']} is already connected in another of your workspaces.", retryable=False)
                enforce_connection_limit(connection, workspace_id)
                connection.execute("INSERT INTO connections (user_id, workspace_id, provider, external_account_id, display_name, encrypted_secrets, settings_json, token_expires_at, created_at) VALUES (?, ?, ?, ?, ?, ?, '{}', ?, ?)", (user_id, workspace_id, record["provider"], record["external_account_id"], record["display_name"], encrypted, expiry, now()))
            saved += 1
    return saved


def refresh_connection_token(record: dict[str, Any]) -> bool:
    """Rotate one OAuth connection's token before it expires; True when rotated."""
    provider = str(record["provider"])
    try:
        stored = decrypt_secrets(record["encrypted_secrets"])
        if provider in {"X", "LinkedIn"}:
            refresh_token = stored.get("refresh_token", "")
            if not refresh_token:
                return False
            settings = social_oauth_settings(provider)
            token = request_form(settings["token"], {"grant_type": "refresh_token", "refresh_token": refresh_token, "client_id": settings["client_id"], "client_secret": settings["client_secret"]})
        elif provider == "Threads":
            access = stored.get("access_token", "")
            if not access:
                return False
            refresh_url = config("THREADS_REFRESH_URL") or "https://graph.threads.net/refresh_access_token"
            token = request_get_json(f"{refresh_url}?{urlencode({'grant_type': 'th_refresh_token', 'access_token': access})}")
        else:
            return False
    except ProviderError:
        LOGGER.warning("Could not refresh the %s token for connection %s", provider, record.get("id"))
        return False
    access_token = str(token.get("access_token") or "")
    if not access_token:
        return False
    updated = {**stored, "access_token": access_token}
    if token.get("refresh_token"):
        updated["refresh_token"] = str(token["refresh_token"])
    with db() as connection:
        connection.execute("UPDATE connections SET encrypted_secrets = ?, token_expires_at = ? WHERE id = ?", (encrypt_secrets(updated), social_token_expiry(token), record["id"]))
    audit(None, "connection.token_refreshed", "connection", record.get("id"), f"Automatically refreshed {provider} token", "worker", workspace_id=record.get("workspace_id"))
    return True


def refresh_expiring_connection_tokens() -> int:
    """Proactively refresh active OAuth tokens that expire within the horizon."""
    horizon = (datetime.now(UTC) + timedelta(hours=TOKEN_REFRESH_HORIZON_HOURS)).isoformat()
    with db() as connection:
        rows = connection.execute(
            "SELECT * FROM connections WHERE is_active = 1 AND token_expires_at IS NOT NULL AND token_expires_at <= ? AND provider IN ('X', 'LinkedIn', 'Threads')",
            (horizon,),
        ).fetchall()
    return sum(1 for row in rows if refresh_connection_token(dict(row)))


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
    threading.Thread(target=media_worker, daemon=True, name="media-worker").start()
    last_token_refresh = 0.0
    while True:
        try:
            worker_heartbeat()
            cleanup_expired_records()
            recover_stale_deliveries()
            if time.monotonic() - last_token_refresh >= TOKEN_REFRESH_INTERVAL_SECONDS:
                last_token_refresh = time.monotonic()
                refreshed = refresh_expiring_connection_tokens()
                if refreshed:
                    LOGGER.info("Refreshed %s expiring provider token(s)", refreshed)
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

    SESSION_QUERY = (
        "SELECT sessions.*, users.username, users.role, users.timezone, users.signature,"
        " workspaces.id AS workspace_id, workspaces.name AS workspace_name, workspaces.slug AS workspace_slug,"
        " workspace_memberships.role AS workspace_role"
        " FROM sessions JOIN users ON users.id = sessions.user_id"
        " LEFT JOIN workspace_memberships ON workspace_memberships.workspace_id = sessions.active_workspace_id"
        " AND workspace_memberships.user_id = sessions.user_id AND workspace_memberships.invite_state = 'active'"
        " LEFT JOIN workspaces ON workspaces.id = workspace_memberships.workspace_id AND workspaces.status = 'active'"
        " WHERE sessions.token_hash = ? AND sessions.expires_at > ? AND users.is_active = 1"
    )

    def _session(self) -> Record | None:
        cookie = SimpleCookie(self.headers.get("Cookie"))
        token = cookie.get("sosopo_session")
        if token is None:
            return None
        token_hash = hashlib.sha256(token.value.encode()).hexdigest()
        with db() as connection:
            session = connection.execute(self.SESSION_QUERY, (token_hash, now())).fetchone()
            if session is not None and session["workspace_id"] is None:
                # The active workspace vanished or the membership was revoked:
                # fall back to the user's first remaining workspace.
                fallback = default_workspace_id(connection, session["user_id"])
                if fallback is not None and fallback != session["active_workspace_id"]:
                    connection.execute("UPDATE sessions SET active_workspace_id = ? WHERE id = ?", (fallback, session["id"]))
                    session = connection.execute(self.SESSION_QUERY, (token_hash, now())).fetchone()
        return session

    def _require_workspace(self, session: Record, minimum_role: str = "viewer") -> int | None:
        if not session.get("workspace_id") or not session.get("workspace_role"):
            self._json({"error": "Join an active workspace to use this feature."}, HTTPStatus.FORBIDDEN)
            return None
        if not workspace_role_allows(session["workspace_role"], minimum_role):
            self._json({"error": f"This action needs the workspace {minimum_role} role or higher."}, HTTPStatus.FORBIDDEN)
            return None
        return int(session["workspace_id"])

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
                "INSERT INTO sessions (token_hash, csrf_token, user_id, active_workspace_id, expires_at, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (hashlib.sha256(token.encode()).hexdigest(), csrf_token, user_id, default_workspace_id(connection, user_id), expires_at(), now()),
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
            self._json({"setup_required": not has_user, "self_signup": self_signup_allowed() and has_user, "deployment_mode": deployment_mode()})
            return
        if path.startswith("/api/invitations/") and len(path.split("/")) == 4:
            if not self._allow("auth", AUTH_REQUESTS_PER_MINUTE):
                return
            token = unquote(path.split("/")[3])
            with db() as connection:
                invitation = invitation_by_token(connection, token)
            if not invitation_is_usable(invitation):
                self._json({"error": "This invitation is invalid, expired, or already used."}, HTTPStatus.NOT_FOUND); return
            self._json({"workspace": invitation["workspace_name"], "role": invitation["role"], "email": invitation["email"], "expires_at": invitation["expires_at"]})
            return
        if path == "/api/session":
            session = self._require_auth()
            if session:
                with db() as connection:
                    workspaces = [{"id": item["id"], "name": item["name"], "slug": item["slug"], "role": item["role"], "is_owner": item["owner_user_id"] == session["user_id"]} for item in user_workspaces(connection, session["user_id"])]
                workspace = {"id": session["workspace_id"], "name": session["workspace_name"], "slug": session["workspace_slug"], "role": session["workspace_role"]} if session.get("workspace_id") else None
                self._json({"username": session["username"], "role": session["role"], "timezone": session["timezone"], "signature": session["signature"], "workspace": workspace, "workspaces": workspaces, "csrf_token": session["csrf_token"]})
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
                    ensure_personal_workspace(connection, user_id, username)
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
                workspace_id = stored["workspace_id"]
                if workspace_id is None:
                    # A state row created before the workspace upgrade: use the
                    # connecting user's default workspace.
                    with db() as connection:
                        workspace_id = default_workspace_id(connection, stored["user_id"])
                if workspace_id is None:
                    self._json({"error": "Join an active workspace before connecting accounts."}, HTTPStatus.FORBIDDEN); return
                records = social_oauth_connections(provider, social_oauth_settings(provider), code, stored["code_verifier"])
                saved = save_social_connections(stored["user_id"], int(workspace_id), records)
                audit(stored["user_id"], "connection.oauth_connected", "connection", None, f"Connected {saved} account(s) through {provider} OAuth", self._source_ip(), workspace_id=int(workspace_id))
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
                session = self._session()
                workspace_id = self._require_workspace(session, "admin")
                if workspace_id is None:
                    return
                settings = social_oauth_settings(provider)
                state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(64)
                with db() as connection:
                    connection.execute("DELETE FROM social_oauth_states WHERE expires_at <= ?", (now(),))
                    connection.execute("INSERT INTO social_oauth_states (state, provider, user_id, workspace_id, code_verifier, expires_at) VALUES (?, ?, ?, ?, ?, ?)", (state, provider, session["user_id"], workspace_id, verifier if provider == "X" else None, (datetime.now(UTC) + timedelta(seconds=SOCIAL_OAUTH_STATE_SECONDS)).isoformat()))
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
            workspace_id = self._require_workspace(session)
            if workspace_id is None:
                return
            with db() as connection:
                posts = [dict(row) for row in connection.execute("SELECT * FROM posts WHERE workspace_id = ? ORDER BY CASE state WHEN 'scheduled' THEN 0 WHEN 'failed' THEN 1 ELSE 2 END, scheduled_for, id DESC", (workspace_id,)).fetchall()]
                for post in posts:
                    post["media_urls"] = [row["media_url"] for row in connection.execute("SELECT media_url FROM post_media WHERE post_id = ? ORDER BY position", (post["id"],)).fetchall()] or ([post["image_url"]] if post.get("image_url") else [])
            self._json({"posts": posts, "providers": [{"name": channel, "status": provider_status(channel), "oauth_available": social_oauth_enabled(channel)} for channel in CHANNELS]})
            return
        if path == "/api/ai/providers":
            session = self._session()
            self._json({"providers": available_ai_providers(session.get("workspace_id"))})
            return
        if path == "/api/workspaces/ai-providers":
            session = self._session()
            workspace_id = self._require_workspace(session, "admin")
            if workspace_id is None:
                return
            providers = []
            for name in AI_PROVIDERS:
                stored = stored_ai_provider_settings(name, workspace_id)
                catalog = ai_model_catalog(stored)
                instance_available = True
                try:
                    ai_provider_settings(name)
                except ProviderError:
                    instance_available = False
                providers.append({"name": name, "model": stored.get("model", AI_PROVIDER_MODELS[name][0]), "models": catalog or AI_PROVIDER_MODELS[name], "models_count": len(catalog), "models_checked_at": stored.get("models_checked_at"), "has_api_key": bool(stored.get("api_key")), "instance_fallback": instance_available})
            self._json({"providers": providers}); return
        if path.startswith("/api/workspaces/ai-providers/") and path.endswith("/models"):
            session = self._session()
            workspace_id = self._require_workspace(session, "admin")
            if workspace_id is None:
                return
            provider = unquote(path.split("/")[4])
            if provider not in AI_PROVIDERS:
                self._json({"error": "Choose a supported AI provider."}, HTTPStatus.BAD_REQUEST); return
            if provider != "OpenRouter" and not stored_ai_provider_settings(provider, workspace_id).get("api_key"):
                self._json({"error": "Save this workspace's API key for the provider before refreshing its models."}, HTTPStatus.BAD_REQUEST); return
            try:
                self._json({"models": ai_provider_models(provider, workspace_id)})
            except ProviderError as error:
                self._json({"error": str(error)}, HTTPStatus.BAD_GATEWAY)
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
            workspace_id = self._require_workspace(session)
            if workspace_id is None:
                return
            with db() as connection:
                records = [dict(row) for row in connection.execute("SELECT id, provider, external_account_id, display_name, token_expires_at, is_active, created_at FROM connections WHERE workspace_id = ? ORDER BY provider, display_name", (workspace_id,)).fetchall()]
            for record in records:
                record["health"] = connection_health(record)
            self._json({"connections": records})
            return
        if path == "/api/media/jobs":
            session = self._session()
            workspace_id = self._require_workspace(session)
            if workspace_id is None:
                return
            reviewer = workspace_role_allows(session["workspace_role"], "admin")
            with db() as connection:
                jobs = [dict(row) for row in connection.execute(
                    "SELECT media_jobs.id, media_jobs.kind, media_jobs.prompt, media_jobs.aspect_ratio, media_jobs.style, media_jobs.provider, media_jobs.model, media_jobs.status, media_jobs.progress, media_jobs.error, media_jobs.result_url, media_jobs.moderation, media_jobs.created_at, users.username AS created_by"
                    " FROM media_jobs JOIN users ON users.id = media_jobs.user_id WHERE media_jobs.workspace_id = ? ORDER BY media_jobs.id DESC LIMIT 100",
                    (workspace_id,),
                ).fetchall()]
            for job in jobs:
                if job["moderation"] != "approved" and not reviewer:
                    job["result_url"] = None
            self._json({"jobs": jobs})
            return
        if path == "/api/media/library":
            session = self._session()
            workspace_id = self._require_workspace(session)
            if workspace_id is None:
                return
            with db() as connection:
                assets = [dict(row) for row in connection.execute(
                    "SELECT id, kind, prompt, aspect_ratio, result_url, created_at FROM media_jobs WHERE workspace_id = ? AND status = 'succeeded' AND moderation = 'approved' ORDER BY id DESC LIMIT 200",
                    (workspace_id,),
                ).fetchall()]
            self._json({"assets": assets})
            return
        if path == "/api/workspaces/export":
            session = self._session()
            workspace_id = self._require_workspace(session, "admin")
            if workspace_id is None:
                return
            with db() as connection:
                workspace = dict(connection.execute("SELECT id, name, slug, plan, status, created_at FROM workspaces WHERE id = ?", (workspace_id,)).fetchone())
                members = [dict(row) for row in connection.execute("SELECT users.username, workspace_memberships.role, workspace_memberships.invite_state, workspace_memberships.created_at FROM workspace_memberships JOIN users ON users.id = workspace_memberships.user_id WHERE workspace_memberships.workspace_id = ?", (workspace_id,)).fetchall()]
                posts = [dict(row) for row in connection.execute("SELECT * FROM posts WHERE workspace_id = ? ORDER BY id", (workspace_id,)).fetchall()]
                for post in posts:
                    post["media_urls"] = [row["media_url"] for row in connection.execute("SELECT media_url FROM post_media WHERE post_id = ? ORDER BY position", (post["id"],)).fetchall()]
                accounts = [dict(row) for row in connection.execute("SELECT id, provider, external_account_id, display_name, token_expires_at, is_active, created_at FROM connections WHERE workspace_id = ? ORDER BY id", (workspace_id,)).fetchall()]
                deliveries = [dict(row) for row in connection.execute("SELECT deliveries.* FROM deliveries JOIN posts ON posts.id = deliveries.post_id WHERE posts.workspace_id = ? ORDER BY deliveries.id", (workspace_id,)).fetchall()]
            audit(session["user_id"], "workspace.exported", "workspace", workspace_id, "Exported workspace data", self._source_ip(), workspace_id=workspace_id)
            body = json.dumps({"exported_at": now(), "workspace": workspace, "members": members, "posts": posts, "connections": accounts, "deliveries": deliveries}, indent=2).encode()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Disposition", f"attachment; filename=\"sosopo-{workspace['slug']}-export.json\"")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/api/workspaces":
            session = self._session()
            with db() as connection:
                workspaces = [{"id": item["id"], "name": item["name"], "slug": item["slug"], "role": item["role"], "is_owner": item["owner_user_id"] == session["user_id"]} for item in user_workspaces(connection, session["user_id"])]
            self._json({"workspaces": workspaces, "active_workspace_id": session.get("workspace_id")})
            return
        if path == "/api/workspaces/invitations":
            session = self._session()
            workspace_id = self._require_workspace(session, "admin")
            if workspace_id is None:
                return
            with db() as connection:
                invitations = [dict(row) for row in connection.execute(
                    "SELECT id, email, role, expires_at, created_at FROM workspace_invitations WHERE workspace_id = ? AND accepted_at IS NULL ORDER BY id DESC",
                    (workspace_id,),
                ).fetchall()]
            for invitation in invitations:
                invitation["expired"] = str(invitation["expires_at"]) <= now()
            self._json({"invitations": invitations})
            return
        if path == "/api/workspaces/members":
            session = self._session()
            workspace_id = self._require_workspace(session, "admin")
            if workspace_id is None:
                return
            with db() as connection:
                members = [dict(row) for row in connection.execute(
                    "SELECT workspace_memberships.user_id, workspace_memberships.role, workspace_memberships.invite_state, workspace_memberships.created_at, users.username, users.is_active"
                    " FROM workspace_memberships JOIN users ON users.id = workspace_memberships.user_id WHERE workspace_memberships.workspace_id = ? ORDER BY workspace_memberships.id",
                    (workspace_id,),
                ).fetchall()]
            self._json({"members": members})
            return
        if path.startswith("/api/posts/") and path.endswith("/deliveries"):
            try:
                post_id = int(path.split("/")[3])
            except ValueError:
                self._json({"error": "Invalid post ID."}, HTTPStatus.BAD_REQUEST); return
            session = self._session()
            workspace_id = self._require_workspace(session)
            if workspace_id is None:
                return
            with db() as connection:
                owner = connection.execute("SELECT id FROM posts WHERE id = ? AND workspace_id = ?", (post_id, workspace_id)).fetchone()
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
            self.send_header("Content-Type", next((kind for kind, suffix in {**IMAGE_TYPES, "video/mp4": ".mp4"}.items() if suffix == upload.suffix), "application/octet-stream"))
            self.send_header("Content-Length", str(upload.stat().st_size))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            with upload.open("rb") as file:
                self.copyfile(file, self.wfile)
            return
        if self.path == "/" or path == "/invite":
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

    def _handle_billing_webhook(self) -> None:
        """Verify and apply a Stripe webhook using the raw request body."""
        secret = config("STRIPE_WEBHOOK_SECRET")
        size = int(self.headers.get("Content-Length", "0") or 0)
        if not secret or size <= 0 or size > 1_000_000:
            self._json({"error": "Billing webhooks are not configured."}, HTTPStatus.NOT_FOUND)
            return
        raw = self.rfile.read(size)
        if not verify_stripe_signature(raw, self.headers.get("Stripe-Signature", ""), secret):
            self._json({"error": "Invalid webhook signature."}, HTTPStatus.BAD_REQUEST)
            return
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            self._json({"error": "Invalid webhook payload."}, HTTPStatus.BAD_REQUEST)
            return
        if isinstance(event, dict):
            apply_billing_event(event)
        self._json({"received": True})

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == "/api/billing/webhook":
            self._handle_billing_webhook()
            return
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
                    workspace_id = ensure_personal_workspace(connection, user_id, username)
                    connection.execute("UPDATE posts SET user_id = ?, workspace_id = ? WHERE user_id IS NULL", (user_id, workspace_id))
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
            if path == "/api/signup":
                if not self._allow("auth", AUTH_REQUESTS_PER_MINUTE): return
                if not self_signup_allowed():
                    self._json({"error": "Self-service signup is disabled on this Sosopo instance."}, HTTPStatus.FORBIDDEN); return
                username, password = str(payload.get("username", "")).strip(), str(payload.get("password", ""))
                if len(username) < 3 or len(password) < 12:
                    self._json({"error": "Use a username of at least 3 characters and a password of at least 12 characters."}, HTTPStatus.BAD_REQUEST); return
                with db() as connection:
                    if connection.execute("SELECT 1 FROM users LIMIT 1").fetchone() is None:
                        self._json({"error": "Complete first-run setup before self-service signup."}, HTTPStatus.CONFLICT); return
                    if connection.execute("SELECT 1 FROM users WHERE username = ?", (username,)).fetchone():
                        self._json({"error": "That username is already taken."}, HTTPStatus.CONFLICT); return
                    user_id = create_local_user(connection, username, password)
                    ensure_personal_workspace(connection, user_id, username)
                audit(user_id, "user.signed_up", "user", user_id, "Self-service signup", self._source_ip())
                self._json_session({"username": username}, self._create_session(user_id), HTTPStatus.CREATED); return
            if path.startswith("/api/invitations/") and path.endswith("/accept"):
                if not self._allow("auth", AUTH_REQUESTS_PER_MINUTE): return
                token = unquote(path.split("/")[3])
                session = self._session()
                with db() as connection:
                    invitation = invitation_by_token(connection, token)
                    if not invitation_is_usable(invitation):
                        self._json({"error": "This invitation is invalid, expired, or already used."}, HTTPStatus.BAD_REQUEST); return
                    if session is not None:
                        user_id, username = int(session["user_id"]), str(session["username"])
                    else:
                        username, password = str(payload.get("username", "")).strip(), str(payload.get("password", ""))
                        if len(username) < 3 or len(password) < 12:
                            self._json({"error": "Use a username of at least 3 characters and a password of at least 12 characters."}, HTTPStatus.BAD_REQUEST); return
                        if connection.execute("SELECT 1 FROM users WHERE username = ?", (username,)).fetchone():
                            self._json({"error": "That username is already taken. Sign in first, then open the invite link again."}, HTTPStatus.CONFLICT); return
                        user_id = create_local_user(connection, username, password)
                    workspace_id = int(invitation["workspace_id"])
                    if connection.execute("SELECT 1 FROM workspace_memberships WHERE workspace_id = ? AND user_id = ?", (workspace_id, user_id)).fetchone() is None:
                        enforce_member_limit(connection, workspace_id)
                        connection.execute(
                            "INSERT INTO workspace_memberships (workspace_id, user_id, role, invite_state, created_at, updated_at) VALUES (?, ?, ?, 'active', ?, ?)",
                            (workspace_id, user_id, invitation["role"], now(), now()),
                        )
                    connection.execute("UPDATE workspace_invitations SET accepted_at = ?, accepted_user_id = ? WHERE id = ?", (now(), user_id, invitation["id"]))
                    if session is not None:
                        connection.execute("UPDATE sessions SET active_workspace_id = ? WHERE id = ?", (workspace_id, session["id"]))
                audit(user_id, "workspace.invitation_accepted", "workspace", invitation["workspace_id"], f"{username} accepted a {invitation['role']} invitation", self._source_ip(), workspace_id=int(invitation["workspace_id"]))
                if session is None:
                    self._json_session({"username": username, "workspace": invitation["workspace_name"]}, self._create_session(user_id), HTTPStatus.CREATED)
                else:
                    self._json({"username": username, "workspace": invitation["workspace_name"]})
                return
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
                workspace_id = self._require_workspace(session, "editor")
                if workspace_id is None:
                    return
                provider = str(payload.get("provider", ""))
                model = str(payload.get("model", ""))
                instruction = str(payload.get("instruction", ""))
                draft = str(payload.get("draft", ""))
                channels = payload.get("channels", [])
                if not isinstance(channels, list) or any(str(channel) not in CHANNELS for channel in channels):
                    self._json({"error": "Choose valid post platforms for AI generation."}, HTTPStatus.BAD_REQUEST); return
                with db() as connection:
                    enforce_monthly_quota(connection, workspace_id, "ai_generations", "ai_generations_per_month", "AI text generations")
                try:
                    copy = generate_post_copy(provider, model, instruction, draft, [str(channel) for channel in channels], workspace_id)
                except ProviderError as error:
                    self._json({"error": str(error)}, HTTPStatus.BAD_GATEWAY); return
                with db() as connection:
                    record_usage(connection, workspace_id, "ai_generations")
                audit(session["user_id"], "post.ai_generated", "user", session["user_id"], f"Generated post copy with {provider}", self._source_ip(), workspace_id=workspace_id)
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
            if path == "/api/workspaces":
                name = str(payload.get("name", "")).strip()
                if not name or len(name) > MAX_WORKSPACE_NAME_LENGTH:
                    self._json({"error": f"Use a workspace name of 1 to {MAX_WORKSPACE_NAME_LENGTH} characters."}, HTTPStatus.BAD_REQUEST); return
                with db() as connection:
                    workspace_id = create_workspace(connection, name, session["user_id"])
                    connection.execute("UPDATE sessions SET active_workspace_id = ? WHERE id = ?", (workspace_id, session["id"]))
                audit(session["user_id"], "workspace.created", "workspace", workspace_id, f"Created workspace {name}", self._source_ip(), workspace_id=workspace_id)
                self._json({"id": workspace_id, "name": name, "role": "owner"}, HTTPStatus.CREATED); return
            if path == "/api/me/workspace":
                try:
                    workspace_id = int(payload.get("workspace_id"))
                except (TypeError, ValueError):
                    self._json({"error": "workspace_id must be a workspace number."}, HTTPStatus.BAD_REQUEST); return
                with db() as connection:
                    membership = workspace_membership(connection, workspace_id, session["user_id"])
                    if membership is None:
                        self._json({"error": "You are not a member of that workspace."}, HTTPStatus.FORBIDDEN); return
                    connection.execute("UPDATE sessions SET active_workspace_id = ? WHERE id = ?", (workspace_id, session["id"]))
                self._json({"workspace": {"id": workspace_id, "name": membership["workspace_name"], "slug": membership["workspace_slug"], "role": membership["role"]}}); return
            if path == "/api/workspaces/members":
                workspace_id = self._require_workspace(session, "admin")
                if workspace_id is None:
                    return
                username, member_role = str(payload.get("username", "")).strip(), str(payload.get("role", "editor")).strip()
                if member_role not in {"viewer", "editor", "admin"}:
                    self._json({"error": "Grant the viewer, editor, or admin role."}, HTTPStatus.BAD_REQUEST); return
                if member_role == "admin" and session["workspace_role"] != "owner":
                    self._json({"error": "Only the workspace owner can grant the admin role."}, HTTPStatus.FORBIDDEN); return
                with db() as connection:
                    user = connection.execute("SELECT id, username FROM users WHERE username = ? AND is_active = 1", (username,)).fetchone()
                    if user is None:
                        self._json({"error": "No active user has that username. An administrator can create the account first."}, HTTPStatus.NOT_FOUND); return
                    if connection.execute("SELECT id FROM workspace_memberships WHERE workspace_id = ? AND user_id = ?", (workspace_id, user["id"])).fetchone():
                        self._json({"error": "That user is already a member of this workspace."}, HTTPStatus.CONFLICT); return
                    enforce_member_limit(connection, workspace_id)
                    connection.execute("INSERT INTO workspace_memberships (workspace_id, user_id, role, invite_state, created_at, updated_at) VALUES (?, ?, ?, 'active', ?, ?)", (workspace_id, user["id"], member_role, now(), now()))
                audit(session["user_id"], "workspace.member_added", "user", user["id"], f"Added {username} as workspace {member_role}", self._source_ip(), workspace_id=workspace_id)
                self._json({"user_id": user["id"], "username": user["username"], "role": member_role}, HTTPStatus.CREATED); return
            if path.startswith("/api/workspaces/members/") and path.endswith("/role"):
                workspace_id = self._require_workspace(session, "admin")
                if workspace_id is None:
                    return
                member_user_id, member_role = int(path.split("/")[4]), str(payload.get("role", "")).strip()
                if member_role not in {"viewer", "editor", "admin"}:
                    self._json({"error": "Grant the viewer, editor, or admin role."}, HTTPStatus.BAD_REQUEST); return
                if member_user_id == session["user_id"]:
                    self._json({"error": "You cannot change your own workspace role."}, HTTPStatus.BAD_REQUEST); return
                with db() as connection:
                    membership = connection.execute("SELECT id, role FROM workspace_memberships WHERE workspace_id = ? AND user_id = ?", (workspace_id, member_user_id)).fetchone()
                    if membership is None:
                        self._json({"error": "That user is not a member of this workspace."}, HTTPStatus.NOT_FOUND); return
                    if membership["role"] == "owner":
                        self._json({"error": "The workspace owner's role cannot be changed."}, HTTPStatus.BAD_REQUEST); return
                    if session["workspace_role"] != "owner" and (membership["role"] == "admin" or member_role == "admin"):
                        self._json({"error": "Only the workspace owner can change admin memberships."}, HTTPStatus.FORBIDDEN); return
                    connection.execute("UPDATE workspace_memberships SET role = ?, updated_at = ? WHERE id = ?", (member_role, now(), membership["id"]))
                audit(session["user_id"], "workspace.member_role_changed", "user", member_user_id, f"Changed workspace role to {member_role}", self._source_ip(), workspace_id=workspace_id)
                self._json({"user_id": member_user_id, "role": member_role}); return
            if path.startswith("/api/workspaces/members/") and path.endswith("/remove"):
                workspace_id = self._require_workspace(session, "admin")
                if workspace_id is None:
                    return
                member_user_id = int(path.split("/")[4])
                if member_user_id == session["user_id"]:
                    self._json({"error": "You cannot remove yourself from a workspace."}, HTTPStatus.BAD_REQUEST); return
                with db() as connection:
                    membership = connection.execute("SELECT id, role FROM workspace_memberships WHERE workspace_id = ? AND user_id = ?", (workspace_id, member_user_id)).fetchone()
                    if membership is None:
                        self._json({"error": "That user is not a member of this workspace."}, HTTPStatus.NOT_FOUND); return
                    if membership["role"] == "owner":
                        self._json({"error": "The workspace owner cannot be removed."}, HTTPStatus.BAD_REQUEST); return
                    if session["workspace_role"] != "owner" and membership["role"] == "admin":
                        self._json({"error": "Only the workspace owner can remove an admin."}, HTTPStatus.FORBIDDEN); return
                    connection.execute("DELETE FROM workspace_memberships WHERE id = ?", (membership["id"],))
                audit(session["user_id"], "workspace.member_removed", "user", member_user_id, "Removed workspace member", self._source_ip(), workspace_id=workspace_id)
                self._json({"status": "removed"}); return
            if path == "/api/media/jobs":
                workspace_id = self._require_workspace(session, "editor")
                if workspace_id is None:
                    return
                kind = str(payload.get("kind", "image")).strip()
                prompt = str(payload.get("prompt", "")).strip()
                aspect = str(payload.get("aspect_ratio", "1:1")).strip()
                style = str(payload.get("style", "")).strip()
                provider = str(payload.get("provider", "")).strip()
                model = str(payload.get("model", "")).strip()
                if kind not in MEDIA_JOB_KINDS:
                    self._json({"error": "Choose image or video generation."}, HTTPStatus.BAD_REQUEST); return
                if not prompt or len(prompt) > MAX_MEDIA_PROMPT_LENGTH or len(style) > MAX_MEDIA_STYLE_LENGTH or len(model) > 200:
                    self._json({"error": f"Provide a prompt up to {MAX_MEDIA_PROMPT_LENGTH} characters and a style up to {MAX_MEDIA_STYLE_LENGTH}."}, HTTPStatus.BAD_REQUEST); return
                sizes = MEDIA_IMAGE_SIZES if kind == "image" else MEDIA_VIDEO_SIZES
                if aspect not in sizes:
                    self._json({"error": f"Choose an aspect ratio from: {', '.join(sizes)}."}, HTTPStatus.BAD_REQUEST); return
                if not provider:
                    configured = available_ai_providers(workspace_id)
                    provider = configured[0]["name"] if configured else ""
                try:
                    ai_provider_settings(provider, workspace_id)
                except ProviderError as error:
                    self._json({"error": str(error)}, HTTPStatus.BAD_REQUEST); return
                if not model and not default_media_model(provider, kind):
                    self._json({"error": f"{provider} has no supported {kind} model in Sosopo. Choose another provider or set a model explicitly."}, HTTPStatus.BAD_REQUEST); return
                with db() as connection:
                    enforce_monthly_quota(connection, workspace_id, "ai_media", "ai_media_per_month", "AI media generations")
                    record_usage(connection, workspace_id, "ai_media")
                    job_id = insert_id(connection,
                        "INSERT INTO media_jobs (workspace_id, user_id, kind, prompt, aspect_ratio, style, provider, model, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?)",
                        (workspace_id, session["user_id"], kind, prompt, aspect, style, provider, model, now(), now()),
                    )
                audit(session["user_id"], "media.job_created", "media_job", job_id, f"Queued {kind} generation with {provider}", self._source_ip(), workspace_id=workspace_id)
                self._json({"id": job_id, "status": "queued", "provider": provider}, HTTPStatus.CREATED); return
            if path.startswith("/api/media/jobs/") and path.endswith("/review"):
                workspace_id = self._require_workspace(session, "admin")
                if workspace_id is None:
                    return
                job_id = int(path.split("/")[4])
                decision = str(payload.get("decision", "")).strip()
                if decision not in {"approved", "rejected"}:
                    self._json({"error": "Choose approved or rejected."}, HTTPStatus.BAD_REQUEST); return
                with db() as connection:
                    changed = connection.execute("UPDATE media_jobs SET moderation = ?, reviewed_by = ?, updated_at = ? WHERE id = ? AND workspace_id = ? AND status = 'succeeded'", (decision, session["user_id"], now(), job_id, workspace_id))
                if changed.rowcount != 1:
                    self._json({"error": "Only this workspace's finished media jobs can be reviewed."}, HTTPStatus.NOT_FOUND); return
                audit(session["user_id"], "media.reviewed", "media_job", job_id, f"Marked generated media {decision}", self._source_ip(), workspace_id=workspace_id)
                self._json({"id": job_id, "moderation": decision}); return
            if path == "/api/workspaces/ai-providers":
                workspace_id = self._require_workspace(session, "admin")
                if workspace_id is None:
                    return
                provider = str(payload.get("provider", "")).strip()
                if provider not in AI_PROVIDERS:
                    self._json({"error": "Choose a supported AI provider."}, HTTPStatus.BAD_REQUEST); return
                current = stored_ai_provider_settings(provider, workspace_id)
                model = str(payload.get("model", "")).strip() or current.get("model") or AI_PROVIDER_MODELS[provider][0]
                api_key = str(payload.get("api_key", "")).strip()
                if len(model) > 200:
                    self._json({"error": "Choose a model."}, HTTPStatus.BAD_REQUEST); return
                if not api_key and not current.get("api_key"):
                    self._json({"error": "Provide this workspace's API key for the provider."}, HTTPStatus.BAD_REQUEST); return
                catalog = ai_model_catalog(current) or AI_PROVIDER_MODELS[provider]
                if model not in catalog:
                    catalog = [model, *catalog]
                stored = {"api_key": api_key or current["api_key"], "base_url": AI_PROVIDERS[provider][2], "model": model, "models": json.dumps(catalog)}
                if current.get("models_checked_at"):
                    stored["models_checked_at"] = current["models_checked_at"]
                save_ai_provider_settings(provider, stored, workspace_id)
                audit(session["user_id"], "ai_provider.workspace_saved", "workspace", workspace_id, f"Configured workspace {provider} AI provider", self._source_ip(), workspace_id=workspace_id)
                self._json({"name": provider, "model": model, "has_api_key": True}); return
            if path.startswith("/api/workspaces/ai-providers/") and path.endswith("/remove"):
                workspace_id = self._require_workspace(session, "admin")
                if workspace_id is None:
                    return
                provider = unquote(path.split("/")[4])
                if provider not in AI_PROVIDERS:
                    self._json({"error": "Choose a supported AI provider."}, HTTPStatus.BAD_REQUEST); return
                removed = remove_ai_provider_settings(provider, workspace_id)
                audit(session["user_id"], "ai_provider.workspace_removed", "workspace", workspace_id, f"Removed workspace {provider} API key", self._source_ip(), workspace_id=workspace_id)
                self._json({"status": "removed" if removed else "not configured", "name": provider}); return
            if path == "/api/workspaces/settings":
                workspace_id = self._require_workspace(session, "owner")
                if workspace_id is None:
                    return
                cap = payload.get("ai_monthly_cap")
                if cap is not None and (not isinstance(cap, int) or isinstance(cap, bool) or cap < 0 or cap > 1_000_000):
                    self._json({"error": "ai_monthly_cap must be a whole number of AI actions, or null to remove the cap."}, HTTPStatus.BAD_REQUEST); return
                with db() as connection:
                    save_workspace_setting(connection, workspace_id, "ai_monthly_cap", str(cap) if cap is not None else None)
                audit(session["user_id"], "workspace.settings_changed", "workspace", workspace_id, f"Set monthly AI cap to {cap}", self._source_ip(), workspace_id=workspace_id)
                self._json({"ai_monthly_cap": cap}); return
            if path == "/api/workspaces/billing/checkout":
                workspace_id = self._require_workspace(session, "owner")
                if workspace_id is None:
                    return
                if not billing_enabled():
                    self._json({"error": "Billing is not configured on this Sosopo deployment."}, HTTPStatus.SERVICE_UNAVAILABLE); return
                plan = str(payload.get("plan", "")).strip()
                price = config(STRIPE_PLAN_PRICE_VARIABLES.get(plan, ""))
                if plan not in STRIPE_PLAN_PRICE_VARIABLES or not price:
                    self._json({"error": "Choose a purchasable plan."}, HTTPStatus.BAD_REQUEST); return
                base = public_url()
                if not base.startswith("https://"):
                    self._json({"error": "Billing requires SOSOPO_PUBLIC_URL with a public HTTPS URL."}, HTTPStatus.BAD_REQUEST); return
                checkout = stripe_request("checkout/sessions", {
                    "mode": "subscription",
                    "line_items[0][price]": price,
                    "line_items[0][quantity]": "1",
                    "success_url": f"{base}/?billing=success",
                    "cancel_url": f"{base}/?billing=cancelled",
                    "client_reference_id": str(workspace_id),
                    "metadata[workspace_id]": str(workspace_id),
                    "metadata[plan]": plan,
                    "subscription_data[metadata][workspace_id]": str(workspace_id),
                    "subscription_data[metadata][plan]": plan,
                })
                if not checkout.get("url"):
                    self._json({"error": "The billing provider did not return a checkout URL."}, HTTPStatus.BAD_GATEWAY); return
                audit(session["user_id"], "billing.checkout_started", "workspace", workspace_id, f"Started {plan} checkout", self._source_ip(), workspace_id=workspace_id)
                self._json({"url": str(checkout["url"])}); return
            if path == "/api/workspaces/delete":
                workspace_id = self._require_workspace(session, "owner")
                if workspace_id is None:
                    return
                with db() as connection:
                    if len(user_workspaces(connection, session["user_id"])) < 2:
                        self._json({"error": "Create or join another workspace before deleting your only workspace."}, HTTPStatus.BAD_REQUEST); return
                    connection.execute("UPDATE workspaces SET status = 'deleted', updated_at = ? WHERE id = ?", (now(), workspace_id))
                    connection.execute("UPDATE connections SET is_active = 0 WHERE workspace_id = ?", (workspace_id,))
                    # Stop the worker from delivering queued content for a deleted tenant.
                    connection.execute("UPDATE posts SET state = 'draft', scheduled_for = NULL WHERE workspace_id = ? AND state = 'scheduled'", (workspace_id,))
                audit(session["user_id"], "workspace.deleted", "workspace", workspace_id, "Soft-deleted workspace, disabled its connections, and unscheduled queued posts", self._source_ip(), workspace_id=workspace_id)
                self._json({"status": "deleted"}); return
            if path == "/api/workspaces/invitations":
                workspace_id = self._require_workspace(session, "admin")
                if workspace_id is None:
                    return
                email, invite_role = str(payload.get("email", "")).strip().lower(), str(payload.get("role", "editor")).strip()
                if len(email) > 254 or not EMAIL_PATTERN.fullmatch(email):
                    self._json({"error": "Enter a valid invitation email address."}, HTTPStatus.BAD_REQUEST); return
                if invite_role not in {"viewer", "editor", "admin"}:
                    self._json({"error": "Grant the viewer, editor, or admin role."}, HTTPStatus.BAD_REQUEST); return
                if invite_role == "admin" and session["workspace_role"] != "owner":
                    self._json({"error": "Only the workspace owner can grant the admin role."}, HTTPStatus.FORBIDDEN); return
                token = secrets.token_urlsafe(32)
                expires = (datetime.now(UTC) + timedelta(seconds=INVITATION_SECONDS)).isoformat()
                with db() as connection:
                    connection.execute("DELETE FROM workspace_invitations WHERE workspace_id = ? AND email = ? AND accepted_at IS NULL", (workspace_id, email))
                    invitation_id = insert_id(
                        connection,
                        "INSERT INTO workspace_invitations (workspace_id, email, role, token_hash, invited_by, expires_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (workspace_id, email, invite_role, hashlib.sha256(token.encode()).hexdigest(), session["user_id"], expires, now()),
                    )
                link = invitation_url(token)
                email_sent = send_email(
                    email,
                    f"You are invited to the {session['workspace_name']} workspace on Sosopo",
                    f"{session['username']} invited you to join the {session['workspace_name']} workspace as {invite_role}.\n\nOpen this link to accept (valid for 7 days):\n{link}\n\nIf you did not expect this invitation, ignore this email.",
                )
                audit(session["user_id"], "workspace.invitation_created", "workspace", workspace_id, f"Invited {email} as {invite_role}", self._source_ip(), workspace_id=workspace_id)
                self._json({"id": invitation_id, "email": email, "role": invite_role, "expires_at": expires, "invite_url": link, "email_sent": email_sent}, HTTPStatus.CREATED); return
            if path.startswith("/api/workspaces/invitations/") and path.endswith("/revoke"):
                workspace_id = self._require_workspace(session, "admin")
                if workspace_id is None:
                    return
                invitation_id = int(path.split("/")[4])
                with db() as connection:
                    removed = connection.execute("DELETE FROM workspace_invitations WHERE id = ? AND workspace_id = ? AND accepted_at IS NULL", (invitation_id, workspace_id))
                if removed.rowcount != 1:
                    self._json({"error": "Invitation not found."}, HTTPStatus.NOT_FOUND); return
                audit(session["user_id"], "workspace.invitation_revoked", "workspace", invitation_id, "Revoked pending invitation", self._source_ip(), workspace_id=workspace_id)
                self._json({"status": "revoked"}); return
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
                    ensure_personal_workspace(connection, user_id, username)
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
                workspace_id = self._require_workspace(session, "admin")
                if workspace_id is None:
                    return
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
                    duplicate = connection.execute("SELECT id FROM connections WHERE (workspace_id = ? OR user_id = ?) AND provider = ? AND external_account_id = ?", (workspace_id, session["user_id"], provider, account_id)).fetchone()
                    if duplicate:
                        self._json({"error": "This provider account is already connected in this workspace or another of your workspaces."}, HTTPStatus.CONFLICT); return
                    enforce_connection_limit(connection, workspace_id)
                    connection_id = insert_id(connection,
                        "INSERT INTO connections (user_id, workspace_id, provider, external_account_id, display_name, encrypted_secrets, settings_json, token_expires_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (session["user_id"], workspace_id, provider, account_id, display_name, encrypt_secrets(secrets_to_store), json.dumps(settings), token_expiry, now()),
                    )
                audit(session["user_id"], "connection.created", "connection", connection_id, f"Created {provider} connection {display_name}", self._source_ip(), workspace_id=workspace_id)
                self._json({"id": connection_id, "provider": provider, "external_account_id": account_id, "display_name": display_name}, HTTPStatus.CREATED); return
            if path.startswith("/api/connections/") and path.endswith("/disable"):
                workspace_id = self._require_workspace(session, "admin")
                if workspace_id is None:
                    return
                connection_id = int(path.split("/")[3])
                with db() as connection:
                    changed = connection.execute("UPDATE connections SET is_active = 0 WHERE id = ? AND workspace_id = ?", (connection_id, workspace_id))
                    if changed.rowcount != 1:
                        self._json({"error": "Connection not found."}, HTTPStatus.NOT_FOUND); return
                audit(session["user_id"], "connection.disabled", "connection", connection_id, "Disabled provider connection", self._source_ip(), workspace_id=workspace_id)
                self._json({"status": "disabled"}); return
            if path.startswith("/api/connections/") and path.endswith("/rotate"):
                workspace_id = self._require_workspace(session, "admin")
                if workspace_id is None:
                    return
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
                    current = connection.execute("SELECT encrypted_secrets FROM connections WHERE id = ? AND workspace_id = ?", (connection_id, workspace_id)).fetchone()
                    if current is None:
                        self._json({"error": "Connection not found."}, HTTPStatus.NOT_FOUND); return
                    merged = {**decrypt_secrets(current["encrypted_secrets"]), **secrets_to_store}
                    connection.execute("UPDATE connections SET encrypted_secrets = ?, token_expires_at = ?, is_active = 1 WHERE id = ?", (encrypt_secrets(merged), token_expiry, connection_id))
                audit(session["user_id"], "connection.rotated", "connection", connection_id, "Rotated provider credentials", self._source_ip(), workspace_id=workspace_id)
                self._json({"status": "rotated", "token_expires_at": token_expiry}); return
            if path == "/api/uploads":
                workspace_id = self._require_workspace(session, "editor")
                if workspace_id is None:
                    return
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
                with db() as connection:
                    enforce_storage_limit(connection, workspace_id, len(image))
                    record_usage(connection, workspace_id, "storage_bytes", len(image), period="total")
                filename = f"{uuid.uuid4().hex}{IMAGE_TYPES[actual_type]}"
                self._json({"url": store_media(filename, actual_type, image)}, HTTPStatus.CREATED); return
            if path == "/api/posts":
                workspace_id = self._require_workspace(session, "editor")
                if workspace_id is None:
                    return
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
                        selected = connection.execute(f"SELECT id, provider FROM connections WHERE workspace_id = ? AND is_active = 1 AND (token_expires_at IS NULL OR token_expires_at > ?) AND id IN ({placeholders})", (workspace_id, now(), *target_ids)).fetchall()
                        if len(selected) != len(set(target_ids)) or {row["provider"] for row in selected} != set(channels):
                            self._json({"error": "Select active accounts for every chosen platform."}, HTTPStatus.BAD_REQUEST); return
                    elif len(channels) != 1:
                        self._json({"error": "Connect an account for each platform when publishing to more than one platform."}, HTTPStatus.BAD_REQUEST); return
                    for url in image_urls:
                        generated = connection.execute("SELECT workspace_id, moderation FROM media_jobs WHERE result_url = ?", (url,)).fetchone()
                        if generated and (generated["workspace_id"] != workspace_id or generated["moderation"] != "approved"):
                            self._json({"error": "Generated media must be approved in this workspace before it can be published."}, HTTPStatus.BAD_REQUEST); return
                    for channel in channels:
                        validate_post(channel, body, image_url, len(image_urls))
                    enforce_monthly_quota(connection, workspace_id, "posts_created", "posts_per_month", "posts")
                    record_usage(connection, workspace_id, "posts_created")
                    post_id = insert_id(connection, "INSERT INTO posts (user_id, workspace_id, body, channel, state, scheduled_for, scheduled_timezone, image_url, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", (session["user_id"], workspace_id, body, channels[0], "scheduled" if schedule else "draft", schedule, schedule_zone if schedule else None, image_url, now()))
                    for target_id in dict.fromkeys(target_ids):
                        connection.execute("INSERT INTO post_targets (post_id, connection_id) VALUES (?, ?)", (post_id, target_id))
                    for position, url in enumerate(image_urls):
                        connection.execute("INSERT INTO post_media (post_id, media_url, position) VALUES (?, ?, ?)", (post_id, url, position))
                    row = dict(connection.execute("SELECT * FROM posts WHERE id = ?", (post_id,)).fetchone())
                row["media_urls"] = image_urls
                audit(session["user_id"], "post.created", "post", post_id, f"Created {'/'.join(channels)} post", self._source_ip(), workspace_id=workspace_id)
                self._json(row, HTTPStatus.CREATED); return
            if path.startswith("/api/posts/") and path.endswith("/remove"):
                workspace_id = self._require_workspace(session, "editor")
                if workspace_id is None:
                    return
                post_id = int(path.split("/")[3])
                with db() as connection:
                    post = connection.execute("SELECT state FROM posts WHERE id = ? AND workspace_id = ?", (post_id, workspace_id)).fetchone()
                    if post is None or post["state"] not in {"draft", "scheduled", "failed"}:
                        self._json({"error": "Only this workspace's drafts, scheduled posts, or failed posts can be removed from the queue."}, HTTPStatus.CONFLICT); return
                    connection.execute("DELETE FROM deliveries WHERE post_id = ?", (post_id,))
                    connection.execute("DELETE FROM post_media WHERE post_id = ?", (post_id,))
                    connection.execute("DELETE FROM post_targets WHERE post_id = ?", (post_id,))
                    connection.execute("DELETE FROM posts WHERE id = ? AND workspace_id = ?", (post_id, workspace_id))
                audit(session["user_id"], "post.removed", "post", post_id, "Removed unpublished post from queue", self._source_ip(), workspace_id=workspace_id)
                self._json({"status": "removed"}); return
            if path.startswith("/api/posts/") and path.endswith("/delete-from-channels"):
                workspace_id = self._require_workspace(session, "editor")
                if workspace_id is None:
                    return
                post_id = int(path.split("/")[3])
                with db() as connection:
                    row = connection.execute("SELECT * FROM posts WHERE id = ? AND workspace_id = ? AND state = 'published'", (post_id, workspace_id)).fetchone()
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
                audit(session["user_id"], "post.remote_delete", "post", post_id, f"Deleted from {len(deleted)} channel(s), failed on {len(failed)}", self._source_ip(), workspace_id=workspace_id)
                self._json({"deleted": deleted, "failed": failed}, HTTPStatus.OK if deleted else HTTPStatus.BAD_GATEWAY); return
            if path.startswith("/api/posts/") and path.endswith("/schedule"):
                workspace_id = self._require_workspace(session, "editor")
                if workspace_id is None:
                    return
                post_id, schedule_zone = int(path.split("/")[3]), timezone_name(payload.get("scheduled_timezone") or session["timezone"])
                schedule = self._schedule_time(payload.get("scheduled_for", ""), schedule_zone)
                with db() as connection:
                    cursor = connection.execute("UPDATE posts SET state = 'scheduled', scheduled_for = ?, scheduled_timezone = ?, attempts = 0, last_error = NULL WHERE id = ? AND workspace_id = ? AND state != 'published'", (schedule, schedule_zone, post_id, workspace_id))
                    if cursor.rowcount == 1:
                        connection.execute("UPDATE post_targets SET state = 'pending', last_error = NULL WHERE post_id = ?", (post_id,))
                if cursor.rowcount != 1:
                    self._json({"error": "Post not found or already published."}, HTTPStatus.NOT_FOUND); return
                audit(session["user_id"], "post.scheduled", "post", post_id, f"Scheduled for {schedule}", self._source_ip(), workspace_id=workspace_id)
                self._json({"status": "scheduled"}); return
            if path.startswith("/api/posts/") and path.endswith("/publish"):
                workspace_id = self._require_workspace(session, "editor")
                if workspace_id is None:
                    return
                post_id = int(path.split("/")[3])
                with db() as connection:
                    found = connection.execute("SELECT id FROM posts WHERE id = ? AND workspace_id = ? AND state != 'published'", (post_id, workspace_id)).fetchone()
                    if found is None:
                        self._json({"error": "Post not found or already published."}, HTTPStatus.NOT_FOUND); return
                    connection.execute("UPDATE posts SET state = 'scheduled', scheduled_for = ? WHERE id = ? AND workspace_id = ?", (now(), post_id, workspace_id))
                audit(session["user_id"], "post.queued", "post", post_id, "Queued for immediate delivery", self._source_ip(), workspace_id=workspace_id)
                self._json({"status": "queued"}, HTTPStatus.ACCEPTED); return
            if path.startswith("/api/posts/") and path.endswith("/retry"):
                workspace_id = self._require_workspace(session, "editor")
                if workspace_id is None:
                    return
                post_id = int(path.split("/")[3])
                with db() as connection:
                    cursor = connection.execute("UPDATE posts SET state = 'scheduled', scheduled_for = ?, scheduled_timezone = 'UTC', attempts = 0, publishing_started_at = NULL, last_error = NULL WHERE id = ? AND workspace_id = ? AND state = 'failed'", (now(), post_id, workspace_id))
                    if cursor.rowcount == 1:
                        connection.execute("UPDATE post_targets SET state = 'pending', last_error = NULL WHERE post_id = ? AND state != 'published'", (post_id,))
                if cursor.rowcount != 1:
                    self._json({"error": "Only this workspace's failed posts can be retried."}, HTTPStatus.NOT_FOUND); return
                audit(session["user_id"], "post.retried", "post", post_id, "Manually retried failed delivery", self._source_ip(), workspace_id=workspace_id)
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
