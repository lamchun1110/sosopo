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
    from ..ai_providers import generate_workspace_summary
    from ..audit import audit
    from ..insights import MAX_SUMMARY_LENGTH, summary_prompt, workspace_status
    from ..brand_voice import load_brand_voice, save_brand_voice, validated_profile
    from ..credits import account_balance, allocate_credits, charge_ai_credit, credits_enforced, funding_chain
    from ..config import EMAIL_PATTERN, INVITATION_SECONDS, MAX_WORKSPACE_NAME_LENGTH, now
    from ..errors import ProviderError
    from ..database import Record, db, insert_id
    from ..invitations import invitation_url, send_email
    from ..plans import enforce_member_limit, enforce_monthly_quota, record_usage
    from ..workspaces import create_workspace, save_workspace_setting, user_workspaces, workspace_membership, workspace_role_allows
except ImportError:  # script import (`python /app/app/server.py`)
    from ai_providers import generate_workspace_summary
    from audit import audit
    from insights import MAX_SUMMARY_LENGTH, summary_prompt, workspace_status
    from brand_voice import load_brand_voice, save_brand_voice, validated_profile
    from credits import account_balance, allocate_credits, charge_ai_credit, credits_enforced, funding_chain
    from config import EMAIL_PATTERN, INVITATION_SECONDS, MAX_WORKSPACE_NAME_LENGTH, now
    from errors import ProviderError
    from database import Record, db, insert_id
    from invitations import invitation_url, send_email
    from plans import enforce_member_limit, enforce_monthly_quota, record_usage
    from workspaces import create_workspace, save_workspace_setting, user_workspaces, workspace_membership, workspace_role_allows


def credit_amount(value: object) -> int | None:
    """Accept only a positive whole number; reject floats, text, and negatives."""
    return int(value) if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


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
                self._json(workspace_status(connection, workspace_id, since))
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
        if path == "/api/workspaces/brand-voice":
            session = self._session()
            workspace_id = self._require_workspace(session)
            if workspace_id is None:
                return True
            with db() as connection:
                profile = load_brand_voice(connection, workspace_id)
            self._json({"profile": profile, "configured": profile is not None, "editable": workspace_role_allows(session["workspace_role"], "admin")}); return True
        if path == "/api/workspaces/credits":
            session = self._session()
            workspace_id = self._require_workspace(session)
            if workspace_id is None:
                return True
            with db() as connection:
                accounts = [{"owner_type": owner_type, "owner_id": owner_id, "balance": account_balance(connection, owner_type, owner_id)}
                            for owner_type, owner_id in funding_chain(connection, workspace_id, session["user_id"])]
            self._json({"enforced": credits_enforced(), "accounts": accounts}); return True
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
        if path == "/api/workspaces/summary":
            workspace_id = self._require_workspace(session, "admin")
            if workspace_id is None:
                return True
            since = (datetime.now(UTC) - timedelta(days=30)).isoformat()
            with db() as connection:
                enforce_monthly_quota(connection, workspace_id, "ai_generations", "ai_generations_per_month", "AI text generations")
                charge_ai_credit(connection, workspace_id, "ai_summary", session["user_id"])
                status = workspace_status(connection, workspace_id, since)
            try:
                summary = generate_workspace_summary(str(payload.get("provider", "")).strip(), str(payload.get("model", "")).strip(), summary_prompt(status), workspace_id)
            except ProviderError as error:
                self._json({"error": str(error)}, HTTPStatus.BAD_GATEWAY); return True
            with db() as connection:
                record_usage(connection, workspace_id, "ai_generations")
            audit(session["user_id"], "workspace.summarized", "workspace", workspace_id, "Generated an AI workspace summary", self._source_ip(), workspace_id=workspace_id)
            self._json({"summary": summary[:MAX_SUMMARY_LENGTH], "ai_generated": True, "period": status["period"]}); return True
        if path == "/api/workspaces/brand-voice":
            workspace_id = self._require_workspace(session, "admin")
            if workspace_id is None:
                return True
            raw = payload.get("profile")
            profile = None if raw in (None, {}) else validated_profile(raw)
            with db() as connection:
                save_brand_voice(connection, workspace_id, profile)
            audit(session["user_id"], "workspace.brand_voice_saved", "workspace", workspace_id,
                  "Saved the brand voice profile" if profile else "Cleared the brand voice profile", self._source_ip(), workspace_id=workspace_id)
            self._json({"profile": profile, "configured": profile is not None}); return True
        if path == "/api/workspaces/credits/allocate":
            workspace_id = self._require_workspace(session, "admin")
            if workspace_id is None:
                return True
            amount, target_id = credit_amount(payload.get("amount")), credit_amount(payload.get("target_id"))
            if amount is None or target_id is None:
                self._json({"error": "Use a positive whole number of credits and a valid member."}, HTTPStatus.BAD_REQUEST); return True
            with db() as connection:
                if workspace_membership(connection, workspace_id, target_id) is None:
                    self._json({"error": "Not found."}, HTTPStatus.NOT_FOUND); return True
                try:
                    allocate_credits(connection, ("workspace", workspace_id), ("user", target_id), amount, session["user_id"])
                except ProviderError as error:
                    self._json({"error": str(error)}, HTTPStatus.BAD_REQUEST); return True
                balance = account_balance(connection, "workspace", workspace_id)
            audit(session["user_id"], "credits.allocated", "workspace", workspace_id, f"Allocated {amount} credits to user {target_id}", self._source_ip(), workspace_id=workspace_id)
            self._json({"balance": balance, "target_type": "user", "target_id": target_id, "amount": amount}); return True
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

