"""Repair AI category fallback rows caused by overly strict category validation.

Run from the evidence-tool folder after replacing the category-validation fix files:

    py repair_ai_category_fallbacks.py

The script only updates rows where:
- status is invalid_category_fallback;
- original_category / raw model category can be normalized to an allowed category.
"""
from __future__ import annotations

from datetime import datetime

from ai_categorizer import canonicalize_category
from common import DEFAULT_CATEGORIES_TEXT, get_db


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def recalc_review_flag(confidence, evidence_basis: str) -> int:
    try:
        conf = float(confidence) if confidence is not None else 0.0
    except Exception:
        conf = 0.0
    basis = (evidence_basis or "").strip().lower()
    if conf < 0.5:
        return 1
    if basis in {"metadata only", "filename only"}:
        return 1
    return 0


def main() -> None:
    checked = 0
    repaired = 0
    skipped = 0
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT id, original_category, confidence, evidence_basis
            FROM ai_results
            WHERE status = 'invalid_category_fallback'
              AND COALESCE(original_category, '') <> ''
            ORDER BY id
            """
        ).fetchall()
        for row in rows:
            checked += 1
            raw = row["original_category"] or ""
            canonical = canonicalize_category(raw, DEFAULT_CATEGORIES_TEXT)
            if not canonical:
                skipped += 1
                continue
            needs_review = recalc_review_flag(row["confidence"], row["evidence_basis"] or "")
            # If raw == canonical, clear original_category so future exports are less confusing.
            # If raw was an alias, preserve it as a raw-model/debug value.
            raw_to_store = "" if raw.strip() == canonical else raw.strip()
            conn.execute(
                """
                UPDATE ai_results
                SET category = ?,
                    status = 'category_repaired',
                    category_valid = 1,
                    needs_human_review = ?,
                    original_category = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (canonical, needs_review, raw_to_store, now_str(), row["id"]),
            )
            repaired += 1
        conn.commit()

    print(f"Checked invalid fallback rows: {checked}")
    print(f"Repaired rows: {repaired}")
    print(f"Skipped rows: {skipped}")


if __name__ == "__main__":
    main()
