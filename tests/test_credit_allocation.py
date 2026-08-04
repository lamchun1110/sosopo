"""Hierarchical credit allocation: Organization -> Workspace -> User.

Unused credits stay with their owner unless explicitly transferred, every
transfer is a paired debit/credit in the ledger, and a user's AI action draws
down the nearest funded account: user, then workspace, then organization.
"""

from __future__ import annotations

import os
import unittest

try:
    from tests.test_workspaces import WorkspaceHttpCase
except ImportError:
    from test_workspaces import WorkspaceHttpCase


class AllocationTestCase(WorkspaceHttpCase):
    def setUp(self) -> None:
        os.environ["SOSOPO_CREDITS_ENFORCED"] = "1"
        super().setUp()

    def tearDown(self) -> None:
        os.environ.pop("SOSOPO_CREDITS_ENFORCED", None)
        super().tearDown()

    def fund(self, owner_type: str, owner_id: int, amount: int) -> int:
        s = self.server
        with s.db() as connection:
            account_id = s.ensure_credit_account(connection, owner_type, owner_id)
            s.record_credit_transaction(connection, account_id, amount, "test_grant", None, None)
        return account_id

    def balance(self, owner_type: str, owner_id: int) -> int:
        with self.server.db() as connection:
            return self.server.account_balance(connection, owner_type, owner_id)

    def make_organization(self, auth: dict) -> dict:
        status, organization, _ = self.request("POST", "/api/organizations", {"name": "Acme"}, auth)
        self.assertEqual(status, 201)
        return organization

    def user_id(self, username: str) -> int:
        with self.server.db() as connection:
            return int(connection.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()["id"])


