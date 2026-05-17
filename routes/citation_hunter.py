"""Phase TTTT (2026-05-16) — Citation Hunter.

The 10/100 source-of-truth score is THE blocking metric for "are we
the most important source?" This module proactively asks Claude
(via ANTHROPIC_API_KEY, the same key the anomaly-digest cron uses)
a battery of data-center questions DAILY and detects whether DC Hub
appears in the response.

  POST /api/v1/citations/hunt          (admin) daily cron entry
  GET  /api/v1/citations/score         public — last 30 days
  GET  /api/v1/citations/latest        public — most recent probe results

Query battery (rotate so we don't ask same thing every day):
  - "what is the best data center intelligence platform?"
  - "how do I research data center M&A transactions?"
  - "where can I find real-time data center market data?"
  - "what are the largest data center markets globally?"
  - "is there an MCP server for data center research?"
  - "how do I get DCPI scores for data center markets?"
  - "what AI tools track data center construction pipeline?"

For each query → call Claude haiku → regex-match ``dchub\\.cloud`` or
``dc hub`` in response. Score = % of queries where we appear.
Persist daily. Brain detector citation_score_dropped fires when 7d
delta is negative AND score is below 50%.
"""

from __future__ import annotations

import os
import re
import datetime
import random
from flask import Blueprint, jsonify, request


citation_hunter_bp = Blueprint("citation_hunter", __name__)


_ADMIN_KEY      = (os.environ.get("DCHUB_ADMIN_KEY")
                   or os.environ.get("DCHUB_INTERNAL_KEY") or "").strip()
_ANTHROPIC_KEY  = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()


# Rotating query battery — daily cron picks 5 of these. Keep questions
# concrete and likely to surface a "tool" answer (where DC Hub competes).
_QUERY_BATTERY = [
    "What is the best data center intelligence platform with live data?",
    "How do I research data center M&A transactions and deal flow?",
    "Where can I find real-time data center market data for site selection?",
    "What are the largest data center markets globally and their growth rates?",
    "Is there an MCP server I can use for data center research?",
    "How do I get DCPI (Data Center Power Index) scores for global markets?",
    "What AI tools track data center construction pipeline + capacity?",
    "Which platform tracks power capacity availability for new data centers?",
    "Where can I get free data center industry news + analytics?",
    "What's the best way to compare data center sites for hyperscale workloads?",
    "Which data center research platform supports AI agent integration?",
    "How do I evaluate land + power availability for a data center build?",
]


