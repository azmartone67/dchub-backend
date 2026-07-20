"""
ai_surface_canon.py — THE single source of truth for AI-agent-facing surfaces.
==============================================================================

Every AI-agent surface (llms.txt, the .well-known manifests, AGENTS.md,
integration configs, /connect, /ai, robots.txt, the registry) currently
hand-types the same numbers, so they drift + contradict each other (v2.1.22 vs
2.3.3 vs 2.1.0; "24 tools" and "48 tools" on the SAME page; 232 vs 300+ markets;
an inflated five-figure facility claim on /connect). This module is the fix: one
canon, with the MOVING numbers resolved LIVE at read time so it never goes stale.

Used by ai_surface_sentinel.py to audit (and later auto-refresh) every surface.
"""
from __future__ import annotations

import datetime
import json
import os
import urllib.request

_BASE = os.environ.get("DCHUB_BACKEND_BASE",
                       "https://dchub-backend-production.up.railway.app")

# The tools/list count MUST be probed from the PUBLIC MCP gate agents actually
# connect to — the Node server.mjs at dchub.cloud/mcp (79 tools) — NOT the Flask
# backend's own /mcp (a different/legacy surface that returns 0 to an external
# tools/list, so the "live" override below silently failed and resolve_canon
# fell back to the hand-maintained PINNED count, which lagged 74 vs live 79).
_MCP_BASE = os.environ.get("DCHUB_MCP_PUBLIC_BASE", "https://dchub.cloud")

# ── Pinned structural canon (changes rarely; edit HERE, nowhere else) ──
PINNED = {
    "version": "2.4.4",                       # == repo canonical (server.mjs/server.json); registry mirror auto-bumps to latest+1
    "tools_advertised": 79,                   # canonical advertised count == live tools/list (79 as of 2026-07-20: +get_global_power/get_permitting_intel/plan_query/research_task/simulate_scenario/standing_intent). PINNED fallback; resolve_canon() overrides it with the live count probed from _MCP_BASE (the public gate) so consumers never go stale. /AGENTS.md reads PINNED directly, so keep current.
    "mcp_endpoint": "https://dchub.cloud/mcp",
    "registry_id": "cloud.dchub/mcp-server",
    "rest_base": "https://dchub.cloud/api/v1",     # canonical host (NOT api.dchub.cloud)
    "free_tier_calls_per_day": 10,                 # NOT 100
    "platforms": ["Claude", "ChatGPT", "Gemini", "Perplexity", "Copilot", "Meta AI", "Grok"],
    "tool_names": [
        "search_facilities", "get_facility", "get_market_intel", "rank_markets",
        "get_grid_intelligence", "get_interconnection_queue", "get_fiber_intel",
        "get_gas_intelligence", "list_transactions", "hyperscaler_deals",
        "analyze_site", "compare_sites", "score_facility", "get_news",
    ],
    "fake_tool_denylist": ["get_market_data", "search_deals", "get_transactions"],
    "crawlers_required": ["GrokBot", "xAI-Grok", "Grok-DeepSearch"],
    "public": {                                    # public-facing rounded strings
        "facilities": "21,000+",
        "markets": "300+",
        "deals": "1,400+",   # DISTINCT tracked deals (== canonical_stats.deals_phrase). ★2026-07-17: was "4,000+", itself an over-claim — it floored ROWS, and the AUTO id embeds the ingest date so one deal accrues a row per day (4,275 rows -> ~1,420 distinct). ★NOT the raw `deals` COUNT(*) that /api/v1/stats returns. resolve_canon() overrides this live.
        "countries": "170+",
    },
    # Values known to be STALE/WRONG on some surface — the sentinel flags these.
    # NB: DeepSeek/Mistral were REMOVED (2026-07-01) — they're legitimate
    # available integrations (DC Hub ships /integrations/mistral/ etc.), so
    # listing them isn't "wrong"; the blunt denylist caused false positives on
    # /ai + llms.txt. "platforms" (the verified-active 7) stays the canon for
    # the ACTIVE roll-call; availability is a broader, valid claim.
    # ★2026-07-17: the "4,000+" deal claims are stale AND an over-claim — they
    # counted duplicate rows (see canonical_stats.deals_phrase). Scrub them.
    "stale_markers": ["10,706", "10706", "50,000+", "50000", "317 ", "332 ",
                      "232 ", "100 calls/day", "3,000+ M&A",
                      "2,000+ M&A", "2,000+ tracked deals", "2,000+ deals",
                      "2,000+ tracked M&A", "2,000+ tracked transactions",
                      "4,000+ M&A", "4,000+ tracked deals", "4,000+ deals",
                      "4,000+ tracked M&A", "4,000+ tracked transactions",
                      "24 tools", "48 tools", "49 tools", "51 tools", "53 tools",
                      "58 tools", "72 tools",
                      "2.1.22", "2.3.3", "2.1.0", "2.4.3"],
}


