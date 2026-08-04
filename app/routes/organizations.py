"""Organizations: the administrative level above workspaces."""

from __future__ import annotations

from http import HTTPStatus
from typing import Any


def credit_amount(value: object) -> int | None:
    """Accept only a positive whole number; reject floats, text, and negatives."""
    return int(value) if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None

try:  # package import (tests, `python -m app.server`)
    from ..audit import audit
    from ..credits import account_balance, allocate_credits, credits_enforced
    from ..errors import ProviderError
    from ..config import MAX_WORKSPACE_NAME_LENGTH
    from ..database import Record, db
    from ..organizations import (
        MAX_ORGANIZATION_NAME_LENGTH,
        ORGANIZATION_ROLES,
        create_organization,
        organization_members,
        organization_membership,
        organization_role_allows,
        organization_workspaces,
        save_organization_member,
        user_organizations,
    )
    from ..workspaces import create_workspace
except ImportError:  # script import (`python /app/app/server.py`)
    from audit import audit
    from credits import account_balance, allocate_credits, credits_enforced
    from errors import ProviderError
    from config import MAX_WORKSPACE_NAME_LENGTH
    from database import Record, db
    from organizations import (
        MAX_ORGANIZATION_NAME_LENGTH,
        ORGANIZATION_ROLES,
        create_organization,
        organization_members,
        organization_membership,
        organization_role_allows,
        organization_workspaces,
        save_organization_member,
        user_organizations,
    )
    from workspaces import create_workspace


