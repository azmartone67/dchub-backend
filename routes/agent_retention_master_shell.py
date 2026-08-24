"""
routes/agent_retention_master_shell.py — Agent Retention Master Shell (#49, 2026-08-02).

Discovery is won and expansion is instrumented (#45). This board holds the
thing neither of those measures: whether an agent that arrives ever COMES
BACK. The dashboard has said since 2026-06-14 that retention is the lever —
week of 07-27 it was 246 new external IPs against 51 returning, key reuse
43.9% and falling from a 48.7% peak — and nothing watched it as a board.

Six lanes, from the 2026-08-02 improvement review:

  1. RETENTION — new vs returning external agents, 7d against the prior 7d,
     on the canonical identity basis. The number the whole board exists for.
  2. RETURN MECHANISM — the return path is save_site -> set_*_alert ->
     get_changes. ★ It is fully OFFERED: a keyed answer carries
     structuredContent.next_session naming save_site / set_site_alert /
     set_market_alert / subscribe_digest, led by the come_back strategy
     (an email is what re-summons a stateless agent), plus the _return_loop
     get_changes line. Verified live 2026-08-02 on analyze_site and
     rank_markets. (An earlier read of this lane claimed the tools were never
     named — that came from an ANONYMOUS probe grepping only text content;
     the block is keyed-only by design and lives in structuredContent. The
     claim was wrong and is corrected here so nobody rebuilds a nudge that
     already ships.) What was genuinely broken is now fixed too: all eleven
     saved-work tools were invisible to discover_tools until mcp-server #125
     (2026-08-02). So the offer exists and the tools are discoverable — what
     remains unknown is ADOPTION, which is the only thing this lane measures.
     BORN RED until >0.
  3. ERROR DEAD ENDS — a partner published "errors are actionable, not
     terminal" as a standing default on 08-02. Measured the same day, 2 of 5
     error paths carried no `_error_mitigation` at all (upstream passthrough:
     API 404, invalid parameter). An agent that hits one has no next move and
     the session ends — retention leaking through the error path.
  4. COUNT PARITY — /ai showed "≈95 DISTINCT AGENTS" beside "84" for the same
     quantity on 08-02, because two endpoints with different cache behaviour
     each call themselves canonical. This lane compares the canonical rolling
     count against the reach_weekly rollup that fed the other number.
  5. CONCENTRATION — one caller was 26.2% of external tool calls and Meta was
     86% of crawler 7d. An aggregate carried by one source reads as growth on
     the way up and collapse on the way down, and is neither.
  6. CRAWLER SILENCE — Copilot (a bucket matched on BingBot) went silent
     2026-07-28 and nothing alarmed. A named platform with real 30d volume
     going quiet is a crawl-rate event worth catching in days, not weeks.

★ HONESTY RULE (Integrity #25 via Ascension #28): a lane must never read PASS
when it couldn't check — an indeterminate critical check is not green.

★ COUNT DISCIPLINE: every AGENT figure comes from the canonical identity
basis (mcp_calls_identity: is_public_ip AND is_real_external) — never raw ip
strings, never session_id. Lane 3 is the ONE deliberate exception and says so
inline: success/status_code live on mcp_connections, so error diagnostics
read that table. It is an error-rate lane, not an agent count.

READ-ONLY / DIAGNOSTIC: every lane names its actuator and fires nothing.
PURE-DB: lanes never make HTTP calls (the 2026-07-06 flywheel-outage
invariant).

NO cron and NO dead-man beat in v1 ON PURPOSE — a scheduled name absent from
_RUNNERS fires zero, silently, and this repo has paid for that twice. A wave 2
registers both together or not at all.

Endpoints:
  GET/POST /api/v1/admin/agent-retention/master-tick   JSON scoreboard
  GET      /admin/agent-retention                       HTML dashboard
  GET      /api/v1/admin/agent-retention                CF zone-worker alias

Auth: same admin gate as the sibling shells (X-Admin-Key / ?admin_key=).
Kill: AGENT_RETENTION_SHELL_DISABLE=1
"""

import datetime as _dt
import os
import re as _re

from flask import Blueprint, Response, jsonify

# Shared shell plumbing — imported, not copied, so the honesty semantics can
# never drift between boards (the transcribed-contract lesson).
from routes.brain_ascension_master_shell import (  # noqa: F401
    _admin_ok, _check, _conn, _lane_verdict, _safe_lane)

agent_retention_master_shell_bp = Blueprint(
    "agent_retention_master_shell", __name__)

