"""Plan-limit, usage-metering, billing, and workspace-AI regression tests."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import unittest
from datetime import UTC, datetime

try:
    from tests.test_workspaces import WorkspaceHttpCase, WorkspaceTestCase
except ImportError:
    from test_workspaces import WorkspaceHttpCase, WorkspaceTestCase


class PlanLimitTest(WorkspaceTestCase):
    def test_self_hosted_plan_is_unlimited_and_overridable(self) -> None:
        s = self.server
        self.assertIsNone(s.plan_limits("self_hosted"))
        self.assertEqual(s.plan_limits("free")["members"], 3)
        os.environ["SOSOPO_PLAN_LIMITS"] = json.dumps({"free": {"members": 99}, "custom": {"posts_per_month": 5}})
        try:
            self.assertEqual(s.plan_limits("free")["members"], 99)
            self.assertEqual(s.plan_limits("custom")["posts_per_month"], 5)
        finally:
            os.environ.pop("SOSOPO_PLAN_LIMITS", None)

    def test_usage_recording_accumulates_by_period(self) -> None:
        s = self.server
        with s.db() as connection:
            user_id = s.insert_id(connection, "INSERT INTO users (username, password_salt, password_hash, role, timezone, created_at) VALUES (?, ?, ?, 'user', 'UTC', ?)", ("owner", "salt", "hash", s.now()))
            workspace_id = s.create_workspace(connection, "Space", user_id)
            s.record_usage(connection, workspace_id, "posts_created")
            s.record_usage(connection, workspace_id, "posts_created", 2)
            s.record_usage(connection, workspace_id, "storage_bytes", 500, period="total")
            self.assertEqual(s.usage_amount(connection, workspace_id, "posts_created"), 3)
            self.assertEqual(s.usage_amount(connection, workspace_id, "storage_bytes", period="total"), 500)
            self.assertEqual(s.usage_amount(connection, workspace_id, "posts_created", period="1999-01"), 0)

    def test_hosted_mode_new_workspaces_start_on_the_free_plan(self) -> None:
        s = self.server
        os.environ["SOSOPO_DEPLOYMENT_MODE"] = "hosted"
        try:
            with s.db() as connection:
                user_id = s.insert_id(connection, "INSERT INTO users (username, password_salt, password_hash, role, timezone, created_at) VALUES (?, ?, ?, 'user', 'UTC', ?)", ("hosted-user", "salt", "hash", s.now()))
                workspace_id = s.create_workspace(connection, "Hosted Space", user_id)
                self.assertEqual(s.workspace_plan(connection, workspace_id), "free")
        finally:
            os.environ.pop("SOSOPO_DEPLOYMENT_MODE", None)


class QuotaEnforcementTest(WorkspaceHttpCase):
    def set_plan(self, workspace_id: int, plan: str) -> None:
        with self.server.db() as connection:
            connection.execute("UPDATE workspaces SET plan = ? WHERE id = ?", (plan, workspace_id))

    def test_monthly_post_quota_is_enforced_per_workspace(self) -> None:
        admin = self.setup_admin()
        workspace_id = self.active_workspace(admin)["workspace"]["id"]
        self.set_plan(workspace_id, "free")
        os.environ["SOSOPO_PLAN_LIMITS"] = json.dumps({"free": {"posts_per_month": 1}})
        try:
            status, _, _ = self.request("POST", "/api/posts", {"body": "first", "channels": ["X"]}, admin)
            self.assertEqual(status, 201)
            status, payload, _ = self.request("POST", "/api/posts", {"body": "second", "channels": ["X"]}, admin)
            self.assertEqual(status, 400)
            self.assertIn("monthly limit", payload["error"])
        finally:
            os.environ.pop("SOSOPO_PLAN_LIMITS", None)
        status, _, _ = self.request("POST", "/api/posts", {"body": "unlimited again", "channels": ["X"]}, admin)
        self.assertEqual(status, 201)

    def test_member_limit_blocks_membership_and_invitation_acceptance(self) -> None:
        admin = self.setup_admin()
        workspace_id = self.active_workspace(admin)["workspace"]["id"]
        self.set_plan(workspace_id, "free")
        self.create_and_login(admin, "bob")
        os.environ["SOSOPO_PLAN_LIMITS"] = json.dumps({"free": {"members": 1}})
        try:
            status, payload, _ = self.request("POST", "/api/workspaces/members", {"username": "bob", "role": "editor"}, admin)
            self.assertEqual(status, 400)
            self.assertIn("members", payload["error"])
            status, invitation, _ = self.request("POST", "/api/workspaces/invitations", {"email": "bob@example.com", "role": "editor"}, admin)
            self.assertEqual(status, 201)
            token = invitation["invite_url"].split("token=")[1]
            status, payload, _ = self.request("POST", f"/api/invitations/{token}/accept", {"username": "carol", "password": "a-long-enough-password"})
            self.assertEqual(status, 400)
        finally:
            os.environ.pop("SOSOPO_PLAN_LIMITS", None)

    def test_ai_budget_cap_limits_generations(self) -> None:
        admin = self.setup_admin()
        status, _, _ = self.request("POST", "/api/workspaces/settings", {"ai_monthly_cap": 1}, admin)
        self.assertEqual(status, 200)
        s = self.server
        with s.db() as connection:
            connection.execute("INSERT INTO instance_settings (name, value) VALUES (?, ?)", ("ai_provider_openai", s.encrypt_secrets({"api_key": "key", "base_url": "https://ai.example/v1", "model": "model-a", "models": '["model-a"]'})))
        original_request_json = s.request_json
        s.request_json = lambda url, payload, headers=None: {"choices": [{"message": {"content": "generated copy"}}]}
        try:
            status, payload, _ = self.request("POST", "/api/ai/generate", {"provider": "OpenAI", "model": "model-a", "instruction": "write", "draft": "", "channels": ["X"]}, admin)
            self.assertEqual(status, 200)
            self.assertEqual(payload["copy"], "generated copy")
            status, payload, _ = self.request("POST", "/api/ai/generate", {"provider": "OpenAI", "model": "model-a", "instruction": "write", "draft": "", "channels": ["X"]}, admin)
            self.assertEqual(status, 400)
            self.assertIn("AI budget cap", payload["error"])
        finally:
            s.request_json = original_request_json

    def test_workspace_settings_require_owner(self) -> None:
        admin = self.setup_admin()
        workspace_id = self.active_workspace(admin)["workspace"]["id"]
        bob = self.create_and_login(admin, "bob")
        status, _, _ = self.request("POST", "/api/workspaces/members", {"username": "bob", "role": "admin"}, admin)
        self.assertEqual(status, 201)
        status, _, _ = self.request("POST", "/api/me/workspace", {"workspace_id": workspace_id}, bob)
        self.assertEqual(status, 200)
        status, _, _ = self.request("POST", "/api/workspaces/settings", {"ai_monthly_cap": 5}, bob)
        self.assertEqual(status, 403)


class BillingTest(WorkspaceHttpCase):
    def signed_webhook(self, event: dict, secret: str) -> tuple[bytes, str]:
        raw = json.dumps(event).encode()
        timestamp = str(int(datetime.now(UTC).timestamp()))
        signature = hmac.new(secret.encode(), f"{timestamp}.".encode() + raw, hashlib.sha256).hexdigest()
        return raw, f"t={timestamp},v1={signature}"

    def post_webhook(self, raw: bytes, signature: str) -> int:
        import http.client
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        connection.request("POST", "/api/billing/webhook", raw, {"Content-Type": "application/json", "Stripe-Signature": signature})
        status = connection.getresponse().status
        connection.close()
        return status

    def test_webhook_signature_gates_plan_changes(self) -> None:
        admin = self.setup_admin()
        workspace_id = self.active_workspace(admin)["workspace"]["id"]
        os.environ["STRIPE_WEBHOOK_SECRET"] = "whsec_test_secret"
        try:
            event = {"type": "checkout.session.completed", "data": {"object": {"customer": "cus_1", "subscription": "sub_1", "metadata": {"workspace_id": str(workspace_id), "plan": "pro"}}}}
            raw, signature = self.signed_webhook(event, "whsec_test_secret")
            self.assertEqual(self.post_webhook(raw, "t=1,v1=bad"), 400)
            with self.server.db() as connection:
                self.assertEqual(self.server.workspace_plan(connection, workspace_id), "self_hosted")
            self.assertEqual(self.post_webhook(raw, signature), 200)
            with self.server.db() as connection:
                self.assertEqual(self.server.workspace_plan(connection, workspace_id), "pro")
                row = connection.execute("SELECT billing_customer_id, billing_subscription_id FROM workspaces WHERE id = ?", (workspace_id,)).fetchone()
            self.assertEqual((row["billing_customer_id"], row["billing_subscription_id"]), ("cus_1", "sub_1"))
            cancel = {"type": "customer.subscription.deleted", "data": {"object": {"metadata": {"workspace_id": str(workspace_id)}}}}
            raw, signature = self.signed_webhook(cancel, "whsec_test_secret")
            self.assertEqual(self.post_webhook(raw, signature), 200)
            with self.server.db() as connection:
                self.assertEqual(self.server.workspace_plan(connection, workspace_id), "free")
        finally:
            os.environ.pop("STRIPE_WEBHOOK_SECRET", None)

    def test_webhook_is_absent_when_unconfigured_and_checkout_requires_billing(self) -> None:
        admin = self.setup_admin()
        self.assertEqual(self.post_webhook(b"{}", "t=1,v1=x"), 404)
        status, payload, _ = self.request("POST", "/api/workspaces/billing/checkout", {"plan": "pro"}, admin)
        self.assertEqual(status, 503)
        self.assertIn("not configured", payload["error"])


class WorkspaceAiProviderTest(WorkspaceHttpCase):
    def test_workspace_key_overrides_instance_and_is_tenant_scoped(self) -> None:
        s = self.server
        admin = self.setup_admin()
        with s.db() as connection:
            connection.execute("INSERT INTO instance_settings (name, value) VALUES (?, ?)", ("ai_provider_openai", s.encrypt_secrets({"api_key": "instance-key", "base_url": "https://ai.example/v1", "model": "instance-model", "models": '["instance-model", "workspace-model"]'})))
        status, providers, _ = self.request("GET", "/api/ai/providers", auth=admin)
        self.assertEqual(providers["providers"][0]["source"], "instance")
        status, _, _ = self.request("POST", "/api/workspaces/ai-providers", {"provider": "OpenAI", "api_key": "workspace-key", "model": "workspace-model"}, admin)
        self.assertEqual(status, 200)
        status, providers, _ = self.request("GET", "/api/ai/providers", auth=admin)
        self.assertEqual(providers["providers"][0]["source"], "workspace")
        self.assertEqual(providers["providers"][0]["model"], "workspace-model")
        captured: dict = {}
        original_request_json = s.request_json
        s.request_json = lambda url, payload, headers=None: captured.update(headers=headers, payload=payload) or {"choices": [{"message": {"content": "ok"}}]}
        try:
            status, _, _ = self.request("POST", "/api/ai/generate", {"provider": "OpenAI", "model": "workspace-model", "instruction": "x", "draft": "", "channels": ["X"]}, admin)
        finally:
            s.request_json = original_request_json
        self.assertEqual(status, 200)
        self.assertEqual(captured["headers"]["Authorization"], "Bearer workspace-key")
        bob = self.create_and_login(admin, "bob")
        status, providers, _ = self.request("GET", "/api/ai/providers", auth=bob)
        self.assertEqual(status, 200)
        self.assertEqual(providers["providers"][0]["source"], "instance")
        status, _, _ = self.request("POST", "/api/workspaces/ai-providers", {"provider": "OpenAI", "api_key": "", "model": ""}, bob)
        self.assertEqual(status, 400)  # bob's own workspace has no saved key to reuse

    def test_workspace_ai_endpoints_require_workspace_admin(self) -> None:
        admin = self.setup_admin()
        workspace_id = self.active_workspace(admin)["workspace"]["id"]
        bob = self.create_and_login(admin, "bob")
        status, _, _ = self.request("POST", "/api/workspaces/members", {"username": "bob", "role": "editor"}, admin)
        self.assertEqual(status, 201)
        status, _, _ = self.request("POST", "/api/me/workspace", {"workspace_id": workspace_id}, bob)
        self.assertEqual(status, 200)
        status, _, _ = self.request("GET", "/api/workspaces/ai-providers", auth=bob)
        self.assertEqual(status, 403)
        status, _, _ = self.request("POST", "/api/workspaces/ai-providers", {"provider": "OpenAI", "api_key": "k", "model": "m"}, bob)
        self.assertEqual(status, 403)


if __name__ == "__main__":
    unittest.main()
