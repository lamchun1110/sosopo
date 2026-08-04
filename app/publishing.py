"""Channel publish and delete adapters plus the scheduled-delivery worker."""

from __future__ import annotations

import base64
import json
import mimetypes
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode, urlparse

try:  # package import (tests, `python -m app.server`)
    from . import config as cfg
    from . import http_client
    from .audit import cleanup_expired_records
    from .config import CHANNELS, MAX_ALT_TEXT_LENGTH, CHANNEL_CHARACTER_LIMITS, CHANNEL_MEDIA_LIMITS, LOGGER, MAX_ATTEMPTS, POLL_SECONDS, PUBLISHING_LEASE_SECONDS, RETRY_BASE_SECONDS, RETRY_MAX_SECONDS, TOKEN_REFRESH_INTERVAL_SECONDS, WORKER_HEARTBEAT_SECONDS, config, now
    from .connections import refresh_expiring_connection_tokens, token_is_expired
    from .database import db
    from .errors import ProviderError
    from .media_jobs import media_worker
    from .media_storage import media_bytes, post_media_items, public_image_url, storage_backend
    from .security import decrypt_secrets
except ImportError:  # script import (`python /app/app/server.py`)
    import config as cfg
    import http_client
    from audit import cleanup_expired_records
    from config import CHANNELS, MAX_ALT_TEXT_LENGTH, CHANNEL_CHARACTER_LIMITS, CHANNEL_MEDIA_LIMITS, LOGGER, MAX_ATTEMPTS, POLL_SECONDS, PUBLISHING_LEASE_SECONDS, RETRY_BASE_SECONDS, RETRY_MAX_SECONDS, TOKEN_REFRESH_INTERVAL_SECONDS, WORKER_HEARTBEAT_SECONDS, config, now
    from connections import refresh_expiring_connection_tokens, token_is_expired
    from database import db
    from errors import ProviderError
    from media_jobs import media_worker
    from media_storage import media_bytes, post_media_items, public_image_url, storage_backend
    from security import decrypt_secrets


def validate_post(channel: str, body: str, image_url: str | None, image_count: int = 0) -> None:
    if channel not in CHANNELS:
        raise ValueError("Choose a supported provider.")
    if len(body) > CHANNEL_CHARACTER_LIMITS[channel]:
        raise ValueError(f"{channel} posts must be {CHANNEL_CHARACTER_LIMITS[channel]} characters or fewer.")
    if channel == "Instagram" and not image_url:
        raise ValueError("Instagram publishing requires an image.")
    if image_count > CHANNEL_MEDIA_LIMITS[channel]:
        raise ValueError(f"{channel} supports up to {CHANNEL_MEDIA_LIMITS[channel]} images per post.")


def provider_status(channel: str) -> str:
    required = {
        "Facebook": ("FACEBOOK_PAGE_ID", "FACEBOOK_PAGE_ACCESS_TOKEN"),
        "Instagram": ("INSTAGRAM_ACCOUNT_ID", "INSTAGRAM_ACCESS_TOKEN"),
        "Threads": ("THREADS_USER_ID", "THREADS_ACCESS_TOKEN"),
        "X": ("X_ACCESS_TOKEN",),
        "Telegram": ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"),
        "Discord": ("DISCORD_WEBHOOK_URL",),
        "LinkedIn": ("LINKEDIN_AUTHOR_URN", "LINKEDIN_ACCESS_TOKEN", "LINKEDIN_API_VERSION"),
    }[channel]
    return "ready" if all(config(item) for item in required) else "needs configuration"