# mcp-server #125 made the saved-work family discoverable. Lane 2 judges
# adoption against this date so "no adoption yet" can be read as "too early"
# rather than "broken" for the first week.
DISCOVERY_FIX_DATE = _dt.date(2026, 8, 2)

# The return path, in the order an agent would walk it. Sourced from the
# saved_work family in mcp-server's _TOOL_FAMILIES_TABLE — if that family
# gains a tool, add it here in the same change.
_RETURN_TOOLS = (
    "save_site", "save_to_shortlist", "get_shortlist", "list_saved_sites",
    "set_site_alert", "set_market_alert", "set_shortlist_alert",
    "subscribe_digest", "standing_intent", "export_dataset",
    "suggest_reallocation", "get_changes",
)

# A named platform quiet for longer than this, while carrying real 30d
# volume, is a crawl-rate event (lane 6). Three days clears normal weekend
# and batch-cadence gaps — Copilot's 07-28 stop ran five days unnoticed.
_SILENCE_DAYS = 3
# Lane 5: a single caller or platform above this share makes the aggregate a
# proxy for one source's mood rather than a trend. ★2026-08-24: single-sourced
# in mcp_calls_deloop so this lane and the public funnel card cannot disagree
# about whether the same share is a problem. Fallback keeps the shell running
# if the import ever fails — the lane degrading is worse than a stale constant.
try:
    from mcp_calls_deloop import CONCENTRATION_PCT as _CONCENTRATION_PCT
except Exception:  # pragma: no cover - import-shape guard
    _CONCENTRATION_PCT = 25.0


def _disabled() -> bool:
    return os.environ.get("AGENT_RETENTION_SHELL_DISABLE", "") == "1"


# ── retention arithmetic — ONE definition, and a guard on what it prints ─────
# ★2026-08-05 WRONG DENOMINATOR. Lane 1 published
#   pct = 100.0 * returning / cur_n
# where cur_n is the CURRENT window's agent count. That is not a retention
# rate: it answers "what share of this week's agents are not new", which
# flatters us whenever the fleet SHRINKS, because a smaller current window
# makes the same `returning` count read higher. Measured live 2026-08-05:
# 7 returning, current cohort 48, prior cohort 83 — the board printed 14.6%
# where retention was 8.4%, wrong in our favour by 6.2 points, in the exact
# sentence an operator would quote.
#
# Retention is always measured against the cohort that had the chance to
# return: the PRIOR window. The current count is context, never a denominator.
def _retention_pct(returning: int, prior_cohort: int):
    """Returning agents as a share of the PRIOR window's cohort.

    None when the prior window is empty — with nobody to return, the rate is
    undefined, not 0% and not 100%. Callers publish UNMEASURED for None.
    """
    if not prior_cohort:
        return None
    return 100.0 * returning / prior_cohort


# The guard reads the PUBLISHED SENTENCE back, not the intermediate variable:
# the defect above was an identifier swap that any check on the intermediate
# would have followed straight into the wrong answer. Re-deriving from the
# three numbers actually printed is the only form that catches it.
_RETENTION_CLAIM_RE = _re.compile(
    r"(\d+) of the prior window's (\d+) agents returned = (\d+\.\d)%")


def _retention_claim_ok(detail: str) -> tuple[bool, str]:
    """Recompute the printed percentage from the printed counts.

    Returns (ok, why). Tolerance is half of the last published digit — the
    sentence prints one decimal, so anything beyond 0.05 is a real disagreement
    and not rounding.
    """
    m = _RETENTION_CLAIM_RE.search(detail or "")
    if not m:
        return False, ("published retention sentence does not match the "
                       "checkable form '<n> of the prior window's <N> agents "
                       "returned = <p>%' — an unparseable claim cannot be "
                       "verified, so it is not publishable")
    num, den, shown = int(m.group(1)), int(m.group(2)), float(m.group(3))
    if not den:
        return False, "printed denominator is zero"
    expect = round(100.0 * num / den, 1)
    if abs(shown - expect) > 0.05:
        return False, (f"published {shown}% but {num}/{den} = {expect}% — the "
                       f"printed percentage is not the printed ratio")
    return True, f"{num}/{den} = {shown}% verified against the published text"


def _table_exists(cur, name: str) -> bool:
    cur.execute("SELECT to_regclass(%s)", (name,))
    row = cur.fetchone()
    return bool(row and row[0])


