"""Plan limits and monthly usage metering.

Self-hosted deployments resolve to unlimited, so none of these guards fire."""

from __future__ import annotations

import json
from datetime import UTC, datetime

try:  # package import (tests, `python -m app.server`)
    from .config import LOGGER, PLAN_LIMITS, config, now
    from .database import Database
    from .errors import ProviderError
    from .workspaces import workspace_plan, workspace_setting
except ImportError:  # script import (`python /app/app/server.py`)
    from config import LOGGER, PLAN_LIMITS, config, now
    from database import Database
    from errors import ProviderError
    from workspaces import workspace_plan, workspace_setting


def plan_limits(plan: str) -> dict[str, int] | None:
    """Resolve one plan's limits; None means unlimited."""
    limits: dict[str, dict[str, int] | None] = dict(PLAN_LIMITS)
    raw = config("SOSOPO_PLAN_LIMITS")
    if raw:
        try:
            override = json.loads(raw)
        except json.JSONDecodeError:
            LOGGER.warning("SOSOPO_PLAN_LIMITS is not valid JSON and was ignored")
            override = {}
        if isinstance(override, dict):
            for name, value in override.items():
                if value is None or isinstance(value, dict):
                    limits[str(name)] = value
    return limits.get(plan)


def current_period() -> str:
    return datetime.now(UTC).strftime("%Y-%m")


def record_usage(connection: Database, workspace_id: int, metric: str, amount: int = 1, period: str | None = None) -> None:
    period = period or current_period()
    updated = connection.execute(
        "UPDATE usage_records SET amount = amount + ?, updated_at = ? WHERE workspace_id = ? AND metric = ? AND period = ?",
        (amount, now(), workspace_id, metric, period),
    )
    if updated.rowcount == 0:
        connection.execute(
            "INSERT INTO usage_records (workspace_id, metric, period, amount, updated_at) VALUES (?, ?, ?, ?, ?)",
            (workspace_id, metric, period, amount, now()),
        )


def usage_amount(connection: Database, workspace_id: int, metric: str, period: str | None = None) -> int:
    row = connection.execute(
        "SELECT amount FROM usage_records WHERE workspace_id = ? AND metric = ? AND period = ?",
        (workspace_id, metric, period or current_period()),
    ).fetchone()
    return int(row["amount"]) if row else 0


def enforce_monthly_quota(connection: Database, workspace_id: int, metric: str, limit_name: str, label: str) -> None:
    limits = plan_limits(workspace_plan(connection, workspace_id))
    if limits is not None:
        limit = limits.get(limit_name)
        if limit is not None and usage_amount(connection, workspace_id, metric) >= int(limit):
            raise ProviderError(f"This workspace reached its monthly limit of {limit} {label}. Upgrade the plan or wait for the next month.", retryable=False)
    if metric.startswith("ai_"):
        cap = workspace_setting(connection, workspace_id, "ai_monthly_cap")
        if cap is not None:
            spent = usage_amount(connection, workspace_id, "ai_generations") + usage_amount(connection, workspace_id, "ai_media")
            if spent >= int(cap):
                raise ProviderError(f"This workspace reached its monthly AI budget cap of {cap} actions. A workspace owner can raise the cap in Team settings.", retryable=False)


def enforce_member_limit(connection: Database, workspace_id: int) -> None:
    limits = plan_limits(workspace_plan(connection, workspace_id))
    if limits is None or limits.get("members") is None:
        return
    count = int(connection.execute("SELECT COUNT(*) AS count FROM workspace_memberships WHERE workspace_id = ?", (workspace_id,)).fetchone()["count"])
    if count >= int(limits["members"]):
        raise ProviderError(f"This workspace plan allows {limits['members']} members. Upgrade the plan to add more.", retryable=False)


def enforce_connection_limit(connection: Database, workspace_id: int) -> None:
    limits = plan_limits(workspace_plan(connection, workspace_id))
    if limits is None or limits.get("connections") is None:
        return
    count = int(connection.execute("SELECT COUNT(*) AS count FROM connections WHERE workspace_id = ? AND is_active = 1", (workspace_id,)).fetchone()["count"])
    if count >= int(limits["connections"]):
        raise ProviderError(f"This workspace plan allows {limits['connections']} connected accounts. Upgrade the plan to add more.", retryable=False)


def enforce_storage_limit(connection: Database, workspace_id: int, additional_bytes: int) -> None:
    limits = plan_limits(workspace_plan(connection, workspace_id))
    if limits is None or limits.get("storage_mb") is None:
        return
    used = usage_amount(connection, workspace_id, "storage_bytes", period="total")
    if used + additional_bytes > int(limits["storage_mb"]) * 1024 * 1024:
        raise ProviderError(f"This workspace reached its {limits['storage_mb']} MB media storage limit. Upgrade the plan or remove media.", retryable=False)
