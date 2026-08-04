"""LinkedIn image publishing (E1) and alt text end to end (E3)."""

from __future__ import annotations

import unittest

try:
    from tests.test_workspaces import WorkspaceHttpCase
except ImportError:
    from test_workspaces import WorkspaceHttpCase


LINKEDIN_ACCOUNT = {"provider": "LinkedIn", "external_account_id": "urn:li:person:abc", "display_name": "Member"}


class LinkedInImageTest(WorkspaceHttpCase):
    """The three-step member image flow: initialize, upload the bytes, then post."""

    def account(self) -> dict:
        s = self.server
        return {**LINKEDIN_ACCOUNT, "encrypted_secrets": s.encrypt_secrets({"access_token": "li-token"}), "token_expires_at": None}

    def publish(self, post: dict) -> tuple[str, list, list]:
        s = self.server
        calls, uploads = [], []
        original_json, original_put = s.request_json, s.request_put_bytes

        def fake_json(url, payload, headers=None):
            calls.append((url, payload, headers))
            if "images?action=initializeUpload" in url:
                return {"value": {"uploadUrl": "https://upload.linkedin.test/slot", "image": "urn:li:image:C123"}}
            return {"id": "urn:li:share:999"}

        s.request_json = fake_json
        s.request_put_bytes = lambda url, content, headers=None: uploads.append((url, len(content), headers)) or {}
        try:
            external_id = s.publish(post, self.account())
        finally:
            s.request_json, s.request_put_bytes = original_json, original_put
        return external_id, calls, uploads

    def stored_image(self) -> str:
        s = self.server
        return s.store_media("linkedin-test.png", "image/png", b"\x89PNG\r\n\x1a\n" + b"0" * 40)

    def setUp(self) -> None:
        super().setUp()
        import os
        os.environ["LINKEDIN_API_VERSION"] = "202506"

    def tearDown(self) -> None:
        import os
        os.environ.pop("LINKEDIN_API_VERSION", None)
        super().tearDown()

    def test_text_only_posts_are_unchanged(self) -> None:
        external_id, calls, uploads = self.publish({"channel": "LinkedIn", "body": "text only", "image_url": None, "media_urls": []})
        self.assertEqual(external_id, "urn:li:share:999")
        self.assertEqual(uploads, [])
        self.assertEqual(len(calls), 1)
        self.assertNotIn("content", calls[0][1])

    def test_one_image_is_initialized_uploaded_and_attached(self) -> None:
        url = self.stored_image()
        external_id, calls, uploads = self.publish({"channel": "LinkedIn", "body": "with image", "image_url": url, "media_urls": [url]})
        self.assertEqual(external_id, "urn:li:share:999")
        initialize, post = calls
        self.assertIn("images?action=initializeUpload", initialize[0])
        self.assertEqual(initialize[1]["initializeUploadRequest"]["owner"], "urn:li:person:abc")
        self.assertEqual(uploads[0][0], "https://upload.linkedin.test/slot")
        self.assertEqual(post[1]["content"]["media"]["id"], "urn:li:image:C123")

    def test_several_images_become_a_multi_image_post(self) -> None:
        first, second = self.stored_image(), self.stored_image()
        _, calls, uploads = self.publish({"channel": "LinkedIn", "body": "carousel", "image_url": first, "media_urls": [first, second]})
        self.assertEqual(len(uploads), 2)
        images = calls[-1][1]["content"]["multiImage"]["images"]
        self.assertEqual([item["id"] for item in images], ["urn:li:image:C123", "urn:li:image:C123"])

    def test_alt_text_travels_with_the_image(self) -> None:
        url = self.stored_image()
        _, calls, _ = self.publish({"channel": "LinkedIn", "body": "with alt", "image_url": url,
                                    "media_urls": [url], "media_items": [{"url": url, "alt_text": "A teal logo"}]})
        self.assertEqual(calls[-1][1]["content"]["media"]["altText"], "A teal logo")

    def test_a_failed_initialize_reports_a_provider_error(self) -> None:
        s = self.server
        url = self.stored_image()
        original = s.request_json
        s.request_json = lambda u, payload, headers=None: {"value": {}}
        try:
            with self.assertRaisesRegex(s.ProviderError, "upload"):
                s.publish({"channel": "LinkedIn", "body": "x", "image_url": url, "media_urls": [url]}, self.account())
        finally:
            s.request_json = original

    def test_linkedin_now_accepts_images_in_validation(self) -> None:
        s = self.server
        self.assertGreater(s.CHANNEL_MEDIA_LIMITS["LinkedIn"], 0)
        s.validate_post("LinkedIn", "body", "/uploads/a.png", 1)
        with self.assertRaisesRegex(ValueError, "images per post"):
            s.validate_post("LinkedIn", "body", "/uploads/a.png", s.CHANNEL_MEDIA_LIMITS["LinkedIn"] + 1)


