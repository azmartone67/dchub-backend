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
import datetime as _dt
from flask import Blueprint, jsonify, request
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


def _as_date(v):
    """reach_weekly.week_start as a date, whatever psycopg2 handed back."""
    if isinstance(v, _dt.date) and not isinstance(v, _dt.datetime):
        return v
    if isinstance(v, _dt.datetime):
        return v.date()
    try:
        return _dt.date.fromisoformat(str(v))
    except (TypeError, ValueError):
        return None


def _latest_complete(rolled: list[dict]) -> dict:
    """The newest row that is NOT the in-progress ISO week.

    `rolled` arrives ORDER BY week_start DESC LIMIT 2, so the first row that
    starts before this week's Monday is the latest complete week.

    ★ Falls back to rolled[0] when every row is the current week — a brand-new
    rollup table on a Monday has exactly one, partial, row. Publishing a
    partial week is the lesser fault; publishing nothing 500s an endpoint whose
    whole contract is fail-soft. The `partial` flag says which one a reader got
    rather than leaving them to infer it.
    """
    today = _dt.date.today()
    monday = today - _dt.timedelta(days=today.weekday())
    for r in rolled:
        d = _as_date(r.get("week_start"))
        if d is not None and d < monday:
            return r
    return rolled[0]


def _mark_superseded(out: dict, week_start) -> None:
    """Flag a published week that a measurement CORRECTION has since withdrawn.

    Reuses routes.weekly_series._superseded_by so the correction registry has
    exactly one home. A second copy of these timestamps here is the twin-drift
    this codebase keeps paying for — the registry is data, and data gets
    imported, not restated.

    Imported lazily and fail-soft: a marker is metadata about honesty, and if
    it cannot be computed the right failure is to lose the marker, not the
    endpoint. Absent keys read exactly as they did before this existed.
    """
    d = _as_date(week_start)
    if d is None:
        return
    try:
        from routes.weekly_series import _superseded_by
        sup = _superseded_by([d.isoformat()])
    except Exception:
        return
    if not sup:
        return
    out["superseded_by_correction"] = True
    out["superseded_by"] = sup
    out["superseded_note"] = (
        f"the {d.isoformat()} week published here was measured BEFORE the "
        "correction(s) in superseded_by[] took effect, so distinct_agents_7d "
        "and requests_7d count a population that has since been withdrawn. "
        "For a count on the current definition read real_agents_7d in this "
        "same payload."
    )


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

    # ★2026-08-25 (r-net-of-top on /ai/reach). The /ai header badge reads THIS
    # endpoint and renders "N distinct external agents called MCP tools" with
    # no concentration context, while the net-of-top-caller figure lives only
    # on /api/v1/mcp/funnel. Measured this evening: one caller (Smithery
    # Connect, a registry GATEWAY that fronts us rather than indexing us) was
    # 93.6% of 1,982 calls, so the badge headline tracked one proxy's mood and
    # said nothing about it. Publishing it HERE rather than making the badge
    # fetch a second endpoint is deliberate: this file's own history is a run
    # of defects where two surfaces read two sources and printed two different
    # agent counts on one page. Same window, same predicates, ONE query.
    try:
        from mcp_calls_deloop import canonical_top_caller_sql
        cur.execute(canonical_top_caller_sql(7))
        tc = cur.fetchone() or {}
        _top = int(tc.get("top_calls") or 0)
        _all = int(tc.get("calls") or 0)
        if _all > 0:
            _pct = round(100.0 * _top / _all, 1)
            out["top_caller_calls_7d"] = _top
            out["top_caller_pct_7d"] = _pct
            out["real_calls_net_of_top_7d"] = int(tc.get("calls_net_of_top") or 0)
            out["real_agents_net_of_top_7d"] = int(tc.get("callers_net_of_top") or 0)
            # A share this high means the headline describes ONE caller. The
            # flag is published so a renderer does not have to pick its own
            # threshold and quietly disagree with the admin lane.
            out["concentration_flag_7d"] = bool(_pct >= 25.0)
            out["net_of_top_basis"] = (
                "Same query, window and exclusions as real_calls_7d "
                "(mcp_calls_identity WHERE is_public_ip AND is_real_external, "
                "rolling 7d), so real_calls_7d == top_caller_calls_7d + "
                "real_calls_net_of_top_7d holds by construction and the two "
                "cannot drift. The subtracted caller is NOT excluded from any "
                "other field — it is a companion to the headline, not a "
                "replacement, and a gateway is not automatically a non-agent. "
                "Read the client name before calling this customer "
                "concentration. IP-derived PROXY: NAT under-counts, rotating "
                "egress over-counts.")
    except Exception:
        pass


