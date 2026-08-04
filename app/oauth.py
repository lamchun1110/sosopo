"""OIDC single sign-on and social-provider OAuth authorization flows."""

from __future__ import annotations

import json
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import urlopen

import jwt
from jwt import PyJWKClient

try:  # package import (tests, `python -m app.server`)
    from . import http_client
    from .config import config, public_url
    from .errors import ProviderError
except ImportError:  # script import (`python /app/app/server.py`)
    import http_client
    from config import config, public_url
    from errors import ProviderError


def oidc_settings() -> dict[str, str]:
    issuer, client_id = config("OIDC_ISSUER_URL").rstrip("/"), config("OIDC_CLIENT_ID")
    if not issuer or not client_id:
        raise ProviderError("SSO is not configured. Set OIDC_ISSUER_URL and OIDC_CLIENT_ID.")
    try:
        with urlopen(f"{issuer}/.well-known/openid-configuration", timeout=15) as response:
            discovery = json.loads(response.read())
    except (URLError, HTTPError, json.JSONDecodeError) as error:
        raise ProviderError("Could not load the OIDC provider configuration.") from error
    if discovery.get("issuer") != issuer or not discovery.get("jwks_uri"):
        raise ProviderError("OIDC discovery issuer or JWKS endpoint does not match the configured issuer.")
    return {**discovery, "issuer": issuer, "client_id": client_id, "client_secret": config("OIDC_CLIENT_SECRET")}


def oidc_redirect_uri() -> str:
    base = public_url()
    if not base.startswith("https://"):
        raise ProviderError("SSO requires SOSOPO_PUBLIC_URL with a public HTTPS URL.")
    return f"{base}/api/auth/oidc/callback"


def verify_oidc_id_token(token: object, settings: dict[str, str], nonce: str) -> dict[str, Any]:
    if not isinstance(token, str):
        raise ProviderError("OIDC provider did not return an ID token.")
    try:
        header = jwt.get_unverified_header(token)
        algorithm = str(header.get("alg", ""))
        allowed = settings.get("id_token_signing_alg_values_supported", [])
        if algorithm in {"none", "HS256", "HS384", "HS512"} or algorithm not in allowed:
            raise ProviderError("OIDC provider returned an unsupported ID-token algorithm.")
        key = PyJWKClient(settings["jwks_uri"]).get_signing_key_from_jwt(token).key
        claims = jwt.decode(token, key, algorithms=[algorithm], audience=settings["client_id"], issuer=settings["issuer"], options={"require": ["exp", "iat", "iss", "aud", "sub"]})
    except (jwt.PyJWTError, KeyError) as error:
        raise ProviderError("OIDC ID-token validation failed.") from error
    if not secrets.compare_digest(str(claims.get("nonce", "")), nonce):
        raise ProviderError("OIDC ID-token nonce validation failed.")
    return claims


def social_oauth_redirect_uri() -> str:
    base = public_url()
    if not base.startswith("https://"):
        raise ProviderError("Social account connection requires SOSOPO_PUBLIC_URL with a public HTTPS URL.")
    return f"{base}/api/social-oauth/callback"


def social_oauth_settings(provider: str) -> dict[str, str]:
    # Instagram professional accounts are authorized and discovered through the
    # same Meta Page grant as Facebook; the dashboard exposes both entry points.
    if provider == "Instagram":
        provider = "Facebook"
    settings = {
        "Facebook": {"client_id": config("FACEBOOK_OAUTH_CLIENT_ID"), "client_secret": config("FACEBOOK_OAUTH_CLIENT_SECRET"), "authorize": config("FACEBOOK_OAUTH_AUTHORIZE_URL") or "https://www.facebook.com/v24.0/dialog/oauth", "token": config("FACEBOOK_OAUTH_TOKEN_URL") or "https://graph.facebook.com/v24.0/oauth/access_token", "scopes": "pages_show_list,pages_read_engagement,pages_manage_posts,instagram_basic,instagram_content_publish"},
        "Threads": {"client_id": config("THREADS_OAUTH_CLIENT_ID"), "client_secret": config("THREADS_OAUTH_CLIENT_SECRET"), "authorize": config("THREADS_OAUTH_AUTHORIZE_URL") or "https://threads.net/oauth/authorize", "token": config("THREADS_OAUTH_TOKEN_URL") or "https://graph.threads.net/oauth/access_token", "scopes": "threads_basic,threads_content_publish"},
        "X": {"client_id": config("X_OAUTH_CLIENT_ID"), "client_secret": config("X_OAUTH_CLIENT_SECRET"), "authorize": config("X_OAUTH_AUTHORIZE_URL") or "https://x.com/i/oauth2/authorize", "token": config("X_OAUTH_TOKEN_URL") or "https://api.x.com/2/oauth2/token", "scopes": "tweet.read,tweet.write,users.read,offline.access"},
        "LinkedIn": {"client_id": config("LINKEDIN_OAUTH_CLIENT_ID"), "client_secret": config("LINKEDIN_OAUTH_CLIENT_SECRET"), "authorize": config("LINKEDIN_OAUTH_AUTHORIZE_URL") or "https://www.linkedin.com/oauth/v2/authorization", "token": config("LINKEDIN_OAUTH_TOKEN_URL") or "https://www.linkedin.com/oauth/v2/accessToken", "scopes": "openid profile w_member_social"},
        "Discord": {"client_id": config("DISCORD_OAUTH_CLIENT_ID"), "client_secret": config("DISCORD_OAUTH_CLIENT_SECRET"), "authorize": config("DISCORD_OAUTH_AUTHORIZE_URL") or "https://discord.com/oauth2/authorize", "token": config("DISCORD_OAUTH_TOKEN_URL") or "https://discord.com/api/oauth2/token", "scopes": "webhook.incoming"},
    }.get(provider)
    if not settings or not settings["client_id"] or not settings["client_secret"]:
        raise ProviderError(f"{provider} OAuth is not configured by this Sosopo administrator.")
    return settings


