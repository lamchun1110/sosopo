"""Connection health, token refresh, and privacy-workflow regression tests."""

from __future__ import annotations

import os
import unittest
from datetime import UTC, datetime, timedelta

try:
    from tests.test_workspaces import WorkspaceHttpCase, WorkspaceTestCase
except ImportError:
    from test_workspaces import WorkspaceHttpCase, WorkspaceTestCase


class ConnectionHealthTest(WorkspaceTestCase):
    def test_connection_health_states(self) -> None:
        s = self.server
        soon = (datetime.now(UTC) + timedelta(days=2)).isoformat()
        later = (datetime.now(UTC) + timedelta(days=30)).isoformat()
        past = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
        self.assertEqual(s.connection_health({"is_active": 1, "token_expires_at": None}), "active")
        self.assertEqual(s.connection_health({"is_active": 1, "token_expires_at": later}), "active")
        self.assertEqual(s.connection_health({"is_active": 1, "token_expires_at": soon}), "expiring_soon")
        self.assertEqual(s.connection_health({"is_active": 1, "token_expires_at": past}), "expired")
        self.assertEqual(s.connection_health({"is_active": 0, "token_expires_at": later}), "disabled")

    def test_oauth_flow_stores_refresh_token_encrypted(self) -> None:
        s = self.server
        os.environ["SOSOPO_PUBLIC_URL"] = "https://sosopo.example.test"
        original_request_form, original_request_get_json = s.request_form, s.request_get_json
        s.request_form = lambda url, payload, headers=None: {"access_token": "new-access", "refresh_token": "refresh-secret", "expires_in": 7200}
        s.request_get_json = lambda url, headers=None: {"data": {"id": "42", "username": "handle"}}
        try:
            records = s.social_oauth_connections("X", {"client_id": "client", "client_secret": "secret", "token": "https://x.example/token"}, "code", "verifier")
        finally:
            s.request_form, s.request_get_json = original_request_form, original_request_get_json
            os.environ.pop("SOSOPO_PUBLIC_URL", None)
        self.assertEqual(records[0]["refresh_token"], "refresh-secret")
        with s.db() as connection:
            user_id = s.insert_id(connection, "INSERT INTO users (username, password_salt, password_hash, role, timezone, created_at) VALUES (?, ?, ?, 'user', 'UTC', ?)", ("owner", "salt", "hash", s.now()))
            workspace_id = s.create_workspace(connection, "Space", user_id)
        s.save_social_connections(user_id, workspace_id, records)
        with s.db() as connection:
            row = connection.execute("SELECT encrypted_secrets FROM connections WHERE workspace_id = ?", (workspace_id,)).fetchone()
        secrets_map = s.decrypt_secrets(row["encrypted_secrets"])
        self.assertEqual(secrets_map["refresh_token"], "refresh-secret")
        self.assertNotIn("refresh-secret", row["encrypted_secrets"])

    def test_expiring_tokens_are_refreshed_by_the_worker_task(self) -> None:
        s = self.server
        os.environ.update({"X_OAUTH_CLIENT_ID": "client", "X_OAUTH_CLIENT_SECRET": "secret"})
        with s.db() as connection:
            user_id = s.insert_id(connection, "INSERT INTO users (username, password_salt, password_hash, role, timezone, created_at) VALUES (?, ?, ?, 'user', 'UTC', ?)", ("owner", "salt", "hash", s.now()))
            workspace_id = s.create_workspace(connection, "Space", user_id)
            expiring = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
            s.insert_id(connection, "INSERT INTO connections (user_id, workspace_id, provider, external_account_id, display_name, encrypted_secrets, settings_json, token_expires_at, created_at) VALUES (?, ?, 'X', 'profile', 'X profile', ?, '{}', ?, ?)", (user_id, workspace_id, s.encrypt_secrets({"access_token": "old-access", "refresh_token": "old-refresh"}), expiring, s.now()))
        calls: list[dict] = []
        original_request_form = s.request_form
        s.request_form = lambda url, payload, headers=None: calls.append(payload) or {"access_token": "rotated-access", "refresh_token": "rotated-refresh", "expires_in": 7200}
        try:
            refreshed = s.refresh_expiring_connection_tokens()
        finally:
            s.request_form = original_request_form
            os.environ.pop("X_OAUTH_CLIENT_ID", None)
            os.environ.pop("X_OAUTH_CLIENT_SECRET", None)
        self.assertEqual(refreshed, 1)
        self.assertEqual(calls[0]["grant_type"], "refresh_token")
        self.assertEqual(calls[0]["refresh_token"], "old-refresh")
        with s.db() as connection:
            row = connection.execute("SELECT encrypted_secrets, token_expires_at FROM connections WHERE workspace_id = ?", (workspace_id,)).fetchone()
        secrets_map = s.decrypt_secrets(row["encrypted_secrets"])
        self.assertEqual(secrets_map["access_token"], "rotated-access")
        self.assertEqual(secrets_map["refresh_token"], "rotated-refresh")
        self.assertGreater(str(row["token_expires_at"]), (datetime.now(UTC) + timedelta(minutes=90)).isoformat())

    def test_connections_without_refresh_material_are_skipped(self) -> None:
        s = self.server
        with s.db() as connection:
            user_id = s.insert_id(connection, "INSERT INTO users (username, password_salt, password_hash, role, timezone, created_at) VALUES (?, ?, ?, 'user', 'UTC', ?)", ("owner", "salt", "hash", s.now()))
            workspace_id = s.create_workspace(connection, "Space", user_id)
            expiring = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
            s.insert_id(connection, "INSERT INTO connections (user_id, workspace_id, provider, external_account_id, display_name, encrypted_secrets, settings_json, token_expires_at, created_at) VALUES (?, ?, 'X', 'profile', 'X profile', ?, '{}', ?, ?)", (user_id, workspace_id, s.encrypt_secrets({"access_token": "only-access"}), expiring, s.now()))
        self.assertEqual(s.refresh_expiring_connection_tokens(), 0)


