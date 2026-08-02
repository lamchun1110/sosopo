#!/usr/bin/env python3
"""Non-destructively validate a Sosopo backup archive before relying on it."""

from __future__ import annotations

import argparse
import shutil
import sqlite3
import subprocess
import tarfile
import tempfile
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("archive", type=Path, help="Path to sosopo-*.tar.gz")
    args = parser.parse_args()
    if not args.archive.is_file():
        raise SystemExit(f"Backup archive not found: {args.archive}")

    with tarfile.open(args.archive, "r:gz") as backup:
        members = backup.getmembers()
        names = {member.name for member in members}
        if not names.intersection({"database.sqlite3", "database.dump", "database.sql"}):
            raise SystemExit("Archive does not contain a supported database backup.")
        if any(member.name.startswith("/") or ".." in Path(member.name).parts for member in members):
            raise SystemExit("Archive contains an unsafe path.")
        with tempfile.TemporaryDirectory(prefix="sosopo-backup-") as directory:
            backup.extractall(directory, filter="data")
            root = Path(directory)
            sqlite_backup = root / "database.sqlite3"
            postgres_backup = root / "database.dump"
            if sqlite_backup.exists():
                with sqlite3.connect(sqlite_backup) as database:
                    database.execute("PRAGMA integrity_check").fetchone()
                    database.execute("SELECT name FROM sqlite_master LIMIT 1").fetchone()
            elif postgres_backup.exists():
                subprocess.run(["pg_restore", "--list", str(postgres_backup)], check=True, stdout=subprocess.DEVNULL)
            else:
                sql_backup = root / "database.sql"
                if not sql_backup.stat().st_size:
                    raise SystemExit("MariaDB/MySQL SQL dump is empty.")
    print(f"Backup verified: {args.archive}")


if __name__ == "__main__":
    main()
