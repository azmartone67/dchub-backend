"""Phase MMMM (2026-05-16) — radar finding history (sparkline data).

Persists the brain consistency-radar finding count daily so the
/transparency console can render a 14-day sparkline showing whether
the system is getting healthier or noisier over time.

  POST /api/v1/radar/snapshot          (admin) write today's count
  GET  /api/v1/radar/history           public — last 30 days of counts

The snapshot endpoint is called by the same daily cron as the
facility-snapshot (.github/workflows/facility-snapshot-daily.yml) to
keep cron count low — just one more curl in the same workflow.
"""

from __future__ import annotations

import os
import datetime
from flask import Blueprint, jsonify, request


radar_history_bp = Blueprint("radar_history", __name__)


_ADMIN_KEY = (os.environ.get("DCHUB_ADMIN_KEY")
              or os.environ.get("DCHUB_INTERNAL_KEY") or "").strip()


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
CREATE TABLE IF NOT EXISTS radar_finding_snapshots (
    snapshot_date  DATE PRIMARY KEY,
    finding_count  INT NOT NULL,
    type_count     INT NOT NULL,
    by_type        JSONB,
    captured_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_rfs_date_desc
    ON radar_finding_snapshots(snapshot_date DESC);
"""


def _ensure_schema(c):
    try:
        with c.cursor() as cur:
            cur.execute(_SCHEMA)
    except Exception:
        try: c.rollback()
        except Exception: pass


def write_snapshot() -> dict:
    """Read current radar state + persist. Idempotent per-day."""
    out: dict = {"ok": False}
    try:
        from routes.brain_consistency_radar import scan_summary
        r = scan_summary()
    except Exception as e:
        out["error"] = f"scan_failed:{type(e).__name__}"
        return out
    finding_count = int(r.get("count") or 0)
    by_issue      = r.get("by_issue") or {}
    type_count    = len(by_issue)
    c = _conn()
    if c is None:
        out["error"] = "no_database"
        return out
    try:
        _ensure_schema(c)
        with c.cursor() as cur:
            import json as _json
            cur.execute("""
                INSERT INTO radar_finding_snapshots
                  (snapshot_date, finding_count, type_count, by_type)
                VALUES (CURRENT_DATE, %s, %s, %s::jsonb)
                ON CONFLICT (snapshot_date) DO UPDATE
                  SET finding_count = EXCLUDED.finding_count,
                      type_count    = EXCLUDED.type_count,
                      by_type       = EXCLUDED.by_type,
                      captured_at   = NOW()
            """, (finding_count, type_count, _json.dumps(by_issue)))
        out = {"ok": True, "finding_count": finding_count,
                "type_count": type_count}
    finally:
        try: c.close()
        except Exception: pass
    return out


def read_history(days: int = 30) -> dict:
    """Return list of (date, finding_count, type_count) for the last
    N days. The /transparency sparkline reads this."""
    c = _conn()
    if c is None: return {"history": [], "days": days}
    out_rows = []
    try:
        _ensure_schema(c)
        with c.cursor() as cur:
            cur.execute("""
                SELECT snapshot_date, finding_count, type_count
                  FROM radar_finding_snapshots
                 WHERE snapshot_date >= CURRENT_DATE - INTERVAL '%s days'
                 ORDER BY snapshot_date ASC
            """, (days,))
            for r in cur.fetchall():
                out_rows.append({
                    "date":          r[0].isoformat() if r[0] else None,
                    "finding_count": int(r[1] or 0),
                    "type_count":    int(r[2] or 0),
                })
    finally:
        try: c.close()
        except Exception: pass
    return {"history": out_rows, "days": days,
            "snapshots_available": len(out_rows)}


@radar_history_bp.route("/api/v1/radar/snapshot", methods=["POST"])
def radar_snapshot():
    """Admin-only cron entry point."""
    provided = (request.headers.get("X-Admin-Key") or "").strip()
    if _ADMIN_KEY and provided != _ADMIN_KEY:
        return jsonify(error="unauthorized"), 401
    return jsonify(write_snapshot()), 200


@radar_history_bp.route("/api/v1/radar/history", methods=["GET"])
def radar_history():
    try: days = max(1, min(90, int(request.args.get("days") or 30)))
    except (ValueError, TypeError): days = 30
    d = read_history(days)
    d["generated_at"] = datetime.datetime.utcnow().isoformat() + "Z"
    resp = jsonify(d)
    resp.headers["Cache-Control"] = "public, max-age=600"
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp, 200