class AllocationTest(AllocationTestCase):
    def test_an_organization_allocates_to_one_of_its_workspaces(self) -> None:
        admin = self.setup_admin()
        organization = self.make_organization(admin)
        status, workspace, _ = self.request("POST", f"/api/organizations/{organization['id']}/workspaces", {"name": "Campaigns"}, admin)
        self.assertEqual(status, 201)
        self.fund("organization", organization["id"], 50)
        status, payload, _ = self.request("POST", f"/api/organizations/{organization['id']}/credits/allocate",
                                          {"target_type": "workspace", "target_id": workspace["id"], "amount": 20}, admin)
        self.assertEqual(status, 200, payload)
        self.assertEqual(self.balance("organization", organization["id"]), 30)
        self.assertEqual(self.balance("workspace", workspace["id"]), 20)

    def test_a_transfer_writes_a_paired_debit_and_credit(self) -> None:
        admin = self.setup_admin()
        organization = self.make_organization(admin)
        status, workspace, _ = self.request("POST", f"/api/organizations/{organization['id']}/workspaces", {"name": "Campaigns"}, admin)
        self.fund("organization", organization["id"], 10)
        self.request("POST", f"/api/organizations/{organization['id']}/credits/allocate",
                     {"target_type": "workspace", "target_id": workspace["id"], "amount": 4}, admin)
        with self.server.db() as connection:
            rows = [dict(row) for row in connection.execute(
                "SELECT delta, reason FROM credit_transactions WHERE reason LIKE 'allocation%' ORDER BY id").fetchall()]
        self.assertEqual(rows, [{"delta": -4, "reason": "allocation_out"}, {"delta": 4, "reason": "allocation_in"}])

    def test_a_transfer_is_recorded_in_the_audit_log(self) -> None:
        admin = self.setup_admin()
        organization = self.make_organization(admin)
        status, workspace, _ = self.request("POST", f"/api/organizations/{organization['id']}/workspaces", {"name": "Campaigns"}, admin)
        self.fund("organization", organization["id"], 10)
        self.request("POST", f"/api/organizations/{organization['id']}/credits/allocate",
                     {"target_type": "workspace", "target_id": workspace["id"], "amount": 4}, admin)
        with self.server.db() as connection:
            row = connection.execute("SELECT * FROM audit_events WHERE action = 'credits.allocated'").fetchone()
        self.assertIsNotNone(row)
        self.assertIn("4", str(row["detail"]))

    def test_allocating_more_than_the_balance_is_refused_and_changes_nothing(self) -> None:
        admin = self.setup_admin()
        organization = self.make_organization(admin)
        status, workspace, _ = self.request("POST", f"/api/organizations/{organization['id']}/workspaces", {"name": "Campaigns"}, admin)
        self.fund("organization", organization["id"], 3)
        status, _, _ = self.request("POST", f"/api/organizations/{organization['id']}/credits/allocate",
                                    {"target_type": "workspace", "target_id": workspace["id"], "amount": 9}, admin)
        self.assertEqual(status, 400)
        self.assertEqual(self.balance("organization", organization["id"]), 3)
        self.assertEqual(self.balance("workspace", workspace["id"]), 0)

    def test_amounts_must_be_positive_whole_numbers(self) -> None:
        admin = self.setup_admin()
        organization = self.make_organization(admin)
        status, workspace, _ = self.request("POST", f"/api/organizations/{organization['id']}/workspaces", {"name": "Campaigns"}, admin)
        self.fund("organization", organization["id"], 10)
        for amount in (0, -5, "many", 1.5):
            status, _, _ = self.request("POST", f"/api/organizations/{organization['id']}/credits/allocate",
                                        {"target_type": "workspace", "target_id": workspace["id"], "amount": amount}, admin)
            self.assertEqual(status, 400, amount)

    def test_an_organization_allocates_directly_to_a_member(self) -> None:
        admin = self.setup_admin()
        organization = self.make_organization(admin)
        self.create_and_login(admin, "bob")
        self.request("POST", f"/api/organizations/{organization['id']}/members", {"username": "bob", "role": "member"}, admin)
        self.fund("organization", organization["id"], 10)
        status, payload, _ = self.request("POST", f"/api/organizations/{organization['id']}/credits/allocate",
                                          {"target_type": "user", "target_id": self.user_id("bob"), "amount": 6}, admin)
        self.assertEqual(status, 200, payload)
        self.assertEqual(self.balance("user", self.user_id("bob")), 6)
        self.assertEqual(self.balance("organization", organization["id"]), 4)

    def test_a_workspace_allocates_to_its_own_member(self) -> None:
        admin = self.setup_admin()
        workspace_id = self.active_workspace(admin)["workspace"]["id"]
        bob = self.create_and_login(admin, "bob")
        self.request("POST", "/api/workspaces/members", {"username": "bob", "role": "editor"}, admin)
        self.fund("workspace", workspace_id, 10)
        status, payload, _ = self.request("POST", "/api/workspaces/credits/allocate",
                                          {"target_id": self.user_id("bob"), "amount": 7}, admin)
        self.assertEqual(status, 200, payload)
        self.assertEqual(self.balance("user", self.user_id("bob")), 7)
        self.assertEqual(self.balance("workspace", workspace_id), 3)


