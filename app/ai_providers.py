"""AI provider registry, credential storage, model catalogs, and text generation."""

from __future__ import annotations

import json
import time

try:  # package import (tests, `python -m app.server`)
    from . import http_client
    from .config import config, now
    from .database import db
    from .errors import ProviderError
    from .security import decrypt_secrets, encrypt_secrets
    from .workspaces import save_workspace_setting
except ImportError:  # script import (`python /app/app/server.py`)
    import http_client
    from config import config, now
    from database import db
    from errors import ProviderError
    from security import decrypt_secrets, encrypt_secrets
    from workspaces import save_workspace_setting


AI_PROVIDERS = {
    "OpenAI": ("openai", "SOSOPO_AI_OPENAI", "https://api.openai.com/v1"),
    "OpenRouter": ("openrouter", "SOSOPO_AI_OPENROUTER", "https://openrouter.ai/api/v1"),
    "Kimi": ("kimi", "SOSOPO_AI_KIMI", "https://api.moonshot.ai/v1"),
    "MiniMax": ("minimax", "SOSOPO_AI_MINIMAX", "https://api.minimax.io/v1"),
    "Z.AI GLM": ("zai", "SOSOPO_AI_ZAI", "https://api.z.ai/api/paas/v4"),
}


# Provider-owned defaults keep endpoint details out of the administrator UI.
# A refreshed provider catalog supersedes these choices when available.
AI_PROVIDER_MODELS = {
    "OpenAI": ["gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna", "gpt-5.5", "gpt-5.2"],
    "OpenRouter": ["openai/gpt-5.6-sol", "openai/gpt-5.5", "anthropic/claude-sonnet-4.6"],
    "Kimi": ["kimi-k3", "kimi-k2.7-code", "kimi-k2.7-code-highspeed", "kimi-k2.6"],
    "MiniMax": ["MiniMax-M2.7", "MiniMax-M2.7-highspeed", "MiniMax-M2.5", "MiniMax-M2.5-highspeed", "M2-her"],
    "Z.AI GLM": ["glm-5.2", "glm-5.1", "glm-5"],
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
    setting_name = f"ai_provider_{definition[0]}"
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
    setting_name = f"ai_provider_{definition[0]}"
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
    setting_name = f"ai_provider_{definition[0]}"
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
    """Return only a configured, OpenAI-compatible text-generation provider."""
    definition = AI_PROVIDERS.get(provider)
    if definition is None:
        raise ProviderError("Choose a supported AI provider.", retryable=False)
    _, prefix, default_base = definition
    stored, source = effective_ai_provider_stored(provider, workspace_id)
    api_key = stored.get("api_key") or ("" if source == "workspace" else config(f"{prefix}_API_KEY"))
    base_url = stored.get("base_url") or config(f"{prefix}_BASE_URL") or default_base
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
    stored = stored_ai_provider_settings(provider, workspace_id)
    # OpenRouter publishes its complete catalog without authentication, so make
    # it available in the selector before an administrator has saved a key.
    if provider == "OpenRouter":
        definition = AI_PROVIDERS[provider]
        settings = {"name": provider, "api_key": stored.get("api_key") or config(f"{definition[1]}_API_KEY"), "base_url": definition[2]}
    else:
        settings = ai_provider_settings(provider, workspace_id)
    # MiniMax documents this exact endpoint for Token Plan keys.  In
    # particular, do not append cache-busting query parameters: some MiniMax
    # API gateways reject an otherwise valid signed/bearer request when the
    # request URI differs from the documented endpoint.
    model_list_url = f"{settings['base_url']}/models"
    if provider != "MiniMax":
        # A unique query string bypasses intermediary caches for providers
        # which accept it. Sosopo itself never caches this response.
        model_list_url = f"{model_list_url}?refresh={int(time.time())}"
    headers = {"Authorization": f"Bearer {settings['api_key']}"} if settings.get("api_key") else None
    result = http_client.request_get_json(model_list_url, headers)
    entries = result.get("data") or result.get("models") or []
    if not isinstance(entries, list):
        raise ProviderError("The AI provider returned an invalid model list.")
    models = []
    for entry in entries:
        identifier = (entry.get("id") or entry.get("model") or entry.get("name")) if isinstance(entry, dict) else entry if isinstance(entry, str) else None
        if isinstance(identifier, str) and identifier and len(identifier) <= 200:
            models.append(identifier)
    if not models:
        raise ProviderError("The AI provider did not return any selectable models.")
    models = sorted(set(models), key=str.casefold)[:1_000]
    # Keep a known-good catalog locally. The composer uses this catalog rather than
    # contacting the provider on every page load, and rejects unknown model IDs.
    stored.update({"models": json.dumps(models), "models_checked_at": now()})
    save_ai_provider_settings(provider, stored, workspace_id)
    return models


def generate_post_copy(provider: str, model: str, instruction: str, draft: str, channels: list[str], workspace_id: int | None = None) -> str:
    settings = ai_provider_settings(provider, workspace_id)
    selected_model = model.strip() or settings["model"]
    stored, _ = effective_ai_provider_stored(provider, workspace_id)
    catalog = ai_model_catalog(stored)
    if catalog and selected_model not in catalog:
        raise ProviderError("Choose a model from the provider's refreshed model catalog.", retryable=False)
    if len(selected_model) > 200 or len(instruction) > 2_000 or len(draft) > 5_000:
        raise ProviderError("AI request is too long.", retryable=False)
    prompt = f"Write one ready-to-publish social media post. Platforms: {', '.join(channels) or 'general social media'}. Brief: {instruction.strip() or 'Improve the draft below.'}\nDraft to improve (may be empty):\n{draft.strip()}"
    messages = [{"role": "system", "content": "You are Sosopo's concise social-media copywriter. Return only the finished post copy; do not add a title, explanation, markdown fence, or quotation marks."}, {"role": "user", "content": prompt}]
    endpoint = f"{settings['base_url']}/text/chatcompletion_v2" if provider == "MiniMax" else f"{settings['base_url']}/chat/completions"
    result = http_client.request_json(endpoint, {"model": selected_model, "messages": messages, "temperature": 0.7, "max_tokens": 700}, {"Authorization": f"Bearer {settings['api_key']}"})
    choices = result.get("choices", [])
    content = choices[0].get("message", {}).get("content", "") if isinstance(choices, list) and choices and isinstance(choices[0], dict) else ""
    if not isinstance(content, str) or not content.strip():
        raise ProviderError("The AI provider did not return post copy.")
    return content.strip()
