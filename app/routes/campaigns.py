"""AI content calendars: plan a brief into reviewable drafts."""

from __future__ import annotations

from http import HTTPStatus
from typing import Any

try:  # package import (tests, `python -m app.server`)
    from ..ai_providers import generate_campaign_plan
    from ..audit import audit
    from ..brand_voice import load_brand_voice
    from ..campaigns import (
        MAX_BRIEF_LENGTH,
        MAX_CADENCE_LENGTH,
        MAX_CAMPAIGN_NAME_LENGTH,
        MAX_PLANNED_POSTS,
        create_campaign,
        create_campaign_drafts,
        parse_campaign_plan,
        planning_prompt,
        workspace_campaigns,
    )
    from ..config import CHANNELS
    from ..credits import charge_ai_credit
    from ..database import Record, db
    from ..errors import ProviderError
    from ..plans import enforce_monthly_quota, record_usage
except ImportError:  # script import (`python /app/app/server.py`)
    from ai_providers import generate_campaign_plan
    from audit import audit
    from brand_voice import load_brand_voice
    from campaigns import (
        MAX_BRIEF_LENGTH,
        MAX_CADENCE_LENGTH,
        MAX_CAMPAIGN_NAME_LENGTH,
        MAX_PLANNED_POSTS,
        create_campaign,
        create_campaign_drafts,
        parse_campaign_plan,
        planning_prompt,
        workspace_campaigns,
    )
    from config import CHANNELS
    from credits import charge_ai_credit
    from database import Record, db
    from errors import ProviderError
    from plans import enforce_monthly_quota, record_usage


class CampaignRoutes:
    """Plan with AI, then review. Mixed into ``Handler``.

    The ordering here is the whole design: validate, then call the provider,
    then parse strictly, and only then open one transaction that charges
    credits and writes the campaign and its drafts together. Nothing is
    charged for a plan that could not be parsed, and no partial calendar can
    survive a failure part-way through.
    """

    def get_campaigns(self, path: str) -> bool:
        """Handle one campaign GET; True when answered."""
        if path == "/api/campaigns":
            session = self._session()
            workspace_id = self._require_workspace(session)
            if workspace_id is None:
                return True
            with db() as connection:
                campaigns = [
                    {"id": item["id"], "name": item["name"], "brief": item["brief"],
                     "posts": item["posts"], "created_at": item["created_at"]}
                    for item in workspace_campaigns(connection, workspace_id)
                ]
            self._json({"campaigns": campaigns}); return True
        return False

    def post_campaigns(self, path: str, payload: dict[str, Any], session: Record) -> bool:
        """Handle one campaign POST; True when answered."""
        if path == "/api/campaigns":
            workspace_id = self._require_workspace(session, "editor")
            if workspace_id is None:
                return True
            name = str(payload.get("name", "")).strip()
            brief = str(payload.get("brief", "")).strip()
            cadence = str(payload.get("cadence", "")).strip()
            provider = str(payload.get("provider", "")).strip()
            model = str(payload.get("model", "")).strip()
            channels = payload.get("channels", [])
            try:
                count = int(payload.get("count", 0))
            except (TypeError, ValueError):
                count = 0
            if not name or len(name) > MAX_CAMPAIGN_NAME_LENGTH:
                self._json({"error": f"Use a campaign name of 1 to {MAX_CAMPAIGN_NAME_LENGTH} characters."}, HTTPStatus.BAD_REQUEST); return True
            if not brief or len(brief) > MAX_BRIEF_LENGTH:
                self._json({"error": f"Use a brief of 1 to {MAX_BRIEF_LENGTH} characters."}, HTTPStatus.BAD_REQUEST); return True
            if len(cadence) > MAX_CADENCE_LENGTH:
                self._json({"error": f"Use a cadence of {MAX_CADENCE_LENGTH} characters or fewer."}, HTTPStatus.BAD_REQUEST); return True
            if not isinstance(channels, list) or not channels or any(str(channel) not in CHANNELS for channel in channels):
                self._json({"error": "Choose at least one supported platform to plan for."}, HTTPStatus.BAD_REQUEST); return True
            if not 1 <= count <= MAX_PLANNED_POSTS:
                self._json({"error": f"Plan between 1 and {MAX_PLANNED_POSTS} posts at a time."}, HTTPStatus.BAD_REQUEST); return True
            selected = [str(channel) for channel in channels]
            with db() as connection:
                enforce_monthly_quota(connection, workspace_id, "ai_generations", "ai_generations_per_month", "AI text generations")
                brand_voice = load_brand_voice(connection, workspace_id) if payload.get("apply_brand_voice", True) is not False else None
            try:
                raw = generate_campaign_plan(provider, model, planning_prompt(brief, cadence, selected, count), workspace_id, brand_voice)
                drafts = parse_campaign_plan(raw, selected, count)
            except ProviderError as error:
                self._json({"error": str(error)}, HTTPStatus.BAD_GATEWAY); return True
            try:
                # One transaction: a credit per draft, the campaign, and the
                # drafts. Any failure rolls the whole calendar back.
                with db() as connection:
                    for _ in drafts:
                        charge_ai_credit(connection, workspace_id, "ai_campaign_draft", session["user_id"])
                    record_usage(connection, workspace_id, "ai_generations", len(drafts))
                    campaign_id = create_campaign(connection, workspace_id, name, brief, session["user_id"])
                    create_campaign_drafts(connection, workspace_id, session["user_id"], campaign_id, drafts)
            except ProviderError as error:
                self._json({"error": str(error)}, HTTPStatus.BAD_REQUEST); return True
            audit(session["user_id"], "campaign.planned", "campaign", campaign_id, f"Planned {len(drafts)} drafts for {name}", self._source_ip(), workspace_id=workspace_id)
            self._json({"id": campaign_id, "name": name, "created": len(drafts)}, HTTPStatus.CREATED); return True
        return False