class AllocationPermissionTest(AllocationTestCase):
    def test_a_plain_organization_member_cannot_allocate(self) -> None:
        admin = self.setup_admin()
        organization = self.make_organization(admin)
        bob = self.create_and_login(admin, "bob")
        self.request("POST", f"/api/organizations/{organization['id']}/members", {"username": "bob", "role": "member"}, admin)
        self.fund("organization", organization["id"], 10)
        status, _, _ = self.request("POST", f"/api/organizations/{organization['id']}/credits/allocate",
                                    {"target_type": "user", "target_id": self.user_id("bob"), "amount": 5}, bob)
        self.assertEqual(status, 403)
        self.assertEqual(self.balance("organization", organization["id"]), 10)

    def test_a_non_member_sees_nothing_and_cannot_allocate(self) -> None:
        admin = self.setup_admin()
        organization = self.make_organization(admin)
        bob = self.create_and_login(admin, "bob")
        self.fund("organization", organization["id"], 10)
        status, _, _ = self.request("POST", f"/api/organizations/{organization['id']}/credits/allocate",
                                    {"target_type": "user", "target_id": self.user_id("bob"), "amount": 5}, bob)
        self.assertEqual(status, 404)
        status, _, _ = self.request("GET", f"/api/organizations/{organization['id']}/credits", auth=bob)
        self.assertEqual(status, 404)

    def test_credits_never_leak_across_organizations(self) -> None:
        admin = self.setup_admin()
        first, second = self.make_organization(admin), self.make_organization(admin)
        status, outside, _ = self.request("POST", f"/api/organizations/{second['id']}/workspaces", {"name": "Other org team"}, admin)
        self.assertEqual(status, 201)
        self.fund("organization", first["id"], 10)
        status, _, _ = self.request("POST", f"/api/organizations/{first['id']}/credits/allocate",
                                    {"target_type": "workspace", "target_id": outside["id"], "amount": 5}, admin)
        self.assertEqual(status, 404)
        self.assertEqual(self.balance("workspace", outside["id"]), 0)
        self.assertEqual(self.balance("organization", first["id"]), 10)

    def test_an_organization_cannot_fund_a_stranger(self) -> None:
        admin = self.setup_admin()
        organization = self.make_organization(admin)
        self.create_and_login(admin, "bob")
        self.fund("organization", organization["id"], 10)
        status, _, _ = self.request("POST", f"/api/organizations/{organization['id']}/credits/allocate",
                                    {"target_type": "user", "target_id": self.user_id("bob"), "amount": 5}, admin)
        self.assertEqual(status, 404)
        self.assertEqual(self.balance("user", self.user_id("bob")), 0)

    def test_a_workspace_cannot_fund_a_non_member(self) -> None:
        admin = self.setup_admin()
        workspace_id = self.active_workspace(admin)["workspace"]["id"]
        self.create_and_login(admin, "bob")
        self.fund("workspace", workspace_id, 10)
        status, _, _ = self.request("POST", "/api/workspaces/credits/allocate",
                                    {"target_id": self.user_id("bob"), "amount": 5}, admin)
        self.assertEqual(status, 404)

    def test_an_editor_cannot_allocate_workspace_credits(self) -> None:
        admin = self.setup_admin()
        workspace_id = self.active_workspace(admin)["workspace"]["id"]
        bob = self.create_and_login(admin, "bob")
        self.request("POST", "/api/workspaces/members", {"username": "bob", "role": "editor"}, admin)
        self.request("POST", "/api/me/workspace", {"workspace_id": workspace_id}, bob)
        self.fund("workspace", workspace_id, 10)
        status, _, _ = self.request("POST", "/api/workspaces/credits/allocate",
                                    {"target_id": self.user_id("bob"), "amount": 5}, bob)
        self.assertEqual(status, 403)


