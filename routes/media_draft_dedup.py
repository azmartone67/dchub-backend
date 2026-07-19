"""media_draft_dedup.py — pending-draft fingerprint dedup (2026-07-18).

WHY THIS EXISTS
===============
The pending-drafts digest jammed at 32 items because every generator lane
dedupes on a key that CHANGES while the story does not:

  * ai_citation_tracker draft-press ... slug embeds observed DATE + prompt_id,
    but the self-solicited TITLE is identical across prompts → 7-8 identical
    "Perplexity Named DC Hub a Primary Reference" drafts minted in one run.
  * partnership_press_template ....... slug embeds the ISO WEEK → the same
    Switzerland release re-minted every week (w23/w25/w28/w29 all pending).
  * press_outreach (journalist pitch)  dedupe was TIME-boxed (14d) — a pending
    draft older than the window did not block an identical re-mint.
  * media_outreach stage-weekly ...... same flaw with a 7d window.

THE RULE: a draft's identity is its FINGERPRINT — (kind + normalized
title/slug + target) with digit-runs collapsed, so the same story with
refreshed numbers maps to the SAME fingerprint. If an UNPUBLISHED draft with
the fingerprint already exists, the lane must UPDATE that row's freshness /
numbers, never INSERT a second pending copy.

This module provides:
  draft_fingerprint(kind, text, target="")      -> stable hex fingerprint
  find_unpublished_press_duplicate(cur, ...)    -> existing pending dup or None
  collapse_pending_duplicates(conn, apply=...)  -> one-time cleanup (keep the
      newest of each fingerprint; press_releases dupes are DELETEd — the table
      has no status column and the partnership /reject endpoint already deletes
      unpublished drafts; the pitch tables use their existing soft-delete
      convention status='superseded').

Admin surface (one-time cleanup):
  POST /api/v1/media/pending-drafts/dedup-cleanup            dry-run report
  POST /api/v1/media/pending-drafts/dedup-cleanup?apply=true execute
"""
from __future__ import annotations

import os
import re
import hashlib
import logging

logger = logging.getLogger(__name__)

try:
    from flask import Blueprint, jsonify, request
    media_draft_dedup_bp = Blueprint("media_draft_dedup", __name__)
except Exception:  # pragma: no cover — Flask is always present in prod
    Blueprint = None
    media_draft_dedup_bp = None


# ── fingerprint ──────────────────────────────────────────────────────────
_DIGIT_RUN = re.compile(r"[\d][\d,\.]*")
_NON_ALNUM = re.compile(r"[^a-z0-9#]+")


def normalize_fp_text(s: str) -> str:
    """Lowercase, collapse every digit-run (incl. 21,405 / 4.0) to '#', squash
    punctuation → the same story with refreshed numbers normalizes equal."""
    s = (s or "").lower()
    s = _DIGIT_RUN.sub("#", s)
    s = _NON_ALNUM.sub(" ", s)
    return " ".join(s.split())


def draft_fingerprint(kind: str, text: str, target: str = "") -> str:
    """Stable fingerprint of (kind + normalized title/slug + target)."""
    basis = f"{(kind or '').strip().lower()}|{normalize_fp_text(text)}|{(target or '').strip().lower()}"
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:32]


# ── pending-duplicate lookup (press_releases) ────────────────────────────
def find_unpublished_press_duplicate(cur, category: str, title: str,
                                     target: str = "",
                                     exclude_slug: str | None = None) -> dict | None:
    """Return {id, slug, title} of the newest UNPUBLISHED press_releases row in
    `category` whose fingerprint matches (kind=category, title, target), or
    None. Never raises — a lookup failure must not block a lane (the lane then
    inserts as before; the digest cleanup will collapse it later)."""
    try:
        want = draft_fingerprint(category, title, target)
        cur.execute("""
            SELECT id, slug, title FROM press_releases
            WHERE published = FALSE AND category = %s
            ORDER BY created_at DESC NULLS LAST, id DESC
            LIMIT 200
        """, (category,))
        for rid, slug, row_title in cur.fetchall():
            if exclude_slug and slug == exclude_slug:
                continue
            if draft_fingerprint(category, row_title or slug or "", target) == want:
                return {"id": rid, "slug": slug, "title": row_title}
        return None
    except Exception as e:
        logger.warning("[draft_dedup] dup lookup failed: %s", str(e)[:120])
        return None


# ── one-time cleanup ─────────────────────────────────────────────────────
def _pick_survivors(rows: list[dict], key_fn) -> tuple[list[dict], list[dict]]:
    """rows must be newest-first. Returns (survivors, dupes): the first row of
    each fingerprint survives, every later (older) row with the same
    fingerprint is a dupe. Pure — unit-testable without a DB."""
    seen: set = set()
    survivors: list[dict] = []
    dupes: list[dict] = []
    for r in rows:
        k = key_fn(r)
        if k in seen:
            dupes.append(r)
        else:
            seen.add(k)
            survivors.append(r)
    return survivors, dupes


