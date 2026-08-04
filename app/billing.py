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
    from .config import LOGGER, STRIPE_CREDIT_PACKS, STRIPE_PLAN_PRICE_VARIABLES, STRIPE_WEBHOOK_TOLERANCE_SECONDS, config, deployment_mode, now
    from .credits import ensure_credit_account, record_credit_transaction
    from .database import Database, db
    from .errors import ProviderError
except ImportError:  # script import (`python /app/app/server.py`)
    from audit import audit
    from config import LOGGER, STRIPE_CREDIT_PACKS, STRIPE_PLAN_PRICE_VARIABLES, STRIPE_WEBHOOK_TOLERANCE_SECONDS, config, deployment_mode, now
    from credits import ensure_credit_account, record_credit_transaction
    from database import Database, db
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


def credit_packs() -> dict[str, dict[str, Any]]:
    """One-time credit packs that this deployment can actually sell.

    A pack is offered only once its Stripe price ID is configured, so an
    unconfigured deployment simply has none.
    """
    packs: dict[str, dict[str, Any]] = {name: dict(pack) for name, pack in STRIPE_CREDIT_PACKS.items()}
    raw = config("SOSOPO_CREDIT_PACKS")
    if raw:
        try:
            override = json.loads(raw)
        except json.JSONDecodeError:
            LOGGER.warning("SOSOPO_CREDIT_PACKS is not valid JSON and was ignored")
            override = {}
        if isinstance(override, dict):
            for name, value in override.items():
                if isinstance(value, dict) and value.get("price_variable") and isinstance(value.get("credits"), int):
                    packs[str(name)] = dict(value)
    return {name: {**pack, "price": config(str(pack["price_variable"]))} for name, pack in packs.items() if config(str(pack["price_variable"]))}


def is_new_billing_event(connection: Database, event_id: str) -> bool:
    """Record a Stripe event id once, so a replayed webhook applies only once.

    Stripe always sends an ``id``. An event without one cannot be deduplicated
    and is applied as-is rather than silently dropped.
    """
    if not event_id:
        return True
    if connection.execute("SELECT 1 FROM billing_events WHERE event_id = ?", (event_id,)).fetchone():
        return False
    connection.execute("INSERT INTO billing_events (event_id, created_at) VALUES (?, ?)", (event_id, now()))
    return True


def apply_credit_purchase(connection: Database, metadata: dict[str, Any], event_id: str) -> tuple[str, int] | None:
    """Credit a workspace or organization account for a completed pack purchase."""
    try:
        credits = int(metadata.get("credits") or 0)
    except (TypeError, ValueError):
        return None
    if credits <= 0:
        return None
    for owner_type, key in (("organization", "organization_id"), ("workspace", "workspace_id")):
        try:
            owner_id = int(metadata.get(key))
        except (TypeError, ValueError):
            continue
        account_id = ensure_credit_account(connection, owner_type, owner_id)
        record_credit_transaction(connection, account_id, credits, "stripe_topup", None, f"event:{event_id}"[:120])
        return owner_type, owner_id
    return None


def apply_billing_event(event: dict[str, Any]) -> None:
    kind = str(event.get("type") or "")
    event_id = str(event.get("id") or "")
    data = event.get("data", {}).get("object", {}) if isinstance(event.get("data"), dict) else {}
    if not isinstance(data, dict):
        return
    metadata = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
    # audit() opens its own connection, so every audit call below happens after
    # this block closes; calling it inside would deadlock on SQLite.
    purchased: tuple[str, int] | None = None
    with db() as connection:
        if not is_new_billing_event(connection, event_id):
            LOGGER.info("Ignored a replayed Stripe event")
            return
        credits_purchase = kind == "checkout.session.completed" and str(metadata.get("kind") or "") == "credits"
        if credits_purchase:
            purchased = apply_credit_purchase(connection, metadata, event_id)
        try:
            workspace_id = int(metadata.get("workspace_id") or data.get("client_reference_id"))
        except (TypeError, ValueError):
            workspace_id = None
        if credits_purchase or workspace_id is None:
            pass
        elif kind == "checkout.session.completed":
            plan = str(metadata.get("plan") or "")
            if plan in STRIPE_PLAN_PRICE_VARIABLES:
                connection.execute(
                    "UPDATE workspaces SET plan = ?, billing_customer_id = ?, billing_subscription_id = ?, updated_at = ? WHERE id = ?",
                    (plan, str(data.get("customer") or ""), str(data.get("subscription") or ""), now(), workspace_id),
                )
        elif kind == "customer.subscription.deleted":
            connection.execute("UPDATE workspaces SET plan = 'free', billing_subscription_id = NULL, updated_at = ? WHERE id = ?", (now(), workspace_id))
    if purchased is not None:
        owner_type, owner_id = purchased
        audit(None, "billing.credits_purchased", owner_type, owner_id, f"Credited {metadata.get('credits')} AI credits", "stripe-webhook",
              workspace_id=owner_id if owner_type == "workspace" else None)
        return
    if credits_purchase or workspace_id is None:
        return
    audit(None, f"billing.{kind}"[:100], "workspace", workspace_id, "Applied verified billing event", "stripe-webhook", workspace_id=workspace_id)
