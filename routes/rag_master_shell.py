"""
rag_master_shell.py — the self-driving RAG orchestrator (2026-07-04).

Sibling to reliability_master_shell / grid_data_master_shell, pointed at the
RAG v1 stack (semantic search + market/ISO context packs + the market_narratives
corpus + deep-dive generation). Every tick it MEASURES the stack's health, SCORES
five levers, finds the weakest ACTIONABLE one, and — only when armed — pulls
exactly one bounded action. It is the single place that answers "is RAG healthy,
growing, and being used, and if not, what's the one thing to fix".

The five levers:
  1. FRESHNESS  — embedding coverage of the flat corpora + the narrative re-chunk
                  backlog + index recency. Weak => nudge a reindex.
  2. COVERAGE   — deep-dive completeness over the PUBLISHED DCPI market universe
                  (the same set the deep-dive cron fills). Weak => nudge the
                  deep-dive rotation so the market_narratives corpus keeps growing.
  3. RETRIEVAL  — a fixed eval query set run in-process through retrieve_context;
                  reads the bi-encoder COSINE (never the rerank 'score' — that
                  silently disarms the gate, r-rag-cosine-passthrough), plus the
                  HNSW index presence (the 07-03 seq-scan regression) and drift vs
                  the prior snapshot. Weak => file a finding (never auto-DDL).
  4. REACH      — the NORTH STAR: distinct external AI agents/week hitting the RAG
                  pack tools (get_market_context + get_iso_context). Pure
                  scoreboard — reported + trended, never actioned (adoption is a
                  GEO/distribution problem, not a shell action). semantic_search is
                  internal-only and is EXCLUDED from the agent count.
  5. GAP        — structural gap filing: a static list of code-shaped RAG gaps +
                  dynamic findings (HNSW missing, eval regression, a corpus with a
                  declared-but-inactive fresh_col, low coverage) upserted to
                  brain_findings so the autonomy loop drafts real fixes.

Design mirrors the house master-shell pattern exactly: admin-gated, killable
(RAG_MASTER_DISABLED=1), loopback self-calls, a snapshot table, tier1..3, 0-100
score. SHADOW BY DEFAULT — it measures + scores + persists every tick but takes
NO action unless RAG_MASTER_ARM=1 (opt-in, not opt-out: reindex + deep-dive are
ALREADY driven by their own crons, so arming the shell's supplemental nudge is a
deliberate choice, never the default). Every action is bounded (one/tick),
individually killable (RAG_LEVER_<NAME>_OFF=1), and fail-soft.

Endpoints:
  POST /api/v1/admin/rag/master-tick   — measure -> score -> act -> persist
  GET  /api/v1/admin/rag/master-state  — latest snapshot + trend
"""
import os
import re
import json
import time
import hmac
import urllib.request
import urllib.error
from datetime import datetime, timezone

from flask import Blueprint, jsonify, request
from routes._swallowed_writes import note_swallowed_write

rag_master_shell_bp = Blueprint("rag_master_shell", __name__)

# Self-calls go LOOPBACK on Railway (mirrors reliability/grid shells) — the shell
# runs INSIDE the backend, so hitting the public URL would round-trip out through
# Cloudflare and back. Loopback keeps each call sub-100ms and the tick well under
# CF's ~15s proxy limit.
_BACKEND_BASE = (
    f"http://127.0.0.1:{os.environ.get('PORT', '8080')}"
    if (os.environ.get("RAILWAY_ENVIRONMENT") or os.environ.get("RAILWAY_PROJECT_ID"))
    else os.environ.get("DCHUB_BACKEND_BASE", "https://dchub-backend-production.up.railway.app")
)

# RAG pack tools whose calls count as external REACH (the north star). Note:
# semantic_search is INTERNAL-only (brain loops call it) and is deliberately
# EXCLUDED — counting it would inflate reach with our own traffic.
_REACH_TOOLS = ("get_market_context", "get_iso_context")
_REACH_TARGET = 20          # ~20 real external agents/wk baseline → reach score 1.0

# Health-lever floors (below => a gap finding when armed).
_COVERAGE_FLOOR = 0.50      # deep-dive completeness over published markets
_FRESHNESS_FLOOR = 0.60     # flat-corpus embedding coverage
_EVAL_MEAN_FLOOR = 0.70     # mean top-1 cosine across the eval set.
                            # brain-ascension #28 wave 2 (2026-07-25): raised
                            # 0.45→0.70 for the mistral scale — measured live:
                            # nonsense tops out ~0.675, weakest on-topic 0.744,
                            # so 0.45 was trivially passable. Anchors (below)
                            # are the real relevance check; the floor now only
                            # separates on-topic from nonsense.
_EVAL_REGRESS_DROP = 0.05   # a drop this large vs the prior snapshot trips a finding

