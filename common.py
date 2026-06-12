"""Shared configuration, defaults, and database helpers for evidence-tool."""
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


def _truthy_env(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "y", "on"}


# Image handling is optional. When enabled, the categorizer attaches local image
# files to the Ollama /api/chat request for multimodal models such as Gemma.
AI_SEND_IMAGES = _truthy_env("EVIDENCE_AI_SEND_IMAGES", "true")
IMAGE_MAX_BYTES = int(os.getenv("EVIDENCE_IMAGE_MAX_BYTES", str(10 * 1024 * 1024)))

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
    "AI primary category",
    "AI secondary tags",
    "AI confidence",
    "AI description",
    "AI evidence basis",
    "AI key people",
    "AI key organizations",
    "AI date or event",
    "AI why useful as evidence",
    "AI needs human review",
    "AI original category",
    "AI status",
]

AI_ERROR_EXPORT_HEADERS = [
    "Error ID",
    "AI job ID",
    "Scan ID",
    "Created at",
    "File name",
    "File path",
    "Hash",
    "Stage",
    "Error",
]

DEFAULT_PRIMARY_CATEGORIES = [
    "Network assembly / convening materials",
    "Participant / attendee lists",
    "People / organization directory",
    "Network strategy / governance / regeneration",
    "Work group / priority-area planning",
    "Policy framework / policy agenda",
    "Community schools",
    "Shared Story / narrative / communications strategy",
    "Education resourcing / school funding",
    "Teacher / educator workforce",
    "Federal funding / ARP / COVID response",
    "Place-based / Key Places strategy",
    "Research / evaluation / findings report",
    "Public education defense / voucher-privatization response",
    "Administrative logistics / internal run-of-show",
    "General network announcement / member update",
    "Unrelated or insufficient evidence",
]

DEFAULT_CATEGORIES_TEXT = "\n".join(DEFAULT_PRIMARY_CATEGORIES)

DEFAULT_PROJECT_CONTEXT = """This evidence set concerns the Partnership for the Future of Learning, also known as PFL, a national education and social justice network connected to public education policy, narrative strategy, community schools, education resourcing, teacher/educator workforce issues, federal COVID/ARP funding, place-based strategy, and efforts to defend public education from vouchers, privatization, and political attacks.

Use this background only to understand likely terminology and context. Do not assume every file is about PFL. Do not write a generic statement that a file is “about PFL” unless the file’s text, visible image content, or metadata supports that.

Your task is to classify and summarize the individual file itself. Describe what the file contains, why it may matter in an evidence set, and whether the description is based on extracted text, visible image text, metadata, filename, or a mix of those sources."""

CATEGORY_DEFINITIONS_TEXT = """Category definitions:

Network assembly / convening materials:
Use for event decks, assembly programs, meeting materials, convening slides, or documents created for a specific PFL convening or assembly. Do not use for participant lists or travel logistics.

Participant / attendee lists:
Use for rosters of people attending a specific event, assembly, retreat, council meeting, or convening.

People / organization directory:
Use for broad lists of people, organizations, roles, affiliations, or contact information that are not limited to one specific event.

Network strategy / governance / regeneration:
Use for documents about PFL’s overall network structure, Strategy Council, Steering Committee, governance model, Strategy Regeneration, network model, decision-making, power sharing, or long-term network organization.

Work group / priority-area planning:
Use for documents about a specific PFL work group, priority group, or recurring internal planning space that is not better classified under a topical category like Community Schools, Shared Story, or Education Resourcing.

Policy framework / policy agenda:
Use for policy frameworks, model legislation, policy agendas, formal policy recommendations, or policy toolkits.

Community schools:
Use for documents primarily about community schools, full-service community schools, community school policy, story strategy, financing, implementation, or writing-group work.

Shared Story / narrative / communications strategy:
Use for documents primarily about narrative strategy, framing, storytelling, communications strategy, story production, Shared Story, circle calls, messaging guidance, communications toolkits, or media strategy.

Education resourcing / school funding:
Use for documents primarily about school funding, education resourcing, adequate/equitable funding, state/local/federal school finance, public education funding narratives, or school finance advocacy.

Teacher / educator workforce:
Use for documents primarily about teachers, educator workforce, teacher diversity, educator preparation, recruitment, retention, profession strategy, or educator pipeline issues.

Federal funding / ARP / COVID response:
Use for documents primarily about ARP, ESSER, CARES, CRRSA, COVID response, federal relief funding, emergency response, or federal funding strategy.

Place-based / Key Places strategy:
Use for documents primarily about Key Places, place-based strategy, state/local partnerships, regional ecosystem work, local coalition strategy, or place-based network development.

Research / evaluation / findings report:
Use for formal research reports, evaluation reports, findings memos, landscape scans, survey findings, research summaries, or evidence reviews.

Public education defense / voucher-privatization response:
Use for documents, screenshots, graphics, or campaign materials focused on vouchers, privatization, attacks on public education, parent-rights campaigns, book bans, anti-CRT/anti-DEI attacks, or defending public schools from political/campaign threats.

Administrative logistics / internal run-of-show:
Use for travel support, reimbursement instructions, hotel/flight logistics, payment setup, internal staff run-of-show documents, operational checklists, or meeting production notes. Do not use this category merely because a document contains an agenda.

General network announcement / member update:
Use for email announcements, newsletters, member updates, leadership announcements, event invitations, or general network communications that are not primarily one of the topical categories above.

Unrelated or insufficient evidence:
Use only when the file does not appear related to PFL or when the available text, visible image content, metadata, and filename are too limited to classify confidently."""

