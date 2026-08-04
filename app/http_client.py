"""Outbound HTTP primitives.

Every provider call goes through this module so tests can replace one
function and intercept all outbound traffic."""

from __future__ import annotations

import json
import mimetypes
import uuid
from http import HTTPStatus
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

try:  # package import (tests, `python -m app.server`)
    from .config import MAX_MEDIA_DOWNLOAD_BYTES, RETRY_MAX_SECONDS
    from .errors import ProviderError
except ImportError:  # script import (`python /app/app/server.py`)
    from config import MAX_MEDIA_DOWNLOAD_BYTES, RETRY_MAX_SECONDS
    from errors import ProviderError


def parse_retry_after(value: str) -> int | None:
    """Accept only bounded Retry-After delay seconds from a provider response."""
    try:
        return min(max(int(value), 1), RETRY_MAX_SECONDS)
    except (TypeError, ValueError):
        return None


def request_json(url: str, payload: dict[str, Any], headers: dict[str, str] | None = None) -> dict[str, Any]:
    data = json.dumps(payload).encode()
    request = Request(url, data=data, method="POST", headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urlopen(request, timeout=30) as response:
            return json.loads(response.read() or b"{}")
    except HTTPError as error:
        body = error.read().decode(errors="replace")[:500]
        retry_after = parse_retry_after(error.headers.get("Retry-After", ""))
        retryable = error.code == HTTPStatus.TOO_MANY_REQUESTS or error.code >= HTTPStatus.INTERNAL_SERVER_ERROR
        raise ProviderError(f"Provider rejected the post ({error.code}): {body}", retryable=retryable, retry_after=retry_after) from error
    except URLError as error:
        raise ProviderError(f"Provider could not be reached: {error.reason}", retryable=True) from error


def request_form(url: str, payload: dict[str, str], headers: dict[str, str] | None = None) -> dict[str, Any]:
    request = Request(url, data=urlencode(payload).encode(), method="POST", headers={"Content-Type": "application/x-www-form-urlencoded", **(headers or {})})
    try:
        with urlopen(request, timeout=30) as response:
            return json.loads(response.read() or b"{}")
    except HTTPError as error:
        retry_after = parse_retry_after(error.headers.get("Retry-After", ""))
        retryable = error.code == HTTPStatus.TOO_MANY_REQUESTS or error.code >= HTTPStatus.INTERNAL_SERVER_ERROR
        raise ProviderError(f"Provider rejected the post ({error.code}): {error.read().decode(errors='replace')[:500]}", retryable=retryable, retry_after=retry_after) from error
    except URLError as error:
        raise ProviderError(f"Provider could not be reached: {error.reason}", retryable=True) from error


def request_get_json(url: str, headers: dict[str, str] | None = None) -> dict[str, Any]:
    request = Request(url, headers=headers or {})
    try:
        with urlopen(request, timeout=30) as response:
            result = json.loads(response.read() or b"{}")
    except HTTPError as error:
        raise ProviderError(f"Provider rejected account discovery ({error.code}): {error.read().decode(errors='replace')[:500]}", retryable=error.code == HTTPStatus.TOO_MANY_REQUESTS or error.code >= HTTPStatus.INTERNAL_SERVER_ERROR) from error
    except (URLError, json.JSONDecodeError) as error:
        raise ProviderError("Provider account discovery could not be completed.", retryable=True) from error
    if not isinstance(result, dict):
        raise ProviderError("Provider account discovery returned an invalid response.")
    return result


def request_delete(url: str, headers: dict[str, str] | None = None) -> dict[str, Any]:
    """Delete a remote resource and normalize providers that return no body."""
    request = Request(url, method="DELETE", headers=headers or {})
    try:
        with urlopen(request, timeout=30) as response:
            body = response.read() or b"{}"
            result = json.loads(body)
    except HTTPError as error:
        body = error.read().decode(errors="replace")[:500]
        retry_after = parse_retry_after(error.headers.get("Retry-After", ""))
        retryable = error.code == HTTPStatus.TOO_MANY_REQUESTS or error.code >= HTTPStatus.INTERNAL_SERVER_ERROR
        raise ProviderError(f"Provider rejected deletion ({error.code}): {body}", retryable=retryable, retry_after=retry_after) from error
    except (URLError, json.JSONDecodeError) as error:
        raise ProviderError(f"Provider deletion could not be completed: {error}", retryable=True) from error
    return result if isinstance(result, dict) else {}


def request_get_bytes(url: str, headers: dict[str, str] | None = None) -> bytes:
    """Download one generated media object with a hard size cap."""
    request = Request(url, headers=headers or {})
    try:
        with urlopen(request, timeout=120) as response:
            content = response.read(MAX_MEDIA_DOWNLOAD_BYTES + 1)
    except (HTTPError, URLError) as error:
        raise ProviderError("The generated media could not be downloaded from the provider.") from error
    if not content or len(content) > MAX_MEDIA_DOWNLOAD_BYTES:
        raise ProviderError("The generated media is empty or larger than the download limit.")
    return content


def telegram_request(token: str, method: str, fields: dict[str, str], image: Path | None = None) -> dict[str, Any]:
    url = f"https://api.telegram.org/bot{token}/{method}"
    if image is None:
        response = request_form(url, fields)
    else:
        boundary = f"----sosopo{uuid.uuid4().hex}"
        chunks: list[bytes] = []
        for key, value in fields.items():
            chunks.extend((f"--{boundary}\r\n".encode(), f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode(), value.encode(), b"\r\n"))
        content_type = mimetypes.guess_type(image.name)[0] or "application/octet-stream"
        chunks.extend((f"--{boundary}\r\n".encode(), f'Content-Disposition: form-data; name="photo"; filename="{image.name}"\r\n'.encode(), f"Content-Type: {content_type}\r\n\r\n".encode(), image.read_bytes(), b"\r\n", f"--{boundary}--\r\n".encode()))
        request = Request(url, data=b"".join(chunks), method="POST", headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
        try:
            with urlopen(request, timeout=30) as result:
                response = json.loads(result.read() or b"{}")
        except (HTTPError, URLError) as error:
            raise ProviderError(f"Telegram could not deliver the post: {error}") from error
    if not response.get("ok"):
        raise ProviderError(f"Telegram rejected the post: {response.get('description', 'unknown error')}")
    return response


def request_put_bytes(url: str, content: bytes, headers: dict[str, str] | None = None) -> dict[str, Any]:
    """Upload raw bytes to a provider-issued upload slot.

    Providers that hand out a one-time upload URL (LinkedIn images) expect the
    bytes as the whole request body, with no multipart wrapper.
    """
    request = Request(url, data=content, method="PUT", headers=headers or {})
    try:
        with urlopen(request, timeout=120) as response:
            body = response.read() or b"{}"
    except HTTPError as error:
        raise ProviderError(f"Provider rejected the media upload ({error.code}): {error.read().decode(errors='replace')[:500]}",
                            retryable=error.code == HTTPStatus.TOO_MANY_REQUESTS or error.code >= HTTPStatus.INTERNAL_SERVER_ERROR) from error
    except URLError as error:
        raise ProviderError(f"Provider media upload could not be completed: {error.reason}", retryable=True) from error
    try:
        result = json.loads(body)
    except json.JSONDecodeError:
        return {}
    return result if isinstance(result, dict) else {}
