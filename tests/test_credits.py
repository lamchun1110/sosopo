"""The AI credit ledger: enforcement, append-only auditing, and refunds.

CLAUDE.md: credits are consumed only by AI usage. Publishing, scheduling,
media storage, users, and organizations never consume credits.
"""

from __future__ import annotations

import base64
import json
import os
import unittest
from io import BytesIO

from PIL import Image

try:
    from tests.test_workspaces import WorkspaceHttpCase
except ImportError:
    from test_workspaces import WorkspaceHttpCase


def png_bytes() -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (8, 8), "purple").save(buffer, format="PNG")
    return buffer.getvalue()


class CreditLedgerTest(WorkspaceHttpCase):
    """Unit-level ledger behavior, independent of deployment mode."""

    def account(self, owner_type: str, owner_id: int) -> dict:
        with self.server.db() as connection:
            row = connection.execute("SELECT * FROM credit_accounts WHERE owner_type = ? AND owner_id = ?", (owner_type, owner_id)).fetchone()
        return dict(row) if row else {}

    def transactions(self, account_id: int) -> list[dict]:
        with self.server.db() as connection:
            return [dict(row) for row in connection.execute("SELECT * FROM credit_transactions WHERE account_id = ? ORDER BY id", (account_id,)).fetchall()]

    def test_an_account_starts_empty_and_records_every_movement(self) -> None:
        s = self.server
        with s.db() as connection:
            account_id = s.ensure_credit_account(connection, "workspace", 1)
            self.assertEqual(s.account_balance(connection, "workspace", 1), 0)
            s.record_credit_transaction(connection, account_id, 10, "grant", None, None)
            s.record_credit_transaction(connection, account_id, -1, "ai_generation", 7, "post:3")
        rows = self.transactions(account_id)
        self.assertEqual([(row["delta"], row["balance_after"], row["reason"]) for row in rows],
                         [(10, 10, "grant"), (-1, 9, "ai_generation")])
        self.assertEqual(rows[1]["actor_user_id"], 7)
        self.assertEqual(rows[1]["reference"], "post:3")
        self.assertEqual(self.account("workspace", 1)["balance"], 9)

    def test_accounts_are_created_once_per_owner(self) -> None:
        s = self.server
        with s.db() as connection:
            first = s.ensure_credit_account(connection, "workspace", 4)
            second = s.ensure_credit_account(connection, "workspace", 4)
            other = s.ensure_credit_account(connection, "organization", 4)
        self.assertEqual(first, second)
        self.assertNotEqual(first, other)

    def test_a_balance_can_never_go_negative(self) -> None:
        s = self.server
        with s.db() as connection:
            account_id = s.ensure_credit_account(connection, "workspace", 2)
            s.record_credit_transaction(connection, account_id, 1, "grant", None, None)
            with self.assertRaisesRegex(s.ProviderError, "credit"):
                s.record_credit_transaction(connection, account_id, -2, "ai_generation", None, None)
        self.assertEqual(self.account("workspace", 2)["balance"], 1)
        self.assertEqual(len(self.transactions(account_id)), 1)

    def test_the_ledger_is_append_only_and_balances_reconcile(self) -> None:
        s = self.server
        with s.db() as connection:
            account_id = s.ensure_credit_account(connection, "workspace", 3)
            for delta in (25, -1, -1, 5, -1):
                s.record_credit_transaction(connection, account_id, delta, "test", None, None)
        rows = self.transactions(account_id)
        self.assertEqual(sum(row["delta"] for row in rows), self.account("workspace", 3)["balance"])
        running = 0
        for row in rows:
            running += row["delta"]
            self.assertEqual(row["balance_after"], running)


class SelfHostedCreditTest(WorkspaceHttpCase):
    """Self-hosted stays unlimited: no enforcement, no ledger rows, no behavior change."""

    def test_credits_are_not_enforced_by_default(self) -> None:
        self.assertFalse(self.server.credits_enforced())

    def test_ai_generation_is_never_blocked_or_recorded(self) -> None:
        s = self.server
        admin = self.setup_admin()
        with s.db() as connection:
            connection.execute("INSERT INTO instance_settings (name, value) VALUES (?, ?)", ("ai_provider_openai", s.encrypt_secrets({"api_key": "k", "base_url": "https://ai.example/v1", "model": "m"})))
        original = s.request_json
        s.request_json = lambda url, payload, headers=None: {"choices": [{"message": {"content": "copy"}}]}
        try:
            for _ in range(3):
                status, _, _ = self.request("POST", "/api/ai/generate", {"provider": "OpenAI", "model": "m", "instruction": "x", "draft": "", "channels": ["X"]}, admin)
                self.assertEqual(status, 200)
        finally:
            s.request_json = original
        with s.db() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) AS count FROM credit_transactions").fetchone()["count"], 0)
            self.assertEqual(s.usage_amount(connection, self.active_workspace(admin)["workspace"]["id"], "ai_generations"), 3)


