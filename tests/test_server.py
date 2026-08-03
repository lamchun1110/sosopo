"""Focused regression tests for Sosopo's safety-critical local behavior."""

from __future__ import annotations

import importlib
import os
import shutil
import tempfile
import unittest
from io import BytesIO
from datetime import UTC, datetime, timedelta
from pathlib import Path

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from PIL import Image


class SosopoTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = Path(tempfile.mkdtemp(prefix="sosopo-test-"))
        os.environ["SOSOPO_DATA_DIR"] = str(self.directory)
        os.environ["SOSOPO_ENCRYPTION_KEY"] = "Gd0EwA9sy_00SUdECwYyWEnyx3axpfAP7jSEWo2-YIE="
        import app.server as imported_server
        self.server = importlib.reload(imported_server)
        self.server.setup_database()

    def tearDown(self) -> None:
        shutil.rmtree(self.directory, ignore_errors=True)
        os.environ.pop("SOSOPO_DATA_DIR", None)
        os.environ.pop("SOSOPO_ENCRYPTION_KEY", None)

    def test_timezone_conversion_and_past_schedule_rejection(self) -> None:
        scheduled = self.server.Handler._schedule_time("2030-01-01T09:30", "Asia/Hong_Kong")
        self.assertEqual(scheduled, "2030-01-01T01:30:00+00:00")
        with self.assertRaises(ValueError):
            self.server.Handler._schedule_time("2020-01-01T09:30", "UTC")

    def test_discord_and_linkedin_provider_validation(self) -> None:
        s = self.server
        s.validate_post("Discord", "hello", None)
        s.validate_post("LinkedIn", "hello", None)
        with self.assertRaisesRegex(ValueError, "LinkedIn supports up to 0 images"):
            s.validate_post("LinkedIn", "hello", "/uploads/image.png", 1)

    def test_discord_publish_uses_encrypted_webhook_connection(self) -> None:
        s = self.server
        captured: dict[str, object] = {}
        original_request_json = s.request_json
        s.request_json = lambda url, payload, headers=None: captured.update(url=url, payload=payload) or {"id": "discord-message"}
        try:
            result = s.publish({"body": "hello", "channel": "Discord", "media_urls": []}, {"provider": "Discord", "external_account_id": "123", "encrypted_secrets": s.encrypt_secrets({"webhook_url": "https://discord.com/api/webhooks/123/secret"})})
        finally:
            s.request_json = original_request_json
        self.assertEqual(result, "discord-message")
        self.assertEqual(captured["url"], "https://discord.com/api/webhooks/123/secret?wait=true")
        self.assertEqual(captured["payload"], {"content": "hello", "embeds": [], "allowed_mentions": {"parse": []}})

    def test_delivered_discord_post_uses_its_saved_webhook_for_deletion(self) -> None:
        s = self.server
        captured = []
        original_request_delete = s.request_delete
        s.request_delete = lambda url, headers=None: captured.append((url, headers)) or {}
        try:
            s.delete_published_content({"channel": "Discord"}, "message-id", {"provider": "Discord", "external_account_id": "123", "encrypted_secrets": s.encrypt_secrets({"webhook_url": "https://discord.com/api/webhooks/123/secret"})})
        finally:
            s.request_delete = original_request_delete
        self.assertEqual(captured, [("https://discord.com/api/webhooks/123/secret/messages/message-id", None)])

    def test_delivered_telegram_album_deletes_every_message(self) -> None:
        s = self.server
        calls = []
        original_telegram_request = s.telegram_request
        s.telegram_request = lambda token, method, fields, image=None: calls.append((token, method, fields)) or {"ok": True}
        try:
            s.delete_published_content({"channel": "Telegram"}, "10,11", {"provider": "Telegram", "external_account_id": "-1001", "encrypted_secrets": s.encrypt_secrets({"bot_token": "bot-token"})})
        finally:
            s.telegram_request = original_telegram_request
        self.assertEqual(calls, [("bot-token", "deleteMessage", {"chat_id": "-1001", "message_id": "10"}), ("bot-token", "deleteMessage", {"chat_id": "-1001", "message_id": "11"})])

    def test_discord_oauth_connection_stores_a_webhook_secret(self) -> None:
        s = self.server
        original_request_form = s.request_form
        os.environ["SOSOPO_PUBLIC_URL"] = "https://sosopo.example.test"
        s.request_form = lambda url, payload, headers=None: {"access_token": "oauth-token", "webhook": {"id": "123", "token": "webhook-secret", "channel_id": "456", "name": "Announcements"}}
        try:
            records = s.social_oauth_connections("Discord", {"client_id": "client", "client_secret": "secret", "token": "https://discord.example/token"}, "code", None)
        finally:
            s.request_form = original_request_form
            os.environ.pop("SOSOPO_PUBLIC_URL", None)
        self.assertEqual(records[0]["external_account_id"], "123")
        self.assertEqual(records[0]["secret_name"], "webhook_url")
        self.assertEqual(records[0]["access_token"], "https://discord.com/api/webhooks/123/webhook-secret")

    def test_instagram_uses_the_facebook_oauth_configuration(self) -> None:
        os.environ["FACEBOOK_OAUTH_CLIENT_ID"] = "meta-client"
        os.environ["FACEBOOK_OAUTH_CLIENT_SECRET"] = "meta-secret"
        try:
            self.assertEqual(self.server.social_oauth_settings("Instagram")["client_id"], "meta-client")
            self.assertTrue(self.server.social_oauth_enabled("Instagram"))
        finally:
            os.environ.pop("FACEBOOK_OAUTH_CLIENT_ID", None)
            os.environ.pop("FACEBOOK_OAUTH_CLIENT_SECRET", None)

    def test_openai_compatible_ai_provider_configuration_is_secret_safe(self) -> None:
        os.environ["SOSOPO_AI_OPENAI_API_KEY"] = "test-key"
        os.environ["SOSOPO_AI_OPENAI_MODEL"] = "test-model"
        try:
            settings = self.server.ai_provider_settings("OpenAI")
            self.assertEqual(settings["base_url"], "https://api.openai.com/v1")
            self.assertEqual(self.server.available_ai_providers(), [{"name": "OpenAI", "model": "test-model", "models": self.server.AI_PROVIDER_MODELS["OpenAI"]}])
        finally:
            os.environ.pop("SOSOPO_AI_OPENAI_API_KEY", None)
            os.environ.pop("SOSOPO_AI_OPENAI_MODEL", None)

    def test_database_ai_provider_settings_override_environment(self) -> None:
        s = self.server
        with s.db() as connection:
            connection.execute("INSERT INTO instance_settings (name, value) VALUES (?, ?)", ("ai_provider_openai", s.encrypt_secrets({"api_key": "stored-key", "base_url": "https://ai.example/v1", "model": "stored-model"})))
        settings = s.ai_provider_settings("OpenAI")
        self.assertEqual({key: settings[key] for key in ("base_url", "model")}, {"base_url": "https://ai.example/v1", "model": "stored-model"})
        self.assertEqual(s.available_ai_providers(), [{"name": "OpenAI", "model": "stored-model", "models": s.AI_PROVIDER_MODELS["OpenAI"]}])

    def test_ai_models_are_fetched_from_the_provider_models_endpoint(self) -> None:
        s = self.server
        with s.db() as connection:
            connection.execute("INSERT INTO instance_settings (name, value) VALUES (?, ?)", ("ai_provider_openai", s.encrypt_secrets({"api_key": "stored-key", "base_url": "https://ai.example/v1", "model": "stored-model"})))
        original_request_get_json = s.request_get_json
        s.request_get_json = lambda url, headers=None: {"data": [{"id": "model-b"}, {"id": "model-a"}, {"id": "model-a"}]}
        try:
            self.assertEqual(s.ai_provider_models("OpenAI"), ["model-a", "model-b"])
            self.assertEqual(s.ai_model_catalog(s.stored_ai_provider_settings("OpenAI")), ["model-a", "model-b"])
        finally:
            s.request_get_json = original_request_get_json

    def test_minimax_model_refresh_uses_its_live_models_endpoint(self) -> None:
        s = self.server
        with s.db() as connection:
            connection.execute("INSERT INTO instance_settings (name, value) VALUES (?, ?)", ("ai_provider_minimax", s.encrypt_secrets({"api_key": "stored-key", "base_url": "https://api.minimax.io/v1", "model": "MiniMax-M2.7"})))
        calls = []
        original_request_get_json = s.request_get_json
        s.request_get_json = lambda url, headers=None: calls.append(url) or {"data": [{"id": "MiniMax-M2.8"}]}
        try:
            self.assertEqual(s.ai_provider_models("MiniMax"), ["MiniMax-M2.8"])
            self.assertEqual(calls[0], "https://api.minimax.io/v1/models")
        finally:
            s.request_get_json = original_request_get_json

    def test_ai_generation_rejects_models_outside_the_saved_catalog(self) -> None:
        s = self.server
        with s.db() as connection:
            connection.execute("INSERT INTO instance_settings (name, value) VALUES (?, ?)", ("ai_provider_openai", s.encrypt_secrets({"api_key": "stored-key", "base_url": "https://ai.example/v1", "model": "model-a", "models": '["model-a"]'})))
        with self.assertRaisesRegex(s.ProviderError, "refreshed model catalog"):
            s.generate_post_copy("OpenAI", "not-in-catalog", "", "", ["Facebook"])

    def test_multi_account_worker_marks_each_target_published(self) -> None:
        s = self.server
        with s.db() as connection:
            user_id = s.insert_id(connection, "INSERT INTO users (username, password_salt, password_hash, role, timezone, created_at) VALUES (?, ?, ?, ?, ?, ?)", ("owner", "salt", "hash", "user", "UTC", s.now()))
            post_id = s.insert_id(connection, "INSERT INTO posts (user_id, body, channel, state, scheduled_for, created_at) VALUES (?, ?, ?, 'publishing', ?, ?)", (user_id, "hello", "Telegram", s.now(), s.now()))
            for account_id in ("-100001", "-100002"):
                connection_id = s.insert_id(connection, "INSERT INTO connections (user_id, provider, external_account_id, display_name, encrypted_secrets, settings_json, created_at) VALUES (?, ?, ?, ?, ?, '{}', ?)", (user_id, "Telegram", account_id, account_id, s.encrypt_secrets({"bot_token": "test"}), s.now()))
                connection.execute("INSERT INTO post_targets (post_id, connection_id) VALUES (?, ?)", (post_id, connection_id))
        delivered: list[str] = []
        original_publish = s.publish
        s.publish = lambda post, account=None: delivered.append(account["external_account_id"]) or f"remote-{account['external_account_id']}"
        try:
            s.deliver(post_id)
        finally:
            s.publish = original_publish
        with s.db() as connection:
            post = connection.execute("SELECT state FROM posts WHERE id = ?", (post_id,)).fetchone()
            targets = connection.execute("SELECT state FROM post_targets WHERE post_id = ?", (post_id,)).fetchall()
        self.assertEqual(post["state"], "published")
        self.assertEqual({target["state"] for target in targets}, {"published"})
        self.assertEqual(set(delivered), {"-100001", "-100002"})

    def test_claim_is_atomic(self) -> None:
        s = self.server
        with s.db() as connection:
            post_id = s.insert_id(connection, "INSERT INTO posts (body, channel, state, scheduled_for, created_at) VALUES (?, ?, 'scheduled', ?, ?)", ("hello", "X", (datetime.now(UTC) - timedelta(minutes=1)).isoformat(), s.now()))
        self.assertTrue(s.claim_post(post_id))
        self.assertFalse(s.claim_post(post_id))

    def test_audit_event_is_persisted_without_secrets(self) -> None:
        s = self.server
        s.audit(7, "connection.created", "connection", 11, "Created Telegram connection", "127.0.0.1")
        with s.db() as connection:
            event = connection.execute("SELECT user_id, action, subject_id, detail FROM audit_events").fetchone()
        self.assertEqual(dict(event), {"user_id": 7, "action": "connection.created", "subject_id": "11", "detail": "Created Telegram connection"})

    def test_inactive_user_session_is_rejected(self) -> None:
        s = self.server
        with s.db() as connection:
            user_id = s.insert_id(connection, "INSERT INTO users (username, password_salt, password_hash, role, is_active, timezone, created_at) VALUES (?, ?, ?, ?, 0, ?, ?)", ("disabled", "salt", "hash", "user", "UTC", s.now()))
            connection.execute("INSERT INTO sessions (token_hash, csrf_token, user_id, expires_at, created_at) VALUES (?, ?, ?, ?, ?)", ("token", "csrf", user_id, s.expires_at(), s.now()))
            session = connection.execute("SELECT sessions.* FROM sessions JOIN users ON users.id = sessions.user_id WHERE sessions.user_id = ? AND users.is_active = 1", (user_id,)).fetchone()
        self.assertIsNone(session)

    def test_failed_delivery_is_rescheduled_with_backoff(self) -> None:
        s = self.server
        with s.db() as connection:
            post_id = s.insert_id(connection, "INSERT INTO posts (body, channel, state, attempts, scheduled_for, created_at) VALUES (?, ?, 'publishing', 1, ?, ?)", ("hello", "Telegram", s.now(), s.now()))
        original_publish = s.publish
        s.publish = lambda post, account=None: (_ for _ in ()).throw(s.ProviderError("temporary provider failure"))
        try:
            s.deliver(post_id)
        finally:
            s.publish = original_publish
        with s.db() as connection:
            post = connection.execute("SELECT state, scheduled_for, last_error FROM posts WHERE id = ?", (post_id,)).fetchone()
        self.assertEqual(post["state"], "scheduled")
        self.assertGreater(datetime.fromisoformat(post["scheduled_for"]), datetime.now(UTC) + timedelta(seconds=20))
        self.assertIn("temporary provider failure", post["last_error"])

    def test_worker_heartbeat_is_observable(self) -> None:
        s = self.server
        self.assertFalse(s.worker_healthy())
        s.worker_heartbeat()
        self.assertTrue(s.worker_healthy())

    def test_file_backed_secret_configuration(self) -> None:
        secret = self.directory / "secret"
        secret.write_text("from-file\n", encoding="utf-8")
        os.environ["TEST_VALUE"] = "from-environment"
        os.environ["TEST_VALUE_FILE"] = str(secret)
        try:
            self.assertEqual(self.server.environment_value("TEST_VALUE"), "from-file")
        finally:
            os.environ.pop("TEST_VALUE", None)
            os.environ.pop("TEST_VALUE_FILE", None)

    def test_cleanup_removes_expired_session_and_oidc_state(self) -> None:
        s = self.server
        expired = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
        with s.db() as connection:
            user_id = s.insert_id(connection, "INSERT INTO users (username, password_salt, password_hash, role, timezone, created_at) VALUES (?, ?, ?, ?, ?, ?)", ("expired", "salt", "hash", "user", "UTC", s.now()))
            connection.execute("INSERT INTO sessions (token_hash, csrf_token, user_id, expires_at, created_at) VALUES (?, ?, ?, ?, ?)", ("expired-token", "csrf", user_id, expired, s.now()))
            connection.execute("INSERT INTO oidc_states (state, nonce, code_verifier, expires_at) VALUES (?, ?, ?, ?)", ("expired-state", "nonce", "verifier", expired))
        s.cleanup_expired_records()
        with s.db() as connection:
            self.assertIsNone(connection.execute("SELECT 1 FROM sessions WHERE token_hash = 'expired-token'").fetchone())
            self.assertIsNone(connection.execute("SELECT 1 FROM oidc_states WHERE state = 'expired-state'").fetchone())

    def test_image_signature_detection_rejects_mismatches(self) -> None:
        s = self.server
        self.assertEqual(s.detected_image_type(b"\x89PNG\r\n\x1a\nrest"), "image/png")
        self.assertEqual(s.detected_image_type(b"GIF89arest"), "image/gif")
        self.assertEqual(s.detected_image_type(b"\xff\xd8\xff\xe0rest"), "image/jpeg")
        self.assertEqual(s.detected_image_type(b"RIFFxxxxWEBPrest"), "image/webp")
        self.assertIsNone(s.detected_image_type(b"<script>alert(1)</script>"))

    def test_image_decoder_checks_real_image_content(self) -> None:
        s = self.server
        buffer = BytesIO()
        Image.new("RGB", (1, 1), "white").save(buffer, format="PNG")
        self.assertEqual(s.inspect_image(buffer.getvalue(), "image/png"), (1, 1))
        with self.assertRaisesRegex(ValueError, "cannot be decoded"):
            s.inspect_image(b"not an image", "image/png")

    def test_non_retryable_provider_failure_goes_to_dead_letter_state(self) -> None:
        s = self.server
        with s.db() as connection:
            post_id = s.insert_id(connection, "INSERT INTO posts (body, channel, state, attempts, scheduled_for, created_at) VALUES (?, ?, 'publishing', 1, ?, ?)", ("hello", "Telegram", s.now(), s.now()))
        original_publish = s.publish
        s.publish = lambda post, account=None: (_ for _ in ()).throw(s.ProviderError("invalid media", retryable=False))
        try:
            s.deliver(post_id)
        finally:
            s.publish = original_publish
        with s.db() as connection:
            post = connection.execute("SELECT state, last_error FROM posts WHERE id = ?", (post_id,)).fetchone()
        self.assertEqual(post["state"], "failed")
        self.assertIn("invalid media", post["last_error"])

    def test_retry_after_is_bounded(self) -> None:
        s = self.server
        self.assertEqual(s.parse_retry_after("90"), 90)
        self.assertEqual(s.parse_retry_after("999999"), s.RETRY_MAX_SECONDS)
        self.assertIsNone(s.parse_retry_after("tomorrow"))

    def test_disabled_connection_never_calls_provider(self) -> None:
        s = self.server
        with s.db() as connection:
            user_id = s.insert_id(connection, "INSERT INTO users (username, password_salt, password_hash, role, timezone, created_at) VALUES (?, ?, ?, ?, ?, ?)", ("connection-owner", "salt", "hash", "user", "UTC", s.now()))
            post_id = s.insert_id(connection, "INSERT INTO posts (user_id, body, channel, state, scheduled_for, created_at) VALUES (?, ?, ?, 'publishing', ?, ?)", (user_id, "hello", "Telegram", s.now(), s.now()))
            connection_id = s.insert_id(connection, "INSERT INTO connections (user_id, provider, external_account_id, display_name, encrypted_secrets, settings_json, is_active, created_at) VALUES (?, ?, ?, ?, ?, '{}', 0, ?)", (user_id, "Telegram", "-100", "disabled", s.encrypt_secrets({"bot_token": "test"}), s.now()))
            connection.execute("INSERT INTO post_targets (post_id, connection_id) VALUES (?, ?)", (post_id, connection_id))
        original_publish = s.publish
        s.publish = lambda post, account=None: self.fail("disabled account must not publish")
        try:
            s.deliver(post_id)
        finally:
            s.publish = original_publish
        with s.db() as connection:
            post = connection.execute("SELECT state, last_error FROM posts WHERE id = ?", (post_id,)).fetchone()
        self.assertEqual(post["state"], "scheduled")
        self.assertIn("disabled", post["last_error"])

    def test_claim_records_publishing_lease(self) -> None:
        s = self.server
        with s.db() as connection:
            post_id = s.insert_id(connection, "INSERT INTO posts (body, channel, state, scheduled_for, created_at) VALUES (?, ?, 'scheduled', ?, ?)", ("hello", "X", s.now(), s.now()))
        self.assertTrue(s.claim_post(post_id))
        with s.db() as connection:
            post = connection.execute("SELECT state, publishing_started_at, attempts FROM posts WHERE id = ?", (post_id,)).fetchone()
        self.assertEqual(post["state"], "publishing")
        self.assertIsNotNone(post["publishing_started_at"])
        self.assertEqual(post["attempts"], 1)

    def test_stale_publishing_lease_is_recovered(self) -> None:
        s = self.server
        stale = (datetime.now(UTC) - timedelta(seconds=s.PUBLISHING_LEASE_SECONDS + 1)).isoformat()
        with s.db() as connection:
            post_id = s.insert_id(connection, "INSERT INTO posts (body, channel, state, publishing_started_at, scheduled_for, created_at) VALUES (?, ?, 'publishing', ?, ?, ?)", ("hello", "X", stale, stale, s.now()))
        self.assertEqual(s.recover_stale_deliveries(), 1)
        with s.db() as connection:
            post = connection.execute("SELECT state, publishing_started_at, last_error FROM posts WHERE id = ?", (post_id,)).fetchone()
        self.assertEqual(post["state"], "scheduled")
        self.assertIsNone(post["publishing_started_at"])
        self.assertIn("lease expired", post["last_error"])

    def test_expired_connection_token_is_rejected_before_publish(self) -> None:
        s = self.server
        expired_account = {"token_expires_at": (datetime.now(UTC) - timedelta(minutes=1)).isoformat(), "encrypted_secrets": s.encrypt_secrets({"access_token": "test"})}
        with self.assertRaisesRegex(s.ProviderError, "token has expired"):
            s.publish({"channel": "X", "body": "hello", "image_url": None}, expired_account)
        self.assertTrue(s.token_is_expired(expired_account["token_expires_at"]))
        self.assertFalse(s.token_is_expired((datetime.now(UTC) + timedelta(days=1)).isoformat()))

    def test_production_preflight_detects_missing_configuration(self) -> None:
        import scripts.preflight as preflight
        failures, _ = preflight.checks(True)
        self.assertTrue(any("SOSOPO_PUBLIC_URL" in failure for failure in failures))

    def test_password_hash_rotation_changes_verifier(self) -> None:
        s = self.server
        first_salt = b"a" * 16
        second_salt = b"b" * 16
        old_hash = s.hash_password("old-password-123", first_salt)
        new_hash = s.hash_password("new-password-123", second_salt)
        self.assertNotEqual(old_hash, new_hash)
        self.assertTrue(__import__("secrets").compare_digest(s.hash_password("new-password-123", second_salt), new_hash))

    def test_backup_secret_file_value(self) -> None:
        import scripts.backup as backup
        secret = self.directory / "backup-secret"
        secret.write_text("secret-from-file\n", encoding="utf-8")
        os.environ["BACKUP_TEST_VALUE_FILE"] = str(secret)
        try:
            self.assertEqual(backup.environment_value("BACKUP_TEST_VALUE"), "secret-from-file")
        finally:
            os.environ.pop("BACKUP_TEST_VALUE_FILE", None)

    def test_invalid_fernet_key_has_safe_provider_error(self) -> None:
        s = self.server
        original = os.environ["SOSOPO_ENCRYPTION_KEY"]
        os.environ["SOSOPO_ENCRYPTION_KEY"] = "invalid"
        try:
            with self.assertRaisesRegex(s.ProviderError, "valid Fernet key"):
                s.encrypt_secrets({"access_token": "secret"})
        finally:
            os.environ["SOSOPO_ENCRYPTION_KEY"] = original

    def test_delivery_history_query_is_scoped_by_post(self) -> None:
        s = self.server
        with s.db() as connection:
            first = s.insert_id(connection, "INSERT INTO posts (body, channel, state, created_at) VALUES (?, ?, ?, ?)", ("first", "X", "published", s.now()))
            second = s.insert_id(connection, "INSERT INTO posts (body, channel, state, created_at) VALUES (?, ?, ?, ?)", ("second", "X", "published", s.now()))
            connection.execute("INSERT INTO deliveries (post_id, provider, status, detail, created_at) VALUES (?, ?, ?, ?, ?)", (first, "X", "published", "first-id", s.now()))
            connection.execute("INSERT INTO deliveries (post_id, provider, status, detail, created_at) VALUES (?, ?, ?, ?, ?)", (second, "X", "failed", "second-error", s.now()))
            records = connection.execute("SELECT detail FROM deliveries WHERE post_id = ? ORDER BY id DESC", (first,)).fetchall()
        self.assertEqual([record["detail"] for record in records], ["first-id"])

    def test_connection_secret_rotation_merges_without_exposing_secret(self) -> None:
        s = self.server
        original = s.encrypt_secrets({"access_token": "old", "other": "kept"})
        rotated = s.encrypt_secrets({**s.decrypt_secrets(original), "access_token": "new"})
        self.assertEqual(s.decrypt_secrets(rotated), {"access_token": "new", "other": "kept"})
        self.assertNotIn("old", rotated)

    def test_provider_specific_post_validation(self) -> None:
        s = self.server
        with self.assertRaisesRegex(ValueError, "Instagram publishing requires"):
            s.validate_post("Instagram", "caption", None)
        with self.assertRaisesRegex(ValueError, "Threads posts must be 500"):
            s.validate_post("Threads", "x" * 501, None)
        s.validate_post("Instagram", "caption", "/uploads/photo.jpg")
        s.validate_post("Telegram", "x" * 4096, None)

    def test_provider_media_limits_and_ordered_post_attachments(self) -> None:
        s = self.server
        with self.assertRaisesRegex(ValueError, "X supports up to 4"):
            s.validate_post("X", "caption", "/uploads/one.png", 5)
        with s.db() as connection:
            post_id = s.insert_id(connection, "INSERT INTO posts (body, channel, state, image_url, created_at) VALUES (?, ?, ?, ?, ?)", ("caption", "Facebook", "draft", "/uploads/first.png", s.now()))
            connection.execute("INSERT INTO post_media (post_id, media_url, position) VALUES (?, ?, ?)", (post_id, "/uploads/first.png", 0))
            connection.execute("INSERT INTO post_media (post_id, media_url, position) VALUES (?, ?, ?)", (post_id, "/uploads/second.png", 1))
        self.assertEqual(s.post_media_urls({"id": post_id, "image_url": "/uploads/first.png"}), ["/uploads/first.png", "/uploads/second.png"])

    def test_target_account_provider_overrides_post_default_provider(self) -> None:
        s = self.server
        post = {"id": 0, "channel": "Facebook", "body": "hello", "image_url": None}
        account = {"provider": "X", "external_account_id": "profile", "encrypted_secrets": s.encrypt_secrets({"access_token": "token"}), "token_expires_at": None}
        original_request = s.request_json
        calls: list[str] = []
        s.request_json = lambda url, payload, headers=None: calls.append(url) or {"data": {"id": "remote"}}
        try:
            self.assertEqual(s.publish(post, account), "remote")
        finally:
            s.request_json = original_request
        self.assertEqual(calls, ["https://api.x.com/2/tweets"])

    def test_initial_setup_marker_is_unique(self) -> None:
        s = self.server
        with s.db() as connection:
            connection.execute("INSERT INTO instance_settings (name, value) VALUES ('initial_setup', ?)", (s.now(),))
        with self.assertRaises(Exception):
            with s.db() as connection:
                connection.execute("INSERT INTO instance_settings (name, value) VALUES ('initial_setup', ?)", (s.now(),))

    def test_restore_rejects_unsafe_archive_members(self) -> None:
        import io
        import tarfile
        import scripts.restore as restore
        archive = self.directory / "unsafe.tar.gz"
        with tarfile.open(archive, "w:gz") as output:
            member = tarfile.TarInfo("../escape")
            member.size = 1
            output.addfile(member, io.BytesIO(b"x"))
        with self.assertRaisesRegex(SystemExit, "unsafe path"):
            restore.extract(archive, self.directory / "extracted")

    def test_restore_uploads_preserves_previous_media(self) -> None:
        import scripts.restore as restore
        root = self.directory / "archive"
        (root / "uploads").mkdir(parents=True)
        (root / "uploads" / "new.png").write_bytes(b"new")
        data = self.directory / "restore-data"
        (data / "uploads").mkdir(parents=True)
        (data / "uploads" / "old.png").write_bytes(b"old")
        restore.restore_uploads(root, data, "stamp")
        self.assertEqual((data / "uploads" / "new.png").read_bytes(), b"new")
        self.assertEqual((data / "uploads.before-restore-stamp" / "old.png").read_bytes(), b"old")

    def test_forwarded_ip_requires_trusted_proxy(self) -> None:
        s = self.server
        self.assertEqual(s.source_ip("198.51.100.4", "203.0.113.9", "127.0.0.1/32"), "198.51.100.4")
        self.assertEqual(s.source_ip("127.0.0.1", "203.0.113.9, 127.0.0.1", "127.0.0.1/32"), "203.0.113.9")
        self.assertEqual(s.source_ip("127.0.0.1", "not-an-ip", "127.0.0.1/32"), "127.0.0.1")

    def test_s3_media_url_requires_public_https_url(self) -> None:
        s = self.server
        previous = {key: os.environ.get(key) for key in ("SOSOPO_STORAGE_BACKEND", "SOSOPO_MEDIA_PUBLIC_URL", "S3_MEDIA_PREFIX")}
        os.environ.update({"SOSOPO_STORAGE_BACKEND": "s3", "SOSOPO_MEDIA_PUBLIC_URL": "https://media.example.com", "S3_MEDIA_PREFIX": "uploads"})
        try:
            self.assertEqual(s.media_url("photo.png"), "https://media.example.com/uploads/photo.png")
            self.assertEqual(s.public_image_url("https://media.example.com/uploads/photo.png"), "https://media.example.com/uploads/photo.png")
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def test_preflight_rejects_incomplete_s3_media_configuration(self) -> None:
        import scripts.preflight as preflight
        previous = {key: os.environ.get(key) for key in ("SOSOPO_STORAGE_BACKEND", "S3_MEDIA_BUCKET", "SOSOPO_MEDIA_PUBLIC_URL")}
        os.environ["SOSOPO_STORAGE_BACKEND"] = "s3"
        os.environ.pop("S3_MEDIA_BUCKET", None)
        os.environ.pop("SOSOPO_MEDIA_PUBLIC_URL", None)
        try:
            failures, _ = preflight.checks(True)
            self.assertTrue(any("S3_MEDIA_BUCKET" in failure for failure in failures))
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def test_oidc_token_requires_valid_signature_and_nonce(self) -> None:
        s = self.server
        private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        claims = {"iss": "https://issuer.example", "aud": "client", "sub": "subject", "nonce": "expected", "iat": int(datetime.now(UTC).timestamp()), "exp": int((datetime.now(UTC) + timedelta(minutes=5)).timestamp())}
        token = jwt.encode(claims, private_key, algorithm="RS256", headers={"kid": "test"})
        class KeyClient:
            def __init__(self, uri: str) -> None: pass
            def get_signing_key_from_jwt(self, value: str):
                return type("SigningKey", (), {"key": private_key.public_key()})()
        original = s.PyJWKClient
        s.PyJWKClient = KeyClient
        settings = {"jwks_uri": "https://issuer.example/keys", "issuer": "https://issuer.example", "client_id": "client", "id_token_signing_alg_values_supported": ["RS256"]}
        try:
            self.assertEqual(s.verify_oidc_id_token(token, settings, "expected")["sub"], "subject")
            with self.assertRaisesRegex(s.ProviderError, "nonce"):
                s.verify_oidc_id_token(token, settings, "wrong")
        finally:
            s.PyJWKClient = original


if __name__ == "__main__":
    unittest.main()
