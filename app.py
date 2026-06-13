from __future__ import annotations

import csv
import io
import shutil
import time
import threading
import uuid
from datetime import datetime
from pathlib import Path
from queue import Queue
from typing import Any

from flask import Flask, Response, jsonify, redirect, render_template, request, url_for
from werkzeug.utils import secure_filename

from ai_categorizer import categorize_file, normalize_categories_text
from common import (
    AI_ERROR_EXPORT_HEADERS,
    AI_EXPORT_HEADERS,
    AI_MAX_CONSECUTIVE_ERRORS,
    CORE_EXPORT_HEADERS,
    DEFAULT_CATEGORIES_TEXT,
    DEFAULT_PROJECT_CONTEXT,
    EXTRACT_MAX_CHARS,
    HASH_ALGO,
    AI_SEND_IMAGES,
    IMAGE_MAX_BYTES,
    OLLAMA_MODEL,
    UPLOAD_DIR,
    get_db,
    init_db,
)
from evidence_scanner import parse_paths, scan_files

APP_PATCH_ID = "2026-06-13-ui-category-confidence-v1"

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 20 * 1024 * 1024 * 1024  # 20GB; local-only app, override in deployment if needed.

# Phase 1 scan jobs are short-lived in-memory progress records.
JOBS: dict[str, dict[str, Any]] = {}
JOBS_LOCK = threading.Lock()

# Phase 2 categorization jobs are persisted in SQLite and run through one FIFO worker.
AI_QUEUE: Queue[int] = Queue()
AI_WORKER_LOCK = threading.Lock()
AI_WORKER_STARTED = False
AI_SUCCESS_STATUSES = {"ok", "parsed_fields", "category_retry_ok", "category_normalized", "category_repaired"}
AI_TERMINAL_STATUSES = {"done", "failed", "cancelled", "interrupted"}


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def create_job(kind: str, message: str = "Queued") -> str:
    job_id = str(uuid.uuid4())
    with JOBS_LOCK:
        JOBS[job_id] = {
            "id": job_id,
            "kind": kind,
            "status": "queued",
            "message": message,
            "current": 0,
            "total": 0,
            "scan_id": None,
            "started_at": now_str(),
            "finished_at": None,
            "errors": [],
            "extra": {},
        }
    return job_id


def update_job(job_id: str, **updates: Any) -> None:
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            return
        for key, value in updates.items():
            if key == "error_append":
                job.setdefault("errors", []).append(value)
                job["errors"] = job["errors"][-50:]
            elif key == "extra_update":
                job.setdefault("extra", {}).update(value)
            else:
                job[key] = value


def make_scan(label: str, source_paths: str) -> int:
    with get_db() as conn:
        cur = conn.execute(
            """
            INSERT INTO scans(label, source_paths, status, started_at, hash_algo)
            VALUES (?, ?, 'queued', ?, ?)
            """,
            (label, source_paths, now_str(), HASH_ALGO),
        )
        return int(cur.lastrowid)


def finalize_scan_counts(conn, scan_id: int) -> None:
    row = conn.execute(
        """
        SELECT COUNT(*) AS file_count,
               COUNT(DISTINCT NULLIF(file_hash, '')) AS unique_count
        FROM files
        WHERE scan_id = ?
        """,
        (scan_id,),
    ).fetchone()
    error_count = conn.execute(
        "SELECT COUNT(*) AS n FROM scan_errors WHERE scan_id = ?",
        (scan_id,),
    ).fetchone()["n"]
    file_count = int(row["file_count"] or 0)
    unique_count = int(row["unique_count"] or 0)
    duplicate_count = max(0, file_count - unique_count)
    conn.execute(
        """
        UPDATE scans
        SET file_count = ?, unique_count = ?, duplicate_count = ?, error_count = ?,
            status = 'done', finished_at = ?
        WHERE id = ?
        """,
        (file_count, unique_count, duplicate_count, int(error_count), now_str(), scan_id),
    )


