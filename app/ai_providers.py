"""AI provider registry, credential storage, model catalogs, and text generation."""

from __future__ import annotations

import json
from typing import NamedTuple

try:  # package import (tests, `python -m app.server`)
    from . import http_client
    from .brand_voice import brand_voice_prompt
    from .ai_adapters import ChatAdapter, ClaudeAdapter, MiniMaxAdapter, OpenRouterAdapter
    from .config import config, now
    from .database import db
    from .errors import ProviderError
    from .security import decrypt_secrets, encrypt_secrets
    from .workspaces import save_workspace_setting
except ImportError:  # script import (`python /app/app/server.py`)
    import http_client
    from brand_voice import brand_voice_prompt
    from ai_adapters import ChatAdapter, ClaudeAdapter, MiniMaxAdapter, OpenRouterAdapter
    from config import config, now
    from database import db
    from errors import ProviderError
    from security import decrypt_secrets, encrypt_secrets
    from workspaces import save_workspace_setting


class AiProvider(NamedTuple):
    """One text-generation provider: where it lives and how it is spoken to.

    ``adapter`` defaults to the OpenAI-compatible shape, so adding a provider
    that speaks it needs one line here and nothing else.
    """

    slug: str
    environment_prefix: str
    base_url: str
    adapter: type[ChatAdapter] = ChatAdapter


AI_PROVIDERS = {
    "OpenAI": AiProvider("openai", "SOSOPO_AI_OPENAI", "https://api.openai.com/v1"),
    "OpenRouter": AiProvider("openrouter", "SOSOPO_AI_OPENROUTER", "https://openrouter.ai/api/v1", OpenRouterAdapter),
    "Kimi": AiProvider("kimi", "SOSOPO_AI_KIMI", "https://api.moonshot.ai/v1"),
    "MiniMax": AiProvider("minimax", "SOSOPO_AI_MINIMAX", "https://api.minimax.io/v1", MiniMaxAdapter),
    "Z.AI GLM": AiProvider("zai", "SOSOPO_AI_ZAI", "https://api.z.ai/api/paas/v4"),
    "Claude": AiProvider("claude", "SOSOPO_AI_CLAUDE", "https://api.anthropic.com", ClaudeAdapter),
    # Gemini, Grok, and DeepSeek all serve an OpenAI-compatible surface, so the
    # default adapter covers them and each costs exactly one line here.
    "Gemini": AiProvider("gemini", "SOSOPO_AI_GEMINI", "https://generativelanguage.googleapis.com/v1beta/openai"),
    "Grok": AiProvider("grok", "SOSOPO_AI_GROK", "https://api.x.ai/v1"),
    "DeepSeek": AiProvider("deepseek", "SOSOPO_AI_DEEPSEEK", "https://api.deepseek.com"),
}


# Provider-owned defaults keep endpoint details out of the administrator UI.
# A refreshed provider catalog supersedes these choices when available.
AI_PROVIDER_MODELS = {
    "OpenAI": ["gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna", "gpt-5.5", "gpt-5.2"],
    "OpenRouter": ["openai/gpt-5.6-sol", "openai/gpt-5.5", "anthropic/claude-sonnet-4.6"],
    "Kimi": ["kimi-k3", "kimi-k2.7-code", "kimi-k2.7-code-highspeed", "kimi-k2.6"],
    "MiniMax": ["MiniMax-M2.7", "MiniMax-M2.7-highspeed", "MiniMax-M2.5", "MiniMax-M2.5-highspeed", "M2-her"],
    "Z.AI GLM": ["glm-5.2", "glm-5.1", "glm-5"],
    "Claude": ["claude-opus-5", "claude-sonnet-5", "claude-fable-5", "claude-haiku-4-5-20251001"],
    "Gemini": ["gemini-3-pro", "gemini-3-flash", "gemini-2.5-pro"],
    "Grok": ["grok-4.1", "grok-4", "grok-3"],
    "DeepSeek": ["deepseek-chat", "deepseek-reasoner"],
}


# Media-capable models per provider. Only OpenAI-compatible media APIs are
# called; providers absent from a map cannot run that media kind.
AI_PROVIDER_IMAGE_MODELS = {
    "OpenAI": ["gpt-image-1.5", "gpt-image-1"],
    "OpenRouter": ["openai/gpt-image-1.5", "google/gemini-2.5-flash-image"],
    "Z.AI GLM": ["cogview-4.5"],
}


AI_PROVIDER_VIDEO_MODELS = {
    "OpenAI": ["sora-2.2", "sora-2"],
}


