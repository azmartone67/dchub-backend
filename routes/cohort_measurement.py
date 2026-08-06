"""Per-cohort execute_plan adoption & retention — GET /api/v1/mcp/cohorts.

Microsoft Copilot emits a routing cohort on execute_plan (dchub-mcp-server
r-cohort). Seven tags, each isolating one routing hypothesis. This is the read
side: what each cohort's adoption and retention actually are.

★ WHY IT EXISTS. The board shows a finding no lane flags: ZERO of the 7
returning agents opened with the front door. So "execute_plan-first improves
retention" is a HYPOTHESIS, not a result — and nothing in the stack could tell
the difference, because no call said which routing it came from. This endpoint
makes that difference measurable. It does NOT make it true.

★ STORAGE NEEDS NO MIGRATION. server.mjs already forwards the whole tool-args
object as `params` on every tracked call, so the tag lands without a schema
change. Verified against the LIVE database, not repo DDL:
  · mcp_call_log.params  is jsonb — but the table has NO ip_address column,
    so it cannot express "how many AGENTS", only "how many calls".
  · mcp_tool_calls.params is text — and it IS the base table of
    mcp_calls_identity, which carries agent_id / is_public_ip /
    is_real_external.
Agent-grain questions therefore read the identity view JOINed back to its base
table for `params`. One source, not two: a second population is how two numbers
for the same thing end up on the same page.

★ FIVE HONESTY RULES, each of which is a defect we shipped this week.

1. SELF-TRAFFIC IS EXCLUDED, AND THE EXCLUSION IS IMPORTED. #2252 exists
   because a published p50 had no externality filter and ~80% of its
   population was our own probes — including the refresh that generated the
   rows on that very page. The verdict here is composed from
   external_platform_predicate + real_ua_predicate, IMPORTED from
   mcp_calls_deloop, never a copied list, which is how regex twins drift.

2. NEVER-USED IS NULL, NOT ZERO. A cohort nobody has tagged renders
   status="never_used" with nulls throughout. Zero says "we measured, and
   found none"; null says "nobody has used this yet". Copilot will read this
   table the day they ship, when six of seven tags legitimately have no data.

3. RETENTION DIVIDES BY THE PRIOR COHORT. The main retention lane divided by
   the CURRENT window and published 14.6% where the truth was 8.4% (#2267).
   That is not a retention rate — it flatters us whenever the fleet shrinks.
   We import that fix's own helper, _retention_pct, rather than re-deriving
   it, and publish the denominator in the sentence.

4. UNTAGGED IS ITS OWN LABELLED BUCKET. Untagged calls are neither folded
   into a cohort nor dropped from totals — today they are 100% of traffic,
   and a reader must see that. `totals.reconciles` proves nothing vanished.

5. VOLUME SITS BESIDE EVERY RATE, AND CAUSALITY IS GATED. execute_plan runs
   ~24 calls/7d in total. A cohort with 3 calls proves nothing, and a 100%
   retention rate on 1 agent is noise wearing a decimal point. `comparison`
   states whether the data can support a between-cohort claim at all, and it
   will say no for a long time.
"""

import datetime as _dt

from flask import Blueprint, jsonify, request

from mcp_calls_deloop import external_platform_predicate, real_ua_predicate
from routes.agent_retention_master_shell import _retention_pct
from routes.brain_ascension_master_shell import _admin_ok, _conn

cohort_measurement_bp = Blueprint("cohort_measurement", __name__)

# The contract Copilot locked in. Declared here so a tag that has never been
# used still gets a ROW (status="never_used") instead of silently not existing
# — the difference between "nobody used it" and "we never asked".
DECLARED_COHORTS = (
    "cohort.front_door",
    "cohort.delta_first",
    "cohort.composite_first",
    "cohort.saved_work_first",
    "cohort.grid_first",
    "cohort.fiber_first",
    "cohort.deals_first",
)

# Same shape the mcp-server validates to (_normalizeCohort). Restated as SQL so
# a tag written before that validation shipped, or by any other client, cannot
# widen a bucket here.
_TAG_RE = r"^[a-z0-9._-]{1,64}$"