def publish(post: dict[str, Any], account: dict[str, Any] | None = None) -> str:
    channel = str(account.get("provider") or post["channel"]) if account else post["channel"]
    body, media_items = post["body"], post_media_items(post)
    image_urls = [item["url"] for item in media_items]
    alt_texts = {item["url"]: item["alt_text"] for item in media_items}
    image_url = image_urls[0] if image_urls else None
    if account and token_is_expired(account.get("token_expires_at")):
        raise ProviderError("This provider account token has expired. Reconnect or rotate it before publishing.")
    secrets_for_account = decrypt_secrets(account["encrypted_secrets"]) if account else {}
    account_id = str(account["external_account_id"]) if account else ""
    def credential(name: str, environment: str) -> str:
        return secrets_for_account.get(name, "") or config(environment)
    if channel == "Discord":
        webhook_url = credential("webhook_url", "DISCORD_WEBHOOK_URL")
        if not webhook_url.startswith("https://discord.com/api/webhooks/") and not webhook_url.startswith("https://discordapp.com/api/webhooks/"):
            raise ProviderError("Discord needs a valid incoming webhook URL.", retryable=False)
        embeds = [{"image": {"url": public_image_url(url)}} for url in image_urls]
        result = http_client.request_json(f"{webhook_url}?wait=true", {"content": body, "embeds": embeds, "allowed_mentions": {"parse": []}})
        return str(result.get("id") or "")
    if channel == "LinkedIn":
        author, token = account_id or credential("author_urn", "LINKEDIN_AUTHOR_URN"), credential("access_token", "LINKEDIN_ACCESS_TOKEN")
        version = config("LINKEDIN_API_VERSION")
        if not author or not token or not version:
            raise ProviderError("LinkedIn needs LINKEDIN_AUTHOR_URN, LINKEDIN_ACCESS_TOKEN, and LINKEDIN_API_VERSION.")
        if not author.startswith("urn:li:"):
            raise ProviderError("LinkedIn author must be a member or organization URN.", retryable=False)
        headers = {"Authorization": f"Bearer {token}", "LinkedIn-Version": version, "X-Restli-Protocol-Version": "2.0.0"}
        # LinkedIn member images are a three-step flow: ask for an upload slot,
        # PUT the bytes to the returned URL, then reference the image URN.
        images = []
        for url in image_urls:
            slot = http_client.request_json("https://api.linkedin.com/rest/images?action=initializeUpload",
                                            {"initializeUploadRequest": {"owner": author}}, headers).get("value", {})
            upload_url, image_urn = str(slot.get("uploadUrl") or ""), str(slot.get("image") or "")
            if not upload_url or not image_urn:
                raise ProviderError("LinkedIn did not return an image upload slot.")
            http_client.request_put_bytes(upload_url, media_bytes(url), {"Authorization": f"Bearer {token}"})
            entry = {"id": image_urn}
            if alt_texts.get(url):
                entry["altText"] = alt_texts[url][:MAX_ALT_TEXT_LENGTH]
            images.append(entry)
        payload = {
            "author": author,
            "commentary": body,
            "visibility": "PUBLIC",
            "distribution": {"feedDistribution": "MAIN_FEED", "targetEntities": [], "thirdPartyDistributionChannels": []},
            "lifecycleState": "PUBLISHED",
            "isReshareDisabledByAuthor": False,
        }
        if len(images) == 1:
            payload["content"] = {"media": images[0]}
        elif images:
            payload["content"] = {"multiImage": {"images": images}}
        result = http_client.request_json("https://api.linkedin.com/rest/posts", payload, headers)
        return str(result.get("id") or "linkedin-posted")
    if channel == "Telegram":
        token, chat_id = credential("bot_token", "TELEGRAM_BOT_TOKEN"), account_id or credential("chat_id", "TELEGRAM_CHAT_ID")
        if not token or not chat_id:
            raise ProviderError("Telegram needs TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID.")
        if not image_urls:
            result = http_client.telegram_request(token, "sendMessage", {"chat_id": chat_id, "text": body})
            return str(result["result"]["message_id"])
        message_ids: list[str] = []
        for index, url in enumerate(image_urls):
            fields = {"chat_id": chat_id, "caption": body if index == 0 else ""}
            image = cfg.UPLOADS_DIR / Path(url).name if storage_backend() == "local" else None
            if storage_backend() == "s3":
                fields["photo"] = public_image_url(url)
            result = http_client.telegram_request(token, "sendPhoto", fields, image)
            message_ids.append(str(result["result"]["message_id"]))
        return ",".join(message_ids)
    if channel == "X":
        token = credential("access_token", "X_ACCESS_TOKEN")
        if not token:
            raise ProviderError("X needs X_ACCESS_TOKEN with post.write permission.")
        media_ids: list[str] = []
        for url in image_urls:
            image_name = Path(urlparse(url).path).name
            result = http_client.request_json("https://api.x.com/2/media/upload", {"media": base64.b64encode(media_bytes(url)).decode(), "media_category": "tweet_image", "media_type": mimetypes.guess_type(image_name)[0] or "image/png"}, {"Authorization": f"Bearer {token}"})
            media_ids.append(str(result.get("data", {}).get("id") or result.get("data", {}).get("media_id") or ""))
            if not media_ids[-1]:
                raise ProviderError("X did not return a media ID.")
            if alt_texts.get(url):
                http_client.request_json("https://api.x.com/2/media/metadata",
                                         {"id": media_ids[-1], "alt_text": {"text": alt_texts[url][:MAX_ALT_TEXT_LENGTH]}},
                                         {"Authorization": f"Bearer {token}"})
        result = http_client.request_json("https://api.x.com/2/tweets", {"text": body, **({"media": {"media_ids": media_ids}} if media_ids else {})}, {"Authorization": f"Bearer {token}"})
        return str(result.get("data", {}).get("id") or "")
    if channel == "Facebook":
        page_id, token = account_id or credential("page_id", "FACEBOOK_PAGE_ID"), credential("access_token", "FACEBOOK_PAGE_ACCESS_TOKEN")
        if not page_id or not token:
            raise ProviderError("Facebook needs FACEBOOK_PAGE_ID and FACEBOOK_PAGE_ACCESS_TOKEN.")
        base = config('META_GRAPH_BASE_URL') or 'https://graph.facebook.com/v24.0'
        if len(image_urls) > 1:
            fields = {"access_token": token, "message": body}
            for index, url in enumerate(image_urls):
                photo = http_client.request_form(f"{base}/{page_id}/photos", {"access_token": token, "url": public_image_url(url), "published": "false", **({"alt_text_custom": alt_texts[url][:MAX_ALT_TEXT_LENGTH]} if alt_texts.get(url) else {})})
                media_id = str(photo.get("id") or "")
                if not media_id:
                    raise ProviderError("Facebook did not upload a carousel image.")
                fields[f"attached_media[{index}]"] = json.dumps({"media_fbid": media_id})
            result = http_client.request_form(f"{base}/{page_id}/feed", fields)
        else:
            endpoint = f"{base}/{page_id}/{'photos' if image_url else 'feed'}"
            fields = {"access_token": token, "caption" if image_url else "message": body}
            if image_url:
                fields["url"] = public_image_url(image_url)
                if alt_texts.get(image_url):
                    fields["alt_text_custom"] = alt_texts[image_url][:MAX_ALT_TEXT_LENGTH]
            result = http_client.request_form(endpoint, fields)
        return str(result.get("post_id") or result.get("id") or "")
    if channel == "Instagram":
        target_id, token = account_id or credential("account_id", "INSTAGRAM_ACCOUNT_ID"), credential("access_token", "INSTAGRAM_ACCESS_TOKEN")
        if not target_id or not token:
            raise ProviderError("Instagram needs INSTAGRAM_ACCOUNT_ID and INSTAGRAM_ACCESS_TOKEN.")
        if not image_url:
            raise ProviderError("Instagram publishing requires an image in this first release.")
        base = config("META_GRAPH_BASE_URL") or "https://graph.facebook.com/v24.0"
        if len(image_urls) > 1:
            children: list[str] = []
            for url in image_urls:
                child = http_client.request_form(f"{base}/{target_id}/media", {"access_token": token, "image_url": public_image_url(url), "is_carousel_item": "true"})
                if not child.get("id"):
                    raise ProviderError("Instagram did not create a carousel item.")
                children.append(str(child["id"]))
            container = http_client.request_form(f"{base}/{target_id}/media", {"access_token": token, "media_type": "CAROUSEL", "children": ",".join(children), "caption": body})
        else:
            container = http_client.request_form(f"{base}/{target_id}/media", {"access_token": token, "image_url": public_image_url(image_url), "caption": body})
        creation_id = str(container.get("id") or "")
        if not creation_id:
            raise ProviderError("Instagram did not create a media container.")
        result = http_client.request_form(f"{base}/{target_id}/media_publish", {"access_token": token, "creation_id": creation_id})
        return str(result.get("id") or "")
    if channel == "Threads":
        user_id, token = account_id or credential("user_id", "THREADS_USER_ID"), credential("access_token", "THREADS_ACCESS_TOKEN")
        if not user_id or not token:
            raise ProviderError("Threads needs THREADS_USER_ID and THREADS_ACCESS_TOKEN.")
        base = config("THREADS_API_BASE_URL") or "https://graph.threads.net/v1.0"
        if len(image_urls) > 1:
            children: list[str] = []
            for url in image_urls:
                child = http_client.request_form(f"{base}/{user_id}/threads", {"access_token": token, "media_type": "IMAGE", "image_url": public_image_url(url), "is_carousel_item": "true"})
                if not child.get("id"):
                    raise ProviderError("Threads did not create a carousel item.")
                children.append(str(child["id"]))
            fields = {"access_token": token, "media_type": "CAROUSEL", "children": ",".join(children), "text": body}
        else:
            fields = {"access_token": token, "media_type": "IMAGE" if image_url else "TEXT", "text": body}
            if image_url:
                fields["image_url"] = public_image_url(image_url)
        container = http_client.request_form(f"{base}/{user_id}/threads", fields)
        creation_id = str(container.get("id") or "")
        if not creation_id:
            raise ProviderError("Threads did not create a media container.")
        result = http_client.request_form(f"{base}/{user_id}/threads_publish", {"access_token": token, "creation_id": creation_id})
        return str(result.get("id") or "")
    raise ProviderError("Unsupported provider.")


