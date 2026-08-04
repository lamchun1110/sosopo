"""Stripe one-time credit top-ups, applied through the verified webhook."""

from __future__ import annotations

import hashlib
import hmac
import http.client
import json
import os
import unittest
from datetime import UTC, datetime

try:
    from tests.test_workspaces import WorkspaceHttpCase
except ImportError:
    from test_workspaces import WorkspaceHttpCase


class TopUpTestCase(WorkspaceHttpCase):
    def setUp(self) -> None:
        os.environ["SOSOPO_DEPLOYMENT_MODE"] = "hosted"
        os.environ["STRIPE_SECRET_KEY"] = "sk_test"
        os.environ["STRIPE_WEBHOOK_SECRET"] = "whsec_test_secret"
        os.environ["STRIPE_PRICE_CREDITS_SMALL"] = "price_credits_small"
        os.environ["SOSOPO_PUBLIC_URL"] = "https://sosopo.example"
        super().setUp()

    def tearDown(self) -> None:
        for name in ("SOSOPO_DEPLOYMENT_MODE", "STRIPE_SECRET_KEY", "STRIPE_WEBHOOK_SECRET", "STRIPE_PRICE_CREDITS_SMALL", "SOSOPO_PUBLIC_URL"):
            os.environ.pop(name, None)
        super().tearDown()

    def signed_webhook(self, event: dict) -> tuple[bytes, str]:
        raw = json.dumps(event).encode()
        stamp = str(int(datetime.now(UTC).timestamp()))
        digest = hmac.new(b"whsec_test_secret", f"{stamp}.".encode() + raw, hashlib.sha256).hexdigest()
        return raw, f"t={stamp},v1={digest}"

    def post_webhook(self, raw: bytes, signature: str) -> int:
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        connection.request("POST", "/api/billing/webhook", raw, {"Content-Type": "application/json", "Stripe-Signature": signature})
        status = connection.getresponse().status
        connection.close()
        return status

    def balance(self, owner_type: str, owner_id: int) -> int:
        with self.server.db() as connection:
            return self.server.account_balance(connection, owner_type, owner_id)

    def credits_event(self, event_id: str, metadata: dict) -> dict:
        return {"id": event_id, "type": "checkout.session.completed", "data": {"object": {"metadata": metadata}}}


class CreditPackTest(TopUpTestCase):
    def test_packs_are_listed_only_when_their_price_is_configured(self) -> None:
        packs = self.server.credit_packs()
        self.assertIn("small", packs)
        self.assertEqual(packs["small"]["credits"], 100)


class TopUpCheckoutTest(TopUpTestCase):
    def test_a_workspace_owner_starts_a_one_time_checkout(self) -> None:
        s = self.server
        admin = self.setup_admin()
        captured: dict = {}
        original = s.stripe_request
        s.stripe_request = lambda path, payload: captured.update(path=path, payload=payload) or {"url": "https://checkout.stripe.test/session"}
        try:
            status, payload, _ = self.request("POST", "/api/workspaces/billing/credits", {"pack": "small"}, admin)
        finally:
            s.stripe_request = original
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["url"], "https://checkout.stripe.test/session")
        self.assertEqual(captured["payload"]["mode"], "payment")
        self.assertEqual(captured["payload"]["line_items[0][price]"], "price_credits_small")
        self.assertEqual(captured["payload"]["metadata[kind]"], "credits")
        self.assertEqual(captured["payload"]["metadata[credits]"], "100")

    def test_an_unknown_pack_is_refused(self) -> None:
        admin = self.setup_admin()
        status, _, _ = self.request("POST", "/api/workspaces/billing/credits", {"pack": "enormous"}, admin)
        self.assertEqual(status, 400)

    def test_only_a_workspace_owner_can_buy_credits(self) -> None:
        admin = self.setup_admin()
        bob = self.create_and_login(admin, "bob")
        workspace_id = self.active_workspace(admin)["workspace"]["id"]
        self.request("POST", "/api/workspaces/members", {"username": "bob", "role": "admin"}, admin)
        self.request("POST", "/api/me/workspace", {"workspace_id": workspace_id}, bob)
        status, _, _ = self.request("POST", "/api/workspaces/billing/credits", {"pack": "small"}, bob)
        self.assertEqual(status, 403)