def stored_ai_provider_settings(provider: str, workspace_id: int | None = None) -> dict:
    definition = AI_PROVIDERS.get(provider)
    if definition is None:
        raise ProviderError("Choose a supported AI provider.", retryable=False)
    setting_name = f"ai_provider_{definition.slug}"
    with db() as connection:
        if workspace_id is None:
            row = connection.execute("SELECT value FROM instance_settings WHERE name = ?", (setting_name,)).fetchone()
        else:
            row = connection.execute("SELECT value FROM workspace_settings WHERE workspace_id = ? AND name = ?", (workspace_id, setting_name)).fetchone()
    return decrypt_secrets(row["value"]) if row else {}


def effective_ai_provider_stored(provider: str, workspace_id: int | None) -> tuple[dict, str]:
    """Prefer a workspace's own provider credential over the instance-wide one.

    A workspace configuration only takes effect once it holds its own API key;
    a cached model catalog alone must not shadow the instance credential.
    """
    if workspace_id is not None:
        workspace_stored = stored_ai_provider_settings(provider, workspace_id)
        if workspace_stored.get("api_key"):
            return workspace_stored, "workspace"
    return stored_ai_provider_settings(provider), "instance"


def save_ai_provider_settings(provider: str, settings: dict, workspace_id: int | None = None) -> None:
    """Save a provider configuration and its locally cached, reviewed model catalog."""
    definition = AI_PROVIDERS[provider]
    setting_name = f"ai_provider_{definition.slug}"
    with db() as connection:
        if workspace_id is None:
            exists = connection.execute("SELECT 1 FROM instance_settings WHERE name = ?", (setting_name,)).fetchone()
            if exists:
                connection.execute("UPDATE instance_settings SET value = ? WHERE name = ?", (encrypt_secrets(settings), setting_name))
            else:
                connection.execute("INSERT INTO instance_settings (name, value) VALUES (?, ?)", (setting_name, encrypt_secrets(settings)))
        else:
            save_workspace_setting(connection, workspace_id, setting_name, encrypt_secrets(settings))


def remove_ai_provider_settings(provider: str, workspace_id: int | None = None) -> bool:
    """Remove the UI-saved credential and local catalog for one provider."""
    definition = AI_PROVIDERS.get(provider)
    if definition is None:
        raise ProviderError("Choose a supported AI provider.", retryable=False)
    setting_name = f"ai_provider_{definition.slug}"
    with db() as connection:
        if workspace_id is None:
            return connection.execute("DELETE FROM instance_settings WHERE name = ?", (setting_name,)).rowcount == 1
        return connection.execute("DELETE FROM workspace_settings WHERE workspace_id = ? AND name = ?", (workspace_id, setting_name)).rowcount == 1


def ai_model_catalog(stored: dict) -> list[str]:
    raw = stored.get("models", "[]")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return []
    return [model for model in raw if isinstance(model, str) and model] if isinstance(raw, list) else []


def ai_provider_settings(provider: str, workspace_id: int | None = None) -> dict[str, str]:
    """Return one text-generation provider only when it is fully configured."""
    definition = AI_PROVIDERS.get(provider)
    if definition is None:
        raise ProviderError("Choose a supported AI provider.", retryable=False)
    prefix = definition.environment_prefix
    stored, source = effective_ai_provider_stored(provider, workspace_id)
    api_key = stored.get("api_key") or ("" if source == "workspace" else config(f"{prefix}_API_KEY"))
    base_url = stored.get("base_url") or config(f"{prefix}_BASE_URL") or definition.base_url
    model = stored.get("model") or config(f"{prefix}_MODEL") or AI_PROVIDER_MODELS[provider][0]
    if not api_key or not base_url.startswith("https://") or not model:
        raise ProviderError(f"{provider} AI is not configured by this Sosopo administrator.", retryable=False)
    return {"name": provider, "api_key": api_key, "base_url": base_url.rstrip("/"), "model": model, "source": source}


def available_ai_providers(workspace_id: int | None = None) -> list[dict]:
    providers: list[dict] = []
    for name in AI_PROVIDERS:
        try:
            settings = ai_provider_settings(name, workspace_id)
            stored, _ = effective_ai_provider_stored(name, workspace_id)
            models = ai_model_catalog(stored) or AI_PROVIDER_MODELS[name]
            providers.append({"name": settings["name"], "model": settings["model"], "models": models, "source": settings["source"]})
        except ProviderError:
            pass
    return providers


