"""Per-provider request and response shapes for text generation.

Most providers speak the OpenAI-compatible ``chat/completions`` shape, so
``ChatAdapter`` is the default and a new provider usually needs no adapter at
all. Providers that differ subclass it and override only what differs; the
provider registry in :mod:`app.ai_providers` names the adapter, so call sites
never branch on a provider name.

Adapters are pure. They build requests and parse responses without performing
I/O, which keeps every provider's wire format directly unit-testable.
"""

from __future__ import annotations

import time
from typing import Any

try:  # package import (tests, `python -m app.server`)
    from .errors import ProviderError
except ImportError:  # script import (`python /app/app/server.py`)
    from errors import ProviderError


MAX_MODEL_ID_LENGTH = 200
MAX_CATALOG_MODELS = 1_000


class ChatAdapter:
    """The OpenAI-compatible chat shape: `POST {base}/chat/completions` with a bearer key."""

    # OpenRouter publishes its complete catalog without authentication, so its
    # models are selectable before an administrator has saved a key.
    catalog_needs_key = True
    # A unique query string bypasses intermediary caches for providers that
    # accept it. Sosopo itself never caches this response.
    catalog_is_cache_busted = True

    @classmethod
    def build_chat_request(cls, settings: dict[str, str], messages: list[dict[str, str]], options: dict[str, Any]) -> tuple[str, dict[str, Any], dict[str, str]]:
        payload = {"model": options["model"], "messages": messages, "temperature": options["temperature"], "max_tokens": options["max_tokens"]}
        return f"{settings['base_url']}/chat/completions", payload, {"Authorization": f"Bearer {settings['api_key']}"}

    @classmethod
    def parse_chat_response(cls, result: dict[str, Any]) -> str:
        choices = result.get("choices", [])
        content = choices[0].get("message", {}).get("content", "") if isinstance(choices, list) and choices and isinstance(choices[0], dict) else ""
        if not isinstance(content, str) or not content.strip():
            raise ProviderError("The AI provider did not return post copy.")
        return content.strip()

    @classmethod
    def build_model_list_request(cls, settings: dict[str, str]) -> tuple[str, dict[str, str] | None]:
        url = f"{settings['base_url']}/models"
        if cls.catalog_is_cache_busted:
            url = f"{url}?refresh={int(time.time())}"
        return url, ({"Authorization": f"Bearer {settings['api_key']}"} if settings.get("api_key") else None)

    @classmethod
    def parse_model_list(cls, result: dict[str, Any]) -> list[str]:
        entries = result.get("data") or result.get("models") or []
        if not isinstance(entries, list):
            raise ProviderError("The AI provider returned an invalid model list.")
        models = []
        for entry in entries:
            identifier = (entry.get("id") or entry.get("model") or entry.get("name")) if isinstance(entry, dict) else entry if isinstance(entry, str) else None
            if isinstance(identifier, str) and identifier and len(identifier) <= MAX_MODEL_ID_LENGTH:
                models.append(identifier)
        if not models:
            raise ProviderError("The AI provider did not return any selectable models.")
        return sorted(set(models), key=str.casefold)[:MAX_CATALOG_MODELS]


class MiniMaxAdapter(ChatAdapter):
    """MiniMax serves chat on its own path and documents an exact models URI.

    Do not append cache-busting query parameters: some MiniMax API gateways
    reject an otherwise valid signed or bearer request when the request URI
    differs from the documented endpoint.
    """

    catalog_is_cache_busted = False

    @classmethod
    def build_chat_request(cls, settings: dict[str, str], messages: list[dict[str, str]], options: dict[str, Any]) -> tuple[str, dict[str, Any], dict[str, str]]:
        _, payload, headers = super().build_chat_request(settings, messages, options)
        return f"{settings['base_url']}/text/chatcompletion_v2", payload, headers


class OpenRouterAdapter(ChatAdapter):
    """OpenRouter publishes its complete catalog without authentication."""

    catalog_needs_key = False


class ClaudeAdapter(ChatAdapter):
    """Anthropic's native Messages API, which differs from the OpenAI shape.

    Three differences matter, and this adapter exists for them:

    - Authentication is ``x-api-key`` plus a pinned ``anthropic-version``.
      Never a bearer token.
    - The system prompt is a top-level ``system`` field, not a message with
      ``role: system``.
    - ``max_tokens`` is required, and a reply is a list of content blocks
      rather than a single message string.
    """

    ANTHROPIC_VERSION = "2023-06-01"

    @classmethod
    def _headers(cls, settings: dict[str, str]) -> dict[str, str]:
        return {"x-api-key": settings["api_key"], "anthropic-version": cls.ANTHROPIC_VERSION}

    @classmethod
    def build_chat_request(cls, settings: dict[str, str], messages: list[dict[str, str]], options: dict[str, Any]) -> tuple[str, dict[str, Any], dict[str, str]]:
        system = "\n".join(str(message.get("content", "")) for message in messages if message.get("role") == "system")
        conversation = [message for message in messages if message.get("role") != "system"]
        payload: dict[str, Any] = {
            "model": options["model"],
            "max_tokens": options["max_tokens"],
            "messages": conversation,
            "temperature": options["temperature"],
        }
        if system:
            payload["system"] = system
        return f"{settings['base_url']}/v1/messages", payload, cls._headers(settings)

    @classmethod
    def parse_chat_response(cls, result: dict[str, Any]) -> str:
        blocks = result.get("content")
        text = "\n".join(
            str(block.get("text", ""))
            for block in (blocks if isinstance(blocks, list) else [])
            if isinstance(block, dict) and block.get("type") == "text"
        ).strip()
        if not text:
            raise ProviderError("The AI provider did not return post copy.")
        return text

    @classmethod
    def build_model_list_request(cls, settings: dict[str, str]) -> tuple[str, dict[str, str] | None]:
        return f"{settings['base_url']}/v1/models", cls._headers(settings)
