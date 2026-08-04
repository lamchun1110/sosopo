"""Uploads plus the AI media studio: jobs, moderation, and the library."""


from __future__ import annotations


import base64
import binascii
import uuid
from http import HTTPStatus
from typing import Any

try:  # package import (tests, `python -m app.server`)
    from ..ai_providers import ai_provider_settings, available_ai_providers
    from ..audit import audit
    from ..config import IMAGE_TYPES, MAX_MEDIA_PROMPT_LENGTH, MAX_MEDIA_STYLE_LENGTH, MAX_UPLOAD_BYTES, MEDIA_IMAGE_SIZES, MEDIA_JOB_KINDS, MEDIA_VIDEO_SIZES, now
    from ..database import Record, db, insert_id
    from ..errors import ProviderError
    from ..media_jobs import default_media_model
    from ..media_storage import detected_image_type, inspect_image, store_media
    from ..credits import charge_ai_credit
    from ..plans import enforce_monthly_quota, enforce_storage_limit, record_usage
    from ..workspaces import workspace_role_allows
except ImportError:  # script import (`python /app/app/server.py`)
    from ai_providers import ai_provider_settings, available_ai_providers
    from audit import audit
    from config import IMAGE_TYPES, MAX_MEDIA_PROMPT_LENGTH, MAX_MEDIA_STYLE_LENGTH, MAX_UPLOAD_BYTES, MEDIA_IMAGE_SIZES, MEDIA_JOB_KINDS, MEDIA_VIDEO_SIZES, now
    from database import Record, db, insert_id
    from errors import ProviderError
    from media_jobs import default_media_model
    from media_storage import detected_image_type, inspect_image, store_media
    from credits import charge_ai_credit
    from plans import enforce_monthly_quota, enforce_storage_limit, record_usage
    from workspaces import workspace_role_allows