def social_oauth_enabled(provider: str) -> bool:
    try:
        social_oauth_settings(provider)
        return True
    except ProviderError:
        return False


def social_token_expiry(token: dict[str, Any]) -> str | None:
    try:
        seconds = int(token.get("expires_in", 0))
        return (datetime.now(UTC) + timedelta(seconds=seconds)).isoformat() if seconds > 0 else None
    except (TypeError, ValueError):
        return None


def social_oauth_connections(provider: str, settings: dict[str, str], code: str, verifier: str | None) -> list[dict[str, str]]:
    redirect_uri = social_oauth_redirect_uri()
    payload = {"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri, "client_id": settings["client_id"], "client_secret": settings["client_secret"]}
    if provider == "X":
        payload["code_verifier"] = verifier or ""
    token = http_client.request_form(settings["token"], payload)
    access_token = str(token.get("access_token") or "")
    if not access_token:
        raise ProviderError("The provider did not return an access token.")
    expiry = social_token_expiry(token)
    if provider == "Facebook":
        base = config("META_GRAPH_BASE_URL") or "https://graph.facebook.com/v24.0"
        pages = http_client.request_get_json(f"{base}/me/accounts?{urlencode({'fields': 'id,name,access_token,instagram_business_account{id,username}', 'access_token': access_token})}").get("data", [])
        records: list[dict[str, str]] = []
        for page in pages if isinstance(pages, list) else []:
            if not isinstance(page, dict) or not page.get("id") or not page.get("access_token"):
                continue
            records.append({"provider": "Facebook", "external_account_id": str(page["id"]), "display_name": str(page.get("name") or page["id"]), "access_token": str(page["access_token"]), "token_expires_at": expiry or ""})
            instagram = page.get("instagram_business_account")
            if isinstance(instagram, dict) and instagram.get("id"):
                records.append({"provider": "Instagram", "external_account_id": str(instagram["id"]), "display_name": str(instagram.get("username") or page.get("name") or instagram["id"]), "access_token": str(page["access_token"]), "token_expires_at": expiry or ""})
        if not records:
            raise ProviderError("No managed Facebook Pages were returned. Confirm that the account manages a Page and approved page permissions.")
        return records
    if provider == "Threads":
        base = config("THREADS_API_BASE_URL") or "https://graph.threads.net/v1.0"
        profile = http_client.request_get_json(f"{base}/me?{urlencode({'fields': 'id,username', 'access_token': access_token})}")
        if not profile.get("id"):
            raise ProviderError("Threads did not return a profile.")
        return [{"provider": "Threads", "external_account_id": str(profile["id"]), "display_name": str(profile.get("username") or profile["id"]), "access_token": access_token, "token_expires_at": expiry or ""}]
    refresh_token = str(token.get("refresh_token") or "")
    if provider == "LinkedIn":
        profile = http_client.request_get_json("https://api.linkedin.com/v2/userinfo", {"Authorization": f"Bearer {access_token}"})
        subject = str(profile.get("sub") or "")
        if not subject:
            raise ProviderError("LinkedIn did not return a member profile.")
        author = subject if subject.startswith("urn:li:") else f"urn:li:person:{subject}"
        return [{"provider": "LinkedIn", "external_account_id": author, "display_name": str(profile.get("name") or profile.get("given_name") or subject), "access_token": access_token, "refresh_token": refresh_token, "token_expires_at": expiry or ""}]
    if provider == "Discord":
        webhook = token.get("webhook")
        if not isinstance(webhook, dict) or not webhook.get("id") or not webhook.get("token"):
            raise ProviderError("Discord did not return an approved channel webhook.")
        webhook_id, webhook_token = str(webhook["id"]), str(webhook["token"])
        return [{"provider": "Discord", "external_account_id": webhook_id, "display_name": str(webhook.get("name") or f"Discord channel {webhook.get('channel_id') or webhook_id}"), "access_token": f"https://discord.com/api/webhooks/{webhook_id}/{webhook_token}", "secret_name": "webhook_url", "token_expires_at": ""}]
    profile = http_client.request_get_json("https://api.x.com/2/users/me", {"Authorization": f"Bearer {access_token}"}).get("data", {})
    if not isinstance(profile, dict) or not profile.get("id"):
        raise ProviderError("X did not return a user profile.")
    return [{"provider": "X", "external_account_id": str(profile["id"]), "display_name": str(profile.get("username") or profile.get("name") or profile["id"]), "access_token": access_token, "refresh_token": refresh_token, "token_expires_at": expiry or ""}]
