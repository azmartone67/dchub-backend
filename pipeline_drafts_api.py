"""
DC Hub — Pipeline Drafts API
Replit Python backend for news-to-pipeline staging queue.

Endpoints:
  POST   /api/pipeline/drafts           — Submit new draft(s) from news digest
  GET    /api/pipeline/drafts           — List drafts (filterable by status)
  GET    /api/pipeline/drafts/:id       — Get single draft
  POST   /api/pipeline/drafts/:id/approve  — Promote to capacity_pipeline
  POST   /api/pipeline/drafts/:id/reject   — Reject draft
  GET    /api/pipeline/drafts/stats     — Quick counts by status
  POST   /api/pipeline/drafts/batch     — Batch approve/reject

Env vars required:
  DATABASE_URL     — Neon connection string
  DRAFTS_API_KEY   — Bearer token for auth
"""

import os
import json
import logging
from datetime import datetime, timezone
from functools import wraps

from flask import Flask, request, jsonify, abort
import psycopg2
from psycopg2.extras import RealDictCursor

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
app = Flask(__name__)
app.config["JSON_SORT_KEYS"] = False
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("pipeline-drafts")

DATABASE_URL = os.environ.get("DATABASE_URL")
DRAFTS_API_KEY = os.environ.get("DRAFTS_API_KEY", "")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def get_db():
    """Return a new psycopg2 connection to Neon."""
    return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)


def require_auth(f):
    """Simple bearer-token auth."""
    @wraps(f)
    def wrapper(*args, **kwargs):
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer ") or auth[7:] != DRAFTS_API_KEY:
            return jsonify({"error": "Unauthorized"}), 401
        return f(*args, **kwargs)
    return wrapper


def row_to_dict(row):
    """Convert psycopg2 RealDictRow, handling datetimes."""
    if row is None:
        return None
    d = dict(row)
    for k, v in d.items():
        if isinstance(v, datetime):
            d[k] = v.isoformat()
    return d

# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/api/pipeline/drafts", methods=["POST"])
@require_auth
def create_drafts():
    body = request.get_json(force=True)
    if "drafts" in body:
        drafts = body["drafts"]
    else:
        drafts = [body]
    if not drafts:
        return jsonify({"error": "No drafts provided"}), 400
    inserted = []
    skipped = []
    conn = get_db()
    try:
        cur = conn.cursor()
        for d in drafts:
            company = d.get("company", "").strip()
            project = d.get("project", "").strip()
            market = d.get("market", "").strip()
            if not all([company, project, market]):
                skipped.append({"draft": d, "reason": "Missing required field"})
                continue
            try:
                cur.execute("""
                    INSERT INTO pipeline_drafts (
                        company, project, market, capacity_mw, investment_m,
                        status, delivery, type, preleased,
                        confidence, source_title, source_url, source_date,
                        matched_pipeline_id, match_type, notes
                    ) VALUES (
                        %(company)s, %(project)s, %(market)s, %(capacity_mw)s, %(investment_m)s,
                        %(status)s, %(delivery)s, %(type)s, %(preleased)s,
                        %(confidence)s, %(source_title)s, %(source_url)s, %(source_date)s,
                        %(matched_pipeline_id)s, %(match_type)s, %(notes)s
                    )
                    RETURNING id, company, project, market, capacity_mw, confidence, draft_status
                """, {
                    "company": company, "project": project, "market": market,
                    "capacity_mw": d.get("capacity_mw"), "investment_m": d.get("investment_m"),
                    "status": d.get("status", "announced"), "delivery": d.get("delivery", "TBD"),
                    "type": d.get("type", "hyperscale"), "preleased": d.get("preleased", False),
                    "confidence": d.get("confidence", 0.5), "source_title": d.get("source_title"),
                    "source_url": d.get("source_url"), "source_date": d.get("source_date"),
                    "matched_pipeline_id": d.get("matched_pipeline_id"),
                    "match_type": d.get("match_type", "new"), "notes": d.get("notes"),
                })
                inserted.append(row_to_dict(cur.fetchone()))
            except psycopg2.errors.UniqueViolation:
                conn.rollback()
                skipped.append({"company": company, "project": project, "reason": "Duplicate"})
        conn.commit()
    finally:
        conn.close()
    return jsonify({"success": True, "inserted": len(inserted), "skipped": len(skipped), "drafts": inserted, "skipped_details": skipped if skipped else None}), 201


@app.route("/api/pipeline/drafts", methods=["GET"])
@require_auth
def list_drafts():
    draft_status = request.args.get("status", "pending")
    company = request.args.get("company", "")
    limit = min(int(request.args.get("limit", 50)), 200)
    offset = int(request.args.get("offset", 0))
    sort = request.args.get("sort", "confidence")
    sort_col = "confidence DESC" if sort == "confidence" else "created_at DESC"
    conn = get_db()
    try:
        cur = conn.cursor()
        where_clauses = ["draft_status = %s"]
        params = [draft_status]
        if company:
            where_clauses.append("company ILIKE %s")
            params.append(f"%{company}%")
        where_sql = " AND ".join(where_clauses)
        cur.execute(f"SELECT * FROM pipeline_drafts WHERE {where_sql} ORDER BY {sort_col} LIMIT %s OFFSET %s", params + [limit, offset])
        rows = cur.fetchall()
        cur.execute(f"SELECT COUNT(*) as cnt FROM pipeline_drafts WHERE {where_sql}", params)
        total = cur.fetchone()["cnt"]
    finally:
        conn.close()
    return jsonify({"success": True, "total": total, "count": len(rows), "drafts": [row_to_dict(r) for r in rows]})