# ── ?period= — REAL windows, 2026-08-09 ────────────────────────────────────
# ★ WHY THIS EXISTS. Until today this endpoint took NO query parameters and
# read none: `?period=7d`, `?period=30d`, `?period=all` and no param at all
# returned BYTE-IDENTICAL JSON (measured live 2026-08-09: md5 identical across
# all three named periods). Every field was hardcoded 7-day — distinct_agents_7d,
# real_agents_7d, real_calls_7d, per_platform — so a caller asking for 30 days
# got 7 days of numbers under keys that SAY 7d, with a 200 and no warning. That
# is the worst kind of contract failure: it does not look like one.
#
# The fix is real windows, not a friendlier error, because the data supports
# them honestly: mcp_calls_identity is the same view the canonical 7d count
# already reads, it is created_at-indexed, and it retains months (reach_weekly
# has rows from 2026-04-20). So 30d and all-time are the SAME question over a
# longer span — no scaling, no extrapolation, no 7d number multiplied by four.
#
# Two rules this code must never break:
#  1. Every window publishes its OWN `basis` naming what it counted and over
#     what span, plus machine-readable window_start/window_end.
#  2. A non-7d response carries NO `*_7d` keys. Emitting real_agents_7d in a
#     30d payload would recreate the exact lie this fix removes, one level
#     deeper. The 7d keys exist ONLY in the 7d payload.
# `all` is bounded by RETENTION, not by the beginning of time — so it publishes
# the observed MIN(created_at) and says so, rather than implying since-launch.
_SUPPORTED_PERIODS = ("7d", "30d", "all")
_wcache: dict = {}          # period -> {"ts": float, "data": dict}
_WTTL = 1800


def _window_reach(period: str):
    """Compute reach over a REAL window at the canonical agent grain.

    Same population as canonical_external_activity_sql (mcp_calls_identity,
    is_public_ip AND is_real_external) so this cannot drift from the 7d
    figure — only the span differs. Fail-soft like the 7d path: never 5xx."""
    now = time.time()
    hit = _wcache.get(period)
    if hit and (now - hit["ts"]) < _WTTL:
        return jsonify(hit["data"]), 200

    days = None if period == "all" else int(period[:-1])
    out = {
        "period": period,
        "window_days": days,
        "per_platform": [], "distinct_platforms": 0,
        "note": ("Honest reach = DISTINCT agents (canonical identity grain) per "
                 "platform over the requested window, not cumulative request "
                 "volume. Counted over the SAME population as the 7d figure; "
                 "only the span differs — nothing here is scaled or projected "
                 "from a shorter window."),
    }
    c = _conn()
    if c is None:
        out["degraded"] = True
        out["basis"] = "could not connect to the database — no window was computed"
        return jsonify(out), 200
    try:
        with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            # NO bound params anywhere below: PLATFORM_CASE carries literal %
            # in its ILIKE patterns and psycopg2 would try to interpolate them.
            cur.execute("SET statement_timeout = '%d'" % (12000 if days is None else 8000))
            from mcp_calls_deloop import PLATFORM_CASE as _PC, CANONICAL_AGENTS_BASIS
            try:
                from ai_platform_canon import count_platforms
            except Exception:
                count_platforms = None

            where = "is_public_ip AND is_real_external"
            if days is not None:
                where += " AND created_at >= now() - interval '%d days'" % days

            cur.execute(
                "SELECT COUNT(DISTINCT agent_id) AS agents, COUNT(*) AS calls, "
                "       MIN(created_at) AS first_seen, MAX(created_at) AS last_seen "
                "FROM mcp_calls_identity WHERE " + where)
            tot = cur.fetchone() or {}
            first_seen, last_seen = tot.get("first_seen"), tot.get("last_seen")
            out["real_agents"] = int(tot.get("agents") or 0)
            out["real_calls"] = int(tot.get("calls") or 0)
            out["window_start"] = first_seen.isoformat() if first_seen else None
            out["window_end"] = last_seen.isoformat() if last_seen else None

            cur.execute(
                "SELECT (" + _PC.strip() + ") AS platform_id, "
                "       COUNT(DISTINCT agent_id) AS agents, COUNT(*) AS requests "
                "FROM mcp_calls_identity WHERE " + where +
                " GROUP BY 1 HAVING COUNT(DISTINCT agent_id) >= 1 "
                " ORDER BY agents DESC, requests DESC LIMIT 25")
            rows = [dict(r) for r in cur.fetchall()]
            out["per_platform"] = rows
            out["per_platform_client_ids"] = len(rows)
            out["distinct_platforms"] = (
                count_platforms(r.get("platform_id") for r in rows)
                if count_platforms is not None else len(rows))
            out["source"] = "live_scan_mcp_calls_identity"

            if days is not None:
                span = ("rolling %d days ending now (created_at >= now() - "
                        "interval '%d days')" % (days, days))
            else:
                span = ("ALL retained history — every row in mcp_calls_identity, "
                        "with NO lower time bound. 'All time' here means since "
                        + (out["window_start"] or "the earliest retained row")
                        + ", which is the oldest row the table still holds, NOT "
                          "since DC Hub launched: anything aged out of "
                          "mcp_tool_calls is not counted and cannot be.")
            out["basis"] = (
                CANONICAL_AGENTS_BASIS + " WINDOW FOR THIS RESPONSE: " + span +
                ". real_agents = COUNT(DISTINCT agent_id) and real_calls = "
                "COUNT(*) over exactly that span; per_platform breaks the same "
                "population down by the canonical PLATFORM_CASE classifier at "
                "the same agent grain, so the platform rows re-sum to this "
                "population (agents do NOT sum — one agent can appear under "
                "two platforms and is counted once in real_agents). "
                "window_start/window_end are the OBSERVED MIN/MAX created_at in "
                "this window, not the nominal bounds. This payload deliberately "
                "carries NO *_7d keys: those exist only in the 7d response.")
            out["distinct_platforms_basis"] = (
                "distinct_platforms counts canonical VENDORS "
                "(ai_platform_canon.count_platforms over the per_platform list "
                "published in THIS payload); per_platform_client_ids is the raw "
                "row count of that same list. They differ by construction — "
                "recompute either from per_platform[] to check.")
    except Exception as e:
        out["degraded"] = True
        out["basis"] = ("window could not be computed (%s) — this response "
                        "counts NOTHING; do not read the zeros as a real "
                        "measurement" % type(e).__name__)
        return jsonify(out), 200
    finally:
        try: c.close()
        except Exception: pass
    _wcache[period] = {"ts": now, "data": out}
    return jsonify(out), 200