def ai_provider_models(provider: str, workspace_id: int | None = None) -> list[str]:
    """Refresh one scope's model catalog using the provider's discovery endpoint."""
    definition = AI_PROVIDERS.get(provider)
    if definition is None:
        raise ProviderError("Choose a supported AI provider.", retryable=False)
    stored = stored_ai_provider_settings(provider, workspace_id)
    if definition.adapter.catalog_needs_key:
        settings = ai_provider_settings(provider, workspace_id)
    else:
        settings = {"name": provider, "api_key": stored.get("api_key") or config(f"{definition.environment_prefix}_API_KEY"), "base_url": definition.base_url}
    model_list_url, headers = definition.adapter.build_model_list_request(settings)
    models = definition.adapter.parse_model_list(http_client.request_get_json(model_list_url, headers))
    # Keep a known-good catalog locally. The composer uses this catalog rather than
    # contacting the provider on every page load, and rejects unknown model IDs.
    stored.update({"models": json.dumps(models), "models_checked_at": now()})
    save_ai_provider_settings(provider, stored, workspace_id)
    return models


def generate_post_copy(provider: str, model: str, instruction: str, draft: str, channels: list[str], workspace_id: int | None = None, brand_voice: dict | None = None) -> str:
    settings = ai_provider_settings(provider, workspace_id)
    selected_model = model.strip() or settings["model"]
    stored, _ = effective_ai_provider_stored(provider, workspace_id)
    catalog = ai_model_catalog(stored)
    if catalog and selected_model not in catalog:
        raise ProviderError("Choose a model from the provider's refreshed model catalog.", retryable=False)
    if len(selected_model) > 200 or len(instruction) > 2_000 or len(draft) > 5_000:
        raise ProviderError("AI request is too long.", retryable=False)
    prompt = f"Write one ready-to-publish social media post. Platforms: {', '.join(channels) or 'general social media'}. Brief: {instruction.strip() or 'Improve the draft below.'}\nDraft to improve (may be empty):\n{draft.strip()}"
    instructions = "You are Sosopo's concise social-media copywriter. Return only the finished post copy; do not add a title, explanation, markdown fence, or quotation marks."
    voice = brand_voice_prompt(brand_voice)
    messages = [{"role": "system", "content": f"{instructions}\n\n{voice}" if voice else instructions}, {"role": "user", "content": prompt}]
    adapter = AI_PROVIDERS[provider].adapter
    endpoint, payload, headers = adapter.build_chat_request(settings, messages, {"model": selected_model, "temperature": 0.7, "max_tokens": 700})
    return adapter.parse_chat_response(http_client.request_json(endpoint, payload, headers))


def generate_campaign_plan(provider: str, model: str, prompt: str, workspace_id: int | None = None, brand_voice: dict | None = None) -> str:
    """Ask one provider for a content plan and return its raw text reply.

    Parsing lives in :mod:`app.campaigns`: this function only owns the request.
    """
    settings = ai_provider_settings(provider, workspace_id)
    selected_model = model.strip() or settings["model"]
    stored, _ = effective_ai_provider_stored(provider, workspace_id)
    catalog = ai_model_catalog(stored)
    if catalog and selected_model not in catalog:
        raise ProviderError("Choose a model from the provider's refreshed model catalog.", retryable=False)
    instructions = "You are Sosopo's social-media campaign planner. Return only valid JSON in the requested shape; never add commentary or a markdown fence."
    voice = brand_voice_prompt(brand_voice)
    messages = [{"role": "system", "content": f"{instructions}\n\n{voice}" if voice else instructions}, {"role": "user", "content": prompt}]
    adapter = AI_PROVIDERS[provider].adapter
    endpoint, payload, headers = adapter.build_chat_request(settings, messages, {"model": selected_model, "temperature": 0.6, "max_tokens": 4_000})
    return adapter.parse_chat_response(http_client.request_json(endpoint, payload, headers))


def generate_workspace_summary(provider: str, model: str, prompt: str, workspace_id: int | None = None) -> str:
    """Ask one provider to summarize workspace metrics. Read-only by construction."""
    settings = ai_provider_settings(provider, workspace_id)
    selected_model = model.strip() or settings["model"]
    messages = [
        {"role": "system", "content": "You are Sosopo's analytics assistant. Summarize the metrics you are given for a workspace administrator. Never invent numbers."},
        {"role": "user", "content": prompt},
    ]
    adapter = AI_PROVIDERS[provider].adapter
    endpoint, payload, headers = adapter.build_chat_request(settings, messages, {"model": selected_model, "temperature": 0.3, "max_tokens": 900})
    return adapter.parse_chat_response(http_client.request_json(endpoint, payload, headers))
