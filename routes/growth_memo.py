"""routes/growth_memo.py — the weekly partner growth memo (2026-07-20).

Perplexity asked for a standing weekly memo, Copilot specced the telemetry
spine — this endpoint produces both. Its whole reason for existing is to keep
the THREE numbers that everyone conflates on SEPARATE lines so the reach-vs-
tool-use gap can never hide again (see reach-vs-tooluse-vs-conversion):

  1. REACH        — requests from a platform's User-Agent (ai_cumulative /
                    ai_daily_stats). Crawler/citation-dominated. NOT tool-use.
  2. REAL TOOL-USE — distinct agents actually invoking MCP tools
                    (mcp_calls_identity, CF-POP-corrected view), split into
                    named-platform vs the generic 'mcp' clientInfo bucket.
  3. CONVERSION   — the handoff funnel: relay-minted -> email -> paid.

GET /api/v1/admin/growth-memo            → JSON (all platforms)
GET /api/v1/admin/growth-memo?format=md  → markdown, partner-shareable
GET /api/v1/admin/growth-memo?platform=perplexity → adds a focused per-platform block

Admin-keyed. Pure-DB, read-only, one short-lived connection per call (mirrors
flywheel_master_shell._conn). Reuses mcp_calls_deloop for the ONE canonical
real-call predicate + platform classifier, and ai_tracking.AI_PLATFORMS for the
external allowlist — so the memo can never drift from the funnel/flywheel.
"""
from __future__ import annotations

import logging
import os

from flask import Blueprint, Response, jsonify, request

logger = logging.getLogger(__name__)

growth_memo_bp = Blueprint("growth_memo", __name__)


# ── auth / db (mirror flywheel_master_shell + growth_ops_digest) ──────────────

def _admin_ok() -> bool:
    sent = (request.headers.get("X-Admin-Key") or request.headers.get("X-Internal-Key")
            or request.args.get("admin_key") or "").strip()
    ok = {(os.environ.get("DCHUB_ADMIN_KEY") or "").strip(),
          (os.environ.get("DCHUB_INTERNAL_KEY") or "").strip()}
    ok.discard("")
    return bool(sent) and sent in ok


def _conn():
    try:
        import psycopg2 as _pg
        url = os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL")
        if not url:
            return None
        c = _pg.connect(url, connect_timeout=8)
        c.autocommit = True
        return c
    except Exception as e:
        logger.warning("[growth_memo] db connect failed: %s", e)
        return None


def _rows(c, sql: str) -> list:
    try:
        with c.cursor() as cur:
            cur.execute(sql)
            return cur.fetchall()
    except Exception as e:
        logger.debug("[growth_memo] query failed: %s", e)
        return []


def _one(c, sql: str):
    r = _rows(c, sql)
    return r[0] if r else None


# ── external-AI allowlist (same source the /ai page + flywheel lane 6 use) ────

_EXT_FALLBACK = (
    "chatgpt", "claude", "gemini", "perplexity", "copilot", "grok", "deepseek",
    "cursor", "windsurf", "meta", "cohere", "you", "smithery", "groq",
    "huggingface", "mistral", "webmcp", "kimi", "qwen", "zai", "minimax")


def _ext_in_list() -> str:
    try:
        from ai_tracking import AI_PLATFORMS as _AP
        keys = sorted(_AP.keys())
    except Exception:
        keys = sorted(_EXT_FALLBACK)
    return ",".join("'" + str(k).replace("'", "''") + "'" for k in keys)


def _wow(cur, prev):
    """'+12.3%' style delta, or None when prior is 0/missing."""
    try:
        if cur is None or prev is None or float(prev) == 0:
            return None
        return round(100.0 * (float(cur) - float(prev)) / float(prev), 1)
    except (TypeError, ValueError):
        return None


# A row is GENERIC (unattributable) when its clientInfo is empty, a rotating
# session UUID, or the literal defaults 'mcp' / 'unknown'. Anything else is a
# NAMED caller. Kept here (not in the deloop module) because it's a memo-only
# split, but it uses the same columns the canonical view exposes.
_GENERIC_CLIENT = (
    "(client_name IS NULL OR btrim(client_name) = '' "
    "OR client_name ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' "
    "OR lower(client_name) IN ('mcp','unknown'))")