class PrivacyWorkflowTest(WorkspaceHttpCase):
    def test_workspace_export_contains_content_but_never_secrets(self) -> None:
        admin = self.setup_admin()
        status, account, _ = self.request("POST", "/api/connections", {"provider": "Telegram", "external_account_id": "-9", "display_name": "channel", "secrets": {"bot_token": "super-secret-token"}}, admin)
        self.assertEqual(status, 201)
        status, export, _ = self.request("GET", "/api/workspaces/export", auth=admin)
        self.assertEqual(status, 200)
        self.assertEqual(export["connections"][0]["id"], account["id"])
        self.assertNotIn("encrypted_secrets", export["connections"][0])
        self.assertNotIn("super-secret-token", str(export))
        self.assertTrue(any(post["body"].startswith("Welcome") for post in export["posts"]))
        bob = self.create_and_login(admin, "bob")
        status, _, _ = self.request("GET", "/api/workspaces/export", auth=bob)
        self.assertEqual(status, 200)  # bob exports his own personal workspace
        status, bob_export, _ = self.request("GET", "/api/workspaces/export", auth=bob)
        self.assertEqual(bob_export["connections"], [])

    def test_workspace_deletion_disables_delivery_and_falls_back(self) -> None:
        admin = self.setup_admin()
        personal = self.active_workspace(admin)["workspace"]["id"]
        status, created, _ = self.request("POST", "/api/workspaces", {"name": "Disposable"}, admin)
        self.assertEqual(status, 201)
        status, account, _ = self.request("POST", "/api/connections", {"provider": "Telegram", "external_account_id": "-8", "display_name": "channel", "secrets": {"bot_token": "x"}}, admin)
        self.assertEqual(status, 201)
        status, post, _ = self.request("POST", "/api/posts", {"body": "queued", "channels": ["Telegram"], "connection_ids": [account["id"]], "scheduled_for": "2030-01-01T09:30", "scheduled_timezone": "UTC"}, admin)
        self.assertEqual(status, 201)
        status, _, _ = self.request("POST", "/api/workspaces/delete", {}, admin)
        self.assertEqual(status, 200)
        session = self.active_workspace(admin)
        self.assertEqual(session["workspace"]["id"], personal)
        self.assertEqual([space["id"] for space in session["workspaces"]], [personal])
        with self.server.db() as connection:
            row = connection.execute("SELECT state, scheduled_for FROM posts WHERE id = ?", (post["id"],)).fetchone()
            disabled = connection.execute("SELECT is_active FROM connections WHERE id = ?", (account["id"],)).fetchone()
        self.assertEqual(row["state"], "draft")
        self.assertIsNone(row["scheduled_for"])
        self.assertEqual(disabled["is_active"], 0)

    def test_workspace_deletion_requires_owner_and_a_remaining_workspace(self) -> None:
        admin = self.setup_admin()
        status, _, _ = self.request("POST", "/api/workspaces/delete", {}, admin)
        self.assertEqual(status, 400)
        admin_workspace = self.active_workspace(admin)["workspace"]["id"]
        bob = self.create_and_login(admin, "bob")
        status, _, _ = self.request("POST", "/api/workspaces/members", {"username": "bob", "role": "admin"}, admin)
        self.assertEqual(status, 201)
        status, _, _ = self.request("POST", "/api/me/workspace", {"workspace_id": admin_workspace}, bob)
        self.assertEqual(status, 200)
        status, _, _ = self.request("POST", "/api/workspaces/delete", {}, bob)
        self.assertEqual(status, 403)


if __name__ == "__main__":
    unittest.main()
