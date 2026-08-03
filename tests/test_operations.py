"""Operational endpoint regression tests: workspace status, oversight, metrics."""

from __future__ import annotations

import http.client
import os
import unittest

try:
    from tests.test_workspaces import WorkspaceHttpCase
except ImportError:
    from test_workspaces import WorkspaceHttpCase


class OperationsTest(WorkspaceHttpCase):
    def test_workspace_status_reports_usage_health_and_limits(self) -> None:
        admin = self.setup_admin()
        status, _, _ = self.request("POST", "/api/posts", {"body": "hello", "channels": ["X"]}, admin)
        self.assertEqual(status, 201)
        status, _, _ = self.request("POST", "/api/connections", {"provider": "Telegram", "external_account_id": "-5", "display_name": "channel", "secrets": {"bot_token": "x"}}, admin)
        self.assertEqual(status, 201)
        status, overview, _ = self.request("GET", "/api/workspaces/status", auth=admin)
        self.assertEqual(status, 200)
        self.assertEqual(overview["plan"], "self_hosted")
        self.assertIsNone(overview["limits"])
        self.assertEqual(overview["usage"]["posts_created"], 1)
        self.assertEqual(overview["members"], 1)
        self.assertEqual(overview["connection_health"]["active"], 1)
        self.assertGreaterEqual(overview["posts"].get("draft", 0), 1)

    def test_workspace_status_requires_workspace_admin(self) -> None:
        admin = self.setup_admin()
        workspace_id = self.active_workspace(admin)["workspace"]["id"]
        bob = self.create_and_login(admin, "bob")
        status, _, _ = self.request("POST", "/api/workspaces/members", {"username": "bob", "role": "editor"}, admin)
        self.assertEqual(status, 201)
        status, _, _ = self.request("POST", "/api/me/workspace", {"workspace_id": workspace_id}, bob)
        self.assertEqual(status, 200)
        status, _, _ = self.request("GET", "/api/workspaces/status", auth=bob)
        self.assertEqual(status, 403)

    def test_instance_admin_workspace_listing_is_metadata_only_and_audited(self) -> None:
        admin = self.setup_admin()
        self.create_and_login(admin, "bob")
        status, listing, _ = self.request("GET", "/api/admin/workspaces", auth=admin)
        self.assertEqual(status, 200)
        self.assertEqual(len(listing["workspaces"]), 2)
        self.assertNotIn("encrypted_secrets", str(listing))
        for row in listing["workspaces"]:
            self.assertIn("member_count", row)
            self.assertIn("post_count", row)
            self.assertNotIn("body", row)
        with self.server.db() as connection:
            event = connection.execute("SELECT 1 FROM audit_events WHERE action = 'admin.workspaces_listed'").fetchone()
        self.assertIsNotNone(event)
        status, _, auth = self.request("POST", "/api/login", {"username": "bob", "password": "another-long-password"})
        status, _, _ = self.request("GET", "/api/admin/workspaces", auth=auth)
        self.assertEqual(status, 403)

    def test_metrics_include_media_and_workspace_gauges(self) -> None:
        self.setup_admin()
        os.environ["SOSOPO_METRICS_TOKEN"] = "metrics-test-token"
        try:
            connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
            connection.request("GET", "/metrics", None, {"Authorization": "Bearer metrics-test-token"})
            response = connection.getresponse()
            body = response.read().decode()
            connection.close()
        finally:
            os.environ.pop("SOSOPO_METRICS_TOKEN", None)
        self.assertEqual(response.status, 200)
        self.assertIn("sosopo_workspaces 1", body)
        self.assertIn("sosopo_media_jobs", body)
        self.assertIn("sosopo_worker_healthy", body)


if __name__ == "__main__":
    unittest.main()
