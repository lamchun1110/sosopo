"""AI content calendar: a brief becomes reviewable drafts, never scheduled posts."""

from __future__ import annotations

import json
import os
import unittest

try:
    from tests.test_workspaces import WorkspaceHttpCase
except ImportError:
    from test_workspaces import WorkspaceHttpCase


def plan(count: int = 3) -> str:
    return json.dumps({"posts": [
        {"body": f"Draft number {index}", "channel": "X", "suggested_for": f"2030-01-0{index + 1}T09:00"}
        for index in range(count)
    ]})


class CampaignTestCase(WorkspaceHttpCase):
    def configure_ai(self) -> None:
        s = self.server
        with s.db() as connection:
            connection.execute("INSERT INTO instance_settings (name, value) VALUES (?, ?)",
                               ("ai_provider_openai", s.encrypt_secrets({"api_key": "k", "base_url": "https://ai.example/v1", "model": "m"})))

    def plan_with(self, auth: dict, content: str, body: dict | None = None) -> tuple[int, dict]:
        s = self.server
        original = s.request_json
        s.request_json = lambda url, payload, headers=None: {"choices": [{"message": {"content": content}}]}
        try:
            request = {"name": "Launch week", "brief": "Announce the launch", "cadence": "one post per weekday",
                       "channels": ["X"], "count": 3, "provider": "OpenAI", "model": "m"}
            status, result, _ = self.request("POST", "/api/campaigns", {**request, **(body or {})}, auth)
        finally:
            s.request_json = original
        return status, result

    def posts(self, auth: dict) -> list[dict]:
        status, dashboard, _ = self.request("GET", "/api/dashboard", auth=auth)
        self.assertEqual(status, 200)
        return dashboard["posts"]


class CampaignPlanningTest(CampaignTestCase):
    def test_a_plan_creates_reviewable_drafts(self) -> None:
        admin = self.setup_admin()
        self.configure_ai()
        status, result = self.plan_with(admin, plan(3))
        self.assertEqual(status, 201, result)
        self.assertEqual(result["created"], 3)
        drafts = [post for post in self.posts(admin) if post["body"].startswith("Draft number")]
        self.assertEqual(len(drafts), 3)

    def test_drafts_are_never_scheduled_or_published(self) -> None:
        admin = self.setup_admin()
        self.configure_ai()
        self.plan_with(admin, plan(3))
        with self.server.db() as connection:
            rows = connection.execute("SELECT state, scheduled_for, published_at FROM posts WHERE campaign_id IS NOT NULL").fetchall()
        self.assertEqual(len(rows), 3)
        for row in rows:
            self.assertEqual(row["state"], "draft")
            self.assertIsNone(row["scheduled_for"])
            self.assertIsNone(row["published_at"])

    def test_the_suggested_time_is_kept_as_a_suggestion_only(self) -> None:
        admin = self.setup_admin()
        self.configure_ai()
        self.plan_with(admin, plan(1), {"count": 1})
        with self.server.db() as connection:
            row = connection.execute("SELECT suggested_for, scheduled_for FROM posts WHERE campaign_id IS NOT NULL").fetchone()
        self.assertEqual(row["suggested_for"], "2030-01-01T09:00")
        self.assertIsNone(row["scheduled_for"])

    def test_campaigns_are_listed_with_their_draft_count(self) -> None:
        admin = self.setup_admin()
        self.configure_ai()
        self.plan_with(admin, plan(2), {"count": 2})
        status, payload, _ = self.request("GET", "/api/campaigns", auth=admin)
        self.assertEqual(status, 200)
        self.assertEqual(len(payload["campaigns"]), 1)
        self.assertEqual(payload["campaigns"][0]["name"], "Launch week")
        self.assertEqual(payload["campaigns"][0]["posts"], 2)

    def test_the_brief_and_cadence_reach_the_provider(self) -> None:
        s = self.server
        admin = self.setup_admin()
        self.configure_ai()
        captured: dict = {}
        original = s.request_json
        s.request_json = lambda url, payload, headers=None: captured.update(payload=payload) or {"choices": [{"message": {"content": plan(1)}}]}
        try:
            self.request("POST", "/api/campaigns", {"name": "N", "brief": "Announce the launch", "cadence": "one post per weekday", "channels": ["X"], "count": 1, "provider": "OpenAI", "model": "m"}, admin)
        finally:
            s.request_json = original
        prompt = json.dumps(captured["payload"]["messages"])
        self.assertIn("Announce the launch", prompt)
        self.assertIn("one post per weekday", prompt)

    def test_the_brand_voice_applies_to_planning(self) -> None:
        s = self.server
        admin = self.setup_admin()
        self.configure_ai()
        self.request("POST", "/api/workspaces/brand-voice", {"profile": {"tone": "warm and direct"}}, admin)
        captured: dict = {}
        original = s.request_json
        s.request_json = lambda url, payload, headers=None: captured.update(payload=payload) or {"choices": [{"message": {"content": plan(1)}}]}
        try:
            self.request("POST", "/api/campaigns", {"name": "N", "brief": "b", "cadence": "c", "channels": ["X"], "count": 1, "provider": "OpenAI", "model": "m"}, admin)
        finally:
            s.request_json = original
        self.assertIn("warm and direct", captured["payload"]["messages"][0]["content"])


