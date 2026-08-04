"""Per-provider request/response shape adapters for text generation.

These adapters are pure: they build requests and parse responses without
touching the network or the database, so they are imported directly rather
than through the app.server facade.
"""

from __future__ import annotations

import unittest

import app.ai_adapters as adapters
import app.ai_providers as ai_providers


SETTINGS = {"name": "OpenAI", "api_key": "test-key", "base_url": "https://ai.example/v1", "model": "test-model"}
MESSAGES = [{"role": "system", "content": "be brief"}, {"role": "user", "content": "write a post"}]
OPTIONS = {"model": "chosen-model", "temperature": 0.7, "max_tokens": 700}


class ChatAdapterTest(unittest.TestCase):
    def test_default_adapter_builds_an_openai_compatible_request(self) -> None:
        url, payload, headers = adapters.ChatAdapter.build_chat_request(SETTINGS, MESSAGES, OPTIONS)
        self.assertEqual(url, "https://ai.example/v1/chat/completions")
        self.assertEqual(payload, {"model": "chosen-model", "messages": MESSAGES, "temperature": 0.7, "max_tokens": 700})
        self.assertEqual(headers, {"Authorization": "Bearer test-key"})

    def test_default_adapter_reads_the_first_choice_message(self) -> None:
        self.assertEqual(adapters.ChatAdapter.parse_chat_response({"choices": [{"message": {"content": "  copy  "}}]}), "copy")

    def test_default_adapter_rejects_a_response_without_usable_copy(self) -> None:
        for result in ({}, {"choices": []}, {"choices": "nonsense"}, {"choices": [{"message": {"content": "   "}}]}, {"choices": [{"message": {"content": 7}}]}):
            with self.subTest(result=result), self.assertRaisesRegex(adapters.ProviderError, "did not return post copy"):
                adapters.ChatAdapter.parse_chat_response(result)

    def test_default_adapter_cache_busts_the_model_catalog(self) -> None:
        url, headers = adapters.ChatAdapter.build_model_list_request(SETTINGS)
        self.assertTrue(url.startswith("https://ai.example/v1/models?refresh="))
        self.assertEqual(headers, {"Authorization": "Bearer test-key"})

    def test_model_catalog_request_is_unauthenticated_without_a_key(self) -> None:
        _, headers = adapters.ChatAdapter.build_model_list_request({**SETTINGS, "api_key": ""})
        self.assertIsNone(headers)

    def test_model_list_is_deduplicated_sorted_and_bounded(self) -> None:
        result = {"data": [{"id": "model-b"}, {"id": "model-a"}, {"id": "model-a"}, {"model": "Model-C"}, {"name": "model-d"}, "model-e", {"id": "x" * 201}, 42]}
        self.assertEqual(adapters.ChatAdapter.parse_model_list(result), ["model-a", "model-b", "Model-C", "model-d", "model-e"])

    def test_model_list_accepts_the_alternate_models_key(self) -> None:
        self.assertEqual(adapters.ChatAdapter.parse_model_list({"models": [{"id": "only"}]}), ["only"])

    def test_model_list_rejects_invalid_and_empty_catalogs(self) -> None:
        with self.assertRaisesRegex(adapters.ProviderError, "invalid model list"):
            adapters.ChatAdapter.parse_model_list({"data": "nonsense"})
        with self.assertRaisesRegex(adapters.ProviderError, "did not return any selectable models"):
            adapters.ChatAdapter.parse_model_list({"data": [{"unrecognized": "shape"}]})


class MiniMaxAdapterTest(unittest.TestCase):
    def test_chat_uses_the_documented_minimax_path(self) -> None:
        url, payload, headers = adapters.MiniMaxAdapter.build_chat_request(SETTINGS, MESSAGES, OPTIONS)
        self.assertEqual(url, "https://ai.example/v1/text/chatcompletion_v2")
        self.assertEqual((payload, headers), adapters.ChatAdapter.build_chat_request(SETTINGS, MESSAGES, OPTIONS)[1:])

    def test_model_catalog_uri_is_left_exactly_as_documented(self) -> None:
        url, _ = adapters.MiniMaxAdapter.build_model_list_request(SETTINGS)
        self.assertEqual(url, "https://ai.example/v1/models")


class AdapterRegistryTest(unittest.TestCase):
    def test_every_provider_declares_an_adapter(self) -> None:
        for name, definition in ai_providers.AI_PROVIDERS.items():
            with self.subTest(provider=name):
                self.assertTrue(issubclass(definition.adapter, adapters.ChatAdapter))

    def test_provider_definitions_expose_named_fields(self) -> None:
        openai = ai_providers.AI_PROVIDERS["OpenAI"]
        self.assertEqual((openai.slug, openai.environment_prefix, openai.base_url), ("openai", "SOSOPO_AI_OPENAI", "https://api.openai.com/v1"))
        self.assertIs(openai.adapter, adapters.ChatAdapter)

    def test_only_openrouter_publishes_its_catalog_anonymously(self) -> None:
        anonymous = {name for name, definition in ai_providers.AI_PROVIDERS.items() if not definition.adapter.catalog_needs_key}
        self.assertEqual(anonymous, {"OpenRouter"})


if __name__ == "__main__":
    unittest.main()
