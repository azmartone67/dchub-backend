"""Candidate lifecycle v1 (2026-07-11) — ChatGPT co-design round 1.

The executable-handoff contract: a search result becomes a durable, opaque
`candidate_id` that downstream tools consume WITHOUT re-specifying (or
re-interpreting) coordinates, filters, or assumptions. Spec by GPT-5.5 from
its live evaluation, adopted with three grounding deltas (tool-name mapping
to the REAL surface, snapshot = queue-load vintage, TTL 7d to match data
freshness — not an arbitrary 30).

Contract invariants (the doc page mirrors these — keep them in lockstep):
- candidate_id: opaque, deterministic within a snapshot ('cand_' +
  md5(queue_id|snapshot_id)[:20]) — re-searching the same snapshot returns
  the SAME id (stable), a new queue vintage mints new ids.
- snapshot_id: immutable ('snap_<queue max loaded_at date>').
- Identity ≠ freshness: expires_at = first mint + 7 days. Past it, every
  consumer returns the DETERMINISTIC error {"error": "candidate_expired"}
  — never a silent recompute against newer data (fail closed).
- No reinterpretation: downstream reads the FROZEN row minted at search
  time (coords, capacity, iso, fuel) even if the live queue row has since
  changed. Fresh data = explicit new search.
- Every consumer echoes the identity block: candidate_id, snapshot_id,
  search_version, analysis_version, methodology_version, retrieved_at,
  citation. analysis_version is deliberately separate from search_version
  (ChatGPT's refinement: analyze_site evolves faster than the search index;
  an auditor must know which moved).
- resolve_candidate stays NARROW: identity, location, snapshot metadata,
  provenance, originating search context. Never analysis. No automatic
  lineage (v1 keeps the contract simpler than a workflow engine — also
  ChatGPT's call).

DDL runs lazily at first mint per process (never in a boot path). SQL uses
only %s placeholders + ::jsonb casts so the SAME helpers serve psycopg3
(interconnection_queues) and psycopg2 (main.py site-score).
"""

from __future__ import annotations

import os
import json
import hashlib
from datetime import datetime, timezone
from flask import Blueprint, jsonify, request

candidates_bp = Blueprint("candidates", __name__)

SEARCH_VERSION = "refined-queue/2026-07-11"
TTL_DAYS = 7
_CITATION = {"source": "DC Hub (dchub.cloud) — ISO interconnection queues + EIA",
             "license": "CC-BY-4.0"}

_DDL = """
CREATE TABLE IF NOT EXISTS search_candidates (
  candidate_id  TEXT PRIMARY KEY,
  snapshot_id   TEXT NOT NULL,
  queue_id      TEXT,
  project_name  TEXT,
  iso           TEXT,
  state         TEXT,
  county        TEXT,
  fuel_type     TEXT,
  capacity_mw   DOUBLE PRECISION,
  lat           DOUBLE PRECISION,
  lng           DOUBLE PRECISION,
  fiber_km      DOUBLE PRECISION,
  coordinate_precision TEXT,
  search_context JSONB,
  minted_at     TIMESTAMPTZ DEFAULT now(),
  expires_at    TIMESTAMPTZ NOT NULL
);
"""
_ddl_done = False


def _ensure_ddl(cur):
    global _ddl_done
    if not _ddl_done:
        cur.execute(_DDL)
        _ddl_done = True


def candidate_id_for(queue_id, snapshot_id):
    return "cand_" + hashlib.md5(f"{queue_id}|{snapshot_id}".encode()).hexdigest()[:20]


def mint_candidates(cur, survivors, snapshot_id, search_context):
    """Mint (idempotently) a candidate per survivor dict; stamps candidate_id,
    snapshot_id, expires_at onto each survivor IN PLACE. ON CONFLICT DO NOTHING
    preserves the ORIGINAL mint's expiry — re-searching never extends a TTL."""
    _ensure_ddl(cur)
    ctx = json.dumps(search_context or {})[:4000]
    for s in survivors:
        qid = s.get("queue_id") or s.get("project_name") or ""
        cid = candidate_id_for(qid, snapshot_id)
        cur.execute(
            "INSERT INTO search_candidates (candidate_id, snapshot_id, queue_id, "
            "project_name, iso, state, county, fuel_type, capacity_mw, lat, lng, "
            "fiber_km, coordinate_precision, search_context, expires_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb, "
            "now() + (%s || ' days')::interval) "
            "ON CONFLICT (candidate_id) DO NOTHING",
            (cid, snapshot_id, s.get("queue_id"), s.get("project_name"),
             s.get("iso"), s.get("state"), s.get("county"), s.get("fuel_type"),
             s.get("capacity_mw"), s.get("lat"), s.get("lng"), s.get("fiber_km"),
             s.get("coordinate_precision"), ctx, str(TTL_DAYS)))
        cur.execute("SELECT expires_at FROM search_candidates WHERE candidate_id=%s", (cid,))
        row = cur.fetchone()
        s["candidate_id"] = cid
        s["snapshot_id"] = snapshot_id
        s["expires_at"] = (row[0].isoformat() if row and row[0] else None)
    return survivors


