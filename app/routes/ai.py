"""AI provider configuration at instance and workspace scope, and generation."""


from __future__ import annotations


import json
from http import HTTPStatus
from typing import Any
from urllib.parse import unquote

try:  # package import (tests, `python -m app.server`)
    from ..ai_providers import AI_PROVIDERS, AI_PROVIDER_MODELS, ai_model_catalog, ai_provider_models, ai_provider_settings, available_ai_providers, generate_post_copy, remove_ai_provider_settings, save_ai_provider_settings, stored_ai_provider_settings
    from ..audit import audit
    from ..config import CHANNELS
    from ..database import Record, db
    from ..errors import ProviderError
    from ..brand_voice import load_brand_voice
    from ..credits import charge_ai_credit
    from ..plans import enforce_monthly_quota, record_usage
except ImportError:  # script import (`python /app/app/server.py`)
    from ai_providers import AI_PROVIDERS, AI_PROVIDER_MODELS, ai_model_catalog, ai_provider_models, ai_provider_settings, available_ai_providers, generate_post_copy, remove_ai_provider_settings, save_ai_provider_settings, stored_ai_provider_settings
    from audit import audit
    from config import CHANNELS
    from database import Record, db
    from errors import ProviderError
    from brand_voice import load_brand_voice
    from credits import charge_ai_credit
    from plans import enforce_monthly_quota, record_usage