# ── lane 1 · retention ───────────────────────────────────────────────────────
def _lane_retention() -> list[dict]:
    checks: list[dict] = []
    c = _conn()
    if c is None:
        return [_check("returning", "returning vs new external agents", False,
                       "db unavailable — could not check", critical=True)]
    try:
        with c.cursor() as cur:
            # An agent is RETURNING this week if it also called in the prior
            # 7d window. Same identity basis as the funnel and /ai.
            cur.execute("""
                WITH cur AS (
                  SELECT DISTINCT agent_id FROM mcp_calls_identity
                   WHERE is_public_ip AND is_real_external
                     AND created_at >= now() - interval '7 days'
                ), prev AS (
                  SELECT DISTINCT agent_id FROM mcp_calls_identity
                   WHERE is_public_ip AND is_real_external
                     AND created_at >= now() - interval '14 days'
                     AND created_at <  now() - interval '7 days'
                )
                SELECT (SELECT COUNT(*) FROM cur),
                       (SELECT COUNT(*) FROM cur WHERE agent_id IN (SELECT agent_id FROM prev)),
                       (SELECT COUNT(*) FROM prev)
            """)
            row = cur.fetchone() or (0, 0, 0)
            cur_n, returning, prev_n = (int(row[0] or 0), int(row[1] or 0),
                                        int(row[2] or 0))
        pct = _retention_pct(returning, prev_n)
        if pct is not None:
            # No invented target: the check reports the measured rate and
            # fails only when the mechanism is provably absent (nobody at all
            # came back), which is the condition lane 2 exists to fix.
            detail = (
                f"{returning} of the prior window's {prev_n} agents returned "
                f"= {pct:.1f}% (7d retention). DENOMINATOR IS THE PRIOR "
                f"COHORT — the {cur_n} agents active in the last 7d are "
                f"context, not the base: dividing by the current window "
                f"answers 'how few of this week's agents are new', which "
                f"rises when the fleet shrinks. Zero returning = the return "
                f"path is not working, which is lane 2.")
            checks.append(_check(
                "returning", "returning vs new external agents",
                returning > 0, detail, critical=True))
            ok, why = _retention_claim_ok(detail)
            checks.append(_check(
                "retention_denominator",
                "published retention % == returning / prior cohort",
                ok, why, critical=True))
        elif cur_n:
            checks.append(_check(
                "returning", "returning vs new external agents", None,
                f"{cur_n} agents active in the last 7d but NOBODY was active "
                f"the prior 7d — retention is UNMEASURED, not 0%: with an "
                f"empty prior cohort there was nobody who could return",
                critical=True))
        else:
            checks.append(_check(
                "returning", "returning vs new external agents", None,
                "no external agents in the 7d window — UNMEASURED "
                "(indeterminate, not green)", critical=True))
    except Exception as e:
        checks.append(_check("returning", "returning vs new external agents",
                             False, f"query failed: {type(e).__name__}: {e}"[:200],
                             critical=True))
    return checks


# ── lane 2 · return mechanism ────────────────────────────────────────────────
def _lane_return_mechanism() -> list[dict]:
    checks: list[dict] = []
    c = _conn()
    if c is None:
        return [_check("adoption", "agents adopting the return path", False,
                       "db unavailable — could not check", critical=True)]
    days_since_fix = (_dt.date.today() - DISCOVERY_FIX_DATE).days
    try:
        with c.cursor() as cur:
            cur.execute("""
                SELECT COUNT(DISTINCT agent_id), COUNT(*)
                  FROM mcp_calls_identity
                 WHERE is_public_ip AND is_real_external
                   AND created_at >= now() - interval '7 days'
                   AND tool_name = ANY(%s)
            """, (list(_RETURN_TOOLS),))
            row = cur.fetchone() or (0, 0)
            agents, calls = int(row[0] or 0), int(row[1] or 0)
            cur.execute("""
                SELECT tool_name, COUNT(*) AS n
                  FROM mcp_calls_identity
                 WHERE is_public_ip AND is_real_external
                   AND created_at >= now() - interval '7 days'
                   AND tool_name = ANY(%s)
                 GROUP BY tool_name ORDER BY n DESC LIMIT 4
            """, (list(_RETURN_TOOLS),))
            top = cur.fetchall() or []
        detail_top = ", ".join(f"{t}={n}" for t, n in top) or "none"
        # BORN RED until adoption exists. The detail carries the age of the
        # discovery fix so a fresh zero reads as "too early", not "broken".
        checks.append(_check(
            "adoption", "agents adopting the return path", agents > 0,
            f"{agents} distinct agents made {calls} saved-work/get_changes "
            f"calls in 7d ({detail_top}). Day {days_since_fix} since the "
            f"discovery fix made this family reachable (mcp-server #125). "
            f"The offer already ships (next_session names save_site / "
            f"set_*_alert / subscribe_digest on keyed answers) — so a zero "
            f"here means agents see the offer and decline it, which is a "
            f"different problem from never being asked. BORN RED until >0.",
            critical=True))
        checks.append(_check(
            "family_pinned", "return-path tool list is stated, not guessed",
            len(_RETURN_TOOLS) >= 12,
            f"{len(_RETURN_TOOLS)} tools mirrored from the saved_work family "
            f"+ get_changes; add here in the same change that adds them there",
            critical=False))
    except Exception as e:
        checks.append(_check("adoption", "agents adopting the return path",
                             False, f"query failed: {type(e).__name__}: {e}"[:200],
                             critical=True))
    return checks