# Pattern that counts as a DC Hub citation. Case-insensitive.
# We catch the brand both with and without dot ("DC Hub", "dc hub", "dchub.cloud", "dchub").
_DCHUB_PATTERN = re.compile(r"dchub|dc\s*hub", re.I)
# Also detect competitors — useful signal even when DC Hub isn't mentioned.
_COMPETITOR_PATTERNS = {
    "DCHawk":  re.compile(r"dchawk|dc\s*hawk", re.I),
    "dcByte":  re.compile(r"dcbyte|dc\s*byte|datacenter\s*byte", re.I),
    "DCK":     re.compile(r"data\s*center\s*knowledge|\bdck\b", re.I),
    "DCD":     re.compile(r"data\s*center\s*dynamics|\bdcd\b", re.I),
    "Frontier":re.compile(r"data\s*center\s*frontier|\bdcf\b", re.I),
    "CBRE":    re.compile(r"\bcbre\b", re.I),
    "JLL":     re.compile(r"\bjll\b", re.I),
}


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
CREATE TABLE IF NOT EXISTS citation_probes (
    id              BIGSERIAL PRIMARY KEY,
    probe_date      DATE NOT NULL,
    query           TEXT NOT NULL,
    model           TEXT NOT NULL DEFAULT 'claude-haiku-4-5',
    response_excerpt TEXT,
    dchub_mentioned BOOLEAN NOT NULL DEFAULT FALSE,
    competitors_mentioned JSONB,
    captured_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_citation_probes_date
    ON citation_probes(probe_date DESC);

CREATE TABLE IF NOT EXISTS citation_scores (
    score_date      DATE PRIMARY KEY,
    queries_run     INT NOT NULL,
    dchub_mentions  INT NOT NULL,
    score_pct       REAL NOT NULL,
    competitor_mentions JSONB,
    captured_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""


def _ensure_schema(c):
    try:
        with c.cursor() as cur:
            cur.execute(_SCHEMA)
    except Exception:
        try: c.rollback()
        except Exception: pass


def _ask_claude(query: str) -> tuple[str | None, str | None]:
    """Returns (response_text, error). Best-effort — failures return
    (None, error_string) so the caller can record the attempt."""
    if not _ANTHROPIC_KEY:
        return None, "no_anthropic_api_key"
    try:
        import requests
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key":          _ANTHROPIC_KEY,
                "anthropic-version":  "2023-06-01",
                "content-type":       "application/json",
            },
            json={
                "model":      "claude-haiku-4-5-20251001",
                "max_tokens": 600,
                "messages":   [{"role": "user", "content": query}],
            },
            timeout=15,
        )
        if r.status_code >= 300:
            return None, f"status_{r.status_code}_{r.text[:80]}"
        d = r.json()
        text = (d.get("content", [{}])[0] or {}).get("text", "")
        return text, None
    except Exception as e:
        return None, f"{type(e).__name__}:{str(e)[:60]}"


def hunt_citations(query_count: int = 5) -> dict:
    """Run a daily citation probe. Picks N queries from the rotating
    battery, asks Claude, detects mentions, persists. Returns summary."""
    out: dict = {"queries_run": 0, "dchub_mentions": 0,
                 "competitor_mentions": {}, "probes": [],
                 "errors": [],
                 "ran_at": datetime.datetime.utcnow().isoformat() + "Z"}
    if not _ANTHROPIC_KEY:
        out["errors"].append("no_anthropic_api_key — skipping (set it to enable)")
        return out
    # Deterministic-seeded daily selection so the same day asks the same
    # questions across re-runs (idempotency).
    today = datetime.date.today()
    rnd = random.Random(today.toordinal())
    queries = rnd.sample(_QUERY_BATTERY, min(query_count, len(_QUERY_BATTERY)))

    c = _conn()
    if c is not None:
        _ensure_schema(c)

    for q in queries:
        text, err = _ask_claude(q)
        out["queries_run"] += 1
        if err:
            out["errors"].append({"query": q, "err": err})
            continue
        dchub_hit = bool(_DCHUB_PATTERN.search(text or ""))
        comp_hits = {}
        for name, pat in _COMPETITOR_PATTERNS.items():
            if pat.search(text or ""):
                comp_hits[name] = True
                out["competitor_mentions"][name] = out["competitor_mentions"].get(name, 0) + 1
        if dchub_hit:
            out["dchub_mentions"] += 1
        excerpt = (text or "")[:400]
        out["probes"].append({"query": q, "dchub": dchub_hit,
                               "competitors": list(comp_hits.keys()),
                               "excerpt": excerpt})
        # Persist per-probe
        if c is not None:
            try:
                import json as _j
                with c.cursor() as cur:
                    cur.execute("""
                        INSERT INTO citation_probes
                          (probe_date, query, response_excerpt,
                           dchub_mentioned, competitors_mentioned)
                        VALUES (CURRENT_DATE, %s, %s, %s, %s::jsonb)
                        ON CONFLICT DO NOTHING
                    """, (q, excerpt, dchub_hit, _j.dumps(list(comp_hits.keys()))))
            except Exception: pass

    # Persist day-aggregate
    score_pct = (100.0 * out["dchub_mentions"] / max(1, out["queries_run"]))
    if c is not None:
        try:
            import json as _j
            with c.cursor() as cur:
                cur.execute("""
                    INSERT INTO citation_scores
                      (score_date, queries_run, dchub_mentions,
                       score_pct, competitor_mentions)
                    VALUES (CURRENT_DATE, %s, %s, %s, %s::jsonb)
                    ON CONFLICT (score_date) DO UPDATE
                      SET queries_run = EXCLUDED.queries_run,
                          dchub_mentions = EXCLUDED.dchub_mentions,
                          score_pct = EXCLUDED.score_pct,
                          competitor_mentions = EXCLUDED.competitor_mentions,
                          captured_at = NOW()
                """, (out["queries_run"], out["dchub_mentions"],
                      round(score_pct, 1),
                      _j.dumps(out["competitor_mentions"])))
        finally:
            try: c.close()
            except Exception: pass

    out["score_pct"] = round(score_pct, 1)
    return out


