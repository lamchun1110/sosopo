"""Sosopo: a small, self-hosted social publishing dashboard.

This module is the HTTP surface and the process entrypoint. Application logic
lives in focused sibling modules; the re-export block below keeps
``app.server`` the one public namespace used by tests, ``app/worker.py``,
``scripts/``, and the container healthcheck.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import logging
import secrets
import sqlite3
import sys
from datetime import UTC, datetime
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import ModuleType
from typing import Any
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

# Dependency order. The test suite reloads this module after changing the
# environment, so every sibling is reloaded in this order first: reloading a
# module rebinds the names it imported, which is only correct once the modules
# it depends on have themselves been reloaded.
_SUBMODULES = (
    "errors", "config", "database", "security", "http_client", "audit", "workspaces", "plans",
    "invitations", "organizations", "credits", "billing", "brand_voice", "campaigns", "media_storage", "ai_adapters", "ai_providers", "media_jobs", "oauth",
    "connections", "insights", "schema",
    "publishing",
    # Route families last: each mixin imports from the modules above.
    "routes.public", "routes.connections", "routes.posts", "routes.ai", "routes.admin", "routes.media",
    "routes.team", "routes.account", "routes.billing", "routes.organizations", "routes.campaigns",
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
credit_packs = _MODULES["billing"].credit_packs
is_new_billing_event = _MODULES["billing"].is_new_billing_event
apply_credit_purchase = _MODULES["billing"].apply_credit_purchase
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
generate_campaign_plan = _MODULES["ai_providers"].generate_campaign_plan
generate_workspace_summary = _MODULES["ai_providers"].generate_workspace_summary
workspace_status = _MODULES["insights"].workspace_status
summary_prompt = _MODULES["insights"].summary_prompt
parse_campaign_plan = _MODULES["campaigns"].parse_campaign_plan
planning_prompt = _MODULES["campaigns"].planning_prompt
workspace_campaigns = _MODULES["campaigns"].workspace_campaigns

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


load_brand_voice = _MODULES["brand_voice"].load_brand_voice
save_brand_voice = _MODULES["brand_voice"].save_brand_voice
validated_profile = _MODULES["brand_voice"].validated_profile
brand_voice_prompt = _MODULES["brand_voice"].brand_voice_prompt
brand_voice_style = _MODULES["brand_voice"].brand_voice_style

CREDIT_OWNER_TYPES = _MODULES["credits"].CREDIT_OWNER_TYPES
credits_enforced = _MODULES["credits"].credits_enforced
ensure_credit_account = _MODULES["credits"].ensure_credit_account
credit_account = _MODULES["credits"].credit_account
account_balance = _MODULES["credits"].account_balance
record_credit_transaction = _MODULES["credits"].record_credit_transaction
monthly_grant = _MODULES["credits"].monthly_grant
grant_monthly_credits = _MODULES["credits"].grant_monthly_credits
charge_ai_credit = _MODULES["credits"].charge_ai_credit
refund_ai_credit = _MODULES["credits"].refund_ai_credit

# Route families, mixed into Handler below. Each owns one slice of the HTTP
# surface and returns True once it has answered a request.
PublicRoutes = _MODULES["routes.public"].PublicRoutes
ConnectionRoutes = _MODULES["routes.connections"].ConnectionRoutes
PostRoutes = _MODULES["routes.posts"].PostRoutes
AiRoutes = _MODULES["routes.ai"].AiRoutes
AdminRoutes = _MODULES["routes.admin"].AdminRoutes
MediaRoutes = _MODULES["routes.media"].MediaRoutes
TeamRoutes = _MODULES["routes.team"].TeamRoutes
AccountRoutes = _MODULES["routes.account"].AccountRoutes
BillingRoutes = _MODULES["routes.billing"].BillingRoutes
OrganizationRoutes = _MODULES["routes.organizations"].OrganizationRoutes
CampaignRoutes = _MODULES["routes.campaigns"].CampaignRoutes


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
    "request_put_bytes": "http_client",
    "telegram_request": "http_client",
    "publish": "publishing",
    "stripe_request": "billing",
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


class Handler(
    PublicRoutes,
    ConnectionRoutes,
    PostRoutes,
    AiRoutes,
    AdminRoutes,
    MediaRoutes,
    TeamRoutes,
    AccountRoutes,
    BillingRoutes,
    OrganizationRoutes,
    CampaignRoutes,
    SimpleHTTPRequestHandler,
):
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
        self.send_header("Content-Security-Policy", "default-src 'self'; base-uri 'self'; frame-ancestors 'none'; form-action 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self'")
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
        if self.get_public(path):
            return
        if path.startswith("/api/") and self._require_auth() is None:
            return
        if (self.get_connections(path) or self.get_posts(path) or self.get_ai(path)
                or self.get_admin(path) or self.get_media(path) or self.get_team(path)
                or self.get_organizations(path) or self.get_campaigns(path)):
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


    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == "/api/billing/webhook":
            self._handle_billing_webhook()
            return
        try:
            payload = self._read_json()
            if self.post_public(path, payload):
                return
            if not self._allow("write", WRITE_REQUESTS_PER_MINUTE):
                return
            if self.post_logout(path):
                return
            session = self._require_auth(csrf=True)
            if session is None:
                return
            if (self.post_ai(path, payload, session) or self.post_account(path, payload, session)
                    or self.post_team(path, payload, session) or self.post_media(path, payload, session)
                    or self.post_billing(path, payload, session) or self.post_admin(path, payload, session)
                    or self.post_connections(path, payload, session) or self.post_posts(path, payload, session)
                    or self.post_organizations(path, payload, session)
                    or self.post_campaigns(path, payload, session)):
                return
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
