from __future__ import annotations

import csv
import io
import shutil
import threading
import uuid
from datetime import datetime
from pathlib import Path
from queue import Queue
from typing import Any

from flask import Flask, Response, jsonify, redirect, render_template, request, url_for
from werkzeug.utils import secure_filename

from ai_categorizer import categorize_file
from common import (
    AI_EXPORT_HEADERS,
    AI_MAX_CONSECUTIVE_ERRORS,
    CORE_EXPORT_HEADERS,
    EXTRACT_MAX_CHARS,
    HASH_ALGO,
    OLLAMA_MODEL,
    UPLOAD_DIR,
    get_db,
    init_db,
)
from evidence_scanner import parse_paths, scan_files

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 20 * 1024 * 1024 * 1024  # 20GB; local-only app, override in deployment if needed.

# Phase 1 scan jobs are short-lived in-memory progress records.
JOBS: dict[str, dict[str, Any]] = {}
JOBS_LOCK = threading.Lock()

# Phase 2 categorization jobs are persisted in SQLite and run through one FIFO worker.
AI_QUEUE: Queue[int] = Queue()
AI_WORKER_LOCK = threading.Lock()
AI_WORKER_STARTED = False
AI_SUCCESS_STATUSES = {"ok", "parsed_fields"}
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


def fetch_dashboard(scan_id: int | None) -> dict[str, Any]:
    with get_db() as conn:
        scans = conn.execute("SELECT * FROM scans ORDER BY id DESC LIMIT 15").fetchall()
        ai_jobs = conn.execute(
            """
            SELECT j.*, s.label AS scan_label
            FROM ai_jobs j
            LEFT JOIN scans s ON s.id = j.scan_id
            ORDER BY j.id DESC
            LIMIT 25
            """
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
                    SELECT f.*, a.category AS ai_category, a.description AS ai_description, a.status AS ai_status
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
            "scan": scan,
            "files": files,
            "duplicate_groups": duplicate_groups,
            "errors": errors,
            "ai_counts": ai_counts,
            "default_model": OLLAMA_MODEL,
            "default_max_chars": EXTRACT_MAX_CHARS,
            "hash_algo": HASH_ALGO,
            "ai_terminal_statuses": AI_TERMINAL_STATUSES,
        }


@app.route("/")
def home():
    requested = request.args.get("scan_id", type=int)
    scan_id = requested or latest_scan_id()
    return render_template("index.html", **fetch_dashboard(scan_id))


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
            conn.execute(
                "UPDATE ai_jobs SET status = 'failed', message = ?, finished_at = ? WHERE id = ?",
                ("Scan not found.", now_str(), ai_job_id),
            )
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
                "creation_date": row["creation_date"],
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
                    model = excluded.model
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
                    SET category = ?, description = ?, status = ?, error = ?, extracted_chars = ?, updated_at = ?,
                        raw_response = ?, model = ?, extraction_status = ?
                    WHERE scan_id = ? AND file_hash = ?
                    """,
                    (
                        result.get("category"),
                        result.get("description"),
                        status,
                        result.get("extraction_error") or "",
                        int(result.get("extracted_chars") or 0),
                        now_str(),
                        result.get("raw_response") or "",
                        job["model"],
                        result.get("extraction_status") or "",
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
                    conn.execute(
                        """
                        UPDATE ai_jobs
                        SET status = 'failed', current = ?, total = ?, message = ?, finished_at = ?
                        WHERE id = ?
                        """,
                        (
                            idx,
                            total,
                            f"Stopped after {consecutive_errors} consecutive Ollama errors. Fix the endpoint/model, then submit another categorization run.",
                            now_str(),
                            ai_job_id,
                        ),
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
            with get_db() as conn:
                conn.execute(
                    "UPDATE ai_jobs SET status = 'failed', message = ?, last_error = ?, finished_at = ? WHERE id = ?",
                    (f"Categorization worker failed: {type(e).__name__}: {e}", f"{type(e).__name__}: {e}", now_str(), ai_job_id),
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


@app.route("/start_ai", methods=["POST"])
def start_ai():
    scan_id = request.form.get("scan_id", type=int)
    if not scan_id:
        return jsonify({"ok": False, "error": "No scan selected."}), 400
    categories = request.form.get("categories", "")
    context = request.form.get("context", "")
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
    return jsonify({"ok": True, "job_id": f"ai-{ai_job_id}", "scan_id": scan_id})


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
        },
    }


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
    return redirect(url_for("home", scan_id=scan_id))


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
                       creation_date, size_mb, file_hash
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
                    row["creation_date"],
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
                       f.creation_date, f.size_mb, f.file_hash,
                       a.category, a.description, a.status AS ai_status
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
                    row["creation_date"],
                    f"{float(row['size_mb'] or 0):.1f}",
                    row["file_hash"],
                    row["category"] or "",
                    row["description"] or "",
                    row["ai_status"] or "not_started",
                ]

    ts = datetime.now().strftime("%Y%m%d_%H%M")
    return csv_response(f"evidence_inventory_with_ai_scan_{scan_id}_{ts}.csv", rows, AI_EXPORT_HEADERS)


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

if __name__ == "__main__":
    app.run(debug=True)
