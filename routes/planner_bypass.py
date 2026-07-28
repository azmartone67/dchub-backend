"""planner_bypass.py — the planner BYPASS rate (2026-07-28).

Proposed by ChatGPT during the 07-28 front-door audit, and it is the metric that
tells us whether that audit actually changed behaviour rather than just traffic:

  "Infrastructure-capable question detected. execute_plan available. The agent
   nevertheless starts with something else."

Total requests tell you adoption. FIRST-tool selection tells you mindshare.

────────────────────────────────────────────────────────────────────────────
WHY THIS IS NOT SESSIONIZED — the trap that would have made it a flattering zero
────────────────────────────────────────────────────────────────────────────
The obvious unit is the session. It is the WRONG one here, measurably:
`session_id` rotates per MCP connection — of 7,933 sessions in 30d exactly ONE
spanned more than a calendar day, and real-external sessions average **1.2
calls**. A session is therefore ≈ a single call, so a session-scoped test for
"did the agent hand-chain instead of planning?" can almost never observe a
second call and would report ~0 bypass BY CONSTRUCTION — a zero that reads as
success and means "not measured".

So the episode here is the **agent-day**: durable `api_key` where present,
falling back to `sess:<session_id>`, bucketed by calendar day. That collapses
connection churn while staying computable from what `mcp_call_log` actually
stores. We report the session-scoped figure ALONGSIDE it, explicitly labelled,
so the artifact stays visible instead of being quietly averaged away.

────────────────────────────────────────────────────────────────────────────
TWO METRICS, NOT ONE — "did it choose the planner?" vs "should it have?"
────────────────────────────────────────────────────────────────────────────
ChatGPT's review of the first draft caught a real defect: a single bypass rate
conflates two different questions, and the naive proxy PUNISHES GOOD AGENTS.

  User: "Compare Dallas and Phoenix."
  Agent: get_market_intel(Dallas); get_market_intel(Phoenix); summarise.

Three calls, no execute_plan — and the first draft scored that a bypass. But
that is deliberate side-by-side inspection, and a platform may prefer it for
determinism. Calling it a bypass slanders the fleet, which is exactly the kind
of flattering-or-damning number this codebase keeps having to retract.

So we measure two OBSERVED things and leave the judgement call explicit:

  OPPORTUNITY          an episode with 2+ calls — something happened that
                       execute_plan could conceivably have absorbed.
  PLANNER ADOPTION     of opportunities, first call == execute_plan.
                       Pure observation, no judgement.
  MANUAL ORCHESTRATION of opportunities, 2+ DISTINCT tools AND a hand-off
                       signal, without execute_plan first. Also pure
                       observation — this is the shape execute_plan replaces.
  BENIGN FAN-OUT       the same tool repeated with different args (the
                       Dallas/Phoenix case). Reported SEPARATELY and never
                       counted as bypass.
  BENIGN DIRECT        one distinct tool. Correct single-capability usage.

  planner_bypass ≈ manual_orchestration − legitimate exceptions,
  where "legitimate exceptions" stays a POLICY decision, not a silent constant.

THE HAND-OFF SIGNAL — approximating a dependency chain
A → B → C where B's input came from A's output is orchestration. A, B, C
independent is not. We do not log results, so true dependency is unobservable;
but we DO log `params`, and the planner's own contract names the values that
thread between steps (candidate_id, metro_slug, market, iso, lat/lon). So: an
episode shows hand-off when a LATER call carries one of those keys that the
FIRST call did not. Weaker than reading the dataflow, stronger than counting
calls, and honest about being neither.

★ Stated assumption, never silently folded in: we cannot see a client's
`allowed_tools` scoping, so "execute_plan was available to this caller" is an
assumption, not a measurement. It is surfaced as `assumptions` in the payload.

────────────────────────────────────────────────────────────────────────────
NEVER A RATE OFF AN EMPTY DENOMINATOR
────────────────────────────────────────────────────────────────────────────
Every rate here is None (→ JSON null) with status UNMEASURED when its
denominator is 0. This codebase has been bitten repeatedly by checks that print
0% or 100% off no rows and get read as a finding; a lane must never read PASS —
or FAIL — when it could not check.

Endpoint (admin-keyed, read-only):
  GET /api/v1/admin/planner-bypass?days=14
"""
from __future__ import annotations

import logging
import os

from flask import Blueprint, jsonify, request

logger = logging.getLogger("planner_bypass")
planner_bypass_bp = Blueprint("planner_bypass", __name__)