# ── lane 3 · error dead ends ─────────────────────────────────────────────────
def _lane_error_dead_ends() -> list[dict]:
    """★ DELIBERATE basis exception: success/status_code live on
    mcp_connections, not on the identity view. This is an ERROR-RATE lane, not
    an agent count — it never reports an agent figure, so the canonical-basis
    rule is not in play. Stated here so a future reader does not "fix" it by
    repointing at mcp_calls_identity, which has no success column."""
    checks: list[dict] = []
    c = _conn()
    if c is None:
        return [_check("failure_rate", "tool-call failure concentration", False,
                       "db unavailable — could not check", critical=True)]
    try:
        with c.cursor() as cur:
            if not _table_exists(cur, "mcp_connections"):
                checks.append(_check(
                    "failure_rate", "tool-call failure concentration", None,
                    "mcp_connections absent — UNMEASURED", critical=True))
            else:
                cur.execute("""
                    SELECT COUNT(*) FILTER (
                             WHERE success IS FALSE
                                OR (success IS NULL AND status_code IS NOT NULL
                                    AND status_code NOT BETWEEN 200 AND 299)),
                           COUNT(*)
                      FROM mcp_connections
                     WHERE created_at >= now() - interval '7 days'
                       AND method = 'tools/call'
                """)
                row = cur.fetchone() or (0, 0)
                bad, total = int(row[0] or 0), int(row[1] or 0)
                cur.execute("""
                    SELECT tool_name, COUNT(*) AS n
                      FROM mcp_connections
                     WHERE created_at >= now() - interval '7 days'
                       AND method = 'tools/call'
                       AND tool_name IS NOT NULL AND tool_name <> ''
                       AND (success IS FALSE
                            OR (success IS NULL AND status_code IS NOT NULL
                                AND status_code NOT BETWEEN 200 AND 299))
                     GROUP BY tool_name ORDER BY n DESC LIMIT 5
                """)
                top = cur.fetchall() or []
                if total:
                    rate = 100.0 * bad / total
                    tops = ", ".join(f"{t}={n}" for t, n in top) or "none"
                    checks.append(_check(
                        "failure_rate", "tool-call failure concentration",
                        True,
                        f"{bad} failed of {total} tools/call in 7d "
                        f"({rate:.1f}%). Top: {tops}. Each failure without a "
                        f"mitigation block is a session that ends.",
                        critical=False))
                else:
                    checks.append(_check(
                        "failure_rate", "tool-call failure concentration", None,
                        "no tools/call rows in 7d — UNMEASURED", critical=True))
        # Contract-read: the registry the mitigation hints come from. A lane
        # that only counted failures would not notice the registry being
        # emptied — this reads the real module the API serves.
        try:
            from routes.error_mitigation import _REGISTRY
            missing_hint = [k for k, v in _REGISTRY.items() if not v.get("hint")]
            checks.append(_check(
                "registry", "error-code registry importable + hinted",
                len(_REGISTRY) > 0 and not missing_hint,
                f"{len(_REGISTRY)} codes registered"
                + (f"; MISSING hint: {missing_hint}" if missing_hint else
                   "; every code carries a deterministic hint"),
                critical=False))
        except Exception as e:
            checks.append(_check(
                "registry", "error-code registry importable + hinted", False,
                f"import failed: {type(e).__name__}: {e}"[:160], critical=False))
        # The measured 08-02 gap, kept visible until the coverage work lands:
        # our own validation errors carry mitigation, upstream passthrough
        # (API 404 / invalid parameter) does not.
        checks.append(_check(
            "passthrough_coverage", "upstream passthrough errors carry a hint",
            None,
            "UNMEASURED from the DB — the envelope is not stored. Measured "
            "live 2026-08-02: 3 of 5 error responses carried "
            "_error_mitigation; get_facility (API 404) and rank_markets "
            "(invalid parameter) carried none. Re-probe from the caller's "
            "seat after coverage work.", critical=False))
    except Exception as e:
        checks.append(_check("failure_rate", "tool-call failure concentration",
                             False, f"query failed: {type(e).__name__}: {e}"[:200],
                             critical=True))
    return checks


