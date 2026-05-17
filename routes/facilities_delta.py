"""Phase HHHH (2026-05-16) — facility-count delta tracker.

User pain: "ai-inventory hasn't improved in weeks, same 12,553
facilities." That's a stagnant discovery pipeline. This module:

  1. Snapshots facility counts daily into facility_count_snapshots
  2. Exposes /api/v1/facilities/delta — what changed today/week/month
  3. Brain detector facility_count_stagnant fires if 7d delta is 0
  4. Cron daily writes a new snapshot (cron lives in
     .github/workflows/facility-snapshot.yml — added separately)

The detector turns "discovery silently stopped" from an invisible
problem into a heartbeat finding so it gets escalated like any other
regression.
"""

from __future__ import annotations

import os
import datetime
from flask import Blueprint, jsonify, request


facilities_delta_bp = Blueprint("facilities_delta", __name__)


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
CREATE TABLE IF NOT EXISTS facility_count_snapshots (
    snapshot_date  DATE PRIMARY KEY,
    total_count    INT NOT NULL,
    operating_count INT,
    pipeline_count INT,
    by_state       JSONB,
    captured_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
-- Phase KKKK (2026-05-16): add verified_count column. Raw total_count
-- counts every row in discovered_facilities; verified_count applies
-- the WHERE merged_at IS NULL AND is_duplicate = 0 filter that the
-- homepage /api/v1/stats uses (the 12,553 number the user saw). Both
-- are persisted so the brain can detect divergence (e.g., dedup
-- worker dies → verified stays flat while raw climbs).
ALTER TABLE facility_count_snapshots
    ADD COLUMN IF NOT EXISTS verified_count  INT,
    ADD COLUMN IF NOT EXISTS published_count INT;
CREATE INDEX IF NOT EXISTS ix_fcs_date_desc
    ON facility_count_snapshots(snapshot_date DESC);
"""


def _ensure_schema(c):
    try:
        with c.cursor() as cur:
            cur.execute(_SCHEMA)
    except Exception:
        try: c.rollback()
        except Exception: pass


def _current_counts(cur) -> dict:
    """Read current counts from discovered_facilities. Phase KKKK +
    OOOO (2026-05-16): returns three counts:
      - total     raw COUNT(*) of discovered_facilities (21,374)
      - verified  deduped: merged_at IS NULL AND is_duplicate = 0
      - published the count from the curated `facilities` table that
                   main.py:5862 reads for the homepage stat
    Three counts surface drift between the discovery pipeline, the
    dedup worker, and the published curation step — any pair
    diverging by >X% is a brain finding (Phase PPPP detector)."""
    out = {"total": 0, "verified": 0, "published": 0,
           "operating": 0, "pipeline": 0, "by_state": {}}
    try:
        cur.execute("SELECT to_regclass('public.discovered_facilities')")
        if not (cur.fetchone() or [None])[0]: return out
    except Exception: return out
    try:
        cur.execute("SELECT COUNT(*) FROM discovered_facilities")
        out["total"] = int((cur.fetchone() or [0])[0] or 0)
    except Exception: pass
    try:
        cur.execute("""
            SELECT COUNT(*) FROM discovered_facilities
             WHERE merged_at IS NULL AND is_duplicate = 0
        """)
        out["verified"] = int((cur.fetchone() or [0])[0] or 0)
    except Exception: pass
    # OOOO: also count the curated `facilities` table (what users see)
    try:
        cur.execute("SELECT to_regclass('public.facilities')")
        if (cur.fetchone() or [None])[0]:
            cur.execute("SELECT COUNT(*) FROM facilities")
            out["published"] = int((cur.fetchone() or [0])[0] or 0)
    except Exception: pass
    try:
        cur.execute("""
            SELECT COUNT(*) FROM discovered_facilities
             WHERE LOWER(COALESCE(status,'')) IN
                   ('operational','operating','live','active','running','in-service')
        """)
        out["operating"] = int((cur.fetchone() or [0])[0] or 0)
    except Exception: pass
    try:
        cur.execute("""
            SELECT COUNT(*) FROM discovered_facilities
             WHERE LOWER(COALESCE(status,'')) IN
                   ('construction','planned','permitting','under construction',
                    'proposed','development')
        """)
        out["pipeline"] = int((cur.fetchone() or [0])[0] or 0)
    except Exception: pass
    try:
        cur.execute("""
            SELECT UPPER(state) AS st, COUNT(*) AS n
              FROM discovered_facilities
             WHERE state IS NOT NULL AND state != ''
             GROUP BY UPPER(state)
             ORDER BY n DESC LIMIT 60
        """)
        out["by_state"] = {r[0]: int(r[1] or 0) for r in cur.fetchall()}
    except Exception: pass
    return out


def write_snapshot() -> dict:
    """Idempotent daily snapshot. Safe to call multiple times per day —
    UPSERT keeps only the last value for the date."""
    c = _conn()
    if c is None: return {"ok": False, "error": "no_database"}
    out = {"ok": False}
    try:
        _ensure_schema(c)
        with c.cursor() as cur:
            counts = _current_counts(cur)
            import json
            cur.execute("""
                INSERT INTO facility_count_snapshots
                  (snapshot_date, total_count, verified_count, published_count,
                   operating_count, pipeline_count, by_state)
                VALUES (CURRENT_DATE, %s, %s, %s, %s, %s, %s::jsonb)
                ON CONFLICT (snapshot_date) DO UPDATE
                  SET total_count = EXCLUDED.total_count,
                      verified_count = EXCLUDED.verified_count,
                      published_count = EXCLUDED.published_count,
                      operating_count = EXCLUDED.operating_count,
                      pipeline_count = EXCLUDED.pipeline_count,
                      by_state = EXCLUDED.by_state,
                      captured_at = NOW()
            """, (counts["total"], counts["verified"], counts["published"],
                  counts["operating"], counts["pipeline"],
                  json.dumps(counts["by_state"])))
            out = {"ok": True, **counts}
    finally:
        try: c.close()
        except Exception: pass
    return out


def compute_delta() -> dict:
    """Returns current + 1d/7d/30d deltas (None if no baseline)."""
    c = _conn()
    if c is None: return {"error": "no_database"}
    out: dict = {
        "current": None,
        "deltas":  {"1d": None, "7d": None, "30d": None},
        "snapshots_available": 0,
        "stagnant_days_7d": 0,
    }
    try:
        _ensure_schema(c)
        with c.cursor() as cur:
            # Live current
            counts = _current_counts(cur)
            out["current"] = counts
            # Snapshot count
            cur.execute("SELECT COUNT(*), MAX(snapshot_date) FROM facility_count_snapshots")
            r = cur.fetchone() or (0, None)
            out["snapshots_available"] = int(r[0] or 0)
            out["latest_snapshot"] = r[1].isoformat() if r[1] else None
            # Deltas — Phase KKKK includes verified_count too so the
            # transparency dashboard can show dedup-pipeline drift.
            for label, days in (("1d", 1), ("7d", 7), ("30d", 30)):
                try:
                    cur.execute("""
                        SELECT total_count, verified_count,
                               operating_count, pipeline_count
                          FROM facility_count_snapshots
                         WHERE snapshot_date <= CURRENT_DATE - INTERVAL '%s days'
                         ORDER BY snapshot_date DESC LIMIT 1
                    """, (days,))
                    p = cur.fetchone()
                    if p:
                        out["deltas"][label] = {
                            "total":     counts["total"]     - int(p[0] or 0),
                            "verified":  counts["verified"]  - int(p[1] or 0),
                            "operating": counts["operating"] - int(p[2] or 0),
                            "pipeline":  counts["pipeline"]  - int(p[3] or 0),
                            "baseline_total":    int(p[0] or 0),
                            "baseline_verified": int(p[1] or 0),
                        }
                except Exception: pass
            # How many of the last 7 days had ZERO net growth?
            try:
                cur.execute("""
                    WITH lagged AS (
                      SELECT snapshot_date, total_count,
                             LAG(total_count) OVER (ORDER BY snapshot_date) AS prev_total
                        FROM facility_count_snapshots
                       WHERE snapshot_date >= CURRENT_DATE - INTERVAL '8 days'
                    )
                    SELECT COUNT(*) FROM lagged
                     WHERE prev_total IS NOT NULL AND total_count - prev_total = 0
                """)
                out["stagnant_days_7d"] = int((cur.fetchone() or [0])[0] or 0)
            except Exception: pass
    finally:
        try: c.close()
        except Exception: pass
    return out


@facilities_delta_bp.route("/api/v1/facilities/delta", methods=["GET"])
def facilities_delta():
    d = compute_delta()
    d["generated_at"] = datetime.datetime.utcnow().isoformat() + "Z"
    resp = jsonify(d)
    resp.headers["Cache-Control"] = "public, max-age=300"
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp, 200


@facilities_delta_bp.route("/api/v1/facilities/snapshot", methods=["POST"])
def facilities_snapshot():
    """Admin-only: write today's snapshot. Called by daily cron."""
    expected = (os.environ.get("DCHUB_ADMIN_KEY")
                or os.environ.get("DCHUB_INTERNAL_KEY") or "").strip()
    provided = (request.headers.get("X-Admin-Key") or "").strip()
    if expected and provided != expected:
        return jsonify(error="unauthorized"), 401
    return jsonify(write_snapshot()), 200