# Fixed retrieval eval — each query has a VERIFIED good hit (live 2026-07-03/04);
# the lever reads the bi-encoder COSINE (always present) not the rerank 'score'.
# brain-ascension #28 (2026-07-25): `anchors` is the GROUND TRUTH the cosine
# floors never gave — the top-1 doc's text must contain >=1 anchor term or the
# query is a MISS regardless of cosine. The floors were tuned on Cohere's
# asymmetric scale; mistral-embed scores ~0.75+ even on nonsense, so a
# floor-pass proves nothing about relevance. Term-presence is provider-
# independent (same trick public_search uses for its miss capture).
_EVAL_QUERIES = [
    {"q": "why is northern virginia constrained for data centers",
     "corpus": "market_narratives", "floor": 0.70,
     "anchors": ["virginia", "loudoun", "ashburn", "dominion"]},
    {"q": "grids opening up for AI load in the Southeast",
     "corpus": "news_articles", "floor": 0.70,
     "anchors": ["southeast", "georgia", "tennessee", "carolina", "tva",
                 "southern company", "duke"]},
    {"q": "nuclear power deals for hyperscalers",
     "corpus": ["deals", "news_articles"], "floor": 0.70,
     "anchors": ["nuclear", "smr", "reactor"]},
    {"q": "behind-the-meter gas for AI data centers",
     "corpus": "news_articles", "floor": 0.70,
     "anchors": ["gas", "behind-the-meter", "btm", "turbine"]},
    {"q": "hyperscale AI data center campus in Northern Virginia",
     "corpus": "discovered_facilities", "floor": 0.70,
     "anchors": ["virginia", "ashburn", "loudoun", "manassas", "sterling",
                 "leesburg"]},
    {"q": "PJM capacity auction clearing price and AI demand",
     "corpus": "news_articles", "floor": 0.70,
     "anchors": ["pjm", "capacity auction", "clearing price"]},
]

# Static code-shaped RAG gaps upserted every ARMED tick so the autonomy loop can
# draft real adapters/fixes. (issue, detail). Kept small + stable — dynamic
# findings (below-floor coverage, missing HNSW, eval regression, inactive
# fresh_col) are added at tick time.
_GAP_FINDINGS = [
    ("rag_iso_pack_narrative_lag",
     "ISO context packs retrieve deep-dive excerpts, but narrative coverage of a "
     "grid's markets trails the market universe — many ISOs surface <2 excerpts. "
     "Widen market_narratives coverage (deep-dive rotation) so every ISO pack has "
     "grounded prose, not just news."),
    ("rag_public_corpus_semantic_only",
     "semantic_search + context packs expose news/deals/facilities/narratives, but "
     "the DCPI numeric scores + grid telemetry stay SQL-only (by design). Consider a "
     "read-only numeric-context adapter so an agent can ask a numeric 'why' in one call."),
]

# Levers eligible to be the WEAKEST-and-actioned. REACH is scored + trended but
# is a pure north-star scoreboard (its 'action' is a cross-team flag, never a fire).
_ACTIONABLE = ("freshness", "coverage", "retrieval", "gap")
_ALL_LEVERS = ("freshness", "coverage", "retrieval", "reach", "gap")


# ── auth (mirrors reliability_master_shell) ───────────────────────────
def _admin_key() -> str | None:
    return os.environ.get("DCHUB_ADMIN_KEY") or os.environ.get("DCHUB_INTERNAL_KEY")


def _admin_ok() -> bool:
    expected = (_admin_key() or "").strip()
    if not expected:
        return False
    got = (request.headers.get("X-Admin-Key")
           or request.args.get("admin_key") or "").strip()
    return bool(got) and hmac.compare_digest(got, expected)


def _disabled() -> bool:
    return str(os.environ.get("RAG_MASTER_DISABLED", "")).lower() in ("1", "true", "yes")


def _act_enabled() -> bool:
    """SHADOW by default. reindex + deep-dive rotation ALREADY run on their own
    crons; the shell's supplemental nudge is an explicit opt-in (RAG_MASTER_ARM=1)
    so we never double-fire by accident. Findings-writing is also gated on this."""
    return str(os.environ.get("RAG_MASTER_ARM", "")).lower() in ("1", "true", "yes")


def _lever_off(name: str) -> bool:
    """Per-lever kill switch, e.g. RAG_LEVER_COVERAGE_OFF=1."""
    return str(os.environ.get(f"RAG_LEVER_{name.upper()}_OFF", "")).lower() in ("1", "true", "yes")