class AiRoutes:
    """AI provider configuration at instance and workspace scope, and generation.

    Mixed into ``Handler``; every method returns True once it has answered.
    """



    def get_ai(self, path: str) -> bool:
        """Handle one AI GET; True when answered."""
        if path == "/api/ai/providers":
            session = self._session()
            self._json({"providers": available_ai_providers(session.get("workspace_id"))})
            return True
        if path == "/api/workspaces/ai-providers":
            session = self._session()
            workspace_id = self._require_workspace(session, "admin")
            if workspace_id is None:
                return True
            providers = []
            for name in AI_PROVIDERS:
                stored = stored_ai_provider_settings(name, workspace_id)
                catalog = ai_model_catalog(stored)
                instance_available = True
                try:
                    ai_provider_settings(name)
                except ProviderError:
                    instance_available = False
                providers.append({"name": name, "model": stored.get("model", AI_PROVIDER_MODELS[name][0]), "models": catalog or AI_PROVIDER_MODELS[name], "models_count": len(catalog), "models_checked_at": stored.get("models_checked_at"), "has_api_key": bool(stored.get("api_key")), "instance_fallback": instance_available})
            self._json({"providers": providers}); return True
        if path.startswith("/api/workspaces/ai-providers/") and path.endswith("/models"):
            session = self._session()
            workspace_id = self._require_workspace(session, "admin")
            if workspace_id is None:
                return True
            provider = unquote(path.split("/")[4])
            if provider not in AI_PROVIDERS:
                self._json({"error": "Choose a supported AI provider."}, HTTPStatus.BAD_REQUEST); return True
            if provider != "OpenRouter" and not stored_ai_provider_settings(provider, workspace_id).get("api_key"):
                self._json({"error": "Save this workspace's API key for the provider before refreshing its models."}, HTTPStatus.BAD_REQUEST); return True
            try:
                self._json({"models": ai_provider_models(provider, workspace_id)})
            except ProviderError as error:
                self._json({"error": str(error)}, HTTPStatus.BAD_GATEWAY)
            return True
        return False

    def post_ai(self, path: str, payload: dict[str, Any], session: Record) -> bool:
        """Handle one AI POST; True when answered."""
        if path == "/api/admin/ai-providers":
            if session["role"] != "admin":
                self._json({"error": "Administrator access required."}, HTTPStatus.FORBIDDEN); return True
            provider = str(payload.get("provider", "")).strip()
            definition = AI_PROVIDERS.get(provider)
            if definition is None:
                self._json({"error": "Choose a supported AI provider."}, HTTPStatus.BAD_REQUEST); return True
            current = stored_ai_provider_settings(provider)
            model = str(payload.get("model", "")).strip() or current.get("model") or AI_PROVIDER_MODELS[provider][0]
            api_key = str(payload.get("api_key", "")).strip()
            if len(model) > 200:
                self._json({"error": "Choose a model."}, HTTPStatus.BAD_REQUEST); return True
            if not api_key and not current.get("api_key"):
                self._json({"error": "Provide an API key for this provider."}, HTTPStatus.BAD_REQUEST); return True
            catalog = ai_model_catalog(current) or AI_PROVIDER_MODELS[provider]
            # The browser normally supplies a model from this catalog. Do
            # not make saving a credential depend on a prior live catalog
            # refresh, though: provider catalogs can be temporarily
            # unavailable, and the selected default may be newly released.
            if model not in catalog:
                catalog = [model, *catalog]
            stored = {"api_key": api_key or current["api_key"], "base_url": definition.base_url, "model": model, "models": json.dumps(catalog)}
            if current.get("models_checked_at"):
                stored["models_checked_at"] = current["models_checked_at"]
            save_ai_provider_settings(provider, stored)
            audit(session["user_id"], "ai_provider.saved", "instance", provider, f"Configured {provider} AI provider", self._source_ip())
            self._json({"name": provider, "model": model, "has_api_key": True}); return True
        if path.startswith("/api/admin/ai-providers/") and path.endswith("/remove"):
            if session["role"] != "admin":
                self._json({"error": "Administrator access required."}, HTTPStatus.FORBIDDEN); return True
            provider = unquote(path.split("/")[4])
            if provider not in AI_PROVIDERS:
                self._json({"error": "Choose a supported AI provider."}, HTTPStatus.BAD_REQUEST); return True
            removed = remove_ai_provider_settings(provider)
            audit(session["user_id"], "ai_provider.removed", "instance", provider, f"Removed {provider} UI-saved API key", self._source_ip())
            self._json({"status": "removed" if removed else "not configured", "name": provider}); return True
        if path == "/api/ai/generate":
            workspace_id = self._require_workspace(session, "editor")
            if workspace_id is None:
                return True
            provider = str(payload.get("provider", ""))
            model = str(payload.get("model", ""))
            instruction = str(payload.get("instruction", ""))
            draft = str(payload.get("draft", ""))
            channels = payload.get("channels", [])
            if not isinstance(channels, list) or any(str(channel) not in CHANNELS for channel in channels):
                self._json({"error": "Choose valid post platforms for AI generation."}, HTTPStatus.BAD_REQUEST); return True
            # The toggle defaults on, so a saved profile applies unless the
            # composer explicitly opts out for this one generation.
            apply_brand_voice = payload.get("apply_brand_voice", True) is not False
            with db() as connection:
                enforce_monthly_quota(connection, workspace_id, "ai_generations", "ai_generations_per_month", "AI text generations")
                charge_ai_credit(connection, workspace_id, "ai_generation", session["user_id"])
                brand_voice = load_brand_voice(connection, workspace_id) if apply_brand_voice else None
            try:
                copy = generate_post_copy(provider, model, instruction, draft, [str(channel) for channel in channels], workspace_id, brand_voice)
            except ProviderError as error:
                self._json({"error": str(error)}, HTTPStatus.BAD_GATEWAY); return True
            with db() as connection:
                record_usage(connection, workspace_id, "ai_generations")
            audit(session["user_id"], "post.ai_generated", "user", session["user_id"], f"Generated post copy with {provider}", self._source_ip(), workspace_id=workspace_id)
            self._json({"copy": copy}); return True
        if path == "/api/workspaces/ai-providers":
            workspace_id = self._require_workspace(session, "admin")
            if workspace_id is None:
                return True
            provider = str(payload.get("provider", "")).strip()
            if provider not in AI_PROVIDERS:
                self._json({"error": "Choose a supported AI provider."}, HTTPStatus.BAD_REQUEST); return True
            current = stored_ai_provider_settings(provider, workspace_id)
            model = str(payload.get("model", "")).strip() or current.get("model") or AI_PROVIDER_MODELS[provider][0]
            api_key = str(payload.get("api_key", "")).strip()
            if len(model) > 200:
                self._json({"error": "Choose a model."}, HTTPStatus.BAD_REQUEST); return True
            if not api_key and not current.get("api_key"):
                self._json({"error": "Provide this workspace's API key for the provider."}, HTTPStatus.BAD_REQUEST); return True
            catalog = ai_model_catalog(current) or AI_PROVIDER_MODELS[provider]
            if model not in catalog:
                catalog = [model, *catalog]
            stored = {"api_key": api_key or current["api_key"], "base_url": AI_PROVIDERS[provider].base_url, "model": model, "models": json.dumps(catalog)}
            if current.get("models_checked_at"):
                stored["models_checked_at"] = current["models_checked_at"]
            save_ai_provider_settings(provider, stored, workspace_id)
            audit(session["user_id"], "ai_provider.workspace_saved", "workspace", workspace_id, f"Configured workspace {provider} AI provider", self._source_ip(), workspace_id=workspace_id)
            self._json({"name": provider, "model": model, "has_api_key": True}); return True
        if path.startswith("/api/workspaces/ai-providers/") and path.endswith("/remove"):
            workspace_id = self._require_workspace(session, "admin")
            if workspace_id is None:
                return True
            provider = unquote(path.split("/")[4])
            if provider not in AI_PROVIDERS:
                self._json({"error": "Choose a supported AI provider."}, HTTPStatus.BAD_REQUEST); return True
            removed = remove_ai_provider_settings(provider, workspace_id)
            audit(session["user_id"], "ai_provider.workspace_removed", "workspace", workspace_id, f"Removed workspace {provider} API key", self._source_ip(), workspace_id=workspace_id)
            self._json({"status": "removed" if removed else "not configured", "name": provider}); return True
        return False

