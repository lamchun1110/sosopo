"""Workspaces, members, invitations, settings, export, and deletion."""


from __future__ import annotations


import hashlib
import json
import secrets
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from http import HTTPStatus
from typing import Any

try:  # package import (tests, `python -m app.server`)
    from ..audit import audit
    from ..config import EMAIL_PATTERN, INVITATION_SECONDS, MAX_WORKSPACE_NAME_LENGTH, now
    from ..connections import connection_health
    from ..database import Record, db, insert_id
    from ..invitations import invitation_url, send_email
    from ..plans import current_period, enforce_member_limit, plan_limits, usage_amount
    from ..workspaces import create_workspace, save_workspace_setting, user_workspaces, workspace_plan, workspace_setting
except ImportError:  # script import (`python /app/app/server.py`)
    from audit import audit
    from config import EMAIL_PATTERN, INVITATION_SECONDS, MAX_WORKSPACE_NAME_LENGTH, now
    from connections import connection_health
    from database import Record, db, insert_id
    from invitations import invitation_url, send_email
    from plans import current_period, enforce_member_limit, plan_limits, usage_amount
    from workspaces import create_workspace, save_workspace_setting, user_workspaces, workspace_plan, workspace_setting


class TeamRoutes:
    """Workspaces, members, invitations, settings, export, and deletion.

    Mixed into ``Handler``; every method returns True once it has answered.
    """



    def get_team(self, path: str) -> bool:
        """Handle one workspace GET; True when answered."""
        if path == "/api/workspaces/status":
            session = self._session()
            workspace_id = self._require_workspace(session, "admin")
            if workspace_id is None:
                return True
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
            return True
        if path == "/api/workspaces/export":
            session = self._session()
            workspace_id = self._require_workspace(session, "admin")
            if workspace_id is None:
                return True
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
            return True
        if path == "/api/workspaces":
            session = self._session()
            with db() as connection:
                workspaces = [{"id": item["id"], "name": item["name"], "slug": item["slug"], "role": item["role"], "is_owner": item["owner_user_id"] == session["user_id"]} for item in user_workspaces(connection, session["user_id"])]
            self._json({"workspaces": workspaces, "active_workspace_id": session.get("workspace_id")})
            return True
        if path == "/api/workspaces/invitations":
            session = self._session()
            workspace_id = self._require_workspace(session, "admin")
            if workspace_id is None:
                return True
            with db() as connection:
                invitations = [dict(row) for row in connection.execute(
                    "SELECT id, email, role, expires_at, created_at FROM workspace_invitations WHERE workspace_id = ? AND accepted_at IS NULL ORDER BY id DESC",
                    (workspace_id,),
                ).fetchall()]
            for invitation in invitations:
                invitation["expired"] = str(invitation["expires_at"]) <= now()
            self._json({"invitations": invitations})
            return True
        if path == "/api/workspaces/members":
            session = self._session()
            workspace_id = self._require_workspace(session, "admin")
            if workspace_id is None:
                return True
            with db() as connection:
                members = [dict(row) for row in connection.execute(
                    "SELECT workspace_memberships.user_id, workspace_memberships.role, workspace_memberships.invite_state, workspace_memberships.created_at, users.username, users.is_active"
                    " FROM workspace_memberships JOIN users ON users.id = workspace_memberships.user_id WHERE workspace_memberships.workspace_id = ? ORDER BY workspace_memberships.id",
                    (workspace_id,),
                ).fetchall()]
            self._json({"members": members})
            return True
        return False

    def post_team(self, path: str, payload: dict[str, Any], session: Record) -> bool:
        """Handle one workspace POST; True when answered."""
        if path == "/api/workspaces":
            name = str(payload.get("name", "")).strip()
            if not name or len(name) > MAX_WORKSPACE_NAME_LENGTH:
                self._json({"error": f"Use a workspace name of 1 to {MAX_WORKSPACE_NAME_LENGTH} characters."}, HTTPStatus.BAD_REQUEST); return True
            with db() as connection:
                workspace_id = create_workspace(connection, name, session["user_id"])
                connection.execute("UPDATE sessions SET active_workspace_id = ? WHERE id = ?", (workspace_id, session["id"]))
            audit(session["user_id"], "workspace.created", "workspace", workspace_id, f"Created workspace {name}", self._source_ip(), workspace_id=workspace_id)
            self._json({"id": workspace_id, "name": name, "role": "owner"}, HTTPStatus.CREATED); return True
        if path == "/api/workspaces/members":
            workspace_id = self._require_workspace(session, "admin")
            if workspace_id is None:
                return True
            username, member_role = str(payload.get("username", "")).strip(), str(payload.get("role", "editor")).strip()
            if member_role not in {"viewer", "editor", "admin"}:
                self._json({"error": "Grant the viewer, editor, or admin role."}, HTTPStatus.BAD_REQUEST); return True
            if member_role == "admin" and session["workspace_role"] != "owner":
                self._json({"error": "Only the workspace owner can grant the admin role."}, HTTPStatus.FORBIDDEN); return True
            with db() as connection:
                user = connection.execute("SELECT id, username FROM users WHERE username = ? AND is_active = 1", (username,)).fetchone()
                if user is None:
                    self._json({"error": "No active user has that username. An administrator can create the account first."}, HTTPStatus.NOT_FOUND); return True
                if connection.execute("SELECT id FROM workspace_memberships WHERE workspace_id = ? AND user_id = ?", (workspace_id, user["id"])).fetchone():
                    self._json({"error": "That user is already a member of this workspace."}, HTTPStatus.CONFLICT); return True
                enforce_member_limit(connection, workspace_id)
                connection.execute("INSERT INTO workspace_memberships (workspace_id, user_id, role, invite_state, created_at, updated_at) VALUES (?, ?, ?, 'active', ?, ?)", (workspace_id, user["id"], member_role, now(), now()))
            audit(session["user_id"], "workspace.member_added", "user", user["id"], f"Added {username} as workspace {member_role}", self._source_ip(), workspace_id=workspace_id)
            self._json({"user_id": user["id"], "username": user["username"], "role": member_role}, HTTPStatus.CREATED); return True
        if path.startswith("/api/workspaces/members/") and path.endswith("/role"):
            workspace_id = self._require_workspace(session, "admin")
            if workspace_id is None:
                return True
            member_user_id, member_role = int(path.split("/")[4]), str(payload.get("role", "")).strip()
            if member_role not in {"viewer", "editor", "admin"}:
                self._json({"error": "Grant the viewer, editor, or admin role."}, HTTPStatus.BAD_REQUEST); return True
            if member_user_id == session["user_id"]:
                self._json({"error": "You cannot change your own workspace role."}, HTTPStatus.BAD_REQUEST); return True
            with db() as connection:
                membership = connection.execute("SELECT id, role FROM workspace_memberships WHERE workspace_id = ? AND user_id = ?", (workspace_id, member_user_id)).fetchone()
                if membership is None:
                    self._json({"error": "That user is not a member of this workspace."}, HTTPStatus.NOT_FOUND); return True
                if membership["role"] == "owner":
                    self._json({"error": "The workspace owner's role cannot be changed."}, HTTPStatus.BAD_REQUEST); return True
                if session["workspace_role"] != "owner" and (membership["role"] == "admin" or member_role == "admin"):
                    self._json({"error": "Only the workspace owner can change admin memberships."}, HTTPStatus.FORBIDDEN); return True
                connection.execute("UPDATE workspace_memberships SET role = ?, updated_at = ? WHERE id = ?", (member_role, now(), membership["id"]))
            audit(session["user_id"], "workspace.member_role_changed", "user", member_user_id, f"Changed workspace role to {member_role}", self._source_ip(), workspace_id=workspace_id)
            self._json({"user_id": member_user_id, "role": member_role}); return True
        if path.startswith("/api/workspaces/members/") and path.endswith("/remove"):
            workspace_id = self._require_workspace(session, "admin")
            if workspace_id is None:
                return True
            member_user_id = int(path.split("/")[4])
            if member_user_id == session["user_id"]:
                self._json({"error": "You cannot remove yourself from a workspace."}, HTTPStatus.BAD_REQUEST); return True
            with db() as connection:
                membership = connection.execute("SELECT id, role FROM workspace_memberships WHERE workspace_id = ? AND user_id = ?", (workspace_id, member_user_id)).fetchone()
                if membership is None:
                    self._json({"error": "That user is not a member of this workspace."}, HTTPStatus.NOT_FOUND); return True
                if membership["role"] == "owner":
                    self._json({"error": "The workspace owner cannot be removed."}, HTTPStatus.BAD_REQUEST); return True
                if session["workspace_role"] != "owner" and membership["role"] == "admin":
                    self._json({"error": "Only the workspace owner can remove an admin."}, HTTPStatus.FORBIDDEN); return True
                connection.execute("DELETE FROM workspace_memberships WHERE id = ?", (membership["id"],))
            audit(session["user_id"], "workspace.member_removed", "user", member_user_id, "Removed workspace member", self._source_ip(), workspace_id=workspace_id)
            self._json({"status": "removed"}); return True
        if path == "/api/workspaces/settings":
            workspace_id = self._require_workspace(session, "owner")
            if workspace_id is None:
                return True
            cap = payload.get("ai_monthly_cap")
            if cap is not None and (not isinstance(cap, int) or isinstance(cap, bool) or cap < 0 or cap > 1_000_000):
                self._json({"error": "ai_monthly_cap must be a whole number of AI actions, or null to remove the cap."}, HTTPStatus.BAD_REQUEST); return True
            with db() as connection:
                save_workspace_setting(connection, workspace_id, "ai_monthly_cap", str(cap) if cap is not None else None)
            audit(session["user_id"], "workspace.settings_changed", "workspace", workspace_id, f"Set monthly AI cap to {cap}", self._source_ip(), workspace_id=workspace_id)
            self._json({"ai_monthly_cap": cap}); return True
        if path == "/api/workspaces/delete":
            workspace_id = self._require_workspace(session, "owner")
            if workspace_id is None:
                return True
            with db() as connection:
                if len(user_workspaces(connection, session["user_id"])) < 2:
                    self._json({"error": "Create or join another workspace before deleting your only workspace."}, HTTPStatus.BAD_REQUEST); return True
                connection.execute("UPDATE workspaces SET status = 'deleted', updated_at = ? WHERE id = ?", (now(), workspace_id))
                connection.execute("UPDATE connections SET is_active = 0 WHERE workspace_id = ?", (workspace_id,))
                # Stop the worker from delivering queued content for a deleted tenant.
                connection.execute("UPDATE posts SET state = 'draft', scheduled_for = NULL WHERE workspace_id = ? AND state = 'scheduled'", (workspace_id,))
            audit(session["user_id"], "workspace.deleted", "workspace", workspace_id, "Soft-deleted workspace, disabled its connections, and unscheduled queued posts", self._source_ip(), workspace_id=workspace_id)
            self._json({"status": "deleted"}); return True
        if path == "/api/workspaces/invitations":
            workspace_id = self._require_workspace(session, "admin")
            if workspace_id is None:
                return True
            email, invite_role = str(payload.get("email", "")).strip().lower(), str(payload.get("role", "editor")).strip()
            if len(email) > 254 or not EMAIL_PATTERN.fullmatch(email):
                self._json({"error": "Enter a valid invitation email address."}, HTTPStatus.BAD_REQUEST); return True
            if invite_role not in {"viewer", "editor", "admin"}:
                self._json({"error": "Grant the viewer, editor, or admin role."}, HTTPStatus.BAD_REQUEST); return True
            if invite_role == "admin" and session["workspace_role"] != "owner":
                self._json({"error": "Only the workspace owner can grant the admin role."}, HTTPStatus.FORBIDDEN); return True
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
            self._json({"id": invitation_id, "email": email, "role": invite_role, "expires_at": expires, "invite_url": link, "email_sent": email_sent}, HTTPStatus.CREATED); return True
        if path.startswith("/api/workspaces/invitations/") and path.endswith("/revoke"):
            workspace_id = self._require_workspace(session, "admin")
            if workspace_id is None:
                return True
            invitation_id = int(path.split("/")[4])
            with db() as connection:
                removed = connection.execute("DELETE FROM workspace_invitations WHERE id = ? AND workspace_id = ? AND accepted_at IS NULL", (invitation_id, workspace_id))
            if removed.rowcount != 1:
                self._json({"error": "Invitation not found."}, HTTPStatus.NOT_FOUND); return True
            audit(session["user_id"], "workspace.invitation_revoked", "workspace", invitation_id, "Revoked pending invitation", self._source_ip(), workspace_id=workspace_id)
            self._json({"status": "revoked"}); return True
        return False

