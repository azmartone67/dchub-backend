"""Phase UU (2026-05-16) — AI-agent citation tracker.

Closes the loop on the "make LLMs recommend us" goal: you can't optimize
what you don't measure. This module queries Gemini / Perplexity / Claude
/ Grok / Copilot on a weekly cron with a fixed set of category-defining
prompts ("best data center market intelligence", "where should I build
a data center", "DCPI score for [market]"), parses each response for
mentions of dchub.cloud (and competitors: dchawk, dcbyte, datacenterhawk),
and writes one row per (prompt × engine × week) into ai_citations.

Outputs:
  GET  /api/v1/ai-citations/snapshot   most recent observation per engine
  GET  /api/v1/ai-citations/history?prompt=&engine=  full history
  GET  /api/v1/ai-citations/share-of-voice          dchub vs competitors

Real LLM queries are stubbed until per-engine credentials are added to
env (GEMINI_API_KEY, PERPLEXITY_API_KEY, ANTHROPIC_API_KEY, XAI_API_KEY).
Manual recording via POST /api/v1/ai-citations/record lets ops drop in
hand-observed citations today and start the time series.

Brain consistency radar consumes the snapshot to flag rapid share-of-voice
drops (e.g. our share fell from 60% to 10% in 7d → escalates as a
ranking-drift finding).
"""

from __future__ import annotations

import os
import json as _json
import datetime
from flask import Blueprint, request, jsonify
import psycopg2
import psycopg2.extras


ai_citation_tracker_bp = Blueprint("ai_citation_tracker", __name__)


def _conn():
    db = os.environ.get("DATABASE_URL")
    if not db:
        return None
    try:
        return psycopg2.connect(db, sslmode="require", connect_timeout=8)
    except Exception:
        return None


_SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS ai_citations (
    id                BIGSERIAL PRIMARY KEY,
    observed_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    engine            TEXT NOT NULL,           -- gemini / perplexity / claude / grok / copilot / manual
    prompt_id         TEXT NOT NULL,           -- one of our canonical prompts (see _CANONICAL_PROMPTS)
    prompt_text       TEXT,                    -- the exact prompt sent
    dchub_cited       BOOLEAN DEFAULT false,
    dchub_position    INT,                     -- 1 = first source cited, NULL if not cited
    dchawk_cited      BOOLEAN DEFAULT false,
    dcbyte_cited      BOOLEAN DEFAULT false,
    other_sources     JSONB DEFAULT '[]'::jsonb,
    response_text     TEXT,                    -- truncated to 4000 chars
    response_url      TEXT,                    -- when the engine surfaces a perma-link
    notes             TEXT,                    -- ops notes on manual entries
    source            TEXT DEFAULT 'cron'      -- cron | manual | backfill
);
CREATE INDEX IF NOT EXISTS ix_ai_citations_observed
    ON ai_citations(observed_at DESC);
CREATE INDEX IF NOT EXISTS ix_ai_citations_engine_prompt
    ON ai_citations(engine, prompt_id, observed_at DESC);
