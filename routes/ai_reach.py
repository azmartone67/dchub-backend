"""DC Hub — honest AI-platform REACH (r86-reach, 2026-06-14).

The /ai "AI Wars" page headlines cumulative request COUNTS (866K, Claude 102K) that are
really ~22-25 distinct IPs per platform looping + internal traffic. This endpoint returns
the HONEST reach: DISTINCT public IPs per platform over the recent window — the real number
of agent sources, not loop-inflated volume. Robust (doesn't depend on fragile internal/
external UA tagging) and cached (the agent_requests scan is heavy).

Standalone file + own blueprint so the concurrent backend refactors can't revert it.
Register in main.py:  from routes.ai_reach import ai_reach_bp; app.register_blueprint(ai_reach_bp)

  GET /api/v1/ai/reach   -> { distinct_agents_7d, distinct_platforms, per_platform:[...], requests_7d, note }
"""
from __future__ import annotations
import os, time, json
from flask import Blueprint, jsonify
import psycopg2, psycopg2.extras

ai_reach_bp = Blueprint("ai_reach_r86", __name__)

# private/loopback ranges = definitely internal; public IPs = real external reach.
# 100.64.0.0/10 = CGNAT (RFC 6598) = Railway's INTERNAL proxy fleet — a server only
# sees a 100.64.x source via an internal proxy, never a real client. agent_requests
# recorded ONLY these (no X-Forwarded-For), which is why its "reach" was ~16 proxy
# nodes; reach now sources from mcp_tool_calls (real client IPs). Keep CGNAT excluded
# everywhere as defense-in-depth.
_PRIVATE_IP = r"^(10\.|127\.|192\.168\.|172\.(1[6-9]|2[0-9]|3[01])\.|169\.254\.|100\.(6[4-9]|[7-9][0-9]|1[01][0-9]|12[0-7])\.|::1|fc|fd|0\.0\.0\.0|$)"
_INTERNAL_PLAT = ('internal', 'mcp_generic', 'direct', 'unknown', 'unknown_ai', 'mcp', 'Unknown', '')
_cache = {"ts": 0.0, "data": None}
_TTL = 1800  # 30 min — the scan is heavy; stale-on-error below covers the cold-refresh window


def _conn():
    db = os.environ.get("DATABASE_URL")
    if not db:
        return None
    try:
        c = psycopg2.connect(db, sslmode="require", connect_timeout=5)
        c.autocommit = True
        return c
    except Exception:
        return None


def _attach_canonical_7d(cur, out):
    """r-agent-parity (2026-07-31): publish THE canonical rolling-7d agent
    count alongside the weekly rollup. distinct_agents_7d (despite the name)
    serves the reach_weekly ISO-WEEK rollup — max of the last 2 weeks so a
    partial week never dips it — which is a different window than the rolling
    7d the /ai tool-use widget and /api/v1/mcp/funnel report. The /ai header
    badge rendered it as "this week"; readers conflated the two. real_agents_7d
    / real_calls_7d below are the same single-sourced query those surfaces run
    (mcp_calls_deloop.canonical_external_activity_sql), so every display that
    says "(7d)" can bind here and agree. Fail-soft: fields stay absent on
    error, the endpoint still serves. Query is cheap (mcp_calls_identity is a
    view over mcp_tool_calls, small + created_at-indexed)."""
    try:
        from mcp_calls_deloop import (canonical_external_activity_sql,
                                      CANONICAL_AGENTS_BASIS)
        cur.execute(canonical_external_activity_sql(7))
        row = cur.fetchone() or {}
        out["real_agents_7d"] = int(row.get("agents") or 0)
        out["real_calls_7d"] = int(row.get("calls") or 0)
        out["real_agents_7d_basis"] = (
            CANONICAL_AGENTS_BASIS
            + " Rolling 7d — THE canonical figure for any '(7d)' display. "
              "distinct_agents_7d in this payload is the ISO-week rollup "
              "(max of last 2 weeks), kept for the WoW trend; label it "
              "weekly, never '(7d)'.")
    except Exception:
        pass