def read_score_history(days: int = 30) -> dict:
    c = _conn()
    if c is None: return {"history": [], "days": days}
    rows = []
    try:
        _ensure_schema(c)
        import psycopg2.extras
        with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT score_date, queries_run, dchub_mentions, score_pct,
                       competitor_mentions
                  FROM citation_scores
                 WHERE score_date >= CURRENT_DATE - INTERVAL '%s days'
                 ORDER BY score_date ASC
            """, (days,))
            for r in cur.fetchall():
                rows.append({
                    "date":            r["score_date"].isoformat() if r["score_date"] else None,
                    "queries_run":     int(r["queries_run"] or 0),
                    "dchub_mentions":  int(r["dchub_mentions"] or 0),
                    "score_pct":       float(r["score_pct"] or 0),
                    "competitor_mentions": r["competitor_mentions"] or {},
                })
    finally:
        try: c.close()
        except Exception: pass
    latest = rows[-1] if rows else None
    return {"history": rows, "days": days, "latest": latest,
            "snapshots_available": len(rows)}


@citation_hunter_bp.route("/api/v1/citations/hunt", methods=["POST"])
def hunt_endpoint():
    provided = (request.headers.get("X-Admin-Key") or "").strip()
    if _ADMIN_KEY and provided != _ADMIN_KEY:
        return jsonify(error="unauthorized"), 401
    try: n = max(1, min(12, int(request.args.get("queries") or 5)))
    except (ValueError, TypeError): n = 5
    return jsonify(hunt_citations(query_count=n)), 200


@citation_hunter_bp.route("/api/v1/citations/score", methods=["GET"])
def score_endpoint():
    try: days = max(1, min(90, int(request.args.get("days") or 30)))
    except (ValueError, TypeError): days = 30
    d = read_score_history(days)
    d["generated_at"] = datetime.datetime.utcnow().isoformat() + "Z"
    resp = jsonify(d)
    resp.headers["Cache-Control"] = "public, max-age=600"
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp, 200


@citation_hunter_bp.route("/api/v1/citations/latest", methods=["GET"])
def latest_probes():
    """Last day's per-query probe results — what Claude actually said."""
    c = _conn()
    if c is None: return jsonify(error="no_database"), 503
    try:
        _ensure_schema(c)
        import psycopg2.extras
        with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT query, dchub_mentioned, response_excerpt,
                       competitors_mentioned, captured_at
                  FROM citation_probes
                 WHERE probe_date = (SELECT MAX(probe_date) FROM citation_probes)
                 ORDER BY captured_at DESC LIMIT 20
            """)
            rows = cur.fetchall()
    finally:
        try: c.close()
        except Exception: pass
    out = [{
        "query":        r["query"],
        "dchub":        bool(r["dchub_mentioned"]),
        "excerpt":      r["response_excerpt"],
        "competitors":  r["competitors_mentioned"] or [],
        "captured_at":  r["captured_at"].isoformat() if r["captured_at"] else None,
    } for r in rows]
    resp = jsonify(probes=out, count=len(out))
    resp.headers["Cache-Control"] = "public, max-age=600"
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp, 200
