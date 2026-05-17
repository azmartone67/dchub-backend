"""Phase UUUU (2026-05-16) — pattern-library self-growth.

The brain has 30+ detectors but only ~9 autopilot patterns. Operators
manually fix the others. This module turns observed manual fixes into
proposed patterns automatically.

Flow:
  1. Operator manually resolves finding X (e.g., bumps a cron, fixes
     a SQL column) → posts /api/v1/brain/mark-resolved with {issue,
     action_taken (free-text or short slug), notes}
  2. brain_resolution_log persists the resolution
  3. After 3+ identical (issue_prefix, action_taken) tuples, the
     detector pattern_proposal_candidate fires
  4. Heartbeat surfaces "operator fixed X manually 3 times with
     'action_taken' Y — propose autopilot pattern?" finding
  5. Future Phase: brain auto-generates the pattern stub as a PR

  POST /api/v1/brain/mark-resolved      operator log a fix
  GET  /api/v1/brain/resolution-log     public — last 50 resolutions
  GET  /api/v1/brain/pattern-proposals  public — patterns the brain
                                                   would auto-create
"""

from __future__ import annotations

import os
import datetime
from flask import Blueprint, jsonify, request


pattern_growth_bp = Blueprint("pattern_growth", __name__)


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
CREATE TABLE IF NOT EXISTS brain_resolution_log (
    id              BIGSERIAL PRIMARY KEY,
    issue           TEXT NOT NULL,
    issue_prefix    TEXT NOT NULL,        -- issue.split(':',1)[0]
    action_taken    TEXT NOT NULL,
    notes           TEXT,
    resolved_by     TEXT NOT NULL DEFAULT 'operator',
    resolved_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_brain_resolution_prefix_action
    ON brain_resolution_log(issue_prefix, action_taken);
"""


def _ensure_schema(c):
    try:
        with c.cursor() as cur:
            cur.execute(_SCHEMA)
    except Exception:
        try: c.rollback()
        except Exception: pass


@pattern_growth_bp.route("/api/v1/brain/mark-resolved", methods=["POST"])
def mark_resolved():
    provided = (request.headers.get("X-Admin-Key") or "").strip()
    if _ADMIN_KEY and provided != _ADMIN_KEY:
        return jsonify(error="unauthorized"), 401
    d = request.get_json(silent=True) or {}
    issue = (d.get("issue") or "").strip()[:200]
    action = (d.get("action_taken") or "").strip()[:200]
    notes = (d.get("notes") or "").strip()[:500] or None
    who = (d.get("by") or "operator").strip()[:80]
    if not issue or not action:
        return jsonify(error="issue_and_action_taken_required"), 400
    prefix = issue.split(":", 1)[0]

    c = _conn()
    if c is None: return jsonify(error="no_database"), 503
    try:
        _ensure_schema(c)
        with c.cursor() as cur:
            cur.execute("""
                INSERT INTO brain_resolution_log
                  (issue, issue_prefix, action_taken, notes, resolved_by)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT DO NOTHING
                RETURNING id
            """, (issue, prefix, action, notes, who))
            r = cur.fetchone()
    finally:
        try: c.close()
        except Exception: pass
    return jsonify(ok=True, recorded_id=int(r[0]) if r else None,
                   message=f"Recorded: {issue} → {action} (by {who})"), 200


def compute_proposals(min_matches: int = 3) -> list[dict]:
    """Group resolutions by (issue_prefix, action_taken); return
    groups with >= min_matches as proposed autopilot patterns."""
    c = _conn()
    if c is None: return []
    out = []
    try:
        _ensure_schema(c)
        import psycopg2.extras
        with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT issue_prefix, action_taken,
                       COUNT(*) AS match_count,
                       MIN(resolved_at) AS first_seen,
                       MAX(resolved_at) AS last_seen,
                       array_agg(DISTINCT issue) AS issue_examples
                  FROM brain_resolution_log
                 GROUP BY issue_prefix, action_taken
                HAVING COUNT(*) >= %s
                 ORDER BY COUNT(*) DESC LIMIT 50
            """, (min_matches,))
            for r in cur.fetchall():
                # Already in pattern library? Skip.
                try:
                    from routes.brain_autopilot import _PATTERN_LIBRARY
                    if r["issue_prefix"] in _PATTERN_LIBRARY:
                        continue
                except Exception:
                    pass
                out.append({
                    "issue_prefix":      r["issue_prefix"],
                    "proposed_action":   r["action_taken"],
                    "match_count":       int(r["match_count"] or 0),
                    "first_seen":        r["first_seen"].isoformat() if r["first_seen"] else None,
                    "last_seen":         r["last_seen"].isoformat() if r["last_seen"] else None,
                    "issue_examples":    list(r["issue_examples"] or [])[:5],
                    "proposed_pattern_stub": (
                        f'"{r["issue_prefix"]}": {{\n'
                        f'    "action":      lambda f: ("{r["action_taken"]}", {{}}),\n'
                        f'    "method":      "POST",\n'
                        f'    "use_admin":   False,\n'
                        f'    "description": "Auto-proposed: operator resolved this {r["match_count"]} times with {r["action_taken"]}",\n'
                        f'}},'
                    ),
                })
    finally:
        try: c.close()
        except Exception: pass
    return out


@pattern_growth_bp.route("/api/v1/brain/pattern-proposals", methods=["GET"])
def proposals():
    out = compute_proposals(min_matches=3)
    resp = jsonify(proposals=out, count=len(out),
                   note=("Patterns the brain proposes auto-creating "
                         "based on observed manual resolutions. Copy "
                         "the proposed_pattern_stub into routes/"
                         "brain_autopilot.py:_PATTERN_LIBRARY to ship."),
                   generated_at=datetime.datetime.utcnow().isoformat() + "Z")
    resp.headers["Cache-Control"] = "public, max-age=600"
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp, 200


@pattern_growth_bp.route("/api/v1/brain/resolution-log", methods=["GET"])
def resolution_log():
    c = _conn()
    if c is None: return jsonify(error="no_database"), 503
    try:
        _ensure_schema(c)
        import psycopg2.extras
        with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT issue, action_taken, notes, resolved_by, resolved_at
                  FROM brain_resolution_log
                 ORDER BY resolved_at DESC LIMIT 50
            """)
            rows = cur.fetchall()
    finally:
        try: c.close()
        except Exception: pass
    out = [{
        "issue":        r["issue"],
        "action_taken": r["action_taken"],
        "notes":        r["notes"],
        "resolved_by":  r["resolved_by"],
        "resolved_at":  r["resolved_at"].isoformat() if r["resolved_at"] else None,
    } for r in rows]
    return jsonify(log=out, count=len(out)), 200
