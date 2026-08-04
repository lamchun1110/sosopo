"""Workspaces, memberships, roles, and per-workspace settings."""

from __future__ import annotations

import re
import secrets

try:  # package import (tests, `python -m app.server`)
    from .config import WORKSPACE_ROLE_RANK, deployment_mode, now
    from .database import Database, Record, insert_id
    from .security import hash_password
except ImportError:  # script import (`python /app/app/server.py`)
    from config import WORKSPACE_ROLE_RANK, deployment_mode, now
    from database import Database, Record, insert_id
    from security import hash_password


def workspace_role_allows(role: object, minimum: str) -> bool:
    return WORKSPACE_ROLE_RANK.get(str(role or ""), -1) >= WORKSPACE_ROLE_RANK[minimum]


def workspace_slug(connection: Database, name: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", name.casefold()).strip("-")[:40] or "workspace"
    slug, counter = base, 1
    while connection.execute("SELECT 1 FROM workspaces WHERE slug = ?", (slug,)).fetchone():
        counter += 1
        slug = f"{base}-{counter}"
    return slug


def create_workspace(connection: Database, name: str, owner_user_id: int, plan: str | None = None, organization_id: int | None = None) -> int:
    """Create a workspace; organization_id is None for personal workspaces."""
    plan = plan or ("free" if deployment_mode() == "hosted" else "self_hosted")
    workspace_id = insert_id(
        connection,
        "INSERT INTO workspaces (name, slug, owner_user_id, plan, status, organization_id, created_at, updated_at) VALUES (?, ?, ?, ?, 'active', ?, ?, ?)",
        (name, workspace_slug(connection, name), owner_user_id, plan, organization_id, now(), now()),
    )
    connection.execute(
        "INSERT INTO workspace_memberships (workspace_id, user_id, role, invite_state, created_at, updated_at) VALUES (?, ?, 'owner', 'active', ?, ?)",
        (workspace_id, owner_user_id, now(), now()),
    )
    return workspace_id


def workspace_membership(connection: Database, workspace_id: int, user_id: int) -> Record | None:
    return connection.execute(
        "SELECT workspace_memberships.*, workspaces.name AS workspace_name, workspaces.slug AS workspace_slug"
        " FROM workspace_memberships JOIN workspaces ON workspaces.id = workspace_memberships.workspace_id AND workspaces.status = 'active'"
        " WHERE workspace_memberships.workspace_id = ? AND workspace_memberships.user_id = ? AND workspace_memberships.invite_state = 'active'",
        (workspace_id, user_id),
    ).fetchone()


def user_workspaces(connection: Database, user_id: int) -> list[Record]:
    return connection.execute(
        "SELECT workspaces.id, workspaces.name, workspaces.slug, workspaces.owner_user_id, workspace_memberships.role"
        " FROM workspace_memberships JOIN workspaces ON workspaces.id = workspace_memberships.workspace_id AND workspaces.status = 'active'"
        " WHERE workspace_memberships.user_id = ? AND workspace_memberships.invite_state = 'active' ORDER BY workspace_memberships.id",
        (user_id,),
    ).fetchall()


def default_workspace_id(connection: Database, user_id: int) -> int | None:
    workspaces = user_workspaces(connection, user_id)
    return int(workspaces[0]["id"]) if workspaces else None


def ensure_personal_workspace(connection: Database, user_id: int, username: str) -> int:
    """Give a user one personal workspace and adopt their pre-workspace data into it."""
    existing = default_workspace_id(connection, user_id)
    if existing is not None:
        return existing
    workspace_id = create_workspace(connection, f"{username}'s workspace", user_id)
    connection.execute("UPDATE posts SET workspace_id = ? WHERE user_id = ? AND workspace_id IS NULL", (workspace_id, user_id))
    connection.execute("UPDATE connections SET workspace_id = ? WHERE user_id = ? AND workspace_id IS NULL", (workspace_id, user_id))
    connection.execute("UPDATE audit_events SET workspace_id = ? WHERE user_id = ? AND workspace_id IS NULL", (workspace_id, user_id))
    return workspace_id


def migrate_users_to_workspaces(connection: Database) -> None:
    """Give every pre-workspace user an isolated personal workspace.

    Existing installations may hold several users whose posts and connections
    were private to each user. One workspace per user preserves exactly that
    isolation; merging everyone into a shared workspace would leak data.
    """
    users = connection.execute(
        "SELECT users.id, users.username FROM users LEFT JOIN workspace_memberships ON workspace_memberships.user_id = users.id"
        " WHERE workspace_memberships.id IS NULL ORDER BY users.id"
    ).fetchall()
    for user in users:
        ensure_personal_workspace(connection, int(user["id"]), str(user["username"]))


def workspace_plan(connection: Database, workspace_id: int) -> str:
    row = connection.execute("SELECT plan FROM workspaces WHERE id = ?", (workspace_id,)).fetchone()
    return str(row["plan"]) if row else "self_hosted"


def workspace_setting(connection: Database, workspace_id: int, name: str) -> str | None:
    row = connection.execute("SELECT value FROM workspace_settings WHERE workspace_id = ? AND name = ?", (workspace_id, name)).fetchone()
    return str(row["value"]) if row else None


def save_workspace_setting(connection: Database, workspace_id: int, name: str, value: str | None) -> None:
    connection.execute("DELETE FROM workspace_settings WHERE workspace_id = ? AND name = ?", (workspace_id, name))
    if value is not None:
        connection.execute("INSERT INTO workspace_settings (workspace_id, name, value) VALUES (?, ?, ?)", (workspace_id, name, value))


def create_local_user(connection: Database, username: str, password: str, role: str = "user", timezone: str = "UTC") -> int:
    salt = secrets.token_bytes(16)
    return insert_id(
        connection,
        "INSERT INTO users (username, password_salt, password_hash, role, timezone, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (username, salt.hex(), hash_password(password, salt), role, timezone, now()),
    )
