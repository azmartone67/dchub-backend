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


class CandidateReadUnavailable(Exception):
    """A TRANSIENT candidate-read failure (replica unreachable, statement
    timeout, connection reset) — as opposed to a genuinely-absent candidate.
    Consumers map this to 503 'retry later', NEVER to 404 unknown / a silent
    drop (audit 2026-07-12: collapsing transient faults into 'unknown 404'
    told agents 'mistyped/re-search' when the truth was 'retry', and could
    mask an EXPIRED candidate's deterministic 410). The contract's whole
    value is deterministic, retry-classifiable errors."""


def _is_missing_table(exc):
    """True iff exc is Postgres UndefinedTable (42P01) — the legitimate
    pre-first-mint case where the table doesn't exist yet → treat as
    'no candidates', not a transient fault."""
    code = getattr(exc, "pgcode", None)
    if code == "42P01":
        return True
    return "does not exist" in str(exc).lower() and "relation" in str(exc).lower()

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
    no-reinterpretation guarantee lives here.

    READ-ONLY by contract: never runs DDL. load_candidate is served from the
    READ REPLICA (resolve-candidate / site-score / cluster all read there),
    where CREATE TABLE fails 'read-only transaction'. The table is created by
    mint_candidates on the PRIMARY; a reader assumes it exists and, if it
    somehow doesn't (pre-first-mint on a fresh DB), the SELECT's
    UndefinedTable is caught → (None, False) = 'unknown candidate', never a
    500. (Prior bug: _ensure_ddl here 500'd any read that ran before a mint
    in the same process — production only survived on mint-first ordering +
    the _ddl_done process global.)"""
    try:
        cur.execute(
            "SELECT candidate_id, snapshot_id, queue_id, project_name, iso, state, "
            "county, fuel_type, capacity_mw, lat, lng, fiber_km, coordinate_precision, "
            "search_context, minted_at, expires_at, (expires_at < now()) AS expired "
            "FROM search_candidates WHERE candidate_id = %s", (candidate_id,))
    except Exception as e:
        try:
            cur.connection.rollback()
        except Exception:
            pass
        # audit-fix 2026-07-12: distinguish the two failure modes the old bare
        # catch conflated. Missing table (pre-first-mint) is legitimately
        # "no candidates" → (None, False). Anything else is a TRANSIENT fault →
        # raise so the caller returns 503/declares-degraded, never a false
        # 'unknown 404' (which would also mask an expired candidate's 410).
        if _is_missing_table(e):
            return None, False
        raise CandidateReadUnavailable(str(e)[:200]) from e
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
    # audit-fix 2026-07-12: connect() INSIDE the try (was outside → a replica
    # connect failure/timeout 500'd instead of the 503 the no_db case returns
    # for the same DB-unavailable condition). Transient reads → 503 retry,
    # never a 500 'server bug' or a false 404 'unknown'.
    conn = None
    try:
        conn = psycopg2.connect(db, sslmode="require", connect_timeout=5)
        conn.autocommit = True
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
    except CandidateReadUnavailable as e:
        return jsonify(ok=False, _entity="error", error="candidate_read_unavailable",
                       message="Candidate store temporarily unavailable — retry.",
                       detail=str(e)[:160]), 503
    except Exception as e:
        # a psycopg2 connect() failure (replica unreachable/timeout) lands here
        return jsonify(ok=False, _entity="error", error="candidate_read_unavailable",
                       message="Candidate store temporarily unavailable — retry.",
                       detail=str(e)[:160]), 503
    finally:
        try:
            if conn is not None:
                conn.close()
        except Exception:
            pass
