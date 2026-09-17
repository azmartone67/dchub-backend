"""Agentic Master Shell (2026-07-18) — seven agent-native capabilities, one
registry, one tick. Repo master-shell pattern (grid-data/depth/monetize):
add a row to CAPABILITIES to extend; every lane is bounded per tick and
fail-soft; kill switches per capability + one master.

Capabilities (v1):
  demand_capture   — query-miss telemetry → weekly-windowed clustering →
                     `unmet_demand:<norm>` brain findings. The dataset
                     expands along REAL agent demand: capture_query_miss()
                     is importable by any search surface (wired: RAG
                     public_search zero-result path).
  research_tasks   — async Analyst-as-a-Service. POST a question (keyed),
                     poll a dossier: RAG evidence across the public corpora
                     + market component rows, LLM synthesis with [n]
                     citations (evidence-only dossier if no LLM key).
  scenario         — simulate_scenario counterfactuals: explicit deltas
                     (power price, time-to-power, queue wait, reserve
                     margin, curtailment) re-scored through a TRANSPARENT
                     v1 composite (formula + weights returned with every
                     response — this is scenario_composite, NOT DCPI).
  answer_sentinel  — golden checks (DB freshness/shape + HTTP self-probes)
                     each tick; failures file ONE bounded
                     `answer_quality_regression` finding via the canonical
                     findings writer.
  deploy_sentinel  — per-deployment golden snapshot; a new deployment that
                     fails MORE checks than the previous one files
                     `deploy_regression`. SHADOW ONLY: auto-rollback is
                     deliberately not implemented yet
                     (DEPLOY_SENTINEL_AUTOROLLBACK reserved).
  permitting       — permitting/moratorium intelligence: curated table +
                     public GET (published rows only) + a news-scan lane
                     that STAGES candidates from real news_articles rows
                     (verified=false, row_status='candidate'; a human
                     promotes via the admin upsert — the scan never
                     fabricates facts).
  standing_intents — keyed agents register standing queries with an HTTPS
                     webhook; the eval lane fires HMAC-signed pushes on
                     change (count-watermark semantics, at-least-once).
                     SSRF-guarded, auto-disabled after 5 straight failures.

Endpoints:
  POST /api/v1/admin/agentic/master-tick        (cron_heartbeat, 2h)
  GET  /api/v1/admin/agentic/status
  GET  /api/v1/admin/agentic/unmet-demand
  POST /api/v1/admin/permitting/upsert
  GET  /api/v1/permitting/intel                 (public, published rows)
  POST /api/v1/agentic/scenario                 (keyless = 3-market preview)
  POST /api/v1/agentic/research                 (keyed, 5/day/key)
  GET  /api/v1/agentic/research/<pid>
  GET/POST /api/v1/agentic/intents  DELETE /api/v1/agentic/intents/<pid>

Kill: AGENTIC_MASTER_DISABLED=1 (everything) or AGENTIC_<CAP>_DISABLED=1.
DB: lazy _ensure (never at boot). Writes to brain_findings go through
routes.brain_findings_writer ONLY (canonical-writer rule).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import threading
import urllib.error
import urllib.parse
import urllib.request

from flask import Blueprint, jsonify, request

from routes.url_registry import build_public_url   # public URLs: single source of truth
from util.json_column import json_for_column

logger = logging.getLogger(__name__)

agentic_bp = Blueprint("agentic_master_shell", __name__)

# Self-probe base — same logic as cron_heartbeat.BASE (localhost on Railway).
_SELF_BASE = (
    f"http://127.0.0.1:{os.environ.get('PORT', '8080')}"
    if (os.environ.get("RAILWAY_ENVIRONMENT") or os.environ.get("RAILWAY_PROJECT_ID"))
    else "https://api.dchub.cloud"
)

_RESEARCH_MODEL_DEFAULT = "claude-haiku-4-5-20251001"
_RESEARCH_DAILY_CAP = 5          # tasks per key per day
_RESEARCH_MAX_ACTIVE = 2         # concurrent synthesis threads
_INTENTS_PER_KEY_CAP = 10
_INTENT_EVAL_BATCH = 25          # intents evaluated per tick
_INTENT_FAILURE_DISABLE = 5      # consecutive webhook failures → disabled
_MISS_RETENTION_DAYS = 30
_PERMITTING_SCAN_CAP = 10        # candidates staged per tick

_research_active = 0
_research_lock = threading.Lock()


def _disabled(cap: str = None) -> bool:
    if str(os.environ.get("AGENTIC_MASTER_DISABLED", "")).lower() in ("1", "true", "yes"):
        return True
    if cap and str(os.environ.get(f"AGENTIC_{cap.upper()}_DISABLED", "")).lower() in ("1", "true", "yes"):
        return True
    return False


# ── auth (mirrors brain_rag) ──────────────────────────────────────────
def _admin_ok() -> bool:
    exp = (os.environ.get("DCHUB_ADMIN_KEY") or os.environ.get("DCHUB_INTERNAL_KEY") or "").strip()
    if not exp:
        return False
    got = (request.headers.get("X-Admin-Key") or request.args.get("admin_key") or "").strip()
    return bool(got) and hmac.compare_digest(got, exp)


def _caller_key() -> str:
    return (request.headers.get("X-API-Key") or request.args.get("api_key") or "").strip()


def _caller_keyed() -> bool:
    """Validated caller (any live key / privileged identity). Fail-closed."""
    try:
        from routes.brain_rag import _search_caller_keyed
        return _search_caller_keyed()
    except Exception:
        return False


# ── DB ────────────────────────────────────────────────────────────────
def _db():
    import psycopg2
    du = (os.environ.get("NEON_DATABASE_URL") or os.environ.get("DATABASE_URL") or "").strip()
    if not du:
        return None
    return psycopg2.connect(du, connect_timeout=8)


def _ensure() -> bool:
    """Lazy DDL for the shell's tables. NEVER at boot."""
    c = _db()
    if c is None:
        return False
    try:
        with c.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS agentic_query_misses (
                    id SERIAL PRIMARY KEY,
                    surface TEXT NOT NULL,
                    query TEXT NOT NULL,
                    norm TEXT NOT NULL,
                    meta JSONB,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""")
            cur.execute("CREATE INDEX IF NOT EXISTS agentic_query_misses_norm_idx "
                        "ON agentic_query_misses (norm, created_at)")
            cur.execute("""
                CREATE TABLE IF NOT EXISTS agentic_research_tasks (
                    id SERIAL PRIMARY KEY,
                    public_id TEXT UNIQUE NOT NULL,
                    question TEXT NOT NULL,
                    requester TEXT,
                    status TEXT NOT NULL DEFAULT 'queued',
                    result_md TEXT,
                    citations JSONB,
                    error TEXT,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    started_at TIMESTAMPTZ,
                    finished_at TIMESTAMPTZ)""")
            cur.execute("""
                CREATE TABLE IF NOT EXISTS agentic_probe_snapshots (
                    id SERIAL PRIMARY KEY,
                    deployment_id TEXT,
                    passed INT NOT NULL DEFAULT 0,
                    failed INT NOT NULL DEFAULT 0,
                    detail JSONB,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""")
            cur.execute("""
                CREATE TABLE IF NOT EXISTS agentic_standing_intents (
                    id SERIAL PRIMARY KEY,
                    public_id TEXT UNIQUE NOT NULL,
                    api_key TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    params JSONB,
                    webhook_url TEXT NOT NULL,
                    secret TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active',
                    watermark JSONB,
                    consecutive_failures INT NOT NULL DEFAULT 0,
                    fires INT NOT NULL DEFAULT 0,
                    last_eval_at TIMESTAMPTZ,
                    last_fired_at TIMESTAMPTZ,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""")
            cur.execute("""
                CREATE TABLE IF NOT EXISTS permitting_intel (
                    id SERIAL PRIMARY KEY,
                    jurisdiction TEXT,
                    state TEXT,
                    country TEXT DEFAULT 'US',
                    market_slug TEXT,
                    class TEXT NOT NULL,
                    title TEXT NOT NULL,
                    detail TEXT,
                    source_url TEXT,
                    effective_date TEXT,
                    verified BOOLEAN NOT NULL DEFAULT FALSE,
                    row_status TEXT NOT NULL DEFAULT 'candidate',
                    provenance TEXT,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""")
            cur.execute("CREATE INDEX IF NOT EXISTS permitting_intel_state_idx "
                        "ON permitting_intel (state, row_status)")
            # Map support (2026-07-18): jurisdiction centroid so the land-power
            # map can plot published rows. Set during curation (admin upsert).
            cur.execute("ALTER TABLE permitting_intel ADD COLUMN IF NOT EXISTS latitude REAL")
            cur.execute("ALTER TABLE permitting_intel ADD COLUMN IF NOT EXISTS longitude REAL")
        c.commit()
        return True
    except Exception:
        try: c.rollback()
        except Exception: pass
        return False
    finally:
        try: c.close()
        except Exception: pass


# ── shared helpers ────────────────────────────────────────────────────
_STOP = {"the", "a", "an", "of", "for", "to", "in", "on", "and", "or", "with",
         "what", "is", "are", "how", "why", "when", "where", "which", "that",
         "this", "by", "from", "as", "at", "into", "about", "your", "our", "it",
         "be", "will", "near", "show", "me", "get", "find", "list", "data"}


def _norm_query(q: str) -> str:
    seen, terms = set(), []
    for t in re.findall(r"[a-z0-9]{3,}", (q or "").lower()):
        if t not in _STOP and t not in seen:
            seen.add(t)
            terms.append(t)
    return " ".join(sorted(terms[:10]))


def _file_finding(issue: str, url: str, detail: str, detector: str,
                  count: int = 1) -> bool:
    """One bounded brain finding through the CANONICAL writer. Never raises."""
    c = _db()
    if c is None:
        return False
    try:
        from routes.brain_findings_writer import upsert_brain_finding
        with c.cursor() as cur:
            upsert_brain_finding(cur, issue=issue[:200], url=url[:500],
                                 count=count, detail=detail[:2000],
                                 detector=detector)
        c.commit()
        return True
    except Exception:
        try: c.rollback()
        except Exception: pass
        return False
    finally:
        try: c.close()
        except Exception: pass


def _llm(system: str, user: str, max_tokens: int = 1200, model: str = None):
    """Anthropic Messages call (urllib, brain_rag pattern). None on any
    failure/missing key — callers must degrade gracefully."""
    key = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
    if not key:
        return None
    body = json.dumps({
        "model": model or (os.environ.get("AGENTIC_RESEARCH_MODEL") or "").strip()
                 or _RESEARCH_MODEL_DEFAULT,
        "max_tokens": int(max_tokens),
        "system": system,
        "messages": [{"role": "user", "content": user}],
    }).encode()
    req = urllib.request.Request("https://api.anthropic.com/v1/messages",
                                 data=body, method="POST")
    req.add_header("x-api-key", key)
    req.add_header("anthropic-version", "2023-06-01")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            d = json.loads(r.read())
        return " ".join(b.get("text", "") for b in (d.get("content") or [])
                        if isinstance(b, dict) and b.get("type") == "text").strip() or None
    except Exception:
        return None


# ═══ 1. demand_capture ════════════════════════════════════════════════
def capture_query_miss(surface: str, query: str, meta: dict = None) -> None:
    """Record a query an agent asked that we could not answer. Importable
    by ANY search surface. Fail-soft, never raises, own connection."""
    try:
        if _disabled("demand_capture"):
            return
        q = (query or "").strip()[:400]
        norm = _norm_query(q)
        if not q or not norm:
            return
        c = _db()
        if c is None:
            return
        try:
            with c.cursor() as cur:
                cur.execute(
                    "INSERT INTO agentic_query_misses (surface, query, norm, meta) "
                    "VALUES (%s, %s, %s, %s::jsonb)",
                    (surface[:80], q, norm[:300],
                     json_for_column(meta or {}, 2000)))
            c.commit()
        finally:
            try: c.close()
            except Exception: pass
    except Exception:
        pass


def _lane_demand_cluster() -> dict:
    """Cluster the last 7d of misses by normalized term-set; the top
    clusters (>=3 hits) become `unmet_demand:` findings the brain's
    existing leaderboard/lanes work. Prunes misses older than 30d."""
    out = {"clusters": 0, "findings": 0, "pruned": 0}
    c = _db()
    if c is None:
        return out
    try:
        with c.cursor() as cur:
            cur.execute("""
                SELECT norm, count(*), max(created_at),
                       (array_agg(query ORDER BY created_at DESC))[1:3]
                  FROM agentic_query_misses
                 WHERE created_at > NOW() - INTERVAL '7 days'
                 GROUP BY norm HAVING count(*) >= 3
                 ORDER BY count(*) DESC LIMIT 5""")
            rows = cur.fetchall()
        out["clusters"] = len(rows)
        for norm, n, _last, samples in rows:
            ok = _file_finding(
                issue=f"unmet_demand:{norm[:160]}",
                url="agentic:query_miss",
                detail=(f"{n} agent queries in 7d found no answer. "
                        f"Samples: {'; '.join((samples or [])[:3])}. "
                        f"Feed the data-gather lane: acquiring this data "
                        f"expands coverage along REAL demand."),
                detector="agentic_demand", count=int(n))
            out["findings"] += 1 if ok else 0
        with c.cursor() as cur:
            cur.execute(
                f"DELETE FROM agentic_query_misses WHERE id IN ("
                f"SELECT id FROM agentic_query_misses "
                f"WHERE created_at < NOW() - INTERVAL '{int(_MISS_RETENTION_DAYS)} days' "
                f"LIMIT 2000)")
            out["pruned"] = cur.rowcount or 0
        c.commit()
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {str(e)[:120]}"
        try: c.rollback()
        except Exception: pass
    finally:
        try: c.close()
        except Exception: pass
    return out


# ═══ 2. research_tasks ════════════════════════════════════════════════
def _research_evidence(question: str) -> list:
    """Evidence bundle: RAG hits over the PUBLIC corpora (hydrated cites)
    + component rows for markets named in the question. Fail-soft []."""
    ev = []
    try:
        from routes.brain_rag import retrieve_context, _hydrate, PUBLIC_CORPORA
        hits = _hydrate(retrieve_context(question, k=8,
                                         corpus=list(PUBLIC_CORPORA)) or [])
        for h in hits:
            ev.append({"kind": h.get("kind"), "source": h.get("source_table"),
                       "id": h.get("source_id"), "text": (h.get("text") or "")[:500],
                       "cite": h.get("cite") or {}})
    except Exception:
        pass
    try:
        terms = [t for t in _norm_query(question).split() if len(t) >= 4][:6]
        if terms:
            c = _db()
            if c is not None:
                try:
                    with c.cursor() as cur:
                        cur.execute("""
                            SELECT market_name, market_slug, iso, state,
                                   time_to_power_months, queue_wait_months,
                                   reserve_margin_pct, curtailment_pct,
                                   avg_kwh_cents, verdict, computed_at::text
                              FROM market_power_scores
                             WHERE published IS NOT FALSE
                               AND lower(market_name) = ANY(%s)
                             LIMIT 4""", (terms,))
                        for r in cur.fetchall():
                            ev.append({"kind": "market_components", "source":
                                       "market_power_scores", "id": r[1],
                                       "text": (f"{r[0]} ({r[2]}, {r[3]}): "
                                                f"time_to_power={r[4]}mo, queue_wait={r[5]}mo, "
                                                f"reserve_margin={r[6]}%, curtailment={r[7]}%, "
                                                f"avg_kwh={r[8]}c, verdict={r[9]}, as_of={r[10]}"),
                                       "cite": {"url": build_public_url("markets", r[1])}})
                finally:
                    try: c.close()
                    except Exception: pass
    except Exception:
        pass
    return ev[:12]


def _run_research(public_id: str) -> None:
    """Execute one research task: evidence → LLM dossier (or evidence-only
    digest when no LLM key). Owns its status transitions. Never raises."""
    global _research_active
    c = _db()
    if c is None:
        return
    try:
        with c.cursor() as cur:
            cur.execute("UPDATE agentic_research_tasks SET status='running', "
                        "started_at=NOW() WHERE public_id=%s AND status='queued' "
                        "RETURNING question", (public_id,))
            row = cur.fetchone()
        c.commit()
        if not row:
            return
        question = row[0]
        ev = _research_evidence(question)
        ev_block = "\n".join(f"[{i+1}] {e['text']}" for i, e in enumerate(ev))
        md = None
        if ev:
            md = _llm(
                system=("You are DC Hub's data-center infrastructure analyst. "
                        "Write a concise, decision-ready dossier in markdown. "
                        "Use ONLY the numbered evidence blocks; cite them as "
                        "[n] inline. If the evidence is insufficient for part "
                        "of the question, say so explicitly — never invent "
                        "numbers. End with a 'Data gaps' line if relevant."),
                user=f"Question: {question}\n\nEvidence:\n{ev_block}")
        if not md:
            md = ("# Evidence digest (no synthesis available)\n\n"
                  f"Question: {question}\n\n" +
                  ("\n".join(f"- [{i+1}] {e['text']}" for i, e in enumerate(ev))
                   if ev else "No matching evidence found in the corpora. "
                              "This gap has been recorded."))
            if not ev:
                capture_query_miss("research_task", question,
                                   {"public_id": public_id})
        with c.cursor() as cur:
            cur.execute("UPDATE agentic_research_tasks SET status='done', "
                        "result_md=%s, citations=%s::jsonb, finished_at=NOW() "
                        "WHERE public_id=%s",
                        (md[:20000], json_for_column(ev, 40000), public_id))
        c.commit()
    except Exception as e:
        try:
            c.rollback()
            with c.cursor() as cur:
                cur.execute("UPDATE agentic_research_tasks SET status='error', "
                            "error=%s, finished_at=NOW() WHERE public_id=%s",
                            (f"{type(e).__name__}: {str(e)[:200]}", public_id))
            c.commit()
        except Exception:
            pass
    finally:
        with _research_lock:
            _research_active = max(0, _research_active - 1)
        try: c.close()
        except Exception: pass


def _spawn_research(public_id: str) -> bool:
    global _research_active
    with _research_lock:
        if _research_active >= _RESEARCH_MAX_ACTIVE:
            return False
        _research_active += 1
    threading.Thread(target=_run_research, args=(public_id,),
                     daemon=True, name=f"agentic-research-{public_id[:8]}").start()
    return True


def _lane_research_drain() -> dict:
    """Tick fallback: run ONE stuck-queued task synchronously (covers the
    spawn-capped and process-recycled cases)."""
    out = {"drained": 0}
    c = _db()
    if c is None:
        return out
    try:
        with c.cursor() as cur:
            cur.execute("SELECT public_id FROM agentic_research_tasks "
                        "WHERE status='queued' ORDER BY created_at ASC LIMIT 1")
            row = cur.fetchone()
        if row:
            global _research_active
            with _research_lock:
                _research_active += 1
            _run_research(row[0])
            out["drained"] = 1
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {str(e)[:120]}"
    finally:
        try: c.close()
        except Exception: pass
    return out


# ═══ 3. scenario ══════════════════════════════════════════════════════
_SCENARIO_WEIGHTS = {"power_readiness": 0.3, "queue_health": 0.2,
                     "headroom": 0.2, "price": 0.2, "excess_power": 0.1}
_SCENARIO_DELTAS = {
    # delta key -> (column, mode: pct multiplies, abs adds)
    "avg_kwh_cents_pct": ("avg_kwh_cents", "pct"),
    "time_to_power_months_delta": ("time_to_power_months", "abs"),
    "queue_wait_months_delta": ("queue_wait_months", "abs"),
    "reserve_margin_pct_delta": ("reserve_margin_pct", "abs"),
    "curtailment_pct_delta": ("curtailment_pct", "abs"),
}


def _clamp(v, lo=0.0, hi=100.0):
    try:
        return max(lo, min(hi, float(v)))
    except Exception:
        return lo


def _scenario_composite(m: dict) -> dict:
    parts = {
        "power_readiness": _clamp(100 - 2.2 * (m.get("time_to_power_months") or 60)),
        "queue_health": _clamp(100 - 1.4 * (m.get("queue_wait_months") or 60)),
        "headroom": _clamp(4.0 * (m.get("reserve_margin_pct") or 0)
                           - 1.5 * (m.get("curtailment_pct") or 0)),
        "price": _clamp(100 - 8.0 * (m.get("avg_kwh_cents") or 12)),
        "excess_power": _clamp(m.get("excess_power_score") or 0),
    }
    score = round(sum(parts[k] * w for k, w in _SCENARIO_WEIGHTS.items()), 1)
    return {"score": score, "components": {k: round(v, 1) for k, v in parts.items()}}


# ═══ 4+5. sentinels (golden checks) ═══════════════════════════════════
def _golden_checks() -> list:
    """Deterministic checks: DB freshness/shape + HTTP self-probes.
    Each: {check, ok, detail}. A broken check reports ok=False, never raises."""
    results = []
    c = _db()
    if c is not None:
        db_checks = [
            ("db_markets_fresh",
             "SELECT count(*) >= 250 AND max(computed_at) > NOW() - INTERVAL '8 days' "
             "FROM market_power_scores"),
            ("db_news_flowing",
             "SELECT count(*) >= 1 FROM news_articles "
             "WHERE published_at::text > (NOW() - INTERVAL '48 hours')::text"),
            ("db_brain_alive",
             "SELECT max(last_seen) > NOW() - INTERVAL '6 hours' FROM brain_findings"),
        ]
        try:
            with c.cursor() as cur:
                for name, sql in db_checks:
                    try:
                        cur.execute(sql)
                        ok = bool((cur.fetchone() or [False])[0])
                        results.append({"check": name, "ok": ok, "detail": ""})
                    except Exception as e:
                        try: c.rollback()
                        except Exception: pass
                        results.append({"check": name, "ok": False,
                                        "detail": f"{type(e).__name__}: {str(e)[:100]}"})
        finally:
            try: c.close()
            except Exception: pass
    else:
        results.append({"check": "db_connect", "ok": False, "detail": "no db"})
    for name, path, probe in (
        ("http_rag_search", "/api/v1/rag/search?q=texas+grid+power",
         lambda d: bool(d.get("ok")) and int(d.get("count") or 0) >= 1),
        ("http_publisher_status", "/api/v1/dchub-media/publisher-status",
         lambda d: "deadman" in d or "loops" in d),
    ):
        try:
            req = urllib.request.Request(_SELF_BASE + path, headers={
                "User-Agent": "DCHub-AgenticSentinel/1.0"})
            with urllib.request.urlopen(req, timeout=15) as r:
                d = json.loads(r.read())
            results.append({"check": name, "ok": bool(probe(d)), "detail": ""})
        except Exception as e:
            results.append({"check": name, "ok": False,
                            "detail": f"{type(e).__name__}: {str(e)[:100]}"})
    return results


def _lane_sentinels() -> dict:
    """answer_sentinel + deploy_sentinel share one golden run per tick.
    Regression files bounded findings; deploy comparison keys on
    RAILWAY_DEPLOYMENT_ID. SHADOW: no rollback is ever attempted."""
    checks = _golden_checks()
    failed = [x for x in checks if not x["ok"]]
    dep = (os.environ.get("RAILWAY_DEPLOYMENT_ID") or "unknown")[:80]
    out = {"passed": len(checks) - len(failed), "failed": len(failed),
           "deployment": dep}
    c = _db()
    if c is None:
        return out
    try:
        prev = None
        with c.cursor() as cur:
            cur.execute("SELECT deployment_id, failed FROM agentic_probe_snapshots "
                        "WHERE deployment_id <> %s "
                        "ORDER BY created_at DESC LIMIT 1", (dep,))
            prev = cur.fetchone()
            cur.execute("INSERT INTO agentic_probe_snapshots "
                        "(deployment_id, passed, failed, detail) "
                        "VALUES (%s, %s, %s, %s::jsonb)",
                        (dep, out["passed"], out["failed"],
                         json_for_column(checks, 8000)))
            # bounded history
            cur.execute("DELETE FROM agentic_probe_snapshots WHERE id IN ("
                        "SELECT id FROM agentic_probe_snapshots "
                        "ORDER BY created_at DESC OFFSET 500)")
        c.commit()
        if failed and not _disabled("answer_sentinel"):
            _file_finding(
                issue="answer_quality_regression",
                url="agentic:golden_checks",
                detail=("Golden checks failing: " +
                        "; ".join(f"{x['check']} ({x['detail'] or 'assert'})"
                                  for x in failed[:5])),
                detector="agentic_answer_sentinel", count=len(failed))
        if (prev and not _disabled("deploy_sentinel")
                and out["failed"] > int(prev[1] or 0)):
            out["deploy_regression"] = True
            _file_finding(
                issue="deploy_regression",
                url=f"deployment:{dep}",
                detail=(f"Deployment {dep} fails {out['failed']} golden checks "
                        f"vs {prev[1]} on previous deployment {prev[0]}. "
                        f"Failing: {'; '.join(x['check'] for x in failed[:5])}. "
                        f"SHADOW: auto-rollback not armed "
                        f"(DEPLOY_SENTINEL_AUTOROLLBACK reserved)."),
                detector="agentic_deploy_sentinel", count=out["failed"])
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {str(e)[:120]}"
        try: c.rollback()
        except Exception: pass
    finally:
        try: c.close()
        except Exception: pass
    return out


# ═══ 6. permitting ════════════════════════════════════════════════════
_PERMIT_CLASSES = ("moratorium", "zoning", "tax", "utility_pause", "other")
_PERMIT_SCAN = (
    ("moratorium", r"moratorium"),
    ("zoning", r"(rezon|zoning (fight|dispute|denial|restrict))"),
    ("utility_pause", r"(paus|halt|freez)\w* .{0,60}(data.?cent|interconnect)"),
)


def _lane_permitting_scan() -> dict:
    """Stage candidate permitting rows from REAL recent news_articles.
    Candidates carry the article title/summary/url + provenance and stay
    row_status='candidate' until a human promotes them (admin upsert)."""
    out = {"staged": 0}
    c = _db()
    if c is None:
        return out
    try:
        staged = 0
        with c.cursor() as cur:
            for cls, pattern in _PERMIT_SCAN:
                if staged >= _PERMITTING_SCAN_CAP:
                    break
                cur.execute("""
                    INSERT INTO permitting_intel
                        (class, title, detail, source_url, provenance)
                    SELECT %s, left(n.title, 300), left(n.summary, 1500),
                           n.url, 'news_scan:' || coalesce(n.source, 'unknown')
                      FROM news_articles n
                     WHERE n.url IS NOT NULL AND coalesce(n.title,'') <> ''
                       AND (coalesce(n.title,'') || ' ' || coalesce(n.summary,'')) ~* %s
                       AND n.published_at::text > (NOW() - INTERVAL '14 days')::text
                       AND NOT EXISTS (SELECT 1 FROM permitting_intel p
                                       WHERE p.source_url = n.url AND p.class = %s)
                     LIMIT %s""",
                    (cls, pattern, cls, _PERMITTING_SCAN_CAP - staged))
                staged += cur.rowcount or 0
        c.commit()
        out["staged"] = staged
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {str(e)[:120]}"
        try: c.rollback()
        except Exception: pass
    finally:
        try: c.close()
        except Exception: pass
    return out


# ═══ 7. standing_intents ══════════════════════════════════════════════
def _webhook_url_ok(url: str) -> bool:
    """HTTPS + public-host only (SSRF guard: no loopback/private/link-local,
    no *.internal/*.local). Fail-closed."""
    try:
        import ipaddress
        import socket
        p = urllib.parse.urlparse(url or "")
        if p.scheme != "https" or not p.hostname:
            return False
        host = p.hostname.lower()
        if (host == "localhost" or host.endswith(".internal")
                or host.endswith(".local")):
            return False
        for info in socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP):
            ip = ipaddress.ip_address(info[4][0])
            if (ip.is_private or ip.is_loopback or ip.is_link_local
                    or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
                return False
        return True
    except Exception:
        return False


def _intent_count(cur, kind: str, params: dict):
    """Current count for an intent's watched set + newest 3 rows.
    Returns (count, rows) or (None, []) on error (skip this eval)."""
    try:
        if kind == "new_deal_in_market":
            m = f"%{(params.get('market') or '').strip()[:80]}%"
            cur.execute("SELECT count(*) FROM deals "
                        "WHERE coalesce(market,'') ILIKE %s OR coalesce(region,'') ILIKE %s",
                        (m, m))
            n = cur.fetchone()[0] or 0
            cur.execute("SELECT id, buyer, seller, value, mw, market FROM deals "
                        "WHERE coalesce(market,'') ILIKE %s OR coalesce(region,'') ILIKE %s "
                        "ORDER BY date DESC NULLS LAST LIMIT 3", (m, m))
            rows = [{"id": r[0], "buyer": r[1], "seller": r[2],
                     "value": (str(r[3]) if r[3] is not None else None),
                     "mw": r[4], "market": r[5]} for r in cur.fetchall()]
            return n, rows
        if kind == "news_keyword":
            q = f"%{(params.get('q') or '').strip()[:120]}%"
            cur.execute("SELECT count(*) FROM news_articles "
                        "WHERE (coalesce(title,'') || ' ' || coalesce(summary,'')) ILIKE %s", (q,))
            n = cur.fetchone()[0] or 0
            cur.execute("SELECT id, title, url, source FROM news_articles "
                        "WHERE (coalesce(title,'') || ' ' || coalesce(summary,'')) ILIKE %s "
                        "ORDER BY published_at DESC NULLS LAST LIMIT 3", (q,))
            rows = [{"id": str(r[0]), "title": r[1], "url": r[2], "source": r[3]}
                    for r in cur.fetchall()]
            return n, rows
        if kind == "permitting_change":
            st = (params.get("state") or "").strip()[:40]
            if st:
                cur.execute("SELECT count(*) FROM permitting_intel "
                            "WHERE row_status='published' AND state ILIKE %s", (st,))
            else:
                cur.execute("SELECT count(*) FROM permitting_intel "
                            "WHERE row_status='published'")
            n = cur.fetchone()[0] or 0
            cur.execute("SELECT id, class, title, jurisdiction, state, source_url "
                        "FROM permitting_intel WHERE row_status='published' "
                        + ("AND state ILIKE %s " if st else "")
                        + "ORDER BY updated_at DESC LIMIT 3",
                        ((st,) if st else ()))
            rows = [{"id": r[0], "class": r[1], "title": r[2],
                     "jurisdiction": r[3], "state": r[4], "source_url": r[5]}
                    for r in cur.fetchall()]
            return n, rows
    except Exception:
        try: cur.connection.rollback()
        except Exception: pass
    return None, []


_INTENT_KINDS = ("new_deal_in_market", "news_keyword", "permitting_change")


def _fire_webhook(url: str, secret: str, payload: dict) -> bool:
    body = json.dumps(payload, default=str).encode()
    sig = hmac.new((secret or "").encode(), body, hashlib.sha256).hexdigest()
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Content-Type": "application/json",
        "User-Agent": "DCHub-StandingIntents/1.0",
        "X-DCHub-Signature": f"sha256={sig}",
    })
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            return 200 <= r.status < 300
    except Exception:
        return False


def _lane_intents_eval() -> dict:
    """Evaluate a bounded batch of active intents (oldest-eval first).
    Count-watermark semantics: first eval initializes the watermark
    silently; growth fires the webhook (at-least-once — the watermark only
    advances on delivery success)."""
    out = {"evaluated": 0, "fired": 0, "disabled": 0}
    c = _db()
    if c is None:
        return out
    try:
        with c.cursor() as cur:
            cur.execute("SELECT id, public_id, kind, params, webhook_url, secret, "
                        "watermark, consecutive_failures "
                        "FROM agentic_standing_intents WHERE status='active' "
                        "ORDER BY last_eval_at ASC NULLS FIRST LIMIT %s",
                        (_INTENT_EVAL_BATCH,))
            intents = cur.fetchall()
        for iid, pid, kind, params, whurl, secret, wm, fails in intents:
            params = params or {}
            wm = wm or {}
            with c.cursor() as cur:
                n, rows = _intent_count(cur, kind, params)
                if n is None:
                    continue
                out["evaluated"] += 1
                prev = wm.get("count")
                fired_ok = None
                if prev is not None and n > int(prev):
                    fired_ok = _fire_webhook(whurl, secret, {
                        "intent_id": pid, "kind": kind, "params": params,
                        "new_matches": n - int(prev), "total_matches": n,
                        "latest": rows,
                        "_cite": "Data: DC Hub (dchub.cloud), CC-BY-4.0",
                    })
                if fired_ok is False:
                    fails = int(fails or 0) + 1
                    status = ("disabled_webhook_failing"
                              if fails >= _INTENT_FAILURE_DISABLE else "active")
                    cur.execute("UPDATE agentic_standing_intents SET "
                                "consecutive_failures=%s, status=%s, "
                                "last_eval_at=NOW() WHERE id=%s",
                                (fails, status, iid))
                    if status != "active":
                        out["disabled"] += 1
                else:
                    if fired_ok:
                        out["fired"] += 1
                    cur.execute("UPDATE agentic_standing_intents SET "
                                "watermark=%s::jsonb, consecutive_failures=0, "
                                "last_eval_at=NOW(), "
                                "fires=fires+%s, last_fired_at=CASE WHEN %s "
                                "THEN NOW() ELSE last_fired_at END WHERE id=%s",
                                (json.dumps({"count": n}), 1 if fired_ok else 0,
                                 bool(fired_ok), iid))
            c.commit()
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {str(e)[:120]}"
        try: c.rollback()
        except Exception: pass
    finally:
        try: c.close()
        except Exception: pass
    return out


# ═══ capability registry ══════════════════════════════════════════════
CAPABILITIES = {
    "demand_capture": {"lane": _lane_demand_cluster,
                       "desc": "query-miss telemetry -> unmet_demand findings"},
    "research_tasks": {"lane": _lane_research_drain,
                       "desc": "async cited dossiers (keyed, 5/day)"},
    "scenario": {"lane": None,
                 "desc": "counterfactual re-scoring (transparent composite)"},
    # One golden run per tick serves BOTH sentinels: the lane rides the
    # answer_sentinel key; the deploy comparison checks its own kill inside.
    "answer_sentinel": {"lane": _lane_sentinels,
                        "desc": "golden checks -> answer_quality_regression"},
    "deploy_sentinel": {"lane": None,
                        "desc": "per-deploy golden snapshot (SHADOW, no rollback)"},
    "permitting": {"lane": _lane_permitting_scan,
                   "desc": "moratorium/zoning intel + news-scan candidates"},
    "standing_intents": {"lane": _lane_intents_eval,
                         "desc": "HMAC-signed webhook pushes on change"},
}


# ── endpoints ─────────────────────────────────────────────────────────
@agentic_bp.route("/api/v1/admin/agentic/master-tick", methods=["POST"])
def master_tick():
    if not _admin_ok():
        return jsonify(error="unauthorized"), 401
    if _disabled():
        return jsonify(skipped="AGENTIC_MASTER_DISABLED"), 200
    if not _ensure():
        return jsonify(ok=False, error="ensure_failed"), 200
    ran = {}
    for cap, spec in CAPABILITIES.items():
        fn = spec.get("lane")
        if fn is None or _disabled(cap):
            continue
        try:
            ran[cap] = fn()
        except Exception as e:  # noqa: BLE001 — lane isolation
            ran[cap] = {"error": f"{type(e).__name__}: {str(e)[:120]}"}
    return jsonify(ok=True, ran=ran), 200


@agentic_bp.route("/api/v1/admin/agentic/status", methods=["GET"])
def status():
    if not _admin_ok():
        return jsonify(error="unauthorized"), 401
    out = {"ok": True, "disabled": _disabled(),
           "capabilities": {k: v["desc"] for k, v in CAPABILITIES.items()}}
    c = _db()
    if c is None:
        return jsonify(ok=False, error="db_unavailable"), 200
    try:
        with c.cursor() as cur:
            for label, sql in (
                ("misses_7d", "SELECT count(*) FROM agentic_query_misses "
                              "WHERE created_at > NOW() - INTERVAL '7 days'"),
                ("research_by_status", "SELECT status, count(*) FROM "
                                       "agentic_research_tasks GROUP BY 1"),
                ("intents_active", "SELECT count(*) FROM agentic_standing_intents "
                                   "WHERE status='active'"),
                ("permitting", "SELECT row_status, count(*) FROM permitting_intel "
                               "GROUP BY 1"),
                ("last_golden", "SELECT passed, failed, created_at::text FROM "
                                "agentic_probe_snapshots ORDER BY created_at "
                                "DESC LIMIT 1"),
            ):
                try:
                    cur.execute(sql)
                    rows = cur.fetchall()
                    if label in ("research_by_status", "permitting"):
                        out[label] = {str(a): int(b) for a, b in rows}
                    elif label == "last_golden":
                        out[label] = (dict(zip(("passed", "failed", "at"),
                                               rows[0])) if rows else None)
                    else:
                        out[label] = int(rows[0][0]) if rows else 0
                except Exception:
                    try: c.rollback()
                    except Exception: pass
                    out[label] = None
    finally:
        try: c.close()
        except Exception: pass
    return jsonify(out), 200


@agentic_bp.route("/api/v1/admin/agentic/unmet-demand", methods=["GET"])
def unmet_demand():
    if not _admin_ok():
        return jsonify(error="unauthorized"), 401
    c = _db()
    if c is None:
        return jsonify(ok=False, error="db_unavailable"), 200
    try:
        with c.cursor() as cur:
            cur.execute("""
                SELECT norm, count(*), max(created_at)::text,
                       (array_agg(DISTINCT surface))[1:4],
                       (array_agg(query ORDER BY created_at DESC))[1:5]
                  FROM agentic_query_misses
                 WHERE created_at > NOW() - INTERVAL '30 days'
                 GROUP BY norm ORDER BY count(*) DESC LIMIT 40""")
            top = [{"norm": r[0], "count": int(r[1]), "last": r[2],
                    "surfaces": r[3], "samples": r[4]} for r in cur.fetchall()]
        return jsonify(ok=True, top_unmet=top), 200
    except Exception as e:
        return jsonify(ok=False, error=f"{type(e).__name__}: {str(e)[:120]}"), 200
    finally:
        try: c.close()
        except Exception: pass


@agentic_bp.route("/api/v1/agentic/scenario", methods=["POST"])
def scenario():
    if _disabled("scenario"):
        return jsonify(error="disabled"), 404
    body = request.get_json(silent=True) or {}
    deltas = {k: body.get(k) for k in _SCENARIO_DELTAS if body.get(k) is not None}
    try:
        deltas = {k: float(v) for k, v in deltas.items()}
    except Exception:
        return jsonify(error="deltas must be numeric",
                       accepted=list(_SCENARIO_DELTAS)), 400
    market = (body.get("market") or "").strip().lower()[:80] or None
    try:
        top_n = max(1, min(25, int(body.get("top_n", 10))))
    except Exception:
        top_n = 10
    keyed = _caller_keyed()
    if not keyed:
        top_n = min(top_n, 3)
    c = _db()
    if c is None:
        return jsonify(ok=False, error="db_unavailable"), 200
    try:
        with c.cursor() as cur:
            cur.execute(
                "SELECT market_slug, market_name, iso, state, "
                "time_to_power_months, queue_wait_months, reserve_margin_pct, "
                "curtailment_pct, avg_kwh_cents, excess_power_score "
                "FROM market_power_scores WHERE published IS NOT FALSE "
                + ("AND market_slug = %s" if market else "")
                + " ORDER BY market_slug",
                ((market,) if market else ()))
            rows = cur.fetchall()
    except Exception as e:
        return jsonify(ok=False, error=f"{type(e).__name__}: {str(e)[:120]}"), 200
    finally:
        try: c.close()
        except Exception: pass
    if not rows:
        capture_query_miss("scenario", market or "all",
                           {"deltas": deltas, "reason": "no market rows"})
        return jsonify(ok=False, error="market_not_found", market=market), 404
    cols = ("time_to_power_months", "queue_wait_months", "reserve_margin_pct",
            "curtailment_pct", "avg_kwh_cents", "excess_power_score")
    results = []
    for r in rows:
        base = dict(zip(cols, [float(x) if x is not None else None for x in r[4:]]))
        scen = dict(base)
        for dk, dv in deltas.items():
            col, mode = _SCENARIO_DELTAS[dk]
            if scen.get(col) is None:
                continue
            scen[col] = scen[col] * (1.0 + dv / 100.0) if mode == "pct" else scen[col] + dv
        b, s = _scenario_composite(base), _scenario_composite(scen)
        results.append({"market": r[0], "name": r[1], "iso": r[2], "state": r[3],
                        "baseline": b, "scenario": s,
                        "delta": round(s["score"] - b["score"], 1)})
    results.sort(key=lambda x: abs(x["delta"]), reverse=True)
    out = {"ok": True, "deltas_applied": deltas, "markets_scored": len(results),
           "results": results[:top_n],
           "method": {"note": ("scenario_composite v1 — TRANSPARENT re-scoring "
                               "of market_power_scores components under your "
                               "deltas. NOT the DCPI."),
                      "weights": _SCENARIO_WEIGHTS,
                      "components": ("power_readiness=100-2.2*time_to_power_mo; "
                                     "queue_health=100-1.4*queue_wait_mo; "
                                     "headroom=4*reserve_margin-1.5*curtailment; "
                                     "price=100-8*avg_kwh_cents; "
                                     "excess_power=excess_power_score")},
           "_cite": "Data: DC Hub (dchub.cloud), CC-BY-4.0"}
    if not keyed:
        out["k_capped"] = 3
        out["_unlock"] = {"message": "Keyless callers see the top 3 markets. "
                                     "Any free key unlocks up to 25.",
                          "claim_url": "https://dchub.cloud/api/v1/keys/claim"}
    return jsonify(out), 200


@agentic_bp.route("/api/v1/agentic/research", methods=["POST"])
def research_submit():
    if _disabled("research_tasks"):
        return jsonify(error="disabled"), 404
    if not _caller_keyed():
        return jsonify(error="api_key_required",
                       how="X-API-Key header with any live key",
                       claim_url="https://dchub.cloud/api/v1/keys/claim"), 401
    body = request.get_json(silent=True) or {}
    q = (body.get("question") or "").strip()
    if len(q) < 12:
        return jsonify(error="question required (min 12 chars)"), 400
    if not _ensure():
        return jsonify(ok=False, error="ensure_failed"), 200
    requester = _caller_key()[:20]
    c = _db()
    if c is None:
        return jsonify(ok=False, error="db_unavailable"), 200
    try:
        with c.cursor() as cur:
            cur.execute("SELECT count(*) FROM agentic_research_tasks "
                        "WHERE requester=%s AND created_at > NOW() - INTERVAL '1 day'",
                        (requester,))
            if (cur.fetchone()[0] or 0) >= _RESEARCH_DAILY_CAP:
                return jsonify(error="daily_cap",
                               cap=_RESEARCH_DAILY_CAP,
                               note="resets 24h rolling"), 429
            pid = secrets.token_hex(12)
            cur.execute("INSERT INTO agentic_research_tasks "
                        "(public_id, question, requester) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                        (pid, q[:2000], requester))
        c.commit()
    except Exception as e:
        return jsonify(ok=False, error=f"{type(e).__name__}: {str(e)[:120]}"), 200
    finally:
        try: c.close()
        except Exception: pass
    spawned = _spawn_research(pid)
    return jsonify(ok=True, task_id=pid, status="running" if spawned else "queued",
                   poll=f"/api/v1/agentic/research/{pid}",
                   note=("Dossier synthesis over DC Hub's corpora; typical "
                         "completion under a minute." if spawned else
                         "Queued — the shell tick drains the queue.")), 202


@agentic_bp.route("/api/v1/agentic/research/<pid>", methods=["GET"])
def research_poll(pid):
    if not _caller_keyed() and not _admin_ok():
        return jsonify(error="api_key_required"), 401
    c = _db()
    if c is None:
        return jsonify(ok=False, error="db_unavailable"), 200
    try:
        with c.cursor() as cur:
            cur.execute("SELECT status, result_md, citations, error, "
                        "created_at::text, finished_at::text "
                        "FROM agentic_research_tasks WHERE public_id=%s",
                        (pid[:40],))
            row = cur.fetchone()
        if not row:
            return jsonify(error="not_found"), 404
        out = {"ok": True, "task_id": pid, "status": row[0],
               "created_at": row[4], "finished_at": row[5],
               "_cite": "Data: DC Hub (dchub.cloud), CC-BY-4.0"}
        if row[0] == "done":
            out["result_md"] = row[1]
            out["citations"] = row[2]
        elif row[0] == "error":
            out["error"] = row[3]
        return jsonify(out), 200
    finally:
        try: c.close()
        except Exception: pass


@agentic_bp.route("/api/v1/agentic/intents", methods=["GET", "POST"])
def intents():
    if _disabled("standing_intents"):
        return jsonify(error="disabled"), 404
    key = _caller_key()
    if not key or not _caller_keyed():
        return jsonify(error="api_key_required",
                       claim_url="https://dchub.cloud/api/v1/keys/claim"), 401
    if not _ensure():
        return jsonify(ok=False, error="ensure_failed"), 200
    c = _db()
    if c is None:
        return jsonify(ok=False, error="db_unavailable"), 200
    try:
        if request.method == "GET":
            with c.cursor() as cur:
                cur.execute("SELECT public_id, kind, params, webhook_url, status, "
                            "fires, last_fired_at::text, created_at::text "
                            "FROM agentic_standing_intents WHERE api_key=%s "
                            "ORDER BY created_at DESC", (key,))
                rows = [{"intent_id": r[0], "kind": r[1], "params": r[2],
                         "webhook_url": r[3], "status": r[4], "fires": r[5],
                         "last_fired_at": r[6], "created_at": r[7]}
                        for r in cur.fetchall()]
            return jsonify(ok=True, intents=rows, kinds=list(_INTENT_KINDS)), 200
        body = request.get_json(silent=True) or {}
        kind = (body.get("kind") or "").strip()
        whurl = (body.get("webhook_url") or "").strip()[:500]
        params = body.get("params") or {}
        if kind not in _INTENT_KINDS:
            return jsonify(error="kind must be one of " + ", ".join(_INTENT_KINDS)), 400
        if not isinstance(params, dict):
            return jsonify(error="params must be an object"), 400
        if not _webhook_url_ok(whurl):
            return jsonify(error="webhook_url must be public HTTPS "
                                 "(no private/internal hosts)"), 400
        with c.cursor() as cur:
            cur.execute("SELECT count(*) FROM agentic_standing_intents "
                        "WHERE api_key=%s AND status='active'", (key,))
            if (cur.fetchone()[0] or 0) >= _INTENTS_PER_KEY_CAP:
                return jsonify(error="intent_cap", cap=_INTENTS_PER_KEY_CAP), 429
            pid = secrets.token_hex(10)
            secret = secrets.token_hex(16)
            cur.execute("INSERT INTO agentic_standing_intents "
                        "(public_id, api_key, kind, params, webhook_url, secret) "
                        "VALUES (%s, %s, %s, %s::jsonb, %s, %s)",
                        (pid, key, kind,
                         json_for_column(params, 2000), whurl, secret))
        c.commit()
        return jsonify(ok=True, intent_id=pid, secret=secret,
                       note=("SAVE the secret — webhook payloads carry "
                             "X-DCHub-Signature: sha256=HMAC(secret, body). "
                             "First evaluation initializes the watermark "
                             "silently; growth in matches fires the webhook. "
                             f"Auto-disabled after {_INTENT_FAILURE_DISABLE} "
                             "consecutive delivery failures.")), 201
    except Exception as e:
        return jsonify(ok=False, error=f"{type(e).__name__}: {str(e)[:120]}"), 200
    finally:
        try: c.close()
        except Exception: pass


@agentic_bp.route("/api/v1/agentic/intents/<pid>", methods=["DELETE"])
def intents_delete(pid):
    key = _caller_key()
    if not key:
        return jsonify(error="api_key_required"), 401
    c = _db()
    if c is None:
        return jsonify(ok=False, error="db_unavailable"), 200
    try:
        with c.cursor() as cur:
            cur.execute("DELETE FROM agentic_standing_intents "
                        "WHERE public_id=%s AND api_key=%s", (pid[:40], key))
            n = cur.rowcount
        c.commit()
        return (jsonify(ok=True, deleted=pid), 200) if n else \
               (jsonify(error="not_found"), 404)
    finally:
        try: c.close()
        except Exception: pass


@agentic_bp.route("/api/v1/permitting/intel", methods=["GET"])
def permitting_intel():
    c = _db()
    if c is None:
        return jsonify(ok=False, error="db_unavailable"), 200
    state = (request.args.get("state") or "").strip()[:40]
    cls = (request.args.get("class") or "").strip()[:30]
    include_candidates = (request.args.get("include_candidates") == "1"
                          and _admin_ok())
    try:
        cond = ["row_status = 'published'"] if not include_candidates else ["1=1"]
        args = []
        if state:
            cond.append("state ILIKE %s"); args.append(state)
        if cls:
            cond.append("class = %s"); args.append(cls)
        with c.cursor() as cur:
            cur.execute(
                "SELECT id, jurisdiction, state, country, market_slug, class, "
                "title, detail, source_url, effective_date, verified, "
                "row_status, updated_at::text, latitude, longitude "
                "FROM permitting_intel WHERE "
                + " AND ".join(cond) + " ORDER BY updated_at DESC LIMIT 100",
                tuple(args))
            rows = [dict(zip(("id", "jurisdiction", "state", "country",
                              "market_slug", "class", "title", "detail",
                              "source_url", "effective_date", "verified",
                              "row_status", "updated_at", "latitude",
                              "longitude"), r))
                    for r in cur.fetchall()]
        return jsonify(ok=True, count=len(rows), records=rows,
                       classes=list(_PERMIT_CLASSES),
                       note=("Curated permitting/moratorium intelligence. "
                             "Published rows are human-verified; the news-scan "
                             "lane stages candidates from real articles."),
                       _cite="Data: DC Hub (dchub.cloud), CC-BY-4.0"), 200
    except Exception as e:
        return jsonify(ok=False, error=f"{type(e).__name__}: {str(e)[:120]}"), 200
    finally:
        try: c.close()
        except Exception: pass


@agentic_bp.route("/api/v1/admin/permitting/upsert", methods=["POST"])
def permitting_upsert():
    """Curation: promote a candidate (id + row_status='published') or insert
    a verified row directly. Admin-gated."""
    if not _admin_ok():
        return jsonify(error="unauthorized"), 401
    if not _ensure():
        return jsonify(ok=False, error="ensure_failed"), 200
    body = request.get_json(silent=True) or {}
    rid = body.get("id")
    fields = {k: (str(body[k]).strip()[:500] if body[k] is not None else None)
              for k in ("jurisdiction", "state", "country", "market_slug",
                        "class", "title", "detail", "source_url",
                        "effective_date", "row_status") if k in body}
    if fields.get("class") and fields["class"] not in _PERMIT_CLASSES:
        return jsonify(error="class must be one of " + ", ".join(_PERMIT_CLASSES)), 400
    if fields.get("row_status") and fields["row_status"] not in (
            "candidate", "published", "rejected"):
        return jsonify(error="row_status must be candidate|published|rejected"), 400
    if "verified" in body:
        fields["verified"] = bool(body["verified"])
    for coord in ("latitude", "longitude"):
        if coord in body:
            try:
                fields[coord] = float(body[coord]) if body[coord] is not None else None
            except Exception:
                return jsonify(error=f"{coord} must be numeric"), 400
    c = _db()
    if c is None:
        return jsonify(ok=False, error="db_unavailable"), 200
    try:
        with c.cursor() as cur:
            if rid:
                if not fields:
                    return jsonify(error="no fields to update"), 400
                sets = ", ".join(f"{k} = %s" for k in fields) + ", updated_at = NOW()"
                cur.execute(f"UPDATE permitting_intel SET {sets} WHERE id = %s "
                            "RETURNING id", (*fields.values(), int(rid)))
                row = cur.fetchone()
                c.commit()
                return (jsonify(ok=True, id=row[0], action="updated"), 200) if row \
                    else (jsonify(error="not_found"), 404)
            if not fields.get("title") or not fields.get("class"):
                return jsonify(error="title and class required for insert"), 400
            fields.setdefault("row_status", "published")
            fields.setdefault("verified", True)
            fields["provenance"] = "admin_curated"
            cols = ", ".join(fields)
            ph = ", ".join(["%s"] * len(fields))
            cur.execute(f"INSERT INTO permitting_intel ({cols}) VALUES ({ph}) ON CONFLICT DO NOTHING "
                        "RETURNING id", tuple(fields.values()))
            new_id = cur.fetchone()[0]
        c.commit()
        return jsonify(ok=True, id=new_id, action="inserted"), 201
    except Exception as e:
        try: c.rollback()
        except Exception: pass
        return jsonify(ok=False, error=f"{type(e).__name__}: {str(e)[:120]}"), 200
    finally:
        try: c.close()
        except Exception: pass