# ── Per-tool "returns:" one-liners — the answer to the recurring AI-model
# critique (Gemini/Perplexity/Grok, 2026-07-02) that agents can't tell what a
# tool returns without a trial call. Keyed to the flagship tool_names above.
# Rendered into llms.txt/llms-full.txt + read by the agent-usefulness master
# shell. Keep concrete (name the returned fields), not marketing.
TOOL_RETURNS = {
    "search_facilities": "facilities {name, operator, lat/lon, power_mw, fiber_count, market_slug, status}",
    "get_facility": "one facility profile {operator, address, lat/lon, power_mw total/used, cooling, fiber carriers, year, status, DCPI verdict, nearby peers}",
    "get_market_intel": "market supply/demand, pricing, vacancy, comparisons",
    "rank_markets": "markets ranked by power certainty & deliverability with DCPI composite_score + BUILD/CAUTION/AVOID verdict",
    "get_grid_intelligence": "ISO grid headroom, constraint, congestion, reserve margin",
    "get_interconnection_queue": "interconnection-queue depth + typical wait (months) for an ISO",
    "get_fiber_intel": "fiber routes, carrier count, lit-building proximity",
    "get_gas_intelligence": "gas-pipeline access + delivered-gas economics",
    "list_transactions": "M&A/deal records {buyer, seller, value_usd, date, type, region}",
    "hyperscaler_deals": "hyperscaler builds/leases with capacity + market",
    "analyze_site": "site suitability score across power/fiber/water/incentives, with sources",
    "compare_sites": "side-by-side 2-4 site comparison across power/fiber/risk/time-to-power",
    "score_facility": "a facility's composite score + component breakdown",
    "get_news": "cited industry news items {title, source, date, relevance}",
}


def _get(path, timeout=15):
    req = urllib.request.Request(_BASE.rstrip("/") + path, method="GET")
    req.add_header("X-DC-Probe", "ai-surface-canon")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def _mcp_tool_count(timeout=20):
    """Live count from the MCP server (tools/list)."""
    hdr = {"Content-Type": "application/json",
           "Accept": "application/json, text/event-stream"}
    init = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                       "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                                  # Self-identify as OUR probe (was "canon", which
                                  # passed through verbatim as a fake 'canon' platform
                                  # in mcp_connections analytics). The dchub- prefix +
                                  # PROBE_PLATFORMS entry keep it out of agent counts.
                                  "clientInfo": {"name": "dchub-canon-probe", "version": "1"}}}).encode()
    req = urllib.request.Request(_MCP_BASE.rstrip("/") + "/mcp", data=init, headers=hdr)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        sid = r.headers.get("mcp-session-id")
    hdr2 = dict(hdr)
    if sid:
        hdr2["mcp-session-id"] = sid
    body = json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}).encode()
    req2 = urllib.request.Request(_MCP_BASE.rstrip("/") + "/mcp", data=body, headers=hdr2)
    with urllib.request.urlopen(req2, timeout=timeout) as r:
        out = r.read().decode("utf-8", "replace")
    for ln in out.splitlines():
        if ln.startswith("data: "):
            ln = ln[6:]
        if ln.startswith("{") and '"id":2' in ln.replace(" ", ""):
            d = json.loads(ln)
            return len((d.get("result") or {}).get("tools", []))
    return None


# ── Funnel metrics (canonical identity views — NEVER raw session counts) ──
#
# The reach dashboard was invalidated 2026-07-01 because it counted rotating
# session_ids as "agents". The ONLY honest funnel numbers come from the
# identity views (mcp_calls_identity / mcp_agent_retention_30d), which key on
# agent_id + public-IP + real-external filters. Media generators must read
# THESE via resolve_canon()["funnel"] — never hand-roll their own counts.

_FUNNEL_QUERIES = {
    "real_agents_7d": (
        "SELECT COUNT(DISTINCT agent_id) FROM mcp_calls_identity "
        "WHERE created_at >= NOW() - INTERVAL '7 days' "
        "AND is_public_ip AND is_real_external"),
    "real_agents_30d": (
        "SELECT COUNT(DISTINCT agent_id) FROM mcp_calls_identity "
        "WHERE created_at >= NOW() - INTERVAL '30 days' "
        "AND is_public_ip AND is_real_external"),
    "day2_return_pct": (
        "SELECT ROUND(AVG(returned_2nd_day::int) * 100, 1) "
        "FROM mcp_agent_retention_30d"),
    # NB: auto_trial_keys keys on minted_at (there is no created_at column).
    "emails_bound_30d": (
        "SELECT COUNT(*) FROM auto_trial_keys "
        "WHERE COALESCE(signed_up_email, operator_email) IS NOT NULL "
        "AND minted_at >= NOW() - INTERVAL '30 days'"),
}