# The front door, and the tool it replaced. plan_query-first is now itself a
# BYPASS — and the most diagnostic kind, because it means that agent is running
# on instructions from before execute_plan shipped.
FRONT_DOOR = "execute_plan"
LEGACY_DOOR = "plan_query"

# ★★ CONTRACT VERSION — ChatGPT's fifth field (07-28). The four we already had
# (observation / interpretation / assumptions / consumers) do not catch the
# failure that actually bit us: the numbers were never wrong, the INTERPRETATION
# CONTRACT changed underneath a consumer that did not know it.
#
# Concretely: agent_adoption_master_shell's `planner_first` kept meaning
# "first call was plan_query" long after execute_plan became the front door. A
# consumer reading v1 semantics off a v2 producer emitted a recommendation for
# the exact opposite of the fix. That is API evolution — producer evolved,
# consumer did not — and these surfaces are versioned whether or not we say so.
# Declaring it does not create the versioning; it makes it checkable.
#
# Consumers SHOULD assert compatibility before acting on these fields.
# BUMP THIS whenever a field's MEANING changes, even if its name and type do not:
#   1  initial — single bypass rate, session-scoped, 2+ distinct tools
#   2  agent-day episodes; observation split from judgement (planner_adoption vs
#      manual_orchestration); benign fan-out excluded; hand-off signal required
DEFINITION_VERSION = 2

# Reuse the canonical synthetic-client list rather than inventing a third one
# (the de-loop drift this repo already fixed once). Same import+fallback shape
# as routes/enterprise_leads_sweep.py.
try:
    from mcp_upgrade_gate import _SYNTHETIC_CLIENT_PREFIXES as _SYNTH_PREFIXES
except Exception:
    _SYNTH_PREFIXES = ('dchub-', 'step2_', 'qa-', 'probe-', 'test-', 'monitor-',
                       'healthcheck', 'r51-', 'r52-', 'e2e-', 'recheck')

# Prefix constants only — no user input — so they are safe to inline.
# ★ Filter on user_agent AS WELL as platform: the server overwrites the platform
# tag on some paths, so a platform-only exclusion lets our own probes back in.
# ★★ The percent-signs below are DOUBLED on purpose. These queries always run
# with real params, so psycopg2 %-formats the entire SQL string — a single
# percent-sign in a LIKE pattern silently eats one of the real arguments and the
# endpoint 500s with a binding error that names no table or column. This exact
# trap took /api/v1/map down on 2026-07-17. Doubling is correct HERE precisely
# because substitution is guaranteed to run; when it is not, pass params=None.
_SYNTH_NOT_LIKE = "".join(
    (" AND LOWER(COALESCE(platform,'')) NOT LIKE '{p}%%'"
     " AND LOWER(COALESCE(user_agent,'')) NOT LIKE '%%{p}%%'").format(
        p=str(prefix).lower().replace("'", ""))
    for prefix in _SYNTH_PREFIXES)


def _conn():
    import psycopg2
    db = os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL")
    if not db:
        return None
    try:
        c = psycopg2.connect(db, sslmode="require", connect_timeout=8)
        c.autocommit = True
        return c
    except Exception:
        return None


def _admin_ok() -> bool:
    expected = (os.environ.get("DCHUB_ADMIN_KEY") or
                os.environ.get("DCHUB_INTERNAL_KEY") or "").strip()
    provided = (request.headers.get("X-Admin-Key")
                or request.args.get("admin_key")
                or request.headers.get("Authorization", "").replace("Bearer ", "").strip())
    return bool(expected) and provided == expected


def _disabled() -> bool:
    return (os.environ.get("PLANNER_BYPASS_DISABLE") or "").strip() == "1"


def _rate(num, den):
    """A rate, or None when there is nothing to divide. Never 0.0-off-zero."""
    if not den:
        return None
    return round(100.0 * num / den, 2)


# Episode identity: durable key first, connection second. COALESCE order matters
# — an agent that persists its key is ONE episode per day no matter how many
# times it reconnects, which is the whole point of not sessionizing.
_EPISODE_ID = "COALESCE(NULLIF(api_key,''), 'sess:' || COALESCE(session_id,''))"

# Keys the planner's own contract threads between steps. A LATER call carrying
# one the FIRST call did not is our proxy for "output of A became input to B".
HANDOFF_KEYS = ("candidate_id", "metro_slug", "market", "iso", "lat", "lon", "slug")

