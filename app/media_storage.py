"""Image inspection plus local-disk or S3-compatible media storage."""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from PIL import Image, UnidentifiedImageError

try:  # package import (tests, `python -m app.server`)
    from . import config as cfg
    from .config import MAX_IMAGE_PIXELS, config, public_url
    from .database import db
    from .errors import ProviderError
except ImportError:  # script import (`python /app/app/server.py`)
    import config as cfg
    from config import MAX_IMAGE_PIXELS, config, public_url
    from database import db
    from errors import ProviderError


def detected_image_type(content: bytes) -> str | None:
    """Recognize only the four image encodings accepted by the upload API."""
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if content.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if content.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if len(content) >= 12 and content.startswith(b"RIFF") and content[8:12] == b"WEBP":
        return "image/webp"
    return None


def inspect_image(content: bytes, declared_type: str) -> tuple[int, int]:
    """Decode uploaded media before persisting it, avoiding disguised/corrupt images."""
    Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS
    try:
        with Image.open(io.BytesIO(content)) as image:
            image.verify()
        with Image.open(io.BytesIO(content)) as image:
            width, height = image.size
            actual = Image.MIME.get(image.format or "")
    except (Image.DecompressionBombError, UnidentifiedImageError, OSError, ValueError) as error:
        raise ValueError("Image content is corrupt, unsafe, or cannot be decoded.") from error
    if actual != declared_type or width < 1 or height < 1:
        raise ValueError("Image content does not match the declared media type.")
    return width, height


def public_image_url(image_url: str) -> str:
    if image_url.startswith(("https://", "http://")):
        return image_url
    base_url = public_url()
    if not base_url.startswith("https://"):
        raise ProviderError("Image publishing needs SOSOPO_PUBLIC_URL set to a public HTTPS URL.")
    return f"{base_url}{image_url}"


def storage_backend() -> str:
    backend = config("SOSOPO_STORAGE_BACKEND") or "local"
    if backend not in {"local", "s3"}:
        raise ProviderError("SOSOPO_STORAGE_BACKEND must be local or s3.")
    return backend


def media_key(filename: str) -> str:
    return f"{config('S3_MEDIA_PREFIX').strip('/') or 'uploads'}/{filename}"


def media_client() -> Any:
    import boto3
    return boto3.client("s3", endpoint_url=config("S3_ENDPOINT_URL") or None, aws_access_key_id=config("AWS_ACCESS_KEY_ID") or None, aws_secret_access_key=config("AWS_SECRET_ACCESS_KEY") or None)


def media_url(filename: str) -> str:
    if storage_backend() == "local":
        return f"/uploads/{filename}"
    base = config("SOSOPO_MEDIA_PUBLIC_URL").rstrip("/")
    if not base.startswith("https://"):
        raise ProviderError("S3 media storage requires SOSOPO_MEDIA_PUBLIC_URL with a public HTTPS URL.")
    return f"{base}/{media_key(filename)}"


def store_media(filename: str, content_type: str, content: bytes) -> str:
    if storage_backend() == "local":
        (cfg.UPLOADS_DIR / filename).write_bytes(content)
    else:
        bucket = config("S3_MEDIA_BUCKET")
        if not bucket:
            raise ProviderError("S3 media storage requires S3_MEDIA_BUCKET.")
        media_client().put_object(Bucket=bucket, Key=media_key(filename), Body=content, ContentType=content_type)
    return media_url(filename)


def media_exists(image_url: str) -> bool:
    if storage_backend() == "local":
        return image_url.startswith("/uploads/") and (cfg.UPLOADS_DIR / Path(image_url).name).is_file()
    return image_url.startswith(config("SOSOPO_MEDIA_PUBLIC_URL").rstrip("/") + "/")


def media_bytes(image_url: str) -> bytes:
    if storage_backend() == "local":
        return (cfg.UPLOADS_DIR / Path(image_url).name).read_bytes()
    bucket = config("S3_MEDIA_BUCKET")
    if not bucket:
        raise ProviderError("S3 media storage requires S3_MEDIA_BUCKET.")
    return media_client().get_object(Bucket=bucket, Key=media_key(Path(urlparse(image_url).path).name))["Body"].read()


def post_media_items(post: dict[str, Any]) -> list[dict[str, str]]:
    """Ordered attachments with their alt text.

    Accepts a pre-resolved ``media_items`` list so a caller that already has
    the rows does not re-read them, and falls back to the old single-image
    ``image_url`` shape for posts created before multi-image support.
    """
    items = post.get("media_items")
    if isinstance(items, list):
        return [{"url": str(item.get("url", "")), "alt_text": str(item.get("alt_text") or "")} for item in items]
    urls = post.get("media_urls")
    if isinstance(urls, list):
        return [{"url": str(url), "alt_text": ""} for url in urls]
    if "id" not in post:
        return [{"url": post["image_url"], "alt_text": ""}] if post.get("image_url") else []
    with db() as connection:
        rows = connection.execute("SELECT media_url, alt_text FROM post_media WHERE post_id = ? ORDER BY position", (post["id"],)).fetchall()
    resolved = [{"url": row["media_url"], "alt_text": str(row["alt_text"] or "")} for row in rows]
    return resolved or ([{"url": post["image_url"], "alt_text": ""}] if post.get("image_url") else [])


def post_media_urls(post: dict[str, Any]) -> list[str]:
    """Return ordered attachments while retaining compatibility with old single-image posts."""
    urls = post.get("media_urls")
    if isinstance(urls, list):
        return [str(url) for url in urls]
    if "id" not in post:
        return [post["image_url"]] if post.get("image_url") else []
    with db() as connection:
        rows = connection.execute("SELECT media_url FROM post_media WHERE post_id = ? ORDER BY position", (post["id"],)).fetchall()
    return [row["media_url"] for row in rows] or ([post["image_url"]] if post.get("image_url") else [])