def _build_memo(c, platform: str | None = None) -> dict:
    ext = _ext_in_list()
    try:
        from mcp_calls_deloop import PLATFORM_CASE
        pcase = PLATFORM_CASE.strip()
    except Exception:
        pcase = "COALESCE(NULLIF(lower(client_name),''),'unknown')"

    # 1 ── REACH (ai_daily_stats): external total 7d vs prior 7d.
    rrow = _one(c, "SELECT "
                   " COALESCE(SUM(request_count) FILTER (WHERE date >= CURRENT_DATE - 7),0),"
                   " COALESCE(SUM(request_count) FILTER (WHERE date >= CURRENT_DATE - 14 AND date < CURRENT_DATE - 7),0) "
                   "FROM ai_daily_stats "
                   "WHERE date >= CURRENT_DATE - 14 AND lower(platform) IN (" + ext + ")")
    reach_7d, reach_prev = (rrow if rrow else (None, None))

    # 2 ── REAL TOOL-USE (mcp_calls_identity — CF-POP-corrected view).
    trow = _one(c, "SELECT "
                   " count(DISTINCT agent_id) FILTER (WHERE created_at >= now() - interval '7 days'),"
                   " count(DISTINCT agent_id) FILTER (WHERE created_at < now() - interval '7 days'),"
                   " count(*) FILTER (WHERE created_at >= now() - interval '7 days'),"
                   " count(*) FILTER (WHERE created_at < now() - interval '7 days') "
                   "FROM mcp_calls_identity "
                   "WHERE is_public_ip AND is_real_external "
                   "AND created_at >= now() - interval '14 days'")
    agents_7d, agents_prev, calls_7d, calls_prev = (trow if trow else (None, None, None, None))

    # 3 ── NAMED vs GENERIC 'mcp' (of real tool-use, 7d) — the attribution gap.
    nrow = _one(c, "SELECT "
                   " count(DISTINCT agent_id) FILTER (WHERE NOT " + _GENERIC_CLIENT + "),"
                   " count(*)                 FILTER (WHERE NOT " + _GENERIC_CLIENT + "),"
                   " count(DISTINCT agent_id) FILTER (WHERE " + _GENERIC_CLIENT + "),"
                   " count(*)                 FILTER (WHERE " + _GENERIC_CLIENT + ") "
                   "FROM mcp_calls_identity "
                   "WHERE is_public_ip AND is_real_external "
                   "AND created_at >= now() - interval '7 days'")
    named_agents, named_calls, generic_agents, generic_calls = (nrow if nrow else (0, 0, 0, 0))

    # 4 ── FUNNEL (handoff): relay-mint -> identity -> paid, 7d vs prior 7d.
    def _funnel(lo: str, hi: str) -> dict:
        win = ("first_hit_at > now() - interval '%s' AND first_hit_at <= now() - interval '%s'"
               % (lo, hi)) if hi != "0" else ("first_hit_at > now() - interval '%s'" % lo)
        pay = _one(c, "SELECT count(DISTINCT session_id) FROM mcp_upgrade_signals "
                      "WHERE signal_type IN ('trial_preview','paid_tool_blocked') AND session_id IS NOT NULL "
                      "AND " + win.replace("first_hit_at", "created_at"))
        high = _one(c, "SELECT count(DISTINCT mcp_session_id) FROM mcp_high_intent_sessions WHERE " + win)
        mint = _one(c, "SELECT count(DISTINCT mcp_session_id) FROM mcp_high_intent_sessions "
                       "WHERE claim_minted_at IS NOT NULL AND " + win)
        email = _one(c, "SELECT count(DISTINCT lower(email)) FROM mcp_dev_keys "
                        "WHERE email IS NOT NULL AND email <> '' AND "
                        + win.replace("first_hit_at", "created_at"))
        conv = _one(c, "SELECT count(*) FROM mcp_conversions WHERE "
                       + win.replace("first_hit_at", "created_at::timestamptz"))
        g = lambda x: (int(x[0]) if x and x[0] is not None else 0)
        return {"paywall": g(pay), "high_intent": g(high), "relay_minted": g(mint),
                "emails_captured": g(email), "conversions": g(conv)}

    funnel_7d = _funnel("7 days", "0")
    funnel_prev = _funnel("14 days", "7 days")

    # 5 ── GROUNDING pass rate — NOT yet instrumented (no grounding_evidence_present
    # column exists). Surface the definition question we put to Perplexity + a
    # HONEST proxy (success rate of real calls), clearly labelled as a proxy.
    srow = _one(c, "SELECT "
                   " count(*) FILTER (WHERE success),"
                   " count(*) "
                   "FROM mcp_calls_identity "
                   "WHERE is_public_ip AND is_real_external "
                   "AND created_at >= now() - interval '7 days'")
    ok_calls, tot_calls = (srow if srow else (0, 0))
    success_rate = (round(100.0 * ok_calls / tot_calls, 1) if tot_calls else None)

    # 6 ── PER-PLATFORM tables (kept separate on purpose — reach is UA-based,
    # tool-use is classifier-based; they don't join, and that's the point).
    reach_by = _rows(c, "SELECT platform, SUM(request_count) AS reach_7d "
                        "FROM ai_daily_stats "
                        "WHERE date >= CURRENT_DATE - 7 AND lower(platform) IN (" + ext + ") "
                        "GROUP BY 1 ORDER BY 2 DESC LIMIT 12")
    tooluse_by = _rows(c, "SELECT (" + pcase + ") AS platform,"
                          " count(DISTINCT agent_id) AS agents_7d, count(*) AS calls_7d "
                          "FROM mcp_calls_identity "
                          "WHERE is_public_ip AND is_real_external "
                          "AND created_at >= now() - interval '7 days' "
                          "GROUP BY 1 ORDER BY 3 DESC LIMIT 12")

    # 6b ── PLANNER ADOPTION (ChatGPT's front-door KPIs, 2026-07-21): does a
    # workflow START with plan_query, and does the planner shorten workflows?
    # A "workflow" = one session_id. Front door shipped 2026-07-20, so early
    # cuts are the BEFORE baseline to watch climb.
    prow = _one(c, "WITH sess AS ("
                   " SELECT session_id,"
                   " (array_agg(tool_name ORDER BY created_at))[1] AS first_tool,"
                   " count(*) AS n, bool_or(tool_name='plan_query') AS used_planner"
                   " FROM mcp_calls_identity"
                   " WHERE is_public_ip AND is_real_external"
                   " AND created_at >= now() - interval '7 days'"
                   " AND session_id IS NOT NULL AND btrim(session_id) <> ''"
                   " GROUP BY session_id) "
                   "SELECT count(*), count(*) FILTER (WHERE first_tool='plan_query'),"
                   " count(*) FILTER (WHERE used_planner),"
                   " round(avg(n)::numeric,2),"
                   " round((avg(n) FILTER (WHERE first_tool='plan_query'))::numeric,2),"
                   " round((avg(n) FILTER (WHERE first_tool<>'plan_query'))::numeric,2) "
                   "FROM sess")
    p_sess, p_first, p_any, p_avg, p_avg_planner, p_avg_direct = (prow if prow else (0, 0, 0, None, None, None))

    # 7 ── WHAT CHANGED — the single biggest WoW mover, phrased.
    movers = []
    for label, cur, prev in (("distinct real agents", agents_7d, agents_prev),
                             ("real tool calls", calls_7d, calls_prev),
                             ("external reach", reach_7d, reach_prev),
                             ("conversions", funnel_7d["conversions"], funnel_prev["conversions"])):
        w = _wow(cur, prev)
        if w is not None:
            movers.append((abs(w), label, cur, prev, w))
    movers.sort(reverse=True)
    what_changed = (
        f"Biggest WoW move: {movers[0][1]} {movers[0][4]:+.1f}% "
        f"({movers[0][3]} → {movers[0][2]})." if movers else "No week-over-week baseline yet.")

    memo = {
        "ok": True,
        "as_of": "live",
        "window": "7d vs prior 7d",
        "headline": {
            "reach_external_7d": reach_7d, "reach_external_wow_pct": _wow(reach_7d, reach_prev),
            "real_agents_7d": agents_7d, "real_agents_wow_pct": _wow(agents_7d, agents_prev),
            "real_calls_7d": calls_7d, "real_calls_wow_pct": _wow(calls_7d, calls_prev),
            "reach_to_tooluse_note": (
                "reach is crawler/citation traffic; real tool-use is agents invoking MCP tools — "
                "different funnels, never sum them"),
        },
        "attribution_gap": {
            "named_agents_7d": named_agents, "named_calls_7d": named_calls,
            "generic_mcp_agents_7d": generic_agents, "generic_mcp_calls_7d": generic_calls,
            "note": ("generic = clientInfo empty / rotating UUID / literal 'mcp' — unattributable to a platform; "
                     "an explicit X-MCP-Platform header or a clean clientInfo moves a caller into the named column"),
        },
        "funnel": {
            "this_week": funnel_7d, "prior_week": funnel_prev,
            "note": ("relay-mint is fixed (mint on first paid-tool hit); the live leaks are relay→identity "
                     "and identity→paid. Human-click ~0 is by design (server auto-redeems the claim)."),
        },
        # Definition block agreed verbatim with Perplexity (2026-07-20): one concept,
        # measurement source made explicit, so a proxy is never read as the pass rate.
        "grounding": {
            "instrumented": False,
            "grounding_pass_rate": {
                "definition": "share of answers that carry connector-supplied evidence",
                "formula": "answers carrying connector-supplied evidence / total answers evaluated",
                "source": "assistant-side QA (PARTNER-measured — DC Hub does not see the agent's answer)",
                "value_pct": None,
            },
            "proxy_coverage_rate": {
                "definition": "share of tool results carrying the provenance envelope",
                "formula": "tool results with provenance envelope / total tool results",
                "source": "DC Hub-side instrumentation (pending envelope-coverage capture)",
                "value_pct": None,
            },
            "citation_presence": {
                "definition": "share of answers containing at least one citation",
                "formula": "answers with >=1 citation / total answers evaluated",
                "source": "assistant-side response formatting (PARTNER-measured)",
                "value_pct": None,
            },
            "success_rate_floor_proxy_pct": success_rate,
            "note": ("Primary (grounding_pass_rate) is the user-facing QA metric and is PARTNER-measured; "
                     "DC Hub contributes only proxy_coverage_rate (tool-result envelope coverage). Keeping the "
                     "source explicit avoids reading a platform-side proxy as the answer-quality score. "
                     "success_rate is a floor, not grounding."),
        },
        "planner_adoption": {
            "tool_using_sessions_7d": int(p_sess or 0),
            "planner_first_sessions_7d": int(p_first or 0),
            "planner_first_rate_pct": (round(100.0 * (p_first or 0) / p_sess, 1) if p_sess else None),
            "sessions_using_planner_anywhere_7d": int(p_any or 0),
            "avg_tools_per_workflow": float(p_avg) if p_avg is not None else None,
            "avg_tools_planner_first": float(p_avg_planner) if p_avg_planner is not None else None,
            "avg_tools_direct": float(p_avg_direct) if p_avg_direct is not None else None,
            "note": ("front door shipped 2026-07-20 — plan_query is now the recommended first call in the "
                     "server instructions, its tool description, and llms.txt. KPI (ChatGPT): planner_first_rate "
                     "should climb, and avg_tools_planner_first should fall BELOW avg_tools_direct as the planner "
                     "picks shorter paths. Early cuts are the BEFORE baseline. A workflow = one session_id."),
        },
        "reach_by_platform_7d": [{"platform": r[0], "reach": int(r[1] or 0)} for r in reach_by],
        "real_tooluse_by_platform_7d": [
            {"platform": r[0], "agents": int(r[1] or 0), "calls": int(r[2] or 0)} for r in tooluse_by],
        "what_changed": what_changed,
    }

    if platform:
        p = platform.strip().lower()
        p_reach = _one(c, "SELECT COALESCE(SUM(request_count),0) FROM ai_daily_stats "
                          "WHERE date >= CURRENT_DATE - 7 AND lower(platform) = '"
                          + p.replace("'", "''") + "'")
        p_tool = _one(c, "SELECT count(DISTINCT agent_id), count(*) FROM mcp_calls_identity "
                         "WHERE is_public_ip AND is_real_external "
                         "AND created_at >= now() - interval '7 days' "
                         "AND (" + pcase + ") = '" + p.replace("'", "''") + "'")
        memo["platform_focus"] = {
            "platform": p,
            "reach_7d": int(p_reach[0]) if p_reach and p_reach[0] is not None else 0,
            "real_agents_7d": int(p_tool[0]) if p_tool and p_tool[0] is not None else 0,
            "real_calls_7d": int(p_tool[1]) if p_tool and p_tool[1] is not None else 0,
            "note": ("if real tool-use is 0 while reach is high, the platform is citing/crawling us but "
                     "not carrying end-user MCP tool-use yet — the connector, not the CTA, is the unlock"),
        }
    return memo


