"""Model Relations shell — HTTP surface (2026-07-11).

POST /api/jobs/model-relations             admin; async tick (worker-proxied
                                           via _WORKER_PROXY_POST_PATHS — the
                                           r-eval-fixwave-2 pool lesson).
                                           ?platforms=openai,mistral to scope.
GET  /api/v1/admin/model-relations/status  admin; recent runs + verdicts for
                                           HUMAN review. Publication of any
                                           verdict stays manual + consent-
                                           gated — this endpoint is the review
                                           queue, not a publisher.
"""

from __future__ import annotations

import os
import json
from flask import Blueprint, jsonify, request

model_relations_bp = Blueprint("model_relations", __name__)


def _admin_ok():
    expected = (os.environ.get("DCHUB_ADMIN_KEY") or os.environ.get("DCHUB_INTERNAL_KEY") or "").strip()
    provided = (request.headers.get("X-Admin-Key") or request.args.get("admin_key") or "").strip()
    return bool(expected) and provided == expected


@model_relations_bp.route("/api/jobs/model-relations", methods=["POST"])
def job_model_relations():
    if not _admin_ok():
        return jsonify(ok=False, error="unauthorized", hint="X-Admin-Key required"), 401
    plats = [p.strip() for p in (request.args.get("platforms") or "").split(",") if p.strip()] or None
    try:
        import threading
        from model_relations import run_model_relations_tick
        threading.Thread(target=run_model_relations_tick, args=(plats,),
                         name="model-relations-tick", daemon=True).start()
        return jsonify(ok=True, job="model-relations",
                       result=f"tick started (async, platforms={plats or 'all'})"), 200
    except Exception as e:
        return jsonify(ok=False, error=str(e)[:200]), 500


@model_relations_bp.route("/api/v1/admin/model-relations/status")
def model_relations_status():
    if not _admin_ok():
        return jsonify(ok=False, error="unauthorized"), 401
    try:
        import psycopg2
        conn = psycopg2.connect(os.environ["DATABASE_URL"], sslmode="require", connect_timeout=5)
        cur = conn.cursor()
        cur.execute(
            "SELECT id, platform, model_id, status, calls_made, http_5xx, "
            "verdict_diff, notes, started_at, verdict "
            "FROM model_relations_runs ORDER BY started_at DESC LIMIT 20")
        runs = [{"id": r[0], "platform": r[1], "model": r[2], "status": r[3],
                 "calls": r[4], "http_5xx": r[5], "diff": r[6], "notes": r[7],
                 "at": r[8].isoformat() if r[8] else None,
                 "assessment": ((r[9] or {}).get("assessment") if isinstance(r[9], dict) else None)}
                for r in cur.fetchall()]
        conn.close()
        return jsonify(ok=True, runs=runs,
                       note=("Review queue only — verdicts publish via the manual "
                             "consent flow, never from here.")), 200
    except Exception as e:
        return jsonify(ok=False, error=str(e)[:200]), 500
