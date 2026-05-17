"""Phase ZZ-2 (2026-05-17) — tier-drift proposal recorder.

When the brain's tier-inconsistency detector finds a mismatch (e.g. MCP
tool gates at IDENTIFIED but the equivalent web endpoint gates at
DEVELOPER), Phase ZZ-2 promoted that finding from escalation-only to
autonomous. The autonomous action POSTs to this endpoint, which records
the proposal into tier_drift_proposals.

Endpoints:
  POST /api/v1/brain/tier-drift/propose  — record a proposal (autopilot)
  GET  /api/v1/brain/tier-drift          — list pending proposals (humans)

The actual decorator change is still a human code change. This module
just turns the brain's volatile finding (disappears on next radar tick)
into a durable worklist (queryable, mark-resolved-able).
"""
from __future__ import annotations
import os
import datetime
from flask import Blueprint, jsonify, request


tier_drift_bp = Blueprint("tier_drift_proposals", __name__)


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
CREATE TABLE IF NOT EXISTS tier_drift_proposals (
    id              BIGSERIAL PRIMARY KEY,
    tool            TEXT,
    web_path        TEXT NOT NULL,
    mcp_tier        TEXT,
    web_min_tier    TEXT,
    detail          TEXT,
    source          TEXT DEFAULT 'autopilot',
    status          TEXT NOT NULL DEFAULT 'pending',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    resolved_at     TIMESTAMPTZ,
    resolved_by     TEXT,
    resolution_note TEXT
);
-- Unique on (web_path, mcp_tier, web_min_tier) so the same drift
-- reported every radar tick collapses to one row rather than 100s
CREATE UNIQUE INDEX IF NOT EXISTS uq_tier_drift_active
    ON tier_drift_proposals(web_path, mcp_tier, web_min_tier)
    WHERE status = 'pending';
"""


def _ensure_schema(c):
    try:
        with c.cursor() as cur:
            cur.execute(_SCHEMA)
    except Exception:
        try: c.rollback()
        except Exception: pass


@tier_drift_bp.route("/api/v1/brain/tier-drift/propose", methods=["POST", "OPTIONS"])
def propose():
    if request.method == "OPTIONS":
        return jsonify(ok=True), 200
    c = _conn()
    if c is None:
        return jsonify(ok=False, error="no_database"), 503
    body = request.get_json(silent=True) or {}
    web_path = (body.get("web_path") or "").strip()
    if not web_path:
        return jsonify(ok=False, error="web_path required"), 400
    try:
        _ensure_schema(c)
        with c.cursor() as cur:
            cur.execute("""
                INSERT INTO tier_drift_proposals
                    (tool, web_path, mcp_tier, web_min_tier, detail, source)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (web_path, mcp_tier, web_min_tier)
                    WHERE status = %s
                DO NOTHING
                RETURNING id, created_at
            """, (
                (body.get("tool") or "")[:80] or None,
                web_path[:200],
                (body.get("mcp_tier") or "")[:30] or None,
                (body.get("web_min_tier") or "")[:30] or None,
                (body.get("detail") or "")[:1000],
                (body.get("source") or "autopilot")[:30],
                'pending',
            ))
            row = cur.fetchone()
            if row:
                pid, created = row[0], row[1]
                return jsonify(
                    ok=True,
                    proposal_id=pid,
                    created_at=created.isoformat() if created else None,
                    deduped=False,
                ), 200
            # Conflict — proposal already exists for this drift
            return jsonify(ok=True, deduped=True,
                            note="proposal already pending for this drift"), 200
    finally:
        try: c.close()
        except Exception: pass


@tier_drift_bp.route("/api/v1/brain/tier-drift", methods=["GET"])
def list_proposals():
    """Human-readable worklist. Public; no secrets exposed."""
    status_filter = (request.args.get("status") or "pending").strip().lower()
    c = _conn()
    if c is None:
        return jsonify(error="no_database"), 503
    try:
        _ensure_schema(c)
        import psycopg2.extras
        with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT id, tool, web_path, mcp_tier, web_min_tier, detail,
                       source, status, created_at, resolved_at, resolved_by,
                       resolution_note
                  FROM tier_drift_proposals
                 WHERE status = %s
                 ORDER BY created_at DESC LIMIT 50
            """, (status_filter,))
            rows = cur.fetchall() or []
        # Normalize timestamps
        for r in rows:
            for k in ("created_at", "resolved_at"):
                if r.get(k):
                    r[k] = r[k].isoformat()
        resp = jsonify(
            proposals=rows,
            count=len(rows),
            status_filter=status_filter,
            generated_at=datetime.datetime.utcnow().isoformat() + "Z",
            note=("Autonomously-recorded tier-drift proposals. Each row is "
                  "a place where an MCP tool tier disagrees with the "
                  "equivalent web endpoint's tier — the decorator alignment "
                  "is the fix. Mark resolved via POST "
                  "/api/v1/brain/tier-drift/<id>/resolve with "
                  "{resolved_by, resolution_note}."),
        )
        resp.headers["Cache-Control"] = "public, max-age=120"
        resp.headers["Access-Control-Allow-Origin"] = "*"
        return resp, 200
    finally:
        try: c.close()
        except Exception: pass


@tier_drift_bp.route("/api/v1/brain/tier-drift/<int:pid>/resolve",
                      methods=["POST", "OPTIONS"])
def resolve(pid: int):
    if request.method == "OPTIONS":
        return jsonify(ok=True), 200
    c = _conn()
    if c is None:
        return jsonify(ok=False, error="no_database"), 503
    body = request.get_json(silent=True) or {}
    try:
        _ensure_schema(c)
        with c.cursor() as cur:
            cur.execute("""
                UPDATE tier_drift_proposals
                   SET status = %s,
                       resolved_at = NOW(),
                       resolved_by = %s,
                       resolution_note = %s
                 WHERE id = %s
             RETURNING id
            """, ('resolved',
                  (body.get("resolved_by") or "human")[:80],
                  (body.get("resolution_note") or "")[:500],
                  pid))
            row = cur.fetchone()
            if not row:
                return jsonify(ok=False, error="proposal_not_found"), 404
            return jsonify(ok=True, resolved_id=pid), 200
    finally:
        try: c.close()
        except Exception: pass
