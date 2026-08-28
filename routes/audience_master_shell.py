"""
audience_master_shell.py — Audience / top-of-funnel master orchestrator (2026-06-29).
=====================================================================================

WHY
---
The conversion machine is tuned, but the binding constraint is AUDIENCE: real
external demand is ~21 distinct agents/week (`unique_ips_7d_real`), ~82 real
tool calls vs ~2,800 probe calls (97% noise), Claude-dominated. It's a pure
top-of-funnel DISCOVERY problem — and it was invisible because (a) the 1.3M
"requests served" headline is mostly internal/transport, and (b) there is NO
acquisition attribution and the weekly reach rollup returns zeros.

GEO is the lever that fits a Claude-dominated, AI-assistant-using audience: when
a developer asks ANY AI "how do I get live data-center / power / grid /
interconnection data into my agent," DC Hub should be the recommended MCP server.
A live probe (2026-06-29) confirmed the gap precisely:
  · narrow/branded query ("MCP server data center power grid data") → DC Hub is
    the #1 result. GEO already WON there.
  · broad high-intent query ("how to get live data center capacity + power data
    into an AI agent") → DC Hub is INVISIBLE; the AI says "go scrape PJM/CAISO."
New audience comes from the broad queries — exactly where DC Hub doesn't exist.

WHAT (mirrors brain_master_orchestrator's tiered self-call shape)
-----------------------------------------------------------------
POST /api/v1/admin/audience/master-tick  runs four tiers and returns a digest:
  · TIER 1 — MEASURE:   honest audience snapshot (real agents, probe ratio,
                        Claude share, new-vs-returning, upgrade signals) from
                        /api/v1/mcp/funnel + /api/v1/ai/reach, PERSISTED to a
                        clean forward series (audience_snapshots) so the trend
                        is reliable from here on — independent of the broken
                        historical reach_weekly backfill.
  · TIER 2 — GEO AUDIT: surface health (llms.txt freshness, mcp.json present) +
                        per-query DISCOVERABILITY COVERAGE across a curated set
                        of broad high-intent developer queries. Emits the GAP
                        list — the queries DC Hub is invisible for.
  · TIER 3 — ACT:       picks the top uncovered query and emits a GEO content
                        brief to close it. Human-gated (no auto-publish) — same
                        posture as the brain's draft-PR writer.
  · TIER 4 — VERIFY:    deltas vs the previous snapshot (real agents, coverage)
                        so each tick shows whether the needle moved.

GET /api/v1/admin/audience/state  returns the latest snapshot + trend.

Auth: X-Admin-Key (DCHUB_ADMIN_KEY / DCHUB_INTERNAL_KEY). Fail-closed.
Kill switch: AUDIENCE_MASTER_DISABLED=1.
"""
from __future__ import annotations

import hmac
import json
import os
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

from flask import Blueprint, jsonify, request
from routes._swallowed_writes import note_swallowed_write

audience_master_shell_bp = Blueprint("audience_master_shell", __name__)

_BACKEND_BASE = os.environ.get(
    "DCHUB_BACKEND_BASE",
    "https://dchub-backend-production.up.railway.app",
)
_GEO_BASE = "https://dchub.cloud"

