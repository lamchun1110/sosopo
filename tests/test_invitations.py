"""Invitation, self-signup, and hosted-identity regression tests."""

from __future__ import annotations

import hashlib
import os
import unittest

try:
    from tests.test_workspaces import WorkspaceHttpCase
except ImportError:
    from test_workspaces import WorkspaceHttpCase


class InvitationTest(WorkspaceHttpCase):
    def invite(self, admin: dict, email: str = "new-user@example.com", role: str = "editor") -> dict:
        status, payload, _ = self.request("POST", "/api/workspaces/invitations", {"email": email, "role": role}, admin)
        self.assertEqual(status, 201)
        return payload

    @staticmethod
    def token_from(payload: dict) -> str:
        return payload["invite_url"].split("token=")[1]

    def test_invitation_tokens_are_stored_hashed_and_single_use(self) -> None:
        admin = self.setup_admin()
        invitation = self.invite(admin)
        self.assertFalse(invitation["email_sent"])
        token = self.token_from(invitation)
        with self.server.db() as connection:
            row = connection.execute("SELECT token_hash FROM workspace_invitations WHERE id = ?", (invitation["id"],)).fetchone()
        self.assertNotEqual(row["token_hash"], token)
        self.assertEqual(row["token_hash"], hashlib.sha256(token.encode()).hexdigest())
        status, _, _ = self.request("POST", f"/api/invitations/{token}/accept", {"username": "invited-user", "password": "a-long-enough-password"})
        self.assertEqual(status, 201)
        status, _, _ = self.request("POST", f"/api/invitations/{token}/accept", {"username": "second-user", "password": "a-long-enough-password"})
        self.assertEqual(status, 400)

    def test_accept_creates_user_with_membership_in_invited_workspace(self) -> None:
        admin = self.setup_admin()
        admin_workspace = self.active_workspace(admin)["workspace"]["id"]
        token = self.token_from(self.invite(admin, role="viewer"))
        status, lookup, _ = self.request("GET", f"/api/invitations/{token}")
        self.assertEqual(status, 200)
        self.assertEqual(lookup["role"], "viewer")
        status, _, auth = self.request("POST", f"/api/invitations/{token}/accept", {"username": "invited-user", "password": "a-long-enough-password"})
        self.assertEqual(status, 201)
        session = self.active_workspace(auth)
        self.assertEqual(session["workspace"]["id"], admin_workspace)
        self.assertEqual(session["workspace"]["role"], "viewer")

    def test_signed_in_user_accepts_invitation_and_switches_workspace(self) -> None:
        admin = self.setup_admin()
        admin_workspace = self.active_workspace(admin)["workspace"]["id"]
        bob = self.create_and_login(admin, "bob")
        token = self.token_from(self.invite(admin, email="bob@example.com"))
        status, _, _ = self.request("POST", f"/api/invitations/{token}/accept", {}, bob)
        self.assertEqual(status, 200)
        session = self.active_workspace(bob)
        self.assertEqual(session["workspace"]["id"], admin_workspace)
        self.assertEqual(session["workspace"]["role"], "editor")

    def test_expired_invitation_is_rejected(self) -> None:
        admin = self.setup_admin()
        invitation = self.invite(admin)
        token = self.token_from(invitation)
        with self.server.db() as connection:
            connection.execute("UPDATE workspace_invitations SET expires_at = ? WHERE id = ?", ("2020-01-01T00:00:00+00:00", invitation["id"]))
        status, _, _ = self.request("GET", f"/api/invitations/{token}")
        self.assertEqual(status, 404)
        status, _, _ = self.request("POST", f"/api/invitations/{token}/accept", {"username": "late-user", "password": "a-long-enough-password"})
        self.assertEqual(status, 400)

    def test_revoked_invitation_cannot_be_used(self) -> None:
        admin = self.setup_admin()
        invitation = self.invite(admin)
        token = self.token_from(invitation)
        status, _, _ = self.request("POST", f"/api/workspaces/invitations/{invitation['id']}/revoke", {}, admin)
        self.assertEqual(status, 200)
        status, _, _ = self.request("POST", f"/api/invitations/{token}/accept", {"username": "revoked-user", "password": "a-long-enough-password"})
        self.assertEqual(status, 400)

    def test_invitation_role_rules_match_membership_rules(self) -> None:
        admin = self.setup_admin()
        admin_workspace = self.active_workspace(admin)["workspace"]["id"]
        bob = self.create_and_login(admin, "bob")
        status, member, _ = self.request("POST", "/api/workspaces/members", {"username": "bob", "role": "editor"}, admin)
        self.assertEqual(status, 201)
        status, _, _ = self.request("POST", "/api/me/workspace", {"workspace_id": admin_workspace}, bob)
        self.assertEqual(status, 200)
        status, _, _ = self.request("POST", "/api/workspaces/invitations", {"email": "x@example.com", "role": "viewer"}, bob)
        self.assertEqual(status, 403)
        status, _, _ = self.request("POST", f"/api/workspaces/members/{member['user_id']}/role", {"role": "admin"}, admin)
        self.assertEqual(status, 200)
        status, _, _ = self.request("POST", "/api/workspaces/invitations", {"email": "x@example.com", "role": "admin"}, bob)
        self.assertEqual(status, 403)
        status, _, _ = self.request("POST", "/api/workspaces/invitations", {"email": "x@example.com", "role": "editor"}, bob)
        self.assertEqual(status, 201)

    def test_self_signup_is_disabled_by_default_and_gated_by_configuration(self) -> None:
        self.setup_admin()
        status, _, _ = self.request("POST", "/api/signup", {"username": "walk-in", "password": "a-long-enough-password"})
        self.assertEqual(status, 403)
        os.environ["SOSOPO_ALLOW_SELF_SIGNUP"] = "true"
        try:
            status, _, auth = self.request("POST", "/api/signup", {"username": "walk-in", "password": "a-long-enough-password"})
            self.assertEqual(status, 201)
            session = self.active_workspace(auth)
            self.assertEqual(session["workspace"]["role"], "owner")
        finally:
            os.environ.pop("SOSOPO_ALLOW_SELF_SIGNUP", None)

    def test_hosted_mode_enables_signup_unless_disabled(self) -> None:
        s = self.server
        self.assertEqual(s.deployment_mode(), "self_hosted")
        self.assertFalse(s.self_signup_allowed())
        os.environ["SOSOPO_DEPLOYMENT_MODE"] = "hosted"
        try:
            self.assertEqual(s.deployment_mode(), "hosted")
            self.assertTrue(s.self_signup_allowed())
            os.environ["SOSOPO_ALLOW_SELF_SIGNUP"] = "false"
            self.assertFalse(s.self_signup_allowed())
        finally:
            os.environ.pop("SOSOPO_DEPLOYMENT_MODE", None)
            os.environ.pop("SOSOPO_ALLOW_SELF_SIGNUP", None)


if __name__ == "__main__":
    unittest.main()