@app.route("/api/pipeline/drafts/stats", methods=["GET"])
@require_auth
def draft_stats():
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT draft_status, COUNT(*) as count, COALESCE(SUM(capacity_mw), 0) as total_mw, ROUND(AVG(confidence)::numeric, 2) as avg_confidence FROM pipeline_drafts GROUP BY draft_status")
        rows = cur.fetchall()
    finally:
        conn.close()
    return jsonify({"success": True, "stats": {r["draft_status"]: row_to_dict(r) for r in rows}})


@app.route("/api/pipeline/drafts/<int:draft_id>", methods=["GET"])
@require_auth
def get_draft(draft_id):
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM pipeline_drafts WHERE id = %s", (draft_id,))
        row = cur.fetchone()
    finally:
        conn.close()
    if not row:
        return jsonify({"error": "Draft not found"}), 404
    return jsonify({"success": True, "draft": row_to_dict(row)})


@app.route("/api/pipeline/drafts/<int:draft_id>/approve", methods=["POST"])
@require_auth
def approve_draft(draft_id):
    body = request.get_json(force=True) if request.data else {}
    notes = body.get("notes", "")
    overrides = body.get("overrides", {})
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM pipeline_drafts WHERE id = %s AND draft_status = 'pending'", (draft_id,))
        draft = cur.fetchone()
        if not draft:
            return jsonify({"error": "Draft not found or already reviewed"}), 404
        company = overrides.get("company", draft["company"])
        project = overrides.get("project", draft["project"])
        market = overrides.get("market", draft["market"])
        capacity = overrides.get("capacity_mw", draft["capacity_mw"])
        investment = overrides.get("investment_m", draft["investment_m"])
        status = overrides.get("status", draft["status"])
        delivery = overrides.get("delivery", draft["delivery"])
        proj_type = overrides.get("type", draft["type"])
        preleased = overrides.get("preleased", draft["preleased"])
        if draft["match_type"] == "new":
            cur.execute("INSERT INTO capacity_pipeline (company, project, market, capacity_mw, investment_millions, status, expected_delivery, type, preleased) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT (company) DO UPDATE SET project = EXCLUDED.project, market = EXCLUDED.market, capacity_mw = EXCLUDED.capacity_mw, investment_millions = EXCLUDED.investment_millions, status = EXCLUDED.status, expected_delivery = EXCLUDED.expected_delivery, type = EXCLUDED.type, preleased = EXCLUDED.preleased RETURNING id",
                (company, project, market, capacity, investment, status, delivery, proj_type, preleased))
            new_id = cur.fetchone()["id"]
        else:
            pid = draft["matched_pipeline_id"]
            if pid:
                update_map = {"update_status": ("status", status), "update_capacity": ("capacity_mw", capacity), "update_operator": ("company", company), "update_delivery": ("expected_delivery", delivery)}
                if draft["match_type"] in update_map:
                    col, val = update_map[draft["match_type"]]
                    cur.execute(f"UPDATE capacity_pipeline SET {col} = %s WHERE id = %s", (val, pid))
            new_id = pid
        cur.execute("UPDATE pipeline_drafts SET draft_status = 'approved', reviewed_at = NOW(), reviewed_by = %s, notes = COALESCE(notes, '') || %s WHERE id = %s",
            ("api", f" | Approved: {notes}" if notes else "", draft_id))
        conn.commit()
    finally:
        conn.close()
    return jsonify({"success": True, "action": "approved", "draft_id": draft_id, "match_type": draft["match_type"]})


@app.route("/api/pipeline/drafts/<int:draft_id>/reject", methods=["POST"])
@require_auth
def reject_draft(draft_id):
    body = request.get_json(force=True) if request.data else {}
    reason = body.get("reason", "Rejected by reviewer")
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("UPDATE pipeline_drafts SET draft_status = 'rejected', reviewed_at = NOW(), reviewed_by = 'api', notes = COALESCE(notes, '') || %s WHERE id = %s AND draft_status = 'pending' RETURNING id",
            (f" | Rejected: {reason}", draft_id))
        row = cur.fetchone()
        conn.commit()
    finally:
        conn.close()
    if not row:
        return jsonify({"error": "Draft not found or already reviewed"}), 404
    return jsonify({"success": True, "action": "rejected", "draft_id": draft_id})


@app.route("/api/pipeline/drafts/batch", methods=["POST"])
@require_auth
def batch_action():
    body = request.get_json(force=True)
    action = body.get("action")
    ids = body.get("ids", [])
    notes = body.get("notes", "")
    if action not in ("approve", "reject"):
        return jsonify({"error": "action must be 'approve' or 'reject'"}), 400
    if not ids:
        return jsonify({"error": "No IDs provided"}), 400
    results = []
    for draft_id in ids:
        if action == "approve":
            with app.test_request_context(json={"notes": notes}):
                approve_draft(draft_id)
        else:
            with app.test_request_context(json={"reason": notes}):
                reject_draft(draft_id)
        results.append({"id": draft_id, "status": action + "d"})
    return jsonify({"success": True, "results": results})


@app.route("/api/pipeline/drafts/health", methods=["GET"])
def health():
    try:
        conn = get_db()
        try:
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) as cnt FROM pipeline_drafts WHERE draft_status = 'pending'")
            pending = cur.fetchone()["cnt"]
        finally:
            conn.close()
        return jsonify({"status": "healthy", "pending_drafts": pending})
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=False)