# ── lane 4 · count parity ────────────────────────────────────────────────────
def _lane_count_parity() -> list[dict]:
    checks: list[dict] = []
    c = _conn()
    if c is None:
        return [_check("parity", "one quantity, one number", False,
                       "db unavailable — could not check", critical=True)]
    try:
        with c.cursor() as cur:
            cur.execute("""
                SELECT COUNT(DISTINCT agent_id) FROM mcp_calls_identity
                 WHERE is_public_ip AND is_real_external
                   AND created_at >= now() - interval '7 days'
            """)
            canonical = int((cur.fetchone() or [0])[0] or 0)
            rollup = None
            if _table_exists(cur, "reach_weekly"):
                try:
                    # ★2026-08-04: guessed `agents`; the column is
                    # `distinct_external_ips`. reach_weekly EXISTS — the lane
                    # reported "absent table or column shape" and rendered
                    # UNMEASURED for two days because the except swallowed an
                    # UndefinedColumn. Fourth name-guess of the day.
                    cur.execute("""
                        SELECT MAX(distinct_external_ips) FROM (
                          SELECT distinct_external_ips FROM reach_weekly
                           ORDER BY week_start DESC LIMIT 2) t
                    """)
                    r = cur.fetchone()
                    rollup = int(r[0]) if r and r[0] is not None else None
                except Exception:
                    rollup = None
        # r-basis-parity (2026-08-03): lane 4 compared AGENT counts only, so
        # it was blind to the split that actually shipped — the funnel card
        # rendered agents from the canonical query beside CALLS from
        # mcp_tool_calls (complete-days, different predicate) and printed that
        # basis's +323.7% next to the canonical +20.6%. Same class, different
        # column. This check asserts the pair the canonical helper returns
        # together is internally coherent: calls cannot be fewer than agents
        # (every agent made at least one call), and a live pair must exist at
        # all. It is deliberately a WEAK invariant — the strong guarantee is
        # that both halves come from ONE query, which is a code property the
        # dashboard now satisfies and tests/test_canonical_counts_drift.py
        # pins for emitters.
        try:
            with c.cursor() as cur2:
                from mcp_calls_deloop import canonical_external_activity_sql
                cur2.execute(canonical_external_activity_sql(7))
                pair = cur2.fetchone() or (0, 0)
                p_agents, p_calls = int(pair[0] or 0), int(pair[1] or 0)
            coherent = (p_calls >= p_agents) and not (p_agents and not p_calls)
            checks.append(_check(
                "pair_coherent", "agents + calls come from one query",
                coherent,
                f"canonical pair = {p_agents} agents / {p_calls} calls (one "
                f"query, one window, includes today). A card pairing this "
                f"agent count with a calls figure from mcp_tool_calls "
                f"complete-days is comparing two populations over two "
                f"windows — that pairing produced +20.6% beside +323.7% on "
                f"2026-08-03.", critical=False))
        except Exception as e:
            checks.append(_check(
                "pair_coherent", "agents + calls come from one query", None,
                f"canonical pair unreadable: {type(e).__name__}: {e}"[:160],
                critical=False))
        if rollup is None:
            checks.append(_check(
                "parity", "one quantity, one number", None,
                f"canonical rolling-7d = {canonical}; reach_weekly rollup not "
                f"readable (absent table or column shape) — UNMEASURED. The "
                f"08-02 skew was 95 (rollup-fed badge) vs 84 (canonical).",
                critical=False))
        else:
            gap = abs(canonical - rollup)
            # ★ 2026-08-04: this check used to assert `gap <= 5` and state, in
            # the present tense, that the two "feed two figures the /ai page
            # shows as one quantity". Both halves were wrong by then.
            #
            # (a) The two measure different populations over different windows
            #     BY CONSTRUCTION — canonical is a rolling-7d identity count
            #     (is_public_ip AND is_real_external); reach_weekly is an
            #     ISO-week rollup without those exclusions. They will never
            #     converge, so `gap <= 5` was permanently red and carried no
            #     information. A guard that cries wolf gets deleted, and this
            #     one nearly earned it.
            # (b) The /ai claim was stale. Verified 2026-08-04 from the
            #     consumer's seat: every agent-count surface binds
            #     real_tool_use_7d.agents_7d, and funnel / tracking / reach all
            #     returned 46 together. The rollup reaches the page only as a
            #     fallback, and the caption is relabeled when it does.
            #
            # So this is now a MONITOR (verdict None = unmeasured, never red):
            # it publishes the spread so a human can see drift, and refuses to
            # make a claim about a public surface a pure-DB lane cannot read.
            # Asserting a page defect that is already fixed sends the next
            # reader — or the weekly auditor — to "fix" correct code.
            checks.append(_check(
                "parity", "one quantity, one number", None,
                f"canonical rolling-7d = {canonical}; reach_weekly rollup = "
                f"{rollup} (spread {gap}). MONITOR, not a defect: different "
                f"populations over different windows, so a spread is expected "
                f"and only its SHAPE is informative. The failure this watches "
                f"for is either number reaching a public surface labelled as "
                f"the other — that binding lives in the frontend and cannot be "
                f"read from here; verified by hand 2026-08-04 (all three "
                f"endpoints agreed at {canonical}). Historic skew: 95 "
                f"(rollup-fed badge) vs 84 (canonical) on 08-02, fixed in "
                f"frontend r-agent-parity.", critical=False))
    except Exception as e:
        checks.append(_check("parity", "one quantity, one number", False,
                             f"query failed: {type(e).__name__}: {e}"[:200],
                             critical=True))
    return checks


