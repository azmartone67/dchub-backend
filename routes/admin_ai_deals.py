"""
admin_ai_deals.py - Flask blueprint for /api/admin/ai-deals
Self-contained: opens its own psycopg connection from DATABASE_URL.
"""

import hmac
import json
import os
from contextlib import contextmanager
from datetime import date, datetime
from typing import Any, Optional

import psycopg2 as _pg
from flask import Blueprint, jsonify, request


admin_ai_deals_bp = Blueprint("admin_ai_deals", __name__, url_prefix="/api/admin/ai-deals")


# ---------------------------------------------------------------------------
# Connection helper -- self-contained
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

MIGRATION_SQL = """
CREATE TABLE IF NOT EXISTS ai_deals (
    id              BIGSERIAL PRIMARY KEY,
    title           TEXT NOT NULL,
    target          TEXT NOT NULL,
    acquirer        TEXT,
    value_usd       NUMERIC(18, 2),
    deal_type       TEXT NOT NULL,
    announced_date  DATE NOT NULL,
    confidence      NUMERIC(3, 2) NOT NULL DEFAULT 0.50 CHECK (confidence BETWEEN 0 AND 1),
    source_url      TEXT,
    notes           TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (target, announced_date, value_usd)
);

CREATE INDEX IF NOT EXISTS ix_ai_deals_announced_date_desc ON ai_deals (announced_date DESC);
CREATE INDEX IF NOT EXISTS ix_ai_deals_value_desc           ON ai_deals (value_usd DESC NULLS LAST);
CREATE INDEX IF NOT EXISTS ix_ai_deals_type                 ON ai_deals (deal_type);
"""


def _ensure_table() -> None:
    if getattr(_ensure_table, "_done", False):
        return
    with _conn() as c, c.cursor() as cur:
        cur.execute(MIGRATION_SQL)
        c.commit()
    _ensure_table._done = True  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def _check_auth() -> Optional[tuple]:
    expected = os.environ.get("DCHUB_ADMIN_SECRET", "dchub-admin-secret-2026")
    presented = ""
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        presented = auth[len("Bearer "):]
    elif request.headers.get("X-Admin-Key"):
        presented = request.headers["X-Admin-Key"]

    if not presented or not hmac.compare_digest(presented, expected):
        return jsonify(error="unauthorized"), 401
    return None


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

VALID_TYPES = {
    "equity", "M&A", "debt", "capex", "JV",
    "AI-contract", "AI-infra", "land", "power-agreement", "other",
}


def _validate(p: dict) -> tuple[Optional[dict], Optional[str]]:
    if not isinstance(p, dict):
        return None, "body must be JSON object"

    title = (p.get("title") or "").strip()
    target = (p.get("target") or "").strip()
    if not title or len(title) > 500:
        return None, "title required, <= 500 chars"
    if not target or len(target) > 200:
        return None, "target required, <= 200 chars"

    deal_type = (p.get("deal_type") or "").strip()
    if deal_type not in VALID_TYPES:
        return None, f"deal_type must be one of {sorted(VALID_TYPES)}"

    announced = p.get("announced_date")
    if isinstance(announced, str):
        try:
            announced = date.fromisoformat(announced)
        except ValueError:
            return None, "announced_date must be YYYY-MM-DD"
    elif not isinstance(announced, date):
        return None, "announced_date required (YYYY-MM-DD)"
    if announced > date.today():
        return None, "announced_date cannot be in the future"

    value_usd = p.get("value_usd")
    if value_usd is not None:
        try:
            value_usd = float(value_usd)
            if value_usd < 0 or value_usd > 1e13:
                return None, "value_usd out of plausible range"
        except (TypeError, ValueError):
            return None, "value_usd must be numeric"

    confidence = p.get("confidence", 0.5)
    try:
        confidence = float(confidence)
        if not (0.0 <= confidence <= 1.0):
            return None, "confidence must be between 0 and 1"
    except (TypeError, ValueError):
        return None, "confidence must be numeric"

    return {
        "title": title,
        "target": target,
        "acquirer": (p.get("acquirer") or "").strip() or None,
        "value_usd": value_usd,
        "deal_type": deal_type,
        "announced_date": announced,
        "confidence": confidence,
        "source_url": (p.get("source_url") or "").strip() or None,
        "notes": (p.get("notes") or "").strip() or None,
    }, None