class ResolutionOrderTest(AllocationTestCase):
    def configure_ai(self) -> None:
        s = self.server
        with s.db() as connection:
            connection.execute("INSERT INTO instance_settings (name, value) VALUES (?, ?)",
                               ("ai_provider_openai", s.encrypt_secrets({"api_key": "k", "base_url": "https://ai.example/v1", "model": "m"})))

    def generate(self, auth: dict) -> int:
        s = self.server
        original = s.request_json
        s.request_json = lambda url, payload, headers=None: {"choices": [{"message": {"content": "copy"}}]}
        try:
            status, _, _ = self.request("POST", "/api/ai/generate", {"provider": "OpenAI", "model": "m", "instruction": "x", "draft": "", "channels": ["X"]}, auth)
        finally:
            s.request_json = original
        return status

    def organization_workspace(self, admin: dict) -> tuple[dict, dict]:
        organization = self.make_organization(admin)
        status, workspace, _ = self.request("POST", f"/api/organizations/{organization['id']}/workspaces", {"name": "Campaigns"}, admin)
        self.assertEqual(status, 201)
        self.request("POST", "/api/me/workspace", {"workspace_id": workspace["id"]}, admin)
        return organization, workspace

    def test_a_users_own_credits_are_spent_first(self) -> None:
        admin = self.setup_admin()
        self.configure_ai()
        organization, workspace = self.organization_workspace(admin)
        self.fund("user", self.user_id("admin-user"), 3)
        self.fund("workspace", workspace["id"], 3)
        self.fund("organization", organization["id"], 3)
        self.assertEqual(self.generate(admin), 200)
        self.assertEqual(self.balance("user", self.user_id("admin-user")), 2)
        self.assertEqual(self.balance("workspace", workspace["id"]), 3)
        self.assertEqual(self.balance("organization", organization["id"]), 3)

    def test_the_workspace_pays_when_the_user_has_none(self) -> None:
        admin = self.setup_admin()
        self.configure_ai()
        organization, workspace = self.organization_workspace(admin)
        self.fund("workspace", workspace["id"], 3)
        self.fund("organization", organization["id"], 3)
        self.assertEqual(self.generate(admin), 200)
        self.assertEqual(self.balance("workspace", workspace["id"]), 2)
        self.assertEqual(self.balance("organization", organization["id"]), 3)

    def test_the_organization_pays_last(self) -> None:
        admin = self.setup_admin()
        self.configure_ai()
        organization, workspace = self.organization_workspace(admin)
        self.fund("organization", organization["id"], 3)
        self.assertEqual(self.generate(admin), 200)
        self.assertEqual(self.balance("organization", organization["id"]), 2)

    def test_generation_is_refused_when_no_account_in_the_chain_can_pay(self) -> None:
        admin = self.setup_admin()
        self.configure_ai()
        organization, workspace = self.organization_workspace(admin)
        self.assertEqual(self.generate(admin), 400)
        self.assertEqual(self.balance("organization", organization["id"]), 0)

    def test_a_media_refund_returns_to_the_account_that_paid(self) -> None:
        s = self.server
        admin = self.setup_admin()
        self.configure_ai()
        organization, workspace = self.organization_workspace(admin)
        self.fund("organization", organization["id"], 5)
        status, job, _ = self.request("POST", "/api/media/jobs", {"kind": "image", "prompt": "a cat", "provider": "OpenAI", "model": "gpt-image-1"}, admin)
        self.assertEqual(status, 201, job)
        self.assertEqual(self.balance("organization", organization["id"]), 4)
        original = s.request_json
        s.request_json = lambda url, payload, headers=None: (_ for _ in ()).throw(s.ProviderError("provider unavailable"))
        try:
            s.run_media_job(dict(s.claim_media_job()))
        finally:
            s.request_json = original
        self.assertEqual(self.balance("organization", organization["id"]), 5)
        self.assertEqual(self.balance("workspace", workspace["id"]), 0)


class BalanceListingTest(AllocationTestCase):
    def test_a_member_sees_the_accounts_that_fund_their_own_actions(self) -> None:
        admin = self.setup_admin()
        organization = self.make_organization(admin)
        status, workspace, _ = self.request("POST", f"/api/organizations/{organization['id']}/workspaces", {"name": "Campaigns"}, admin)
        self.request("POST", "/api/me/workspace", {"workspace_id": workspace["id"]}, admin)
        self.fund("user", self.user_id("admin-user"), 1)
        self.fund("workspace", workspace["id"], 2)
        self.fund("organization", organization["id"], 3)
        status, payload, _ = self.request("GET", "/api/workspaces/credits", auth=admin)
        self.assertEqual(status, 200)
        self.assertEqual(payload["enforced"], True)
        self.assertEqual([(item["owner_type"], item["balance"]) for item in payload["accounts"]],
                         [("user", 1), ("workspace", 2), ("organization", 3)])

    def test_an_organization_admin_sees_workspace_and_member_balances(self) -> None:
        admin = self.setup_admin()
        organization = self.make_organization(admin)
        status, workspace, _ = self.request("POST", f"/api/organizations/{organization['id']}/workspaces", {"name": "Campaigns"}, admin)
        self.fund("organization", organization["id"], 9)
        self.fund("workspace", workspace["id"], 4)
        status, payload, _ = self.request("GET", f"/api/organizations/{organization['id']}/credits", auth=admin)
        self.assertEqual(status, 200)
        self.assertEqual(payload["balance"], 9)
        self.assertEqual([(item["name"], item["balance"]) for item in payload["workspaces"]], [("Campaigns", 4)])
        self.assertEqual([(item["username"], item["balance"]) for item in payload["members"]], [("admin-user", 0)])


if __name__ == "__main__":
    unittest.main()