# ── lane 5 · concentration ───────────────────────────────────────────────────
def _lane_concentration() -> list[dict]:
    checks: list[dict] = []
    c = _conn()
    if c is None:
        return [_check("caller_share", "no single caller carries the trend",
                       False, "db unavailable — could not check", critical=True)]
    try:
        with c.cursor() as cur:
            # ★ REPOINTED 2026-08-05 (basis alignment). This lane used to
            # inline its own near-copy of the concentration query. The copy
            # took MAX(n) over ALL groups — including the NULL agent_id
            # bucket, which mcp_calls_identity uses for Cloudflare POP ranges
            # (edge proxies are deliberately not agents). If that bucket ever
            # became the max, this lane would have reported "the POP bucket"
            # as the top caller and gated on it. Checked 2026-08-05 before
            # repointing: the NULL bucket held 0 rows in the window and was
            # NOT the max, so the number this lane published was not wrong —
            # but it was unguarded, and one CF routing change away from it.
            # canonical_top_caller_sql applies the guard to the NUMERATOR
            # only and is now the single source this lane and the public
            # funnel field both read, so the two can no longer drift.
            from mcp_calls_deloop import canonical_top_caller_sql
            cur.execute(canonical_top_caller_sql(7))
            row = cur.fetchone() or (0, 0, 0)
            top_calls, all_calls = int(row[0] or 0), int(row[1] or 0)
        if all_calls:
            share = 100.0 * top_calls / all_calls
            checks.append(_check(
                "caller_share", "no single caller carries the trend",
                share <= _CONCENTRATION_PCT,
                f"top caller = {share:.1f}% of {all_calls} external tool "
                f"calls in 7d ({top_calls}). Above {_CONCENTRATION_PCT:.0f}% "
                f"the total tracks one caller's mood, not demand — read "
                f"agents, not calls.", critical=False))
        else:
            checks.append(_check(
                "caller_share", "no single caller carries the trend", None,
                "no external tool calls in 7d — UNMEASURED", critical=False))
        # Crawler-side concentration, from the platform roster.
        with c.cursor() as cur:
            if _table_exists(cur, "ai_cumulative"):
                cur.execute("""
                    SELECT platform, requests_7d FROM ai_cumulative
                     WHERE requests_7d > 0
                       AND platform NOT IN ('internal','mcp','mcp_generic','direct')
                     ORDER BY requests_7d DESC
                """)
                rows = cur.fetchall() or []
                tot = sum(int(r[1] or 0) for r in rows)
                if tot and rows:
                    p, n = rows[0][0], int(rows[0][1] or 0)
                    sh = 100.0 * n / tot
                    # ★ THRESHOLD CORRECTED 2026-08-05. This bar was the
                    # literal 90.0 while the sibling caller_share uses
                    # _CONCENTRATION_PCT (25.0). At 78.5% the check returned
                    # pass=True under the name "no single platform carries
                    # reach" and a detail that reads "A WoW built on one
                    # platform's burst is concentration, not growth" — the
                    # verdict, the name and the prose all disagreed, and the
                    # green was the only part a reader skimming the lane saw.
                    #
                    # Which of the three was wrong: the THRESHOLD. A 90% bar
                    # does not test "carries reach", it tests "is the only
                    # platform left" — nothing short of near-total monopoly
                    # could ever trip it, so the check could effectively only
                    # ever pass. A check that can only fail one way is the
                    # class this repo already learned to distrust; one that
                    # can only PASS is the same defect with the sign flipped
                    # and no false alarm to make anyone look. The name and
                    # prose describe concentration in exactly the sense
                    # caller_share means it, so both now read the same
                    # constant and a rename of the idea moves both at once.
                    # State the bar in the detail, as caller_share does, so
                    # the number and the verdict are legible together.
                    checks.append(_check(
                        "platform_share", "no single platform carries reach",
                        sh <= _CONCENTRATION_PCT,
                        f"top platform '{p}' = {sh:.1f}% of {tot} named "
                        f"crawler requests in 7d. Above "
                        f"{_CONCENTRATION_PCT:.0f}% a WoW built on one "
                        f"platform's burst is concentration, not growth.",
                        critical=False))
                else:
                    checks.append(_check(
                        "platform_share", "no single platform carries reach",
                        None, "no named crawler traffic in 7d — UNMEASURED",
                        critical=False))
    except Exception as e:
        checks.append(_check("caller_share", "no single caller carries the trend",
                             False, f"query failed: {type(e).__name__}: {e}"[:200],
                             critical=True))
    return checks