# ── The discovery queries that define the audience. Each is a BROAD,
# high-intent developer question — the moment someone needs this data and
# turns to an AI. `must_any` = intent keywords; DC Hub's GEO surface is
# "covering" a query when its language is present (proxy for "an AI would
# retrieve + recommend DC Hub here"). The 2026-06-29 probe showed the narrow
# branded queries are won and the broad ones are the gap — this list is
# deliberately weighted toward the broad ones.
_DISCOVERY_QUERIES = [
    {"q": "how to get live data center capacity data into an AI agent",
     "must_any": ["data center capacity", "capacity data", "live data center"]},
    {"q": "power grid / ISO headroom data API for developers",
     "must_any": ["grid intelligence", "iso", "grid headroom", "interconnection"]},
    {"q": "interconnection queue data API",
     "must_any": ["interconnection queue", "interconnection", "queue"]},
    {"q": "data center M&A / transaction data API",
     "must_any": ["m&a", "transactions", "deal", "acquisition"]},
    {"q": "dark fiber route data API for site selection",
     "must_any": ["fiber", "dark fiber", "fiber route"]},
    {"q": "data center site selection data programmatically",
     "must_any": ["site selection", "site scoring", "score_facility"]},
    {"q": "power market / energy price data MCP server",
     "must_any": ["power market", "energy price", "energy prices"]},
    {"q": "data center water risk data for AI infrastructure",
     "must_any": ["water risk", "water"]},
    {"q": "where do AI agents get real-time infrastructure ground truth",
     "must_any": ["ground truth", "infrastructure data layer", "live infrastructure"]},
    {"q": "MCP server for data center power data (branded — should already win)",
     "must_any": ["dc hub", "dchub", "data center power index", "dcpi"]},
]


# ── auth ──────────────────────────────────────────────────────────────
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
    return str(os.environ.get("AUDIENCE_MASTER_DISABLED", "")).lower() in ("1", "true", "yes")


# ── self-call (mirrors brain_master_orchestrator._call) ───────────────
def _call(path: str, timeout: int = 40) -> dict:
    url = _BACKEND_BASE.rstrip("/") + path
    started = time.time()
    try:
        req = urllib.request.Request(url, method="GET")
        req.add_header("X-DC-Probe", "audience-tick")  # rate-limiter bypass
        req.add_header("User-Agent", "dchub-audience-orchestrator/1.0")
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


def _fetch_text(url: str, timeout: int = 15) -> tuple[int, str]:
    try:
        req = urllib.request.Request(url, method="GET")
        req.add_header("User-Agent", "dchub-audience-orchestrator/1.0")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, ""
    except Exception:
        return 0, ""


# ── DB (raw autocommit conn — db_utils SKIPS DDL at SKIP_DDL=1) ───────
def _conn():
    try:
        from routes.ai_reach import _conn as _raw
        return _raw()
    except Exception:
        return None


