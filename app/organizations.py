"""Organizations: the level above workspaces in CLAUDE.md's hierarchy.

Organization -> Team -> User, where a workspace is the team. An organization
groups workspaces for shared administration and (later) shared credit
allocation. Two rules keep this simple and safe:

- Organization membership is **administrative**, not a content grant. Being an
  organization owner does not let you read a workspace's posts; workspace
  membership and workspace roles keep governing content access.
- Membership is optional. Personal workspaces carry no ``organization_id``,
  and an installation that never creates an organization behaves exactly as
  it did before this layer existed.
"""

from __future__ import annotations

import re

try:  # package import (tests, `python -m app.server`)
    from .config import now
    from .database import Database, Record, insert_id
except ImportError:  # script import (`python /app/app/server.py`)
    from config import now
    from database import Database, Record, insert_id


ORGANIZATION_ROLES = ("owner", "admin", "member")
ORGANIZATION_ROLE_RANK = {"member": 0, "admin": 1, "owner": 2}
MAX_ORGANIZATION_NAME_LENGTH = 80


def organization_role_allows(role: object, minimum: str) -> bool:
    return ORGANIZATION_ROLE_RANK.get(str(role or ""), -1) >= ORGANIZATION_ROLE_RANK[minimum]


def organization_slug(connection: Database, name: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", name.casefold()).strip("-")[:40] or "organization"
    slug, counter = base, 1
    while connection.execute("SELECT 1 FROM organizations WHERE slug = ?", (slug,)).fetchone():
        counter += 1
        slug = f"{base}-{counter}"
    return slug


def create_organization(connection: Database, name: str, owner_user_id: int) -> int:
    organization_id = insert_id(
        connection,
        "INSERT INTO organizations (name, slug, owner_user_id, status, created_at, updated_at) VALUES (?, ?, ?, 'active', ?, ?)",
        (name, organization_slug(connection, name), owner_user_id, now(), now()),
    )
    connection.execute(
        "INSERT INTO organization_memberships (organization_id, user_id, role, created_at, updated_at) VALUES (?, ?, 'owner', ?, ?)",
        (organization_id, owner_user_id, now(), now()),
    )
    return organization_id


def organization_membership(connection: Database, organization_id: int, user_id: int) -> Record | None:
    """Return the caller's membership, or None when they cannot see the organization."""
    return connection.execute(
        "SELECT organization_memberships.*, organizations.name AS organization_name, organizations.slug AS organization_slug"
        " FROM organization_memberships JOIN organizations ON organizations.id = organization_memberships.organization_id AND organizations.status = 'active'"
        " WHERE organization_memberships.organization_id = ? AND organization_memberships.user_id = ?",
        (organization_id, user_id),
    ).fetchone()


def user_organizations(connection: Database, user_id: int) -> list[Record]:
    return connection.execute(
        "SELECT organizations.id, organizations.name, organizations.slug, organizations.owner_user_id, organization_memberships.role"
        " FROM organization_memberships JOIN organizations ON organizations.id = organization_memberships.organization_id AND organizations.status = 'active'"
        " WHERE organization_memberships.user_id = ? ORDER BY organization_memberships.id",
        (user_id,),
    ).fetchall()


def organization_workspaces(connection: Database, organization_id: int) -> list[Record]:
    return connection.execute(
        "SELECT id, name, slug, owner_user_id, plan, status, created_at FROM workspaces"
        " WHERE organization_id = ? AND status = 'active' ORDER BY id",
        (organization_id,),
    ).fetchall()


def organization_members(connection: Database, organization_id: int) -> list[Record]:
    return connection.execute(
        "SELECT organization_memberships.user_id, organization_memberships.role, organization_memberships.created_at, users.username"
        " FROM organization_memberships JOIN users ON users.id = organization_memberships.user_id"
        " WHERE organization_memberships.organization_id = ? ORDER BY organization_memberships.id",
        (organization_id,),
    ).fetchall()


def save_organization_member(connection: Database, organization_id: int, user_id: int, role: str) -> None:
    """Add a member or change an existing member's role."""
    updated = connection.execute(
        "UPDATE organization_memberships SET role = ?, updated_at = ? WHERE organization_id = ? AND user_id = ?",
        (role, now(), organization_id, user_id),
    )
    if updated.rowcount == 0:
        connection.execute(
            "INSERT INTO organization_memberships (organization_id, user_id, role, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            (organization_id, user_id, role, now(), now()),
        )