@ai_reach_bp.route("/api/v1/ai/reach", methods=["GET"])
def ai_reach():
    # Unknown period is a CLIENT error and says so, instead of silently
    # serving 7d under the caller's label (the old behaviour for every value).
    period = (request.args.get("period") or "7d").strip().lower()
    if period in ("", "7", "week", "7day", "7days"):
        period = "7d"
    if period == "alltime":
        period = "all"
    if period not in _SUPPORTED_PERIODS:
        return jsonify({
            "error": "unsupported period",
            "requested": period,
            "supported_periods": list(_SUPPORTED_PERIODS),
            "note": ("This endpoint used to ACCEPT any period and return 7-day "
                     "numbers regardless. It now refuses rather than mislabel."),
        }), 400
    if period != "7d":
        return _window_reach(period)

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
           "window_basis": "ISO calendar week (reach_weekly rollup), NOT trailing 7d",
           "period": "7d",
           "supported_periods": list(_SUPPORTED_PERIODS),
           "period_basis": ("?period=7d (default) serves THIS payload — the "
                            "reach_weekly rollup fast path plus the canonical "
                            "rolling-7d real_agents_7d/real_calls_7d. "
                            "?period=30d and ?period=all are computed live "
                            "over mcp_calls_identity and return window-neutral "
                            "keys (real_agents / real_calls / window_start / "
                            "window_end) — deliberately NOT *_7d, so a longer "
                            "window can never be read as a 7-day number. An "
                            "unrecognised period returns 400, not this payload.")}
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
                # ★2026-08-20 A METRIC THAT COULD NOT GO DOWN.
                # This was max(distinct_external_ips) across both rollup weeks,
                # to stop a Monday-morning partial week dipping the number. The
                # intent is right; max() is the wrong instrument for it. max()
                # does not just ignore a partial week — it ignores EVERY
                # decline, because the metric latches to whichever of the two
                # weeks is higher and stays there until that week ages out.
                #
                # Live 2026-08-19 that published 72: the week of 2026-08-10,
                # measured BEFORE dchub-mcp-server#202 (2026-08-18 06:31Z)
                # removed DC Hub's own GitHub Actions from is_real_external —
                # 72.1% of agents in the 7d before it. The same payload's
                # canonical real_agents_7d read 47 and the post-correction week
                # read 12. A public endpoint was serving the count the
                # correction withdrew, and by construction could not stop.
                #
                # Fix: name the week we mean — the latest COMPLETE one — and
                # take EVERY published field from that single row. That also
                # closes what the 2026-08-05 fix below left open: it aligned
                # distinct_platforms with per_platform but left `agents` on its
                # own max(), so the headline count could still come from one
                # week while the list beside it came from the other.
                _pick = _latest_complete(rolled)
                agents = int(_pick.get("distinct_external_ips") or 0)
                reqs   = int(_pick.get("requests") or 0)
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

                # ONE row for the count and the list both — see _latest_complete
                # above. `best` used to be a second, independent max() over the
                # same two weeks.
                best = _pick
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
                # Name the week the number is FOR. Without it "max of last 2
                # weeks" left a reader unable to tell WHICH week they were
                # holding, so a superseded one was indistinguishable from a
                # current one by inspection.
                out["rollup_week_start"] = str(_pick.get("week_start") or "")
                out["window"] = ("iso_week rollup — the latest COMPLETE week "
                                 "(reach_weekly · precomputed daily), named in "
                                 "rollup_week_start. NOT rolling 7d; for a "
                                 "trailing-7d count see real_agents_7d")
                out["source"] = "rollup"
                _mark_superseded(out, _pick.get("week_start"))
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
