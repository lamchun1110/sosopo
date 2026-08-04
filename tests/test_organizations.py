"""Organization layer: membership, org-scoped workspaces, and isolation.

CLAUDE.md's hierarchy is Organization -> Team -> User. Workspaces are the team
level; an organization groups them. Personal workspaces stay org-less, and
workspace roles keep governing content access regardless of organization role.
"""

from __future__ import annotations

import unittest

try:
    from tests.test_workspaces import WorkspaceHttpCase
except ImportError:
    from test_workspaces import WorkspaceHttpCase


class OrganizationTestCase(WorkspaceHttpCase):
    def create_organization(self, auth: dict, name: str = "Acme Marketing") -> dict:
        status, organization, _ = self.request("POST", "/api/organizations", {"name": name}, auth)
        self.assertEqual(status, 201)
        return organization


class OrganizationCreationTest(OrganizationTestCase):
    def test_creator_becomes_the_organization_owner(self) -> None:
        admin = self.setup_admin()
        organization = self.create_organization(admin)
        self.assertEqual(organization["role"], "owner")
        self.assertEqual(organization["name"], "Acme Marketing")
        self.assertTrue(organization["slug"])
        status, payload, _ = self.request("GET", "/api/organizations", auth=admin)
        self.assertEqual(status, 200)
        self.assertEqual([(item["id"], item["role"]) for item in payload["organizations"]], [(organization["id"], "owner")])

    def test_organization_names_are_validated(self) -> None:
        admin = self.setup_admin()
        for name in ("", "   ", "x" * 200):
            status, payload, _ = self.request("POST", "/api/organizations", {"name": name}, admin)
            self.assertEqual(status, 400, payload)

    def test_slugs_never_collide(self) -> None:
        admin = self.setup_admin()
        first, second = self.create_organization(admin), self.create_organization(admin)
        self.assertNotEqual(first["slug"], second["slug"])

    def test_personal_workspaces_stay_organization_less(self) -> None:
        admin = self.setup_admin()
        self.create_organization(admin)
        with self.server.db() as connection:
            rows = connection.execute("SELECT organization_id FROM workspaces").fetchall()
        self.assertTrue(all(row["organization_id"] is None for row in rows))

    def test_migration_is_idempotent(self) -> None:
        self.server.setup_database()
        self.server.setup_database()
        with self.server.db() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) AS count FROM organizations").fetchone()["count"], 0)


class OrganizationWorkspaceTest(OrganizationTestCase):
    def test_workspaces_created_in_an_organization_are_listed_for_it(self) -> None:
        admin = self.setup_admin()
        organization = self.create_organization(admin)
        status, workspace, _ = self.request("POST", f"/api/organizations/{organization['id']}/workspaces", {"name": "Campaigns"}, admin)
        self.assertEqual(status, 201)
        status, payload, _ = self.request("GET", f"/api/organizations/{organization['id']}/workspaces", auth=admin)
        self.assertEqual(status, 200)
        self.assertEqual([item["id"] for item in payload["workspaces"]], [workspace["id"]])
        self.assertEqual(payload["workspaces"][0]["name"], "Campaigns")

    def test_an_organization_workspace_makes_its_creator_the_workspace_owner(self) -> None:
        admin = self.setup_admin()
        organization = self.create_organization(admin)
        status, workspace, _ = self.request("POST", f"/api/organizations/{organization['id']}/workspaces", {"name": "Campaigns"}, admin)
        self.assertEqual(status, 201)
        status, payload, _ = self.request("GET", "/api/workspaces", auth=admin)
        self.assertIn(workspace["id"], [item["id"] for item in payload["workspaces"]])
        self.assertEqual([item["role"] for item in payload["workspaces"] if item["id"] == workspace["id"]], ["owner"])

    def test_one_organizations_workspaces_never_appear_under_another(self) -> None:
        admin = self.setup_admin()
        first, second = self.create_organization(admin, "First"), self.create_organization(admin, "Second")
        self.request("POST", f"/api/organizations/{first['id']}/workspaces", {"name": "Only in first"}, admin)
        status, payload, _ = self.request("GET", f"/api/organizations/{second['id']}/workspaces", auth=admin)
        self.assertEqual(status, 200)
        self.assertEqual(payload["workspaces"], [])


