"""Append-only audit trail and retention cleanup."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

try:  # package import (tests, `python -m app.server`)
    from .config import DEFAULT_AUDIT_RETENTION_DAYS, EXPIRED_INVITATION_RETENTION_DAYS, config, now
    from .database import db
except ImportError:  # script import (`python /app/app/server.py`)
    from config import DEFAULT_AUDIT_RETENTION_DAYS, EXPIRED_INVITATION_RETENTION_DAYS, config, now
    from database import db


def audit(user_id: int | None, action: str, subject_type: str, subject_id: object | None, detail: str, source_ip: str, workspace_id: int | None = None) -> None:
    with db() as connection:
        connection.execute(
            "INSERT INTO audit_events (user_id, workspace_id, action, subject_type, subject_id, detail, source_ip, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (user_id, workspace_id, action, subject_type, str(subject_id) if subject_id is not None else None, detail[:500], source_ip[:100], now()),
        )


def cleanup_expired_records() -> None:
    try:
        retention_days = max(1, int(config("SOSOPO_AUDIT_RETENTION_DAYS") or DEFAULT_AUDIT_RETENTION_DAYS))
    except ValueError:
        retention_days = DEFAULT_AUDIT_RETENTION_DAYS
    with db() as connection:
        connection.execute("DELETE FROM sessions WHERE expires_at <= ?", (now(),))
        connection.execute("DELETE FROM oidc_states WHERE expires_at <= ?", (now(),))
        connection.execute("DELETE FROM social_oauth_states WHERE expires_at <= ?", (now(),))
        connection.execute("DELETE FROM workspace_invitations WHERE accepted_at IS NULL AND expires_at < ?", ((datetime.now(UTC) - timedelta(days=EXPIRED_INVITATION_RETENTION_DAYS)).isoformat(),))
        connection.execute("DELETE FROM audit_events WHERE created_at < ?", ((datetime.now(UTC) - timedelta(days=retention_days)).isoformat(),))
