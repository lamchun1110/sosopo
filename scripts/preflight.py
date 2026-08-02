#!/usr/bin/env python3
"""Check a Sosopo deployment configuration without changing application data."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.server import DATA_DIR, DATABASE_URL, Fernet, config, public_url  # noqa: E402


def checks(production: bool) -> tuple[list[str], list[str]]:
    failures: list[str] = []
    warnings: list[str] = []
    data = Path(DATA_DIR)
    if not data.is_dir() or not os.access(data, os.W_OK):
        failures.append(f"Data directory is not writable: {data}")
    public = public_url()
    if production and (urlparse(public).scheme != "https" or not urlparse(public).hostname or urlparse(public).hostname in {"localhost", "127.0.0.1"}):
        failures.append("Production requires SOSOPO_PUBLIC_URL with a public HTTPS hostname.")
    if production:
        key = config("SOSOPO_ENCRYPTION_KEY")
        try:
            Fernet(key.encode())
        except (ValueError, TypeError):
            failures.append("Production requires a valid persistent SOSOPO_ENCRYPTION_KEY (or _FILE).")
        if config("SOSOPO_STORAGE_BACKEND") == "s3":
            if not config("S3_MEDIA_BUCKET"):
                failures.append("S3 media storage requires S3_MEDIA_BUCKET.")
            if not config("SOSOPO_MEDIA_PUBLIC_URL").startswith("https://"):
                failures.append("S3 media storage requires public HTTPS SOSOPO_MEDIA_PUBLIC_URL.")
    if not DATABASE_URL:
        warnings.append("SQLite is active. PostgreSQL is recommended for production and multiple workers.")
    elif not DATABASE_URL.startswith(("postgres://", "postgresql://", "mysql://", "mariadb://", "sqlite:")):
        failures.append("DATABASE_URL has an unsupported scheme.")
    if production and not config("BACKUP_DESTINATION"):
        warnings.append("BACKUP_DESTINATION is not set; local backup is assumed. Schedule backup.py externally.")
    configured = [name for name in ("FACEBOOK_PAGE_ACCESS_TOKEN", "INSTAGRAM_ACCESS_TOKEN", "THREADS_ACCESS_TOKEN", "X_ACCESS_TOKEN", "TELEGRAM_BOT_TOKEN") if config(name)]
    if not configured:
        warnings.append("No legacy provider credentials are configured. Add encrypted connected accounts in the dashboard before publishing.")
    return failures, warnings


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--production", action="store_true", help="Treat missing HTTPS/encryption configuration as errors")
    args = parser.parse_args()
    failures, warnings = checks(args.production)
    for warning in warnings:
        print(f"WARNING: {warning}")
    for failure in failures:
        print(f"ERROR: {failure}")
    if failures:
        raise SystemExit(1)
    print("Preflight passed.")


if __name__ == "__main__":
    main()
