"""Backend-neutral connection layer for SQLite, PostgreSQL, MariaDB, and MySQL."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

try:  # package import (tests, `python -m app.server`)
    from . import config as cfg
except ImportError:  # script import (`python /app/app/server.py`)
    import config as cfg


class Record(dict[str, Any]):
    def __getitem__(self, key: str | int) -> Any:
        return list(self.values())[key] if isinstance(key, int) else super().__getitem__(key)


class Result:
    def __init__(self, cursor: Any) -> None:
        self.cursor = cursor
        self.rowcount = cursor.rowcount
        self.lastrowid = getattr(cursor, "lastrowid", None)
        self.names = [item[0] for item in cursor.description] if cursor.description else []

    def _record(self, row: Any) -> Record | None:
        if row is None:
            return None
        return Record(row) if isinstance(row, dict) else Record(zip(self.names, row))

    def fetchone(self) -> Record | None:
        return self._record(self.cursor.fetchone())

    def fetchall(self) -> list[Record]:
        return [self._record(row) for row in self.cursor.fetchall()]


class Database:
    def __init__(self) -> None:
        self.kind = "sqlite" if not cfg.DATABASE_URL or cfg.DATABASE_URL.startswith("sqlite:") else "postgres" if cfg.DATABASE_URL.startswith(("postgres://", "postgresql://")) else "mariadb" if cfg.DATABASE_URL.startswith(("mysql://", "mariadb://")) else ""
        if not self.kind:
            raise RuntimeError("DATABASE_URL must start with sqlite:, postgresql:, mysql:, or mariadb:.")
        if self.kind == "sqlite":
            path = cfg.DB_PATH if not cfg.DATABASE_URL else Path(unquote(urlparse(cfg.DATABASE_URL).path))
            self.raw = sqlite3.connect(path, timeout=10)
        elif self.kind == "postgres":
            import psycopg
            from psycopg.rows import dict_row
            self.raw = psycopg.connect(cfg.DATABASE_URL, row_factory=dict_row)
        else:
            import pymysql
            parsed = urlparse(cfg.DATABASE_URL)
            self.raw = pymysql.connect(host=parsed.hostname, port=parsed.port or 3306, user=unquote(parsed.username or ""), password=unquote(parsed.password or ""), database=parsed.path.lstrip("/"), charset="utf8mb4", cursorclass=pymysql.cursors.DictCursor)

    def __enter__(self) -> "Database":
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        (self.raw.rollback if exc_type else self.raw.commit)()
        self.raw.close()

    def execute(self, statement: str, params: tuple[Any, ...] = ()) -> Result:
        if self.kind != "sqlite":
            statement = statement.replace("?", "%s")
        cursor = self.raw.cursor()
        cursor.execute(statement, params)
        return Result(cursor)


def db() -> Database:
    return Database()


def insert_id(connection: Database, statement: str, params: tuple[Any, ...]) -> int:
    if connection.kind == "postgres":
        return int(connection.execute(f"{statement} RETURNING id", params).fetchone()["id"])
    result = connection.execute(statement, params)
    if result.lastrowid is None:
        raise RuntimeError("Database did not return an inserted ID.")
    return int(result.lastrowid)


def columns(connection: Database) -> set[str]:
    if connection.kind == "sqlite":
        return {row["name"] for row in connection.execute("PRAGMA table_info(posts)").fetchall()}
    if connection.kind == "postgres":
        return {row["name"] for row in connection.execute("SELECT column_name AS name FROM information_schema.columns WHERE table_schema = current_schema() AND table_name = 'posts'").fetchall()}
    return {row["name"] for row in connection.execute("SELECT column_name AS name FROM information_schema.columns WHERE table_schema = DATABASE() AND table_name = 'posts'").fetchall()}


def table_columns(connection: Database, table: str) -> set[str]:
    if connection.kind == "sqlite":
        return {row["name"] for row in connection.execute(f"PRAGMA table_info({table})").fetchall()}
    if connection.kind == "postgres":
        return {row["name"] for row in connection.execute("SELECT column_name AS name FROM information_schema.columns WHERE table_schema = current_schema() AND table_name = ?", (table,)).fetchall()}
    return {row["name"] for row in connection.execute("SELECT column_name AS name FROM information_schema.columns WHERE table_schema = DATABASE() AND table_name = ?", (table,)).fetchall()}


def add_column(connection: Database, name: str, definition: str) -> None:
    if name not in table_columns(connection, "posts"):
        connection.execute(f"ALTER TABLE posts ADD COLUMN {name} {definition}")


def add_table_column(connection: Database, table: str, name: str, definition: str) -> None:
    if name not in table_columns(connection, table):
        connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
