"""Claude, Gemini, Grok, and DeepSeek on top of the C1 adapter seam.

Gemini, Grok, and DeepSeek speak the OpenAI-compatible shape and need only a
registry line. Claude speaks the native Messages API and gets its own adapter,
which is the case the seam existed for.
"""

from __future__ import annotations

import unittest

import app.ai_adapters as adapters
import app.ai_providers as ai_providers

try:
    from tests.test_workspaces import WorkspaceHttpCase
except ImportError:
    from test_workspaces import WorkspaceHttpCase


CLAUDE = {"name": "Claude", "api_key": "sk-ant-test", "base_url": "https://api.anthropic.com", "model": "claude-sonnet-5"}
MESSAGES = [{"role": "system", "content": "be brief"}, {"role": "user", "content": "write a post"}]
OPTIONS = {"model": "claude-sonnet-5", "temperature": 0.7, "max_tokens": 700}


class ClaudeAdapterTest(unittest.TestCase):
    def test_chat_posts_to_the_messages_endpoint(self) -> None:
        url, _, _ = adapters.ClaudeAdapter.build_chat_request(CLAUDE, MESSAGES, OPTIONS)
        self.assertEqual(url, "https://api.anthropic.com/v1/messages")

    def test_authentication_uses_x_api_key_and_never_bearer(self) -> None:
        _, _, headers = adapters.ClaudeAdapter.build_chat_request(CLAUDE, MESSAGES, OPTIONS)
        self.assertEqual(headers["x-api-key"], "sk-ant-test")
        self.assertIn("anthropic-version", headers)
        self.assertNotIn("Authorization", headers)
        self.assertNotIn("Bearer", repr(headers))

    def test_the_system_prompt_is_hoisted_out_of_the_message_list(self) -> None:
        _, payload, _ = adapters.ClaudeAdapter.build_chat_request(CLAUDE, MESSAGES, OPTIONS)
        self.assertEqual(payload["system"], "be brief")
        self.assertEqual(payload["messages"], [{"role": "user", "content": "write a post"}])

    def test_max_tokens_is_always_sent(self) -> None:
        _, payload, _ = adapters.ClaudeAdapter.build_chat_request(CLAUDE, MESSAGES, {**OPTIONS, "max_tokens": 512})
        self.assertEqual(payload["max_tokens"], 512)
        self.assertEqual(payload["model"], "claude-sonnet-5")

    def test_content_blocks_are_joined_into_copy(self) -> None:
        result = {"content": [{"type": "text", "text": "first"}, {"type": "text", "text": "second"}]}
        self.assertEqual(adapters.ClaudeAdapter.parse_chat_response(result), "first\nsecond")

    def test_non_text_blocks_are_ignored(self) -> None:
        result = {"content": [{"type": "thinking", "thinking": "hmm"}, {"type": "text", "text": "the post"}]}
        self.assertEqual(adapters.ClaudeAdapter.parse_chat_response(result), "the post")

    def test_an_empty_response_is_refused(self) -> None:
        for result in ({}, {"content": []}, {"content": "nonsense"}, {"content": [{"type": "text", "text": "  "}]}):
            with self.subTest(result=result), self.assertRaisesRegex(adapters.ProviderError, "did not return post copy"):
                adapters.ClaudeAdapter.parse_chat_response(result)

    def test_the_model_catalog_uses_the_same_authentication(self) -> None:
        url, headers = adapters.ClaudeAdapter.build_model_list_request(CLAUDE)
        self.assertTrue(url.startswith("https://api.anthropic.com/v1/models"))
        self.assertEqual(headers["x-api-key"], "sk-ant-test")
        self.assertIn("anthropic-version", headers)

    def test_the_catalog_response_is_parsed_by_the_shared_rules(self) -> None:
        self.assertEqual(adapters.ClaudeAdapter.parse_model_list({"data": [{"id": "claude-sonnet-5"}]}), ["claude-sonnet-5"])


class RegistryTest(unittest.TestCase):
    def test_every_planned_provider_is_registered(self) -> None:
        for name in ("OpenAI", "OpenRouter", "Kimi", "MiniMax", "Z.AI GLM", "Claude", "Gemini", "Grok", "DeepSeek"):
            with self.subTest(provider=name):
                self.assertIn(name, ai_providers.AI_PROVIDERS)
                self.assertTrue(ai_providers.AI_PROVIDER_MODELS[name])

    def test_only_claude_needs_a_non_default_adapter(self) -> None:
        custom = {name: definition.adapter.__name__ for name, definition in ai_providers.AI_PROVIDERS.items()
                  if definition.adapter is not adapters.ChatAdapter}
        self.assertEqual(custom, {"Claude": "ClaudeAdapter", "MiniMax": "MiniMaxAdapter", "OpenRouter": "OpenRouterAdapter"})

    def test_the_openai_compatible_providers_keep_the_default_shape(self) -> None:
        for name, base in (("Gemini", "https://generativelanguage.googleapis.com/v1beta/openai"),
                           ("Grok", "https://api.x.ai/v1"), ("DeepSeek", "https://api.deepseek.com")):
            with self.subTest(provider=name):
                definition = ai_providers.AI_PROVIDERS[name]
                self.assertEqual(definition.base_url, base)
                self.assertIs(definition.adapter, adapters.ChatAdapter)
                url, headers = definition.adapter.build_chat_request(
                    {"base_url": base, "api_key": "k"}, MESSAGES, OPTIONS)[0::2]
                self.assertEqual(url, f"{base}/chat/completions")
                self.assertEqual(headers, {"Authorization": "Bearer k"})

    def test_every_provider_has_its_own_environment_prefix_and_slug(self) -> None:
        slugs = [definition.slug for definition in ai_providers.AI_PROVIDERS.values()]
        prefixes = [definition.environment_prefix for definition in ai_providers.AI_PROVIDERS.values()]
        self.assertEqual(len(set(slugs)), len(slugs))
        self.assertEqual(len(set(prefixes)), len(prefixes))
        self.assertEqual(ai_providers.AI_PROVIDERS["Claude"].environment_prefix, "SOSOPO_AI_CLAUDE")
        self.assertEqual(ai_providers.AI_PROVIDERS["Grok"].environment_prefix, "SOSOPO_AI_GROK")
        self.assertEqual(ai_providers.AI_PROVIDERS["DeepSeek"].environment_prefix, "SOSOPO_AI_DEEPSEEK")


