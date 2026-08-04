"""Additive, idempotent schema creation and migration for all four backends."""

from __future__ import annotations

try:  # package import (tests, `python -m app.server`)
    from . import config as cfg
    from .config import now
    from .database import add_column, add_table_column, db
    from .workspaces import migrate_users_to_workspaces
except ImportError:  # script import (`python /app/app/server.py`)
    import config as cfg
    from config import now
    from database import add_column, add_table_column, db
    from workspaces import migrate_users_to_workspaces


def setup_database() -> None:
    cfg.DATA_DIR.mkdir(parents=True, exist_ok=True)
    cfg.UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    if (not cfg.DATABASE_URL or cfg.DATABASE_URL.startswith("sqlite:")) and cfg.LEGACY_DB_PATH.is_file() and not cfg.DB_PATH.exists():
        cfg.LEGACY_DB_PATH.replace(cfg.DB_PATH)
    with db() as connection:
        id_column = "INTEGER PRIMARY KEY AUTOINCREMENT" if connection.kind == "sqlite" else "BIGSERIAL PRIMARY KEY" if connection.kind == "postgres" else "BIGINT AUTO_INCREMENT PRIMARY KEY"
        connection.execute(
            """CREATE TABLE IF NOT EXISTS posts (
                id %s,
                body TEXT NOT NULL,
                channel TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'draft',
                scheduled_for TEXT,
                image_url TEXT,
                created_at TEXT NOT NULL,
                published_at TEXT,
                external_id TEXT,
                attempts INTEGER NOT NULL DEFAULT 0,
                last_error TEXT
            )""" % id_column
        )
        connection.execute(
            """CREATE TABLE IF NOT EXISTS users (
                id %s,
                username TEXT NOT NULL UNIQUE,
                password_salt TEXT NOT NULL,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'user',
                is_active INTEGER NOT NULL DEFAULT 1,
                timezone TEXT NOT NULL DEFAULT 'UTC',
                signature TEXT NOT NULL DEFAULT '',
                oidc_issuer TEXT,
                oidc_subject TEXT,
                created_at TEXT NOT NULL
            )""" % id_column
        )
        connection.execute(
            """CREATE TABLE IF NOT EXISTS sessions (
                id %s,
                token_hash TEXT NOT NULL UNIQUE,
                csrf_token TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                expires_at TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(id)
            )""" % id_column
        )
        connection.execute(
            """CREATE TABLE IF NOT EXISTS connections (
                id %s,
                user_id INTEGER NOT NULL,
                provider TEXT NOT NULL,
                external_account_id TEXT NOT NULL,
                display_name TEXT NOT NULL,
                encrypted_secrets TEXT NOT NULL,
                settings_json TEXT NOT NULL DEFAULT '{}',
                token_expires_at TEXT,
                is_active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                UNIQUE(user_id, provider, external_account_id),
                FOREIGN KEY(user_id) REFERENCES users(id)
            )""" % id_column
        )
        connection.execute(
            """CREATE TABLE IF NOT EXISTS post_targets (
                post_id INTEGER NOT NULL,
                connection_id INTEGER NOT NULL,
                state TEXT NOT NULL DEFAULT 'pending',
                external_id TEXT,
                last_error TEXT,
                PRIMARY KEY(post_id, connection_id),
                FOREIGN KEY(post_id) REFERENCES posts(id),
                FOREIGN KEY(connection_id) REFERENCES connections(id)
            )"""
        )
        connection.execute(
            """CREATE TABLE IF NOT EXISTS post_media (
                id %s,
                post_id INTEGER NOT NULL,
                media_url TEXT NOT NULL,
                alt_text TEXT,
                position INTEGER NOT NULL,
                FOREIGN KEY(post_id) REFERENCES posts(id),
                UNIQUE(post_id, position)
            )""" % id_column
        )
        connection.execute(
            """CREATE TABLE IF NOT EXISTS oidc_states (
                state TEXT PRIMARY KEY,
                nonce TEXT NOT NULL,
                code_verifier TEXT NOT NULL,
                expires_at TEXT NOT NULL
            )"""
        )
        connection.execute(
            """CREATE TABLE IF NOT EXISTS social_oauth_states (
                state TEXT PRIMARY KEY,
                provider TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                code_verifier TEXT,
                expires_at TEXT NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(id)
            )"""
        )
        for name, definition in (
            ("image_url", "TEXT"), ("published_at", "TEXT"), ("external_id", "TEXT"),
            ("attempts", "INTEGER NOT NULL DEFAULT 0"), ("last_error", "TEXT"), ("user_id", "INTEGER"),
            ("scheduled_timezone", "TEXT"), ("publishing_started_at", "TEXT"),
        ):
            add_column(connection, name, definition)
        for name, definition in (("role", "TEXT NOT NULL DEFAULT 'user'"), ("is_active", "INTEGER NOT NULL DEFAULT 1"), ("timezone", "TEXT NOT NULL DEFAULT 'UTC'"), ("signature", "TEXT NOT NULL DEFAULT ''"), ("oidc_issuer", "TEXT"), ("oidc_subject", "TEXT")):
            add_table_column(connection, "users", name, definition)
        add_table_column(connection, "connections", "is_active", "INTEGER NOT NULL DEFAULT 1")
        connection.execute("CREATE UNIQUE INDEX IF NOT EXISTS users_oidc_identity ON users(oidc_issuer, oidc_subject)")
        connection.execute(
            """CREATE TABLE IF NOT EXISTS deliveries (
                id %s,
                post_id INTEGER NOT NULL,
                provider TEXT NOT NULL,
                status TEXT NOT NULL,
                detail TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY(post_id) REFERENCES posts(id)
        )""" % id_column
        )
        connection.execute(
            """CREATE TABLE IF NOT EXISTS audit_events (
                id %s,
                user_id INTEGER,
                action TEXT NOT NULL,
                subject_type TEXT NOT NULL,
                subject_id TEXT,
                detail TEXT,
                source_ip TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(id)
        )""" % id_column
        )
        connection.execute(
            """CREATE TABLE IF NOT EXISTS worker_heartbeats (
                name TEXT PRIMARY KEY,
                checked_at TEXT NOT NULL
        )"""
        )
        connection.execute(
            """CREATE TABLE IF NOT EXISTS instance_settings (
                name TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )"""
        )
        connection.execute(
            """CREATE TABLE IF NOT EXISTS workspaces (
                id %s,
                name TEXT NOT NULL,
                slug TEXT NOT NULL UNIQUE,
                owner_user_id INTEGER NOT NULL,
                plan TEXT NOT NULL DEFAULT 'self_hosted',
                status TEXT NOT NULL DEFAULT 'active',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(owner_user_id) REFERENCES users(id)
            )""" % id_column
        )
        connection.execute(
            """CREATE TABLE IF NOT EXISTS workspace_memberships (
                id %s,
                workspace_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                role TEXT NOT NULL DEFAULT 'editor',
                invite_state TEXT NOT NULL DEFAULT 'active',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(workspace_id, user_id),
                FOREIGN KEY(workspace_id) REFERENCES workspaces(id),
                FOREIGN KEY(user_id) REFERENCES users(id)
            )""" % id_column
        )
        connection.execute(
            """CREATE TABLE IF NOT EXISTS workspace_invitations (
                id %s,
                workspace_id INTEGER NOT NULL,
                email TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'editor',
                token_hash TEXT NOT NULL UNIQUE,
                invited_by INTEGER NOT NULL,
                expires_at TEXT NOT NULL,
                accepted_at TEXT,
                accepted_user_id INTEGER,
                created_at TEXT NOT NULL,
                FOREIGN KEY(workspace_id) REFERENCES workspaces(id),
                FOREIGN KEY(invited_by) REFERENCES users(id)
            )""" % id_column
        )
        connection.execute(
            """CREATE TABLE IF NOT EXISTS media_jobs (
                id %s,
                workspace_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                kind TEXT NOT NULL,
                prompt TEXT NOT NULL,
                aspect_ratio TEXT NOT NULL DEFAULT '1:1',
                style TEXT NOT NULL DEFAULT '',
                provider TEXT NOT NULL,
                model TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'queued',
                progress INTEGER NOT NULL DEFAULT 0,
                error TEXT,
                result_url TEXT,
                moderation TEXT NOT NULL DEFAULT 'pending',
                reviewed_by INTEGER,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(workspace_id) REFERENCES workspaces(id),
                FOREIGN KEY(user_id) REFERENCES users(id)
            )""" % id_column
        )
        connection.execute("CREATE INDEX IF NOT EXISTS media_jobs_queue ON media_jobs(status, id)")
        connection.execute("CREATE INDEX IF NOT EXISTS media_jobs_workspace ON media_jobs(workspace_id, id)")
        connection.execute(
            """CREATE TABLE IF NOT EXISTS usage_records (
                workspace_id INTEGER NOT NULL,
                metric TEXT NOT NULL,
                period TEXT NOT NULL,
                amount INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(workspace_id, metric, period)
            )"""
        )
        connection.execute(
            """CREATE TABLE IF NOT EXISTS workspace_settings (
                workspace_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                value TEXT NOT NULL,
                PRIMARY KEY(workspace_id, name)
            )"""
        )
        add_column(connection, "workspace_id", "INTEGER")
        add_table_column(connection, "connections", "workspace_id", "INTEGER")
        add_table_column(connection, "sessions", "active_workspace_id", "INTEGER")
        add_table_column(connection, "social_oauth_states", "workspace_id", "INTEGER")
        add_table_column(connection, "audit_events", "workspace_id", "INTEGER")
        add_table_column(connection, "workspaces", "billing_customer_id", "TEXT")
        add_table_column(connection, "workspaces", "billing_subscription_id", "TEXT")
        connection.execute("CREATE INDEX IF NOT EXISTS posts_workspace ON posts(workspace_id, state)")
        connection.execute("CREATE INDEX IF NOT EXISTS connections_workspace ON connections(workspace_id, provider)")
        connection.execute("CREATE INDEX IF NOT EXISTS workspace_memberships_user ON workspace_memberships(user_id)")
        connection.execute("CREATE INDEX IF NOT EXISTS posts_due_delivery ON posts(state, scheduled_for)")
        connection.execute("CREATE INDEX IF NOT EXISTS post_media_post ON post_media(post_id, position)")
        connection.execute("CREATE INDEX IF NOT EXISTS sessions_expiry ON sessions(expires_at)")
        connection.execute("CREATE INDEX IF NOT EXISTS social_oauth_states_expiry ON social_oauth_states(expires_at)")
        connection.execute("CREATE INDEX IF NOT EXISTS deliveries_post ON deliveries(post_id, created_at)")
        if connection.execute("SELECT COUNT(*) FROM posts").fetchone()[0] == 0:
            connection.execute(
                "INSERT INTO posts (body, channel, state, created_at) VALUES (?, ?, 'draft', ?)",
                ("Welcome to Sosopo. Configure a provider when you are ready to publish.", "Facebook", now()),
            )
        migrate_users_to_workspaces(connection)
