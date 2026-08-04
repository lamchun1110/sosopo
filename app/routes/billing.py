"""Stripe Checkout for workspace subscriptions."""


from __future__ import annotations


from http import HTTPStatus
from typing import Any

try:  # package import (tests, `python -m app.server`)
    from ..audit import audit
    from ..billing import billing_enabled, stripe_request
    from ..config import STRIPE_PLAN_PRICE_VARIABLES, config, public_url
    from ..database import Record
except ImportError:  # script import (`python /app/app/server.py`)
    from audit import audit
    from billing import billing_enabled, stripe_request
    from config import STRIPE_PLAN_PRICE_VARIABLES, config, public_url
    from database import Record


class BillingRoutes:
    """Stripe Checkout for workspace subscriptions.

    Mixed into ``Handler``; every method returns True once it has answered.
    """



    def post_billing(self, path: str, payload: dict[str, Any], session: Record) -> bool:
        """Handle one billing POST; True when answered."""
        if path == "/api/workspaces/billing/checkout":
            workspace_id = self._require_workspace(session, "owner")
            if workspace_id is None:
                return True
            if not billing_enabled():
                self._json({"error": "Billing is not configured on this Sosopo deployment."}, HTTPStatus.SERVICE_UNAVAILABLE); return True
            plan = str(payload.get("plan", "")).strip()
            price = config(STRIPE_PLAN_PRICE_VARIABLES.get(plan, ""))
            if plan not in STRIPE_PLAN_PRICE_VARIABLES or not price:
                self._json({"error": "Choose a purchasable plan."}, HTTPStatus.BAD_REQUEST); return True
            base = public_url()
            if not base.startswith("https://"):
                self._json({"error": "Billing requires SOSOPO_PUBLIC_URL with a public HTTPS URL."}, HTTPStatus.BAD_REQUEST); return True
            checkout = stripe_request("checkout/sessions", {
                "mode": "subscription",
                "line_items[0][price]": price,
                "line_items[0][quantity]": "1",
                "success_url": f"{base}/?billing=success",
                "cancel_url": f"{base}/?billing=cancelled",
                "client_reference_id": str(workspace_id),
                "metadata[workspace_id]": str(workspace_id),
                "metadata[plan]": plan,
                "subscription_data[metadata][workspace_id]": str(workspace_id),
                "subscription_data[metadata][plan]": plan,
            })
            if not checkout.get("url"):
                self._json({"error": "The billing provider did not return a checkout URL."}, HTTPStatus.BAD_GATEWAY); return True
            audit(session["user_id"], "billing.checkout_started", "workspace", workspace_id, f"Started {plan} checkout", self._source_ip(), workspace_id=workspace_id)
            self._json({"url": str(checkout["url"])}); return True
        return False

