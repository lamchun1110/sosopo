"""Sosopo: a small, self-hosted social publishing dashboard.

This module is the HTTP surface and the process entrypoint. Application logic
lives in focused sibling modules; the re-export block below keeps
``app.server`` the one public namespace used by tests, ``app/worker.py``,
``scripts/``, and the container healthcheck.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import importlib
import json
import logging
import re
import secrets
import sqlite3
import sys
import uuid
from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import ModuleType
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, unquote, urlencode, urlparse
from zoneinfo import ZoneInfo

# Dependency order. The test suite reloads this module after changing the
# environment, so every sibling is reloaded in this order first: reloading a
# module rebinds the names it imported, which is only correct once the modules
# it depends on have themselves been reloaded.
_SUBMODULES = (
    "errors", "config", "database", "security", "http_client", "audit", "workspaces", "plans", "billing",
    "invitations", "media_storage", "ai_adapters", "ai_providers", "media_jobs", "oauth", "connections", "schema",
    "publishing",
)
_PACKAGE = __name__.rpartition(".")[0]


def _load_submodules() -> dict[str, ModuleType]:
    """Import every sibling, reloading them when this module is itself reloaded."""
    reloading = globals().get("_MODULES") is not None
    loaded: dict[str, ModuleType] = {}
    for name in _SUBMODULES:
        module = importlib.import_module(f"{_PACKAGE}.{name}" if _PACKAGE else name)
        loaded[name] = importlib.reload(module) if reloading else module
    return loaded


_MODULES = _load_submodules()
http_client = _MODULES["http_client"]

# Public facade. Everything Sosopo exposes outside this package is re-exported
# here by hand so the surface is greppable and additions are deliberate.

ProviderError = _MODULES["errors"].ProviderError

environment_value = _MODULES["config"].environment_value
APP_DIR = _MODULES["config"].APP_DIR
DATA_DIR = _MODULES["config"].DATA_DIR
DB_PATH = _MODULES["config"].DB_PATH
LEGACY_DB_PATH = _MODULES["config"].LEGACY_DB_PATH
DATABASE_URL = _MODULES["config"].DATABASE_URL
UPLOADS_DIR = _MODULES["config"].UPLOADS_DIR
MAX_POST_LENGTH = _MODULES["config"].MAX_POST_LENGTH
MAX_UPLOAD_BYTES = _MODULES["config"].MAX_UPLOAD_BYTES
MAX_IMAGE_PIXELS = _MODULES["config"].MAX_IMAGE_PIXELS
MAX_POST_MEDIA = _MODULES["config"].MAX_POST_MEDIA
MAX_ATTEMPTS = _MODULES["config"].MAX_ATTEMPTS
POLL_SECONDS = _MODULES["config"].POLL_SECONDS
RETRY_BASE_SECONDS = _MODULES["config"].RETRY_BASE_SECONDS
RETRY_MAX_SECONDS = _MODULES["config"].RETRY_MAX_SECONDS
WORKER_HEARTBEAT_SECONDS = _MODULES["config"].WORKER_HEARTBEAT_SECONDS
PUBLISHING_LEASE_SECONDS = _MODULES["config"].PUBLISHING_LEASE_SECONDS
DEFAULT_AUDIT_RETENTION_DAYS = _MODULES["config"].DEFAULT_AUDIT_RETENTION_DAYS
SESSION_SECONDS = _MODULES["config"].SESSION_SECONDS
OIDC_STATE_SECONDS = _MODULES["config"].OIDC_STATE_SECONDS
SOCIAL_OAUTH_STATE_SECONDS = _MODULES["config"].SOCIAL_OAUTH_STATE_SECONDS
AUTH_REQUESTS_PER_MINUTE = _MODULES["config"].AUTH_REQUESTS_PER_MINUTE
WRITE_REQUESTS_PER_MINUTE = _MODULES["config"].WRITE_REQUESTS_PER_MINUTE
CHANNELS = _MODULES["config"].CHANNELS
IMAGE_TYPES = _MODULES["config"].IMAGE_TYPES
CHANNEL_CHARACTER_LIMITS = _MODULES["config"].CHANNEL_CHARACTER_LIMITS
CHANNEL_MEDIA_LIMITS = _MODULES["config"].CHANNEL_MEDIA_LIMITS
WORKSPACE_ROLES = _MODULES["config"].WORKSPACE_ROLES
WORKSPACE_ROLE_RANK = _MODULES["config"].WORKSPACE_ROLE_RANK
MAX_WORKSPACE_NAME_LENGTH = _MODULES["config"].MAX_WORKSPACE_NAME_LENGTH
INVITATION_SECONDS = _MODULES["config"].INVITATION_SECONDS
EXPIRED_INVITATION_RETENTION_DAYS = _MODULES["config"].EXPIRED_INVITATION_RETENTION_DAYS
EMAIL_PATTERN = _MODULES["config"].EMAIL_PATTERN
CONNECTION_EXPIRY_WARNING_DAYS = _MODULES["config"].CONNECTION_EXPIRY_WARNING_DAYS
TOKEN_REFRESH_INTERVAL_SECONDS = _MODULES["config"].TOKEN_REFRESH_INTERVAL_SECONDS
TOKEN_REFRESH_HORIZON_HOURS = _MODULES["config"].TOKEN_REFRESH_HORIZON_HOURS
PLAN_LIMITS = _MODULES["config"].PLAN_LIMITS
STRIPE_PLAN_PRICE_VARIABLES = _MODULES["config"].STRIPE_PLAN_PRICE_VARIABLES
STRIPE_WEBHOOK_TOLERANCE_SECONDS = _MODULES["config"].STRIPE_WEBHOOK_TOLERANCE_SECONDS
MEDIA_JOB_KINDS = _MODULES["config"].MEDIA_JOB_KINDS
MAX_MEDIA_PROMPT_LENGTH = _MODULES["config"].MAX_MEDIA_PROMPT_LENGTH
MAX_MEDIA_STYLE_LENGTH = _MODULES["config"].MAX_MEDIA_STYLE_LENGTH
MAX_MEDIA_DOWNLOAD_BYTES = _MODULES["config"].MAX_MEDIA_DOWNLOAD_BYTES
MEDIA_IMAGE_SIZES = _MODULES["config"].MEDIA_IMAGE_SIZES
MEDIA_VIDEO_SIZES = _MODULES["config"].MEDIA_VIDEO_SIZES
VIDEO_POLL_LIMIT = _MODULES["config"].VIDEO_POLL_LIMIT
LOGGER = _MODULES["config"].LOGGER
now = _MODULES["config"].now
config = _MODULES["config"].config
public_url = _MODULES["config"].public_url
deployment_mode = _MODULES["config"].deployment_mode
self_signup_allowed = _MODULES["config"].self_signup_allowed
timezone_name = _MODULES["config"].timezone_name

Record = _MODULES["database"].Record
Result = _MODULES["database"].Result
Database = _MODULES["database"].Database
db = _MODULES["database"].db
insert_id = _MODULES["database"].insert_id
columns = _MODULES["database"].columns
table_columns = _MODULES["database"].table_columns
add_column = _MODULES["database"].add_column
add_table_column = _MODULES["database"].add_table_column

RATE_LIMITS = _MODULES["security"].RATE_LIMITS
RATE_LIMIT_LOCK = _MODULES["security"].RATE_LIMIT_LOCK
allowed_request = _MODULES["security"].allowed_request
source_ip = _MODULES["security"].source_ip
encryption = _MODULES["security"].encryption
encrypt_secrets = _MODULES["security"].encrypt_secrets
decrypt_secrets = _MODULES["security"].decrypt_secrets
hash_password = _MODULES["security"].hash_password
expires_at = _MODULES["security"].expires_at

parse_retry_after = _MODULES["http_client"].parse_retry_after

audit = _MODULES["audit"].audit
cleanup_expired_records = _MODULES["audit"].cleanup_expired_records

workspace_role_allows = _MODULES["workspaces"].workspace_role_allows
workspace_slug = _MODULES["workspaces"].workspace_slug
create_workspace = _MODULES["workspaces"].create_workspace
workspace_membership = _MODULES["workspaces"].workspace_membership
user_workspaces = _MODULES["workspaces"].user_workspaces
default_workspace_id = _MODULES["workspaces"].default_workspace_id
ensure_personal_workspace = _MODULES["workspaces"].ensure_personal_workspace
migrate_users_to_workspaces = _MODULES["workspaces"].migrate_users_to_workspaces
workspace_plan = _MODULES["workspaces"].workspace_plan
workspace_setting = _MODULES["workspaces"].workspace_setting
save_workspace_setting = _MODULES["workspaces"].save_workspace_setting
create_local_user = _MODULES["workspaces"].create_local_user

plan_limits = _MODULES["plans"].plan_limits
current_period = _MODULES["plans"].current_period
record_usage = _MODULES["plans"].record_usage
usage_amount = _MODULES["plans"].usage_amount
enforce_monthly_quota = _MODULES["plans"].enforce_monthly_quota
enforce_member_limit = _MODULES["plans"].enforce_member_limit
enforce_connection_limit = _MODULES["plans"].enforce_connection_limit
enforce_storage_limit = _MODULES["plans"].enforce_storage_limit

billing_enabled = _MODULES["billing"].billing_enabled
stripe_request = _MODULES["billing"].stripe_request
verify_stripe_signature = _MODULES["billing"].verify_stripe_signature
apply_billing_event = _MODULES["billing"].apply_billing_event

send_email = _MODULES["invitations"].send_email
invitation_url = _MODULES["invitations"].invitation_url
invitation_by_token = _MODULES["invitations"].invitation_by_token
invitation_is_usable = _MODULES["invitations"].invitation_is_usable

detected_image_type = _MODULES["media_storage"].detected_image_type
inspect_image = _MODULES["media_storage"].inspect_image
public_image_url = _MODULES["media_storage"].public_image_url
storage_backend = _MODULES["media_storage"].storage_backend
media_key = _MODULES["media_storage"].media_key
media_client = _MODULES["media_storage"].media_client
media_url = _MODULES["media_storage"].media_url
store_media = _MODULES["media_storage"].store_media
media_exists = _MODULES["media_storage"].media_exists
media_bytes = _MODULES["media_storage"].media_bytes
post_media_urls = _MODULES["media_storage"].post_media_urls

AiProvider = _MODULES["ai_providers"].AiProvider
AI_PROVIDERS = _MODULES["ai_providers"].AI_PROVIDERS
AI_PROVIDER_MODELS = _MODULES["ai_providers"].AI_PROVIDER_MODELS
AI_PROVIDER_IMAGE_MODELS = _MODULES["ai_providers"].AI_PROVIDER_IMAGE_MODELS
AI_PROVIDER_VIDEO_MODELS = _MODULES["ai_providers"].AI_PROVIDER_VIDEO_MODELS
stored_ai_provider_settings = _MODULES["ai_providers"].stored_ai_provider_settings
effective_ai_provider_stored = _MODULES["ai_providers"].effective_ai_provider_stored
save_ai_provider_settings = _MODULES["ai_providers"].save_ai_provider_settings
remove_ai_provider_settings = _MODULES["ai_providers"].remove_ai_provider_settings
ai_model_catalog = _MODULES["ai_providers"].ai_model_catalog
ai_provider_settings = _MODULES["ai_providers"].ai_provider_settings
available_ai_providers = _MODULES["ai_providers"].available_ai_providers
ai_provider_models = _MODULES["ai_providers"].ai_provider_models
generate_post_copy = _MODULES["ai_providers"].generate_post_copy

default_media_model = _MODULES["media_jobs"].default_media_model
media_job_prompt = _MODULES["media_jobs"].media_job_prompt
store_generated_media = _MODULES["media_jobs"].store_generated_media
generate_image_media = _MODULES["media_jobs"].generate_image_media
generate_video_media = _MODULES["media_jobs"].generate_video_media
claim_media_job = _MODULES["media_jobs"].claim_media_job
run_media_job = _MODULES["media_jobs"].run_media_job
media_worker = _MODULES["media_jobs"].media_worker

oidc_settings = _MODULES["oauth"].oidc_settings
oidc_redirect_uri = _MODULES["oauth"].oidc_redirect_uri
verify_oidc_id_token = _MODULES["oauth"].verify_oidc_id_token
social_oauth_redirect_uri = _MODULES["oauth"].social_oauth_redirect_uri
social_oauth_settings = _MODULES["oauth"].social_oauth_settings
social_oauth_enabled = _MODULES["oauth"].social_oauth_enabled
social_token_expiry = _MODULES["oauth"].social_token_expiry
social_oauth_connections = _MODULES["oauth"].social_oauth_connections

token_is_expired = _MODULES["connections"].token_is_expired
connection_health = _MODULES["connections"].connection_health
save_social_connections = _MODULES["connections"].save_social_connections
refresh_connection_token = _MODULES["connections"].refresh_connection_token
refresh_expiring_connection_tokens = _MODULES["connections"].refresh_expiring_connection_tokens

setup_database = _MODULES["schema"].setup_database

validate_post = _MODULES["publishing"].validate_post
provider_status = _MODULES["publishing"].provider_status
delete_published_content = _MODULES["publishing"].delete_published_content
claim_post = _MODULES["publishing"].claim_post
deliver = _MODULES["publishing"].deliver
worker_heartbeat = _MODULES["publishing"].worker_heartbeat
worker_healthy = _MODULES["publishing"].worker_healthy
recover_stale_deliveries = _MODULES["publishing"].recover_stale_deliveries
scheduler = _MODULES["publishing"].scheduler


# Tests replace outbound HTTP and the publish entry point by assigning to
# ``app.server``. These names are deliberately absent from this module's own
# namespace: reads and writes are forwarded to the module that defines them, so
# a replacement is visible to every caller rather than to this module alone.
_SEAMS = {
    "request_json": "http_client",
    "request_form": "http_client",
    "request_get_json": "http_client",
    "request_get_bytes": "http_client",
    "request_delete": "http_client",
    "telegram_request": "http_client",
    "publish": "publishing",
    "PyJWKClient": "oauth",
    "VIDEO_POLL_SECONDS": "config",
}


class _Facade(ModuleType):
    """Module type that leaves the seam names above owned by their real module."""

    def __getattr__(self, name: str) -> Any:
        owner = _SEAMS.get(name)
        if owner is None:
            raise AttributeError(f"module {self.__name__!r} has no attribute {name!r}")
        return getattr(_MODULES[owner], name)

    def __setattr__(self, name: str, value: Any) -> None:
        owner = _SEAMS.get(name)
        if owner is None:
            super().__setattr__(name, value)
        else:
            setattr(_MODULES[owner], name, value)


sys.modules[__name__].__class__ = _Facade


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
                media = {row["status"]: row["count"] for row in connection.execute("SELECT status, COUNT(*) AS count FROM media_jobs GROUP BY status").fetchall()}
                active_workspaces = connection.execute("SELECT COUNT(*) AS count FROM workspaces WHERE status = 'active'").fetchone()["count"]
            lines = ["# HELP sosopo_posts Number of posts by state.", "# TYPE sosopo_posts gauge"]
            lines.extend(f'sosopo_posts{{state="{state}"}} {count}' for state, count in sorted(states.items()))
            lines.extend(["# HELP sosopo_deliveries_total Delivery attempts by result.", "# TYPE sosopo_deliveries_total counter"])
            lines.extend(f'sosopo_deliveries_total{{status="{status}"}} {count}' for status, count in sorted(deliveries.items()))
            lines.extend(["# HELP sosopo_media_jobs Number of AI media jobs by status.", "# TYPE sosopo_media_jobs gauge"])
            lines.extend(f'sosopo_media_jobs{{status="{status}"}} {count}' for status, count in sorted(media.items()))
            lines.extend(["# HELP sosopo_workspaces Number of active workspaces.", "# TYPE sosopo_workspaces gauge", f"sosopo_workspaces {active_workspaces}"])
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
                token = http_client.request_form(settings["token_endpoint"], {"grant_type": "authorization_code", "code": code, "redirect_uri": oidc_redirect_uri(), "client_id": settings["client_id"], "client_secret": settings["client_secret"], "code_verifier": stored["code_verifier"]})
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
            for name in AI_PROVIDERS:
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
        if path == "/api/workspaces/status":
            session = self._session()
            workspace_id = self._require_workspace(session, "admin")
            if workspace_id is None:
                return
            since = (datetime.now(UTC) - timedelta(days=30)).isoformat()
            with db() as connection:
                posts = {row["state"]: row["count"] for row in connection.execute("SELECT state, COUNT(*) AS count FROM posts WHERE workspace_id = ? GROUP BY state", (workspace_id,)).fetchall()}
                deliveries = [dict(row) for row in connection.execute(
                    "SELECT deliveries.provider, deliveries.status, COUNT(*) AS count FROM deliveries JOIN posts ON posts.id = deliveries.post_id"
                    " WHERE posts.workspace_id = ? AND deliveries.created_at >= ? GROUP BY deliveries.provider, deliveries.status",
                    (workspace_id, since),
                ).fetchall()]
                accounts = [dict(row) for row in connection.execute("SELECT is_active, token_expires_at FROM connections WHERE workspace_id = ?", (workspace_id,)).fetchall()]
                members = int(connection.execute("SELECT COUNT(*) AS count FROM workspace_memberships WHERE workspace_id = ?", (workspace_id,)).fetchone()["count"])
                media_jobs = {row["status"]: row["count"] for row in connection.execute("SELECT status, COUNT(*) AS count FROM media_jobs WHERE workspace_id = ? GROUP BY status", (workspace_id,)).fetchall()}
                plan = workspace_plan(connection, workspace_id)
                usage = {
                    "posts_created": usage_amount(connection, workspace_id, "posts_created"),
                    "ai_generations": usage_amount(connection, workspace_id, "ai_generations"),
                    "ai_media": usage_amount(connection, workspace_id, "ai_media"),
                    "storage_bytes": usage_amount(connection, workspace_id, "storage_bytes", period="total"),
                }
                cap = workspace_setting(connection, workspace_id, "ai_monthly_cap")
            health = {"active": 0, "expiring_soon": 0, "expired": 0, "disabled": 0}
            for account in accounts:
                health[connection_health(account)] += 1
            self._json({"plan": plan, "limits": plan_limits(plan), "usage": usage, "ai_monthly_cap": int(cap) if cap is not None else None, "posts": posts, "deliveries_30d": deliveries, "connection_health": health, "members": members, "media_jobs": media_jobs, "period": current_period()})
            return
        if path == "/api/admin/workspaces":
            session = self._session()
            if session["role"] != "admin":
                self._json({"error": "Administrator access required."}, HTTPStatus.FORBIDDEN); return
            with db() as connection:
                rows = [dict(row) for row in connection.execute(
                    "SELECT workspaces.id, workspaces.name, workspaces.slug, workspaces.plan, workspaces.status, workspaces.created_at, users.username AS owner,"
                    " (SELECT COUNT(*) FROM workspace_memberships WHERE workspace_memberships.workspace_id = workspaces.id) AS member_count,"
                    " (SELECT COUNT(*) FROM posts WHERE posts.workspace_id = workspaces.id) AS post_count"
                    " FROM workspaces JOIN users ON users.id = workspaces.owner_user_id ORDER BY workspaces.id",
                ).fetchall()]
            # Support access is deliberately metadata-only and always audited.
            audit(session["user_id"], "admin.workspaces_listed", "instance", None, f"Listed {len(rows)} workspaces (support access)", self._source_ip())
            self._json({"workspaces": rows})
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
                stored = {"api_key": api_key or current["api_key"], "base_url": definition.base_url, "model": model, "models": json.dumps(catalog)}
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
                stored = {"api_key": api_key or current["api_key"], "base_url": AI_PROVIDERS[provider].base_url, "model": model, "models": json.dumps(catalog)}
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