# `?` is the jsonb key-exists operator. psycopg2 does NOT treat it as a
# placeholder (only %s is), so it is safe inline here.
_HANDOFF_SQL = " OR ".join(
    "(r.params ? '{k}' AND NOT (f.first_params ? '{k}'))".format(k=k) for k in HANDOFF_KEYS
)

_SQL = """
WITH scoped AS (
  SELECT {episode} AS episode_id,
         timestamp::date AS day,
         tool,
         timestamp,
         COALESCE(params, '{{}}'::jsonb) AS params
    FROM mcp_call_log
   WHERE timestamp > NOW() - make_interval(days => %s)
     AND tool IS NOT NULL AND tool <> ''
     AND ({episode}) <> 'sess:'
     {synth}
),
ranked AS (
  SELECT s.*,
         ROW_NUMBER() OVER (PARTITION BY episode_id, day ORDER BY timestamp ASC) AS rn
    FROM scoped s
),
firsts AS (
  SELECT episode_id, day, tool AS first_tool, params AS first_params
    FROM ranked WHERE rn = 1
),
agg AS (
  SELECT r.episode_id, r.day,
         COUNT(*)             AS calls,
         COUNT(DISTINCT r.tool) AS distinct_tools,
         f.first_tool,
         BOOL_OR(r.rn > 1 AND ({handoff})) AS has_handoff
    FROM ranked r
    JOIN firsts f USING (episode_id, day)
   GROUP BY r.episode_id, r.day, f.first_tool
)
SELECT
  COUNT(*)                                                           AS episodes,
  COUNT(*) FILTER (WHERE calls >= 2)                                 AS opportunities,
  COUNT(*) FILTER (WHERE calls >= 2 AND first_tool = %s)             AS planner_adopted,
  COUNT(*) FILTER (WHERE calls >= 2 AND first_tool <> %s
                     AND distinct_tools >= 2 AND has_handoff)        AS manual_orchestration,
  COUNT(*) FILTER (WHERE calls >= 2 AND first_tool <> %s
                     AND distinct_tools = 1)                         AS benign_fanout,
  COUNT(*) FILTER (WHERE calls >= 2 AND first_tool <> %s
                     AND distinct_tools >= 2 AND NOT has_handoff)    AS independent_multi,
  COUNT(*) FILTER (WHERE calls >= 2 AND first_tool = %s)             AS legacy_door_first,
  COUNT(*) FILTER (WHERE calls = 1)                                  AS benign_direct
  FROM agg
"""

_SQL_FIRST_TOOLS = """
WITH scoped AS (
  SELECT {episode} AS episode_id, timestamp::date AS day, tool, timestamp
    FROM mcp_call_log
   WHERE timestamp > NOW() - make_interval(days => %s)
     AND tool IS NOT NULL AND tool <> ''
     AND ({episode}) <> 'sess:'
     {synth}
),
episodes AS (
  SELECT episode_id, day, COUNT(DISTINCT tool) AS distinct_tools,
         (ARRAY_AGG(tool ORDER BY timestamp ASC))[1] AS first_tool
    FROM scoped GROUP BY episode_id, day
)
SELECT first_tool, COUNT(*) AS n
  FROM episodes
 WHERE distinct_tools >= 2
 GROUP BY first_tool
 ORDER BY n DESC
 LIMIT 15
"""

# The session-scoped version — reported ONLY so the artifact stays visible.
_SQL_SESSION = """
WITH first_call AS (
  SELECT DISTINCT ON (session_id) session_id, tool
    FROM mcp_call_log
   WHERE timestamp > NOW() - make_interval(days => %s)
     AND session_id IS NOT NULL AND session_id <> ''
   ORDER BY session_id, timestamp ASC
)
SELECT COUNT(*), COUNT(*) FILTER (WHERE tool = %s) FROM first_call
"""


