"""AI media studio regression tests: jobs, moderation, isolation, and quotas."""

from __future__ import annotations

import base64
import json
import os
import unittest
from io import BytesIO

from PIL import Image

try:
    from tests.test_workspaces import WorkspaceHttpCase
except ImportError:
    from test_workspaces import WorkspaceHttpCase


def png_bytes() -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (2, 2), "teal").save(buffer, format="PNG")
    return buffer.getvalue()


class MediaStudioTest(WorkspaceHttpCase):
    def configure_instance_ai(self) -> None:
        s = self.server
        with s.db() as connection:
            connection.execute(
                "INSERT INTO instance_settings (name, value) VALUES (?, ?)",
                ("ai_provider_openai", s.encrypt_secrets({"api_key": "media-key", "base_url": "https://ai.example/v1", "model": "model-a", "models": '["model-a"]'})),
            )

    def run_queued_job(self) -> dict:
        job = self.server.claim_media_job()
        self.assertIsNotNone(job)
        self.server.run_media_job(dict(job))
        with self.server.db() as connection:
            return dict(connection.execute("SELECT * FROM media_jobs WHERE id = ?", (job["id"],)).fetchone())

    def test_image_job_lifecycle_with_moderation_gate(self) -> None:
        s = self.server
        admin = self.setup_admin()
        self.configure_instance_ai()
        status, created, _ = self.request("POST", "/api/media/jobs", {"kind": "image", "prompt": "a calm teal square", "aspect_ratio": "1:1", "style": "flat"}, admin)
        self.assertEqual(status, 201)
        self.assertEqual(created["provider"], "OpenAI")
        captured: dict = {}
        original_request_json = s.request_json
        s.request_json = lambda url, payload, headers=None: captured.update(url=url, payload=payload) or {"data": [{"b64_json": base64.b64encode(png_bytes()).decode()}]}
        try:
            job = self.run_queued_job()
        finally:
            s.request_json = original_request_json
        self.assertEqual(job["status"], "succeeded")
        self.assertEqual(job["moderation"], "pending")
        self.assertTrue(job["result_url"].startswith("/uploads/"))
        self.assertEqual(captured["url"], "https://ai.example/v1/images/generations")
        self.assertEqual(captured["payload"]["model"], "gpt-image-1.5")
        self.assertIn("Visual style: flat", captured["payload"]["prompt"])
        status, library, _ = self.request("GET", "/api/media/library", auth=admin)
        self.assertEqual(library["assets"], [])
        status, payload, _ = self.request("POST", "/api/posts", {"body": "with pending media", "channels": ["X"], "image_urls": [job["result_url"]]}, admin)
        self.assertEqual(status, 400)
        self.assertIn("approved", payload["error"])
        status, _, _ = self.request("POST", f"/api/media/jobs/{job['id']}/review", {"decision": "approved"}, admin)
        self.assertEqual(status, 200)
        status, library, _ = self.request("GET", "/api/media/library", auth=admin)
        self.assertEqual([asset["id"] for asset in library["assets"]], [job["id"]])
        status, _, _ = self.request("POST", "/api/posts", {"body": "with approved media", "channels": ["X"], "image_urls": [job["result_url"]]}, admin)
        self.assertEqual(status, 201)

    def test_generated_media_is_tenant_scoped_and_hidden_until_approved(self) -> None:
        s = self.server
        admin = self.setup_admin()
        self.configure_instance_ai()
        status, _, _ = self.request("POST", "/api/media/jobs", {"kind": "image", "prompt": "private asset"}, admin)
        self.assertEqual(status, 201)
        original_request_json = s.request_json
        s.request_json = lambda url, payload, headers=None: {"data": [{"b64_json": base64.b64encode(png_bytes()).decode()}]}
        try:
            job = self.run_queued_job()
        finally:
            s.request_json = original_request_json
        status, _, _ = self.request("POST", f"/api/media/jobs/{job['id']}/review", {"decision": "approved"}, admin)
        self.assertEqual(status, 200)
        bob = self.create_and_login(admin, "bob")
        status, jobs, _ = self.request("GET", "/api/media/jobs", auth=bob)
        self.assertEqual(jobs["jobs"], [])
        status, payload, _ = self.request("POST", "/api/posts", {"body": "steal media", "channels": ["X"], "image_urls": [job["result_url"]]}, bob)
        self.assertEqual(status, 400)
        status, _, _ = self.request("POST", f"/api/media/jobs/{job['id']}/review", {"decision": "rejected"}, bob)
        self.assertEqual(status, 404)

    def test_pending_results_are_hidden_from_non_admin_members(self) -> None:
        s = self.server
        admin = self.setup_admin()
        workspace_id = self.active_workspace(admin)["workspace"]["id"]
        self.configure_instance_ai()
        status, _, _ = self.request("POST", "/api/media/jobs", {"kind": "image", "prompt": "review me"}, admin)
        self.assertEqual(status, 201)
        original_request_json = s.request_json
        s.request_json = lambda url, payload, headers=None: {"data": [{"b64_json": base64.b64encode(png_bytes()).decode()}]}
        try:
            self.run_queued_job()
        finally:
            s.request_json = original_request_json
        bob = self.create_and_login(admin, "bob")
        status, _, _ = self.request("POST", "/api/workspaces/members", {"username": "bob", "role": "editor"}, admin)
        self.assertEqual(status, 201)
        status, _, _ = self.request("POST", "/api/me/workspace", {"workspace_id": workspace_id}, bob)
        self.assertEqual(status, 200)
        status, jobs, _ = self.request("GET", "/api/media/jobs", auth=bob)
        self.assertEqual(status, 200)
        self.assertIsNone(jobs["jobs"][0]["result_url"])
        status, jobs, _ = self.request("GET", "/api/media/jobs", auth=admin)
        self.assertIsNotNone(jobs["jobs"][0]["result_url"])

    def test_failed_jobs_record_error_and_refund_the_media_credit(self) -> None:
        s = self.server
        admin = self.setup_admin()
        workspace_id = self.active_workspace(admin)["workspace"]["id"]
        self.configure_instance_ai()
        status, _, _ = self.request("POST", "/api/media/jobs", {"kind": "image", "prompt": "will fail"}, admin)
        self.assertEqual(status, 201)
        with s.db() as connection:
            self.assertEqual(s.usage_amount(connection, workspace_id, "ai_media"), 1)
        original_request_json = s.request_json
        s.request_json = lambda url, payload, headers=None: (_ for _ in ()).throw(s.ProviderError("provider unavailable"))
        try:
            job = self.run_queued_job()
        finally:
            s.request_json = original_request_json
        self.assertEqual(job["status"], "failed")
        self.assertIn("provider unavailable", job["error"])
        with s.db() as connection:
            self.assertEqual(s.usage_amount(connection, workspace_id, "ai_media"), 0)

    def test_media_jobs_require_editor_and_supported_provider(self) -> None:
        admin = self.setup_admin()
        workspace_id = self.active_workspace(admin)["workspace"]["id"]
        self.configure_instance_ai()
        status, payload, _ = self.request("POST", "/api/media/jobs", {"kind": "video", "prompt": "clip", "provider": "Kimi"}, admin)
        self.assertEqual(status, 400)
        bob = self.create_and_login(admin, "bob")
        status, _, _ = self.request("POST", "/api/workspaces/members", {"username": "bob", "role": "viewer"}, admin)
        self.assertEqual(status, 201)
        status, _, _ = self.request("POST", "/api/me/workspace", {"workspace_id": workspace_id}, bob)
        self.assertEqual(status, 200)
        status, _, _ = self.request("POST", "/api/media/jobs", {"kind": "image", "prompt": "nope"}, bob)
        self.assertEqual(status, 403)

    def test_video_job_uses_provider_async_flow(self) -> None:
        s = self.server
        admin = self.setup_admin()
        self.configure_instance_ai()
        status, _, _ = self.request("POST", "/api/media/jobs", {"kind": "video", "prompt": "sunrise timelapse", "aspect_ratio": "16:9"}, admin)
        self.assertEqual(status, 201)
        original = (s.request_json, s.request_get_json, s.request_get_bytes, s.VIDEO_POLL_SECONDS)
        s.request_json = lambda url, payload, headers=None: {"id": "video_123", "status": "queued"}
        s.request_get_json = lambda url, headers=None: {"status": "completed", "progress": 100}
        s.request_get_bytes = lambda url, headers=None: b"FAKE-MP4-DATA"
        s.VIDEO_POLL_SECONDS = 0
        try:
            job = self.run_queued_job()
        finally:
            s.request_json, s.request_get_json, s.request_get_bytes, s.VIDEO_POLL_SECONDS = original
        self.assertEqual(job["status"], "succeeded")
        self.assertTrue(job["result_url"].endswith(".mp4"))
        self.assertEqual(job["progress"], 100)

    def test_media_quota_is_enforced(self) -> None:
        admin = self.setup_admin()
        workspace_id = self.active_workspace(admin)["workspace"]["id"]
        self.configure_instance_ai()
        with self.server.db() as connection:
            connection.execute("UPDATE workspaces SET plan = 'free' WHERE id = ?", (workspace_id,))
        os.environ["SOSOPO_PLAN_LIMITS"] = json.dumps({"free": {"ai_media_per_month": 1}})
        try:
            status, _, _ = self.request("POST", "/api/media/jobs", {"kind": "image", "prompt": "one"}, admin)
            self.assertEqual(status, 201)
            status, payload, _ = self.request("POST", "/api/media/jobs", {"kind": "image", "prompt": "two"}, admin)
            self.assertEqual(status, 400)
            self.assertIn("monthly limit", payload["error"])
        finally:
            os.environ.pop("SOSOPO_PLAN_LIMITS", None)


if __name__ == "__main__":
    unittest.main()