class EnforcedCreditTest(WorkspaceHttpCase):
    """Hosted mode: AI spends credits, and only AI does."""

    def setUp(self) -> None:
        os.environ["SOSOPO_CREDITS_ENFORCED"] = "1"
        super().setUp()

    def tearDown(self) -> None:
        os.environ.pop("SOSOPO_CREDITS_ENFORCED", None)
        super().tearDown()

    def configure_ai(self) -> None:
        s = self.server
        with s.db() as connection:
            connection.execute("INSERT INTO instance_settings (name, value) VALUES (?, ?)", ("ai_provider_openai", s.encrypt_secrets({"api_key": "k", "base_url": "https://ai.example/v1", "model": "m"})))

    def fund(self, workspace_id: int, amount: int) -> int:
        s = self.server
        with s.db() as connection:
            account_id = s.ensure_credit_account(connection, "workspace", workspace_id)
            if amount:
                s.record_credit_transaction(connection, account_id, amount, "test_grant", None, None)
        return account_id

    def balance(self, workspace_id: int) -> int:
        with self.server.db() as connection:
            return self.server.account_balance(connection, "workspace", workspace_id)

    def generate(self, auth: dict) -> int:
        s = self.server
        original = s.request_json
        s.request_json = lambda url, payload, headers=None: {"choices": [{"message": {"content": "copy"}}]}
        try:
            status, _, _ = self.request("POST", "/api/ai/generate", {"provider": "OpenAI", "model": "m", "instruction": "x", "draft": "", "channels": ["X"]}, auth)
        finally:
            s.request_json = original
        return status

    def test_text_generation_spends_exactly_one_credit(self) -> None:
        admin = self.setup_admin()
        self.configure_ai()
        workspace_id = self.active_workspace(admin)["workspace"]["id"]
        self.fund(workspace_id, 2)
        self.assertEqual(self.generate(admin), 200)
        self.assertEqual(self.balance(workspace_id), 1)

    def test_generation_is_refused_with_an_empty_balance(self) -> None:
        admin = self.setup_admin()
        self.configure_ai()
        workspace_id = self.active_workspace(admin)["workspace"]["id"]
        self.fund(workspace_id, 0)
        status = self.generate(admin)
        self.assertEqual(status, 400)
        self.assertEqual(self.balance(workspace_id), 0)

    def test_publishing_and_uploads_never_spend_credits(self) -> None:
        admin = self.setup_admin()
        workspace_id = self.active_workspace(admin)["workspace"]["id"]
        self.fund(workspace_id, 5)
        status, _, _ = self.request("POST", "/api/posts", {"body": "a post", "channels": ["X"]}, admin)
        self.assertEqual(status, 201)
        status, _, _ = self.request("POST", "/api/workspaces", {"name": "Another team"}, admin)
        self.assertEqual(status, 201)
        self.assertEqual(self.balance(workspace_id), 5)

    def test_a_media_job_spends_a_credit_and_a_failure_refunds_it(self) -> None:
        s = self.server
        admin = self.setup_admin()
        self.configure_ai()
        workspace_id = self.active_workspace(admin)["workspace"]["id"]
        self.fund(workspace_id, 3)
        status, job, _ = self.request("POST", "/api/media/jobs", {"kind": "image", "prompt": "a cat", "provider": "OpenAI", "model": "gpt-image-1"}, admin)
        self.assertEqual(status, 201, job)
        self.assertEqual(self.balance(workspace_id), 2)
        original = s.request_json
        s.request_json = lambda url, payload, headers=None: (_ for _ in ()).throw(s.ProviderError("provider unavailable"))
        try:
            s.run_media_job(dict(s.claim_media_job()))
        finally:
            s.request_json = original
        self.assertEqual(self.balance(workspace_id), 3)
        with s.db() as connection:
            reasons = [row["reason"] for row in connection.execute("SELECT reason FROM credit_transactions ORDER BY id").fetchall()]
        self.assertEqual(reasons, ["test_grant", "ai_media", "ai_media_refund"])

    def test_a_successful_media_job_keeps_the_credit_spent(self) -> None:
        s = self.server
        admin = self.setup_admin()
        self.configure_ai()
        workspace_id = self.active_workspace(admin)["workspace"]["id"]
        self.fund(workspace_id, 3)
        self.request("POST", "/api/media/jobs", {"kind": "image", "prompt": "a cat", "provider": "OpenAI", "model": "gpt-image-1"}, admin)
        original = s.request_json
        s.request_json = lambda url, payload, headers=None: {"data": [{"b64_json": base64.b64encode(png_bytes()).decode()}]}
        try:
            s.run_media_job(dict(s.claim_media_job()))
        finally:
            s.request_json = original
        self.assertEqual(self.balance(workspace_id), 2)

    def test_every_movement_names_its_actor_and_reference(self) -> None:
        admin = self.setup_admin()
        self.configure_ai()
        workspace_id = self.active_workspace(admin)["workspace"]["id"]
        self.fund(workspace_id, 2)
        self.generate(admin)
        with self.server.db() as connection:
            row = connection.execute("SELECT * FROM credit_transactions WHERE reason = 'ai_generation'").fetchone()
        self.assertIsNotNone(row)
        self.assertIsNotNone(row["actor_user_id"])
        self.assertEqual(row["reference"], f"workspace:{workspace_id}")

    def test_usage_records_still_track_analytics_alongside_the_ledger(self) -> None:
        admin = self.setup_admin()
        self.configure_ai()
        workspace_id = self.active_workspace(admin)["workspace"]["id"]
        self.fund(workspace_id, 2)
        self.generate(admin)
        with self.server.db() as connection:
            self.assertEqual(self.server.usage_amount(connection, workspace_id, "ai_generations"), 1)