def delete_published_content(post: dict[str, Any], external_id: str, account: dict[str, Any] | None = None) -> None:
    """Delete one delivered item using the credential that originally published it."""
    channel = str(account.get("provider") or post["channel"]) if account else post["channel"]
    if not external_id:
        raise ProviderError("This delivery has no remote post ID and cannot be deleted.", retryable=False)
    secrets_for_account = decrypt_secrets(account["encrypted_secrets"]) if account else {}
    account_id = str(account["external_account_id"]) if account else ""
    def credential(name: str, environment: str) -> str:
        return secrets_for_account.get(name, "") or config(environment)
    if channel == "Discord":
        webhook_url = credential("webhook_url", "DISCORD_WEBHOOK_URL")
        http_client.request_delete(f"{webhook_url.rstrip('/')}/messages/{quote(external_id, safe='')}")
        return
    if channel == "Telegram":
        token, chat_id = credential("bot_token", "TELEGRAM_BOT_TOKEN"), account_id or credential("chat_id", "TELEGRAM_CHAT_ID")
        for message_id in external_id.split(","):
            http_client.telegram_request(token, "deleteMessage", {"chat_id": chat_id, "message_id": message_id})
        return
    if channel == "X":
        token = credential("access_token", "X_ACCESS_TOKEN")
        http_client.request_delete(f"https://api.x.com/2/tweets/{quote(external_id, safe='')}", {"Authorization": f"Bearer {token}"})
        return
    if channel == "LinkedIn":
        token, version = credential("access_token", "LINKEDIN_ACCESS_TOKEN"), config("LINKEDIN_API_VERSION")
        http_client.request_delete(f"https://api.linkedin.com/rest/posts/{quote(external_id, safe='')}", {"Authorization": f"Bearer {token}", "LinkedIn-Version": version, "X-Restli-Protocol-Version": "2.0.0"})
        return
    if channel in {"Facebook", "Instagram"}:
        token = credential("access_token", "FACEBOOK_PAGE_ACCESS_TOKEN" if channel == "Facebook" else "INSTAGRAM_ACCESS_TOKEN")
        base = config("META_GRAPH_BASE_URL") or "https://graph.facebook.com/v24.0"
        http_client.request_delete(f"{base}/{quote(external_id, safe='')}?{urlencode({'access_token': token})}")
        return
    if channel == "Threads":
        token = credential("access_token", "THREADS_ACCESS_TOKEN")
        base = config("THREADS_API_BASE_URL") or "https://graph.threads.net/v1.0"
        http_client.request_delete(f"{base}/{quote(external_id, safe='')}?{urlencode({'access_token': token})}")
        return
    raise ProviderError("This provider does not support deleting delivered content.", retryable=False)


