"""Job claiming must hand each post and media job to exactly one worker."""

from __future__ import annotations

import threading
import unittest

try:
    from tests.test_workspaces import WorkspaceHttpCase
except ImportError:
    from test_workspaces import WorkspaceHttpCase


class ClaimTestCase(WorkspaceHttpCase):
    def scheduled_post(self) -> int:
        s = self.server
        with s.db() as connection:
            return s.insert_id(connection, "INSERT INTO posts (body, channel, state, scheduled_for, created_at) VALUES ('queued', 'X', 'scheduled', ?, ?)", (s.now(), s.now()))

    def queued_media_job(self) -> int:
        s = self.server
        admin = self.setup_admin()
        workspace_id = self.active_workspace(admin)["workspace"]["id"]
        with s.db() as connection:
            return s.insert_id(connection,
                "INSERT INTO media_jobs (workspace_id, user_id, kind, prompt, aspect_ratio, style, provider, model, status, created_at, updated_at) VALUES (?, 1, 'image', 'p', '1:1', '', 'OpenAI', 'gpt-image-1', 'queued', ?, ?)",
                (workspace_id, s.now(), s.now()))


class SequentialClaimTest(ClaimTestCase):
    def test_a_post_is_claimed_once(self) -> None:
        s = self.server
        post_id = self.scheduled_post()
        self.assertTrue(s.claim_post(post_id))
        self.assertFalse(s.claim_post(post_id))

    def test_a_media_job_is_claimed_once(self) -> None:
        s = self.server
        self.queued_media_job()
        self.assertIsNotNone(s.claim_media_job())
        self.assertIsNone(s.claim_media_job())

    def test_claiming_increments_attempts_exactly_once(self) -> None:
        s = self.server
        post_id = self.scheduled_post()
        s.claim_post(post_id)
        s.claim_post(post_id)
        with s.db() as connection:
            row = connection.execute("SELECT attempts, state FROM posts WHERE id = ?", (post_id,)).fetchone()
        self.assertEqual((row["attempts"], row["state"]), (1, "publishing"))


class ConcurrentClaimTest(ClaimTestCase):
    """Several workers racing for the same row: exactly one may win."""

    def race(self, attempt) -> list:
        results, barrier = [], threading.Barrier(4)
        lock = threading.Lock()

        def worker() -> None:
            barrier.wait()
            try:
                outcome = attempt()
            except Exception as error:  # a backend lock error is a loss, not a second claim
                outcome = error
            with lock:
                results.append(outcome)

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        return results

    def test_only_one_worker_claims_a_post(self) -> None:
        s = self.server
        post_id = self.scheduled_post()
        results = self.race(lambda: s.claim_post(post_id))
        self.assertEqual(sum(1 for item in results if item is True), 1, results)
        with s.db() as connection:
            self.assertEqual(connection.execute("SELECT attempts FROM posts WHERE id = ?", (post_id,)).fetchone()["attempts"], 1)

    def test_only_one_worker_claims_a_media_job(self) -> None:
        s = self.server
        self.queued_media_job()
        results = self.race(s.claim_media_job)
        claimed = [item for item in results if isinstance(item, dict) or (item is not None and not isinstance(item, Exception))]
        self.assertEqual(len(claimed), 1, results)


class LeaseRecoveryTest(ClaimTestCase):
    def test_an_expired_lease_returns_the_post_to_the_queue(self) -> None:
        s = self.server
        post_id = self.scheduled_post()
        self.assertTrue(s.claim_post(post_id))
        with s.db() as connection:
            connection.execute("UPDATE posts SET publishing_started_at = '2000-01-01T00:00:00+00:00' WHERE id = ?", (post_id,))
        self.assertEqual(s.recover_stale_deliveries(), 1)
        self.assertTrue(s.claim_post(post_id))


if __name__ == "__main__":
    unittest.main()
