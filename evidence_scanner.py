"""File inventory scanning utilities."""
from __future__ import annotations

import hashlib
import os
import platform
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable

from common import HASH_ALGO

ProgressCallback = Callable[[str, int, int], None]
ErrorCallback = Callable[[str, str], None]

DOCUMENT_EXTS = {".doc", ".docx", ".odt", ".rtf"}
TEXT_EXTS = {".txt", ".md", ".markdown", ".log", ".json", ".xml", ".html", ".htm"}
SPREADSHEET_EXTS = {".xls", ".xlsx", ".xlsm", ".csv", ".tsv", ".ods"}
PRESENTATION_EXTS = {".ppt", ".pptx", ".odp"}
VIDEO_EXTS = {".mp4", ".mov", ".avi", ".wmv", ".mkv", ".webm", ".mpg", ".mpeg", ".m4v"}
AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".wma"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".tif", ".tiff", ".bmp", ".webp", ".heic"}
EMAIL_EXTS = {".eml", ".msg", ".mbox"}
ARCHIVE_EXTS = {".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz"}
DATABASE_EXTS = {".db", ".sqlite", ".sqlite3", ".mdb", ".accdb"}


@dataclass(slots=True)
class FileRecord:
    file_name: str
    file_type: str
    folder_location: str
    full_path: str
    last_modified_date: str
    creation_date: str
    size_mb: float
    size_bytes: int
    file_hash: str
    hash_algo: str


def parse_paths(raw_text: str) -> list[Path]:
    """Parse one path per line, allowing pasted quotes around Windows paths."""
    paths: list[Path] = []
    for line in (raw_text or "").splitlines():
        item = line.strip().strip('"').strip("'")
        if not item:
            continue
        paths.append(Path(item).expanduser())
    return paths


def file_type_for_path(path: Path) -> str:
    ext = path.suffix.lower()
    if ext in DOCUMENT_EXTS:
        return "Document"
    if ext in TEXT_EXTS:
        return "Text"
    if ext in SPREADSHEET_EXTS:
        return "Spreadsheet"
    if ext in PRESENTATION_EXTS:
        return "Presentation"
    if ext == ".pdf":
        return "PDF"
    if ext in VIDEO_EXTS:
        return "Video"
    if ext in AUDIO_EXTS:
        return "Audio"
    if ext in IMAGE_EXTS:
        return "Image"
    if ext in EMAIL_EXTS:
        return "Email"
    if ext in ARCHIVE_EXTS:
        return "Archive"
    if ext in DATABASE_EXTS:
        return "Database"
    return "Other"


def _hash_factory(algo: str):
    algo = (algo or HASH_ALGO).lower()
    if algo == "md5":
        return hashlib.md5(), "md5"  # noqa: S324 - duplicate detection, not security.
    if algo == "sha256":
        return hashlib.sha256(), "sha256"
    if algo == "blake2b":
        return hashlib.blake2b(digest_size=32), "blake2b"
    raise ValueError(f"Unsupported hash algorithm: {algo}. Use blake2b, sha256, or md5.")


def hash_file(path: Path, algo: str = HASH_ALGO, chunk_size: int = 8 * 1024 * 1024) -> tuple[str, str]:
    h, name = _hash_factory(algo)
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(chunk_size), b""):
            h.update(chunk)
    return h.hexdigest(), name


def format_datetime(timestamp: float | None) -> str:
    if timestamp is None:
        return ""
    return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S")


def creation_timestamp(stat_result: os.stat_result) -> float | None:
    """Return creation time where the platform exposes it; otherwise None."""
    system = platform.system().lower()
    if system == "windows":
        return stat_result.st_ctime
    birth = getattr(stat_result, "st_birthtime", None)
    if birth is not None:
        return birth
    return None


def record_for_file(path: Path, algo: str = HASH_ALGO) -> FileRecord:
    stat = path.stat()
    digest, digest_algo = hash_file(path, algo=algo)
    folder = str(path.parent.resolve())
    size_bytes = int(stat.st_size)
    return FileRecord(
        file_name=path.name,
        file_type=file_type_for_path(path),
        folder_location=folder,
        full_path=str(path.resolve()),
        last_modified_date=format_datetime(stat.st_mtime),
        creation_date=format_datetime(creation_timestamp(stat)),
        size_mb=round(size_bytes / (1024 * 1024), 1),
        size_bytes=size_bytes,
        file_hash=digest,
        hash_algo=digest_algo,
    )


def iter_candidate_files(paths: Iterable[Path], on_error: ErrorCallback | None = None) -> Iterable[Path]:
    """Yield every file from input files/directories. Directory symlinks are not followed."""
    for input_path in paths:
        try:
            p = input_path.resolve(strict=False)
        except Exception:
            p = input_path
        if not p.exists():
            if on_error:
                on_error(str(input_path), "Path does not exist.")
            continue
        if p.is_file():
            yield p
            continue
        if p.is_dir():
            for root, dirs, files in os.walk(p, followlinks=False, onerror=None):
                # Avoid common noisy system directories in copied evidence sets while still scanning hidden folders.
                dirs[:] = [d for d in dirs if d not in {"$RECYCLE.BIN", "System Volume Information"}]
                for name in files:
                    fp = Path(root) / name
                    try:
                        if fp.is_file():
                            yield fp
                    except Exception as e:
                        if on_error:
                            on_error(str(fp), f"Could not inspect file: {e}")
            continue
        if on_error:
            on_error(str(input_path), "Path is neither a file nor a directory.")


def scan_files(
    paths: Iterable[Path],
    *,
    algo: str = HASH_ALGO,
    progress: ProgressCallback | None = None,
    on_error: ErrorCallback | None = None,
) -> Iterable[FileRecord]:
    """Scan input paths and yield FileRecord objects.

    The total is reported as 0 because the scanner streams files instead of pre-counting them,
    which avoids walking very large folders twice.
    """
    count = 0
    for path in iter_candidate_files(paths, on_error=on_error):
        count += 1
        if progress:
            progress(str(path), count, 0)
        try:
            yield record_for_file(path, algo=algo)
        except Exception as e:
            if on_error:
                on_error(str(path), f"Could not scan file: {type(e).__name__}: {e}")
