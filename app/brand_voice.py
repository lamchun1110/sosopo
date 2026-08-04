"""Per-workspace brand voice, injected as context into AI prompts.

A profile is plain JSON in ``workspace_settings`` — no schema migration, and a
workspace without one behaves exactly as it did before this existed.

Two safety properties matter here, because this is user-authored text that
ends up in a system prompt:

- **Only known fields survive.** :func:`validated_profile` rebuilds the
  profile from a fixed field list, so a caller cannot smuggle in an extra key
  hoping it will be rendered verbatim into the prompt.
- **Everything is bounded.** Per-field and total size caps keep a profile from
  crowding out the actual instruction, or from inflating token cost.

The profile is context, not instructions: :func:`brand_voice_prompt` renders
it as labelled description, and the surrounding system prompt keeps telling
the model what its job is.
"""

from __future__ import annotations

import json

try:  # package import (tests, `python -m app.server`)
    from .database import Database
    from .errors import ProviderError
    from .workspaces import save_workspace_setting, workspace_setting
except ImportError:  # script import (`python /app/app/server.py`)
    from database import Database
    from errors import ProviderError
    from workspaces import save_workspace_setting, workspace_setting


SETTING_NAME = "brand_voice"
MAX_PROFILE_BYTES = 4 * 1024
MAX_TEXT_LENGTH = 600
MAX_LIST_ITEMS = 20
MAX_ITEM_LENGTH = 280
TEXT_FIELDS = ("tone", "audience", "visual_style")
LIST_FIELDS = ("do_phrases", "dont_phrases", "sample_posts", "hashtags")
FIELD_LABELS = {
    "tone": "Tone",
    "audience": "Audience",
    "visual_style": "Visual style",
    "do_phrases": "Prefer these words and phrases",
    "dont_phrases": "Never use these words and phrases",
    "sample_posts": "Example posts in this voice",
    "hashtags": "Default hashtags",
}


def validated_profile(raw: object) -> dict[str, object]:
    """Return a profile containing only known, bounded fields."""
    if not isinstance(raw, dict):
        raise ProviderError("Send the brand voice profile as an object.", retryable=False)
    profile: dict[str, object] = {}
    for field in TEXT_FIELDS:
        value = raw.get(field)
        if value in (None, ""):
            continue
        if not isinstance(value, str):
            raise ProviderError(f"Brand voice {field} must be text.", retryable=False)
        if len(value) > MAX_TEXT_LENGTH:
            raise ProviderError(f"Brand voice {field} must be {MAX_TEXT_LENGTH} characters or fewer.", retryable=False)
        profile[field] = value.strip()
    for field in LIST_FIELDS:
        value = raw.get(field)
        if value in (None, [], ""):
            continue
        if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
            raise ProviderError(f"Brand voice {field} must be a list of short text entries.", retryable=False)
        if len(value) > MAX_LIST_ITEMS or any(len(item) > MAX_ITEM_LENGTH for item in value):
            raise ProviderError(f"Brand voice {field} allows up to {MAX_LIST_ITEMS} entries of {MAX_ITEM_LENGTH} characters.", retryable=False)
        entries = [item.strip() for item in value if item.strip()]
        if entries:
            profile[field] = entries
    if len(json.dumps(profile).encode()) > MAX_PROFILE_BYTES:
        raise ProviderError(f"The brand voice profile must be {MAX_PROFILE_BYTES // 1024} KB or smaller.", retryable=False)
    return profile


def load_brand_voice(connection: Database, workspace_id: int | None) -> dict[str, object] | None:
    if workspace_id is None:
        return None
    raw = workspace_setting(connection, workspace_id, SETTING_NAME)
    if not raw:
        return None
    try:
        stored = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return stored if isinstance(stored, dict) and stored else None


def save_brand_voice(connection: Database, workspace_id: int, profile: dict[str, object] | None) -> None:
    save_workspace_setting(connection, workspace_id, SETTING_NAME, json.dumps(profile) if profile else None)


def brand_voice_prompt(profile: dict[str, object] | None) -> str:
    """Render a profile as labelled context for a system prompt."""
    if not profile:
        return ""
    lines = ["Brand voice for this workspace. Follow it unless the brief says otherwise."]
    for field in (*TEXT_FIELDS, *LIST_FIELDS):
        value = profile.get(field)
        if not value:
            continue
        rendered = value if isinstance(value, str) else "; ".join(str(item) for item in value)
        lines.append(f"{FIELD_LABELS[field]}: {rendered}")
    return "\n".join(lines)


def brand_voice_style(profile: dict[str, object] | None) -> str:
    """The visual style hint used for generated media."""
    value = (profile or {}).get("visual_style")
    return value.strip() if isinstance(value, str) else ""
