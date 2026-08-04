"""Instance-administrator views and user management."""


from __future__ import annotations


import secrets
from http import HTTPStatus
from typing import Any
from urllib.parse import unquote

try:  # package import (tests, `python -m app.server`)
    from ..ai_providers import AI_PROVIDERS, AI_PROVIDER_MODELS, ai_model_catalog, ai_provider_models, stored_ai_provider_settings
    from ..audit import audit
    from ..config import now, timezone_name
    from ..database import Record, db, insert_id
    from ..errors import ProviderError
    from ..publishing import worker_healthy
    from ..security import hash_password
    from ..workspaces import ensure_personal_workspace
except ImportError:  # script import (`python /app/app/server.py`)
    from ai_providers import AI_PROVIDERS, AI_PROVIDER_MODELS, ai_model_catalog, ai_provider_models, stored_ai_provider_settings
    from audit import audit
    from config import now, timezone_name
    from database import Record, db, insert_id
    from errors import ProviderError
    from publishing import worker_healthy
    from security import hash_password
    from workspaces import ensure_personal_workspace


class AdminRoutes:
    """Instance-administrator views and user management.

    Mixed into ``Handler``; every method returns True once it has answered.
    """



    def get_admin(self, path: str) -> bool:
        """Handle one administrator GET; True when answered."""
        if path == "/api/admin/users":
            session = self._session()
            if session["role"] != "admin":
                self._json({"error": "Administrator access required."}, HTTPStatus.FORBIDDEN); return True
            with db() as connection:
                users = [dict(row) for row in connection.execute("SELECT id, username, role, is_active, timezone, oidc_issuer, created_at FROM users ORDER BY id").fetchall()]
            self._json({"users": users}); return True
        if path == "/api/admin/ai-providers":
            session = self._session()
            if session["role"] != "admin":
                self._json({"error": "Administrator access required."}, HTTPStatus.FORBIDDEN); return True
            providers = []
            for name in AI_PROVIDERS:
                stored = stored_ai_provider_settings(name)
                catalog = ai_model_catalog(stored)
                providers.append({"name": name, "model": stored.get("model", AI_PROVIDER_MODELS[name][0]), "models": catalog or AI_PROVIDER_MODELS[name], "models_count": len(catalog), "models_checked_at": stored.get("models_checked_at"), "has_api_key": bool(stored.get("api_key"))})
            self._json({"providers": providers}); return True
        if path.startswith("/api/admin/ai-providers/") and path.endswith("/models"):
            session = self._session()
            if session["role"] != "admin":
                self._json({"error": "Administrator access required."}, HTTPStatus.FORBIDDEN); return True
            provider = unquote(path.split("/")[4])
            try:
                self._json({"models": ai_provider_models(provider)})
            except ProviderError as error:
                self._json({"error": str(error)}, HTTPStatus.BAD_GATEWAY)
            return True
        if path == "/api/admin/audit-events":
            session = self._session()
            if session["role"] != "admin":
                self._json({"error": "Administrator access required."}, HTTPStatus.FORBIDDEN); return True
            with db() as connection:
                events = [dict(row) for row in connection.execute("SELECT audit_events.*, users.username FROM audit_events LEFT JOIN users ON users.id = audit_events.user_id ORDER BY audit_events.id DESC LIMIT 200").fetchall()]
            self._json({"events": events}); return True
        if path == "/api/admin/status":
            session = self._session()
            if session["role"] != "admin":
                self._json({"error": "Administrator access required."}, HTTPStatus.FORBIDDEN); return True
            with db() as connection:
                states = {row["state"]: row["count"] for row in connection.execute("SELECT state, COUNT(*) AS count FROM posts GROUP BY state").fetchall()}
                heartbeat = connection.execute("SELECT checked_at FROM worker_heartbeats WHERE name = 'delivery'").fetchone()
                latest_failure = connection.execute("SELECT created_at, detail FROM deliveries WHERE status = 'failed' ORDER BY id DESC LIMIT 1").fetchone()
            self._json({"posts": states, "worker_healthy": worker_healthy(), "worker_checked_at": heartbeat["checked_at"] if heartbeat else None, "latest_delivery_failure": dict(latest_failure) if latest_failure else None})
            return True
        if path == "/api/admin/workspaces":
            session = self._session()
            if session["role"] != "admin":
                self._json({"error": "Administrator access required."}, HTTPStatus.FORBIDDEN); return True
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
            return True
        return False

    def post_admin(self, path: str, payload: dict[str, Any], session: Record) -> bool:
        """Handle one administrator POST; True when answered."""
        if path == "/api/admin/users":
            if session["role"] != "admin":
                self._json({"error": "Administrator access required."}, HTTPStatus.FORBIDDEN); return True
            username, password = str(payload.get("username", "")).strip(), str(payload.get("password", ""))
            role, zone = str(payload.get("role", "user")), timezone_name(payload.get("timezone", "UTC"))
            if len(username) < 3 or len(password) < 12 or role not in {"admin", "user"}:
                self._json({"error": "Use a username/password of at least 3/12 characters and a valid role."}, HTTPStatus.BAD_REQUEST); return True
            salt = secrets.token_bytes(16)
            with db() as connection:
                user_id = insert_id(connection, "INSERT INTO users (username, password_salt, password_hash, role, timezone, created_at) VALUES (?, ?, ?, ?, ?, ?)", (username, salt.hex(), hash_password(password, salt), role, zone, now()))
                ensure_personal_workspace(connection, user_id, username)
            audit(session["user_id"], "user.created", "user", user_id, f"Created {role} user {username}", self._source_ip())
            self._json({"id": user_id, "username": username, "role": role, "timezone": zone}, HTTPStatus.CREATED); return True
        if path.startswith("/api/admin/users/") and path.endswith("/disable"):
            if session["role"] != "admin":
                self._json({"error": "Administrator access required."}, HTTPStatus.FORBIDDEN); return True
            user_id = int(path.split("/")[4])
            if user_id == session["user_id"]:
                self._json({"error": "An administrator cannot disable their own account."}, HTTPStatus.BAD_REQUEST); return True
            with db() as connection:
                changed = connection.execute("UPDATE users SET is_active = 0 WHERE id = ?", (user_id,))
                if changed.rowcount != 1:
                    self._json({"error": "User not found."}, HTTPStatus.NOT_FOUND); return True
                connection.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
            audit(session["user_id"], "user.disabled", "user", user_id, "Disabled user and revoked sessions", self._source_ip())
            self._json({"status": "disabled"}); return True
        return False