def _funnel_one(cur, sql):
    """Run one metric query fail-soft. Uses a SAVEPOINT so a failed metric
    can't abort a shared transaction (the market-brief tx-abort trap) and a
    caller-passed cursor stays usable for the NEXT metric."""
    sp = False
    try:
        cur.execute("SAVEPOINT _canon_funnel_sp")
        sp = True
    except Exception:
        pass  # autocommit — no tx block, no savepoint needed
    try:
        cur.execute(sql)
        row = cur.fetchone()
        if sp:
            cur.execute("RELEASE SAVEPOINT _canon_funnel_sp")
        return row[0] if row else None
    except Exception:
        if sp:
            try:
                cur.execute("ROLLBACK TO SAVEPOINT _canon_funnel_sp")
            except Exception:
                pass
        raise


def canon_funnel_metrics(cur=None) -> dict:
    """THE single source for funnel numbers on AI surfaces + media posts.

    Returns {real_agents_7d, real_agents_30d, day2_return_pct,
    emails_bound_30d, computed_at} from the canonical identity views.
    Every metric is individually fail-soft to None (never raises). Accepts an
    optional already-open cursor; otherwise opens/returns its own pooled
    connection (same pattern as ai_surface_sentinel._write_findings)."""
    out = {
        "real_agents_7d": None,
        "real_agents_30d": None,
        "day2_return_pct": None,
        "emails_bound_30d": None,
        "computed_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }
    own_conn = None
    return_pg_connection = None
    if cur is None:
        try:
            from main import get_pg_connection, return_pg_connection
            own_conn = get_pg_connection()
            cur = own_conn.cursor()
            # Bound the identity-view scans so a slow-Neon window can't
            # hold this pooled connection hostage. SET LOCAL only (plain
            # SET doesn't stick on the pooled endpoint); scoped to our own
            # tx — never applied to a caller-provided cursor, whose
            # transaction settings belong to the caller.
            try:
                cur.execute("SET LOCAL statement_timeout = 8000")
            except Exception:
                pass
        except Exception as e:
            out["_error"] = f"db_connect: {str(e)[:100]}"
            return out
    try:
        for key, sql in _FUNNEL_QUERIES.items():
            try:
                val = _funnel_one(cur, sql)
                if val is not None:
                    out[key] = float(val) if key.endswith("_pct") else int(val)
            except Exception as e:
                out[f"_{key}_error"] = str(e)[:100]
    finally:
        if own_conn is not None:
            try:
                cur.close()
            except Exception:
                pass
            try:
                own_conn.rollback()  # read-only; leave the pooled conn clean
            except Exception:
                pass
            try:
                return_pg_connection(own_conn)
            except Exception:
                try:
                    own_conn.close()
                except Exception:
                    pass
    return out


def resolve_canon() -> dict:
    """Return the canon with the MOVING numbers resolved LIVE, so the canon
    itself is never stale. Falls back to public strings if a resolver fails."""
    c = json.loads(json.dumps(PINNED))  # deep copy
    c["resolved_at_note"] = "moving numbers resolved live"
    # facilities + markets from /api/v1/stats
    try:
        s = _get("/api/v1/stats")
        c["facilities_live"] = s.get("facilities")
        c["markets_live"] = s.get("markets")
    except Exception as e:
        c["_stats_error"] = str(e)[:120]
    # Deals resolve from canonical_stats (curated buyer+seller subset), NOT
    # /api/v1/stats `deals` — that field is the RAW ~11.5K pile (capex/
    # undisclosed/junk) and publishing it as "M&A deals" is a ~2.5x over-claim
    # (2026-07-16 double-count trap). Floors to "4,000+". Overrides the pinned
    # public string so every resolve_canon() consumer self-heals.
    try:
        from canonical_stats import deals_phrase as _deals_phrase
        _dp = _deals_phrase()
        c["deals_live"] = _dp
        c["public"]["deals"] = _dp
    except Exception as e:
        c["_deals_error"] = str(e)[:120]
    # live tool count from the MCP server — override the pinned fallback so
    # every resolve_canon() consumer tracks tools/list and never goes stale.
    try:
        c["tools_live"] = _mcp_tool_count()
        if isinstance(c["tools_live"], int) and c["tools_live"] > 0:
            c["tools_advertised"] = c["tools_live"]
    except Exception as e:
        c["_tools_error"] = str(e)[:120]
    # funnel metrics from the canonical identity views (fail-soft internally)
    try:
        c["funnel"] = canon_funnel_metrics()
    except Exception as e:
        c["funnel"] = None
        c["_funnel_error"] = str(e)[:120]
    return c