class TopUpWebhookTest(TopUpTestCase):
    def test_a_signed_credits_purchase_credits_the_workspace(self) -> None:
        admin = self.setup_admin()
        workspace_id = self.active_workspace(admin)["workspace"]["id"]
        raw, signature = self.signed_webhook(self.credits_event("evt_1", {"kind": "credits", "workspace_id": str(workspace_id), "credits": "100", "pack": "small"}))
        self.assertEqual(self.post_webhook(raw, signature), 200)
        self.assertEqual(self.balance("workspace", workspace_id), 100)

    def test_a_replayed_event_credits_exactly_once(self) -> None:
        admin = self.setup_admin()
        workspace_id = self.active_workspace(admin)["workspace"]["id"]
        raw, signature = self.signed_webhook(self.credits_event("evt_repeat", {"kind": "credits", "workspace_id": str(workspace_id), "credits": "100", "pack": "small"}))
        self.assertEqual(self.post_webhook(raw, signature), 200)
        self.assertEqual(self.post_webhook(raw, signature), 200)
        self.assertEqual(self.balance("workspace", workspace_id), 100)
        with self.server.db() as connection:
            rows = connection.execute("SELECT COUNT(*) AS count FROM credit_transactions WHERE reason = 'stripe_topup'").fetchone()
        self.assertEqual(rows["count"], 1)

    def test_an_organization_purchase_credits_the_organization(self) -> None:
        admin = self.setup_admin()
        status, organization, _ = self.request("POST", "/api/organizations", {"name": "Acme"}, admin)
        self.assertEqual(status, 201)
        raw, signature = self.signed_webhook(self.credits_event("evt_org", {"kind": "credits", "organization_id": str(organization["id"]), "credits": "250", "pack": "small"}))
        self.assertEqual(self.post_webhook(raw, signature), 200)
        self.assertEqual(self.balance("organization", organization["id"]), 250)

    def test_an_unsigned_purchase_credits_nothing(self) -> None:
        admin = self.setup_admin()
        workspace_id = self.active_workspace(admin)["workspace"]["id"]
        raw, _ = self.signed_webhook(self.credits_event("evt_bad", {"kind": "credits", "workspace_id": str(workspace_id), "credits": "100"}))
        self.assertEqual(self.post_webhook(raw, "t=1,v1=forged"), 400)
        self.assertEqual(self.balance("workspace", workspace_id), 0)

    def test_a_nonsense_credit_amount_is_ignored(self) -> None:
        admin = self.setup_admin()
        workspace_id = self.active_workspace(admin)["workspace"]["id"]
        for amount in ("-500", "many", "0"):
            raw, signature = self.signed_webhook(self.credits_event(f"evt_{amount}", {"kind": "credits", "workspace_id": str(workspace_id), "credits": amount}))
            self.assertEqual(self.post_webhook(raw, signature), 200)
        self.assertEqual(self.balance("workspace", workspace_id), 0)

    def test_subscription_events_still_change_the_plan(self) -> None:
        admin = self.setup_admin()
        workspace_id = self.active_workspace(admin)["workspace"]["id"]
        event = {"id": "evt_plan", "type": "checkout.session.completed",
                 "data": {"object": {"customer": "cus_1", "subscription": "sub_1", "metadata": {"workspace_id": str(workspace_id), "plan": "pro"}}}}
        raw, signature = self.signed_webhook(event)
        self.assertEqual(self.post_webhook(raw, signature), 200)
        with self.server.db() as connection:
            self.assertEqual(self.server.workspace_plan(connection, workspace_id), "pro")
        self.assertEqual(self.balance("workspace", workspace_id), 0)


if __name__ == "__main__":
    unittest.main()
