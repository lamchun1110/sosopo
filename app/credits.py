"""The AI credit ledger.

CLAUDE.md is explicit about what credits are for: **only AI usage consumes
credits**. Publishing posts, scheduling, media storage, users, teams, and
organizations never do. Nothing in this module is wired into those paths.

Design rules:

- ``credit_transactions`` is append-only. Rows are never updated or deleted,
  and each carries ``balance_after`` so an auditor can verify the running
  balance without replaying the whole table.
- ``credit_accounts.balance`` is a materialized cache of that history. It is
  only ever moved by :func:`record_credit_transaction`, which is the single
  write path.
- A balance can never go negative. An over-spend raises before either the
  balance or the ledger changes.
- Self-hosted is unlimited. :func:`credits_enforced` is False unless the
  deployment is hosted or ``SOSOPO_CREDITS_ENFORCED`` says otherwise, and the
  charge helpers are no-ops when it is False — so a self-hosted install
  behaves exactly as it did before this ledger existed, with no ledger rows.

``usage_records`` continues to carry analytics; this ledger carries money-like
state. They are deliberately separate: usage is a counter that may be reset or
re-aggregated, the ledger is an audit trail that may not.
"""

from __future__ import annotations

try:  # package import (tests, `python -m app.server`)
    from .config import config, deployment_mode, now
    from .database import Database, Record, insert_id
    from .errors import ProviderError
    from .plans import current_period, plan_limits
    from .workspaces import workspace_plan
except ImportError:  # script import (`python /app/app/server.py`)
    from config import config, deployment_mode, now
    from database import Database, Record, insert_id
    from errors import ProviderError
    from plans import current_period, plan_limits
    from workspaces import workspace_plan


CREDIT_OWNER_TYPES = ("organization", "workspace", "user")
# Plan limits that make up one month's credit grant. Both are AI actions, and
# one AI action costs one credit, so the grant is simply their sum.
GRANTED_LIMIT_NAMES = ("ai_generations_per_month", "ai_media_per_month")


def credits_enforced() -> bool:
    """Whether balances gate AI usage. Self-hosted is unlimited by default."""
    value = config("SOSOPO_CREDITS_ENFORCED").strip().lower()
    if value in {"1", "true", "yes"}:
        return True
    if value in {"0", "false", "no"}:
        return False
    return deployment_mode() == "hosted"


def ensure_credit_account(connection: Database, owner_type: str, owner_id: int) -> int:
    """Return the account id for one owner, creating an empty account once."""
    if owner_type not in CREDIT_OWNER_TYPES:
        raise ProviderError(f"Credit accounts belong to one of: {', '.join(CREDIT_OWNER_TYPES)}.", retryable=False)
    existing = connection.execute("SELECT id FROM credit_accounts WHERE owner_type = ? AND owner_id = ?", (owner_type, owner_id)).fetchone()
    if existing:
        return int(existing["id"])
    return insert_id(
        connection,
        "INSERT INTO credit_accounts (owner_type, owner_id, balance, created_at, updated_at) VALUES (?, ?, 0, ?, ?)",
        (owner_type, owner_id, now(), now()),
    )


def credit_account(connection: Database, owner_type: str, owner_id: int) -> Record | None:
    return connection.execute("SELECT * FROM credit_accounts WHERE owner_type = ? AND owner_id = ?", (owner_type, owner_id)).fetchone()


def account_balance(connection: Database, owner_type: str, owner_id: int) -> int:
    account = credit_account(connection, owner_type, owner_id)
    return int(account["balance"]) if account else 0


def record_credit_transaction(connection: Database, account_id: int, delta: int, reason: str, actor_user_id: int | None, reference: str | None) -> int:
    """Move one account's balance and append the matching ledger row.

    This is the only write path for a balance. It raises before writing
    anything when the movement would take the balance below zero.
    """
    row = connection.execute("SELECT balance FROM credit_accounts WHERE id = ?", (account_id,)).fetchone()
    if row is None:
        raise ProviderError("That credit account does not exist.", retryable=False)
    balance_after = int(row["balance"]) + int(delta)
    if balance_after < 0:
        raise ProviderError("This account does not have enough AI credits. Top up or wait for the next monthly grant.", retryable=False)
    connection.execute("UPDATE credit_accounts SET balance = ?, updated_at = ? WHERE id = ?", (balance_after, now(), account_id))
    connection.execute(
        "INSERT INTO credit_transactions (account_id, delta, balance_after, reason, actor_user_id, reference, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (account_id, int(delta), balance_after, reason[:60], actor_user_id, (reference or "")[:120] or None, now()),
    )
    return balance_after