def load_candidate(cur, candidate_id):
    """→ (frozen_dict | None, expired: bool). Frozen values only — the
    no-reinterpretation guarantee lives here."""
    _ensure_ddl(cur)
    cur.execute(
        "SELECT candidate_id, snapshot_id, queue_id, project_name, iso, state, "
        "county, fuel_type, capacity_mw, lat, lng, fiber_km, coordinate_precision, "
        "search_context, minted_at, expires_at, (expires_at < now()) AS expired "
        "FROM search_candidates WHERE candidate_id = %s", (candidate_id,))
    r = cur.fetchone()
    if not r:
        return None, False
    keys = ["candidate_id", "snapshot_id", "queue_id", "project_name", "iso",
            "state", "county", "fuel_type", "capacity_mw", "lat", "lng",
            "fiber_km", "coordinate_precision", "search_context", "minted_at",
            "expires_at", "expired"]
    d = dict(zip(keys, r))
    expired = bool(d.pop("expired"))
    return d, expired


def expired_response(candidate_id):
    """The deterministic fail-closed error — identical from every consumer."""
    return {"error": "candidate_expired",
            "message": "Re-run the search to obtain a fresh candidate.",
            "candidate_id": candidate_id}


def candidate_echo(cand, analysis_version, methodology_version=None):
    """The identity block every downstream response carries."""
    return {
        "candidate_id": cand.get("candidate_id"),
        "snapshot_id": cand.get("snapshot_id"),
        "search_version": SEARCH_VERSION,
        "analysis_version": analysis_version,
        "methodology_version": methodology_version,
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
        "citation": dict(_CITATION,
                         as_of=str(cand.get("snapshot_id") or "").replace("snap_", "")),
    }


@candidates_bp.route("/docs/candidate-lifecycle")
def candidate_lifecycle_doc():
    """The contract's document (ChatGPT's closing suggestion: a page an agent
    or developer can internalize and build against). Kept in lockstep with the
    module docstring invariants above."""
    from flask import send_from_directory
    return send_from_directory("static", "candidate-lifecycle.html")


@candidates_bp.route("/api/v1/resolve-candidate")
def resolve_candidate():
    """Narrow by contract: identity, location, snapshot metadata, provenance,
    originating search context. NEVER analysis, never a recompute."""
    cid = (request.args.get("candidate_id") or "").strip()
    if not cid.startswith("cand_"):
        return jsonify(ok=False, _entity="error",
                       error="candidate_id required (cand_…)"), 400
    import psycopg2
    db = os.environ.get("NEON_REPLICA_URL") or os.environ.get("DATABASE_URL")
    if not db:
        return jsonify(ok=False, _entity="error", error="no_db"), 503
    conn = psycopg2.connect(db, sslmode="require", connect_timeout=5)
    conn.autocommit = True
    try:
        cand, expired = load_candidate(conn.cursor(), cid)
        if cand is None:
            return jsonify(ok=False, _entity="error", error="unknown_candidate",
                           message="No such candidate_id — it may predate the "
                                   "candidate contract, or the id is mistyped."), 404
        if expired:
            return jsonify(dict(expired_response(cid), _entity="error", ok=False)), 410
        ctx = cand.get("search_context")
        if isinstance(ctx, str):
            try:
                ctx = json.loads(ctx)
            except Exception:
                pass
        return jsonify({
            "_entity": "candidate",
            "ok": True,
            "candidate_id": cand["candidate_id"],
            "snapshot_id": cand["snapshot_id"],
            "identity": {"queue_id": cand["queue_id"],
                         "project_name": cand["project_name"],
                         "iso": cand["iso"], "fuel_type": cand["fuel_type"],
                         "capacity_mw": cand["capacity_mw"]},
            "location": {"lat": cand["lat"], "lng": cand["lng"],
                         "state": cand["state"], "county": cand["county"],
                         "coordinate_precision": cand["coordinate_precision"],
                         "fiber_km": cand["fiber_km"]},
            "search_context": ctx,
            "minted_at": cand["minted_at"].isoformat() if cand.get("minted_at") else None,
            "expires_at": cand["expires_at"].isoformat() if cand.get("expires_at") else None,
            "echo": candidate_echo(cand, analysis_version="resolve/1.0"),
            "_cite": "DC Hub (dchub.cloud)",
        }), 200
    finally:
        try:
            conn.close()
        except Exception:
            pass