# Below this, a cohort's numbers are reported but MUST NOT be compared against
# another cohort's. Chosen against live volume: execute_plan is ~24 calls/7d in
# total, so any threshold that lets today's data support a causal claim is a
# threshold set to produce one.
_MIN_CALLS_FOR_COMPARISON = 30
_MIN_AGENTS_FOR_COMPARISON = 10

_DEFAULT_DAYS = 7
_MAX_DAYS = 90


def _cohort_filters(days: int) -> list[str]:
    """Every WHERE clause applied to the cohort population, in order.

    Returned as a list so it can be BOTH joined into the query and published
    verbatim — the published filters must BE the filters, not a hand-written
    description of them that drifts from the SQL underneath (#2253).

    ★ external_platform_predicate contains a literal % (LIKE). That is safe
    ONLY because the execute() below passes NO bound params — psycopg2
    interprets % only when parameters are supplied. `days` is interpolated as a
    validated int for exactly this reason. Do not add a bound parameter to that
    call without switching to internal_tag_regex_predicate(), the bound-safe
    twin rendered from the same constants.

    The window spans 2*days because retention needs the PRIOR window in the
    same pass; the current/prior split happens in the query, not here.
    """
    return [
        "i.tool_name = 'execute_plan'",
        "i.is_public_ip",
        "i.is_real_external",
        external_platform_predicate("i.platform"),
        real_ua_predicate("i.user_agent"),
        f"i.created_at >= now() - interval '{int(days) * 2} days'",
    ]


def _cohort_population(days: int) -> dict:
    """What is counted, in prose and in the exact SQL that counts it."""
    return {
        "observations": "execute_plan tool calls carrying a cohort tag",
        "window": f"{days} days, rolling, ending now",
        "prior_window": f"the {days} days immediately before that",
        "source": "mcp_calls_identity JOIN mcp_tool_calls (for params)",
        "agent_identity": (
            "agent_id from mcp_calls_identity (md5 of the first public "
            "X-Forwarded-For hop). Rows whose first hop is a Cloudflare POP "
            "carry agent_id NULL and are counted in `calls` but excluded from "
            "every AGENT count — a POP is not an agent"
        ),
        "includes": "external callers only, keyed and keyless alike",
        "excludes": (
            "DC Hub's own platforms (dchub-*, probes, test clients), "
            "registry crawlers, and scripted/internal user-agents"
        ),
        "returning_definition": (
            "an agent is RETURNING for a cohort if it called execute_plan "
            "under THAT SAME tag in both the current and the prior window. "
            "Cross-cohort returns are deliberately not counted as retention "
            "for either cohort — the question each tag exists to answer is "
            "whether THAT routing brings an agent back"
        ),
        "retention_denominator": (
            "the PRIOR window's cohort, never the current one. Dividing by "
            "the current window is not a retention rate: it rises when the "
            "fleet shrinks. That defect published 14.6% where the truth was "
            "8.4% (#2267); this endpoint imports that fix's helper"
        ),
        "cohort_extraction": (
            "params ->> 'cohort', parsed only when pg_input_is_valid(params, "
            "'jsonb') — mcp_tool_calls.params is TEXT truncated at 4000 "
            "chars, so a long intent could store JSON that does not parse. "
            "Such a row counts as UNTAGGED; it is never dropped"
        ),
        "sql_filters": _cohort_filters(days),
    }


_SQL = """
WITH base AS (
  SELECT i.agent_id,
         (i.created_at >= now() - interval '{d} days') AS in_cur,
         CASE WHEN pg_input_is_valid(t.params, 'jsonb')
              THEN lower(btrim(coalesce(t.params::jsonb ->> 'cohort', '')))
              ELSE '' END AS raw_cohort
    FROM mcp_calls_identity i
    JOIN mcp_tool_calls t ON t.id = i.id
   WHERE {where}
), tagged AS (
  SELECT agent_id, in_cur,
         CASE WHEN raw_cohort ~ '{tag_re}' THEN raw_cohort END AS cohort
    FROM base
)
SELECT COALESCE(cohort, '') AS bucket,
       COUNT(*) FILTER (WHERE in_cur)                     AS calls_cur,
       COUNT(*) FILTER (WHERE NOT in_cur)                 AS calls_prior,
       COUNT(DISTINCT agent_id) FILTER (
             WHERE in_cur AND agent_id IS NOT NULL)       AS agents_cur,
       COUNT(DISTINCT agent_id) FILTER (
             WHERE NOT in_cur AND agent_id IS NOT NULL)   AS agents_prior,
       COUNT(DISTINCT agent_id) FILTER (
             WHERE in_cur AND agent_id IS NOT NULL
               AND agent_id IN (SELECT x.agent_id FROM tagged x
                                 WHERE NOT x.in_cur
                                   AND x.agent_id IS NOT NULL
                                   AND x.cohort IS NOT DISTINCT FROM tagged.cohort)
       ) AS returning_agents
  FROM tagged
 GROUP BY 1
"""


