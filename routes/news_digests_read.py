"""
news_digests_read.py - Flask blueprint for public news_digests reads.
Self-contained: opens its own psycopg connection from DATABASE_URL.
"""

import json
import os
from contextlib import contextmanager
from datetime import date, datetime
from typing import Any, Optional

import psycopg2 as _pg
from flask import Blueprint, jsonify, request


news_digests_read_bp = Blueprint("news_digests_read", __name__, url_prefix="/api/v1/news-digests")

TABLE = "news_digests"
SELECT_COLS = """
    slug, digest_date, title, html, markdown, linkedin_text,
    story_count, categories, sources, created_at
"""


def _dsn() -> str:
    return os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL") or ""


@contextmanager
def _conn():
    dsn = _dsn()
    if not dsn:
        raise RuntimeError("No DATABASE_URL / NEON_DATABASE_URL env var set")
    c = _pg.connect(dsn)
    try:
        yield c
    finally:
        c.close()


def _row_to_dict(row, cols):
    out = dict(zip(cols, row))
    if isinstance(out.get("digest_date"), date):
        out["digest_date"] = out["digest_date"].isoformat()
    if isinstance(out.get("created_at"), datetime):
        out["created_at"] = out["created_at"].isoformat()
    for k in ("categories", "sources"):
        v = out.get(k)
        if isinstance(v, str):
            try:
                out[k] = json.loads(v)
            except (json.JSONDecodeError, TypeError):
                pass
    return out


# AUTO-REPAIR: duplicate route '/latest' also in routes/iso_caiso.py:151 — review and remove one
@news_digests_read_bp.route("/latest", methods=["GET"])
def latest():
    sql = f"""
        SELECT {SELECT_COLS}
        FROM {TABLE}
        ORDER BY digest_date DESC, created_at DESC
        LIMIT 1;
    """
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute(sql)
            row = cur.fetchone()
            if row is None:
                return jsonify(error="no digests found"), 404
            cols = [d[0] for d in cur.description]
        return jsonify(_row_to_dict(row, cols)), 200
    except Exception as e:
        # Most likely cause: table doesn't exist yet, or column mismatch
        return jsonify(error=f"news_digests query failed: {type(e).__name__}: {e}"), 500

# AUTO-REPAIR: duplicate route '' also in cors_proxy_routes.py:114 — review and remove one

@news_digests_read_bp.route("", methods=["GET"])
def list_digests():
    try:
        limit = max(1, min(int(request.args.get("limit", 30)), 200))
    except ValueError:
        return jsonify(error="limit must be int"), 400

    since = None
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
    args = {"limit": limit}
    if since:
        args["since"] = since

    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute(sql, args)
            cols = [d[0] for d in cur.description]
            rows = [_row_to_dict(r, cols) for r in cur.fetchall()]
        return jsonify(count=len(rows), digests=rows), 200
    except Exception as e:
        return jsonify(error=f"news_digests query failed: {type(e).__name__}: {e}"), 500


@news_digests_read_bp.route("/<string:slug>", methods=["GET"])
def by_slug(slug):
    if not slug or len(slug) > 200 or not all(c.isalnum() or c in "-_" for c in slug):
        return jsonify(error="invalid slug"), 400

    sql = f"""
        SELECT {SELECT_COLS}
        FROM {TABLE}
        WHERE slug = %(slug)s
        ORDER BY created_at DESC
        LIMIT 1;
    """
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute(sql, {"slug": slug})
            row = cur.fetchone()
            if row is None:
                return jsonify(error="not found", slug=slug), 404
            cols = [d[0] for d in cur.description]
        return jsonify(_row_to_dict(row, cols)), 200
    except Exception as e:
        return jsonify(error=f"news_digests query failed: {type(e).__name__}: {e}"), 500
