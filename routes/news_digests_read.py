"""
news_digests_read.py — Flask blueprint for public news-digest reads

DROP-IN INSTRUCTIONS
====================
1. Save this file at: dchub-backend/routes/news_digests_read.py

2. In your main Flask app factory, add:

       from routes.news_digests_read import news_digests_read_bp
       app.register_blueprint(news_digests_read_bp)

3. No env vars required. No auth (these endpoints are public — same trust level
   as /news and /press).

4. Assumes the `news_digests` table already exists (populated by news-digest
   task via /api/publish → Railway → Neon). If your table name differs, change
   the TABLE constant.

ENDPOINTS
=========
  GET /api/v1/news-digests/latest          — most recent digest, full payload
  GET /api/v1/news-digests                 — list (default 30, ?limit=N)
  GET /api/v1/news-digests/<slug>          — fetch a specific digest by slug

  (All return 404 with {"error": "not found"} if no rows match.)

NOTE
====
The pr-daily-publisher task expects this exact shape from /latest:
  {
    "slug": "digest-YYYY-MM-DD",
    "digest_date": "YYYY-MM-DD",
    "title": "...",
    "html": "...",
    "markdown": "...",
    "linkedin_text": "...",
    "story_count": <int>,
    "categories": {...},
    "sources": [...]
  }

If your news_digests columns are named differently, adjust the SELECT or add
column aliases to keep this contract stable.
"""

import json
from datetime import date, datetime
from typing import Any, Optional

from flask import Blueprint, jsonify, request

# Replace with your existing DB helper (psycopg2 pool, SQLAlchemy, etc.).
from db import get_conn  # type: ignore[import-not-found]


news_digests_read_bp = Blueprint("news_digests_read", __name__, url_prefix="/api/v1/news-digests")

TABLE = "news_digests"

# Columns we expose. Add/remove based on your actual schema.
SELECT_COLS = """
    slug,
    digest_date,
    title,
    html,
    markdown,
    linkedin_text,
    story_count,
    categories,
    sources,
    created_at
"""


# ---------------------------------------------------------------------------
# Row → JSON normalizer
# ---------------------------------------------------------------------------

def _row_to_dict(row: tuple, cols: list[str]) -> dict[str, Any]:
    out = dict(zip(cols, row))

    if isinstance(out.get("digest_date"), date):
        out["digest_date"] = out["digest_date"].isoformat()
    if isinstance(out.get("created_at"), datetime):
        out["created_at"] = out["created_at"].isoformat()

    # categories / sources may be JSONB (already dict/list), TEXT (needs parse),
    # or NULL. Normalize to native types.
    for k in ("categories", "sources"):
        v = out.get(k)
        if isinstance(v, str):
            try:
                out[k] = json.loads(v)
            except (json.JSONDecodeError, TypeError):
                # leave as string if it doesn't parse
                pass

    return out


# ---------------------------------------------------------------------------
# GET /latest — most recent digest
# ---------------------------------------------------------------------------

@news_digests_read_bp.route("/latest", methods=["GET"])
def latest() -> Any:
    sql = f"""
        SELECT {SELECT_COLS}
        FROM {TABLE}
        ORDER BY digest_date DESC, created_at DESC
        LIMIT 1;
    """
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(sql)
        row = cur.fetchone()
        if row is None:
            return jsonify(error="no digests found"), 404
        cols = [c[0] for c in cur.description]

    return jsonify(_row_to_dict(row, cols)), 200


# ---------------------------------------------------------------------------
# GET / — list (slug + date + title + story_count, no html/markdown bloat)
# ---------------------------------------------------------------------------

@news_digests_read_bp.route("", methods=["GET"])
def list_digests() -> Any:
    try:
        limit = max(1, min(int(request.args.get("limit", 30)), 200))
    except ValueError:
        return jsonify(error="limit must be int"), 400

    since: Optional[date] = None
    since_arg = request.args.get("since")
    if since_arg:
        try:
            since = date.fromisoformat(since_arg)
        except ValueError:
            return jsonify(error="since must be YYYY-MM-DD"), 400

    where = "WHERE digest_date >= %(since)s" if since else ""
    sql = f"""
        SELECT slug, digest_date, title, story_count, created_at
        FROM {TABLE}
        {where}
        ORDER BY digest_date DESC, created_at DESC
        LIMIT %(limit)s;
    """
    args: dict = {"limit": limit}
    if since:
        args["since"] = since

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(sql, args)
        cols = [c[0] for c in cur.description]
        rows = [_row_to_dict(r, cols) for r in cur.fetchall()]

    return jsonify(count=len(rows), digests=rows), 200


# ---------------------------------------------------------------------------
# GET /<slug> — single digest by slug
# ---------------------------------------------------------------------------

@news_digests_read_bp.route("/<string:slug>", methods=["GET"])
def by_slug(slug: str) -> Any:
    if not slug or len(slug) > 200 or not all(c.isalnum() or c in "-_" for c in slug):
        return jsonify(error="invalid slug"), 400

    sql = f"""
        SELECT {SELECT_COLS}
        FROM {TABLE}
        WHERE slug = %(slug)s
        ORDER BY created_at DESC
        LIMIT 1;
    """
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(sql, {"slug": slug})
        row = cur.fetchone()
        if row is None:
            return jsonify(error="not found", slug=slug), 404
        cols = [c[0] for c in cur.description]

    return jsonify(_row_to_dict(row, cols)), 200