class OrganizationIsolationTest(OrganizationTestCase):
    def test_non_members_see_nothing_and_cannot_act(self) -> None:
        admin = self.setup_admin()
        organization = self.create_organization(admin)
        self.request("POST", f"/api/organizations/{organization['id']}/workspaces", {"name": "Private"}, admin)
        bob = self.create_and_login(admin, "bob")
        status, payload, _ = self.request("GET", "/api/organizations", auth=bob)
        self.assertEqual((status, payload["organizations"]), (200, []))
        for method, path, body in (
            ("GET", f"/api/organizations/{organization['id']}/workspaces", None),
            ("GET", f"/api/organizations/{organization['id']}/members", None),
            ("POST", f"/api/organizations/{organization['id']}/workspaces", {"name": "Sneaky"}),
            ("POST", f"/api/organizations/{organization['id']}/members", {"username": "bob", "role": "admin"}),
        ):
            with self.subTest(path=path, method=method):
                status, _, _ = self.request(method, path, body, bob)
                self.assertEqual(status, 404)

    def test_unknown_organizations_are_not_found(self) -> None:
        admin = self.setup_admin()
        status, _, _ = self.request("GET", "/api/organizations/424242/workspaces", auth=admin)
        self.assertEqual(status, 404)


class OrganizationMemberTest(OrganizationTestCase):
    def add_member(self, auth: dict, organization: dict, username: str, role: str) -> int:
        status, member, _ = self.request("POST", f"/api/organizations/{organization['id']}/members", {"username": username, "role": role}, auth)
        self.assertEqual(status, 201, member)
        return int(member["user_id"])

    def test_members_are_listed_with_their_role(self) -> None:
        admin = self.setup_admin()
        organization = self.create_organization(admin)
        self.create_and_login(admin, "bob")
        self.add_member(admin, organization, "bob", "admin")
        status, payload, _ = self.request("GET", f"/api/organizations/{organization['id']}/members", auth=admin)
        self.assertEqual(status, 200)
        self.assertEqual({(item["username"], item["role"]) for item in payload["members"]}, {("admin-user", "owner"), ("bob", "admin")})

    def test_a_plain_member_cannot_create_workspaces_or_add_members(self) -> None:
        admin = self.setup_admin()
        organization = self.create_organization(admin)
        bob = self.create_and_login(admin, "bob")
        self.add_member(admin, organization, "bob", "member")
        status, payload, _ = self.request("GET", "/api/organizations", auth=bob)
        self.assertEqual([item["role"] for item in payload["organizations"]], ["member"])
        status, _, _ = self.request("POST", f"/api/organizations/{organization['id']}/workspaces", {"name": "Nope"}, bob)
        self.assertEqual(status, 403)
        status, _, _ = self.request("POST", f"/api/organizations/{organization['id']}/members", {"username": "admin-user", "role": "member"}, bob)
        self.assertEqual(status, 403)

    def test_an_organization_admin_can_create_workspaces(self) -> None:
        admin = self.setup_admin()
        organization = self.create_organization(admin)
        bob = self.create_and_login(admin, "bob")
        self.add_member(admin, organization, "bob", "admin")
        status, workspace, _ = self.request("POST", f"/api/organizations/{organization['id']}/workspaces", {"name": "Bob's team"}, bob)
        self.assertEqual(status, 201)
        status, payload, _ = self.request("GET", f"/api/organizations/{organization['id']}/workspaces", auth=bob)
        self.assertEqual([item["id"] for item in payload["workspaces"]], [workspace["id"]])

    def test_organization_role_does_not_grant_workspace_content_access(self) -> None:
        """Workspace roles keep governing content; org membership never bypasses them."""
        admin = self.setup_admin()
        organization = self.create_organization(admin)
        status, workspace, _ = self.request("POST", f"/api/organizations/{organization['id']}/workspaces", {"name": "Campaigns"}, admin)
        self.assertEqual(status, 201)
        bob = self.create_and_login(admin, "bob")
        self.add_member(admin, organization, "bob", "admin")
        status, _, _ = self.request("POST", "/api/me/workspace", {"workspace_id": workspace["id"]}, bob)
        self.assertEqual(status, 403)

    def test_members_must_exist_and_hold_a_known_role(self) -> None:
        admin = self.setup_admin()
        organization = self.create_organization(admin)
        status, _, _ = self.request("POST", f"/api/organizations/{organization['id']}/members", {"username": "ghost", "role": "admin"}, admin)
        self.assertEqual(status, 404)
        self.create_and_login(admin, "bob")
        status, _, _ = self.request("POST", f"/api/organizations/{organization['id']}/members", {"username": "bob", "role": "sovereign"}, admin)
        self.assertEqual(status, 400)


if __name__ == "__main__":
    unittest.main()