# ── self-call helpers (mirror reliability_master_shell) ───────────────
def _req(path: str, method: str = "GET", timeout: int = 10) -> dict:
    url = (path if path.startswith("http") else _BACKEND_BASE.rstrip("/") + path)
    started = time.time()
    try:
        req = urllib.request.Request(url, data=(b"" if method == "POST" else None), method=method)
        req.add_header("X-DC-Probe", "rag-tick")  # rate-limiter bypass
        req.add_header("User-Agent", "dchub-rag-orchestrator/1.0")
        ak = _admin_key()
        if ak:
            req.add_header("X-Admin-Key", ak)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", "replace")
            try:
                parsed = json.loads(body)
            except Exception:
                parsed = {"_raw": body[:300]}
            return {"ok": True, "http": resp.status,
                    "ms": int((time.time() - started) * 1000), "data": parsed}
    except urllib.error.HTTPError as e:
        return {"ok": False, "http": e.code, "error": f"HTTP {e.code}"}
    except Exception as e:
        return {"ok": False, "http": None, "error": f"{type(e).__name__}: {str(e)[:160]}"}


def _fire(path: str, timeout: int = 4) -> dict:
    """Trigger a downstream ACTION without blocking on full completion — a short
    read timeout is treated as 'dispatched' so the tick stays under CF's limit."""
    url = (path if path.startswith("http") else _BACKEND_BASE.rstrip("/") + path)
    try:
        req = urllib.request.Request(url, data=b"", method="POST")
        req.add_header("X-DC-Probe", "rag-tick")
        req.add_header("User-Agent", "dchub-rag-orchestrator/1.0")
        ak = _admin_key()
        if ak:
            req.add_header("X-Admin-Key", ak)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            resp.read(64)
            return {"dispatched": True, "http": resp.status}
    except (urllib.error.URLError, TimeoutError) as e:
        if isinstance(e, urllib.error.HTTPError):
            return {"dispatched": False, "http": e.code, "error": f"HTTP {e.code}"}
        return {"dispatched": True, "note": f"async ({type(e).__name__})"}
    except Exception as e:
        return {"dispatched": False, "error": f"{type(e).__name__}: {str(e)[:160]}"}


# ── db ────────────────────────────────────────────────────────────────
def _conn():
    """Autocommit read/snapshot connection (ai_reach._conn is autocommit=True).
    NEVER used for brain_findings writes — those need savepoints (autocommit=False),
    handled by _file_gap_findings() opening its own conn."""
    try:
        from routes.ai_reach import _conn as _raw
        return _raw()
    except Exception:
        return None


def _fetchone(cur, sql, default=None):
    try:
        cur.execute(sql)
        row = cur.fetchone()
        return row if row is not None else default
    except Exception:
        return default


