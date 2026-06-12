"""Shared configuration and database helpers for evidence-tool."""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Iterable

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - python-dotenv is in requirements, but keep app resilient.
    load_dotenv = None

APP_ROOT = Path(__file__).resolve().parent
if load_dotenv:
    load_dotenv(APP_ROOT / ".env")

DB_PATH = Path(os.getenv("EVIDENCE_DB_PATH", APP_ROOT / "db" / "evidence.db")).expanduser().resolve()
UPLOAD_DIR = Path(os.getenv("EVIDENCE_UPLOAD_DIR", APP_ROOT / "uploads")).expanduser().resolve()
HASH_ALGO = os.getenv("EVIDENCE_HASH_ALGO", "blake2b").strip().lower() or "blake2b"
EXTRACT_MAX_CHARS = int(os.getenv("EVIDENCE_EXTRACT_MAX_CHARS", "20000"))
AI_MAX_CONSECUTIVE_ERRORS = int(os.getenv("EVIDENCE_AI_MAX_CONSECUTIVE_ERRORS", "3"))

OLLAMA_ENDPOINTS = [
    item.strip()
    for item in os.getenv("OLLAMA_ENDPOINTS", "http://localhost:11434/api/chat").split(",")
    if item.strip()
]
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "").strip()
OLLAMA_NUM_CTX = int(os.getenv("OLLAMA_NUM_CTX", "8192"))
OLLAMA_NUM_PREDICT = int(os.getenv("OLLAMA_NUM_PREDICT", "1200"))
OLLAMA_TIMEOUT = int(os.getenv("OLLAMA_TIMEOUT", "180"))
OLLAMA_RETRIES = int(os.getenv("OLLAMA_RETRIES", "2"))
OLLAMA_RETRY_DELAY = float(os.getenv("OLLAMA_RETRY_DELAY", "3"))

CORE_EXPORT_HEADERS = [
    "File name",
    "Type of file",
    "Folder location",
    "Last modified date",
    "Creation date",
    "Size (MB)",
    "Hash",
]

AI_EXPORT_HEADERS = CORE_EXPORT_HEADERS + [
    "AI category",
    "AI description",
    "AI status",
]


def get_db() -> sqlite3.Connection:
    """Return a SQLite connection with rows accessible by column name."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
    """Add a column during lightweight SQLite migrations if it is missing."""
    columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")


def init_db() -> None:
    """Create local tables if they do not already exist and apply small migrations."""
    with get_db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS scans (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                label TEXT,
                source_paths TEXT,
                status TEXT NOT NULL DEFAULT 'created',
                started_at TEXT,
                finished_at TEXT,
                file_count INTEGER NOT NULL DEFAULT 0,
                unique_count INTEGER NOT NULL DEFAULT 0,
                duplicate_count INTEGER NOT NULL DEFAULT 0,
                error_count INTEGER NOT NULL DEFAULT 0,
                hash_algo TEXT
            );

            CREATE TABLE IF NOT EXISTS files (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scan_id INTEGER NOT NULL,
                file_name TEXT NOT NULL,
                file_type TEXT NOT NULL,
                folder_location TEXT NOT NULL,
                full_path TEXT NOT NULL,
                last_modified_date TEXT,
                creation_date TEXT,
                size_mb REAL,
                size_bytes INTEGER,
                file_hash TEXT,
                hash_algo TEXT,
                FOREIGN KEY(scan_id) REFERENCES scans(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_files_scan ON files(scan_id);
            CREATE INDEX IF NOT EXISTS idx_files_scan_hash ON files(scan_id, file_hash);
            CREATE INDEX IF NOT EXISTS idx_files_scan_path ON files(scan_id, full_path);

            CREATE TABLE IF NOT EXISTS ai_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scan_id INTEGER NOT NULL,
                file_hash TEXT NOT NULL,
                source_file_id INTEGER,
                source_file_path TEXT,
                category TEXT,
                description TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                error TEXT,
                extracted_chars INTEGER DEFAULT 0,
                updated_at TEXT,
                FOREIGN KEY(scan_id) REFERENCES scans(id) ON DELETE CASCADE,
                UNIQUE(scan_id, file_hash)
            );

            CREATE INDEX IF NOT EXISTS idx_ai_scan_hash ON ai_results(scan_id, file_hash);

            CREATE TABLE IF NOT EXISTS ai_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scan_id INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'queued',
                message TEXT,
                current INTEGER NOT NULL DEFAULT 0,
                total INTEGER NOT NULL DEFAULT 0,
                categories TEXT,
                context TEXT,
                model TEXT,
                max_chars INTEGER,
                max_files INTEGER,
                force INTEGER NOT NULL DEFAULT 0,
                cancel_requested INTEGER NOT NULL DEFAULT 0,
                created_at TEXT,
                started_at TEXT,
                finished_at TEXT,
                error_count INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                FOREIGN KEY(scan_id) REFERENCES scans(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_ai_jobs_scan ON ai_jobs(scan_id);
            CREATE INDEX IF NOT EXISTS idx_ai_jobs_status ON ai_jobs(status, id);

            CREATE TABLE IF NOT EXISTS scan_errors (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scan_id INTEGER NOT NULL,
                path TEXT,
                error TEXT,
                created_at TEXT,
                FOREIGN KEY(scan_id) REFERENCES scans(id) ON DELETE CASCADE
            );
            """
        )
        # Migrations for databases created by earlier versions of the tool.
        _ensure_column(conn, "ai_results", "raw_response", "raw_response TEXT")
        _ensure_column(conn, "ai_results", "model", "model TEXT")
        _ensure_column(conn, "ai_results", "extraction_status", "extraction_status TEXT")
        conn.commit()


def dict_rows(rows: Iterable[sqlite3.Row]) -> list[dict]:
    return [dict(row) for row in rows]
