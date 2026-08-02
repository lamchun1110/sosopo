#!/usr/bin/env python3
"""Create a Sosopo database-and-media backup locally or in S3-compatible storage."""

from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import tarfile
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse


data_dir = Path(os.environ.get("SOSOPO_DATA_DIR", "/data"))
database_url = os.environ.get("DATABASE_URL", "")
backup_dir = Path(os.environ.get("BACKUP_LOCAL_DIR", "/backups"))
stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
workspace = backup_dir / f".sosopo-{stamp}"
archive = backup_dir / f"sosopo-{stamp}.tar.gz"


def environment_value(name: str) -> str:
    file_name = os.environ.get(f"{name}_FILE", "").strip()
    if file_name:
        return Path(file_name).read_text(encoding="utf-8").strip()
    return os.environ.get(name, "").strip()


def dump_database() -> Path:
    database_url = environment_value("DATABASE_URL")
    if not database_url or database_url.startswith("sqlite:"):
        source = data_dir / "sosopo.sqlite3" if not database_url else Path(urlparse(database_url).path)
        output = workspace / "database.sqlite3"
        with sqlite3.connect(source) as source_db, sqlite3.connect(output) as output_db:
            source_db.backup(output_db)
        return output
    if database_url.startswith(("postgres://", "postgresql://")):
        output = workspace / "database.dump"
        subprocess.run(["pg_dump", "--format=custom", "--file", str(output), database_url], check=True)
        return output
    if database_url.startswith(("mysql://", "mariadb://")):
        output = workspace / "database.sql"
        parsed = urlparse(database_url)
        if not parsed.hostname or not parsed.path or parsed.path == "/":
            raise SystemExit("DATABASE_URL must include a MariaDB/MySQL host and database name.")
        environment = os.environ.copy()
        if parsed.password:
            environment["MYSQL_PWD"] = parsed.password
        command = [
            "mysqldump", "--single-transaction", "--routines", "--events",
            "--host", parsed.hostname, "--port", str(parsed.port or 3306),
            "--user", parsed.username or "", parsed.path.lstrip("/"),
        ]
        subprocess.run(command, check=True, stdout=output.open("wb"), env=environment)
        return output
    raise SystemExit("DATABASE_URL must be SQLite, PostgreSQL, MariaDB, or MySQL.")


def main() -> None:
    backup_dir.mkdir(parents=True, exist_ok=True)
    workspace.mkdir(mode=0o700)
    try:
        dump_database()
        uploads = data_dir / "uploads"
        if uploads.is_dir():
            shutil.copytree(uploads, workspace / "uploads")
        with tarfile.open(archive, "w:gz") as output:
            for item in workspace.iterdir():
                output.add(item, arcname=item.name)
        if environment_value("BACKUP_DESTINATION") == "s3":
            if not environment_value("S3_BUCKET"):
                raise SystemExit("S3_BUCKET is required when BACKUP_DESTINATION=s3.")
            import boto3
            client = boto3.client("s3", endpoint_url=environment_value("S3_ENDPOINT_URL") or None, aws_access_key_id=environment_value("AWS_ACCESS_KEY_ID") or None, aws_secret_access_key=environment_value("AWS_SECRET_ACCESS_KEY") or None)
            options = {"Bucket": environment_value("S3_BUCKET"), "Key": f"{environment_value('S3_PREFIX') or 'sosopo'}/{archive.name}", "Filename": str(archive)}
            if environment_value("S3_SERVER_SIDE_ENCRYPTION"):
                options["ExtraArgs"] = {"ServerSideEncryption": environment_value("S3_SERVER_SIDE_ENCRYPTION")}
            client.upload_file(**options)
        print(archive)
    finally:
        shutil.rmtree(workspace, ignore_errors=True)


if __name__ == "__main__":
    main()
