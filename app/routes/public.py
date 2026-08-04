"""Routes reachable without a session: setup, sign-in, invitations, SSO, health."""


from __future__ import annotations


import base64
import hashlib
import json
import secrets
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from http import HTTPStatus
from typing import Any
from urllib.error import HTTPError
from urllib.error import URLError
from urllib.parse import parse_qs
from urllib.parse import unquote
from urllib.parse import urlencode
from urllib.parse import urlparse

try:  # package import (tests, `python -m app.server`)
    from .. import http_client
    from ..audit import audit
    from ..billing import apply_billing_event, verify_stripe_signature
    from ..config import AUTH_REQUESTS_PER_MINUTE, OIDC_STATE_SECONDS, config, deployment_mode, now, self_signup_allowed
    from ..connections import save_social_connections
    from ..database import db, insert_id
    from ..errors import ProviderError
    from ..invitations import invitation_by_token, invitation_is_usable
    from ..oauth import oidc_redirect_uri, oidc_settings, social_oauth_connections, social_oauth_settings, verify_oidc_id_token
    from ..plans import enforce_member_limit
    from ..publishing import worker_healthy
    from ..security import hash_password
    from ..workspaces import create_local_user, default_workspace_id, ensure_personal_workspace, user_workspaces
except ImportError:  # script import (`python /app/app/server.py`)
    import http_client
    from audit import audit
    from billing import apply_billing_event, verify_stripe_signature
    from config import AUTH_REQUESTS_PER_MINUTE, OIDC_STATE_SECONDS, config, deployment_mode, now, self_signup_allowed
    from connections import save_social_connections
    from database import db, insert_id
    from errors import ProviderError
    from invitations import invitation_by_token, invitation_is_usable
    from oauth import oidc_redirect_uri, oidc_settings, social_oauth_connections, social_oauth_settings, verify_oidc_id_token
    from plans import enforce_member_limit
    from publishing import worker_healthy
    from security import hash_password
    from workspaces import create_local_user, default_workspace_id, ensure_personal_workspace, user_workspaces


