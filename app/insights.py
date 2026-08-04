"""Workspace metrics, and the AI summary built from them.

:func:`workspace_status` is the single source for the dashboard payload and
for the summary prompt, so the two can never describe different numbers.

The summary is deliberately narrow: it reads metrics and returns prose. It
changes nothing, and the prompt is assembled from a fixed list of aggregate
counters — never from rows that could carry a credential, a token, or post
content. What reaches the provider is countable facts about the workspace.
"""

from __future__ import annotations

import json

try:  # package import (tests, `python -m app.server`)
    from .connections import connection_health
    from .database import Database
    from .plans import current_period, plan_limits, usage_amount
    from .workspaces import workspace_plan, workspace_setting
except ImportError:  # script import (`python /app/app/server.py`)
    from connections import connection_health
    from database import Database
    from plans import current_period, plan_limits, usage_amount
    from workspaces import workspace_plan, workspace_setting


MAX_SUMMARY_LENGTH = 4_000


def workspace_status(connection: Database, workspace_id: int, since: str) -> dict:
    """Aggregate counters for one workspace. No credentials, no post content."""
    posts = {row["state"]: row["count"] for row in connection.execute(
        "SELECT state, COUNT(*) AS count FROM posts WHERE workspace_id = ? GROUP BY state", (workspace_id,)).fetchall()}
    deliveries = [dict(row) for row in connection.execute(
        "SELECT deliveries.provider, deliveries.status, COUNT(*) AS count FROM deliveries JOIN posts ON posts.id = deliveries.post_id"
        " WHERE posts.workspace_id = ? AND deliveries.created_at >= ? GROUP BY deliveries.provider, deliveries.status",
        (workspace_id, since)).fetchall()]
    accounts = [dict(row) for row in connection.execute(
        "SELECT is_active, token_expires_at FROM connections WHERE workspace_id = ?", (workspace_id,)).fetchall()]
    members = int(connection.execute(
        "SELECT COUNT(*) AS count FROM workspace_memberships WHERE workspace_id = ?", (workspace_id,)).fetchone()["count"])
    media_jobs = {row["status"]: row["count"] for row in connection.execute(
        "SELECT status, COUNT(*) AS count FROM media_jobs WHERE workspace_id = ? GROUP BY status", (workspace_id,)).fetchall()}
    plan = workspace_plan(connection, workspace_id)
    usage = {
        "posts_created": usage_amount(connection, workspace_id, "posts_created"),
        "ai_generations": usage_amount(connection, workspace_id, "ai_generations"),
        "ai_media": usage_amount(connection, workspace_id, "ai_media"),
        "storage_bytes": usage_amount(connection, workspace_id, "storage_bytes", period="total"),
    }
    cap = workspace_setting(connection, workspace_id, "ai_monthly_cap")
    health = {"active": 0, "expiring_soon": 0, "expired": 0, "disabled": 0}
    for account in accounts:
        health[connection_health(account)] += 1
    return {
        "plan": plan,
        "limits": plan_limits(plan),
        "usage": usage,
        "ai_monthly_cap": int(cap) if cap is not None else None,
        "posts": posts,
        "deliveries_30d": deliveries,
        "connection_health": health,
        "members": members,
        "media_jobs": media_jobs,
        "period": current_period(),
    }


def summary_prompt(status: dict) -> str:
    """Render the metrics for the model.

    Only the keys assembled by :func:`workspace_status` are serialized, so a
    future column on `connections` or `posts` cannot silently start leaking
    into an outbound prompt.
    """
    return (
        "Summarize this social media workspace for its administrator.\n"
        "Write plain language in at most four short paragraphs: what the numbers show, "
        "anything that looks like a problem, and two or three concrete suggestions.\n"
        "Do not invent metrics that are not present. Do not repeat the JSON back.\n\n"
        f"Metrics (last 30 days where dated):\n{json.dumps(status, indent=2, sort_keys=True)}"
    )