CATEGORY_PRECEDENCE_RULES_TEXT = """Classification precedence rules:
1. If the file is a participant list for a specific event, choose Participant / attendee lists, even if it mentions strategy, assemblies, organizations, or network members.
2. If the file is a broad directory of people or organizations not tied to one event, choose People / organization directory.
3. If the file is travel, reimbursement, booking, payment setup, hotel block, transportation, or meeting production logistics, choose Administrative logistics / internal run-of-show.
4. If the file is about PFL’s network model, governance, Strategy Council, Steering Committee, Strategy Regeneration, power sharing, work group structure, funder group, or long-term network organization, choose Network strategy / governance / regeneration.
5. If the file is about vouchers, privatization, or defending public education from political attacks, choose Public education defense / voucher-privatization response, even if school funding is also mentioned.
6. If the file is about ARP, ESSER, CARES, CRRSA, or COVID relief funding, choose Federal funding / ARP / COVID response.
7. If the file is primarily about community schools, choose Community schools unless the document is mainly a formal evaluation, findings report, or research report.
8. If the file is a formal evaluation, findings report, landscape scan, or research report, choose Research / evaluation / findings report unless a more specific category is clearly dominant.
9. If the file is a general email, newsletter, invitation, or announcement and no more specific topical category dominates, choose General network announcement / member update.
10. If multiple categories apply, choose the category that best describes the document’s dominant purpose and likely evidentiary use."""

EVIDENCE_BASIS_VALUES = [
    "extracted text",
    "visible image text",
    "metadata only",
    "filename only",
    "mixed",
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

            CREATE TABLE IF NOT EXISTS ai_errors (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ai_job_id INTEGER NOT NULL,
                scan_id INTEGER NOT NULL,
                file_hash TEXT,
                file_name TEXT,
                source_file_path TEXT,
                error TEXT NOT NULL,
                stage TEXT,
                created_at TEXT,
                FOREIGN KEY(ai_job_id) REFERENCES ai_jobs(id) ON DELETE CASCADE,
                FOREIGN KEY(scan_id) REFERENCES scans(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_ai_errors_job ON ai_errors(ai_job_id, id);
            CREATE INDEX IF NOT EXISTS idx_ai_errors_scan ON ai_errors(scan_id, id);

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
        _ensure_column(conn, "ai_results", "secondary_tags", "secondary_tags TEXT")
        _ensure_column(conn, "ai_results", "confidence", "confidence REAL")
        _ensure_column(conn, "ai_results", "evidence_basis", "evidence_basis TEXT")
        _ensure_column(conn, "ai_results", "key_people", "key_people TEXT")
        _ensure_column(conn, "ai_results", "key_organizations", "key_organizations TEXT")
        _ensure_column(conn, "ai_results", "date_or_event", "date_or_event TEXT")
        _ensure_column(conn, "ai_results", "why_useful_as_evidence", "why_useful_as_evidence TEXT")
        _ensure_column(conn, "ai_results", "needs_human_review", "needs_human_review INTEGER DEFAULT 0")
        _ensure_column(conn, "ai_results", "category_valid", "category_valid INTEGER DEFAULT 1")
        _ensure_column(conn, "ai_results", "original_category", "original_category TEXT")
        _ensure_column(conn, "ai_results", "image_sent", "image_sent INTEGER DEFAULT 0")
        _ensure_column(conn, "ai_results", "ocr_status", "ocr_status TEXT")
        _ensure_column(conn, "ai_errors", "file_name", "file_name TEXT")
        _ensure_column(conn, "ai_errors", "source_file_path", "source_file_path TEXT")
        _ensure_column(conn, "ai_errors", "stage", "stage TEXT")
        conn.commit()


def dict_rows(rows: Iterable[sqlite3.Row]) -> list[dict]:
    return [dict(row) for row in rows]