# ── markdown renderer (partner-shareable) ────────────────────────────────────

def _pct(v) -> str:
    return f"{v:+.1f}% WoW" if isinstance(v, (int, float)) else "n/a WoW"


def _to_md(m: dict) -> str:
    h = m["headline"]
    L = ["# DC Hub — Weekly Growth Memo", "", f"_Window: {m['window']} · live_", "",
         "## The three numbers (kept separate on purpose)", "",
         f"- **Reach (external, crawler/citation):** {h['reach_external_7d']} ({_pct(h['reach_external_wow_pct'])})",
         f"- **Real agentic tool-use:** {h['real_agents_7d']} distinct agents / {h['real_calls_7d']} calls "
         f"({_pct(h['real_agents_wow_pct'])} agents, {_pct(h['real_calls_wow_pct'])} calls)",
         f"- _{h['reach_to_tooluse_note']}_", ""]
    a = m["attribution_gap"]
    L += ["## Attribution gap (of real tool-use)",
          f"- Named platform: **{a['named_agents_7d']}** agents / {a['named_calls_7d']} calls",
          f"- Generic `mcp` bucket: **{a['generic_mcp_agents_7d']}** agents / {a['generic_mcp_calls_7d']} calls",
          f"- _{a['note']}_", ""]
    f7, fp = m["funnel"]["this_week"], m["funnel"]["prior_week"]
    L += ["## Funnel (relay → identity → paid)",
          f"- paywall {f7['paywall']} → high-intent {f7['high_intent']} → relay-minted {f7['relay_minted']} "
          f"→ emails {f7['emails_captured']} → **paid {f7['conversions']}**  (prior wk paid {fp['conversions']})",
          f"- _{m['funnel']['note']}_", ""]
    g = m["grounding"]
    gp, pc, cp = g["grounding_pass_rate"], g["proxy_coverage_rate"], g["citation_presence"]
    L += ["## Grounding (agreed definition block)",
          f"- **Grounding pass rate (primary):** {gp['formula']} — _{gp['source']}_ → {gp['value_pct']}",
          f"- **Proxy coverage rate:** {pc['formula']} — _{pc['source']}_ → {pc['value_pct']}",
          f"- **Citation presence (secondary):** {cp['formula']} — _{cp['source']}_ → {cp['value_pct']}",
          f"- success-rate floor proxy: {g['success_rate_floor_proxy_pct']}%",
          f"- _{g['note']}_", ""]
    pa = m["planner_adoption"]
    L += ["## Planner adoption (front-door KPI)",
          f"- **planner-first rate:** {pa['planner_first_rate_pct']}% ({pa['planner_first_sessions_7d']} of "
          f"{pa['tool_using_sessions_7d']} tool-using sessions started with plan_query)",
          f"- sessions using plan_query anywhere: {pa['sessions_using_planner_anywhere_7d']}",
          f"- avg tools / workflow: {pa['avg_tools_per_workflow']} "
          f"(planner-first {pa['avg_tools_planner_first']} vs direct {pa['avg_tools_direct']})",
          f"- _{pa['note']}_", ""]
    L += ["## Reach by platform (7d)", "", "| platform | reach |", "|---|---|"]
    L += [f"| {r['platform']} | {r['reach']} |" for r in m["reach_by_platform_7d"]]
    L += ["", "## Real tool-use by platform (7d)", "", "| platform | agents | calls |", "|---|---|---|"]
    L += [f"| {r['platform']} | {r['agents']} | {r['calls']} |" for r in m["real_tooluse_by_platform_7d"]]
    if m.get("platform_focus"):
        pf = m["platform_focus"]
        L += ["", f"## Focus: {pf['platform']}",
              f"- reach 7d: {pf['reach_7d']} · real tool-use 7d: {pf['real_agents_7d']} agents / {pf['real_calls_7d']} calls",
              f"- _{pf['note']}_"]
    L += ["", f"**What changed:** {m['what_changed']}", "",
          "_Data: DC Hub (dchub.cloud). Reach = ai_daily_stats (UA-based); real tool-use = mcp_calls_identity "
          "(distinct agents, Cloudflare-POP-corrected); funnel = handoff instrumentation._"]
    return "\n".join(L)


# ── route ────────────────────────────────────────────────────────────────────

@growth_memo_bp.route("/api/v1/admin/growth-memo", methods=["GET"])
def growth_memo():
    if not _admin_ok():
        return jsonify(ok=False, error="forbidden — X-Admin-Key or ?admin_key="), 403
    c = _conn()
    if c is None:
        return jsonify(ok=False, error="no db"), 503
    try:
        platform = (request.args.get("platform") or "").strip() or None
        memo = _build_memo(c, platform)
    finally:
        try:
            c.close()
        except Exception:
            pass
    if (request.args.get("format") or "").lower() in ("md", "markdown"):
        return Response(_to_md(memo), content_type="text/markdown; charset=utf-8")
    return jsonify(memo)