class MonthlyGrantTest(WorkspaceHttpCase):
    def setUp(self) -> None:
        os.environ["SOSOPO_CREDITS_ENFORCED"] = "1"
        os.environ["SOSOPO_PLAN_LIMITS"] = json.dumps({"self_hosted": {"ai_generations_per_month": 4, "ai_media_per_month": 6}})
        super().setUp()

    def tearDown(self) -> None:
        os.environ.pop("SOSOPO_CREDITS_ENFORCED", None)
        os.environ.pop("SOSOPO_PLAN_LIMITS", None)
        super().tearDown()

    def test_the_plan_quota_becomes_a_monthly_grant(self) -> None:
        s = self.server
        admin = self.setup_admin()
        workspace_id = self.active_workspace(admin)["workspace"]["id"]
        with s.db() as connection:
            s.grant_monthly_credits(connection, workspace_id)
            self.assertEqual(s.account_balance(connection, "workspace", workspace_id), 10)

    def test_the_grant_is_applied_once_per_period(self) -> None:
        s = self.server
        admin = self.setup_admin()
        workspace_id = self.active_workspace(admin)["workspace"]["id"]
        with s.db() as connection:
            s.grant_monthly_credits(connection, workspace_id)
            s.grant_monthly_credits(connection, workspace_id)
            self.assertEqual(s.account_balance(connection, "workspace", workspace_id), 10)
            rows = connection.execute("SELECT COUNT(*) AS count FROM credit_transactions WHERE reason = 'monthly_grant'").fetchone()
        self.assertEqual(rows["count"], 1)

    def test_a_new_period_tops_the_account_back_up(self) -> None:
        s = self.server
        admin = self.setup_admin()
        workspace_id = self.active_workspace(admin)["workspace"]["id"]
        with s.db() as connection:
            account_id = s.ensure_credit_account(connection, "workspace", workspace_id)
            s.grant_monthly_credits(connection, workspace_id)
            s.record_credit_transaction(connection, account_id, -7, "ai_generation", None, None)
            connection.execute("UPDATE credit_accounts SET granted_period = '1999-01' WHERE id = ?", (account_id,))
            s.grant_monthly_credits(connection, workspace_id)
            self.assertEqual(s.account_balance(connection, "workspace", workspace_id), 10)


if __name__ == "__main__":
    unittest.main()