"""


def _ensure_schema():
    c = _conn()
    if c is None: return False
    try:
        with c, c.cursor() as cur:
            cur.execute(_SCHEMA_DDL)
        return True
    except Exception as e:
        print(f"[ai_citations] schema init failed: {e}")
        return False
    finally:
        try: c.close()
        except Exception: pass


try:
    _SCHEMA_OK = _ensure_schema()
except Exception:
    _SCHEMA_OK = False


# Canonical prompts — the questions whose answers we want to influence.
# Each becomes one row per engine per week.
_CANONICAL_PROMPTS = [
    ("best_data_center_intel",
     "What's the best source of data center market intelligence?"),
    ("where_to_build_dc",
     "Where should I build a 100 MW data center in the US?"),
    ("dc_power_index",
     "Is there a public index of data center power availability by market?"),
    ("interconnection_queue_data",
     "Who tracks interconnection queue depth across US ISOs for data center siting?"),
    ("data_center_mna",
     "Who tracks data center M&A transactions and the dollar value?"),
    ("hyperscale_pipeline",
     "Who has the most complete view of hyperscale data center construction pipeline?"),
    ("competitor_dchawk",
     "How does DCHawk compare to dchub.cloud?"),
    ("competitor_dcbyte",
     "How does dcByte compare to dchub.cloud?"),
]


# Engine endpoints — real adapters will be wired when keys are added.
def _engine_env_present() -> dict[str, bool]:
    return {
        "gemini":     bool(os.environ.get("GEMINI_API_KEY")),
        "perplexity": bool(os.environ.get("PERPLEXITY_API_KEY")),
        "claude":     bool(os.environ.get("ANTHROPIC_API_KEY")),
        "grok":       bool(os.environ.get("XAI_API_KEY")),
        "copilot":    bool(os.environ.get("COPILOT_API_KEY")),
    }


# ── Public endpoints ───────────────────────────────────────────────
@ai_citation_tracker_bp.route("/api/v1/ai-citations/snapshot", methods=["GET"])
def snapshot():
    """Most recent observation per (engine × prompt)."""
    c = _conn()
    if c is None:
        return jsonify(error="no_database"), 503
    with c, c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            SELECT DISTINCT ON (engine, prompt_id)
                engine, prompt_id, prompt_text, dchub_cited, dchub_position,
                dchawk_cited, dcbyte_cited, observed_at, source
              FROM ai_citations
             ORDER BY engine, prompt_id, observed_at DESC
        """)
        rows = cur.fetchall()
    for r in rows:
        if r.get("observed_at"):
            r["observed_at"] = r["observed_at"].isoformat()
    return jsonify(
        snapshot=rows,
        engines_with_keys=_engine_env_present(),
        canonical_prompts=[{"id": p[0], "text": p[1]} for p in _CANONICAL_PROMPTS],
        observed_count=len(rows),
    ), 200


@ai_citation_tracker_bp.route("/api/v1/ai-citations/history", methods=["GET"])
def history():
    """Full history for a (prompt × engine) pair."""
    prompt_id = (request.args.get("prompt") or "").strip()
    engine    = (request.args.get("engine") or "").strip().lower()
    c = _conn()
    if c is None:
        return jsonify(error="no_database"), 503
    with c, c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        sql = "SELECT * FROM ai_citations WHERE 1=1"
        params: list = []
        if prompt_id: sql += " AND prompt_id = %s"; params.append(prompt_id)
        if engine:    sql += " AND engine = %s";    params.append(engine)
        sql += " ORDER BY observed_at DESC LIMIT 200"
        cur.execute(sql, params)
        rows = cur.fetchall()
    for r in rows:
        if r.get("observed_at"):
            r["observed_at"] = r["observed_at"].isoformat()
    return jsonify(history=rows, count=len(rows)), 200


