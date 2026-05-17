"""Phase XXXX (2026-05-16) — competitor intel watcher.

Daily scrapes the public homepages of the 3 main static competitors
(DCHawk, dcByte, DC Knowledge) so DC Hub Media gets a heads-up when
they ship something. Surfaces drift as a brain finding so we can
auto-publish counter-positioning content via /vs + press.

  POST /api/v1/competitors/scan          admin cron entry
  GET  /api/v1/competitors/snapshots     last 30 days per competitor
  GET  /api/v1/competitors/diffs         most recent meaningful changes

For each competitor:
  - Fetch homepage (or pricing page if known)
  - Hash content (first 8KB)
  - Compare to yesterday — flag if hash AND byte-delta > 10%
  - Persist daily snapshot for trend

Cron: same daily slot as facility-snapshot (1 cron, multiple steps)
to keep cron count predictable.
"""

from __future__ import annotations

import os
import re
import datetime
import hashlib
from flask import Blueprint, jsonify, request


# Phase XXXX (2026-05-16) — name 'competitor_intel' was already
# registered by routes/competitor_intelligence (a legacy module),
# so XXXX failed to register in prod logs:
#   [main] competitor_intel register failed: The name 'competitor_intel'
#   is already registered for a different blueprint.
# Rename to competitor_intel_v2 to unblock.
competitor_intel_bp = Blueprint("competitor_intel_v2", __name__)


_ADMIN_KEY = (os.environ.get("DCHUB_ADMIN_KEY")
              or os.environ.get("DCHUB_INTERNAL_KEY") or "").strip()


# Public surfaces we scrape. Keep tight: too many = brittle + slow.
_COMPETITORS = [
    {"slug": "dchawk",  "name": "DCHawk",
     "urls": ["https://dchawk.com/", "https://dchawk.com/pricing"]},
    {"slug": "dcbyte",  "name": "dcByte",
     "urls": ["https://dcbyte.com/", "https://dcbyte.com/pricing"]},
    {"slug": "dck",     "name": "Data Center Knowledge",
     "urls": ["https://www.datacenterknowledge.com/"]},
    {"slug": "dcd",     "name": "Data Center Dynamics",
     "urls": ["https://www.datacenterdynamics.com/"]},
    {"slug": "dcf",     "name": "Data Center Frontier",
     "urls": ["https://www.datacenterfrontier.com/"]},
]


def _conn():
    import psycopg2
    db = os.environ.get("DATABASE_URL")
    if not db: return None
    try:
        c = psycopg2.connect(db, sslmode="require", connect_timeout=5)
        c.autocommit = True
        return c
    except Exception:
        return None


_SCHEMA = """
CREATE TABLE IF NOT EXISTS competitor_snapshots (
    id              BIGSERIAL PRIMARY KEY,
    competitor_slug TEXT NOT NULL,
    url             TEXT NOT NULL,
    snapshot_date   DATE NOT NULL,
    content_hash    TEXT,
    bytes_size      INT,
    status_code     INT,
    title_extracted TEXT,
    captured_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_competitor_snap
    ON competitor_snapshots(competitor_slug, url, snapshot_date);
CREATE INDEX IF NOT EXISTS ix_competitor_recent
    ON competitor_snapshots(competitor_slug, snapshot_date DESC);
"""


def _ensure_schema(c):
    try:
        with c.cursor() as cur:
            cur.execute(_SCHEMA)
    except Exception:
        try: c.rollback()
        except Exception: pass


def _fetch(url: str) -> dict:
    """Returns {status, bytes, hash, title}. Tolerates failure."""
    import requests
    out = {"status": 0, "bytes": 0, "hash": None, "title": None}
    try:
        r = requests.get(url, timeout=12, headers={
            "User-Agent": "DCHub-Competitor-Intel/1.0 (research only)",
            "Cache-Control": "no-cache",
        }, stream=True)
        body = r.raw.read(8192, decode_content=True) if r.raw else r.content[:8192]
        out["status"] = r.status_code
        out["bytes"] = len(body) if body else 0
        try: r.close()
        except Exception: pass
        if body:
            out["hash"] = hashlib.sha256(body).hexdigest()[:32]
            text = body.decode("utf-8", errors="ignore") if isinstance(body, bytes) else body
            # Cheap title extraction
            m = re.search(r"<title[^>]*>([^<]{1,200})</title>", text, re.I)
            if m: out["title"] = m.group(1).strip()[:160]
    except Exception:
        pass
    return out