class PublicRoutes:
    """Routes reachable without a session: setup, sign-in, invitations, SSO, health.

    Mixed into ``Handler``; every method returns True once it has answered.
    """



    def get_public(self, path: str) -> bool:
        """Handle one unauthenticated GET; True when answered."""
        if path == "/metrics":
            token = config("SOSOPO_METRICS_TOKEN")
            authorization = self.headers.get("Authorization", "")
            if not token or not secrets.compare_digest(authorization, f"Bearer {token}"):
                self._json({"error": "Not found."}, HTTPStatus.NOT_FOUND); return True
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
            return True
        if path == "/api/setup-status":
            with db() as connection:
                has_user = connection.execute("SELECT 1 FROM users LIMIT 1").fetchone() is not None
            self._json({"setup_required": not has_user, "self_signup": self_signup_allowed() and has_user, "deployment_mode": deployment_mode()})
            return True
        if path.startswith("/api/invitations/") and len(path.split("/")) == 4:
            if not self._allow("auth", AUTH_REQUESTS_PER_MINUTE):
                return True
            token = unquote(path.split("/")[3])
            with db() as connection:
                invitation = invitation_by_token(connection, token)
            if not invitation_is_usable(invitation):
                self._json({"error": "This invitation is invalid, expired, or already used."}, HTTPStatus.NOT_FOUND); return True
            self._json({"workspace": invitation["workspace_name"], "role": invitation["role"], "email": invitation["email"], "expires_at": invitation["expires_at"]})
            return True
        if path == "/api/session":
            session = self._require_auth()
            if session:
                with db() as connection:
                    workspaces = [{"id": item["id"], "name": item["name"], "slug": item["slug"], "role": item["role"], "is_owner": item["owner_user_id"] == session["user_id"]} for item in user_workspaces(connection, session["user_id"])]
                workspace = {"id": session["workspace_id"], "name": session["workspace_name"], "slug": session["workspace_slug"], "role": session["workspace_role"]} if session.get("workspace_id") else None
                self._json({"username": session["username"], "role": session["role"], "timezone": session["timezone"], "signature": session["signature"], "workspace": workspace, "workspaces": workspaces, "csrf_token": session["csrf_token"]})
            return True
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
            return True
        if path == "/api/auth/oidc/callback":
            values = parse_qs(urlparse(self.path).query)
            state, code = values.get("state", [""])[0], values.get("code", [""])[0]
            if not state or not code:
                self._json({"error": "Invalid SSO callback."}, HTTPStatus.BAD_REQUEST); return True
            with db() as connection:
                stored = connection.execute("SELECT * FROM oidc_states WHERE state = ? AND expires_at > ?", (state, now())).fetchone()
                connection.execute("DELETE FROM oidc_states WHERE state = ?", (state,))
            if stored is None:
                self._json({"error": "Expired or invalid SSO state."}, HTTPStatus.BAD_REQUEST); return True
            try:
                settings = oidc_settings()
                token = http_client.request_form(settings["token_endpoint"], {"grant_type": "authorization_code", "code": code, "redirect_uri": oidc_redirect_uri(), "client_id": settings["client_id"], "client_secret": settings["client_secret"], "code_verifier": stored["code_verifier"]})
                identity = verify_oidc_id_token(token.get("id_token"), settings, stored["nonce"])
                subject = str(identity.get("sub", ""))
                username = str(identity.get("preferred_username") or identity.get("email") or subject).strip()
                if not subject or not username:
                    raise ProviderError("SSO provider did not return a usable identity.")
            except (ProviderError, HTTPError, URLError, json.JSONDecodeError) as error:
                self._json({"error": str(error) or "SSO sign-in failed."}, HTTPStatus.UNAUTHORIZED); return True
            with db() as connection:
                user = connection.execute("SELECT * FROM users WHERE oidc_issuer = ? AND oidc_subject = ?", (settings["issuer"], subject)).fetchone()
                if user is None:
                    if config("OIDC_ALLOW_SIGNUP").lower() not in {"1", "true", "yes"}:
                        self._json({"error": "Your SSO account has not been provisioned."}, HTTPStatus.FORBIDDEN); return True
                    salt = secrets.token_bytes(16)
                    user_id = insert_id(connection, "INSERT INTO users (username, password_salt, password_hash, role, timezone, oidc_issuer, oidc_subject, created_at) VALUES (?, ?, ?, 'user', 'UTC', ?, ?, ?)", (username, salt.hex(), hash_password(secrets.token_urlsafe(32), salt), settings["issuer"], subject, now()))
                    ensure_personal_workspace(connection, user_id, username)
                    user = connection.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
                elif not user["is_active"]:
                    self._json({"error": "This user account is disabled."}, HTTPStatus.FORBIDDEN); return True
            self._redirect_session("/", self._create_session(user["id"])); return True
        if path == "/api/social-oauth/callback":
            values = parse_qs(urlparse(self.path).query)
            provider, state, code = values.get("provider", [""])[0], values.get("state", [""])[0], values.get("code", [""])[0]
            if provider not in {"Facebook", "Threads", "X", "LinkedIn", "Discord"} or not state or not code:
                self._json({"error": "Invalid social account callback."}, HTTPStatus.BAD_REQUEST); return True
            with db() as connection:
                stored = connection.execute("SELECT * FROM social_oauth_states WHERE state = ? AND provider = ? AND expires_at > ?", (state, provider, now())).fetchone()
                connection.execute("DELETE FROM social_oauth_states WHERE state = ?", (state,))
            if stored is None:
                self._json({"error": "Expired or invalid social account connection state."}, HTTPStatus.BAD_REQUEST); return True
            try:
                workspace_id = stored["workspace_id"]
                if workspace_id is None:
                    # A state row created before the workspace upgrade: use the
                    # connecting user's default workspace.
                    with db() as connection:
                        workspace_id = default_workspace_id(connection, stored["user_id"])
                if workspace_id is None:
                    self._json({"error": "Join an active workspace before connecting accounts."}, HTTPStatus.FORBIDDEN); return True
                records = social_oauth_connections(provider, social_oauth_settings(provider), code, stored["code_verifier"])
                saved = save_social_connections(stored["user_id"], int(workspace_id), records)
                audit(stored["user_id"], "connection.oauth_connected", "connection", None, f"Connected {saved} account(s) through {provider} OAuth", self._source_ip(), workspace_id=int(workspace_id))
                self.send_response(HTTPStatus.FOUND); self.send_header("Location", "/?connected=" + urlencode({"provider": provider, "accounts": saved})); self.end_headers()
            except ProviderError as error:
                self._json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
            return True
        if path == "/api/health":
            try:
                with db() as connection:
                    connection.execute("SELECT 1")
                self._json({"status": "ok"})
            except Exception:
                self._json({"status": "unhealthy"}, HTTPStatus.SERVICE_UNAVAILABLE)
            return True
        return False

    def post_public(self, path: str, payload: dict[str, Any]) -> bool:
        """Handle one unauthenticated POST; True when answered."""
        if path == "/api/setup":
            if not self._allow("auth", AUTH_REQUESTS_PER_MINUTE): return True
            username, password = str(payload.get("username", "")).strip(), str(payload.get("password", ""))
            if len(username) < 3 or len(password) < 12:
                self._json({"error": "Use a username of at least 3 characters and a password of at least 12 characters."}, HTTPStatus.BAD_REQUEST); return True
            with db() as connection:
                if connection.execute("SELECT 1 FROM users LIMIT 1").fetchone():
                    self._json({"error": "Setup has already been completed."}, HTTPStatus.CONFLICT); return True
                try:
                    connection.execute("INSERT INTO instance_settings (name, value) VALUES ('initial_setup', ?)", (now(),))
                except Exception:
                    self._json({"error": "Setup has already been completed."}, HTTPStatus.CONFLICT); return True
                salt = secrets.token_bytes(16)
                user_id = insert_id(connection, "INSERT INTO users (username, password_salt, password_hash, role, timezone, created_at) VALUES (?, ?, ?, ?, 'UTC', ?)", (username, salt.hex(), hash_password(password, salt), "admin", now()))
                workspace_id = ensure_personal_workspace(connection, user_id, username)
                connection.execute("UPDATE posts SET user_id = ?, workspace_id = ? WHERE user_id IS NULL", (user_id, workspace_id))
            audit(user_id, "setup.completed", "user", user_id, "Created initial administrator", self._source_ip())
            self._json_session({"username": username}, self._create_session(user_id), HTTPStatus.CREATED); return True
        if path == "/api/login":
            if not self._allow("auth", AUTH_REQUESTS_PER_MINUTE): return True
            username, password = str(payload.get("username", "")).strip(), str(payload.get("password", ""))
            with db() as connection:
                user = connection.execute("SELECT * FROM users WHERE username = ? AND is_active = 1", (username,)).fetchone()
            if user is None or not secrets.compare_digest(hash_password(password, bytes.fromhex(user["password_salt"])), user["password_hash"]):
                self._json({"error": "Invalid username or password."}, HTTPStatus.UNAUTHORIZED); return True
            self._json_session({"username": user["username"]}, self._create_session(user["id"])); return True
        if path == "/api/signup":
            if not self._allow("auth", AUTH_REQUESTS_PER_MINUTE): return True
            if not self_signup_allowed():
                self._json({"error": "Self-service signup is disabled on this Sosopo instance."}, HTTPStatus.FORBIDDEN); return True
            username, password = str(payload.get("username", "")).strip(), str(payload.get("password", ""))
            if len(username) < 3 or len(password) < 12:
                self._json({"error": "Use a username of at least 3 characters and a password of at least 12 characters."}, HTTPStatus.BAD_REQUEST); return True
            with db() as connection:
                if connection.execute("SELECT 1 FROM users LIMIT 1").fetchone() is None:
                    self._json({"error": "Complete first-run setup before self-service signup."}, HTTPStatus.CONFLICT); return True
                if connection.execute("SELECT 1 FROM users WHERE username = ?", (username,)).fetchone():
                    self._json({"error": "That username is already taken."}, HTTPStatus.CONFLICT); return True
                user_id = create_local_user(connection, username, password)
                ensure_personal_workspace(connection, user_id, username)
            audit(user_id, "user.signed_up", "user", user_id, "Self-service signup", self._source_ip())
            self._json_session({"username": username}, self._create_session(user_id), HTTPStatus.CREATED); return True
        if path.startswith("/api/invitations/") and path.endswith("/accept"):
            if not self._allow("auth", AUTH_REQUESTS_PER_MINUTE): return True
            token = unquote(path.split("/")[3])
            session = self._session()
            with db() as connection:
                invitation = invitation_by_token(connection, token)
                if not invitation_is_usable(invitation):
                    self._json({"error": "This invitation is invalid, expired, or already used."}, HTTPStatus.BAD_REQUEST); return True
                if session is not None:
                    user_id, username = int(session["user_id"]), str(session["username"])
                else:
                    username, password = str(payload.get("username", "")).strip(), str(payload.get("password", ""))
                    if len(username) < 3 or len(password) < 12:
                        self._json({"error": "Use a username of at least 3 characters and a password of at least 12 characters."}, HTTPStatus.BAD_REQUEST); return True
                    if connection.execute("SELECT 1 FROM users WHERE username = ?", (username,)).fetchone():
                        self._json({"error": "That username is already taken. Sign in first, then open the invite link again."}, HTTPStatus.CONFLICT); return True
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
            return True
        return False

    def post_logout(self, path: str) -> bool:
        """Handle sign-out, which authenticates itself; True when answered."""
        if path == "/api/logout":
            session = self._require_auth(csrf=True)
            if session is None: return True
            with db() as connection:
                connection.execute("DELETE FROM sessions WHERE id = ?", (session["id"],))
            self._json({"status": "logged out"}); return True
        return False

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
