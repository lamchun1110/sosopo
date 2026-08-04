"""The composer: drafts, scheduling, publishing, and delivery history."""


from __future__ import annotations


from http import HTTPStatus
from typing import Any

try:  # package import (tests, `python -m app.server`)
    from ..audit import audit
    from ..config import CHANNELS, MAX_POST_MEDIA, now, timezone_name
    from ..database import Record, db, insert_id
    from ..errors import ProviderError
    from ..media_storage import media_exists
    from ..oauth import social_oauth_enabled
    from ..plans import enforce_monthly_quota, record_usage
    from ..publishing import delete_published_content, provider_status, validate_post
except ImportError:  # script import (`python /app/app/server.py`)
    from audit import audit
    from config import CHANNELS, MAX_POST_MEDIA, now, timezone_name
    from database import Record, db, insert_id
    from errors import ProviderError
    from media_storage import media_exists
    from oauth import social_oauth_enabled
    from plans import enforce_monthly_quota, record_usage
    from publishing import delete_published_content, provider_status, validate_post


class PostRoutes:
    """The composer: drafts, scheduling, publishing, and delivery history.

    Mixed into ``Handler``; every method returns True once it has answered.
    """



    def get_posts(self, path: str) -> bool:
        """Handle one post GET; True when answered."""
        if path == "/api/dashboard":
            session = self._session()
            workspace_id = self._require_workspace(session)
            if workspace_id is None:
                return True
            with db() as connection:
                posts = [dict(row) for row in connection.execute("SELECT * FROM posts WHERE workspace_id = ? ORDER BY CASE state WHEN 'scheduled' THEN 0 WHEN 'failed' THEN 1 ELSE 2 END, scheduled_for, id DESC", (workspace_id,)).fetchall()]
                for post in posts:
                    post["media_urls"] = [row["media_url"] for row in connection.execute("SELECT media_url FROM post_media WHERE post_id = ? ORDER BY position", (post["id"],)).fetchall()] or ([post["image_url"]] if post.get("image_url") else [])
            self._json({"posts": posts, "providers": [{"name": channel, "status": provider_status(channel), "oauth_available": social_oauth_enabled(channel)} for channel in CHANNELS]})
            return True
        if path.startswith("/api/posts/") and path.endswith("/deliveries"):
            try:
                post_id = int(path.split("/")[3])
            except ValueError:
                self._json({"error": "Invalid post ID."}, HTTPStatus.BAD_REQUEST); return True
            session = self._session()
            workspace_id = self._require_workspace(session)
            if workspace_id is None:
                return True
            with db() as connection:
                owner = connection.execute("SELECT id FROM posts WHERE id = ? AND workspace_id = ?", (post_id, workspace_id)).fetchone()
                if owner is None:
                    self._json({"error": "Post not found."}, HTTPStatus.NOT_FOUND); return True
                deliveries = [dict(row) for row in connection.execute("SELECT provider, status, detail, created_at FROM deliveries WHERE post_id = ? ORDER BY id DESC", (post_id,)).fetchall()]
                targets = [dict(row) for row in connection.execute("SELECT post_targets.connection_id, post_targets.state, post_targets.external_id, post_targets.last_error, connections.provider, connections.display_name, connections.external_account_id FROM post_targets JOIN connections ON connections.id = post_targets.connection_id WHERE post_targets.post_id = ? ORDER BY connections.display_name", (post_id,)).fetchall()]
            self._json({"deliveries": deliveries, "targets": targets})
            return True
        return False

    def post_posts(self, path: str, payload: dict[str, Any], session: Record) -> bool:
        """Handle one post POST; True when answered."""
        if path == "/api/posts":
            workspace_id = self._require_workspace(session, "editor")
            if workspace_id is None:
                return True
            body, legacy_channel = str(payload.get("body", "")).strip(), str(payload.get("channel", "")).strip()
            requested_channels = payload.get("channels", [legacy_channel])
            if not isinstance(requested_channels, list) or not requested_channels:
                self._json({"error": "Select at least one platform."}, HTTPStatus.BAD_REQUEST); return True
            channels = list(dict.fromkeys(str(channel).strip() for channel in requested_channels))
            image_urls = payload.get("image_urls")
            if image_urls is None:
                image_urls = [str(payload.get("image_url", "")).strip()] if payload.get("image_url") else []
            if not isinstance(image_urls, list) or any(not isinstance(url, str) or not url.strip() for url in image_urls):
                self._json({"error": "image_urls must be an array of uploaded image URLs."}, HTTPStatus.BAD_REQUEST); return True
            image_urls = list(dict.fromkeys(url.strip() for url in image_urls))
            image_url = image_urls[0] if image_urls else None
            target_ids = payload.get("connection_ids", [])
            if not body or any(channel not in CHANNELS for channel in channels):
                self._json({"error": "A post and at least one supported platform are required."}, HTTPStatus.BAD_REQUEST); return True
            if len(image_urls) > MAX_POST_MEDIA or any(not media_exists(url) for url in image_urls):
                self._json({"error": "Unknown image upload."}, HTTPStatus.BAD_REQUEST); return True
            if not isinstance(target_ids, list) or any(not isinstance(item, int) for item in target_ids):
                self._json({"error": "connection_ids must be an array of numeric account IDs."}, HTTPStatus.BAD_REQUEST); return True
            if payload.get("apply_signature", True) and session["signature"]:
                body = f"{body.rstrip()}\n\n{str(session['signature']).strip()}"
            schedule_zone = timezone_name(payload.get("scheduled_timezone") or session["timezone"])
            schedule = self._schedule_time(payload["scheduled_for"], schedule_zone) if payload.get("scheduled_for") else None
            with db() as connection:
                if target_ids:
                    placeholders = ",".join("?" for _ in target_ids)
                    selected = connection.execute(f"SELECT id, provider FROM connections WHERE workspace_id = ? AND is_active = 1 AND (token_expires_at IS NULL OR token_expires_at > ?) AND id IN ({placeholders})", (workspace_id, now(), *target_ids)).fetchall()
                    if len(selected) != len(set(target_ids)) or {row["provider"] for row in selected} != set(channels):
                        self._json({"error": "Select active accounts for every chosen platform."}, HTTPStatus.BAD_REQUEST); return True
                elif len(channels) != 1:
                    self._json({"error": "Connect an account for each platform when publishing to more than one platform."}, HTTPStatus.BAD_REQUEST); return True
                for url in image_urls:
                    generated = connection.execute("SELECT workspace_id, moderation FROM media_jobs WHERE result_url = ?", (url,)).fetchone()
                    if generated and (generated["workspace_id"] != workspace_id or generated["moderation"] != "approved"):
                        self._json({"error": "Generated media must be approved in this workspace before it can be published."}, HTTPStatus.BAD_REQUEST); return True
                for channel in channels:
                    validate_post(channel, body, image_url, len(image_urls))
                enforce_monthly_quota(connection, workspace_id, "posts_created", "posts_per_month", "posts")
                record_usage(connection, workspace_id, "posts_created")
                post_id = insert_id(connection, "INSERT INTO posts (user_id, workspace_id, body, channel, state, scheduled_for, scheduled_timezone, image_url, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", (session["user_id"], workspace_id, body, channels[0], "scheduled" if schedule else "draft", schedule, schedule_zone if schedule else None, image_url, now()))
                for target_id in dict.fromkeys(target_ids):
                    connection.execute("INSERT INTO post_targets (post_id, connection_id) VALUES (?, ?)", (post_id, target_id))
                for position, url in enumerate(image_urls):
                    connection.execute("INSERT INTO post_media (post_id, media_url, position) VALUES (?, ?, ?)", (post_id, url, position))
                row = dict(connection.execute("SELECT * FROM posts WHERE id = ?", (post_id,)).fetchone())
            row["media_urls"] = image_urls
            audit(session["user_id"], "post.created", "post", post_id, f"Created {'/'.join(channels)} post", self._source_ip(), workspace_id=workspace_id)
            self._json(row, HTTPStatus.CREATED); return True
        if path.startswith("/api/posts/") and path.endswith("/remove"):
            workspace_id = self._require_workspace(session, "editor")
            if workspace_id is None:
                return True
            post_id = int(path.split("/")[3])
            with db() as connection:
                post = connection.execute("SELECT state FROM posts WHERE id = ? AND workspace_id = ?", (post_id, workspace_id)).fetchone()
                if post is None or post["state"] not in {"draft", "scheduled", "failed"}:
                    self._json({"error": "Only this workspace's drafts, scheduled posts, or failed posts can be removed from the queue."}, HTTPStatus.CONFLICT); return True
                connection.execute("DELETE FROM deliveries WHERE post_id = ?", (post_id,))
                connection.execute("DELETE FROM post_media WHERE post_id = ?", (post_id,))
                connection.execute("DELETE FROM post_targets WHERE post_id = ?", (post_id,))
                connection.execute("DELETE FROM posts WHERE id = ? AND workspace_id = ?", (post_id, workspace_id))
            audit(session["user_id"], "post.removed", "post", post_id, "Removed unpublished post from queue", self._source_ip(), workspace_id=workspace_id)
            self._json({"status": "removed"}); return True
        if path.startswith("/api/posts/") and path.endswith("/delete-from-channels"):
            workspace_id = self._require_workspace(session, "editor")
            if workspace_id is None:
                return True
            post_id = int(path.split("/")[3])
            with db() as connection:
                row = connection.execute("SELECT * FROM posts WHERE id = ? AND workspace_id = ? AND state = 'published'", (post_id, workspace_id)).fetchone()
                targets = [dict(item) for item in connection.execute("SELECT post_targets.connection_id, post_targets.external_id, connections.* FROM post_targets JOIN connections ON connections.id = post_targets.connection_id WHERE post_targets.post_id = ? AND post_targets.state = 'published'", (post_id,)).fetchall()]
            if row is None:
                self._json({"error": "Only one of your published posts can be deleted from channels."}, HTTPStatus.NOT_FOUND); return True
            post, deleted, failed = dict(row), [], []
            # Legacy one-provider records do not have a connection target.
            pending = targets or [{"connection_id": None, "external_id": post.get("external_id", "")}]
            for target in pending:
                try:
                    delete_published_content(post, str(target.get("external_id") or ""), target if target.get("connection_id") is not None else None)
                    deleted.append(str(target.get("provider") or post["channel"]))
                    if target.get("connection_id") is not None:
                        with db() as connection:
                            connection.execute("UPDATE post_targets SET state = 'deleted', last_error = NULL WHERE post_id = ? AND connection_id = ?", (post_id, target["connection_id"]))
                except ProviderError as error:
                    failed.append({"provider": str(target.get("provider") or post["channel"]), "error": str(error)})
                    if target.get("connection_id") is not None:
                        with db() as connection:
                            connection.execute("UPDATE post_targets SET last_error = ? WHERE post_id = ? AND connection_id = ?", (str(error)[:500], post_id, target["connection_id"]))
            if deleted and not failed:
                with db() as connection:
                    connection.execute("UPDATE posts SET state = 'deleted', last_error = NULL WHERE id = ?", (post_id,))
            elif failed:
                with db() as connection:
                    connection.execute("UPDATE posts SET last_error = ? WHERE id = ?", ("Some channels could not delete the post.", post_id))
            audit(session["user_id"], "post.remote_delete", "post", post_id, f"Deleted from {len(deleted)} channel(s), failed on {len(failed)}", self._source_ip(), workspace_id=workspace_id)
            self._json({"deleted": deleted, "failed": failed}, HTTPStatus.OK if deleted else HTTPStatus.BAD_GATEWAY); return True
        if path.startswith("/api/posts/") and path.endswith("/schedule"):
            workspace_id = self._require_workspace(session, "editor")
            if workspace_id is None:
                return True
            post_id, schedule_zone = int(path.split("/")[3]), timezone_name(payload.get("scheduled_timezone") or session["timezone"])
            schedule = self._schedule_time(payload.get("scheduled_for", ""), schedule_zone)
            with db() as connection:
                cursor = connection.execute("UPDATE posts SET state = 'scheduled', scheduled_for = ?, scheduled_timezone = ?, attempts = 0, last_error = NULL WHERE id = ? AND workspace_id = ? AND state != 'published'", (schedule, schedule_zone, post_id, workspace_id))
                if cursor.rowcount == 1:
                    connection.execute("UPDATE post_targets SET state = 'pending', last_error = NULL WHERE post_id = ?", (post_id,))
            if cursor.rowcount != 1:
                self._json({"error": "Post not found or already published."}, HTTPStatus.NOT_FOUND); return True
            audit(session["user_id"], "post.scheduled", "post", post_id, f"Scheduled for {schedule}", self._source_ip(), workspace_id=workspace_id)
            self._json({"status": "scheduled"}); return True
        if path.startswith("/api/posts/") and path.endswith("/publish"):
            workspace_id = self._require_workspace(session, "editor")
            if workspace_id is None:
                return True
            post_id = int(path.split("/")[3])
            with db() as connection:
                found = connection.execute("SELECT id FROM posts WHERE id = ? AND workspace_id = ? AND state != 'published'", (post_id, workspace_id)).fetchone()
                if found is None:
                    self._json({"error": "Post not found or already published."}, HTTPStatus.NOT_FOUND); return True
                connection.execute("UPDATE posts SET state = 'scheduled', scheduled_for = ? WHERE id = ? AND workspace_id = ?", (now(), post_id, workspace_id))
            audit(session["user_id"], "post.queued", "post", post_id, "Queued for immediate delivery", self._source_ip(), workspace_id=workspace_id)
            self._json({"status": "queued"}, HTTPStatus.ACCEPTED); return True
        if path.startswith("/api/posts/") and path.endswith("/retry"):
            workspace_id = self._require_workspace(session, "editor")
            if workspace_id is None:
                return True
            post_id = int(path.split("/")[3])
            with db() as connection:
                cursor = connection.execute("UPDATE posts SET state = 'scheduled', scheduled_for = ?, scheduled_timezone = 'UTC', attempts = 0, publishing_started_at = NULL, last_error = NULL WHERE id = ? AND workspace_id = ? AND state = 'failed'", (now(), post_id, workspace_id))
                if cursor.rowcount == 1:
                    connection.execute("UPDATE post_targets SET state = 'pending', last_error = NULL WHERE post_id = ? AND state != 'published'", (post_id,))
            if cursor.rowcount != 1:
                self._json({"error": "Only this workspace's failed posts can be retried."}, HTTPStatus.NOT_FOUND); return True
            audit(session["user_id"], "post.retried", "post", post_id, "Manually retried failed delivery", self._source_ip(), workspace_id=workspace_id)
            self._json({"status": "queued"}, HTTPStatus.ACCEPTED); return True
        return False