def _measure(days: int = 14) -> dict:
    out = {
        "ok": True,
        "window_days": days,
        "front_door": FRONT_DOOR,
        # Declare the semantics, not just the numbers. A consumer that acts on
        # these fields should refuse to run against a version it does not know.
        "definition_version": DEFINITION_VERSION,
        "definition_changelog": {
            1: "session-scoped; single bypass rate; any 2+ distinct tools counted",
            2: "agent-day episodes; planner_adoption (observed) split from "
               "manual_orchestration (observed) and bypass (policy); benign "
               "fan-out and single-capability lookups excluded; hand-off signal "
               "required before an episode counts as orchestration",
        },
        "unit": "agent-day episode (durable api_key, else session; bucketed by calendar day)",
        "status": "UNMEASURED",
        "assumptions": [
            "execute_plan availability is ASSUMED, not measured — a client's allowed_tools "
            "scoping is invisible to the server. Read every rate below as 'estimated, "
            "assuming execute_plan was callable whenever exposed by tools/list'.",
            "Multi-capability intent is INFERRED from behaviour, because the user's question "
            "is never logged — only the tool and its args.",
            "Hand-off is APPROXIMATED from params: a later call carrying a chaining key "
            f"({', '.join(HANDOFF_KEYS)}) the first call lacked. Results are not logged, so "
            "true output-to-input dataflow is unobservable.",
        ],
        "handoff_keys": list(HANDOFF_KEYS),
    }
    c = _conn()
    if c is None:
        out["ok"] = False
        out["error"] = "no database connection"
        return out
    try:
        with c.cursor() as cur:
            sql = _SQL.format(episode=_EPISODE_ID, synth=_SYNTH_NOT_LIKE,
                              handoff=_HANDOFF_SQL)
            cur.execute(sql, (days, FRONT_DOOR, FRONT_DOOR, FRONT_DOOR,
                              FRONT_DOOR, LEGACY_DOOR))
            (episodes, opportunities, adopted, manual, fanout, independent,
             legacy_first, direct) = [int(x or 0) for x in cur.fetchone()]

            out.update({
                "episodes": episodes,
                "opportunities": opportunities,
                # ── OBSERVED, no judgement ──────────────────────────────────
                "planner_adopted": adopted,
                "planner_adoption_pct": _rate(adopted, opportunities),
                "manual_orchestration": manual,
                "manual_orchestration_pct": _rate(manual, opportunities),
                # ── explicitly NOT bypass ───────────────────────────────────
                "benign_fanout": fanout,
                "benign_direct_single_call": direct,
                "independent_multi_no_handoff": independent,
                # ── the most diagnostic bypass: pre-execute_plan instructions
                "legacy_door_first": legacy_first,
                "stale_door_pct": _rate(legacy_first, opportunities),
                # ── the JUDGEMENT, kept separate and explicit ───────────────
                "planner_bypass_pct": _rate(manual, opportunities),
                "bypass_definition":
                    "manual_orchestration / opportunities. Benign fan-out (same tool, "
                    "different args — e.g. Dallas then Phoenix) and single-capability "
                    "direct lookups are EXCLUDED, not counted against the fleet. "
                    "Subtract legitimate exceptions as a policy decision; this endpoint "
                    "does not bake one in.",
            })
            out["status"] = "MEASURED" if opportunities else "UNMEASURED"
            if not opportunities:
                out["unmeasured_reason"] = (
                    "no episode in the window made 2+ calls, so there was no opportunity "
                    "to score. This is NOT a 0% bypass rate — nothing was measurable."
                )

            try:
                cur.execute(_SQL_FIRST_TOOLS.format(episode=_EPISODE_ID, synth=_SYNTH_NOT_LIKE),
                            (days,))
                out["first_tool_breakdown"] = [
                    {"tool": t, "episodes": int(n or 0)} for t, n in cur.fetchall()
                ]
            except Exception as e:
                logger.warning("[planner-bypass] breakdown: %s", str(e)[:150])

            try:
                cur.execute(_SQL_SESSION, (days, FRONT_DOOR))
                s_total, s_planner = [int(x or 0) for x in cur.fetchone()]
                out["session_scoped_reference"] = {
                    "sessions": s_total,
                    "planner_first_sessions": s_planner,
                    "planner_first_pct": _rate(s_planner, s_total),
                    "calls_per_session": None,
                    "why_not_headline":
                        "session_id rotates per MCP connection (~1.2 calls/session measured "
                        "2026-07-01), so a session cannot observe hand-chaining. Shown for "
                        "continuity with the older planner_first metric only.",
                }
            except Exception as e:
                logger.warning("[planner-bypass] session ref: %s", str(e)[:150])
    except Exception as e:
        out["ok"] = False
        out["error"] = str(e)[:300]
        logger.warning("[planner-bypass] measure failed: %s", str(e)[:200])
    finally:
        try:
            c.close()
        except Exception:
            pass
    return out


@planner_bypass_bp.route("/api/v1/admin/planner-bypass", methods=["GET"])
def planner_bypass_state():
    if _disabled():
        return jsonify(ok=False, error="disabled"), 503
    if not _admin_ok():
        return jsonify(ok=False, error="unauthorized"), 401
    try:
        days = max(1, min(90, int(request.args.get("days", 14))))
    except Exception:
        days = 14
    return jsonify(_measure(days))