def scan_competitors() -> dict:
    """Run a daily snapshot pass. Idempotent per (competitor, url, date)."""
    out: dict = {"scanned": 0, "snapshots": [], "errors": [],
                 "ran_at": datetime.datetime.utcnow().isoformat() + "Z"}
    c = _conn()
    if c is None:
        out["errors"].append("no_database"); return out
    try:
        _ensure_schema(c)
        with c.cursor() as cur:
            for comp in _COMPETITORS:
                for url in comp["urls"]:
                    r = _fetch(url)
                    out["scanned"] += 1
                    if r["status"] == 0:
                        out["errors"].append({"competitor": comp["slug"],
                                                "url": url, "err": "fetch_failed"})
                        continue
                    try:
                        cur.execute("""
                            INSERT INTO competitor_snapshots
                              (competitor_slug, url, snapshot_date,
                               content_hash, bytes_size, status_code,
                               title_extracted)
                            VALUES (%s, %s, CURRENT_DATE, %s, %s, %s, %s)
                            ON CONFLICT (competitor_slug, url, snapshot_date)
                            DO UPDATE SET content_hash = EXCLUDED.content_hash,
                                          bytes_size  = EXCLUDED.bytes_size,
                                          status_code = EXCLUDED.status_code,
                                          title_extracted = EXCLUDED.title_extracted,
                                          captured_at = NOW()
                        """, (comp["slug"], url, r["hash"], r["bytes"],
                              r["status"], r["title"]))
                    except Exception as e:
                        out["errors"].append({"competitor": comp["slug"],
                                                "url": url, "err": str(e)[:80]})
                        continue
                    out["snapshots"].append({"competitor": comp["slug"],
                                               "url": url,
                                               "status": r["status"],
                                               "bytes": r["bytes"],
                                               "title": r["title"]})
    finally:
        try: c.close()
        except Exception: pass
    return out


def compute_diffs(min_byte_delta_pct: float = 10.0) -> list[dict]:
    """For each (competitor, url), find pairs where today's hash !=
    yesterday's hash AND byte delta is >X%. Returns the meaningful
    changes — the press team's homework."""
    c = _conn()
    if c is None: return []
    out = []
    try:
        _ensure_schema(c)
        import psycopg2.extras
        with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                WITH paired AS (
                  SELECT
                    competitor_slug, url, snapshot_date,
                    content_hash, bytes_size, title_extracted,
                    LAG(content_hash) OVER (PARTITION BY competitor_slug, url
                                             ORDER BY snapshot_date) AS prev_hash,
                    LAG(bytes_size)   OVER (PARTITION BY competitor_slug, url
                                             ORDER BY snapshot_date) AS prev_bytes,
                    LAG(title_extracted) OVER (PARTITION BY competitor_slug, url
                                                 ORDER BY snapshot_date) AS prev_title
                    FROM competitor_snapshots
                   WHERE snapshot_date >= CURRENT_DATE - INTERVAL '7 days'
                )
                SELECT * FROM paired
                 WHERE prev_hash IS NOT NULL
                   AND content_hash IS DISTINCT FROM prev_hash
                 ORDER BY snapshot_date DESC LIMIT 50
            """)
            for r in cur.fetchall():
                prev_b = float(r["prev_bytes"] or 0) or 1.0
                delta_pct = 100.0 * abs((r["bytes_size"] or 0) - r["prev_bytes"]) / prev_b
                if delta_pct < min_byte_delta_pct and (r["title_extracted"] == r["prev_title"]):
                    continue
                out.append({
                    "competitor":      r["competitor_slug"],
                    "url":             r["url"],
                    "snapshot_date":   r["snapshot_date"].isoformat() if r["snapshot_date"] else None,
                    "byte_delta_pct":  round(delta_pct, 1),
                    "title_now":       r["title_extracted"],
                    "title_prev":      r["prev_title"],
                    "title_changed":   r["title_extracted"] != r["prev_title"],
                })
    finally:
        try: c.close()
        except Exception: pass
    return out


@competitor_intel_bp.route("/api/v1/competitors/scan", methods=["POST"])
def scan_endpoint():
    provided = (request.headers.get("X-Admin-Key") or "").strip()
    if _ADMIN_KEY and provided != _ADMIN_KEY:
        return jsonify(error="unauthorized"), 401
    return jsonify(scan_competitors()), 200


@competitor_intel_bp.route("/api/v1/competitors/snapshots", methods=["GET"])
def snapshots_endpoint():
    c = _conn()
    if c is None: return jsonify(error="no_database"), 503
    try:
        _ensure_schema(c)
        import psycopg2.extras
        with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT competitor_slug, url, snapshot_date, bytes_size,
                       status_code, title_extracted
                  FROM competitor_snapshots
                 WHERE snapshot_date >= CURRENT_DATE - INTERVAL '30 days'
                 ORDER BY snapshot_date DESC, competitor_slug LIMIT 200
            """)
            rows = cur.fetchall()
    finally:
        try: c.close()
        except Exception: pass
    out = [{
        "competitor":  r["competitor_slug"],
        "url":         r["url"],
        "date":        r["snapshot_date"].isoformat() if r["snapshot_date"] else None,
        "bytes":       int(r["bytes_size"] or 0),
        "status":      r["status_code"],
        "title":       r["title_extracted"],
    } for r in rows]
    resp = jsonify(snapshots=out, count=len(out))
    resp.headers["Cache-Control"] = "public, max-age=600"
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp, 200


@competitor_intel_bp.route("/api/v1/competitors/diffs", methods=["GET"])
def diffs_endpoint():
    out = compute_diffs(min_byte_delta_pct=10.0)
    resp = jsonify(diffs=out, count=len(out),
                   generated_at=datetime.datetime.utcnow().isoformat() + "Z")
    resp.headers["Cache-Control"] = "public, max-age=600"
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp, 200
