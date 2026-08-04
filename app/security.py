"""Credential encryption, password hashing, and request-level abuse guards."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import threading
import time
from datetime import UTC, datetime

from cryptography.fernet import Fernet, InvalidToken

try:  # package import (tests, `python -m app.server`)
    from .config import SESSION_SECONDS, config
    from .errors import ProviderError
except ImportError:  # script import (`python /app/app/server.py`)
    from config import SESSION_SECONDS, config
    from errors import ProviderError


RATE_LIMITS: dict[tuple[str, str], list[float]] = {}


RATE_LIMIT_LOCK = threading.Lock()


def allowed_request(client: str, scope: str, limit: int) -> bool:
    """Simple process-local abuse guard for endpoints that accept credentials or writes."""
    cutoff = time.monotonic() - 60
    with RATE_LIMIT_LOCK:
        requests = [item for item in RATE_LIMITS.get((client, scope), []) if item >= cutoff]
        if len(requests) >= limit:
            RATE_LIMITS[(client, scope)] = requests
            return False
        requests.append(time.monotonic())
        RATE_LIMITS[(client, scope)] = requests
        return True


def source_ip(peer: str, forwarded_for: str, trusted_proxy_cidrs: str) -> str:
    """Use X-Forwarded-For only when the direct peer is explicitly trusted."""
    try:
        peer_address = ipaddress.ip_address(peer)
        trusted = [ipaddress.ip_network(item.strip(), strict=False) for item in trusted_proxy_cidrs.split(",") if item.strip()]
        if not any(peer_address in network for network in trusted) or not forwarded_for:
            return peer
        candidate = forwarded_for.split(",")[0].strip()
        ipaddress.ip_address(candidate)
        return candidate
    except ValueError:
        return peer


def encryption() -> Fernet:
    key = config("SOSOPO_ENCRYPTION_KEY")
    if not key:
        raise ProviderError("Set SOSOPO_ENCRYPTION_KEY before saving account credentials.")
    try:
        return Fernet(key.encode())
    except (ValueError, TypeError) as error:
        raise ProviderError("SOSOPO_ENCRYPTION_KEY must be a valid Fernet key.") from error


def encrypt_secrets(value: dict[str, str]) -> str:
    return encryption().encrypt(json.dumps(value).encode()).decode()


def decrypt_secrets(value: str) -> dict[str, str]:
    try:
        decoded = encryption().decrypt(value.encode())
        result = json.loads(decoded)
    except (InvalidToken, ValueError, TypeError, json.JSONDecodeError) as error:
        raise ProviderError("Could not decrypt the saved provider credentials.") from error
    if not isinstance(result, dict):
        raise ProviderError("Saved provider credentials are invalid.")
    return {str(key): str(item) for key, item in result.items()}


def hash_password(password: str, salt: bytes) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 600_000).hex()


def expires_at() -> str:
    return datetime.fromtimestamp(datetime.now(UTC).timestamp() + SESSION_SECONDS, UTC).isoformat()