def run_scan_job(job_id: str, scan_id: int, raw_paths: str) -> None:
    update_job(job_id, status="running", scan_id=scan_id, message="Scanning files and calculating hashes...")
    paths = parse_paths(raw_paths)
    seen_paths: set[str] = set()

    with get_db() as conn:
        conn.execute("UPDATE scans SET status = 'running', started_at = ? WHERE id = ?", (now_str(), scan_id))
        conn.commit()

        def on_error(path: str, error: str) -> None:
            conn.execute(
                "INSERT INTO scan_errors(scan_id, path, error, created_at) VALUES (?, ?, ?, ?)",
                (scan_id, path, error, now_str()),
            )
            update_job(job_id, error_append={"path": path, "error": error})

        def progress(path: str, current: int, total: int) -> None:
            update_job(job_id, current=current, total=total, message=f"Scanning: {path}")

        inserted = 0
        try:
            for rec in scan_files(paths, algo=HASH_ALGO, progress=progress, on_error=on_error):
                if rec.full_path in seen_paths:
                    continue
                seen_paths.add(rec.full_path)
                conn.execute(
                    """
                    INSERT INTO files(
                        scan_id, file_name, file_type, folder_location, full_path,
                        last_modified_date, creation_date, size_mb, size_bytes,
                        file_hash, hash_algo
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        scan_id,
                        rec.file_name,
                        rec.file_type,
                        rec.folder_location,
                        rec.full_path,
                        rec.last_modified_date,
                        rec.creation_date,
                        rec.size_mb,
                        rec.size_bytes,
                        rec.file_hash,
                        rec.hash_algo,
                    ),
                )
                inserted += 1
                if inserted % 100 == 0:
                    conn.commit()
                    update_job(job_id, extra_update={"inserted": inserted})
            conn.commit()
            finalize_scan_counts(conn, scan_id)
            conn.commit()
            row = conn.execute("SELECT * FROM scans WHERE id = ?", (scan_id,)).fetchone()
            update_job(
                job_id,
                status="done",
                current=int(row["file_count"] or inserted),
                total=int(row["file_count"] or inserted),
                finished_at=now_str(),
                message=f"Scan complete: {row['file_count']} files, {row['unique_count']} unique hashes.",
                extra_update={
                    "file_count": row["file_count"],
                    "unique_count": row["unique_count"],
                    "duplicate_count": row["duplicate_count"],
                    "error_count": row["error_count"],
                },
            )
        except Exception as e:
            conn.execute("UPDATE scans SET status = 'failed', finished_at = ? WHERE id = ?", (now_str(), scan_id))
            conn.commit()
            update_job(
                job_id,
                status="failed",
                finished_at=now_str(),
                message=f"Scan failed: {type(e).__name__}: {e}",
                error_append={"path": "", "error": f"{type(e).__name__}: {e}"},
            )


def latest_scan_id() -> int | None:
    with get_db() as conn:
        row = conn.execute("SELECT id FROM scans ORDER BY id DESC LIMIT 1").fetchone()
        return int(row["id"]) if row else None


def scan_id_for_ai_job(ai_job_id: int) -> int | None:
    with get_db() as conn:
        row = conn.execute("SELECT scan_id FROM ai_jobs WHERE id = ?", (ai_job_id,)).fetchone()
        return int(row["scan_id"]) if row else None


def _fetch_ai_jobs(conn, limit: int = 25) -> list[Any]:
    return conn.execute(
        """
        SELECT j.*, s.label AS scan_label
        FROM ai_jobs j
        LEFT JOIN scans s ON s.id = j.scan_id
        ORDER BY j.id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()


def fetch_dashboard(scan_id: int | None, selected_ai_job_id: int | None = None) -> dict[str, Any]:
    with get_db() as conn:
        scans = conn.execute("SELECT * FROM scans ORDER BY id DESC LIMIT 15").fetchall()
        ai_jobs = _fetch_ai_jobs(conn)
        selected_ai_job = None
        selected_ai_job_errors = []
        if selected_ai_job_id:
            selected_ai_job = conn.execute(
                """
                SELECT j.*, s.label AS scan_label
                FROM ai_jobs j
                LEFT JOIN scans s ON s.id = j.scan_id
                WHERE j.id = ?
                """,
                (selected_ai_job_id,),
            ).fetchone()
            if selected_ai_job:
                selected_ai_job_errors = conn.execute(
                    """
                    SELECT *
                    FROM ai_errors
                    WHERE ai_job_id = ?
                    ORDER BY id DESC
                    LIMIT 200
                    """,
                    (selected_ai_job_id,),
                ).fetchall()

        scan = None
        files = []
        duplicate_groups = []
        errors = []
        ai_counts = []
        if scan_id:
            scan = conn.execute("SELECT * FROM scans WHERE id = ?", (scan_id,)).fetchone()
            if scan:
                files = conn.execute(
                    """
                    SELECT f.*,
                           a.category AS ai_category,
                           a.secondary_tags AS ai_secondary_tags,
                           a.confidence AS ai_confidence,
                           a.description AS ai_description,
                           a.evidence_basis AS ai_evidence_basis,
                           a.key_people AS ai_key_people,
                           a.key_organizations AS ai_key_organizations,
                           a.date_or_event AS ai_date_or_event,
                           a.why_useful_as_evidence AS ai_why_useful_as_evidence,
                           a.needs_human_review AS ai_needs_human_review,
                           a.status AS ai_status
                    FROM files f
                    LEFT JOIN ai_results a
                      ON a.scan_id = f.scan_id AND a.file_hash = f.file_hash
                    WHERE f.scan_id = ?
                    ORDER BY f.id
                    LIMIT 1000
                    """,
                    (scan_id,),
                ).fetchall()
                duplicate_groups = conn.execute(
                    """
                    SELECT file_hash, COUNT(*) AS copies, MIN(file_name) AS example_name
                    FROM files
                    WHERE scan_id = ? AND file_hash <> ''
                    GROUP BY file_hash
                    HAVING COUNT(*) > 1
                    ORDER BY copies DESC, example_name ASC
                    LIMIT 25
                    """,
                    (scan_id,),
                ).fetchall()
                errors = conn.execute(
                    "SELECT * FROM scan_errors WHERE scan_id = ? ORDER BY id DESC LIMIT 25",
                    (scan_id,),
                ).fetchall()
                ai_counts = conn.execute(
                    """
                    SELECT COALESCE(status, 'not_started') AS status, COUNT(*) AS n
                    FROM (
                        SELECT DISTINCT f.file_hash, a.status
                        FROM files f
                        LEFT JOIN ai_results a ON a.scan_id = f.scan_id AND a.file_hash = f.file_hash
                        WHERE f.scan_id = ? AND f.file_hash <> ''
                    )
                    GROUP BY COALESCE(status, 'not_started')
                    ORDER BY status
                    """,
                    (scan_id,),
                ).fetchall()
        return {
            "scans": scans,
            "ai_jobs": ai_jobs,
            "selected_ai_job": selected_ai_job,
            "selected_ai_job_errors": selected_ai_job_errors,
            "scan": scan,
            "files": files,
            "duplicate_groups": duplicate_groups,
            "errors": errors,
            "ai_counts": ai_counts,
            "default_model": OLLAMA_MODEL,
            "default_max_chars": EXTRACT_MAX_CHARS,
            "default_categories_text": DEFAULT_CATEGORIES_TEXT,
            "default_project_context": DEFAULT_PROJECT_CONTEXT,
            "ai_send_images": AI_SEND_IMAGES,
            "image_max_bytes": IMAGE_MAX_BYTES,
            "hash_algo": HASH_ALGO,
            "ai_terminal_statuses": AI_TERMINAL_STATUSES,
        }




@app.route("/debug/version")
def debug_version():
    """Small local diagnostic endpoint to confirm the replacement files are loaded."""
    import ollama_client

    return jsonify(
        {
            "ok": True,
            "patch_id": APP_PATCH_ID,
            "app_file": str(Path(__file__).resolve()),
            "ollama_client_file": str(Path(ollama_client.__file__).resolve()),
            "ollama_think": getattr(ollama_client, "OLLAMA_THINK", None),
            "ollama_endpoints": getattr(ollama_client, "NORMALIZED_OLLAMA_ENDPOINTS", []),
            "ai_send_images": AI_SEND_IMAGES,
            "image_max_bytes": IMAGE_MAX_BYTES,
        }
    )


@app.route("/debug/ollama_test")
def debug_ollama_test():
    """Run one tiny Ollama call so model/endpoint/thinking problems are easy to isolate."""
    from ollama_client import chat

    model = (request.args.get("model") or OLLAMA_MODEL or "").strip()
    if not model:
        return jsonify({"ok": False, "error": "No model supplied and OLLAMA_MODEL is blank."}), 400

    messages = [
        {
            "role": "system",
            "content": "You are a diagnostic endpoint. Return compact JSON only. Do not think. Do not explain.",
        },
        {
            "role": "user",
            "content": 'Return exactly this JSON object and nothing else: {"category":"diagnostic","description":"Ollama API test succeeded."}',
        },
    ]
    started = time.perf_counter()
    try:
        raw = chat(
            messages,
            model=model,
            temperature=0.0,
            num_predict=256,
            timeout=90,
            response_format="json",
            retries=0,
            think=False,
        )
        return jsonify(
            {
                "ok": True,
                "patch_id": APP_PATCH_ID,
                "model": model,
                "seconds": round(time.perf_counter() - started, 2),
                "response": raw,
            }
        )
    except Exception as e:
        return (
            jsonify(
                {
                    "ok": False,
                    "patch_id": APP_PATCH_ID,
                    "model": model,
                    "seconds": round(time.perf_counter() - started, 2),
                    "error_type": type(e).__name__,
                    "error": str(e),
                }
            ),
            500,
        )


@app.route("/")
def home():
    requested = request.args.get("scan_id", type=int)
    selected_ai_job_id = request.args.get("ai_job_id", type=int)
    if selected_ai_job_id and not requested:
        requested = scan_id_for_ai_job(selected_ai_job_id)
    scan_id = requested or latest_scan_id()
    return render_template("index.html", **fetch_dashboard(scan_id, selected_ai_job_id))


@app.route("/start_scan", methods=["POST"])
def start_scan():
    raw_paths = request.form.get("paths", "")
    label = request.form.get("label", "").strip() or f"Scan {now_str()}"
    if not raw_paths.strip():
        return jsonify({"ok": False, "error": "Enter at least one file or folder path."}), 400
    scan_id = make_scan(label, raw_paths)
    job_id = create_job("scan", "Queued scan")
    update_job(job_id, scan_id=scan_id)
    thread = threading.Thread(target=run_scan_job, args=(job_id, scan_id, raw_paths), daemon=True)
    thread.start()
    return jsonify({"ok": True, "job_id": job_id, "scan_id": scan_id})


def _safe_upload_path(base: Path, browser_filename: str) -> Path:
    # Preserve relative folders from webkitdirectory while sanitizing each segment.
    raw_parts = Path(browser_filename.replace("\\", "/")).parts
    safe_parts = [secure_filename(part) for part in raw_parts if secure_filename(part)]
    if not safe_parts:
        safe_parts = [f"uploaded_{uuid.uuid4().hex}"]
    return base.joinpath(*safe_parts)


@app.route("/upload_and_scan", methods=["POST"])
def upload_and_scan():
    files = request.files.getlist("evidence_files") + request.files.getlist("evidence_folder_files")
    files = [f for f in files if f and f.filename]
    if not files:
        return jsonify({"ok": False, "error": "Choose at least one file or folder upload."}), 400
    batch = UPLOAD_DIR / datetime.now().strftime("%Y%m%d_%H%M%S") / uuid.uuid4().hex[:8]
    batch.mkdir(parents=True, exist_ok=True)
    saved = 0
    for f in files:
        dest = _safe_upload_path(batch, f.filename)
        dest.parent.mkdir(parents=True, exist_ok=True)
        f.save(dest)
        saved += 1
    label = request.form.get("upload_label", "").strip() or f"Uploaded scan {now_str()}"
    scan_id = make_scan(label, str(batch))
    job_id = create_job("scan", f"Uploaded {saved} files; queued scan")
    update_job(job_id, scan_id=scan_id, extra_update={"uploaded_files": saved})
    thread = threading.Thread(target=run_scan_job, args=(job_id, scan_id, str(batch)), daemon=True)
    thread.start()
    return jsonify({"ok": True, "job_id": job_id, "scan_id": scan_id})


def unique_files_for_ai(conn, scan_id: int, force: bool, max_files: int | None) -> list[Any]:
    rows = conn.execute(
        """
        SELECT f.*,
               d.copies AS duplicate_count,
               a.status AS existing_status
        FROM files f
        JOIN (
            SELECT MIN(id) AS first_id, file_hash, COUNT(*) AS copies
            FROM files
            WHERE scan_id = ? AND file_hash <> ''
            GROUP BY file_hash
        ) d ON d.first_id = f.id
        LEFT JOIN ai_results a ON a.scan_id = f.scan_id AND a.file_hash = f.file_hash
        WHERE f.scan_id = ?
        ORDER BY f.id
        """,
        (scan_id, scan_id),
    ).fetchall()
    filtered = []
    for row in rows:
        if not force and row["existing_status"] in AI_SUCCESS_STATUSES:
            continue
        filtered.append(row)
        if max_files and len(filtered) >= max_files:
            break
    return filtered


def create_ai_job_db(
    *,
    scan_id: int,
    categories: str,
    context: str,
    model: str,
    max_chars: int,
    max_files: int | None,
    force: bool,
) -> int:
    with get_db() as conn:
        cur = conn.execute(
            """
            INSERT INTO ai_jobs(
                scan_id, status, message, categories, context, model, max_chars, max_files, force, created_at
            ) VALUES (?, 'queued', ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                scan_id,
                "Queued; waiting for the categorization worker.",
                categories,
                context,
                model,
                int(max_chars),
                int(max_files) if max_files else None,
                1 if force else 0,
                now_str(),
            ),
        )
        return int(cur.lastrowid)


def _log_ai_error(
    conn,
    *,
    ai_job_id: int,
    scan_id: int,
    file_hash: str | None = None,
    file_name: str | None = None,
    source_file_path: str | None = None,
    error: str,
    stage: str = "ollama",
) -> None:
    conn.execute(
        """
        INSERT INTO ai_errors(ai_job_id, scan_id, file_hash, file_name, source_file_path, error, stage, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (ai_job_id, scan_id, file_hash or "", file_name or "", source_file_path or "", error, stage, now_str()),
    )


def _ai_cancel_requested(conn, ai_job_id: int) -> bool:
    row = conn.execute("SELECT cancel_requested, status FROM ai_jobs WHERE id = ?", (ai_job_id,)).fetchone()
    if not row:
        return True
    return bool(row["cancel_requested"]) or row["status"] == "cancelled"


def _set_ai_job_cancelled(conn, ai_job_id: int, current: int | None = None, total: int | None = None) -> None:
    conn.execute(
        """
        UPDATE ai_jobs
        SET status = 'cancelled', message = ?, current = COALESCE(?, current), total = COALESCE(?, total),
            finished_at = COALESCE(finished_at, ?)
        WHERE id = ?
        """,
        ("Cancelled by user.", current, total, now_str(), ai_job_id),
    )
    conn.commit()


def run_ai_job_by_id(ai_job_id: int) -> None:
    with get_db() as conn:
        job = conn.execute("SELECT * FROM ai_jobs WHERE id = ?", (ai_job_id,)).fetchone()
        if not job:
            return
        if job["status"] == "cancelled" or job["cancel_requested"]:
            _set_ai_job_cancelled(conn, ai_job_id)
            return

        scan_id = int(job["scan_id"])
        scan = conn.execute("SELECT * FROM scans WHERE id = ?", (scan_id,)).fetchone()
        if not scan:
            error_text = "Scan not found."
            conn.execute(
                "UPDATE ai_jobs SET status = 'failed', message = ?, last_error = ?, error_count = error_count + 1, finished_at = ? WHERE id = ?",
                (error_text, error_text, now_str(), ai_job_id),
            )
            _log_ai_error(conn, ai_job_id=ai_job_id, scan_id=scan_id, error=error_text, stage="setup")
            conn.commit()
            return

        conn.execute(
            """
            UPDATE ai_jobs
            SET status = 'running', started_at = COALESCE(started_at, ?), message = ?
            WHERE id = ?
            """,
            (now_str(), "Preparing unique files for Ollama...", ai_job_id),
        )
        conn.commit()

        force = bool(job["force"])
        max_files = int(job["max_files"]) if job["max_files"] else None
        max_chars = int(job["max_chars"] or EXTRACT_MAX_CHARS)
        files = unique_files_for_ai(conn, scan_id, force, max_files)
        total = len(files)
        conn.execute(
            "UPDATE ai_jobs SET total = ?, current = 0, message = ? WHERE id = ?",
            (total, f"Categorizing {total} unique files...", ai_job_id),
        )
        conn.commit()

        if total == 0:
            conn.execute(
                "UPDATE ai_jobs SET status = 'done', message = ?, finished_at = ? WHERE id = ?",
                ("No uncategorized unique files to process.", now_str(), ai_job_id),
            )
            conn.commit()
            return

        consecutive_errors = 0
        for idx, row in enumerate(files, start=1):
            if _ai_cancel_requested(conn, ai_job_id):
                _set_ai_job_cancelled(conn, ai_job_id, current=idx - 1, total=total)
                return

            conn.execute(
                "UPDATE ai_jobs SET status = 'running', current = ?, total = ?, message = ? WHERE id = ?",
                (idx - 1, total, f"Sending to Ollama: {row['file_name']}", ai_job_id),
            )
            metadata = {
                "file_name": row["file_name"],
                "file_type": row["file_type"],
                "folder_location": row["folder_location"],
                "last_modified_date": row["last_modified_date"],
                "size_mb": row["size_mb"],
                "hash": row["file_hash"],
                "hash_algorithm": row["hash_algo"],
                "duplicate_count_in_scan": row["duplicate_count"],
            }
            conn.execute(
                """
                INSERT INTO ai_results(scan_id, file_hash, source_file_id, source_file_path, status, updated_at, model)
                VALUES (?, ?, ?, ?, 'running', ?, ?)
                ON CONFLICT(scan_id, file_hash) DO UPDATE SET
                    source_file_id = excluded.source_file_id,
                    source_file_path = excluded.source_file_path,
                    status = 'running',
                    error = NULL,
                    updated_at = excluded.updated_at,
                    model = excluded.model,
                    original_category = NULL,
                    category_valid = 1
                """,
                (scan_id, row["file_hash"], row["id"], row["full_path"], now_str(), job["model"]),
            )
            conn.commit()

            try:
                result = categorize_file(
                    path=row["full_path"],
                    file_metadata=metadata,
                    categories=job["categories"] or "",
                    context=job["context"] or "",
                    model=(job["model"] or None),
                    max_chars=max_chars,
                )
                status = result.get("status") or "ok"
                conn.execute(
                    """
                    UPDATE ai_results
                    SET category = ?, secondary_tags = ?, confidence = ?, description = ?, evidence_basis = ?,
                        key_people = ?, key_organizations = ?, date_or_event = ?, why_useful_as_evidence = ?,
                        needs_human_review = ?, original_category = ?, category_valid = ?,
                        status = ?, error = ?, extracted_chars = ?, updated_at = ?,
                        raw_response = ?, model = ?, extraction_status = ?, image_sent = ?, ocr_status = ?
                    WHERE scan_id = ? AND file_hash = ?
                    """,
                    (
                        result.get("primary_category") or result.get("category"),
                        result.get("secondary_tags") or "",
                        result.get("confidence"),
                        result.get("description"),
                        result.get("evidence_basis") or "",
                        result.get("key_people") or "",
                        result.get("key_organizations") or "",
                        result.get("date_or_event") or "",
                        result.get("why_useful_as_evidence") or "",
                        1 if result.get("needs_human_review") else 0,
                        result.get("original_category") or "",
                        int(result.get("category_valid") if result.get("category_valid") is not None else 1),
                        status,
                        result.get("extraction_error") or "",
                        int(result.get("extracted_chars") or 0),
                        now_str(),
                        result.get("raw_response") or "",
                        job["model"],
                        result.get("extraction_status") or "",
                        int(result.get("image_sent") or 0),
                        result.get("ocr_status") or "",
                        scan_id,
                        row["file_hash"],
                    ),
                )
                consecutive_errors = 0
                conn.commit()
            except Exception as e:
                consecutive_errors += 1
                error_text = f"{type(e).__name__}: {e}"
                conn.execute(
                    """
                    UPDATE ai_results
                    SET status = 'error', error = ?, updated_at = ?
                    WHERE scan_id = ? AND file_hash = ?
                    """,
                    (error_text, now_str(), scan_id, row["file_hash"]),
                )
                _log_ai_error(
                    conn,
                    ai_job_id=ai_job_id,
                    scan_id=scan_id,
                    file_hash=row["file_hash"],
                    file_name=row["file_name"],
                    source_file_path=row["full_path"],
                    error=error_text,
                    stage="categorize_file",
                )
                conn.execute(
                    """
                    UPDATE ai_jobs
                    SET error_count = error_count + 1, last_error = ?, message = ?
                    WHERE id = ?
                    """,
                    (error_text, f"Error on {row['file_name']}: {error_text[:220]}", ai_job_id),
                )
                conn.commit()

                if AI_MAX_CONSECUTIVE_ERRORS > 0 and consecutive_errors >= AI_MAX_CONSECUTIVE_ERRORS:
                    stop_message = (
                        f"Stopped after {consecutive_errors} consecutive Ollama errors. "
                        "Open the error log below, fix the endpoint/model, then submit another categorization run."
                    )
                    conn.execute(
                        """
                        UPDATE ai_jobs
                        SET status = 'failed', current = ?, total = ?, message = ?, finished_at = ?
                        WHERE id = ?
                        """,
                        (idx, total, stop_message, now_str(), ai_job_id),
                    )
                    conn.commit()
                    return

            conn.execute(
                "UPDATE ai_jobs SET current = ?, total = ?, message = ? WHERE id = ?",
                (idx, total, f"Processed {idx} of {total} unique files", ai_job_id),
            )
            conn.commit()

        conn.execute(
            "UPDATE ai_jobs SET status = 'done', current = ?, total = ?, message = ?, finished_at = ? WHERE id = ?",
            (total, total, f"Ollama categorization complete: {total} unique files processed.", now_str(), ai_job_id),
        )
        conn.commit()


def ai_worker_loop() -> None:
    while True:
        ai_job_id = AI_QUEUE.get()
        try:
            run_ai_job_by_id(ai_job_id)
        except Exception as e:  # Keep worker alive even if a job trips an unexpected bug.
            error_text = f"{type(e).__name__}: {e}"
            with get_db() as conn:
                job = conn.execute("SELECT scan_id FROM ai_jobs WHERE id = ?", (ai_job_id,)).fetchone()
                scan_id = int(job["scan_id"]) if job else 0
                if job:
                    _log_ai_error(conn, ai_job_id=ai_job_id, scan_id=scan_id, error=error_text, stage="worker")
                conn.execute(
                    "UPDATE ai_jobs SET status = 'failed', message = ?, last_error = ?, error_count = error_count + 1, finished_at = ? WHERE id = ?",
                    (f"Categorization worker failed: {error_text}", error_text, now_str(), ai_job_id),
                )
                conn.commit()
        finally:
            AI_QUEUE.task_done()


def start_ai_worker_once() -> None:
    global AI_WORKER_STARTED
    with AI_WORKER_LOCK:
        if AI_WORKER_STARTED:
            return
        thread = threading.Thread(target=ai_worker_loop, daemon=True, name="evidence-tool-ai-worker")
        thread.start()
        AI_WORKER_STARTED = True


def enqueue_pending_ai_jobs() -> None:
    """Put queued database jobs back into the in-memory FIFO queue after app startup."""
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT id
            FROM ai_jobs
            WHERE status = 'queued' AND cancel_requested = 0
            ORDER BY id
            """
        ).fetchall()
    for row in rows:
        AI_QUEUE.put(int(row["id"]))


def _categories_from_uploaded_csv() -> str:
    """Return category definition lines from an optional two-column CSV upload."""
    uploaded = request.files.get("category_csv")
    if not uploaded or not uploaded.filename:
        return ""
    try:
        raw = uploaded.read()
        text = raw.decode("utf-8-sig", errors="replace")
        reader = csv.reader(io.StringIO(text))
        lines: list[str] = []
        for row in reader:
            if not row:
                continue
            name = str(row[0] if len(row) > 0 else "").strip()
            definition = str(row[1] if len(row) > 1 else "").strip()
            if not name:
                continue
            lowered = name.casefold()
            # Skip common header rows such as category,definition.
            if lowered in {"category", "category name", "primary category", "name"}:
                continue
            if definition:
                lines.append(f"{name}: {definition}")
            else:
                lines.append(name)
        return "\n".join(lines)
    except Exception as e:
        raise ValueError(f"Could not read category CSV: {type(e).__name__}: {e}") from e


@app.route("/start_ai", methods=["POST"])
def start_ai():
    scan_id = request.form.get("scan_id", type=int)
    if not scan_id:
        return jsonify({"ok": False, "error": "No scan selected."}), 400
    try:
        uploaded_categories = _categories_from_uploaded_csv()
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    raw_categories = uploaded_categories or request.form.get("categories", "").strip() or DEFAULT_CATEGORIES_TEXT
    categories = normalize_categories_text(raw_categories)
    context = request.form.get("context", "").strip()
    model = request.form.get("model", "").strip() or OLLAMA_MODEL
    force = request.form.get("force") in {"on", "true", "1", "yes"}
    try:
        max_chars = int(request.form.get("max_chars") or EXTRACT_MAX_CHARS)
    except ValueError:
        max_chars = EXTRACT_MAX_CHARS
    try:
        max_files_raw = request.form.get("max_files", "").strip()
        max_files = int(max_files_raw) if max_files_raw else None
    except ValueError:
        max_files = None

    ai_job_id = create_ai_job_db(
        scan_id=scan_id,
        categories=categories,
        context=context,
        model=model,
        max_chars=max_chars,
        max_files=max_files,
        force=force,
    )
    start_ai_worker_once()
    AI_QUEUE.put(ai_job_id)
    return jsonify({"ok": True, "job_id": f"ai-{ai_job_id}", "ai_job_id": ai_job_id, "scan_id": scan_id})


def _ai_job_to_client(row) -> dict[str, Any]:
    errors = []
    if row["last_error"]:
        errors.append({"path": "", "error": row["last_error"]})
    return {
        "id": f"ai-{row['id']}",
        "db_id": int(row["id"]),
        "kind": "ai",
        "status": row["status"],
        "message": row["message"] or "",
        "current": int(row["current"] or 0),
        "total": int(row["total"] or 0),
        "scan_id": int(row["scan_id"]),
        "started_at": row["started_at"] or row["created_at"],
        "finished_at": row["finished_at"],
        "errors": errors,
        "extra": {
            "model": row["model"],
            "error_count": int(row["error_count"] or 0),
            "cancel_requested": bool(row["cancel_requested"]),
            "error_log_url": url_for("ai_job_errors_page", ai_job_id=int(row["id"])),
            "log_url": url_for("ai_job_errors_page", ai_job_id=int(row["id"])),
            "settings_url": url_for("home", scan_id=int(row["scan_id"]), ai_job_id=int(row["id"])),
        },
    }


def _ai_job_row_to_dict(row) -> dict[str, Any]:
    status = row["status"] or ""
    return {
        "id": int(row["id"]),
        "scan_id": int(row["scan_id"]),
        "scan_label": row["scan_label"] or f"Scan {row['scan_id']}",
        "status": status,
        "message": row["message"] or "",
        "current": int(row["current"] or 0),
        "total": int(row["total"] or 0),
        "model": row["model"] or "",
        "error_count": int(row["error_count"] or 0),
        "last_error": row["last_error"] or "",
        "created_at": row["created_at"] or "",
        "started_at": row["started_at"] or "",
        "finished_at": row["finished_at"] or "",
        "cancellable": status not in AI_TERMINAL_STATUSES,
        "terminal": status in AI_TERMINAL_STATUSES,
        "load_url": url_for("home", scan_id=int(row["scan_id"]), ai_job_id=int(row["id"])),
        "settings_url": url_for("home", scan_id=int(row["scan_id"]), ai_job_id=int(row["id"])),
        "scan_url": url_for("home", scan_id=int(row["scan_id"])),
        "cancel_url": url_for("cancel_ai_job", ai_job_id=int(row["id"])),
        "error_url": url_for("ai_job_errors_page", ai_job_id=int(row["id"])),
        "log_url": url_for("ai_job_errors_page", ai_job_id=int(row["id"])),
    }


@app.route("/api/ai_jobs")
def api_ai_jobs():
    with get_db() as conn:
        rows = _fetch_ai_jobs(conn)
    return jsonify({"ok": True, "jobs": [_ai_job_row_to_dict(row) for row in rows]})


@app.route("/cancel_ai/<int:ai_job_id>", methods=["POST"])
def cancel_ai_job(ai_job_id: int):
    with get_db() as conn:
        job = conn.execute("SELECT * FROM ai_jobs WHERE id = ?", (ai_job_id,)).fetchone()
        if not job:
            if request.headers.get("X-Requested-With") == "fetch":
                return jsonify({"ok": False, "error": "Categorization job not found."}), 404
            return redirect(url_for("home"))
        if job["status"] == "queued":
            conn.execute(
                "UPDATE ai_jobs SET status = 'cancelled', cancel_requested = 1, message = ?, finished_at = ? WHERE id = ?",
                ("Cancelled before it started.", now_str(), ai_job_id),
            )
        elif job["status"] not in AI_TERMINAL_STATUSES:
            conn.execute(
                "UPDATE ai_jobs SET status = 'cancel_requested', cancel_requested = 1, message = ? WHERE id = ?",
                ("Cancel requested. The current Ollama request may finish before the job stops.", ai_job_id),
            )
        conn.commit()
        scan_id = int(job["scan_id"])
    if request.headers.get("X-Requested-With") == "fetch":
        return jsonify({"ok": True})
    return redirect(url_for("home", scan_id=scan_id, ai_job_id=ai_job_id))


@app.route("/job/<job_id>")
def job_status(job_id: str):
    if job_id.startswith("ai-"):
        try:
            ai_job_id = int(job_id.removeprefix("ai-"))
        except ValueError:
            return jsonify({"ok": False, "error": "Job not found."}), 404
        with get_db() as conn:
            row = conn.execute("SELECT * FROM ai_jobs WHERE id = ?", (ai_job_id,)).fetchone()
            if not row:
                return jsonify({"ok": False, "error": "Job not found."}), 404
            return jsonify({"ok": True, "job": _ai_job_to_client(row)})

    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            return jsonify({"ok": False, "error": "Job not found."}), 404
        return jsonify({"ok": True, "job": job})


def csv_response(filename: str, rows_iter, headers: list[str]) -> Response:
    def generate():
        buf = io.StringIO(newline="")
        writer = csv.writer(buf, lineterminator="\r\n")
        writer.writerow(headers)
        yield buf.getvalue()
        buf.seek(0)
        buf.truncate(0)
        for row in rows_iter():
            writer.writerow(row)
            yield buf.getvalue()
            buf.seek(0)
            buf.truncate(0)

    return Response(
        generate(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@app.route("/export/<int:scan_id>")
def export_scan(scan_id: int):
    def rows():
        with get_db() as conn:
            for row in conn.execute(
                """
                SELECT file_name, file_type, folder_location, last_modified_date,
                       size_mb, file_hash
                FROM files
                WHERE scan_id = ?
                ORDER BY id
                """,
                (scan_id,),
            ):
                yield [
                    row["file_name"],
                    row["file_type"],
                    row["folder_location"],
                    row["last_modified_date"],
                    f"{float(row['size_mb'] or 0):.1f}",
                    row["file_hash"],
                ]

    ts = datetime.now().strftime("%Y%m%d_%H%M")
    return csv_response(f"evidence_inventory_scan_{scan_id}_{ts}.csv", rows, CORE_EXPORT_HEADERS)


@app.route("/export_ai/<int:scan_id>")
def export_scan_ai(scan_id: int):
    def rows():
        with get_db() as conn:
            for row in conn.execute(
                """
                SELECT f.file_name, f.file_type, f.folder_location, f.last_modified_date,
                       f.size_mb, f.file_hash,
                       a.category, a.secondary_tags, a.confidence, a.description, a.evidence_basis,
                       a.key_people, a.key_organizations, a.date_or_event, a.why_useful_as_evidence,
                       a.needs_human_review, a.status AS ai_status
                FROM files f
                LEFT JOIN ai_results a
                  ON a.scan_id = f.scan_id AND a.file_hash = f.file_hash
                WHERE f.scan_id = ?
                ORDER BY f.id
                """,
                (scan_id,),
            ):
                yield [
                    row["file_name"],
                    row["file_type"],
                    row["folder_location"],
                    row["last_modified_date"],
                    f"{float(row['size_mb'] or 0):.1f}",
                    row["file_hash"],
                    row["category"] or "",
                    row["secondary_tags"] or "",
                    "" if row["confidence"] is None else f"{float(row['confidence']):.2f}",
                    row["description"] or "",
                    row["evidence_basis"] or "",
                    row["key_people"] or "",
                    row["key_organizations"] or "",
                    row["date_or_event"] or "",
                    row["why_useful_as_evidence"] or "",
                    "true" if row["needs_human_review"] else "false",
                    row["ai_status"] or "not_started",
                ]

    ts = datetime.now().strftime("%Y%m%d_%H%M")
    return csv_response(f"evidence_inventory_with_ai_scan_{scan_id}_{ts}.csv", rows, AI_EXPORT_HEADERS)


def load_ai_job_errors(ai_job_id: int) -> tuple[Any | None, list[Any]]:
    with get_db() as conn:
        job = conn.execute(
            """
            SELECT j.*, s.label AS scan_label
            FROM ai_jobs j
            LEFT JOIN scans s ON s.id = j.scan_id
            WHERE j.id = ?
            """,
            (ai_job_id,),
        ).fetchone()
        if not job:
            return None, []
        errors = conn.execute(
            """
            SELECT *
            FROM ai_errors
            WHERE ai_job_id = ?
            ORDER BY id ASC
            """,
            (ai_job_id,),
        ).fetchall()
        # Keep this log job-specific. Earlier versions showed old ai_results errors
        # from the same scan here, which made a running/new job look like it already
        # had failures from a previous categorization attempt.
        return job, errors


def build_ai_error_log_text(job, errors) -> str:
    if not job:
        return "Categorization job not found."
    lines = [
        f"Evidence Tool categorization error log",
        f"Job ID: {job['id']}",
        f"Scan: {job['scan_label'] or job['scan_id']} (ID {job['scan_id']})",
        f"Status: {job['status']}",
        f"Model: {job['model'] or ''}",
        f"Created: {job['created_at'] or ''}",
        f"Started: {job['started_at'] or ''}",
        f"Finished: {job['finished_at'] or ''}",
        f"Message: {job['message'] or ''}",
        f"Last error: {job['last_error'] or ''}",
        "",
        f"Stored error entries: {len(errors)}",
        "",
    ]
    for i, err in enumerate(errors, start=1):
        lines.extend(
            [
                f"--- Error {i} ---",
                f"Time: {err['created_at'] or ''}",
                f"Stage: {err['stage'] or ''}",
                f"File: {err['file_name'] or ''}",
                f"Path: {err['source_file_path'] or ''}",
                f"Hash: {err['file_hash'] or ''}",
                "Error:",
                err["error"] or "",
                "",
            ]
        )
    return "\n".join(lines)


@app.route("/ai_job/<int:ai_job_id>/errors")
def ai_job_errors_page(ai_job_id: int):
    job, errors = load_ai_job_errors(ai_job_id)
    if not job:
        return Response("Categorization job not found.", status=404, mimetype="text/plain")
    return render_template("ai_job_log.html", job=job, errors=errors, plain_text=build_ai_error_log_text(job, errors))


@app.route("/ai_job/<int:ai_job_id>/errors.txt")
def ai_job_errors_text(ai_job_id: int):
    job, errors = load_ai_job_errors(ai_job_id)
    if not job:
        return Response("Categorization job not found.", status=404, mimetype="text/plain; charset=utf-8")
    return Response(build_ai_error_log_text(job, errors), mimetype="text/plain; charset=utf-8")


@app.route("/ai_job/<int:ai_job_id>/errors.csv")
def export_ai_job_errors(ai_job_id: int):
    def rows():
        with get_db() as conn:
            for row in conn.execute(
                """
                SELECT id, ai_job_id, scan_id, created_at, file_name, source_file_path, file_hash, stage, error
                FROM ai_errors
                WHERE ai_job_id = ?
                ORDER BY id ASC
                """,
                (ai_job_id,),
            ):
                yield [
                    row["id"],
                    row["ai_job_id"],
                    row["scan_id"],
                    row["created_at"] or "",
                    row["file_name"] or "",
                    row["source_file_path"] or "",
                    row["file_hash"] or "",
                    row["stage"] or "",
                    row["error"] or "",
                ]

    ts = datetime.now().strftime("%Y%m%d_%H%M")
    return csv_response(f"evidence_ai_errors_job_{ai_job_id}_{ts}.csv", rows, AI_ERROR_EXPORT_HEADERS)


@app.route("/delete_scan/<int:scan_id>", methods=["POST"])
def delete_scan(scan_id: int):
    with get_db() as conn:
        conn.execute("DELETE FROM scans WHERE id = ?", (scan_id,))
    return redirect(url_for("home"))


@app.route("/clear_uploads", methods=["POST"])
def clear_uploads():
    if UPLOAD_DIR.exists():
        for child in UPLOAD_DIR.iterdir():
            if child.name == ".gitkeep":
                continue
            if child.is_dir():
                shutil.rmtree(child, ignore_errors=True)
            else:
                child.unlink(missing_ok=True)
    return redirect(url_for("home"))


def mark_interrupted_ai_jobs() -> None:
    """Avoid showing stale running jobs after the local Flask app restarts."""
    with get_db() as conn:
        conn.execute(
            """
            UPDATE ai_jobs
            SET status = 'interrupted', message = ?, finished_at = COALESCE(finished_at, ?)
            WHERE status IN ('running', 'cancel_requested')
            """,
            ("The app restarted before this job finished. Submit another categorization run to continue.", now_str()),
        )
        conn.commit()


init_db()
mark_interrupted_ai_jobs()
start_ai_worker_once()
enqueue_pending_ai_jobs()

if __name__ == "__main__":
    # use_reloader=False prevents Flask's debug reloader from starting duplicate background workers.
    print(f"Evidence Tool patch {APP_PATCH_ID} loaded from {Path(__file__).resolve()}")
    app.run(debug=True, use_reloader=False)
