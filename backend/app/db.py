"""SQLite storage. All queries use bound parameters (no string-built SQL with user input)."""
from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from typing import Iterator

from .config import settings

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    username TEXT UNIQUE NOT NULL,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL,                  -- employee | sales | hr | admin
    password_hash TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,         -- sha256 of the bearer token; raw token never stored
    user_id TEXT NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS documents (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    original_filename TEXT NOT NULL,
    source_id TEXT NOT NULL,             -- stable source identifier, e.g. upload://<id>
    file_type TEXT NOT NULL,             -- pdf | html | markdown | txt
    stored_filename TEXT,                -- generated internal name, never user supplied
    product TEXT,
    product_version TEXT,                -- only when explicitly present in the doc or upload form
    effective_date TEXT,                 -- only when explicitly present
    uploader_id TEXT NOT NULL,
    uploaded_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    baseline_hash TEXT,                  -- hash approved by an admin (integrity baseline)
    allowed_roles TEXT NOT NULL,         -- JSON list of roles; deny by default
    status TEXT NOT NULL,                -- processing | ready | failed | quarantined | deleted
    approval_status TEXT NOT NULL,       -- pending | approved | rejected
    approved_by TEXT,
    approved_at TEXT,
    collection TEXT NOT NULL DEFAULT 'trusted',   -- trusted | test_fixtures
    page_count INTEGER,
    chunk_count INTEGER NOT NULL DEFAULT 0,
    security_flags TEXT NOT NULL DEFAULT '[]',     -- JSON list
    error TEXT
);

CREATE TABLE IF NOT EXISTS document_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    replaced_at TEXT NOT NULL,
    replaced_by TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS chunks (
    id TEXT PRIMARY KEY,                 -- <document_id>:c<ordinal>
    document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    ordinal INTEGER NOT NULL,
    section TEXT,                        -- heading path, e.g. "Account access > Password reset"
    section_anchor TEXT NOT NULL,        -- stable passage id inside the document
    page INTEGER,                        -- PDF page (1-based) only; NULL for other formats
    text TEXT NOT NULL,                  -- exact source text used for citation excerpts
    embedding BLOB,
    injection_flag INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_chunks_doc ON chunks(document_id);

CREATE TABLE IF NOT EXISTS qa_history (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    question TEXT NOT NULL,
    response_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_history_user ON qa_history(user_id);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    request_id TEXT,
    user_id TEXT,
    action TEXT NOT NULL,
    resource TEXT,
    decision TEXT,
    details TEXT
);

CREATE TABLE IF NOT EXISTS app_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

_local = threading.local()
_init_lock = threading.Lock()


def _connect() -> sqlite3.Connection:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    # Autocommit mode; multi-statement changes use the explicit tx() helper below.
    conn = sqlite3.connect(settings.db_path, timeout=30, check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def get_conn() -> sqlite3.Connection:
    conn = getattr(_local, "conn", None)
    path = str(settings.db_path)
    if conn is None or getattr(_local, "path", None) != path:
        conn = _connect()
        _local.conn = conn
        _local.path = path
    return conn


@contextmanager
def tx() -> Iterator[sqlite3.Connection]:
    conn = get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def init_db() -> None:
    with _init_lock:
        conn = get_conn()
        conn.executescript(SCHEMA)
        conn.execute("INSERT OR IGNORE INTO app_state(key, value) VALUES ('index_epoch', '0')")
        conn.commit()


def bump_index_epoch() -> int:
    """Called on every change that affects what any user can retrieve (upload, ACL
    change, approval, replacement, deletion). Retrieval index and answer caches are
    keyed on this value, so stale entries are never served."""
    conn = get_conn()
    conn.execute("UPDATE app_state SET value = CAST(value AS INTEGER) + 1 WHERE key = 'index_epoch'")
    conn.commit()
    return index_epoch()


def index_epoch() -> int:
    row = get_conn().execute("SELECT value FROM app_state WHERE key='index_epoch'").fetchone()
    return int(row["value"]) if row else 0
