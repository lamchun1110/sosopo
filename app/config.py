"""Process configuration, tunable constants, and the primitives shared by every module.

Values derived from the environment are read once at import time. Modules that
need them import this module (``from . import config as cfg``) and read
``cfg.NAME`` so a reload of this module is visible everywhere immediately."""

from __future__ import annotations

import logging
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


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


# One-time credit packs. Each is sold only when its Stripe price ID is set.
# Override or extend with the SOSOPO_CREDIT_PACKS JSON environment value.
STRIPE_CREDIT_PACKS: dict[str, dict[str, object]] = {
    "small": {"price_variable": "STRIPE_PRICE_CREDITS_SMALL", "credits": 100},
    "medium": {"price_variable": "STRIPE_PRICE_CREDITS_MEDIUM", "credits": 500},
    "large": {"price_variable": "STRIPE_PRICE_CREDITS_LARGE", "credits": 2_000},
}
STRIPE_WEBHOOK_TOLERANCE_SECONDS = 300


MEDIA_JOB_KINDS = ("image", "video")


MAX_MEDIA_PROMPT_LENGTH = 2_000


MAX_MEDIA_STYLE_LENGTH = 200


MAX_MEDIA_DOWNLOAD_BYTES = 100 * 1024 * 1024


MEDIA_IMAGE_SIZES = {"1:1": "1024x1024", "3:2": "1536x1024", "2:3": "1024x1536", "16:9": "1792x1024", "9:16": "1024x1792"}


MEDIA_VIDEO_SIZES = {"16:9": "1280x720", "9:16": "720x1280", "1:1": "720x720"}


VIDEO_POLL_SECONDS = 10


VIDEO_POLL_LIMIT = 90


LOGGER = logging.getLogger("sosopo")


def now() -> str:
    return datetime.now(UTC).isoformat()


def config(name: str) -> str:
    return environment_value(name)


def public_url() -> str:
    return (config("SOSOPO_PUBLIC_URL") or config("SOSOPO_PUBLIC_BASE_URL")).rstrip("/")


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


def timezone_name(value: object) -> str:
    name = str(value or "UTC").strip()
    try:
        ZoneInfo(name)
    except ZoneInfoNotFoundError as error:
        raise ValueError("Use an IANA timezone such as Asia/Hong_Kong or Europe/London.") from error
    return name
