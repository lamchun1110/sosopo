"""The signed-in user's own profile, preferences, and active workspace."""


from __future__ import annotations


import secrets
from http import HTTPStatus
from typing import Any

try:  # package import (tests, `python -m app.server`)
    from ..audit import audit
    from ..config import timezone_name
    from ..database import Record, db
    from ..security import hash_password
    from ..workspaces import workspace_membership
except ImportError:  # script import (`python /app/app/server.py`)
    from audit import audit
    from config import timezone_name
    from database import Record, db
    from security import hash_password
    from workspaces import workspace_membership


class AccountRoutes:
    """The signed-in user's own profile, preferences, and active workspace.

    Mixed into ``Handler``; every method returns True once it has answered.
    """



    def post_account(self, path: str, payload: dict[str, Any], session: Record) -> bool:
        """Handle one account POST; True when answered."""
        if path == "/api/me/timezone":
            zone = timezone_name(payload.get("timezone"))
            with db() as connection:
                connection.execute("UPDATE users SET timezone = ? WHERE id = ?", (zone, session["user_id"]))
            self._json({"timezone": zone}); return True
        if path == "/api/me/settings":
            zone = timezone_name(payload.get("timezone") or session["timezone"])
            signature = str(payload.get("signature", "")).strip()
            if len(signature) > 1_000:
                self._json({"error": "Signature must be 1,000 characters or fewer."}, HTTPStatus.BAD_REQUEST); return True
            with db() as connection:
                connection.execute("UPDATE users SET timezone = ?, signature = ? WHERE id = ?", (zone, signature, session["user_id"]))
            audit(session["user_id"], "user.settings_changed", "user", session["user_id"], "Updated timezone or signature", self._source_ip())
            self._json({"timezone": zone, "signature": signature}); return True
        if path == "/api/me/password":
            current_password = str(payload.get("current_password", ""))
            new_password = str(payload.get("new_password", ""))
            if len(new_password) < 12:
                self._json({"error": "Use a new password of at least 12 characters."}, HTTPStatus.BAD_REQUEST); return True
            with db() as connection:
                user = connection.execute("SELECT password_salt, password_hash FROM users WHERE id = ?", (session["user_id"],)).fetchone()
                if user is None or not secrets.compare_digest(hash_password(current_password, bytes.fromhex(user["password_salt"])), user["password_hash"]):
                    self._json({"error": "Current password is incorrect."}, HTTPStatus.UNAUTHORIZED); return True
                salt = secrets.token_bytes(16)
                connection.execute("UPDATE users SET password_salt = ?, password_hash = ? WHERE id = ?", (salt.hex(), hash_password(new_password, salt), session["user_id"]))
                connection.execute("DELETE FROM sessions WHERE user_id = ?", (session["user_id"],))
            audit(session["user_id"], "user.password_changed", "user", session["user_id"], "Changed password and revoked sessions", self._source_ip())
            self._json_session({"status": "password changed"}, self._create_session(session["user_id"])); return True
        if path == "/api/me/workspace":
            try:
                workspace_id = int(payload.get("workspace_id"))
            except (TypeError, ValueError):
                self._json({"error": "workspace_id must be a workspace number."}, HTTPStatus.BAD_REQUEST); return True
            with db() as connection:
                membership = workspace_membership(connection, workspace_id, session["user_id"])
                if membership is None:
                    self._json({"error": "You are not a member of that workspace."}, HTTPStatus.FORBIDDEN); return True
                connection.execute("UPDATE sessions SET active_workspace_id = ? WHERE id = ?", (workspace_id, session["id"]))
            self._json({"workspace": {"id": workspace_id, "name": membership["workspace_name"], "slug": membership["workspace_slug"], "role": membership["role"]}}); return True
        return False

