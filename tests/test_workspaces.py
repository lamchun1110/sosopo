"""Workspace migration, tenant-isolation, and role-permission regression tests."""

from __future__ import annotations

import http.client
import importlib
import json
import os
import shutil
import tempfile
import threading
import unittest
from http.cookies import SimpleCookie
from pathlib import Path


class WorkspaceTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = Path(tempfile.mkdtemp(prefix="sosopo-workspace-test-"))
        os.environ["SOSOPO_DATA_DIR"] = str(self.directory)
        os.environ["SOSOPO_ENCRYPTION_KEY"] = "Gd0EwA9sy_00SUdECwYyWEnyx3axpfAP7jSEWo2-YIE="
        import app.server as imported_server
        self.server = importlib.reload(imported_server)
        self.server.setup_database()

    def tearDown(self) -> None:
        shutil.rmtree(self.directory, ignore_errors=True)
        os.environ.pop("SOSOPO_DATA_DIR", None)
        os.environ.pop("SOSOPO_ENCRYPTION_KEY", None)

    def create_legacy_user(self, username: str) -> int:
        s = self.server
        with s.db() as connection:
            return s.insert_id(connection, "INSERT INTO users (username, password_salt, password_hash, role, timezone, created_at) VALUES (?, ?, ?, 'user', 'UTC', ?)", (username, "salt", "hash", s.now()))


class WorkspaceMigrationTest(WorkspaceTestCase):
    def test_each_existing_user_gets_an_isolated_personal_workspace(self) -> None:
        s = self.server
        alice, bob = self.create_legacy_user("alice"), self.create_legacy_user("bob")
        with s.db() as connection:
            alice_post = s.insert_id(connection, "INSERT INTO posts (user_id, body, channel, state, created_at) VALUES (?, 'alice draft', 'X', 'draft', ?)", (alice, s.now()))
            bob_account = s.insert_id(connection, "INSERT INTO connections (user_id, provider, external_account_id, display_name, encrypted_secrets, settings_json, created_at) VALUES (?, 'Telegram', '-1', 'bob bot', ?, '{}', ?)", (bob, s.encrypt_secrets({"bot_token": "x"}), s.now()))
        s.setup_database()
        with s.db() as connection:
            alice_spaces, bob_spaces = s.user_workspaces(connection, alice), s.user_workspaces(connection, bob)
            self.assertEqual([len(alice_spaces), len(bob_spaces)], [1, 1])
            self.assertNotEqual(alice_spaces[0]["id"], bob_spaces[0]["id"])
            self.assertEqual({alice_spaces[0]["role"], bob_spaces[0]["role"]}, {"owner"})
            post = connection.execute("SELECT workspace_id FROM posts WHERE id = ?", (alice_post,)).fetchone()
            account = connection.execute("SELECT workspace_id FROM connections WHERE id = ?", (bob_account,)).fetchone()
        self.assertEqual(post["workspace_id"], alice_spaces[0]["id"])
        self.assertEqual(account["workspace_id"], bob_spaces[0]["id"])

    def test_migration_is_idempotent(self) -> None:
        s = self.server
        user_id = self.create_legacy_user("repeat")
        s.setup_database()
        s.setup_database()
        with s.db() as connection:
            memberships = connection.execute("SELECT COUNT(*) AS count FROM workspace_memberships WHERE user_id = ?", (user_id,)).fetchone()
        self.assertEqual(memberships["count"], 1)

    def test_workspace_slugs_never_collide(self) -> None:
        s = self.server
        user_id = self.create_legacy_user("owner")
        with s.db() as connection:
            first = s.create_workspace(connection, "Marketing Team", user_id)
            second = s.create_workspace(connection, "Marketing Team", user_id)
            slugs = {row["slug"] for row in connection.execute("SELECT slug FROM workspaces WHERE id IN (?, ?)", (first, second)).fetchall()}
        self.assertEqual(len(slugs), 2)

    def test_workspace_role_ranking(self) -> None:
        s = self.server
        self.assertTrue(s.workspace_role_allows("owner", "admin"))
        self.assertTrue(s.workspace_role_allows("editor", "viewer"))
        self.assertFalse(s.workspace_role_allows("viewer", "editor"))
        self.assertFalse(s.workspace_role_allows(None, "viewer"))
        self.assertFalse(s.workspace_role_allows("stranger", "viewer"))