def claim_post(post_id: int) -> bool:
    """Take exclusive ownership of one scheduled post. True when this worker won.

    The conditional UPDATE is already atomic on every backend, so a second
    worker sees rowcount 0. On PostgreSQL the row is additionally locked with
    SKIP LOCKED first, so concurrent workers step over a contended row instead
    of blocking on it — which is what makes running more than one worker
    worthwhile. See README: scale workers only on PostgreSQL.
    """
    with db() as connection:
        if connection.kind == "postgres":
            locked = connection.execute("SELECT id FROM posts WHERE id = ? AND state = 'scheduled' FOR UPDATE SKIP LOCKED", (post_id,)).fetchone()
            if locked is None:
                return False
        result = connection.execute("UPDATE posts SET state = 'publishing', publishing_started_at = ?, attempts = attempts + 1, last_error = NULL WHERE id = ? AND state = 'scheduled'", (now(), post_id))
    return result.rowcount == 1


def deliver(post_id: int) -> None:
    with db() as connection:
        row = connection.execute("SELECT * FROM posts WHERE id = ? AND state = 'publishing'", (post_id,)).fetchone()
    if row is None:
        return
    post = dict(row)
    with db() as connection:
        targets = [dict(target) for target in connection.execute(
            "SELECT post_targets.connection_id, connections.* FROM post_targets JOIN connections ON connections.id = post_targets.connection_id WHERE post_targets.post_id = ? AND post_targets.state != 'published'",
            (post_id,),
        ).fetchall()]
    if not targets:
        targets = [None]
    failures: list[ProviderError] = []
    delivered: list[str] = []
    for target in targets:
        try:
            if target and not target.get("is_active"):
                raise ProviderError("This provider account has been disabled.")
            external_id = publish(post, target)
            delivered.append(external_id)
            if target:
                with db() as connection:
                    connection.execute("UPDATE post_targets SET state = 'published', external_id = ?, last_error = NULL WHERE post_id = ? AND connection_id = ?", (external_id, post_id, target["connection_id"]))
        except ProviderError as error:
            failures.append(error)
            if target:
                with db() as connection:
                    connection.execute("UPDATE post_targets SET state = 'failed', last_error = ? WHERE post_id = ? AND connection_id = ?", (str(error)[:500], post_id, target["connection_id"]))
    if failures:
        with db() as connection:
            attempts = connection.execute("SELECT attempts FROM posts WHERE id = ?", (post_id,)).fetchone()[0]
            retryable = any(error.retryable for error in failures)
            state = "failed" if attempts >= MAX_ATTEMPTS or not retryable else "scheduled"
            detail = "; ".join(str(error) for error in failures)[:500]
            retry_delay = max([RETRY_BASE_SECONDS * (2 ** max(attempts - 1, 0)), *(error.retry_after or 0 for error in failures)])
            retry_at = (datetime.now(UTC) + timedelta(seconds=min(retry_delay, RETRY_MAX_SECONDS))).isoformat()
            connection.execute("UPDATE posts SET state = ?, publishing_started_at = NULL, scheduled_for = CASE WHEN ? = 'scheduled' THEN ? ELSE scheduled_for END, last_error = ? WHERE id = ?", (state, state, retry_at, detail, post_id))
            connection.execute("INSERT INTO deliveries (post_id, provider, status, detail, created_at) VALUES (?, ?, 'failed', ?, ?)", (post_id, post["channel"], detail, now()))
        return
    with db() as connection:
        external_id = ",".join(delivered)
        connection.execute("UPDATE posts SET state = 'published', publishing_started_at = NULL, published_at = ?, external_id = ?, last_error = NULL WHERE id = ?", (now(), external_id, post_id))
        connection.execute("INSERT INTO deliveries (post_id, provider, status, detail, created_at) VALUES (?, ?, 'published', ?, ?)", (post_id, post["channel"], external_id, now()))