class NewProviderHttpTest(WorkspaceHttpCase):
    """Each new provider must work end to end at both configuration scopes."""

    def store(self, slug: str, base_url: str, model: str) -> None:
        s = self.server
        with s.db() as connection:
            connection.execute("INSERT INTO instance_settings (name, value) VALUES (?, ?)",
                               (f"ai_provider_{slug}", s.encrypt_secrets({"api_key": "k", "base_url": base_url, "model": model})))

    def test_claude_generation_sends_the_native_request(self) -> None:
        s = self.server
        admin = self.setup_admin()
        self.store("claude", "https://api.anthropic.com", "claude-sonnet-5")
        captured: dict = {}
        original = s.request_json
        s.request_json = lambda url, payload, headers=None: captured.update(url=url, payload=payload, headers=headers) or {"content": [{"type": "text", "text": "claude copy"}]}
        try:
            status, result, _ = self.request("POST", "/api/ai/generate", {"provider": "Claude", "model": "claude-sonnet-5", "instruction": "x", "draft": "", "channels": ["X"]}, admin)
        finally:
            s.request_json = original
        self.assertEqual(status, 200, result)
        self.assertEqual(result["copy"], "claude copy")
        self.assertEqual(captured["url"], "https://api.anthropic.com/v1/messages")
        self.assertEqual(captured["headers"]["x-api-key"], "k")
        self.assertNotIn("Authorization", captured["headers"])
        self.assertIn("system", captured["payload"])

    def test_gemini_generation_uses_the_compatible_endpoint(self) -> None:
        s = self.server
        admin = self.setup_admin()
        self.store("gemini", "https://generativelanguage.googleapis.com/v1beta/openai", "gemini-3-pro")
        captured: dict = {}
        original = s.request_json
        s.request_json = lambda url, payload, headers=None: captured.update(url=url, headers=headers) or {"choices": [{"message": {"content": "gemini copy"}}]}
        try:
            status, result, _ = self.request("POST", "/api/ai/generate", {"provider": "Gemini", "model": "gemini-3-pro", "instruction": "x", "draft": "", "channels": ["X"]}, admin)
        finally:
            s.request_json = original
        self.assertEqual(status, 200, result)
        self.assertEqual(captured["url"], "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions")
        self.assertEqual(captured["headers"]["Authorization"], "Bearer k")

    def test_model_refresh_works_for_every_new_provider(self) -> None:
        s = self.server
        self.setup_admin()
        for provider, slug, base in (("Claude", "claude", "https://api.anthropic.com"),
                                     ("Gemini", "gemini", "https://generativelanguage.googleapis.com/v1beta/openai"),
                                     ("Grok", "grok", "https://api.x.ai/v1"),
                                     ("DeepSeek", "deepseek", "https://api.deepseek.com")):
            with self.subTest(provider=provider):
                self.store(slug, base, s.AI_PROVIDER_MODELS[provider][0])
                original = s.request_get_json
                s.request_get_json = lambda url, headers=None: {"data": [{"id": "refreshed-model"}]}
                try:
                    self.assertEqual(s.ai_provider_models(provider), ["refreshed-model"])
                finally:
                    s.request_get_json = original
                self.assertTrue(s.remove_ai_provider_settings(provider))

    def test_new_providers_are_configurable_at_workspace_scope(self) -> None:
        admin = self.setup_admin()
        for provider in ("Claude", "Gemini", "Grok", "DeepSeek"):
            with self.subTest(provider=provider):
                status, payload, _ = self.request("POST", "/api/workspaces/ai-providers", {"provider": provider, "api_key": "workspace-key", "model": self.server.AI_PROVIDER_MODELS[provider][0]}, admin)
                self.assertEqual(status, 200, payload)
        status, payload, _ = self.request("GET", "/api/ai/providers", auth=admin)
        self.assertEqual(status, 200)
        configured = {item["name"] for item in payload["providers"]}
        self.assertTrue({"Claude", "Gemini", "Grok", "DeepSeek"} <= configured, configured)

    def test_a_provider_key_is_never_returned_to_the_browser(self) -> None:
        admin = self.setup_admin()
        self.request("POST", "/api/workspaces/ai-providers", {"provider": "Claude", "api_key": "sk-ant-secret", "model": "claude-sonnet-5"}, admin)
        for path in ("/api/ai/providers", "/api/workspaces/ai-providers"):
            status, payload, _ = self.request("GET", path, auth=admin)
            self.assertEqual(status, 200)
            self.assertNotIn("sk-ant-secret", repr(payload), path)


if __name__ == "__main__":
    unittest.main()