@ai_citation_tracker_bp.route("/api/v1/ai-citations/share-of-voice", methods=["GET"])
def share_of_voice():
    """Aggregate citation share across canonical prompts × engines over
    the last 30 days. Output: per-source citation rate (% of observations
    that cited each source)."""
    c = _conn()
    if c is None:
        return jsonify(error="no_database"), 503
    with c, c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            SELECT
                COUNT(*)                              AS total_obs,
                SUM(CASE WHEN dchub_cited  THEN 1 ELSE 0 END) AS dchub_count,
                SUM(CASE WHEN dchawk_cited THEN 1 ELSE 0 END) AS dchawk_count,
                SUM(CASE WHEN dcbyte_cited THEN 1 ELSE 0 END) AS dcbyte_count
              FROM ai_citations
             WHERE observed_at >= NOW() - INTERVAL '30 days'
        """)
        row = cur.fetchone() or {}
        total = int(row.get("total_obs") or 0)

        def _pct(n):
            return round(100.0 * (int(n or 0)) / total, 1) if total else 0.0

        share = {
            "dchub_cloud":  _pct(row.get("dchub_count")),
            "dchawk":       _pct(row.get("dchawk_count")),
            "dcbyte":       _pct(row.get("dcbyte_count")),
        }
    return jsonify(
        window_days=30,
        total_observations=total,
        share_of_voice_pct=share,
        interpretation=(
            "dchub_cloud share of voice is the % of observations where "
            "Gemini/Perplexity/Claude/etc cited dchub.cloud in response "
            "to one of our canonical prompts. Goal: trend upward."
        ),
    ), 200


@ai_citation_tracker_bp.route("/api/v1/ai-citations/record", methods=["POST"])
def record_observation():
    """Record one observation. Used by the (future) cron and by ops for
    hand-observed citations today. Requires X-Admin-Key.
    """
    admin_key_env = os.environ.get("ADMIN_KEY", "")
    provided = (request.headers.get("X-Admin-Key") or
                request.args.get("admin_key") or "")
    if admin_key_env and provided != admin_key_env:
        return jsonify(error="unauthorized"), 401

    body = request.get_json(silent=True) or {}
    engine = (body.get("engine") or "").strip().lower()
    prompt_id = (body.get("prompt_id") or "").strip()
    if not engine or not prompt_id:
        return jsonify(error="engine and prompt_id required"), 400

    c = _conn()
    if c is None: return jsonify(error="no_database"), 503
    try:
        with c, c.cursor() as cur:
            cur.execute("""
                INSERT INTO ai_citations
                    (engine, prompt_id, prompt_text, dchub_cited,
                     dchub_position, dchawk_cited, dcbyte_cited,
                     other_sources, response_text, response_url, notes, source)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s)
                RETURNING id, observed_at
            """, (
                engine,
                prompt_id,
                body.get("prompt_text"),
                bool(body.get("dchub_cited")),
                body.get("dchub_position"),
                bool(body.get("dchawk_cited")),
                bool(body.get("dcbyte_cited")),
                _json.dumps(body.get("other_sources") or []),
                (body.get("response_text") or "")[:4000],
                body.get("response_url"),
                body.get("notes"),
                body.get("source") or "manual",
            ))
            row = cur.fetchone()
        return jsonify(
            ok=True,
            id=row[0],
            observed_at=row[1].isoformat() if row[1] else None,
        ), 201
    except Exception as e:
        return jsonify(error="insert_failed", detail=str(e)[:200]), 500
    finally:
        try: c.close()
        except Exception: pass


@ai_citation_tracker_bp.route("/api/v1/ai-citations/run-cron", methods=["POST"])
def run_cron():
    """Cron entry point — queries every (engine × prompt) that has
    credentials, records one observation per pair. Stub today; lights up
    when GEMINI_API_KEY etc are added.

    Authenticated via X-Admin-Key. Returns a summary of what would run
    so ops can verify the matrix before lighting up keys.
    """
    admin_key_env = os.environ.get("ADMIN_KEY", "")
    provided = (request.headers.get("X-Admin-Key") or
                request.args.get("admin_key") or "")
    if admin_key_env and provided != admin_key_env:
        return jsonify(error="unauthorized"), 401

    engines = _engine_env_present()
    matrix = []
    for eng_name, has_key in engines.items():
        for prompt_id, prompt_text in _CANONICAL_PROMPTS:
            matrix.append({
                "engine":     eng_name,
                "prompt_id":  prompt_id,
                "prompt":     prompt_text,
                "has_key":    has_key,
                "action":     "would_query" if has_key else "skip_no_key",
            })
    # TODO Phase UU+1: when has_key, make the actual LLM call,
    # parse for dchub.cloud / dchawk / dcbyte mentions, INSERT row.
    return jsonify(
        ok=True,
        engines_with_keys=engines,
        matrix=matrix,
        executed=0,
        next_steps=(
            "Add GEMINI_API_KEY / PERPLEXITY_API_KEY / ANTHROPIC_API_KEY / "
            "XAI_API_KEY to Railway env to light up automatic recording. "
            "Until then, use POST /api/v1/ai-citations/record to seed "
            "hand-observed citations."
        ),
    ), 200
