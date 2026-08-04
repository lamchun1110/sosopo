"""Connected provider accounts and the social OAuth start."""


from __future__ import annotations


import base64
import hashlib
import json
import re
import secrets
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from http import HTTPStatus
from typing import Any
from urllib.parse import urlencode

try:  # package import (tests, `python -m app.server`)
    from ..audit import audit
    from ..config import CHANNELS, SOCIAL_OAUTH_STATE_SECONDS, now
    from ..connections import connection_health, token_is_expired
    from ..database import Record, db, insert_id
    from ..errors import ProviderError
    from ..oauth import social_oauth_redirect_uri, social_oauth_settings
    from ..plans import enforce_connection_limit
    from ..security import decrypt_secrets, encrypt_secrets
except ImportError:  # script import (`python /app/app/server.py`)
    from audit import audit
    from config import CHANNELS, SOCIAL_OAUTH_STATE_SECONDS, now
    from connections import connection_health, token_is_expired
    from database import Record, db, insert_id
    from errors import ProviderError
    from oauth import social_oauth_redirect_uri, social_oauth_settings
    from plans import enforce_connection_limit
    from security import decrypt_secrets, encrypt_secrets


class ConnectionRoutes:
    """Connected provider accounts and the social OAuth start.

    Mixed into ``Handler``; every method returns True once it has answered.
    """



    def get_connections(self, path: str) -> bool:
        """Handle one connection GET; True when answered."""
        if path.startswith("/api/social-oauth/") and path.endswith("/start"):
            provider = path.split("/")[3]
            if provider == "Instagram":
                provider = "Facebook"
            if provider not in {"Facebook", "Threads", "X", "LinkedIn", "Discord"}:
                self._json({"error": "Unsupported social OAuth provider."}, HTTPStatus.NOT_FOUND); return True
            try:
                session = self._session()
                workspace_id = self._require_workspace(session, "admin")
                if workspace_id is None:
                    return True
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
            return True
        if path == "/api/connections":
            session = self._session()
            workspace_id = self._require_workspace(session)
            if workspace_id is None:
                return True
            with db() as connection:
                records = [dict(row) for row in connection.execute("SELECT id, provider, external_account_id, display_name, token_expires_at, is_active, created_at FROM connections WHERE workspace_id = ? ORDER BY provider, display_name", (workspace_id,)).fetchall()]
            for record in records:
                record["health"] = connection_health(record)
            self._json({"connections": records})
            return True
        return False

    def post_connections(self, path: str, payload: dict[str, Any], session: Record) -> bool:
        """Handle one connection POST; True when answered."""
        if path == "/api/connections":
            workspace_id = self._require_workspace(session, "admin")
            if workspace_id is None:
                return True
            provider = str(payload.get("provider", "")).strip()
            account_id = str(payload.get("external_account_id", "")).strip()
            display_name = str(payload.get("display_name", "")).strip()
            secret_values = payload.get("secrets", {})
            settings = payload.get("settings", {})
            if provider not in CHANNELS or not account_id or not display_name or not isinstance(secret_values, dict) or not isinstance(settings, dict):
                self._json({"error": "Provider, account ID, display name, secrets, and settings are required."}, HTTPStatus.BAD_REQUEST); return True
            secrets_to_store = {str(key): str(value) for key, value in secret_values.items() if value}
            if provider == "Discord":
                webhook_url = secrets_to_store.get("webhook_url") or account_id
                match = re.fullmatch(r"https://(?:discord(?:app)?\.com)/api/webhooks/(\d+)/[^/?#]+/?", webhook_url)
                if not match:
                    self._json({"error": "Enter a valid Discord incoming webhook URL."}, HTTPStatus.BAD_REQUEST); return True
                account_id = match.group(1)
                secrets_to_store = {"webhook_url": webhook_url}
            token_expiry = str(payload.get("token_expires_at", "")).strip() or None
            if token_expiry and token_is_expired(token_expiry):
                self._json({"error": "token_expires_at must be a future ISO 8601 timestamp with timezone."}, HTTPStatus.BAD_REQUEST); return True
            if not secrets_to_store:
                self._json({"error": "At least one credential is required."}, HTTPStatus.BAD_REQUEST); return True
            with db() as connection:
                duplicate = connection.execute("SELECT id FROM connections WHERE (workspace_id = ? OR user_id = ?) AND provider = ? AND external_account_id = ?", (workspace_id, session["user_id"], provider, account_id)).fetchone()
                if duplicate:
                    self._json({"error": "This provider account is already connected in this workspace or another of your workspaces."}, HTTPStatus.CONFLICT); return True
                enforce_connection_limit(connection, workspace_id)
                connection_id = insert_id(connection,
                    "INSERT INTO connections (user_id, workspace_id, provider, external_account_id, display_name, encrypted_secrets, settings_json, token_expires_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (session["user_id"], workspace_id, provider, account_id, display_name, encrypt_secrets(secrets_to_store), json.dumps(settings), token_expiry, now()),
                )
            audit(session["user_id"], "connection.created", "connection", connection_id, f"Created {provider} connection {display_name}", self._source_ip(), workspace_id=workspace_id)
            self._json({"id": connection_id, "provider": provider, "external_account_id": account_id, "display_name": display_name}, HTTPStatus.CREATED); return True
        if path.startswith("/api/connections/") and path.endswith("/disable"):
            workspace_id = self._require_workspace(session, "admin")
            if workspace_id is None:
                return True
            connection_id = int(path.split("/")[3])
            with db() as connection:
                changed = connection.execute("UPDATE connections SET is_active = 0 WHERE id = ? AND workspace_id = ?", (connection_id, workspace_id))
                if changed.rowcount != 1:
                    self._json({"error": "Connection not found."}, HTTPStatus.NOT_FOUND); return True
            audit(session["user_id"], "connection.disabled", "connection", connection_id, "Disabled provider connection", self._source_ip(), workspace_id=workspace_id)
            self._json({"status": "disabled"}); return True
        if path.startswith("/api/connections/") and path.endswith("/rotate"):
            workspace_id = self._require_workspace(session, "admin")
            if workspace_id is None:
                return True
            connection_id = int(path.split("/")[3])
            secret_values = payload.get("secrets", {})
            token_expiry = str(payload.get("token_expires_at", "")).strip() or None
            if not isinstance(secret_values, dict):
                self._json({"error": "secrets must be an object."}, HTTPStatus.BAD_REQUEST); return True
            secrets_to_store = {str(key): str(value) for key, value in secret_values.items() if value}
            if not secrets_to_store:
                self._json({"error": "Provide at least one replacement credential."}, HTTPStatus.BAD_REQUEST); return True
            if token_expiry and token_is_expired(token_expiry):
                self._json({"error": "token_expires_at must be a future ISO 8601 timestamp with timezone."}, HTTPStatus.BAD_REQUEST); return True
            with db() as connection:
                current = connection.execute("SELECT encrypted_secrets FROM connections WHERE id = ? AND workspace_id = ?", (connection_id, workspace_id)).fetchone()
                if current is None:
                    self._json({"error": "Connection not found."}, HTTPStatus.NOT_FOUND); return True
                merged = {**decrypt_secrets(current["encrypted_secrets"]), **secrets_to_store}
                connection.execute("UPDATE connections SET encrypted_secrets = ?, token_expires_at = ?, is_active = 1 WHERE id = ?", (encrypt_secrets(merged), token_expiry, connection_id))
            audit(session["user_id"], "connection.rotated", "connection", connection_id, "Rotated provider credentials", self._source_ip(), workspace_id=workspace_id)
            self._json({"status": "rotated", "token_expires_at": token_expiry}); return True
        return False

