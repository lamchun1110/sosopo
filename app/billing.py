"""Stripe Checkout requests and signature-verified webhook handling."""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import UTC, datetime
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

try:  # package import (tests, `python -m app.server`)
    from .audit import audit
    from .config import LOGGER, STRIPE_PLAN_PRICE_VARIABLES, STRIPE_WEBHOOK_TOLERANCE_SECONDS, config, deployment_mode, now
    from .database import db
    from .errors import ProviderError
except ImportError:  # script import (`python /app/app/server.py`)
    from audit import audit
    from config import LOGGER, STRIPE_PLAN_PRICE_VARIABLES, STRIPE_WEBHOOK_TOLERANCE_SECONDS, config, deployment_mode, now
    from database import db
    from errors import ProviderError


def billing_enabled() -> bool:
    return deployment_mode() == "hosted" and bool(config("STRIPE_SECRET_KEY"))


def stripe_request(path: str, payload: dict[str, str]) -> dict[str, Any]:
    request = Request(
        f"https://api.stripe.com/v1/{path}",
        data=urlencode(payload).encode(),
        method="POST",
        headers={"Authorization": f"Bearer {config('STRIPE_SECRET_KEY')}", "Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urlopen(request, timeout=30) as response:
            result = json.loads(response.read() or b"{}")
    except HTTPError as error:
        LOGGER.error("Stripe request failed with status %s", error.code)
        raise ProviderError("The billing provider rejected the request. Check the billing configuration.", retryable=False) from error
    except (URLError, json.JSONDecodeError) as error:
        raise ProviderError("The billing provider could not be reached.") from error
    if not isinstance(result, dict):
        raise ProviderError("The billing provider returned an invalid response.")
    return result


def verify_stripe_signature(payload: bytes, header: str, secret: str) -> bool:
    """Verify a Stripe-Signature header (t=timestamp,v1=hmac) within tolerance."""
    timestamp, candidates = "", []
    for item in header.split(","):
        key, _, value = item.strip().partition("=")
        if key == "t":
            timestamp = value
        elif key == "v1":
            candidates.append(value)
    try:
        age = abs(datetime.now(UTC).timestamp() - int(timestamp))
    except ValueError:
        return False
    if age > STRIPE_WEBHOOK_TOLERANCE_SECONDS or not candidates:
        return False
    expected = hmac.new(secret.encode(), f"{timestamp}.".encode() + payload, hashlib.sha256).hexdigest()
    return any(hmac.compare_digest(expected, candidate) for candidate in candidates)


def apply_billing_event(event: dict[str, Any]) -> None:
    kind = str(event.get("type") or "")
    data = event.get("data", {}).get("object", {}) if isinstance(event.get("data"), dict) else {}
    if not isinstance(data, dict):
        return
    metadata = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
    try:
        workspace_id = int(metadata.get("workspace_id") or data.get("client_reference_id"))
    except (TypeError, ValueError):
        return
    with db() as connection:
        if kind == "checkout.session.completed":
            plan = str(metadata.get("plan") or "")
            if plan in STRIPE_PLAN_PRICE_VARIABLES:
                connection.execute(
                    "UPDATE workspaces SET plan = ?, billing_customer_id = ?, billing_subscription_id = ?, updated_at = ? WHERE id = ?",
                    (plan, str(data.get("customer") or ""), str(data.get("subscription") or ""), now(), workspace_id),
                )
        elif kind == "customer.subscription.deleted":
            connection.execute("UPDATE workspaces SET plan = 'free', billing_subscription_id = NULL, updated_at = ? WHERE id = ?", (now(), workspace_id))
    audit(None, f"billing.{kind}"[:100], "workspace", workspace_id, "Applied verified billing event", "stripe-webhook", workspace_id=workspace_id)