def _never_used_row(tag: str) -> dict:
    """A declared cohort nobody has tagged yet.

    Every count is null, not 0. This is rule 2, and it is the row Copilot will
    see for six of seven tags on day one: "no call has carried this tag" is a
    different statement from "this tag was used and nobody came back".
    """
    return {
        "cohort": tag,
        "status": "never_used",
        "declared": True,
        "calls": None,
        "calls_prior_window": None,
        "agents": None,
        "prior_agents": None,
        "returning_agents": None,
        "retention_pct": None,
        "retention_basis": "never used — no execute_plan call has carried this tag",
        "comparable": False,
    }


def _measured_row(tag: str, r: dict, declared: bool) -> dict:
    calls, agents = r["calls_cur"], r["agents_cur"]
    prior, returning = r["agents_prior"], r["returning_agents"]
    pct = _retention_pct(returning, prior)

    if pct is None:
        basis = (
            f"retention UNMEASURED: no agent used this tag in the prior "
            f"{r['days']}d window, so there was nobody who could return. "
            f"Not 0%"
        )
    else:
        basis = (
            f"{returning} of the prior window's {prior} agents returned "
            f"= {pct:.1f}%"
        )

    comparable = (calls >= _MIN_CALLS_FOR_COMPARISON
                  and agents >= _MIN_AGENTS_FOR_COMPARISON)
    return {
        "cohort": tag or "(untagged)",
        "status": "measured",
        "declared": declared,
        "calls": calls,
        "calls_prior_window": r["calls_prior"],
        "agents": agents,
        "prior_agents": prior,
        "returning_agents": returning,
        "retention_pct": None if pct is None else round(pct, 1),
        "retention_basis": basis,
        "comparable": comparable,
        **({} if comparable else {"not_comparable_because": (
            f"{calls} calls / {agents} agents is below the "
            f"{_MIN_CALLS_FOR_COMPARISON}-call, {_MIN_AGENTS_FOR_COMPARISON}-"
            f"agent floor this endpoint requires before one cohort may be "
            f"compared against another"
        )}),
    }