class MediaRoutes:
    """Uploads plus the AI media studio: jobs, moderation, and the library.

    Mixed into ``Handler``; every method returns True once it has answered.
    """



    def get_media(self, path: str) -> bool:
        """Handle one media GET; True when answered."""
        if path == "/api/media/jobs":
            session = self._session()
            workspace_id = self._require_workspace(session)
            if workspace_id is None:
                return True
            reviewer = workspace_role_allows(session["workspace_role"], "admin")
            with db() as connection:
                jobs = [dict(row) for row in connection.execute(
                    "SELECT media_jobs.id, media_jobs.kind, media_jobs.prompt, media_jobs.aspect_ratio, media_jobs.style, media_jobs.provider, media_jobs.model, media_jobs.status, media_jobs.progress, media_jobs.error, media_jobs.result_url, media_jobs.moderation, media_jobs.created_at, users.username AS created_by"
                    " FROM media_jobs JOIN users ON users.id = media_jobs.user_id WHERE media_jobs.workspace_id = ? ORDER BY media_jobs.id DESC LIMIT 100",
                    (workspace_id,),
                ).fetchall()]
            for job in jobs:
                if job["moderation"] != "approved" and not reviewer:
                    job["result_url"] = None
            self._json({"jobs": jobs})
            return True
        if path == "/api/media/library":
            session = self._session()
            workspace_id = self._require_workspace(session)
            if workspace_id is None:
                return True
            with db() as connection:
                assets = [dict(row) for row in connection.execute(
                    "SELECT id, kind, prompt, aspect_ratio, result_url, created_at FROM media_jobs WHERE workspace_id = ? AND status = 'succeeded' AND moderation = 'approved' ORDER BY id DESC LIMIT 200",
                    (workspace_id,),
                ).fetchall()]
            self._json({"assets": assets})
            return True
        return False

    def post_media(self, path: str, payload: dict[str, Any], session: Record) -> bool:
        """Handle one media POST; True when answered."""
        if path == "/api/media/jobs":
            workspace_id = self._require_workspace(session, "editor")
            if workspace_id is None:
                return True
            kind = str(payload.get("kind", "image")).strip()
            prompt = str(payload.get("prompt", "")).strip()
            aspect = str(payload.get("aspect_ratio", "1:1")).strip()
            style = str(payload.get("style", "")).strip()
            provider = str(payload.get("provider", "")).strip()
            model = str(payload.get("model", "")).strip()
            if kind not in MEDIA_JOB_KINDS:
                self._json({"error": "Choose image or video generation."}, HTTPStatus.BAD_REQUEST); return True
            if not prompt or len(prompt) > MAX_MEDIA_PROMPT_LENGTH or len(style) > MAX_MEDIA_STYLE_LENGTH or len(model) > 200:
                self._json({"error": f"Provide a prompt up to {MAX_MEDIA_PROMPT_LENGTH} characters and a style up to {MAX_MEDIA_STYLE_LENGTH}."}, HTTPStatus.BAD_REQUEST); return True
            sizes = MEDIA_IMAGE_SIZES if kind == "image" else MEDIA_VIDEO_SIZES
            if aspect not in sizes:
                self._json({"error": f"Choose an aspect ratio from: {', '.join(sizes)}."}, HTTPStatus.BAD_REQUEST); return True
            if not provider:
                configured = available_ai_providers(workspace_id)
                provider = configured[0]["name"] if configured else ""
            try:
                ai_provider_settings(provider, workspace_id)
            except ProviderError as error:
                self._json({"error": str(error)}, HTTPStatus.BAD_REQUEST); return True
            if not model and not default_media_model(provider, kind):
                self._json({"error": f"{provider} has no supported {kind} model in Sosopo. Choose another provider or set a model explicitly."}, HTTPStatus.BAD_REQUEST); return True
            with db() as connection:
                enforce_monthly_quota(connection, workspace_id, "ai_media", "ai_media_per_month", "AI media generations")
                charge_ai_credit(connection, workspace_id, "ai_media", session["user_id"])
                record_usage(connection, workspace_id, "ai_media")
                job_id = insert_id(connection,
                    "INSERT INTO media_jobs (workspace_id, user_id, kind, prompt, aspect_ratio, style, provider, model, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?)",
                    (workspace_id, session["user_id"], kind, prompt, aspect, style, provider, model, now(), now()),
                )
            audit(session["user_id"], "media.job_created", "media_job", job_id, f"Queued {kind} generation with {provider}", self._source_ip(), workspace_id=workspace_id)
            self._json({"id": job_id, "status": "queued", "provider": provider}, HTTPStatus.CREATED); return True
        if path.startswith("/api/media/jobs/") and path.endswith("/review"):
            workspace_id = self._require_workspace(session, "admin")
            if workspace_id is None:
                return True
            job_id = int(path.split("/")[4])
            decision = str(payload.get("decision", "")).strip()
            if decision not in {"approved", "rejected"}:
                self._json({"error": "Choose approved or rejected."}, HTTPStatus.BAD_REQUEST); return True
            with db() as connection:
                changed = connection.execute("UPDATE media_jobs SET moderation = ?, reviewed_by = ?, updated_at = ? WHERE id = ? AND workspace_id = ? AND status = 'succeeded'", (decision, session["user_id"], now(), job_id, workspace_id))
            if changed.rowcount != 1:
                self._json({"error": "Only this workspace's finished media jobs can be reviewed."}, HTTPStatus.NOT_FOUND); return True
            audit(session["user_id"], "media.reviewed", "media_job", job_id, f"Marked generated media {decision}", self._source_ip(), workspace_id=workspace_id)
            self._json({"id": job_id, "moderation": decision}); return True
        if path == "/api/uploads":
            workspace_id = self._require_workspace(session, "editor")
            if workspace_id is None:
                return True
            content_type, encoded = str(payload.get("content_type", "")).lower(), payload.get("data", "")
            if content_type not in IMAGE_TYPES or not isinstance(encoded, str):
                self._json({"error": "Upload a PNG, JPEG, GIF, or WebP image."}, HTTPStatus.BAD_REQUEST); return True
            try:
                image = base64.b64decode(encoded, validate=True)
            except (binascii.Error, ValueError):
                self._json({"error": "Invalid image data."}, HTTPStatus.BAD_REQUEST); return True
            if not image or len(image) > MAX_UPLOAD_BYTES:
                self._json({"error": "Images must be between 1 byte and 5 MB."}, HTTPStatus.BAD_REQUEST); return True
            actual_type = detected_image_type(image)
            if actual_type != content_type:
                self._json({"error": "Image bytes do not match the declared content type."}, HTTPStatus.BAD_REQUEST); return True
            inspect_image(image, actual_type)
            with db() as connection:
                enforce_storage_limit(connection, workspace_id, len(image))
                record_usage(connection, workspace_id, "storage_bytes", len(image), period="total")
            filename = f"{uuid.uuid4().hex}{IMAGE_TYPES[actual_type]}"
            self._json({"url": store_media(filename, actual_type, image)}, HTTPStatus.CREATED); return True
        return False