@ai_reach_bp.route("/api/v1/ai/reach", methods=["GET"])
def ai_reach():
    now = time.time()
    if _cache["data"] is not None and (now - _cache["ts"]) < _TTL:
        return jsonify(_cache["data"]), 200
    out = {"distinct_agents_7d": 0, "distinct_platforms": 0, "per_platform": [], "requests_7d": 0,
           "window": "~recent (id-bounded ≈7d)",
           "note": "Honest reach = DISTINCT public IPs per platform (real agent sources), not cumulative request volume. The big 'requests served' counts are real traffic but loop-inflated; this is the addressable reach.",
           "basis": ("distinct_agents_7d counts DISTINCT non-Cloudflare public client IPs "
                     "that pass the canonical is_real_external de-loop. SAME identity basis "
                     "as /api/v1/stats/live-proof.distinct_callers_7d but NOT the same "
                     "WINDOW: the fast path reads the reach_weekly ISO-week rollup (max of "
                     "the last 2 weeks), so mid-week it reports a partial week and runs "
                     "BELOW the trailing-7d canonical count — 64 vs 95 on 2026-07-31. For "
                     "the honest trailing-7d agent count use real_agents_7d in THIS "
                     "payload (or /api/v1/mcp/funnel.real_external_agents_7d — same "
                     "single-sourced query). It is an IP-derived PROXY for agents, not a true "
                     "agent identity: agent_id is md5(client_ip), so several agents behind "
                     "one NAT count once, and one agent on a rotating proxy counts many "
                     "times. Treat it as distinct calling SOURCES."),
           "window_basis": "ISO calendar week (reach_weekly rollup), NOT trailing 7d"}
    # fail-soft (2026-06-14): this is a PUBLIC DISPLAY endpoint for the /ai page —
    # it must NEVER return 5xx. A 5xx throws an F12 console error and can blank the
    # reach lines (caught live: a cold replica whose first request hit the 9s scan
    # timeout with an empty in-memory cache returned 500). On ANY failure: serve
    # last-good cache, else the valid empty skeleton at 200 with degraded=true so the
    # page renders gracefully and quietly retries on the next poll.
    def _soft():
        if _cache["data"] is not None:
            stale = dict(_cache["data"]); stale["stale"] = True
            return jsonify(stale), 200
        out["degraded"] = True
        return jsonify(out), 200
    c = _conn()
    if c is None:
        return _soft()
    try:
        with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            # ── FAST PATH (r-reach-rollup 2026-06-22): read the precomputed weekly
            # rollup (reach_weekly, built by routes/ai_reach_rollup.run_reach_rollup
            # on the daily cron). O(2-row PK read), NO agent_requests scan — so this
            # endpoint never times out and never exhausts the 1-replica pool when the
            # Leadership/Utilization engines self-call it concurrently (the documented
            # anti-pattern that froze those dashboards at 0.0). MAX over the last 2
            # weeks so the current (partial) ISO week never dips the metric to a
            # Monday-morning low. Falls through to a single capped live scan only if
            # the rollup table is empty (cold start) — same _PRIVATE_IP / _INTERNAL_PLAT
            # filters, so the two sources can never drift.
            rolled = None
            try:
                cur.execute("SET statement_timeout = '4000'")
                cur.execute("""
                    SELECT week_start, distinct_external_ips, distinct_platforms,
                           requests, per_platform
                    FROM reach_weekly ORDER BY week_start DESC LIMIT 2
                """)
                rolled = [dict(r) for r in cur.fetchall()] or None
            except Exception:
                rolled = None   # table missing / cold → live-scan fallback below

            # Bound before the branch: BOTH the rollup path and the cold-start
            # fallback below need it, and binding it inside `if rolled:` would
            # NameError on exactly the cold replica the fallback exists for.
            try:
                from ai_platform_canon import count_platforms
            except Exception:
                count_platforms = None

            if rolled:
                agents = max(int(r.get("distinct_external_ips") or 0) for r in rolled)
                reqs   = max(int(r.get("requests") or 0) for r in rolled)
                # ★2026-08-05 A COUNT THAT DISAGREED WITH THE LIST BESIDE IT.
                # distinct_platforms was max(distinct_platforms) across BOTH
                # rollup weeks while per_platform came from rolled[0] — one
                # week's list published under another week's count. Live
                # 2026-08-05 that read distinct_platforms=3 next to a
                # per_platform of FIVE entries, in the same JSON object, with
                # nothing saying they were different questions.
                #
                # Two things are true and both are now said: 5 is the number of
                # distinct CLIENT IDs, 3 is the number of canonical VENDORS
                # (anthropic/claudeai + claude-code + claude are one vendor —
                # the collapse ai_platform_canon exists to perform). Fixed by
                # (a) taking the count and the list from ONE row, and (b)
                # deriving the count from the list actually published, so a
                # reader can reproduce it from what they can see.
                def _ids(row):
                    pp = row.get("per_platform") or []
                    if isinstance(pp, str):
                        try: pp = json.loads(pp)
                        except Exception: pp = []
                    return pp if isinstance(pp, list) else []

                def _vendors(row):
                    if count_platforms is None:
                        return int(row.get("distinct_platforms") or 0)
                    return count_platforms(
                        d.get("platform_id") for d in _ids(row) if isinstance(d, dict))

                # Keep the "max of last 2 weeks" intent — a Monday-morning
                # partial week must not dip the metric — but pick the winning
                # ROW, never a scalar detached from the list it came from.
                best = max(rolled, key=_vendors)
                pp = _ids(best)
                out["distinct_agents_7d"] = agents
                # Assigned from the counter directly, not via an intermediate:
                # the published key must be traceable to the canonical count at
                # the line that publishes it.
                out["distinct_platforms"] = _vendors(best)
                out["per_platform"] = pp
                out["per_platform_client_ids"] = len(pp)
                out["distinct_platforms_basis"] = (
                    "distinct_platforms counts canonical VENDORS "
                    "(ai_platform_canon.count_platforms over the per_platform "
                    "list published in THIS payload): unrecognized ids (the "
                    "`mcp` protocol bucket, connectors-manager, test "
                    "harnesses) are not platforms, and claude / claude-code / "
                    "anthropic-* collapse to one vendor. per_platform_client_ids "
                    "is the raw row count of that same list. The two differ by "
                    "construction and are NOT two answers to one question — "
                    "recompute either from per_platform[] to check."
                )
                out["requests_7d"] = reqs
                out["window"] = ("iso_week rollup, max of last 2 weeks "
                                 "(reach_weekly · precomputed daily) — NOT "
                                 "rolling 7d; see real_agents_7d")
                out["source"] = "rollup"
                _attach_canonical_7d(cur, out)
                _cache["data"] = out
                _cache["ts"] = now
                return jsonify(out), 200

            # ── COLD-START FALLBACK (rollup empty): live query over mcp_tool_calls ──
            # r-reach-mcp-source (2026-06-24): mcp_tool_calls captures REAL public client
            # IPs (agent_requests only ever had CGNAT proxy IPs), is small + created_at-
            # indexed → a 7d live query is fast. Inlined predicate (no bound params) so
            # the literal % in PLATFORM_CASE's ILIKE patterns are left alone; reuse the
            # canonical de-loop (real_calls_predicate) so this == the funnel's real reach.
            cur.execute("SET statement_timeout = '8000'")
            from mcp_calls_deloop import PLATFORM_CASE as _PC, real_calls_predicate as _rcp
            _w = ("created_at >= NOW() - INTERVAL '7 days' "
                  "AND ip_address IS NOT NULL AND ip_address <> '' "
                  "AND ip_address !~ '" + _PRIVATE_IP + "' "
                  "AND (" + _rcp() + ")")
            cur.execute(
                "SELECT (" + _PC.strip() + ") AS platform_id, "
                "       COUNT(DISTINCT ip_address) AS agents, COUNT(*) AS requests "
                "FROM mcp_tool_calls WHERE " + _w +
                " GROUP BY 1 HAVING COUNT(DISTINCT ip_address) >= 1 "
                " ORDER BY agents DESC, requests DESC LIMIT 25")
            rows = [dict(r) for r in cur.fetchall()]
            out["per_platform"] = rows
            # Same rule as the rollup path above — and this branch was still
            # publishing len(rows), the raw distinct-string count the 07-27
            # canon fix removed from the hot path only. A cold replica served
            # the inflated number.
            out["distinct_platforms"] = (
                count_platforms(r.get("platform_id") for r in rows)
                if count_platforms is not None else len(rows))
            out["per_platform_client_ids"] = len(rows)
            out["distinct_platforms_basis"] = (
                "distinct_platforms counts canonical VENDORS "
                "(ai_platform_canon.count_platforms over the per_platform "
                "list published in THIS payload); per_platform_client_ids is "
                "the raw row count of that same list. They differ by "
                "construction — recompute either from per_platform[] to check.")
            # overall distinct external agents (an IP can span platforms — count once)
            cur.execute(
                "SELECT COUNT(DISTINCT ip_address) AS agents, COUNT(*) AS reqs "
                "FROM mcp_tool_calls WHERE " + _w)
            tot = cur.fetchone() or {}
            _legacy_agents = int(tot.get("agents") or 0)
            out["requests_7d"] = int(tot.get("reqs") or 0)
            out["distinct_agents_7d"] = _legacy_agents
            out["source"] = "live_scan_mcp_tool_calls"
            # ── #49 lane 4: count at the CANONICAL grain ──────────────
            # COUNT(DISTINCT ip_address) keys on the WHOLE X-Forwarded-For
            # string, so one agent behind two Cloudflare POPs is two agents,
            # and a POP itself is an agent. mcp_calls_identity.agent_id is the
            # view that already fixes both (first hop only, POP first-hops
            # NULLed) and that flywheel + monetization already count — this
            # endpoint reading raw IPs is the measured 62-vs-99 gap, not two
            # defensible methods.
            # Fail-soft on its own: if the view is absent the legacy number
            # stands and `agent_grain` says so, rather than the whole endpoint
            # degrading over a metric refinement.
            try:
                cur.execute(
                    "SELECT COUNT(DISTINCT agent_id) AS agents "
                    "FROM mcp_calls_identity "
                    "WHERE created_at >= NOW() - INTERVAL '7 days' "
                    "  AND agent_id IS NOT NULL AND agent_id <> '' "
                    "  AND is_public_ip AND is_real_external")
                _canon = (cur.fetchone() or {}).get("agents")
                if _canon is not None:
                    out["distinct_agents_7d"] = int(_canon)
                    out["distinct_ips_7d_legacy"] = _legacy_agents
                    out["agent_grain"] = "mcp_calls_identity.agent_id"
                    out["source"] = "live_scan_mcp_calls_identity"
            except Exception:
                out["agent_grain"] = ("raw_ip_address — mcp_calls_identity "
                                      "unavailable; this number counts "
                                      "Cloudflare POPs as agents")
                # ★The connection is `c` here, not `conn`. This matters beyond
                # tidiness: a failed SELECT aborts the transaction, so without
                # a real rollback the _attach_canonical_7d() call below fails
                # too and a metric refinement takes out the rest of the
                # payload. A NameError would have been swallowed silently.
                try:
                    c.rollback()
                except Exception:
                    pass
            _attach_canonical_7d(cur, out)
    except Exception:
        return _soft()   # fail-soft: never 5xx (last-good cache, else degraded 200)
    finally:
        try: c.close()
        except Exception: pass
    _cache["data"] = out
    _cache["ts"] = now
    return jsonify(out), 200
