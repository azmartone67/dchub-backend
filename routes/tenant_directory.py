"""Phase CCCCC (2026-05-16) — tenant directory MVP.

Per-building tenant data is DCHawk's last unique moat ("who's in
cage 4B"). Real auto-scraping needs CRE comps + SEC filings + news
NLP — bigger phase. This module ships the FOUNDATION so when we
have data, surfacing it is one endpoint away.

  POST /api/v1/tenants/ingest         admin — bulk-ingest tenant rows
  GET  /api/v1/facilities/<id>/tenants per-facility tenant list
  GET  /api/v1/tenants/coverage       coverage report (% of top facilities)

  Brain detector check_tenant_coverage_thin: flags top-50 facilities
  by power_mw that have zero tenants recorded. Closes the gap by
  surfacing the gap.

The ingest endpoint takes a JSON array so external sources (manual
research, paid data feeds, NLP pipelines) can post structured tenant
records. Each record has confidence + source so the brain can later
weigh quality.
"""

from __future__ import annotations

import os
import datetime
from flask import Blueprint, jsonify, request


tenant_directory_bp = Blueprint("tenant_directory", __name__)


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
CREATE TABLE IF NOT EXISTS facility_tenants (
    id              BIGSERIAL PRIMARY KEY,
    facility_id     TEXT NOT NULL,
    tenant_name     TEXT NOT NULL,
    tenant_type     TEXT,        -- hyperscaler|enterprise|colo|broker|other
    estimated_mw    REAL,
    signed_date     DATE,
    source          TEXT NOT NULL,   -- sec_filing|news|press|manual|nlp
    source_url      TEXT,
    confidence      REAL DEFAULT 0.5,  -- 0-1
    notes           TEXT,
    captured_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_tenants_facility ON facility_tenants(facility_id);
CREATE INDEX IF NOT EXISTS ix_tenants_name ON facility_tenants(tenant_name);
CREATE UNIQUE INDEX IF NOT EXISTS uq_tenants_dedup
    ON facility_tenants(facility_id, tenant_name, COALESCE(source, ''));
"""


def _ensure_schema(c):
    try:
        with c.cursor() as cur:
            cur.execute(_SCHEMA)
    except Exception:
        try: c.rollback()
        except Exception: pass


@tenant_directory_bp.route("/api/v1/tenants/ingest", methods=["POST"])
def ingest():
    """Admin bulk ingest. Body: {tenants: [{facility_id, tenant_name,
    tenant_type, estimated_mw, signed_date, source, source_url,
    confidence, notes}, ...]}"""
    provided = (request.headers.get("X-Admin-Key") or "").strip()
    if _ADMIN_KEY and provided != _ADMIN_KEY:
        return jsonify(error="unauthorized"), 401
    d = request.get_json(silent=True) or {}
    rows = d.get("tenants") or []
    if not isinstance(rows, list) or not rows:
        return jsonify(error="tenants_array_required"), 400
    c = _conn()
    if c is None: return jsonify(error="no_database"), 503
    inserted = 0
    errors = []
    try:
        _ensure_schema(c)
        with c.cursor() as cur:
            for r in rows[:500]:  # safety cap
                try:
                    fid = (r.get("facility_id") or "").strip()[:80]
                    name = (r.get("tenant_name") or "").strip()[:200]
                    if not fid or not name:
                        errors.append({"row": r, "err": "missing_facility_id_or_tenant_name"})
                        continue
                    cur.execute("""
                        INSERT INTO facility_tenants
                          (facility_id, tenant_name, tenant_type, estimated_mw,
                           signed_date, source, source_url, confidence, notes)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (facility_id, tenant_name, COALESCE(source, ''))
                        DO UPDATE SET tenant_type = COALESCE(EXCLUDED.tenant_type, facility_tenants.tenant_type),
                                      estimated_mw = COALESCE(EXCLUDED.estimated_mw, facility_tenants.estimated_mw),
                                      signed_date = COALESCE(EXCLUDED.signed_date, facility_tenants.signed_date),
                                      source_url = COALESCE(EXCLUDED.source_url, facility_tenants.source_url),
                                      confidence = GREATEST(EXCLUDED.confidence, facility_tenants.confidence),
                                      notes = COALESCE(EXCLUDED.notes, facility_tenants.notes),
                                      captured_at = NOW()
                    """, (
                        fid, name,
                        (r.get("tenant_type") or "").strip()[:40] or None,
                        float(r.get("estimated_mw")) if r.get("estimated_mw") is not None else None,
                        r.get("signed_date") or None,
                        (r.get("source") or "manual").strip()[:40],
                        (r.get("source_url") or "")[:500] or None,
                        max(0.0, min(1.0, float(r.get("confidence") or 0.5))),
                        (r.get("notes") or "")[:500] or None,
                    ))
                    inserted += 1
                except Exception as e:
                    errors.append({"row": r, "err": str(e)[:120]})
    finally:
        try: c.close()
        except Exception: pass
    return jsonify(ok=True, ingested=inserted, errors=errors[:5],
                   error_count=len(errors)), 200


@tenant_directory_bp.route("/api/v1/facilities/<facility_id>/tenants", methods=["GET"])
def facility_tenants(facility_id):
    c = _conn()
    if c is None: return jsonify(error="no_database"), 503
    try:
        _ensure_schema(c)
        import psycopg2.extras
        with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT tenant_name, tenant_type, estimated_mw,
                       signed_date, source, source_url, confidence, notes,
                       captured_at
                  FROM facility_tenants
                 WHERE facility_id = %s
                 ORDER BY confidence DESC, captured_at DESC LIMIT 100
            """, (facility_id,))
            rows = cur.fetchall()
    finally:
        try: c.close()
        except Exception: pass
    out = [{
        "tenant_name":   r["tenant_name"],
        "tenant_type":   r["tenant_type"],
        "estimated_mw":  float(r["estimated_mw"]) if r["estimated_mw"] is not None else None,
        "signed_date":   r["signed_date"].isoformat() if r["signed_date"] else None,
        "source":        r["source"],
        "source_url":    r["source_url"],
        "confidence":    float(r["confidence"]) if r["confidence"] is not None else None,
        "notes":         r["notes"],
        "captured_at":   r["captured_at"].isoformat() if r["captured_at"] else None,
    } for r in rows]
    resp = jsonify(facility_id=facility_id, tenants=out, count=len(out))
    resp.headers["Cache-Control"] = "public, max-age=600"
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp, 200


