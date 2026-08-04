"""Connected provider accounts: health, persistence, and token rotation."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode

try:  # package import (tests, `python -m app.server`)
    from . import http_client
    from .audit import audit
    from .config import CONNECTION_EXPIRY_WARNING_DAYS, LOGGER, TOKEN_REFRESH_HORIZON_HOURS, config, now
    from .database import db
    from .errors import ProviderError
    from .oauth import social_oauth_settings, social_token_expiry
    from .plans import enforce_connection_limit
    from .security import decrypt_secrets, encrypt_secrets
    from .workspaces import workspace_membership
except ImportError:  # script import (`python /app/app/server.py`)
    import http_client
    from audit import audit
    from config import CONNECTION_EXPIRY_WARNING_DAYS, LOGGER, TOKEN_REFRESH_HORIZON_HOURS, config, now
    from database import db
    from errors import ProviderError
    from oauth import social_oauth_settings, social_token_expiry
    from plans import enforce_connection_limit
    from security import decrypt_secrets, encrypt_secrets
    from workspaces import workspace_membership


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
            token = http_client.request_form(settings["token"], {"grant_type": "refresh_token", "refresh_token": refresh_token, "client_id": settings["client_id"], "client_secret": settings["client_secret"]})
        elif provider == "Threads":
            access = stored.get("access_token", "")
            if not access:
                return False
            refresh_url = config("THREADS_REFRESH_URL") or "https://graph.threads.net/refresh_access_token"
            token = http_client.request_get_json(f"{refresh_url}?{urlencode({'grant_type': 'th_refresh_token', 'access_token': access})}")
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