def _ensure_tables() -> bool:
    c = _conn()
    if c is None:
        return False
    try:
        with c.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS audience_snapshots (
                    id                  SERIAL PRIMARY KEY,
                    computed_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    real_agents_7d      INTEGER,
                    real_calls_7d       INTEGER,
                    probe_calls_7d      INTEGER,
                    probe_ratio         NUMERIC(6,3),
                    claude_share        NUMERIC(6,3),
                    new_agents_7d       INTEGER,
                    upgrade_signals_7d  INTEGER,
                    geo_surface_health  NUMERIC(6,3),
                    geo_query_coverage  NUMERIC(6,3),
                    geo_gaps            JSONB,
                    detail              JSONB
                )
            """)
            # CREATE TABLE IF NOT EXISTS is a no-op on an existing table, so a
            # new column needs its own ALTER — the table predates this one.
            cur.execute("""
                ALTER TABLE audience_snapshots
                ADD COLUMN IF NOT EXISTS real_agents_7d_loose INTEGER
            """)
        return True
    except Exception:
        return False
    finally:
        try: c.close()
        except Exception: pass


# ── TIER 1 — MEASURE ──────────────────────────────────────────────────
def tier1_measure() -> dict:
    funnel = _call("/api/v1/mcp/funnel")
    reach = _call("/api/v1/ai/reach")
    fd = funnel.get("data") or {}
    rd = reach.get("data") or {}

    real_calls = _num(fd.get("tool_calls_7d_real"))
    probe_calls = _num(fd.get("tool_calls_7d_probes"))
    # ★★2026-07-31: was `unique_ips_7d_real OR distinct_agents_7d` — it took
    # the LOOSER of two counters that disagreed 2.3x (145 vs 64 on 07-31) and
    # published it as the headline. Neither was right: the funnel field is a
    # loose DISTINCT ip_address (keeps scripted clients + CF edge, ~1.5x high)
    # and reach's distinct_agents_7d is the canonical basis on an ISO-WEEK
    # rollup, so mid-week it reads ~33% low. Canonical trailing-7d = 95.
    #
    # Both canonical fields below come from ONE query
    # (mcp_calls_deloop.canonical_external_activity_sql, added by r-agent-
    # parity the same day) — funnel and reach publish it under different
    # names, so try both before degrading. Order: canonical → ISO-week rollup
    # → loose, each step labelled, so a cold funnel still yields a number and
    # the report says which basis produced it.
    real_agents_loose = _num(fd.get("unique_ips_7d_real"))
    real_agents = (_num(fd.get("real_external_agents_7d"))
                   or _num(rd.get("real_agents_7d"))
                   or _num(rd.get("distinct_agents_7d"))
                   or real_agents_loose)
    upgrade_signals = _num(fd.get("upgrade_signals_7d"))
    total = (real_calls or 0) + (probe_calls or 0)
    probe_ratio = round((probe_calls or 0) / total, 3) if total else None

    # Claude share + new-vs-returning from the reach per-platform breakdown.
    per = rd.get("per_platform") or []
    claude_agents = sum(_num(p.get("agents")) or 0 for p in per
                        if "claud" in str(p.get("platform_id", "")).lower()
                        or "anthropic" in str(p.get("platform_id", "")).lower())
    total_agents = sum(_num(p.get("agents")) or 0 for p in per) or (real_agents or 0)
    claude_share = round(claude_agents / total_agents, 3) if total_agents else None

    # new_external_ips comes from the reach trend "current".
    trend = _call("/api/v1/ai/reach/trend")
    cur = (trend.get("data") or {}).get("current") or {}
    new_agents = _num(cur.get("new_external_ips"))

    return {
        "real_agents_7d": real_agents,
        # The old loose counter, carried so the pre-07-31 series stays
        # interpretable: every audience_snapshots row before that date holds a
        # LOOSE number ~1.5x the canonical one. Compare like with like.
        "real_agents_7d_loose": real_agents_loose,
        "real_agents_basis": (
            "canonical (funnel.real_external_agents_7d)"
            if _num(fd.get("real_external_agents_7d"))
            else "canonical (reach.real_agents_7d)" if _num(rd.get("real_agents_7d"))
            else "reach rollup (ISO week — runs low mid-week)"
            if _num(rd.get("distinct_agents_7d"))
            else "LOOSE unique_ips_7d_real — canonical unavailable"
        ),
        "real_calls_7d": real_calls,
        "probe_calls_7d": probe_calls,
        "probe_ratio": probe_ratio,
        "claude_share": claude_share,
        "new_agents_7d": new_agents,
        "upgrade_signals_7d": upgrade_signals,
        "honest_read": (
            f"{real_agents} real agents / {real_calls} real calls this week "
            f"({int((probe_ratio or 0)*100)}% of traffic is probes); "
            f"Claude share {int((claude_share or 0)*100)}%."
        ),
    }


# ── TIER 2 — GEO AUDIT ────────────────────────────────────────────────
def tier2_geo_audit() -> dict:
    # surface health
    code_llms, llms = _fetch_text(_GEO_BASE + "/llms.txt")
    code_mcp, _mcp = _fetch_text(_GEO_BASE + "/.well-known/mcp.json")
    fresh_days = None
    for line in (llms.splitlines()[:12] if llms else []):
        if "Last Updated" in line or "Last-Updated" in line:
            try:
                ds = line.split(":")[-1].strip()
                dt = datetime.fromisoformat(ds)
                fresh_days = (datetime.now(timezone.utc).date() - dt.date()).days
            except Exception:
                pass
    surface_health = round(
        0.5 * (1.0 if code_llms == 200 else 0.0)
        + 0.3 * (1.0 if code_mcp == 200 else 0.0)
        + 0.2 * (1.0 if (fresh_days is not None and fresh_days <= 14) else 0.0),
        3)

    # per-query coverage: is the query's intent language present in the GEO
    # surface? Proxy for "an AI would retrieve + recommend DC Hub here."
    hay = (llms or "").lower()
    covered, gaps = [], []
    for item in _DISCOVERY_QUERIES:
        hit = any(kw.lower() in hay for kw in item["must_any"])
        (covered if hit else gaps).append(item["q"])
    coverage = round(len(covered) / len(_DISCOVERY_QUERIES), 3) if _DISCOVERY_QUERIES else None

    return {
        "surface_health": surface_health,
        "llms_txt": {"http": code_llms, "bytes": len(llms), "fresh_days": fresh_days},
        "mcp_json": {"http": code_mcp},
        "query_coverage": coverage,
        "covered": covered,
        "gaps": gaps,
    }


# ── TIER 3 — ACT (human-gated GEO content brief) ──────────────────────
def tier3_act(geo: dict) -> dict:
    gaps = geo.get("gaps") or []
    if not gaps:
        return {"action": "none", "note": "no uncovered discovery queries — GEO coverage full."}
    target = gaps[0]
    brief = {
        "target_query": target,
        "intent": "A developer asking this turns to an AI and currently gets 'go scrape "
                  "PJM/CAISO yourself' — DC Hub is invisible at the moment of intent.",
        "surface_to_ship": "A dedicated GEO answer page + an llms.txt section that directly "
                           "answers this query and names DC Hub's MCP tool as the one-step solution.",
        "must_include": [
            f"H1/opening that mirrors the query verbatim: \"{target}\"",
            "the exact MCP tool(s) that answer it + a copy-paste connect line "
            "(claude mcp add dchub --transport http https://dchub.cloud/mcp)",
            "a 1-call example + a cited live data point (proof, not claims)",
            "the free-tier hook (10 calls/day, no email) so trying it is frictionless",
        ],
        "gate": "human_review",  # never auto-publish positioning copy
        "remaining_gaps": gaps[1:],
    }
    return {"action": "geo_content_brief", "brief": brief}


# ── persistence + verify ──────────────────────────────────────────────
def _num(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None


def _persist(m: dict, geo: dict) -> bool:
    if not _ensure_tables():
        return False
    c = _conn()
    if c is None:
        return False
    try:
        with c.cursor() as cur:
            cur.execute("""
                INSERT INTO audience_snapshots
                  (real_agents_7d, real_agents_7d_loose, real_calls_7d,
                   probe_calls_7d, probe_ratio,
                   claude_share, new_agents_7d, upgrade_signals_7d,
                   geo_surface_health, geo_query_coverage, geo_gaps, detail)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING
            """, (
                m.get("real_agents_7d"), m.get("real_agents_7d_loose"),
                m.get("real_calls_7d"), m.get("probe_calls_7d"),
                m.get("probe_ratio"), m.get("claude_share"), m.get("new_agents_7d"),
                m.get("upgrade_signals_7d"), geo.get("surface_health"),
                geo.get("query_coverage"), json.dumps(geo.get("gaps") or []),
                json.dumps({"measure": m, "geo": geo}),
            ))
        return True
    except Exception:
        note_swallowed_write("audience_snapshots", where="audience_master_shell._persist")
        return False
    finally:
        try: c.close()
        except Exception: pass


def _prev_snapshot() -> dict | None:
    c = _conn()
    if c is None:
        return None
    try:
        import psycopg2.extras
        with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM audience_snapshots ORDER BY id DESC LIMIT 1")
            return cur.fetchone()
    except Exception:
        return None
    finally:
        try: c.close()
        except Exception: pass


def tier4_verify(m: dict, geo: dict) -> dict:
    prev = _prev_snapshot()
    if not prev:
        return {"baseline": True, "note": "first snapshot — deltas start next tick."}

    def d(cur, key):
        p = prev.get(key)
        try:
            return None if p is None or cur is None else round(float(cur) - float(p), 3)
        except (TypeError, ValueError):
            return None
    return {
        "baseline": False,
        "since": str(prev.get("computed_at")),
        "delta_real_agents_7d": d(m.get("real_agents_7d"), "real_agents_7d"),
        "delta_new_agents_7d": d(m.get("new_agents_7d"), "new_agents_7d"),
        "delta_geo_coverage": d(geo.get("query_coverage"), "geo_query_coverage"),
        "delta_upgrade_signals_7d": d(m.get("upgrade_signals_7d"), "upgrade_signals_7d"),
    }


# ── orchestrator ──────────────────────────────────────────────────────
@audience_master_shell_bp.route("/api/v1/admin/audience/master-tick", methods=["POST", "GET"])
def master_tick():
    if not _admin_ok():
        return jsonify(error="unauthorized"), 401
    if _disabled():
        return jsonify(skipped="AUDIENCE_MASTER_DISABLED"), 200
    started = time.time()
    measure = tier1_measure()
    geo = tier2_geo_audit()
    verify = tier4_verify(measure, geo)        # delta vs PREVIOUS snapshot (before insert)
    act = tier3_act(geo)
    persisted = _persist(measure, geo)

    headline = (
        f"{measure.get('real_agents_7d')} real agents/wk · "
        f"GEO coverage {int((geo.get('query_coverage') or 0)*100)}% · "
        f"{len(geo.get('gaps') or [])} discovery gaps · "
        f"top gap → {(act.get('brief') or {}).get('target_query', 'none')}"
    )
    return jsonify(
        ok=True,
        ms=int((time.time() - started) * 1000),
        headline=headline,
        tier1_measure=measure,
        tier2_geo=geo,
        tier3_act=act,
        tier4_verify=verify,
        persisted=persisted,
        generated_at=datetime.now(timezone.utc).isoformat(),
    ), 200


@audience_master_shell_bp.route("/api/v1/admin/audience/state", methods=["GET"])
def state():
    if not _admin_ok():
        return jsonify(error="unauthorized"), 401
    c = _conn()
    if c is None:
        return jsonify(error="db_unavailable"), 503
    try:
        import psycopg2.extras
        with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            # ONE ROW PER UTC DAY (the last snapshot of each day), last 14 days.
            # A plain "ORDER BY id DESC LIMIT 14" collapsed to a SINGLE day:
            # master-tick is fired by cron_heartbeat, brain_lane_driver,
            # gap_master_shell and distribution_master_shell as well as the
            # daily task, so a day routinely writes 10+ snapshots and the
            # window never spanned long enough to show a trend.
            cur.execute("""
                SELECT DISTINCT ON ((computed_at AT TIME ZONE 'UTC')::date) *
                FROM audience_snapshots
                ORDER BY (computed_at AT TIME ZONE 'UTC')::date DESC, id DESC
                LIMIT 14
            """)
            rows = cur.fetchall() or []
            # `latest` stays the true most-recent snapshot, not a day rollup.
            cur.execute("SELECT * FROM audience_snapshots ORDER BY id DESC LIMIT 1")
            latest = cur.fetchone()
            cur.execute("SELECT COUNT(*) AS n FROM audience_snapshots")
            total = (cur.fetchone() or {}).get("n")

        trend = list(reversed(rows))
        span = None
        if len(trend) >= 2:
            try:
                span = round(
                    (trend[-1]["computed_at"] - trend[0]["computed_at"]).total_seconds() / 86400.0,
                    2,
                )
            except (TypeError, KeyError):
                span = None
        return jsonify(
            latest=latest,
            trend=trend,
            count=len(trend),          # = distinct DAYS present, not raw snapshots
            days_span=span,            # oldest→newest trend row, in days
            snapshots_total=total,     # every raw row ever written
        ), 200
    except Exception as e:
        return jsonify(error=f"{type(e).__name__}: {str(e)[:120]}"), 500
    finally:
        try: c.close()
        except Exception: pass