def _ensure_tables() -> bool:
    c = _conn()
    if c is None:
        return False
    try:
        with c.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS rag_snapshots (
                    id                    SERIAL PRIMARY KEY,
                    computed_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    rag_score             NUMERIC(6,2),
                    coverage_pct          NUMERIC(6,2),
                    deep_dives_present    INTEGER,
                    deep_dives_target     INTEGER,
                    eval_mean_cosine      NUMERIC(6,4),
                    rag_agents_7d         INTEGER,
                    rag_context_packs_7d  INTEGER,
                    weakest_lever         TEXT,
                    action_taken          TEXT,
                    lever_scores          JSONB,
                    findings_filed        INTEGER,
                    detail                JSONB
                )
            """)
        return True
    except Exception:
        return False
    finally:
        try: c.close()
        except Exception: pass


# ── TIER 1 — MEASURE ──────────────────────────────────────────────────
def tier1_measure(prev: dict | None) -> dict:
    m = {
        "coverage_pct": None, "corpus_total": None, "embedded": None,
        "by_corpus": {}, "fresh_cols": {}, "last_indexed": None,
        "narrative_pending": None,
        "deep_dives_present": None, "deep_dives_target": None, "deep_dive_pct": None,
        "eval_mean_cosine": None, "eval_below_floor": None, "eval_zero_results": None,
        "eval_per_query": [], "eval_regressed": False,
        "hnsw_present": None,
        "rag_agents_7d": None, "rag_context_packs_7d": None, "semantic_search_7d": None,
    }

    # 1. Flat-corpus embedding coverage + narrative backlog (via the status endpoint
    #    — the same numbers the reindex cron acts on; endpoint reuse over re-query).
    st = _req("/api/v1/admin/brain/rag/status").get("data") or {}
    if isinstance(st, dict) and st.get("ok"):
        m["coverage_pct"] = _num(st.get("coverage_pct"))
        m["corpus_total"] = _num(st.get("corpus_total"))
        m["embedded"] = _num(st.get("embedded"))
        m["by_corpus"] = st.get("by_corpus") or {}
        m["fresh_cols"] = st.get("fresh_cols") or {}
        m["last_indexed"] = st.get("last_indexed")
        # narrative pending is embedded in the by_corpus string "(N pending)"
        narr = str((m["by_corpus"] or {}).get("market_narratives", ""))
        mo = re.search(r"\((\d+)\s+pending\)", narr)
        m["narrative_pending"] = int(mo.group(1)) if mo else 0

    # 2. Deep-dive coverage over the PUBLISHED market universe (the exact set the
    #    deep-dive cron fills — so 100% is reachable). market_power_scores has NO
    #    'score' column; select nothing risky.
    c = _conn()
    if c is not None:
        try:
            with c.cursor() as cur:
                row = _fetchone(cur, """
                    WITH mkts AS (
                        SELECT DISTINCT ON (market_slug) market_slug
                          FROM market_power_scores
                         WHERE published = true
                         ORDER BY market_slug, computed_at DESC)
                    SELECT COUNT(*)::int AS total,
                           COUNT(*) FILTER (WHERE mdd.market_slug IS NOT NULL)::int AS present
                      FROM mkts
                      LEFT JOIN market_deep_dives mdd
                        ON mdd.market_slug = mkts.market_slug
                       AND coalesce(mdd.narrative_md, '') <> ''
                """, default=(None, None))
                total, present = row[0], row[1]
                m["deep_dives_target"] = int(total) if total is not None else None
                m["deep_dives_present"] = int(present) if present is not None else None
                if total:
                    m["deep_dive_pct"] = round(100.0 * (present or 0) / total, 1)

                # 3. HNSW index present? (the 07-03 seq-scan regression detector)
                hz = _fetchone(cur, """
                    SELECT EXISTS(
                      SELECT 1 FROM pg_indexes
                       WHERE tablename = 'brain_corpus_embeddings'
                         AND indexdef ILIKE '%hnsw%')
                """, default=(None,))
                m["hnsw_present"] = bool(hz[0]) if hz and hz[0] is not None else None

                # 4. REACH — distinct external agents + packs served (7d) over the
                #    canonical dedup table. semantic_search counted SEPARATELY.
                rr = _fetchone(cur, """
                    SELECT
                      COUNT(*) FILTER (WHERE tool_name IN ('get_market_context','get_iso_context')),
                      COUNT(DISTINCT agent_id) FILTER (WHERE tool_name IN ('get_market_context','get_iso_context')),
                      COUNT(*) FILTER (WHERE tool_name = 'semantic_search')
                      FROM mcp_calls_identity
                     WHERE created_at >= NOW() - INTERVAL '7 days'
                       AND is_public_ip IS TRUE AND is_real_external IS TRUE
                """, default=(0, 0, 0))
                m["rag_context_packs_7d"] = int(rr[0] or 0)
                m["rag_agents_7d"] = int(rr[1] or 0)
                m["semantic_search_7d"] = int(rr[2] or 0)
        except Exception:
            pass
        finally:
            try: c.close()
            except Exception: pass

    # 5. RETRIEVAL eval — in-process, reads COSINE (never the rerank 'score').
    ev = _measure_eval(prev)
    m.update(ev)

    m["honest_read"] = (
        f"flat coverage {m['coverage_pct']}% ({m['embedded']}/{m['corpus_total']}) · "
        f"deep-dives {m['deep_dives_present']}/{m['deep_dives_target']} "
        f"({m['deep_dive_pct']}%) · eval mean-cos {m['eval_mean_cosine']} "
        f"({m['eval_below_floor']} below floor, {m['eval_zero_results']} empty) · "
        f"HNSW {'present' if m['hnsw_present'] else 'MISSING'} · "
        f"reach {m['rag_agents_7d']} agents / {m['rag_context_packs_7d']} packs (7d) · "
        f"semantic_search {m['semantic_search_7d']} (internal)"
    )
    return m


def _num(v):
    try:
        if v is None:
            return None
        return float(v) if isinstance(v, str) and "." in v else int(v)
    except (TypeError, ValueError):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None


_EVAL_BUDGET_S = 9.0   # wall-clock cap: if Cohere is slow (embed timeout is 30s
                       # per query), stop after the budget so the tick stays under
                       # the CF/cron deadline. Skipped queries are NOT counted as
                       # broken (skipped != zero-results).


def _eval_one(retrieve_context, spec, remaining):
    """Run one eval query with a HARD per-query wall-clock cap. retrieve_context
    makes up to two sequential Cohere HTTP calls (embed + rerank), each with a
    30s socket timeout — so a between-query budget alone can't stop a single hung
    call from blocking ~60s. Run it on a daemon thread and join() for at most the
    remaining budget; a timeout → (None, timed_out=True), and the orphaned thread
    dies with the process. Returns (rows_or_None, timed_out)."""
    import threading
    box = {}

    def _run():
        try:
            box["rows"] = retrieve_context(spec["q"], k=8, corpus=spec.get("corpus"))
        except Exception:
            box["rows"] = []
    t = threading.Thread(target=_run, daemon=True)
    t.start()
    t.join(max(0.1, remaining))
    if t.is_alive():
        return None, True
    return (box.get("rows") or []), False


def _measure_eval(prev: dict | None) -> dict:
    """Run the fixed eval set through retrieve_context in-process. Reads the
    bi-encoder COSINE (present on every row, stable 0.8+ scale) NOT the rerank
    'score'. Detects: empty results (broken index/corpus), below-floor recall,
    and drift vs the prior snapshot's mean. Each query is HARD-bounded so a
    slow/hung Cohere degrades gracefully (skipped, not counted as broken)."""
    out = {"eval_mean_cosine": None, "eval_below_floor": 0, "eval_zero_results": 0,
           "eval_skipped": 0, "eval_full": False, "eval_per_query": [], "eval_regressed": False,
           "eval_anchor_miss": 0}
    try:
        from routes.brain_rag import retrieve_context
    except Exception:
        out["eval_zero_results"] = len(_EVAL_QUERIES)
        return out
    cosines = []
    ev_start = time.time()
    for i, spec in enumerate(_EVAL_QUERIES):
        remaining = _EVAL_BUDGET_S - (time.time() - ev_start)
        if remaining <= 0.3:
            out["eval_skipped"] = len(_EVAL_QUERIES) - i
            break
        rows, timed_out = _eval_one(retrieve_context, spec, remaining)
        if timed_out:
            # this query ate the remaining budget — mark it + the rest skipped
            # (a slow Cohere is not a "broken index"), and stop.
            out["eval_skipped"] = len(_EVAL_QUERIES) - i
            break
        rows = rows or []
        n = len(rows)
        top1 = float(rows[0].get("cosine", rows[0].get("score", 0.0)) or 0.0) if rows else 0.0
        below = (n == 0) or (top1 < float(spec.get("floor", _EVAL_MEAN_FLOOR)))
        if n == 0:
            out["eval_zero_results"] += 1
        if below:
            out["eval_below_floor"] += 1
        # Ground truth: the top-1 doc must actually be ABOUT the query
        # (>=1 anchor term in its text). None = no anchors declared.
        anchor_hit = None
        anchors = spec.get("anchors") or []
        if anchors and n > 0:
            _t = (rows[0].get("text") or "").lower()
            anchor_hit = any(a in _t for a in anchors)
            if not anchor_hit:
                out["eval_anchor_miss"] += 1
        elif anchors:
            anchor_hit = False
            out["eval_anchor_miss"] += 1
        cosines.append(top1)
        out["eval_per_query"].append({"q": spec["q"][:48], "top1_cosine": round(top1, 4),
                                      "results": n, "below_floor": below,
                                      "anchor_hit": anchor_hit})
    if cosines:
        out["eval_mean_cosine"] = round(sum(cosines) / len(cosines), 4)
    out["eval_full"] = (out["eval_skipped"] == 0 and len(cosines) == len(_EVAL_QUERIES))
    # Regression vs the prior snapshot — ONLY when BOTH runs are FULL. A
    # budget-truncated run means over a front-subset of the eval set (whose
    # corpora/floors differ — market_narratives 0.50 leads), so comparing it to a
    # full-6 mean fabricates regressions that would file bogus findings + tank the
    # retrieval score. prev's completeness lives in its stored measure detail.
    prev_mean, prev_full = None, False
    if prev is not None:
        try:
            pm = prev.get("eval_mean_cosine")
            prev_mean = float(pm) if pm is not None else None
            prev_full = bool(((prev.get("detail") or {}).get("measure") or {}).get("eval_full"))
        except Exception:
            prev_mean, prev_full = None, False
    if (out["eval_full"] and prev_full and prev_mean is not None
            and out["eval_mean_cosine"] is not None):
        out["eval_regressed"] = (prev_mean - out["eval_mean_cosine"]) > _EVAL_REGRESS_DROP
    return out


# ── TIER 2 — SCORE THE FIVE LEVERS (0..1; higher = healthier) ─────────
def tier2_score_levers(m: dict) -> dict:
    # 1. FRESHNESS — flat-corpus embedding coverage, small penalty for a re-chunk
    #    backlog. (Index recency is captured indirectly: a stalled reindex shows as
    #    coverage falling behind.)
    cov = (m.get("coverage_pct") or 0) / 100.0
    pending = m.get("narrative_pending") or 0
    freshness = max(0.0, min(1.0, cov - min(0.30, pending / 100.0)))

    # 2. COVERAGE — deep-dive completeness over published markets.
    dt = m.get("deep_dives_target") or 0
    dp = m.get("deep_dives_present") or 0
    coverage = round(min(1.0, dp / dt), 3) if dt else 0.0

    # 3. RETRIEVAL — mean cosine normalized 0.35..0.65, gated on HNSW + hit-rate;
    #    a broken index or empty results floors it.
    mean_cos = m.get("eval_mean_cosine")
    zero = m.get("eval_zero_results") or 0
    below = m.get("eval_below_floor") or 0
    nq = len(_EVAL_QUERIES)
    hnsw = m.get("hnsw_present")
    if mean_cos is None or zero > 0 or hnsw is False:
        retrieval = 0.2  # broken/degraded — surfaces immediately
    else:
        base = max(0.0, min(1.0, (mean_cos - 0.35) / 0.30))
        hit_rate = (nq - below) / nq if nq else 1.0
        retrieval = round(base * (0.5 + 0.5 * hit_rate), 3)
        if m.get("eval_regressed"):
            retrieval = round(retrieval * 0.6, 3)

    # 4. REACH — north star: distinct external agents/wk over the pack tools.
    agents = m.get("rag_agents_7d") or 0
    reach = round(min(1.0, agents / _REACH_TARGET), 3)

    # 5. GAP — inverse of how many structural gaps are open THIS tick.
    gaps = _count_gaps(m)
    gap = round(max(0.0, 1.0 - min(1.0, gaps * 0.2)), 3)

    scores = {"freshness": round(freshness, 3), "coverage": coverage,
              "retrieval": retrieval, "reach": reach, "gap": gap}
    # weakest ACTIONABLE lever (reach is a pure scoreboard — excluded from action).
    actionable = [(name, scores[name]) for name in _ACTIONABLE if not _lever_off(name)]
    ranked = sorted(actionable, key=lambda kv: kv[1])
    weakest = ranked[0][0] if ranked else "gap"
    return {"scores": scores, "weakest": weakest, "gaps_open": gaps}


def _count_gaps(m: dict) -> int:
    """Dynamic structural gaps this tick (drives the GAP lever score + findings)."""
    g = 0
    if m.get("hnsw_present") is False:
        g += 1
    if (m.get("eval_zero_results") or 0) > 0 or m.get("eval_regressed"):
        g += 1
    dd = m.get("deep_dive_pct")
    if dd is not None and dd < _COVERAGE_FLOOR * 100:
        g += 1
    cp = m.get("coverage_pct")
    if cp is not None and cp < _FRESHNESS_FLOOR * 100:
        g += 1
    for src, state in (m.get("fresh_cols") or {}).items():
        if isinstance(state, str) and state.startswith("inactive"):
            g += 1
    return g


# ── TIER 3 — ACT (one bounded action on the weakest actionable lever) ──
def tier3_act(m: dict, levers: dict) -> dict:
    if not _act_enabled():
        return {"action": "none", "reason": "SHADOW (set RAG_MASTER_ARM=1 to act)",
                "findings_filed": 0}
    lever = levers.get("weakest")
    if _lever_off(lever):
        return {"action": "none", "reason": f"lever '{lever}' killed", "findings_filed": 0}

    if lever == "freshness":
        # Nudge a reindex (the same endpoint brain_rag_reindex_4h hits). Supplemental
        # to the standing cron — bounded to one fire/tick.
        r = _fire("/api/v1/admin/brain/rag/reindex?cap=500", timeout=4)
        return {"action": "reindex_nudge", "lever": lever,
                "target": "/api/v1/admin/brain/rag/reindex?cap=500",
                "dispatched": r.get("dispatched"), "findings_filed": 0}

    if lever == "coverage":
        # Nudge the deep-dive rotation so market_narratives keeps growing. count=10
        # matches the tuned daily rate (~monthly full refresh); do NOT raise without
        # a spend guard (generate_for_market calls Claude per market, no internal cap).
        r = _fire("/api/v1/markets/deep-dive/cron?count=10", timeout=4)
        return {"action": "deep_dive_nudge", "lever": lever,
                "target": "/api/v1/markets/deep-dive/cron?count=10",
                "dispatched": r.get("dispatched"), "findings_filed": 0}

    if lever in ("retrieval", "gap"):
        # File the structural + dynamic gaps for autonomous closure. Retrieval
        # regressions / missing HNSW are filed here too — NEVER auto-run CREATE
        # INDEX (heavy; leave to a reviewed fix).
        filed = _file_gap_findings(m)
        return {"action": "file_findings", "lever": lever, "findings_filed": filed}

    return {"action": "none", "reason": f"unknown lever {lever}", "findings_filed": 0}


def _reach_flag(m: dict) -> str | None:
    """When REACH is the softest signal, surface a cross-team flag (never a fire)
    — adoption of newly-shipped tools is a GEO/distribution problem."""
    a = m.get("rag_agents_7d")
    if a is not None and a < _REACH_TARGET:
        return (f"RAG reach {a} external agents / {m.get('rag_context_packs_7d')} packs (7d) "
                f"vs target {_REACH_TARGET} — get_market_context/get_iso_context are newly "
                "shipped; drive adoption via GEO surfaces (dchub:// resources, /markets pages) "
                "and the MCP tool descriptions, not a shell action.")
    return None


def _file_gap_findings(m: dict) -> int:
    """Upsert the static + dynamic RAG gaps into brain_findings. Opens its OWN
    autocommit=False conn — upsert_brain_finding needs savepoints, and
    ai_reach._conn() is autocommit=True which breaks them SILENTLY (findings would
    report >0 filed while nothing lands)."""
    if not _act_enabled():
        return 0
    try:
        from routes.brain_findings_writer import upsert_brain_finding
    except Exception:
        return 0
    db = os.environ.get("DATABASE_URL")
    if not db:
        return 0
    dyn = []
    if m.get("hnsw_present") is False:
        dyn.append(("rag_hnsw_index_missing",
                    "brain_corpus_embeddings has NO HNSW index — retrieval falls back to a "
                    "full seq scan (the 07-03 regression). Rebuild via "
                    "tools/add_brain_rag_hnsw_index.py (CONCURRENTLY, direct endpoint)."))
    if (m.get("eval_zero_results") or 0) > 0:
        dyn.append(("rag_retrieval_empty_results",
                    f"{m.get('eval_zero_results')} of {len(_EVAL_QUERIES)} fixed eval queries "
                    "returned ZERO results — a corpus is empty or embeddings/index are broken. "
                    f"Detail: {json.dumps(m.get('eval_per_query'))[:400]}"))
    if m.get("eval_regressed"):
        dyn.append(("rag_retrieval_cosine_regression",
                    f"eval mean top-1 cosine dropped >{_EVAL_REGRESS_DROP} vs the prior tick "
                    f"(now {m.get('eval_mean_cosine')}) — recall drift; check embedding model / "
                    "corpus text changes."))
    dd = m.get("deep_dive_pct")
    if dd is not None and dd < _COVERAGE_FLOOR * 100:
        dyn.append(("rag_deep_dive_coverage_low",
                    f"deep-dive coverage {m.get('deep_dives_present')}/{m.get('deep_dives_target')} "
                    f"({dd}%) below {int(_COVERAGE_FLOOR*100)}% — market_narratives corpus is thin; "
                    "the deep-dive rotation (10/day) is closing it ~monthly."))
    for src, state in (m.get("fresh_cols") or {}).items():
        if isinstance(state, str) and state.startswith("inactive"):
            dyn.append((f"rag_fresh_col_inactive_{src}",
                        f"corpus '{src}' declares a fresh_col but it is {state} — the corpus stays "
                        "INSERT-ONLY and updated-in-place rows never re-embed. Fix the column/type."))
    filed = 0
    try:
        import psycopg2
        conn = psycopg2.connect(db, sslmode="require", connect_timeout=8)
        conn.autocommit = False
        try:
            with conn.cursor() as cur:
                for issue, detail in (list(_GAP_FINDINGS) + dyn):
                    try:
                        r = upsert_brain_finding(
                            cur, issue=issue,
                            url="/api/v1/admin/rag/master-tick",
                            detail=detail[:1200], detector="rag_master", status="open")
                        if r in ("inserted", "updated"):
                            filed += 1
                    except Exception:
                        pass
            conn.commit()
        finally:
            try: conn.close()
            except Exception: pass
    except Exception:
        return filed
    return filed


# ── SCORE + PERSIST ───────────────────────────────────────────────────
def rag_score(levers: dict) -> float:
    """0-100. Health = mean of the 4 actionable levers (0.7); adoption = reach
    north star (0.3). Honest: a healthy-but-unused system scores in the 60s, not
    100 — reach only climbs as real agents arrive."""
    s = levers.get("scores") or {}
    health = [s.get(n, 0.0) for n in _ACTIONABLE]
    health_avg = sum(health) / len(health) if health else 0.0
    reach = s.get("reach", 0.0)
    return round(100.0 * (0.7 * health_avg + 0.3 * reach), 2)


def _prev_snapshot() -> dict | None:
    c = _conn()
    if c is None:
        return None
    try:
        import psycopg2.extras
        with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM rag_snapshots ORDER BY id DESC LIMIT 1")
            row = cur.fetchone()
            return dict(row) if row else None
    except Exception:
        return None
    finally:
        try: c.close()
        except Exception: pass


def _persist(m: dict, levers: dict, score: float, action: dict) -> bool:
    if not _ensure_tables():
        return False
    c = _conn()
    if c is None:
        return False
    try:
        with c.cursor() as cur:
            cur.execute("""
                INSERT INTO rag_snapshots
                  (rag_score, coverage_pct, deep_dives_present, deep_dives_target,
                   eval_mean_cosine, rag_agents_7d, rag_context_packs_7d,
                   weakest_lever, action_taken, lever_scores, findings_filed, detail)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """, (
                score, m.get("coverage_pct"),
                m.get("deep_dives_present"), m.get("deep_dives_target"),
                m.get("eval_mean_cosine"), m.get("rag_agents_7d"), m.get("rag_context_packs_7d"),
                levers.get("weakest"), (action or {}).get("action"),
                json.dumps(levers.get("scores") or {}),
                (action or {}).get("findings_filed", 0),
                # ★2026-08-26: reach_flag + north_star were returned ONLY in the
                # master-tick HTTP response — and the tick's sole caller is cron
                # `rag_master_tick_daily`, whose _hit() does `body = resp.read(512)`
                # and keeps {"status", "bytes"}, discarding the body. So the one
                # signal this module produces about its own 0-agent reach had no
                # reader anywhere: it was never persisted, and /master-state (the
                # surface a human queries) never carried it. Persist it here —
                # `detail` is JSONB, so this needs no schema change.
                json.dumps({"measure": m, "levers": levers, "action": action,
                            "reach_flag": _reach_flag(m),
                            "north_star": {
                                "rag_agents_7d": m.get("rag_agents_7d"),
                                "rag_context_packs_7d": m.get("rag_context_packs_7d"),
                                "target_agents_wk": _REACH_TARGET}}),
            ))
        return True
    except Exception:
        note_swallowed_write("rag_snapshots", where="rag_master_shell._persist")
        return False
    finally:
        try: c.close()
        except Exception: pass


# ── ORCHESTRATOR ──────────────────────────────────────────────────────
@rag_master_shell_bp.route("/api/v1/admin/rag/master-tick", methods=["POST", "GET"])
def master_tick():
    if not _admin_ok():
        return jsonify(error="unauthorized"), 401
    if _disabled():
        return jsonify(skipped="RAG_MASTER_DISABLED"), 200
    started = time.time()
    prev = _prev_snapshot()
    # Idempotency: ONE snapshot per UTC day. The cron window is wide (06:xx, so a
    # random ~hourly heartbeat reliably lands in it) which means it re-fires
    # several times/hour — only the first fire of the day should do work (else we
    # write duplicate snapshots, skew the day-over-day deltas, and, when armed,
    # re-fire the reindex/deep-dive nudges). ?force=1 overrides for manual runs.
    # Fail-open: any timestamp-math error falls through to a normal tick.
    if prev and prev.get("computed_at") and request.args.get("force") not in ("1", "true"):
        try:
            ca = prev["computed_at"]
            if isinstance(ca, str):
                ca = datetime.fromisoformat(ca.replace("Z", "+00:00"))
            if (datetime.now(timezone.utc) - ca).total_seconds() < 20 * 3600:
                return jsonify(ok=True, skipped="already_ran_today",
                               last_computed_at=ca.isoformat(),
                               mode=("armed" if _act_enabled() else "shadow")), 200
        except Exception:
            pass
    measure = tier1_measure(prev)
    levers = tier2_score_levers(measure)
    action = tier3_act(measure, levers)
    score = rag_score(levers)
    persisted = _persist(measure, levers, score, action)

    s = levers.get("scores") or {}
    weakest = levers.get("weakest")
    headline = (
        f"rag {score}/100 · weakest → {weakest} ({s.get(weakest)}) · "
        f"deep-dives {measure.get('deep_dives_present')}/{measure.get('deep_dives_target')} "
        f"({measure.get('deep_dive_pct')}%) · eval mean-cos {measure.get('eval_mean_cosine')} · "
        f"reach {measure.get('rag_agents_7d')} agents/wk · acted: {action.get('action')}"
    )
    return jsonify(
        ok=True,
        ms=int((time.time() - started) * 1000),
        rag_score=score,
        headline=headline,
        north_star={"rag_agents_7d": measure.get("rag_agents_7d"),
                    "rag_context_packs_7d": measure.get("rag_context_packs_7d"),
                    "target_agents_wk": _REACH_TARGET},
        reach_flag=_reach_flag(measure),
        tier1_measure=measure,
        tier2_levers=levers,
        tier3_action=action,
        persisted=persisted,
        mode=("armed" if _act_enabled() else "shadow"),
        generated_at=datetime.now(timezone.utc).isoformat(),
    ), 200


@rag_master_shell_bp.route("/api/v1/admin/rag/master-state", methods=["GET"])
def master_state():
    if not _admin_ok():
        return jsonify(error="unauthorized"), 401
    c = _conn()
    if c is None:
        return jsonify(error="db_unavailable"), 503
    try:
        import psycopg2.extras
        with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM rag_snapshots ORDER BY id DESC LIMIT 20")
            rows = [dict(r) for r in cur.fetchall()]
        return jsonify(
            ok=True,
            latest=(rows[0] if rows else None),
            history=rows,
            mode=("armed" if _act_enabled() else "shadow"),
        ), 200
    except Exception as e:
        return jsonify(error=f"{type(e).__name__}: {str(e)[:160]}"), 500
    finally:
        try: c.close()
        except Exception: pass
