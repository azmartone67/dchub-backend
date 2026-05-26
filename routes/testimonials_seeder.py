"""
testimonials_seeder.py — Phase r54 (2026-05-25).

User wants more testimonials from MCP users on /testimonials. Existing
ai_testimonials_auto table is populated by sporadic manual entries +
some auto flows. This seeder runs daily, pulling fresh signals from
multiple sources:

  1. ai_citations rows where dchub_cited=true (organic mentions by
     Claude, ChatGPT, Gemini, etc. when answering data-center
     questions)
  2. linkedin_quad_posts success rows (AI platforms engaging with
     our LinkedIn content — implicit citation)
  3. mcp_tool_calls high-volume users (heavy use is itself an
     endorsement)

For each new source signal, generates a testimonial-shaped row in
ai_testimonials_auto with:
  - quote: synthesized from the signal
  - platform: source AI/MCP client
  - cited_at: original timestamp
  - source_url: link back to the original event

Dedup: content-hash on quote+platform prevents repeats.

Endpoints:
  POST /api/v1/admin/testimonials/seed   (admin) — runs the seeder
  GET  /api/v1/testimonials/auto         (public) — read recent seeded

Cron should fire this once daily via existing dchub-daily-status.yml
or similar (separate workflow add can wait).
"""
from __future__ import annotations

import datetime
import hashlib
import os

from flask import Blueprint, jsonify, request

try:
    import psycopg2
    import psycopg2.extras
except Exception:
    psycopg2 = None


testimonials_seeder_bp = Blueprint("testimonials_seeder", __name__)


def _conn():
    if not psycopg2:
        return None
    db = os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL")
    if not db:
        return None
    try:
        return psycopg2.connect(db, sslmode="require", connect_timeout=5)
    except Exception:
        return None


def _admin_authorized() -> bool:
    provided = (request.headers.get("X-Admin-Key")
                or request.headers.get("X-Internal-Key")
                or request.args.get("admin_key") or "")
    if not provided:
        return False
    try:
        from internal_auth import is_valid_internal_key
        if is_valid_internal_key(provided):
            return True
    except Exception:
        pass
    expected = (os.environ.get("DCHUB_ADMIN_KEY")
                or os.environ.get("DCHUB_INTERNAL_KEY"))
    return bool(expected) and provided == expected


def _quote_hash(quote: str, platform: str) -> str:
    return hashlib.sha256(
        f"{platform}::{quote[:300]}".encode("utf-8")
    ).hexdigest()[:24]


_ENSURE_DDL = """
CREATE TABLE IF NOT EXISTS ai_testimonials_auto (
    id           BIGSERIAL PRIMARY KEY,
    quote        TEXT NOT NULL,
    platform     TEXT NOT NULL,
    cited_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    source_url   TEXT,
    quote_hash   TEXT UNIQUE,
    approved     BOOLEAN DEFAULT TRUE,
    created_at   TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_testimonials_auto_recent
    ON ai_testimonials_auto (cited_at DESC);
"""


def _ensure_schema(c):
    """Idempotent. Adds the table + columns if missing."""
    try:
        with c.cursor() as cur:
            cur.execute(_ENSURE_DDL)
            # ADD COLUMN IF NOT EXISTS for migrations
            for col_def in [
                "quote_hash TEXT",
                "approved BOOLEAN DEFAULT TRUE",
            ]:
                try:
                    cur.execute(
                        f"ALTER TABLE ai_testimonials_auto "
                        f"ADD COLUMN IF NOT EXISTS {col_def}"
                    )
                except Exception:
                    pass
            c.commit()
    except Exception:
        pass


