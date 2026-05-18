"""Phase III (2026-05-17) — cron-collision proposal recorder.

When the brain's cron_schedule_collision detector finds two workflows
sharing the same cron minute (thundering herd against Railway), Phase
III promoted that finding from escalation-only to autonomous. The
autonomous action POSTs to this endpoint, which records the proposal
into cron_collision_proposals.

Endpoints:
  POST /api/v1/brain/cron-collision/propose   — record (autopilot)
  GET  /api/v1/brain/cron-collision           — list pending (humans)
  POST /api/v1/brain/cron-collision/<id>/resolve  — mark resolved

Same pattern as routes/tier_drift_proposals.py.
"""
from __future__ import annotations
import os
import datetime
from flask import Blueprint, jsonify, request


cron_collision_bp = Blueprint("cron_collision_proposals", __name__)


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
CREATE TABLE IF NOT EXISTS cron_collision_proposals (
    id              BIGSERIAL PRIMARY KEY,
    collision_minute TEXT,
    detail          TEXT,
    count           INT,
    source          TEXT DEFAULT 'autopilot',
    status          TEXT NOT NULL DEFAULT 'pending',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    resolved_at     TIMESTAMPTZ,
    resolved_by     TEXT,
    resolution_note TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_cron_collision_active
    ON cron_collision_proposals(collision_minute)
    WHERE status = 'pending';
"""


def _ensure_schema(c):
    try:
        with c.cursor() as cur:
            cur.execute(_SCHEMA)
    except Exception:
        try: c.rollback()
        except Exception: pass


@cron_collision_bp.route("/api/v1/brain/cron-collision/propose",
                          methods=["POST", "OPTIONS"])
def propose():
    if request.method == "OPTIONS":
        return jsonify(ok=True), 200
    c = _conn()
    if c is None:
        return jsonify(ok=False, error="no_database"), 503
    body = request.get_json(silent=True) or {}
    cm = (body.get("collision_minute") or "").strip()
    try:
        _ensure_schema(c)
        with c.cursor() as cur:
            cur.execute("""
                INSERT INTO cron_collision_proposals
                    (collision_minute, detail, count, source)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (collision_minute) WHERE status = %s
                DO NOTHING
                RETURNING id, created_at
            """, (
                cm[:80] or 'unknown',
                (body.get("detail") or "")[:1000],
                body.get("count"),
                (body.get("source") or "autopilot")[:30],
                'pending',
            ))
            row = cur.fetchone()
            if row:
                return jsonify(ok=True, proposal_id=row[0],
                                created_at=row[1].isoformat() if row[1] else None,
                                deduped=False), 200
            return jsonify(ok=True, deduped=True,
                            note="proposal already pending for this collision"), 200
    finally:
        try: c.close()
        except Exception: pass


@cron_collision_bp.route("/api/v1/brain/cron-collision", methods=["GET"])
def list_proposals():
    status_filter = (request.args.get("status") or "pending").strip().lower()
    c = _conn()
    if c is None:
        return jsonify(error="no_database"), 503
    try:
        _ensure_schema(c)
        import psycopg2.extras
        with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT id, collision_minute, detail, count, source, status,
                       created_at, resolved_at, resolved_by, resolution_note
                  FROM cron_collision_proposals
                 WHERE status = %s
                 ORDER BY created_at DESC LIMIT 50
            """, (status_filter,))
            rows = cur.fetchall() or []
        for r in rows:
            for k in ("created_at", "resolved_at"):
                if r.get(k):
                    r[k] = r[k].isoformat()
        resp = jsonify(
            proposals=rows,
            count=len(rows),
            status_filter=status_filter,
            generated_at=datetime.datetime.utcnow().isoformat() + "Z",
            note=("Autonomously-recorded cron-collision proposals. "
                  "Each row is a (cron minute) where two workflows fire "
                  "simultaneously — stagger one by N minutes in its .yml. "
                  "Mark resolved via POST .../resolve."),
        )
        resp.headers["Cache-Control"] = "public, max-age=120"
        resp.headers["Access-Control-Allow-Origin"] = "*"
        return resp, 200
    finally:
        try: c.close()
        except Exception: pass


@cron_collision_bp.route("/api/v1/brain/cron-collision/<int:pid>/resolve",
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
                UPDATE cron_collision_proposals
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