class AltTextTest(WorkspaceHttpCase):
    def upload(self, auth: dict) -> str:
        s = self.server
        return s.store_media("alt-test.png", "image/png", b"\x89PNG\r\n\x1a\n" + b"0" * 40)

    def create_post(self, auth: dict, url: str, alt: str | None) -> dict:
        body = {"body": "a post", "channels": ["X"], "image_urls": [url]}
        if alt is not None:
            body["image_alt_texts"] = [alt]
        status, post, _ = self.request("POST", "/api/posts", body, auth)
        self.assertEqual(status, 201, post)
        return post

    def test_alt_text_is_persisted_with_its_image(self) -> None:
        admin = self.setup_admin()
        url = self.upload(admin)
        post = self.create_post(admin, url, "A teal square")
        with self.server.db() as connection:
            row = connection.execute("SELECT media_url, alt_text FROM post_media WHERE post_id = ?", (post["id"],)).fetchone()
        self.assertEqual((row["media_url"], row["alt_text"]), (url, "A teal square"))

    def test_alt_text_is_optional(self) -> None:
        admin = self.setup_admin()
        url = self.upload(admin)
        post = self.create_post(admin, url, None)
        with self.server.db() as connection:
            row = connection.execute("SELECT alt_text FROM post_media WHERE post_id = ?", (post["id"],)).fetchone()
        self.assertIsNone(row["alt_text"])

    def test_alt_text_is_bounded(self) -> None:
        admin = self.setup_admin()
        url = self.upload(admin)
        status, payload, _ = self.request("POST", "/api/posts", {"body": "a post", "channels": ["X"], "image_urls": [url], "image_alt_texts": ["x" * 2000]}, admin)
        self.assertEqual(status, 400, payload)

    def test_alt_text_reaches_x_media_metadata(self) -> None:
        s = self.server
        url = s.store_media("x-alt.png", "image/png", b"\x89PNG\r\n\x1a\n" + b"0" * 40)
        calls = []
        original = s.request_json
        def fake(endpoint, payload, headers=None):
            calls.append((endpoint, payload))
            if "media/upload" in endpoint:
                return {"data": {"id": "media-1"}}
            return {"data": {"id": "tweet-1"}}
        s.request_json = fake
        try:
            s.publish({"channel": "X", "body": "hello", "image_url": url, "media_urls": [url],
                       "media_items": [{"url": url, "alt_text": "A teal square"}]},
                      {"provider": "X", "external_account_id": "1", "encrypted_secrets": s.encrypt_secrets({"access_token": "t"}), "token_expires_at": None})
        finally:
            s.request_json = original
        metadata = [payload for endpoint, payload in calls if "metadata" in endpoint]
        self.assertTrue(metadata, [endpoint for endpoint, _ in calls])
        self.assertEqual(metadata[0]["alt_text"]["text"], "A teal square")

    def test_alt_text_is_included_in_the_workspace_export(self) -> None:
        admin = self.setup_admin()
        url = self.upload(admin)
        self.create_post(admin, url, "A teal square")
        status, export, _ = self.request("GET", "/api/workspaces/export", auth=admin)
        self.assertEqual(status, 200)
        media = [item for post in export["posts"] for item in post.get("media", [])]
        self.assertIn({"url": url, "alt_text": "A teal square"}, media)


if __name__ == "__main__":
    unittest.main()