# ── lane 6 · crawler silence ─────────────────────────────────────────────────
def _lane_crawler_silence() -> list[dict]:
    checks: list[dict] = []
    c = _conn()
    if c is None:
        return [_check("silence", "no named platform has gone quiet", False,
                       "db unavailable — could not check", critical=True)]
    try:
        with c.cursor() as cur:
            if not _table_exists(cur, "ai_cumulative"):
                return [_check("silence", "no named platform has gone quiet",
                               None, "ai_cumulative absent — UNMEASURED",
                               critical=False)]
            # last_seen is TEXT on this table (documented schema drift) — the
            # regex CASE guard runs before the cast can throw, the same
            # pattern get_cumulative_totals uses and changes_feed adopted.
            # ★2026-08-04: the denylist above let REGISTRY PROBES through —
            # fabrique-noauth-probe, chiark-prober, glama, yellowmcp-health all
            # crossed 1k lifetime and have been quiet ~97 days, so this lane
            # screamed about four things that were never AI platforms and would
            # have gone on screaming forever. A lane that always fails is a
            # lane nobody reads.
            # Filter to the curated roster instead — AI_PLATFORMS is the same
            # vocabulary detect_platform() and the /ai roster use. Imported,
            # never restated, so a new platform is covered the day it is added.
            from ai_tracking import AI_PLATFORMS as _ROSTER
            cur.execute("""
                SELECT platform, total_requests,
                       CASE WHEN last_seen ~ '^\\d{4}-\\d{2}-\\d{2}'
                            THEN EXTRACT(EPOCH FROM (now() - last_seen::timestamptz))/86400.0
                       END AS days_quiet
                  FROM ai_cumulative
                 WHERE total_requests >= 1000
                   AND platform = ANY(%s)
                 ORDER BY total_requests DESC
            """, (list(_ROSTER),))
            rows = cur.fetchall() or []
        quiet = [(p, int(t or 0), float(d)) for p, t, d in rows
                 if d is not None and float(d) > _SILENCE_DAYS]
        if not rows:
            checks.append(_check("silence", "no named platform has gone quiet",
                                 None, "no named platforms with >=1k lifetime "
                                 "requests — UNMEASURED", critical=False))
        elif quiet:
            worst = ", ".join(f"{p} {d:.1f}d ({t:,} lifetime)"
                              for p, t, d in sorted(quiet, key=lambda x: -x[2])[:4])
            checks.append(_check(
                "silence", "no named platform has gone quiet", False,
                f"{len(quiet)} platform(s) silent >{_SILENCE_DAYS}d: {worst}. "
                f"NOTE the 'copilot' bucket is matched on BingBot — its "
                f"silence is a search-crawl event (Bing Webmaster Tools), not "
                f"an assistant abandoning us.", critical=False))
        else:
            checks.append(_check(
                "silence", "no named platform has gone quiet", True,
                f"all {len(rows)} named platforms seen within "
                f"{_SILENCE_DAYS}d", critical=False))
    except Exception as e:
        checks.append(_check("silence", "no named platform has gone quiet",
                             False, f"query failed: {type(e).__name__}: {e}"[:200],
                             critical=True))
    return checks


