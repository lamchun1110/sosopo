"""AI content calendars: a brief becomes a set of reviewable drafts.

Two rules shape this module, both from CLAUDE.md's insistence that AI assists
rather than acts:

- **A plan produces drafts, never scheduled or published posts.** The AI's
  proposed time is stored as ``posts.suggested_for``, which nothing acts on.
  Turning a suggestion into a schedule stays the existing manual flow, so a
  bad plan can never reach an audience.
- **A malformed plan produces nothing.** :func:`parse_campaign_plan` is
  strict: one unusable draft rejects the whole response. The caller then
  writes the campaign, its drafts, and the credit charges in a single
  transaction, so there is no partial calendar to clean up.
"""

from __future__ import annotations

import json

try:  # package import (tests, `python -m app.server`)
    from .config import CHANNEL_CHARACTER_LIMITS, MAX_POST_LENGTH, now
    from .database import Database, Record, insert_id
    from .errors import ProviderError
except ImportError:  # script import (`python /app/app/server.py`)
    from config import CHANNEL_CHARACTER_LIMITS, MAX_POST_LENGTH, now
    from database import Database, Record, insert_id
    from errors import ProviderError


MAX_CAMPAIGN_NAME_LENGTH = 80
MAX_BRIEF_LENGTH = 2_000
MAX_CADENCE_LENGTH = 200
MAX_PLANNED_POSTS = 14
MAX_SUGGESTED_LENGTH = 40


def planning_prompt(brief: str, cadence: str, channels: list[str], count: int) -> str:
    return (
        f"Plan {count} social media posts.\n"
        f"Brief: {brief.strip()}\n"
        f"Cadence: {cadence.strip() or 'spread evenly'}\n"
        f"Allowed platforms: {', '.join(channels)}\n\n"
        "Return only a JSON object of the form "
        '{"posts": [{"body": "...", "channel": "...", "suggested_for": "YYYY-MM-DDTHH:MM"}]}. '
        f"Return exactly {count} posts. Every channel must be one of the allowed platforms. "
        "suggested_for is a proposed local time for the author to review; it is optional. "
        "Do not add commentary, explanation, or a markdown fence."
    )


def parse_campaign_plan(raw: str, channels: list[str], count: int) -> list[dict[str, str]]:
    """Parse the model's plan strictly. Any unusable entry rejects the whole plan."""
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text[text.index("\n") + 1:] if "\n" in text else text
    try:
        document = json.loads(text)
    except json.JSONDecodeError as error:
        raise ProviderError("The AI provider did not return a usable content plan.") from error
    entries = document.get("posts") if isinstance(document, dict) else document
    if not isinstance(entries, list) or not entries:
        raise ProviderError("The AI provider did not return any planned posts.")
    if len(entries) > count:
        entries = entries[:count]
    allowed = set(channels)
    drafts: list[dict[str, str]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise ProviderError("The AI provider returned a malformed planned post.")
        body = entry.get("body")
        channel = entry.get("channel")
        if not isinstance(body, str) or not body.strip() or len(body) > MAX_POST_LENGTH:
            raise ProviderError("The AI provider returned a planned post with no usable text.")
        if not isinstance(channel, str) or channel not in allowed:
            raise ProviderError("The AI provider planned a post for a platform that was not requested.")
        if len(body) > CHANNEL_CHARACTER_LIMITS[channel]:
            raise ProviderError(f"The AI provider planned a {channel} post longer than {CHANNEL_CHARACTER_LIMITS[channel]} characters.")
        suggested = entry.get("suggested_for")
        drafts.append({
            "body": body.strip(),
            "channel": channel,
            "suggested_for": suggested.strip()[:MAX_SUGGESTED_LENGTH] if isinstance(suggested, str) and suggested.strip() else "",
        })
    return drafts


def create_campaign(connection: Database, workspace_id: int, name: str, brief: str, created_by: int) -> int:
    return insert_id(
        connection,
        "INSERT INTO campaigns (workspace_id, name, brief, created_by, created_at) VALUES (?, ?, ?, ?, ?)",
        (workspace_id, name, brief, created_by, now()),
    )


def create_campaign_drafts(connection: Database, workspace_id: int, user_id: int, campaign_id: int, drafts: list[dict[str, str]]) -> list[int]:
    """Insert the planned posts as drafts. Never scheduled, never published."""
    identifiers = []
    for draft in drafts:
        identifiers.append(insert_id(
            connection,
            "INSERT INTO posts (user_id, workspace_id, campaign_id, body, channel, state, suggested_for, created_at)"
            " VALUES (?, ?, ?, ?, ?, 'draft', ?, ?)",
            (user_id, workspace_id, campaign_id, draft["body"], draft["channel"], draft["suggested_for"] or None, now()),
        ))
    return identifiers


def workspace_campaigns(connection: Database, workspace_id: int) -> list[Record]:
    return connection.execute(
        "SELECT campaigns.id, campaigns.name, campaigns.brief, campaigns.created_at,"
        " (SELECT COUNT(*) FROM posts WHERE posts.campaign_id = campaigns.id) AS posts"
        " FROM campaigns WHERE campaigns.workspace_id = ? ORDER BY campaigns.id DESC",
        (workspace_id,),
    ).fetchall()
