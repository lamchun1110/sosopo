"""AI analytics summarization: read-only, admin-only, and secret-free."""

from __future__ import annotations

import json
import os
import unittest

try:
    from tests.test_workspaces import WorkspaceHttpCase
except ImportError:
    from test_workspaces import WorkspaceHttpCase


class InsightTestCase(WorkspaceHttpCase):
    def configure_ai(self) -> None:
        s = self.server
        with s.db() as connection:
            connection.execute("INSERT INTO instance_settings (name, value) VALUES (?, ?)",
                               ("ai_provider_openai", s.encrypt_secrets({"api_key": "super-secret-key", "base_url": "https://ai.example/v1", "model": "m"})))

    def summarize(self, auth: dict, capture: dict | None = None) -> tuple[int, dict]:
        s = self.server
        original = s.request_json

        def fake(url, payload, headers=None):
            if capture is not None:
                capture.update(payload=payload, headers=headers)
            return {"choices": [{"message": {"content": "Delivery is healthy. Consider posting more on X."}}]}

        s.request_json = fake
        try:
            status, result, _ = self.request("POST", "/api/workspaces/summary", {"provider": "OpenAI", "model": "m"}, auth)
        finally:
            s.request_json = original
        return status, result


class SummaryPermissionTest(InsightTestCase):
    def test_an_admin_receives_a_labelled_summary(self) -> None:
        admin = self.setup_admin()
        self.configure_ai()
        status, result = self.summarize(admin)
        self.assertEqual(status, 200, result)
        self.assertIn("Delivery is healthy", result["summary"])
        self.assertTrue(result["ai_generated"])

    def test_viewers_and_editors_are_refused(self) -> None:
        admin = self.setup_admin()
        self.configure_ai()
        workspace_id = self.active_workspace(admin)["workspace"]["id"]
        bob = self.create_and_login(admin, "bob")
        for role in ("viewer", "editor"):
            with self.subTest(role=role):
                self.request("POST", "/api/workspaces/members", {"username": "bob", "role": role}, admin)
                self.request("POST", "/api/me/workspace", {"workspace_id": workspace_id}, bob)
                status, _ = self.summarize(bob)
                self.assertEqual(status, 403)


class SummaryPromptTest(InsightTestCase):
    def test_the_prompt_carries_real_workspace_metrics(self) -> None:
        admin = self.setup_admin()
        self.configure_ai()
        self.request("POST", "/api/posts", {"body": "a real post", "channels": ["X"]}, admin)
        captured: dict = {}
        status, _ = self.summarize(admin, captured)
        self.assertEqual(status, 200)
        prompt = json.dumps(captured["payload"]["messages"])
        self.assertIn("self_hosted", prompt)
        self.assertIn("draft", prompt)
        self.assertIn("connection_health", prompt)

    def test_the_prompt_never_carries_credentials(self) -> None:
        s = self.server
        admin = self.setup_admin()
        self.configure_ai()
        status, account, _ = self.request("POST", "/api/connections", {"provider": "Telegram", "external_account_id": "-100",
                                                                       "display_name": "team channel", "secrets": {"bot_token": "telegram-secret-token"}}, admin)
        self.assertEqual(status, 201, account)
        captured: dict = {}
        self.summarize(admin, captured)
        blob = json.dumps(captured["payload"]) + json.dumps(captured["headers"] or {})
        for secret in ("telegram-secret-token", "encrypted_secrets", "bot_token"):
            self.assertNotIn(secret, blob, secret)
        self.assertIn("super-secret-key", json.dumps(captured["headers"]))  # the key authenticates, never the prompt
        self.assertNotIn("super-secret-key", json.dumps(captured["payload"]))

    def test_summarizing_changes_no_workspace_data(self) -> None:
        admin = self.setup_admin()
        self.configure_ai()
        self.request("POST", "/api/posts", {"body": "a real post", "channels": ["X"]}, admin)
        with self.server.db() as connection:
            before = [dict(row) for row in connection.execute("SELECT id, state, body FROM posts ORDER BY id").fetchall()]
        self.summarize(admin)
        with self.server.db() as connection:
            after = [dict(row) for row in connection.execute("SELECT id, state, body FROM posts ORDER BY id").fetchall()]
        self.assertEqual(before, after)

    def test_a_provider_failure_is_reported_cleanly(self) -> None:
        s = self.server
        admin = self.setup_admin()
        self.configure_ai()
        original = s.request_json
        s.request_json = lambda url, payload, headers=None: (_ for _ in ()).throw(s.ProviderError("provider unavailable"))
        try:
            status, result, _ = self.request("POST", "/api/workspaces/summary", {"provider": "OpenAI", "model": "m"}, admin)
        finally:
            s.request_json = original
        self.assertEqual(status, 502)
        self.assertIn("provider unavailable", result["error"])


class SummaryCreditTest(InsightTestCase):
    def setUp(self) -> None:
        os.environ["SOSOPO_CREDITS_ENFORCED"] = "1"
        super().setUp()

    def tearDown(self) -> None:
        os.environ.pop("SOSOPO_CREDITS_ENFORCED", None)
        super().tearDown()

    def balance(self, workspace_id: int) -> int:
        with self.server.db() as connection:
            return self.server.account_balance(connection, "workspace", workspace_id)

    def test_a_summary_spends_exactly_one_credit(self) -> None:
        s = self.server
        admin = self.setup_admin()
        self.configure_ai()
        workspace_id = self.active_workspace(admin)["workspace"]["id"]
        with s.db() as connection:
            account_id = s.ensure_credit_account(connection, "workspace", workspace_id)
            s.record_credit_transaction(connection, account_id, 3, "test_grant", None, None)
        status, result = self.summarize(admin)
        self.assertEqual(status, 200, result)
        self.assertEqual(self.balance(workspace_id), 2)

    def test_an_empty_balance_refuses_the_summary(self) -> None:
        admin = self.setup_admin()
        self.configure_ai()
        status, _ = self.summarize(admin)
        self.assertEqual(status, 400)


if __name__ == "__main__":
    unittest.main()