# ---------------------------------------------------------------------------
# POST -- insert
# ---------------------------------------------------------------------------

@admin_ai_deals_bp.route("", methods=["POST"])
def insert_deal():
    err = _check_auth()
    if err is not None:
        return err

    _ensure_table()

    payload, msg = _validate(request.get_json(silent=True) or {})
    if payload is None:
        return jsonify(error=msg), 400

    sql = """
        INSERT INTO ai_deals
            (title, target, acquirer, value_usd, deal_type,
             announced_date, confidence, source_url, notes)
        VALUES
            (%(title)s, %(target)s, %(acquirer)s, %(value_usd)s, %(deal_type)s,
             %(announced_date)s, %(confidence)s, %(source_url)s, %(notes)s)
        ON CONFLICT (target, announced_date, value_usd) DO UPDATE SET
            confidence  = GREATEST(ai_deals.confidence, EXCLUDED.confidence),
            title       = COALESCE(NULLIF(EXCLUDED.title, ''), ai_deals.title),
            source_url  = COALESCE(EXCLUDED.source_url, ai_deals.source_url),
            notes       = COALESCE(EXCLUDED.notes, ai_deals.notes)
        RETURNING id, (xmax = 0) AS inserted;
    """
    with _conn() as c, c.cursor() as cur:
        cur.execute(sql, payload)
        row = cur.fetchone()
        c.commit()

    deal_id, was_inserted = row[0], bool(row[1])
    return jsonify(
        id=deal_id,
        status="inserted" if was_inserted else "merged",
        deal=payload | {"announced_date": payload["announced_date"].isoformat()},
    ), (201 if was_inserted else 200)


# ---------------------------------------------------------------------------
# GET -- list
# ---------------------------------------------------------------------------

@admin_ai_deals_bp.route("", methods=["GET"])
def list_deals():
    _ensure_table()

    try:
        limit = max(1, min(int(request.args.get("limit", 100)), 500))
    except ValueError:
        return jsonify(error="limit must be int"), 400

    since = request.args.get("since")
    args = {"limit": limit}
    where = ""
    if since:
        try:
            args["since"] = date.fromisoformat(since)
            where = "WHERE announced_date >= %(since)s"
        except ValueError:
            return jsonify(error="since must be YYYY-MM-DD"), 400

    sql = f"""
        SELECT id, title, target, acquirer, value_usd, deal_type,
               announced_date, confidence, source_url, notes, created_at
        FROM ai_deals
        {where}
        ORDER BY announced_date DESC, value_usd DESC NULLS LAST, id DESC
        LIMIT %(limit)s;
    """
    with _conn() as c, c.cursor() as cur:
        cur.execute(sql, args)
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]

    for r in rows:
        if isinstance(r.get("announced_date"), date):
            r["announced_date"] = r["announced_date"].isoformat()
        if isinstance(r.get("created_at"), datetime):
            r["created_at"] = r["created_at"].isoformat()
        if r.get("value_usd") is not None:
            r["value_usd"] = float(r["value_usd"])
        if r.get("confidence") is not None:
            r["confidence"] = float(r["confidence"])

    return jsonify(count=len(rows), deals=rows), 200


# ---------------------------------------------------------------------------
# GET /health
# ---------------------------------------------------------------------------

@admin_ai_deals_bp.route("/health", methods=["GET"])
def health():
    _ensure_table()
    with _conn() as c, c.cursor() as cur:
        cur.execute("SELECT COUNT(*), MAX(announced_date) FROM ai_deals;")
        count, latest = cur.fetchone()
    return jsonify(
        status="ok",
        count=int(count or 0),
        latest_announced_date=latest.isoformat() if latest else None,
    ), 200