def monthly_grant(connection: Database, workspace_id: int) -> int:
    """One period's credit allowance, derived from the workspace plan."""
    limits = plan_limits(workspace_plan(connection, workspace_id))
    if limits is None:
        return 0
    return sum(int(limits[name]) for name in GRANTED_LIMIT_NAMES if limits.get(name) is not None)


def grant_monthly_credits(connection: Database, workspace_id: int) -> None:
    """Top a workspace up to its plan allowance once per period.

    Called before every charge, so a workspace that has rolled into a new
    month is funded without a scheduled job.
    """
    allowance = monthly_grant(connection, workspace_id)
    if allowance <= 0:
        return
    account_id = ensure_credit_account(connection, "workspace", workspace_id)
    account = connection.execute("SELECT balance, granted_period FROM credit_accounts WHERE id = ?", (account_id,)).fetchone()
    period = current_period()
    if str(account["granted_period"] or "") == period:
        return
    topped_up = allowance - int(account["balance"])
    connection.execute("UPDATE credit_accounts SET granted_period = ?, updated_at = ? WHERE id = ?", (period, now(), account_id))
    if topped_up > 0:
        record_credit_transaction(connection, account_id, topped_up, "monthly_grant", None, f"period:{period}")


def workspace_organization_id(connection: Database, workspace_id: int) -> int | None:
    row = connection.execute("SELECT organization_id FROM workspaces WHERE id = ?", (workspace_id,)).fetchone()
    return int(row["organization_id"]) if row and row["organization_id"] is not None else None


def funding_chain(connection: Database, workspace_id: int, user_id: int | None) -> list[tuple[str, int]]:
    """The accounts that may fund one user's AI action, nearest owner first.

    CLAUDE.md: unused credits stay with their owner unless explicitly
    transferred, so a user's own balance is spent before the workspace's, and
    the workspace's before the organization's.
    """
    chain: list[tuple[str, int]] = []
    if user_id is not None:
        chain.append(("user", int(user_id)))
    chain.append(("workspace", int(workspace_id)))
    organization_id = workspace_organization_id(connection, workspace_id)
    if organization_id is not None:
        chain.append(("organization", organization_id))
    return chain


def charge_ai_credit(connection: Database, workspace_id: int, reason: str, actor_user_id: int | None) -> int | None:
    """Spend one credit for one AI action, returning the account that paid.

    Returns None when credits are not enforced, so callers can store the
    paying account and refund exactly it later.
    """
    if not credits_enforced():
        return None
    grant_monthly_credits(connection, workspace_id)
    for owner_type, owner_id in funding_chain(connection, workspace_id, actor_user_id):
        account = credit_account(connection, owner_type, owner_id)
        if account is not None and int(account["balance"]) > 0:
            record_credit_transaction(connection, int(account["id"]), -1, reason, actor_user_id, f"{owner_type}:{owner_id}")
            return int(account["id"])
    raise ProviderError("This account does not have enough AI credits. Top up or wait for the next monthly grant.", retryable=False)


def refund_ai_credit(connection: Database, account_id: int | None, reason: str, actor_user_id: int | None = None) -> None:
    """Return one credit to the account that paid. A no-op when not enforced."""
    if not credits_enforced() or account_id is None:
        return
    record_credit_transaction(connection, account_id, 1, reason, actor_user_id, f"account:{account_id}")


def allocate_credits(connection: Database, source: tuple[str, int], target: tuple[str, int], amount: int, actor_user_id: int | None) -> None:
    """Move credits between two accounts as a paired debit and credit.

    The debit runs first, so an over-allocation raises before the target is
    credited and the ledger never records half a transfer.
    """
    if amount <= 0:
        raise ProviderError("Allocate a positive whole number of credits.", retryable=False)
    source_id = ensure_credit_account(connection, source[0], source[1])
    target_id = ensure_credit_account(connection, target[0], target[1])
    if source_id == target_id:
        raise ProviderError("Choose a different account to allocate to.", retryable=False)
    record_credit_transaction(connection, source_id, -amount, "allocation_out", actor_user_id, f"{target[0]}:{target[1]}")
    record_credit_transaction(connection, target_id, amount, "allocation_in", actor_user_id, f"{source[0]}:{source[1]}")
