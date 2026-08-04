"""Hashed workspace invitations and transactional email delivery."""

from __future__ import annotations

import hashlib
from urllib.parse import quote

try:  # package import (tests, `python -m app.server`)
    from .config import LOGGER, config, now, public_url
    from .database import Database, Record
except ImportError:  # script import (`python /app/app/server.py`)
    from config import LOGGER, config, now, public_url
    from database import Database, Record


def send_email(recipient: str, subject: str, body: str) -> bool:
    """Send one transactional email; return False when unconfigured or delivery fails."""
    host = config("SOSOPO_SMTP_HOST")
    if not host:
        return False
    import smtplib
    from email.message import EmailMessage
    message = EmailMessage()
    message["From"] = config("SOSOPO_SMTP_FROM") or config("SOSOPO_SMTP_USERNAME") or f"sosopo@{host}"
    message["To"] = recipient
    message["Subject"] = subject
    message.set_content(body)
    try:
        with smtplib.SMTP(host, int(config("SOSOPO_SMTP_PORT") or 587), timeout=15) as client:
            if config("SOSOPO_SMTP_STARTTLS").lower() not in {"0", "false", "no"}:
                client.starttls()
            username = config("SOSOPO_SMTP_USERNAME")
            if username:
                client.login(username, config("SOSOPO_SMTP_PASSWORD"))
            client.send_message(message)
        return True
    except (OSError, ValueError, smtplib.SMTPException):
        LOGGER.exception("Transactional email could not be sent")
        return False


def invitation_url(token: str) -> str:
    base = public_url()
    return f"{base}/invite?token={quote(token, safe='')}" if base else f"/invite?token={quote(token, safe='')}"


def invitation_by_token(connection: Database, token: str) -> Record | None:
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    return connection.execute(
        "SELECT workspace_invitations.*, workspaces.name AS workspace_name, workspaces.status AS workspace_status"
        " FROM workspace_invitations JOIN workspaces ON workspaces.id = workspace_invitations.workspace_id"
        " WHERE workspace_invitations.token_hash = ?",
        (token_hash,),
    ).fetchone()


def invitation_is_usable(invitation: Record | None) -> bool:
    return (
        invitation is not None
        and invitation["accepted_at"] is None
        and str(invitation["expires_at"]) > now()
        and invitation["workspace_status"] == "active"
    )