class OrganizationRoutes:
    """Organization membership, org-scoped workspace lists, and org workspace creation.

    Mixed into ``Handler``; every method returns True once it has answered.

    Organizations a caller does not belong to answer 404 rather than 403, so
    membership itself is not discoverable by probing IDs.
    """

    def _organization_id(self, path: str, position: int = 3) -> int | None:
        try:
            return int(path.split("/")[position])
        except (IndexError, ValueError):
            return None

    def _require_organization(self, path: str, minimum_role: str = "member") -> Record | None:
        """Resolve the caller's membership, answering 404/403 when it is insufficient."""
        organization_id = self._organization_id(path)
        session = self._session()
        if organization_id is None or session is None:
            self._json({"error": "Not found."}, HTTPStatus.NOT_FOUND)
            return None
        with db() as connection:
            membership = organization_membership(connection, organization_id, session["user_id"])
        if membership is None:
            self._json({"error": "Not found."}, HTTPStatus.NOT_FOUND)
            return None
        if not organization_role_allows(membership["role"], minimum_role):
            self._json({"error": f"Organization {minimum_role} access required."}, HTTPStatus.FORBIDDEN)
            return None
        return membership

    def get_organizations(self, path: str) -> bool:
        """Handle one organization GET; True when answered."""
        if path == "/api/organizations":
            session = self._session()
            with db() as connection:
                organizations = [
                    {"id": item["id"], "name": item["name"], "slug": item["slug"], "role": item["role"],
                     "is_owner": item["owner_user_id"] == session["user_id"]}
                    for item in user_organizations(connection, session["user_id"])
                ]
            self._json({"organizations": organizations}); return True
        if path.startswith("/api/organizations/") and path.endswith("/workspaces"):
            membership = self._require_organization(path)
            if membership is None:
                return True
            with db() as connection:
                workspaces = [
                    {"id": item["id"], "name": item["name"], "slug": item["slug"], "plan": item["plan"],
                     "created_at": item["created_at"]}
                    for item in organization_workspaces(connection, int(membership["organization_id"]))
                ]
            self._json({"workspaces": workspaces}); return True
        if path.startswith("/api/organizations/") and path.endswith("/credits"):
            membership = self._require_organization(path, "admin")
            if membership is None:
                return True
            organization_id = int(membership["organization_id"])
            with db() as connection:
                balance = account_balance(connection, "organization", organization_id)
                workspaces = [{"id": item["id"], "name": item["name"], "balance": account_balance(connection, "workspace", int(item["id"]))}
                              for item in organization_workspaces(connection, organization_id)]
                members = [{"user_id": item["user_id"], "username": item["username"], "balance": account_balance(connection, "user", int(item["user_id"]))}
                           for item in organization_members(connection, organization_id)]
            self._json({"enforced": credits_enforced(), "balance": balance, "workspaces": workspaces, "members": members}); return True
        if path.startswith("/api/organizations/") and path.endswith("/members"):
            membership = self._require_organization(path)
            if membership is None:
                return True
            with db() as connection:
                members = [
                    {"user_id": item["user_id"], "username": item["username"], "role": item["role"],
                     "created_at": item["created_at"]}
                    for item in organization_members(connection, int(membership["organization_id"]))
                ]
            self._json({"members": members}); return True
        return False

    def post_organizations(self, path: str, payload: dict[str, Any], session: Record) -> bool:
        """Handle one organization POST; True when answered."""
        if path == "/api/organizations":
            name = str(payload.get("name", "")).strip()
            if not name or len(name) > MAX_ORGANIZATION_NAME_LENGTH:
                self._json({"error": f"Use an organization name of 1 to {MAX_ORGANIZATION_NAME_LENGTH} characters."}, HTTPStatus.BAD_REQUEST); return True
            with db() as connection:
                organization_id = create_organization(connection, name, session["user_id"])
                created = connection.execute("SELECT id, name, slug FROM organizations WHERE id = ?", (organization_id,)).fetchone()
            audit(session["user_id"], "organization.created", "organization", organization_id, f"Created organization {name}", self._source_ip())
            self._json({"id": created["id"], "name": created["name"], "slug": created["slug"], "role": "owner"}, HTTPStatus.CREATED); return True
        if path.startswith("/api/organizations/") and path.endswith("/workspaces"):
            membership = self._require_organization(path, "admin")
            if membership is None:
                return True
            name = str(payload.get("name", "")).strip()
            if not name or len(name) > MAX_WORKSPACE_NAME_LENGTH:
                self._json({"error": f"Use a workspace name of 1 to {MAX_WORKSPACE_NAME_LENGTH} characters."}, HTTPStatus.BAD_REQUEST); return True
            organization_id = int(membership["organization_id"])
            with db() as connection:
                workspace_id = create_workspace(connection, name, session["user_id"], organization_id=organization_id)
                created = connection.execute("SELECT id, name, slug FROM workspaces WHERE id = ?", (workspace_id,)).fetchone()
            audit(session["user_id"], "organization.workspace_created", "workspace", workspace_id, f"Created workspace {name} in organization {organization_id}", self._source_ip(), workspace_id=workspace_id)
            self._json({"id": created["id"], "name": created["name"], "slug": created["slug"], "organization_id": organization_id, "role": "owner"}, HTTPStatus.CREATED); return True
        if path.startswith("/api/organizations/") and path.endswith("/credits/allocate"):
            membership = self._require_organization(path, "admin")
            if membership is None:
                return True
            organization_id = int(membership["organization_id"])
            target_type = str(payload.get("target_type", "")).strip()
            if target_type not in {"workspace", "user"}:
                self._json({"error": "Allocate to a workspace or a user."}, HTTPStatus.BAD_REQUEST); return True
            amount, target_id = credit_amount(payload.get("amount")), credit_amount(payload.get("target_id"))
            if amount is None or target_id is None:
                self._json({"error": "Use a positive whole number of credits and a valid target."}, HTTPStatus.BAD_REQUEST); return True
            with db() as connection:
                if target_type == "workspace":
                    owned = connection.execute("SELECT 1 FROM workspaces WHERE id = ? AND organization_id = ?", (target_id, organization_id)).fetchone()
                else:
                    owned = connection.execute("SELECT 1 FROM organization_memberships WHERE user_id = ? AND organization_id = ?", (target_id, organization_id)).fetchone()
                if owned is None:
                    self._json({"error": "Not found."}, HTTPStatus.NOT_FOUND); return True
                try:
                    allocate_credits(connection, ("organization", organization_id), (target_type, target_id), amount, session["user_id"])
                except ProviderError as error:
                    self._json({"error": str(error)}, HTTPStatus.BAD_REQUEST); return True
                balance = account_balance(connection, "organization", organization_id)
            audit(session["user_id"], "credits.allocated", "organization", organization_id, f"Allocated {amount} credits to {target_type} {target_id}", self._source_ip())
            self._json({"balance": balance, "target_type": target_type, "target_id": target_id, "amount": amount}); return True
        if path.startswith("/api/organizations/") and path.endswith("/members"):
            membership = self._require_organization(path, "admin")
            if membership is None:
                return True
            username, role = str(payload.get("username", "")).strip(), str(payload.get("role", "member")).strip()
            if role not in ORGANIZATION_ROLES:
                self._json({"error": f"Choose one of: {', '.join(ORGANIZATION_ROLES)}."}, HTTPStatus.BAD_REQUEST); return True
            organization_id = int(membership["organization_id"])
            with db() as connection:
                user = connection.execute("SELECT id FROM users WHERE username = ? AND is_active = 1", (username,)).fetchone()
                if user is None:
                    self._json({"error": "No active user with that username."}, HTTPStatus.NOT_FOUND); return True
                save_organization_member(connection, organization_id, int(user["id"]), role)
            audit(session["user_id"], "organization.member_saved", "organization", organization_id, f"Set {username} to {role}", self._source_ip())
            self._json({"user_id": user["id"], "username": username, "role": role}, HTTPStatus.CREATED); return True
        return False