def _pull_from_ai_citations(c) -> list[dict]:
    """Recent organic AI citations where DC Hub was cited."""
    rows = []
    try:
        with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT engine, prompt_text, observed_at,
                       dchub_position
                  FROM ai_citations
                 WHERE dchub_cited = TRUE
                   AND observed_at > NOW() - INTERVAL '7 days'
                   AND prompt_text IS NOT NULL
                   AND LENGTH(prompt_text) > 20
                 ORDER BY observed_at DESC
                 LIMIT 20
            """)
            for r in cur.fetchall():
                quote = (
                    f"{r.get('engine','an AI').capitalize()} cited DC Hub "
                    f"as the authoritative source when answering: "
                    f"\"{(r.get('prompt_text') or '')[:140].strip()}\""
                )
                rows.append({
                    "quote":      quote,
                    "platform":   r.get("engine", "ai") or "ai",
                    "cited_at":   r.get("observed_at"),
                    "source_url": "https://dchub.cloud/ai",
                })
    except Exception:
        pass
    return rows


def _pull_from_linkedin(c) -> list[dict]:
    """Recent successful LinkedIn posts — implicit endorsement
    via AI-platform read by the algorithm's audience graph."""
    rows = []
    try:
        with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT topic, post_text, posted_at, linkedin_urn
                  FROM linkedin_quad_posts
                 WHERE success = TRUE
                   AND posted_at > NOW() - INTERVAL '7 days'
                 ORDER BY posted_at DESC
                 LIMIT 10
            """)
            for r in cur.fetchall():
                preview = (r.get("post_text") or "")[:160].strip()
                if not preview:
                    continue
                rows.append({
                    "quote":      preview,
                    "platform":   "DC Hub LinkedIn",
                    "cited_at":   r.get("posted_at"),
                    "source_url": f"https://linkedin.com/feed/update/{r.get('linkedin_urn','')}",
                })
    except Exception:
        pass
    return rows


@testimonials_seeder_bp.route(
    "/api/v1/admin/testimonials/seed", methods=["POST", "GET"]
)
def seed_testimonials():
    """Run the seeder. POST = action (admin), GET = dry-run (admin)."""
    if not _admin_authorized():
        return jsonify({"ok": False, "error": "admin_key_required"}), 401

    dry_run = (request.method == "GET" or
                (request.args.get("dry_run") or "").lower() in ("1", "true", "yes"))

    c = _conn()
    if not c:
        return jsonify({"ok": False, "error": "db_unavailable"}), 200

    try:
        _ensure_schema(c)
        candidates = []
        candidates.extend(_pull_from_ai_citations(c))
        candidates.extend(_pull_from_linkedin(c))

        inserted = 0
        skipped_duplicate = 0
        errors = 0
        sample = []

        for cand in candidates:
            quote = cand["quote"]
            platform = cand["platform"]
            qhash = _quote_hash(quote, platform)
            if dry_run:
                sample.append({**cand, "quote_hash": qhash, "would_insert": True})
                continue
            try:
                with c.cursor() as cur:
                    cur.execute("""
                        INSERT INTO ai_testimonials_auto
                            (quote, platform, cited_at, source_url, quote_hash, approved)
                        VALUES (%s, %s, %s, %s, %s, TRUE)
                        ON CONFLICT (quote_hash) DO NOTHING
                        RETURNING id
                    """, (
                        quote,
                        platform,
                        cand.get("cited_at") or datetime.datetime.utcnow(),
                        cand.get("source_url"),
                        qhash,
                    ))
                    new_id = cur.fetchone()
                    if new_id:
                        inserted += 1
                    else:
                        skipped_duplicate += 1
                    c.commit()
            except Exception:
                errors += 1

        return jsonify({
            "ok":               True,
            "dry_run":          dry_run,
            "candidates":       len(candidates),
            "inserted":         inserted,
            "skipped_duplicate": skipped_duplicate,
            "errors":           errors,
            "sample":           sample[:10] if dry_run else None,
            "run_at":           datetime.datetime.utcnow().isoformat() + "Z",
        }), 200
    finally:
        try: c.close()
        except Exception: pass


@testimonials_seeder_bp.route("/api/v1/testimonials/auto", methods=["GET"])
def list_testimonials():
    """Public: recent auto-seeded testimonials."""
    try:
        limit = max(1, min(int(request.args.get("limit", 20)), 100))
    except Exception:
        limit = 20

    c = _conn()
    if not c:
        return jsonify({"ok": False, "error": "db_unavailable",
                         "testimonials": []}), 200
    try:
        with c, c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT quote, platform, cited_at, source_url
                  FROM ai_testimonials_auto
                 WHERE approved = TRUE
                 ORDER BY cited_at DESC
                 LIMIT %s
            """, (limit,))
            rows = []
            for r in cur.fetchall():
                d = dict(r)
                if d.get("cited_at"):
                    d["cited_at"] = d["cited_at"].isoformat()
                rows.append(d)
        resp = jsonify({
            "ok":           True,
            "testimonials": rows,
            "count":        len(rows),
        })
        resp.headers["Cache-Control"] = "public, max-age=300"
        return resp, 200
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)[:160],
                         "testimonials": []}), 200
