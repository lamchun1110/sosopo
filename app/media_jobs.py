"""Asynchronous AI image and video generation jobs and their worker loop."""

from __future__ import annotations

import base64
import binascii
import time
import uuid
from typing import Any
from urllib.parse import quote

try:  # package import (tests, `python -m app.server`)
    from . import config as cfg
    from . import http_client
    from .ai_providers import AI_PROVIDER_IMAGE_MODELS, AI_PROVIDER_VIDEO_MODELS, ai_provider_settings
    from .config import IMAGE_TYPES, LOGGER, MEDIA_IMAGE_SIZES, MEDIA_VIDEO_SIZES, POLL_SECONDS, VIDEO_POLL_LIMIT, now
    from .database import Record, db
    from .errors import ProviderError
    from .media_storage import detected_image_type, inspect_image, store_media
    from .credits import refund_ai_credit
    from .plans import enforce_storage_limit, record_usage
except ImportError:  # script import (`python /app/app/server.py`)
    import config as cfg
    import http_client
    from ai_providers import AI_PROVIDER_IMAGE_MODELS, AI_PROVIDER_VIDEO_MODELS, ai_provider_settings
    from config import IMAGE_TYPES, LOGGER, MEDIA_IMAGE_SIZES, MEDIA_VIDEO_SIZES, POLL_SECONDS, VIDEO_POLL_LIMIT, now
    from database import Record, db
    from errors import ProviderError
    from media_storage import detected_image_type, inspect_image, store_media
    from credits import refund_ai_credit
    from plans import enforce_storage_limit, record_usage


def default_media_model(provider: str, kind: str) -> str:
    models = (AI_PROVIDER_IMAGE_MODELS if kind == "image" else AI_PROVIDER_VIDEO_MODELS).get(provider) or []
    return models[0] if models else ""


def media_job_prompt(job: dict[str, Any]) -> str:
    style = str(job.get("style") or "").strip()
    prompt = str(job["prompt"]).strip()
    return f"{prompt}\n\nVisual style: {style}" if style else prompt


def store_generated_media(job: dict[str, Any], content: bytes, content_type: str, suffix: str) -> str:
    with db() as connection:
        enforce_storage_limit(connection, int(job["workspace_id"]), len(content))
        record_usage(connection, int(job["workspace_id"]), "storage_bytes", len(content), period="total")
    return store_media(f"{uuid.uuid4().hex}{suffix}", content_type, content)


def generate_image_media(job: dict[str, Any], settings: dict[str, str]) -> str:
    payload = {"model": job["model"] or default_media_model(job["provider"], "image"), "prompt": media_job_prompt(job), "size": MEDIA_IMAGE_SIZES.get(str(job["aspect_ratio"]), "1024x1024"), "n": 1}
    result = http_client.request_json(f"{settings['base_url']}/images/generations", payload, {"Authorization": f"Bearer {settings['api_key']}"})
    data = result.get("data")
    first = data[0] if isinstance(data, list) and data and isinstance(data[0], dict) else {}
    if first.get("b64_json"):
        try:
            content = base64.b64decode(str(first["b64_json"]), validate=True)
        except (binascii.Error, ValueError) as error:
            raise ProviderError("The AI provider returned unreadable image data.") from error
    elif first.get("url"):
        content = http_client.request_get_bytes(str(first["url"]))
    else:
        raise ProviderError("The AI provider did not return an image.")
    image_type = detected_image_type(content)
    if image_type is None:
        raise ProviderError("The AI provider returned an unsupported image format.")
    inspect_image(content, image_type)
    return store_generated_media(job, content, image_type, IMAGE_TYPES[image_type])


def generate_video_media(job: dict[str, Any], settings: dict[str, str]) -> str:
    """Run one provider-side asynchronous video job (OpenAI-style /videos API)."""
    headers = {"Authorization": f"Bearer {settings['api_key']}"}
    model = job["model"] or default_media_model(job["provider"], "video")
    creation = http_client.request_json(f"{settings['base_url']}/videos", {"model": model, "prompt": media_job_prompt(job), "size": MEDIA_VIDEO_SIZES.get(str(job["aspect_ratio"]), "1280x720")}, headers)
    video_id = str(creation.get("id") or "")
    if not video_id:
        raise ProviderError("The AI provider did not accept the video job.")
    for _ in range(VIDEO_POLL_LIMIT):
        time.sleep(cfg.VIDEO_POLL_SECONDS)
        remote = http_client.request_get_json(f"{settings['base_url']}/videos/{quote(video_id, safe='')}", headers)
        state = str(remote.get("status") or "")
        try:
            progress = min(max(int(remote.get("progress") or 0), 5), 99)
        except (TypeError, ValueError):
            progress = 50
        with db() as connection:
            connection.execute("UPDATE media_jobs SET progress = ?, updated_at = ? WHERE id = ?", (progress, now(), job["id"]))
        if state == "completed":
            content = http_client.request_get_bytes(f"{settings['base_url']}/videos/{quote(video_id, safe='')}/content", headers)
            return store_generated_media(job, content, "video/mp4", ".mp4")
        if state in {"failed", "cancelled", "expired"}:
            detail = remote.get("error", {}).get("message") if isinstance(remote.get("error"), dict) else ""
            raise ProviderError(f"The provider video job {state}: {detail or 'no detail returned'}"[:400])
    raise ProviderError("The provider video job did not finish in time.")


def claim_media_job() -> Record | None:
    with db() as connection:
        row = connection.execute("SELECT id FROM media_jobs WHERE status = 'queued' ORDER BY id LIMIT 1").fetchone()
        if row is None:
            return None
        claimed = connection.execute("UPDATE media_jobs SET status = 'running', progress = 5, updated_at = ? WHERE id = ? AND status = 'queued'", (now(), row["id"]))
        if claimed.rowcount != 1:
            return None
        return connection.execute("SELECT * FROM media_jobs WHERE id = ?", (row["id"],)).fetchone()


def run_media_job(job: dict[str, Any]) -> None:
    """Generate one media asset; on failure record the error and refund the credit."""
    try:
        settings = ai_provider_settings(str(job["provider"]), int(job["workspace_id"]))
        result_url = generate_image_media(job, settings) if job["kind"] == "image" else generate_video_media(job, settings)
    except ProviderError as error:
        detail = str(error)[:500]
    except Exception:
        LOGGER.exception("Media job %s failed unexpectedly", job["id"])
        detail = "The media job failed unexpectedly. Check the worker logs."
    else:
        with db() as connection:
            connection.execute("UPDATE media_jobs SET status = 'succeeded', progress = 100, result_url = ?, error = NULL, updated_at = ? WHERE id = ?", (result_url, now(), job["id"]))
        return
    with db() as connection:
        connection.execute("UPDATE media_jobs SET status = 'failed', error = ?, updated_at = ? WHERE id = ?", (detail, now(), job["id"]))
        record_usage(connection, int(job["workspace_id"]), "ai_media", -1)
        refund_ai_credit(connection, int(job["workspace_id"]), "ai_media_refund", job.get("user_id"))


def media_worker() -> None:
    """Process queued media jobs without blocking scheduled post delivery."""
    while True:
        try:
            job = claim_media_job()
            if job is not None:
                run_media_job(dict(job))
                continue
        except Exception:
            LOGGER.exception("Media job poll failed")
        time.sleep(POLL_SECONDS)