@cohort_measurement_bp.route("/api/v1/admin/mcp/cohorts", methods=["GET"])
@cohort_measurement_bp.route("/api/v1/mcp/cohorts", methods=["GET"])
def mcp_cohorts():
    # ADMIN-GATED, matching the nearest peer. The agent-retention lane —
    # which publishes this same class of data (agent counts, returning
    # agents, retention rates) — sits behind _admin_ok() at
    # /api/v1/admin/agent-retention. This endpoint additionally exposes
    # absolute execute_plan traffic volume, which is commercially
    # unflattering at current scale, so a public default would have been a
    # quiet disclosure decision made by omission. canonical-benchmarks is
    # public, but that is a DELIBERATE partner-facing publication of one
    # latency statistic; this is an internal measurement surface. Copilot
    # does not read it directly — we report from it.
    if not _admin_ok():
        return jsonify({"ok": False, "error": "admin key required"}), 401
    try:
        days = int(request.args.get("days", _DEFAULT_DAYS))
    except (TypeError, ValueError):
        days = _DEFAULT_DAYS
    days = max(1, min(_MAX_DAYS, days))

    out = {
        "report": "mcp-cohorts",
        "version": "1.0",
        "window_days": days,
        "generated_at": _dt.datetime.now(_dt.timezone.utc)
                           .isoformat(timespec="seconds").replace("+00:00", "Z"),
        "what_this_is": (
            "Per-cohort execute_plan adoption and retention. A cohort is an "
            "OPTIONAL tag the caller sets; it has no effect on routing, "
            "planning or results, so these rows compare how agents were "
            "ROUTED, never what they were served."
        ),
        "population": _cohort_population(days),
        "cohorts": None,
        "untagged": None,
        "totals": None,
        "comparison": None,
        "reason": None,
    }

    c = _conn()
    if c is None:
        # Nulls stay nulls. An unreachable database means every count is
        # UNKNOWN; rendering zeros would state that we measured and found none.
        out["reason"] = "db unavailable — cohort counts unknown, not zero"
        return jsonify(out), 200

    try:
        with c.cursor() as cur:
            # NO BOUND PARAMS — see _cohort_filters(). `days` is an int already
            # clamped to [1, 90]; the predicates carry literal % (LIKE) and
            # psycopg2 would eat them the moment a parameter is supplied.
            cur.execute(_SQL.format(
                d=days, where=" AND ".join(_cohort_filters(days)),
                tag_re=_TAG_RE))
            rows = {}
            for (bucket, cc, cp, ac, ap, ret) in cur.fetchall():
                rows[bucket or ""] = {
                    "calls_cur": int(cc or 0), "calls_prior": int(cp or 0),
                    "agents_cur": int(ac or 0), "agents_prior": int(ap or 0),
                    "returning_agents": int(ret or 0), "days": days,
                }
    except Exception as e:  # noqa: BLE001 — a read endpoint never 500s the board
        out["reason"] = f"query failed — counts unknown, not zero: {str(e)[:200]}"
        return jsonify(out), 200
    finally:
        try:
            c.close()
        except Exception:
            pass

    # Declared cohorts first, in contract order, so the table shape is stable
    # whether or not anyone has shipped yet.
    cohorts = [
        _measured_row(t, rows[t], True) if t in rows else _never_used_row(t)
        for t in DECLARED_COHORTS
    ]
    # A tag we did not declare but which really appeared. Surfaced rather than
    # discarded: an undeclared tag is either a typo worth seeing or a partner
    # shipping ahead of the contract — both are facts, neither is nothing.
    unexpected = sorted(k for k in rows if k and k not in DECLARED_COHORTS)
    cohorts += [_measured_row(t, rows[t], False) for t in unexpected]

    untagged = (_measured_row("", rows[""], False) if "" in rows else {
        "cohort": "(untagged)", "status": "none_in_window", "declared": False,
        "calls": 0, "calls_prior_window": 0, "agents": 0, "prior_agents": 0,
        "returning_agents": 0, "retention_pct": None,
        "retention_basis": "no untagged execute_plan calls in this window",
        "comparable": False,
    })

    tagged_calls = sum(r["calls"] or 0 for r in cohorts)
    untagged_calls = untagged["calls"] or 0
    total_calls = sum(v["calls_cur"] for v in rows.values())

    out["cohorts"] = cohorts
    out["untagged"] = untagged
    out["totals"] = {
        "execute_plan_calls": total_calls,
        "tagged_calls": tagged_calls,
        "untagged_calls": untagged_calls,
        # Rule 4 made checkable: if this is ever false, a bucket went missing.
        "reconciles": (tagged_calls + untagged_calls) == total_calls,
        "tagged_share_pct": (round(100.0 * tagged_calls / total_calls, 1)
                             if total_calls else None),
        "note": (
            "tagged + untagged == execute_plan_calls, by construction. "
            "Untagged is a labelled bucket, never folded into a cohort."
        ),
    }

    comparable = [r for r in cohorts if r.get("comparable")]
    out["comparison"] = {
        "ready": len(comparable) >= 2,
        "comparable_cohorts": [r["cohort"] for r in comparable],
        "min_calls_per_cohort": _MIN_CALLS_FOR_COMPARISON,
        "min_agents_per_cohort": _MIN_AGENTS_FOR_COMPARISON,
        "reason": (
            "at least two cohorts clear the volume floor — differences may be "
            "described, though this is an observational split and not a "
            "randomised trial, so it still does not establish cause"
            if len(comparable) >= 2 else
            f"fewer than two cohorts clear the volume floor "
            f"({_MIN_CALLS_FOR_COMPARISON} calls / "
            f"{_MIN_AGENTS_FOR_COMPARISON} agents). Report each cohort's "
            f"counts; do NOT attribute a difference between them to routing"
        ),
    }
    return jsonify(out), 200