class WorkspaceApiTest(WorkspaceTestCase):
    def setUp(self) -> None:
        super().setUp()
        quiet = type("QuietHandler", (self.server.Handler,), {"log_message": lambda self, *args: None})
        self.httpd = self.server.ThreadingHTTPServer(("127.0.0.1", 0), quiet)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.port = self.httpd.server_address[1]

    def tearDown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        super().tearDown()

    def request(self, method: str, path: str, body: dict | None = None, auth: dict | None = None) -> tuple[int, dict, dict | None]:
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        headers = {"Content-Type": "application/json"}
        if auth:
            headers["Cookie"] = f"sosopo_session={auth['token']}"
            headers["X-CSRF-Token"] = auth["csrf"]
        connection.request(method, path, json.dumps(body) if body is not None else None, headers)
        response = connection.getresponse()
        payload = json.loads(response.read() or b"{}")
        cookie = SimpleCookie()
        for name, value in response.getheaders():
            if name.lower() == "set-cookie":
                cookie.load(value)
        connection.close()
        token = cookie.get("sosopo_session")
        session = {"token": token.value, "csrf": payload.get("csrf_token", "")} if token else None
        return response.status, payload, session

    def setup_admin(self) -> dict:
        status, _, auth = self.request("POST", "/api/setup", {"username": "admin-user", "password": "a-very-long-password"})
        self.assertEqual(status, 201)
        return auth

    def create_and_login(self, admin_auth: dict, username: str) -> dict:
        status, _, _ = self.request("POST", "/api/admin/users", {"username": username, "password": "another-long-password"}, admin_auth)
        self.assertEqual(status, 201)
        status, _, auth = self.request("POST", "/api/login", {"username": username, "password": "another-long-password"})
        self.assertEqual(status, 200)
        return auth

    def active_workspace(self, auth: dict) -> dict:
        status, payload, _ = self.request("GET", "/api/session", auth=auth)
        self.assertEqual(status, 200)
        return payload

    def test_setup_creates_an_owner_workspace_session(self) -> None:
        admin = self.setup_admin()
        payload = self.active_workspace(admin)
        self.assertIsNotNone(payload["workspace"])
        self.assertEqual(payload["workspace"]["role"], "owner")
        self.assertEqual(len(payload["workspaces"]), 1)
        status, dashboard, _ = self.request("GET", "/api/dashboard", auth=admin)
        self.assertEqual(status, 200)
        self.assertTrue(all(post["workspace_id"] == payload["workspace"]["id"] for post in dashboard["posts"]))

    def test_posts_are_isolated_between_workspaces(self) -> None:
        admin = self.setup_admin()
        admin_workspace = self.active_workspace(admin)["workspace"]["id"]
        status, post, _ = self.request("POST", "/api/posts", {"body": "admin only", "channels": ["X"]}, admin)
        self.assertEqual(status, 201)
        bob = self.create_and_login(admin, "bob")
        status, dashboard, _ = self.request("GET", "/api/dashboard", auth=bob)
        self.assertEqual(status, 200)
        self.assertEqual(dashboard["posts"], [])
        status, _, _ = self.request("POST", f"/api/posts/{post['id']}/publish", {}, bob)
        self.assertEqual(status, 404)
        status, _, _ = self.request("POST", f"/api/posts/{post['id']}/schedule", {"scheduled_for": "2030-01-01T09:30", "scheduled_timezone": "UTC"}, bob)
        self.assertEqual(status, 404)
        status, _, _ = self.request("POST", f"/api/posts/{post['id']}/remove", {}, bob)
        self.assertEqual(status, 409)
        status, _, _ = self.request("GET", f"/api/posts/{post['id']}/deliveries", auth=bob)
        self.assertEqual(status, 404)
        status, _, _ = self.request("POST", "/api/me/workspace", {"workspace_id": admin_workspace}, bob)
        self.assertEqual(status, 403)

    def test_viewer_and_editor_roles_are_enforced(self) -> None:
        admin = self.setup_admin()
        admin_workspace = self.active_workspace(admin)["workspace"]["id"]
        bob = self.create_and_login(admin, "bob")
        status, member, _ = self.request("POST", "/api/workspaces/members", {"username": "bob", "role": "viewer"}, admin)
        self.assertEqual(status, 201)
        status, _, _ = self.request("POST", "/api/me/workspace", {"workspace_id": admin_workspace}, bob)
        self.assertEqual(status, 200)
        status, dashboard, _ = self.request("GET", "/api/dashboard", auth=bob)
        self.assertEqual(status, 200)
        self.assertTrue(any(post["body"].startswith("Welcome") for post in dashboard["posts"]))
        status, payload, _ = self.request("POST", "/api/posts", {"body": "viewer post", "channels": ["X"]}, bob)
        self.assertEqual(status, 403)
        status, _, _ = self.request("POST", "/api/connections", {"provider": "Telegram", "external_account_id": "-1", "display_name": "bot", "secrets": {"bot_token": "x"}}, bob)
        self.assertEqual(status, 403)
        status, _, _ = self.request("GET", "/api/workspaces/members", auth=bob)
        self.assertEqual(status, 403)
        status, _, _ = self.request("POST", f"/api/workspaces/members/{member['user_id']}/role", {"role": "editor"}, admin)
        self.assertEqual(status, 200)
        status, post, _ = self.request("POST", "/api/posts", {"body": "editor post", "channels": ["X"]}, bob)
        self.assertEqual(status, 201)
        self.assertEqual(post["workspace_id"], admin_workspace)
        status, _, _ = self.request("POST", "/api/connections", {"provider": "Telegram", "external_account_id": "-1", "display_name": "bot", "secrets": {"bot_token": "x"}}, bob)
        self.assertEqual(status, 403)
        status, _, _ = self.request("POST", "/api/workspaces/members", {"username": "admin-user", "role": "viewer"}, bob)
        self.assertEqual(status, 403)

    def test_workspace_connections_are_shared_with_members_and_scoped(self) -> None:
        admin = self.setup_admin()
        admin_workspace = self.active_workspace(admin)["workspace"]["id"]
        status, account, _ = self.request("POST", "/api/connections", {"provider": "Telegram", "external_account_id": "-100", "display_name": "team channel", "secrets": {"bot_token": "x"}}, admin)
        self.assertEqual(status, 201)
        bob = self.create_and_login(admin, "bob")
        status, _, _ = self.request("POST", "/api/workspaces/members", {"username": "bob", "role": "editor"}, admin)
        self.assertEqual(status, 201)
        status, _, _ = self.request("POST", "/api/me/workspace", {"workspace_id": admin_workspace}, bob)
        self.assertEqual(status, 200)
        status, listed, _ = self.request("GET", "/api/connections", auth=bob)
        self.assertEqual(status, 200)
        self.assertEqual([item["id"] for item in listed["connections"]], [account["id"]])
        status, post, _ = self.request("POST", "/api/posts", {"body": "shared channel", "channels": ["Telegram"], "connection_ids": [account["id"]]}, bob)
        self.assertEqual(status, 201)
        carol = self.create_and_login(admin, "carol")
        status, listed, _ = self.request("GET", "/api/connections", auth=carol)
        self.assertEqual(status, 200)
        self.assertEqual(listed["connections"], [])
        status, _, _ = self.request("POST", f"/api/connections/{account['id']}/disable", {}, carol)
        self.assertEqual(status, 404)
        status, _, _ = self.request("POST", f"/api/connections/{account['id']}/rotate", {"secrets": {"bot_token": "y"}}, carol)
        self.assertEqual(status, 404)

    def test_admin_grants_and_owner_protections(self) -> None:
        admin = self.setup_admin()
        admin_workspace = self.active_workspace(admin)["workspace"]["id"]
        bob = self.create_and_login(admin, "bob")
        status, _, _ = self.request("POST", "/api/workspaces/members", {"username": "bob", "role": "admin"}, admin)
        self.assertEqual(status, 201)
        self.create_and_login(admin, "carol")
        status, _, _ = self.request("POST", "/api/me/workspace", {"workspace_id": admin_workspace}, bob)
        self.assertEqual(status, 200)
        status, _, _ = self.request("POST", "/api/workspaces/members", {"username": "carol", "role": "admin"}, bob)
        self.assertEqual(status, 403)
        status, carol_member, _ = self.request("POST", "/api/workspaces/members", {"username": "carol", "role": "editor"}, bob)
        self.assertEqual(status, 201)
        status, members, _ = self.request("GET", "/api/workspaces/members", auth=bob)
        self.assertEqual(status, 200)
        owner_id = next(member["user_id"] for member in members["members"] if member["role"] == "owner")
        status, _, _ = self.request("POST", f"/api/workspaces/members/{owner_id}/role", {"role": "editor"}, bob)
        self.assertEqual(status, 400)
        status, _, _ = self.request("POST", f"/api/workspaces/members/{owner_id}/remove", {}, bob)
        self.assertEqual(status, 400)
        status, _, _ = self.request("POST", f"/api/workspaces/members/{carol_member['user_id']}/role", {"role": "admin"}, bob)
        self.assertEqual(status, 403)
        status, _, _ = self.request("POST", f"/api/workspaces/members/{carol_member['user_id']}/remove", {}, bob)
        self.assertEqual(status, 200)

    def test_creating_and_switching_workspaces(self) -> None:
        admin = self.setup_admin()
        bob = self.create_and_login(admin, "bob")
        personal = self.active_workspace(bob)["workspace"]["id"]
        status, created, _ = self.request("POST", "/api/workspaces", {"name": "Second Space"}, bob)
        self.assertEqual(status, 201)
        payload = self.active_workspace(bob)
        self.assertEqual(payload["workspace"]["id"], created["id"])
        self.assertEqual(payload["workspace"]["role"], "owner")
        self.assertEqual(len(payload["workspaces"]), 2)
        status, _, _ = self.request("POST", "/api/me/workspace", {"workspace_id": personal}, bob)
        self.assertEqual(status, 200)
        self.assertEqual(self.active_workspace(bob)["workspace"]["id"], personal)

    def test_removed_member_falls_back_to_a_remaining_workspace(self) -> None:
        admin = self.setup_admin()
        admin_workspace = self.active_workspace(admin)["workspace"]["id"]
        bob = self.create_and_login(admin, "bob")
        personal = self.active_workspace(bob)["workspace"]["id"]
        status, member, _ = self.request("POST", "/api/workspaces/members", {"username": "bob", "role": "viewer"}, admin)
        self.assertEqual(status, 201)
        status, _, _ = self.request("POST", "/api/me/workspace", {"workspace_id": admin_workspace}, bob)
        self.assertEqual(status, 200)
        status, _, _ = self.request("POST", f"/api/workspaces/members/{member['user_id']}/remove", {}, admin)
        self.assertEqual(status, 200)
        payload = self.active_workspace(bob)
        self.assertEqual(payload["workspace"]["id"], personal)
        status, dashboard, _ = self.request("GET", "/api/dashboard", auth=bob)
        self.assertEqual(status, 200)
        self.assertEqual(dashboard["posts"], [])


if __name__ == "__main__":
    unittest.main()
