import sqlite3
import os
from contextlib import contextmanager

# On Railway, store DB on the persistent volume at /data
# Locally it lives next to the app file
_data_dir = os.environ.get("RAILWAY_VOLUME_MOUNT_PATH", os.path.dirname(__file__))
DB_PATH = os.path.join(_data_dir, "warmy.db")


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def db():
    conn = get_conn()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS seed_email (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                email TEXT NOT NULL UNIQUE,
                smtp_host TEXT NOT NULL,
                smtp_port INTEGER NOT NULL DEFAULT 587,
                smtp_user TEXT NOT NULL,
                smtp_pass TEXT NOT NULL,
                use_tls INTEGER NOT NULL DEFAULT 1,
                imap_host TEXT DEFAULT '',
                imap_port INTEGER DEFAULT 993,
                created_at TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS partners (
                id INTEGER PRIMARY KEY,
                email TEXT NOT NULL UNIQUE,
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS sent_emails (
                id INTEGER PRIMARY KEY,
                to_email TEXT NOT NULL,
                subject TEXT NOT NULL,
                sent_at TEXT DEFAULT (datetime('now')),
                day_number INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS warm_start (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                started_at TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS app_settings (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                tracking_base_url TEXT DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS campaigns (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                status TEXT DEFAULT 'draft',
                daily_limit INTEGER DEFAULT 30,
                created_at TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS campaign_steps (
                id INTEGER PRIMARY KEY,
                campaign_id INTEGER NOT NULL,
                step_number INTEGER NOT NULL,
                delay_days INTEGER NOT NULL DEFAULT 0,
                subject TEXT NOT NULL DEFAULT '',
                body TEXT NOT NULL DEFAULT '',
                FOREIGN KEY (campaign_id) REFERENCES campaigns(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS leads (
                id INTEGER PRIMARY KEY,
                campaign_id INTEGER NOT NULL,
                email TEXT NOT NULL,
                first_name TEXT DEFAULT '',
                last_name TEXT DEFAULT '',
                company TEXT DEFAULT '',
                extra_data TEXT DEFAULT '{}',
                status TEXT DEFAULT 'pending',
                current_step INTEGER DEFAULT 0,
                next_send_at TEXT,
                created_at TEXT DEFAULT (datetime('now')),
                FOREIGN KEY (campaign_id) REFERENCES campaigns(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS lead_events (
                id INTEGER PRIMARY KEY,
                lead_id INTEGER NOT NULL,
                campaign_id INTEGER NOT NULL,
                event_type TEXT NOT NULL,
                step_number INTEGER DEFAULT 0,
                detail TEXT DEFAULT '',
                created_at TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS open_tokens (
                id INTEGER PRIMARY KEY,
                token TEXT NOT NULL UNIQUE,
                lead_id INTEGER NOT NULL,
                campaign_id INTEGER NOT NULL,
                step_number INTEGER NOT NULL,
                opened INTEGER DEFAULT 0,
                opened_at TEXT,
                created_at TEXT DEFAULT (datetime('now'))
            );
        """)
    _migrate()


def _migrate():
    """Add new columns to existing tables without breaking existing data."""
    migrations = [
        ("seed_email", "imap_host", "TEXT DEFAULT ''"),
        ("seed_email", "imap_port", "INTEGER DEFAULT 993"),
    ]
    with db() as conn:
        for table, col, definition in migrations:
            try:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {definition}")
            except Exception:
                pass  # Column already exists
