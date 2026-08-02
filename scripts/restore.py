#!/usr/bin/env python3
"""Restore a Sosopo backup archive. This is destructive and requires --confirm."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import tarfile
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import unquote, urlparse


def environment_value(name: str) -> str:
    secret_file = os.environ.get(f"{name}_FILE", "").strip()
    return Path(secret_file).read_text(encoding="utf-8").strip() if secret_file else os.environ.get(name, "").strip()


def extract(archive: Path, destination: Path) -> None:
    with tarfile.open(archive, "r:gz") as source:
        members = source.getmembers()
        if any(item.name.startswith("/") or ".." in Path(item.name).parts for item in members):
            raise SystemExit("Backup archive contains an unsafe path.")
        source.extractall(destination, filter="data")


def restore_uploads(root: Path, data_dir: Path, stamp: str) -> None:
    uploads = root / "uploads"
    if uploads.is_dir():
        current_uploads = data_dir / "uploads"
        if current_uploads.exists():
            current_uploads.rename(data_dir / f"uploads.before-restore-{stamp}")
        shutil.copytree(uploads, current_uploads)


def restore_sqlite(root: Path, data_dir: Path) -> None:
    source = root / "database.sqlite3"
    if not source.is_file():
        raise SystemExit("Archive does not contain database.sqlite3.")
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    database = data_dir / "sosopo.sqlite3"
    if database.exists():
        shutil.copy2(database, data_dir / f"sosopo.sqlite3.before-restore-{stamp}")
    shutil.copy2(source, database)
    restore_uploads(root, data_dir, stamp)


def restore_postgres(root: Path, database_url: str) -> None:
    dump = root / "database.dump"
    if not dump.is_file():
        raise SystemExit("Archive does not contain database.dump.")
    subprocess.run(["pg_restore", "--clean", "--if-exists", "--no-owner", "--dbname", database_url, str(dump)], check=True)


def restore_mysql(root: Path, database_url: str) -> None:
    dump = root / "database.sql"
    if not dump.is_file():
        raise SystemExit("Archive does not contain database.sql.")
    parsed = urlparse(database_url)
    environment = os.environ.copy()
    if parsed.password:
        environment["MYSQL_PWD"] = unquote(parsed.password)
    with dump.open("rb") as source:
        subprocess.run(["mysql", "--host", parsed.hostname or "", "--port", str(parsed.port or 3306), "--user", unquote(parsed.username or ""), parsed.path.lstrip("/")], stdin=source, env=environment, check=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("--confirm", action="store_true", help="Acknowledge destructive database/media replacement")
    args = parser.parse_args()
    if not args.confirm:
        raise SystemExit("Refusing restore without --confirm. Stop sosopo and sosopo-worker first.")
    if not args.archive.is_file():
        raise SystemExit(f"Backup archive not found: {args.archive}")
    database_url = environment_value("DATABASE_URL")
    data_dir = Path(os.environ.get("SOSOPO_DATA_DIR", "/data"))
    with tempfile.TemporaryDirectory(prefix="sosopo-restore-") as directory:
        root = Path(directory)
        extract(args.archive, root)
        if not database_url or database_url.startswith("sqlite:"):
            restore_sqlite(root, data_dir)
        elif database_url.startswith(("postgres://", "postgresql://")):
            restore_postgres(root, database_url)
        elif database_url.startswith(("mysql://", "mariadb://")):
            restore_mysql(root, database_url)
        else:
            raise SystemExit("DATABASE_URL has an unsupported scheme.")
        if database_url and not database_url.startswith("sqlite:"):
            restore_uploads(root, data_dir, datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ"))
    print("Restore completed. Start Sosopo and verify /api/health before allowing users back in.")


if __name__ == "__main__":
    main()