def _run_tick() -> dict:
    lanes = [
        {"id": "retention", "name": "1 · retention",
         "checks": _safe_lane(_lane_retention)},
        {"id": "return_mechanism", "name": "2 · return mechanism",
         "checks": _safe_lane(_lane_return_mechanism)},
        {"id": "error_dead_ends", "name": "3 · error dead ends",
         "checks": _safe_lane(_lane_error_dead_ends)},
        {"id": "count_parity", "name": "4 · count parity",
         "checks": _safe_lane(_lane_count_parity)},
        {"id": "concentration", "name": "5 · concentration",
         "checks": _safe_lane(_lane_concentration)},
        {"id": "crawler_silence", "name": "6 · crawler silence",
         "checks": _safe_lane(_lane_crawler_silence)},
    ]
    for ln in lanes:
        ln["verdict"] = _lane_verdict(ln["checks"])
    return {
        "ok": True,
        "shell": "agent-retention-master-shell#49",
        "generated_at": _dt.datetime.utcnow().isoformat() + "Z",
        "summary": " ".join(f"{ln['id']}={ln['verdict']}" for ln in lanes),
        "lanes": lanes,
        "kill": "AGENT_RETENTION_SHELL_DISABLE=1",
        "note": ("READ-ONLY board over the 2026-08-02 improvement review. "
                 "Lane 2 is BORN RED until agents adopt the return path — it "
                 "goes green on behaviour, not on a deploy. v1 has no cron on "
                 "purpose (see docstring)."),
    }


@agent_retention_master_shell_bp.route(
    "/api/v1/admin/agent-retention/master-tick", methods=["GET", "POST"])
def retention_master_tick():
    if _disabled():
        # ★404, never 5xx (2026-08-12): the CF worker's proxyWithRetry reads
        # ANY 5xx from Railway as a dead origin and fails the site over to the
        # stale Render backend. Turning off one diagnostic shell must not be
        # able to do that. See graph_spine_master_shell for the original note.
        return jsonify(ok=False, error="AGENT_RETENTION_SHELL_DISABLE=1"), 404
    if not _admin_ok():
        return jsonify(ok=False, error="admin key required"), 401
    r = jsonify(_run_tick())
    r.headers["Cache-Control"] = "no-store"
    return r


@agent_retention_master_shell_bp.route("/admin/agent-retention", methods=["GET"])
@agent_retention_master_shell_bp.route("/api/v1/admin/agent-retention",
                                       methods=["GET"])
def retention_master_dashboard():
    if _disabled():
        return Response("shell disabled", status=404, mimetype="text/plain")
    if not _admin_ok():
        return Response("admin key required", status=401, mimetype="text/plain")
    d = _run_tick()
    css = ("body{font:14px/1.5 -apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;"
           "background:#0f1320;color:#e8ecf7;padding:28px;max-width:1000px;margin:auto}"
           "h1{font-size:20px;margin:0 0 4px}.sub{color:#8b92a8;font-size:12px;margin-bottom:20px}"
           ".lane{border:1px solid #1f2540;border-radius:10px;padding:14px 16px;margin-bottom:12px}"
           ".v{float:right;font-weight:700;font-size:12px;letter-spacing:.08em}"
           ".green{color:#3ddc97}.red{color:#ff6b6b}.amber{color:#ffc857}"
           ".c{margin:8px 0 0;padding-left:18px;color:#c3c9db;font-size:13px}"
           ".c li{margin-bottom:4px}.d{color:#8b92a8}")
    def cls(v):
        return "green" if v == "green" else ("red" if v == "red" else "amber")
    parts = [f"<style>{css}</style><h1>Agent Retention Master Shell #49</h1>",
             f"<div class='sub'>{d['generated_at']} · {d['summary']}</div>"]
    for ln in d["lanes"]:
        parts.append(f"<div class='lane'><span class='v {cls(ln['verdict'])}'>"
                     f"{ln['verdict'].upper()}</span><b>{ln['name']}</b><ul class='c'>")
        for ch in ln["checks"]:
            mark = "✓" if ch["pass"] is True else ("✗" if ch["pass"] is False else "?")
            parts.append(f"<li>{mark} <b>{ch['name']}</b><br>"
                         f"<span class='d'>{ch['detail']}</span></li>")
        parts.append("</ul></div>")
    parts.append("<small class='d'>READ-ONLY · pure-DB · lane 2 born red by "
                 "design · kill AGENT_RETENTION_SHELL_DISABLE=1</small>")
    r = Response("".join(parts), mimetype="text/html")
    r.headers["Cache-Control"] = "no-store"
    return r