def worker_heartbeat() -> None:
    with db() as connection:
        connection.execute("DELETE FROM worker_heartbeats WHERE name = 'delivery'")
        connection.execute("INSERT INTO worker_heartbeats (name, checked_at) VALUES ('delivery', ?)", (now(),))


def worker_healthy() -> bool:
    try:
        with db() as connection:
            row = connection.execute("SELECT checked_at FROM worker_heartbeats WHERE name = 'delivery'").fetchone()
        return row is not None and datetime.fromisoformat(row["checked_at"]) >= datetime.now(UTC) - timedelta(seconds=WORKER_HEARTBEAT_SECONDS)
    except Exception:
        return False


def recover_stale_deliveries() -> int:
    stale_before = (datetime.now(UTC) - timedelta(seconds=PUBLISHING_LEASE_SECONDS)).isoformat()
    with db() as connection:
        result = connection.execute("UPDATE posts SET state = 'scheduled', scheduled_for = ?, publishing_started_at = NULL, last_error = 'Delivery worker lease expired; retrying.' WHERE state = 'publishing' AND publishing_started_at < ?", (now(), stale_before))
    return result.rowcount


def scheduler() -> None:
    threading.Thread(target=media_worker, daemon=True, name="media-worker").start()
    last_token_refresh = 0.0
    while True:
        try:
            worker_heartbeat()
            cleanup_expired_records()
            recover_stale_deliveries()
            if time.monotonic() - last_token_refresh >= TOKEN_REFRESH_INTERVAL_SECONDS:
                last_token_refresh = time.monotonic()
                refreshed = refresh_expiring_connection_tokens()
                if refreshed:
                    LOGGER.info("Refreshed %s expiring provider token(s)", refreshed)
            with db() as connection:
                rows = connection.execute("SELECT id FROM posts WHERE state = 'scheduled' AND scheduled_for <= ? ORDER BY scheduled_for LIMIT 10", (now(),)).fetchall()
            for row in rows:
                if claim_post(row["id"]):
                    deliver(row["id"])
        except Exception:
            LOGGER.exception("Scheduled-delivery poll failed")
        time.sleep(POLL_SECONDS)