def collapse_pending_duplicates(conn, apply: bool = False) -> dict:
    """Collapse existing pending duplicates, keeping the NEWEST of each
    fingerprint. press_releases dupes are DELETEd (no status column; deleting
    unpublished drafts is the established convention — see the partnership
    /reject endpoint). press_pitch_drafts / media_pitch_drafts dupes get
    status='superseded' (the tables' existing soft-delete convention).

    apply=False (default) reports what WOULD happen without writing."""
    out: dict = {"apply": bool(apply), "tables": {}}
    cur = conn.cursor()

    # press_releases — fingerprint on (category + normalized title)
    cur.execute("""
        SELECT id, slug, title, category, created_at
        FROM press_releases WHERE published = FALSE
        ORDER BY created_at DESC NULLS LAST, id DESC
    """)
    rows = [{"id": r[0], "slug": r[1], "title": r[2], "category": r[3]}
            for r in cur.fetchall()]
    survivors, dupes = _pick_survivors(
        rows, lambda r: draft_fingerprint(r["category"] or "", r["title"] or r["slug"] or ""))
    if apply and dupes:
        cur.execute(
            "DELETE FROM press_releases WHERE published = FALSE AND id = ANY(%s)",
            ([d["id"] for d in dupes],))
    out["tables"]["press_releases"] = {
        "pending_before": len(rows),
        "pending_after": len(rows) - len(dupes),
        "deleted" if apply else "would_delete": len(dupes),
        "kept": [{"id": s["id"], "slug": s["slug"]} for s in survivors],
        "removed": [{"id": d["id"], "slug": d["slug"]} for d in dupes],
    }

    # pitch tables — natural-key fingerprints, soft-delete via status
    for table, key_cols in (
        ("press_pitch_drafts", ("contact_id", "angle_key")),
        ("media_pitch_drafts", ("recipient_email", "topic")),
    ):
        try:
            cur.execute(f"""
                SELECT id, {key_cols[0]}, {key_cols[1]}, created_at
                FROM {table} WHERE status = 'pending'
                ORDER BY created_at DESC NULLS LAST, id DESC
            """)
            rows = [{"id": r[0], "k1": r[1], "k2": r[2]} for r in cur.fetchall()]
            survivors, dupes = _pick_survivors(rows, lambda r: (r["k1"], r["k2"]))
            if apply and dupes:
                cur.execute(
                    f"UPDATE {table} SET status = 'superseded' "
                    f"WHERE status = 'pending' AND id = ANY(%s)",
                    ([d["id"] for d in dupes],))
            out["tables"][table] = {
                "pending_before": len(rows),
                "pending_after": len(rows) - len(dupes),
                ("superseded" if apply else "would_supersede"): len(dupes),
                "removed_ids": [d["id"] for d in dupes],
            }
        except Exception as e:
            # a missing table must not sink the whole cleanup
            out["tables"][table] = {"error": str(e)[:120]}
            try:
                conn.rollback()
            except Exception:
                pass

    out["pending_before"] = sum(
        t.get("pending_before", 0) for t in out["tables"].values() if isinstance(t, dict))
    out["pending_after"] = sum(
        t.get("pending_after", 0) for t in out["tables"].values() if isinstance(t, dict))
    return out


# ── admin endpoint (guarded; dry-run by default) ─────────────────────────
def _admin_ok() -> bool:
    expected = {v for v in (os.environ.get("DCHUB_ADMIN_KEY"),
                            os.environ.get("DCHUB_INTERNAL_KEY"),
                            os.environ.get("INTERNAL_KEY")) if v}
    provided = (request.headers.get("X-Admin-Key")
                or request.headers.get("X-Internal-Key")
                or request.args.get("admin_key")
                or (request.headers.get("Authorization", "")
                    .replace("Bearer ", "").strip()))
    return bool(expected) and provided in expected


if media_draft_dedup_bp is not None:

    @media_draft_dedup_bp.route("/api/v1/media/pending-drafts/dedup-cleanup",
                                methods=["POST"])
    def dedup_cleanup():
        """One-time collapse of pending duplicates. Admin-gated, dry-run by
        default; ?apply=true executes. Keeps the newest of each fingerprint."""
        if not _admin_ok():
            return jsonify(error="unauthorized"), 401
        apply = request.args.get("apply", "").lower() in ("1", "true", "yes")
        import psycopg2
        db = os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL")
        if not db:
            return jsonify(error="no_database"), 503
        c = None
        try:
            c = psycopg2.connect(db, sslmode="require", connect_timeout=8)
            c.autocommit = True
            return jsonify(ok=True, **collapse_pending_duplicates(c, apply=apply)), 200
        except Exception as e:
            return jsonify(ok=False, error=str(e)[:200]), 500
        finally:
            try:
                c.close()
            except Exception:
                pass