@tenant_directory_bp.route("/api/v1/tenants/coverage", methods=["GET"])
def coverage():
    """% of top-50 facilities (by power_mw) that have at least one
    tenant recorded. Used by /transparency + brain detector."""
    c = _conn()
    if c is None: return jsonify(error="no_database"), 503
    out = {"top50_with_tenants": 0, "top50_total": 0,
           "coverage_pct": 0.0, "total_tenant_rows": 0}
    try:
        _ensure_schema(c)
        with c.cursor() as cur:
            try:
                cur.execute("""
                    WITH top50 AS (
                      SELECT id::text AS fid
                        FROM discovered_facilities
                       WHERE merged_at IS NULL AND is_duplicate = 0
                         AND power_mw IS NOT NULL
                       ORDER BY power_mw DESC LIMIT 50
                    )
                    SELECT COUNT(*) FILTER (WHERE ft.tenant_name IS NOT NULL) AS with_tenants,
                           COUNT(*) AS total
                      FROM top50 t
                      LEFT JOIN facility_tenants ft ON ft.facility_id = t.fid
                """)
                r = cur.fetchone() or (0, 0)
                with_t, total = int(r[0] or 0), int(r[1] or 0)
                out["top50_with_tenants"] = with_t
                out["top50_total"]        = total
                out["coverage_pct"]       = round(100.0 * with_t / max(1, total), 1)
                cur.execute("SELECT COUNT(*) FROM facility_tenants")
                out["total_tenant_rows"] = int((cur.fetchone() or [0])[0] or 0)
            except Exception: pass
    finally:
        try: c.close()
        except Exception: pass
    resp = jsonify(out)
    resp.headers["Cache-Control"] = "public, max-age=600"
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp, 200