class CampaignStrictnessTest(CampaignTestCase):
    def test_malformed_output_creates_nothing_at_all(self) -> None:
        admin = self.setup_admin()
        self.configure_ai()
        for content in ("not json at all", "{}", '{"posts": []}', '{"posts": "text"}',
                        '{"posts": [{"body": ""}]}', '{"posts": [{"channel": "X"}]}',
                        '{"posts": [{"body": "ok", "channel": "Telepathy"}]}'):
            with self.subTest(content=content[:34]):
                status, result = self.plan_with(admin, content)
                self.assertEqual(status, 502, result)
        with self.server.db() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) AS count FROM campaigns").fetchone()["count"], 0)
            self.assertEqual(connection.execute("SELECT COUNT(*) AS count FROM posts WHERE campaign_id IS NOT NULL").fetchone()["count"], 0)

    def test_one_bad_draft_rejects_the_whole_plan(self) -> None:
        admin = self.setup_admin()
        self.configure_ai()
        content = json.dumps({"posts": [{"body": "fine", "channel": "X"}, {"body": "", "channel": "X"}]})
        status, _ = self.plan_with(admin, content)
        self.assertEqual(status, 502)
        with self.server.db() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) AS count FROM posts WHERE campaign_id IS NOT NULL").fetchone()["count"], 0)

    def test_a_channel_outside_the_request_is_refused(self) -> None:
        admin = self.setup_admin()
        self.configure_ai()
        content = json.dumps({"posts": [{"body": "fine", "channel": "Telegram"}]})
        status, _ = self.plan_with(admin, content, {"channels": ["X"], "count": 1})
        self.assertEqual(status, 502)

    def test_requests_are_validated_before_any_provider_call(self) -> None:
        admin = self.setup_admin()
        self.configure_ai()
        for body in ({"count": 0}, {"count": 99}, {"channels": []}, {"channels": ["Telepathy"]},
                     {"name": ""}, {"brief": ""}, {"brief": "x" * 3000}):
            with self.subTest(body=body):
                status, _ = self.plan_with(admin, plan(3), body)
                self.assertEqual(status, 400)

    def test_a_viewer_cannot_plan(self) -> None:
        admin = self.setup_admin()
        self.configure_ai()
        workspace_id = self.active_workspace(admin)["workspace"]["id"]
        bob = self.create_and_login(admin, "bob")
        self.request("POST", "/api/workspaces/members", {"username": "bob", "role": "viewer"}, admin)
        self.request("POST", "/api/me/workspace", {"workspace_id": workspace_id}, bob)
        status, _ = self.plan_with(bob, plan(3))
        self.assertEqual(status, 403)

    def test_campaigns_are_workspace_scoped(self) -> None:
        admin = self.setup_admin()
        self.configure_ai()
        self.plan_with(admin, plan(2), {"count": 2})
        bob = self.create_and_login(admin, "bob")
        status, payload, _ = self.request("GET", "/api/campaigns", auth=bob)
        self.assertEqual((status, payload["campaigns"]), (200, []))


class CampaignCreditTest(CampaignTestCase):
    def setUp(self) -> None:
        os.environ["SOSOPO_CREDITS_ENFORCED"] = "1"
        super().setUp()

    def tearDown(self) -> None:
        os.environ.pop("SOSOPO_CREDITS_ENFORCED", None)
        super().tearDown()

    def fund(self, workspace_id: int, amount: int) -> None:
        s = self.server
        with s.db() as connection:
            account_id = s.ensure_credit_account(connection, "workspace", workspace_id)
            if amount:
                s.record_credit_transaction(connection, account_id, amount, "test_grant", None, None)

    def balance(self, workspace_id: int) -> int:
        with self.server.db() as connection:
            return self.server.account_balance(connection, "workspace", workspace_id)

    def test_one_credit_is_spent_per_generated_draft(self) -> None:
        admin = self.setup_admin()
        self.configure_ai()
        workspace_id = self.active_workspace(admin)["workspace"]["id"]
        self.fund(workspace_id, 10)
        status, result = self.plan_with(admin, plan(3))
        self.assertEqual(status, 201, result)
        self.assertEqual(self.balance(workspace_id), 7)

    def test_an_unaffordable_plan_creates_nothing_and_spends_nothing(self) -> None:
        admin = self.setup_admin()
        self.configure_ai()
        workspace_id = self.active_workspace(admin)["workspace"]["id"]
        self.fund(workspace_id, 2)
        status, _ = self.plan_with(admin, plan(3))
        self.assertEqual(status, 400)
        self.assertEqual(self.balance(workspace_id), 2)
        with self.server.db() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) AS count FROM campaigns").fetchone()["count"], 0)
            self.assertEqual(connection.execute("SELECT COUNT(*) AS count FROM posts WHERE campaign_id IS NOT NULL").fetchone()["count"], 0)

    def test_a_rejected_plan_refunds_nothing_because_it_charged_nothing(self) -> None:
        admin = self.setup_admin()
        self.configure_ai()
        workspace_id = self.active_workspace(admin)["workspace"]["id"]
        self.fund(workspace_id, 10)
        status, _ = self.plan_with(admin, "not json at all")
        self.assertEqual(status, 502)
        self.assertEqual(self.balance(workspace_id), 10)


if __name__ == "__main__":
    unittest.main()
