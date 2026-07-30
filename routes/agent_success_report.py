"""agent_success_report.py — the public weekly Agent Success Report (2026-07-30).

ChatGPT's proposal from the 07-30 partner round, restructured the same day to
its round-3 design constraints (Perplexity converged on the same artifact —
"the activation board IS this report"). The requirements, verbatim enough:

  1. STRUCTURE AROUND CONTRACTS, NOT COUNTS — five sections:
       Reach              which ecosystems discovered DC Hub
       Activation         did connected agents actually call
       Planner adoption   did they choose the intended entry point
       Execution quality  did workflows complete with integrity
       Learning           what changed this week that improved/degraded
                          behaviour — and the report ENDS here, on what the
                          numbers taught us, not on numbers.
     (sections are an ARRAY: JSON object keys carry no order, and Flask may
     sort them — an array is the only way "learning comes last" survives
     serialization.)
  2. PUBLISH GATE RULE — "no derived metric may appear unless its
     observation, assumptions, definition version, and consumer are all
     declared." Enforced fail-closed in _metric_block: a registry entry
     missing any contract field renders UNPUBLISHABLE with no value.
  3. NO COMPOSITE "HEALTH SCORES" — explicitly refused. The week's work
     separated observation/policy, execution/integrity, adoption/
     orchestration; one blended number would collapse exactly those
     distinctions. The payload says so, and a test pins it.
  4. Second-recipe take-up joins time-to-first-result in Activation (both
     computable off the existing episode model). Recipe-LIFECYCLE logging
     needs a schema change and stays out of scope here, on purpose.

ROUND-2 FEEDBACK (same day, after the partners read the live payload):
  · ChatGPT: UNPUBLISHABLE now carries a human-readable publish_block_reason
    (auditable, not just machine-parsable); the Learning section splits
    MEASUREMENT INTEGRITY from BEHAVIOUR so a telemetry repair can never be
    read as a change in agent behaviour; a top-level `confidence` block
    DECLARES why each axis should or shouldn't be trusted (states are words,
    never a synthetic percentage — same philosophy as refusing composites);
    and publish_contract states the invariant: a definition_version change
    must never silently alter historical interpretation.
  · Copilot: the metric contract is machine-readable at
    GET /api/v1/reports/agent-success/contract — GENERATED from
    PUBLISH_CONTRACT_FIELDS (derived, never transcribed, the drift class this
    repo keeps paying for). Its semver ask was DECLINED: versions here are
    integers because ANY meaning change bumps (MAJOR-only semantics), and a
    format migration would break pinned consumers for zero semantic gain.

Every number on this surface is crawler-excluded. That is the report's whole
reason to exist as a separate endpoint: the 07-28 audit found the "real agent"
init population dominated by MCP registry crawlers/indexers/health checkers,
and this codebase has retracted enough headline numbers (86 agents, 35-41k
calls, 89.4% one-and-gone) to know that a public metric starts from the
excluded view or it starts wrong.

SOURCES — exactly two, both pre-existing:
  · mcp_calls_identity — the canonical crawler-excluded identity VIEW
    (rendered from mcp_calls_deloop by scripts/render_identity_views.py;
    registry-crawler families applied to Neon 2026-07-28 22:12Z, 9/9 verified).
    Supplies: tool calls, active agents, median time-to-first-result, episode
    result rate, and the generic-bucket share that gates the per-platform
    split.
  · the planner-bypass agent-day episode model (routes/planner_bypass.py,
    DEFINITION_VERSION 2) — planner adoption vs manual orchestration with the
    observed/judgement split, and the second-recipe query built from the same
    episode identity. Reused with the registry-crawler exclusions ADDED
    (regex-form predicates, because those queries run with bound params where
    a literal LIKE % eats an argument). The population therefore differs from
    the admin endpoint's — each metric declares that in its own contract.

HOUSE RULES ENFORCED HERE (tests pin all of them):
  · every rate is None + status UNMEASURED on an empty denominator;
  · every metric carries the full publish contract (observation, assumptions,
    definition_version + changelog, consumers, definition, unit, source);
  · per-platform splits are GATED AT BIRTH: the attribution fix
    (client_name_raw on the stateless path, dchub-mcp-server 2026-07-28)
    needs ~7 days of accumulation AND a verified drop of the generic-bucket
    share before a per-platform number is publishable. The gate is encoded,
    not remembered, and its state ships in the payload either way.

Endpoint (public, read-only, cached):
  GET /api/v1/reports/agent-success

Stale-while-revalidate cache, single-flight refresh (same shape as
/api/v1/reach): a public DB-backed endpoint must never let a crawler stampede
reach the pool (the sitemap incident). Kill switch:
AGENT_SUCCESS_REPORT_DISABLE=1.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from datetime import date, datetime, timezone

from flask import Blueprint, jsonify

from mcp_calls_deloop import (
    PLATFORM_CASE,
    internal_tag_regex_predicate,
    real_ua_predicate,
)
from routes.planner_bypass import (
    DEFINITION_VERSION as EPISODE_MODEL_VERSION,
    FRONT_DOOR,
    _EPISODE_ID,
    _SYNTH_NOT_LIKE,
    _measure as _episode_measure,
    _rate,
)

logger = logging.getLogger("agent_success_report")
agent_success_report_bp = Blueprint("agent_success_report", __name__)

WINDOW_DAYS = 7
_STMT_TIMEOUT_MS = 6000

# The payload SHAPE version. Individual metrics carry their own versions —
# bump those when a metric's MEANING changes; bump this when the envelope does.
REPORT_DEFINITION_VERSION = 3
REPORT_DEFINITION_CHANGELOG = {
    1: "initial (PR #1954, 2026-07-30 morning): flat metrics dict; five "
       "deliverable metrics; per-platform gate; crawler-excluded population",
    2: "round-3 partner requirements (same day): five contract sections — "
       "reach → activation → planner adoption → execution quality → learning, "
       "and the report ENDS with learning; publish contract (observation / "
       "assumptions / definition_version / consumers) required on every "
       "derived metric, fail-closed as UNPUBLISHABLE; activation gains "
       "second_recipe_take_up_pct, execution quality gains "
       "episode_result_rate, learning carries week-over-week deltas plus "
       "dated notes; composite scores explicitly refused",
    3: "round-2 feedback (same day, partners read the live payload): "
       "UNPUBLISHABLE carries publish_block_reason (ChatGPT); learning splits "
       "measurement_integrity from behaviour so a telemetry repair can never "
       "read as behaviour change (ChatGPT — attribution trend lives under "
       "measurement_integrity because it gauges measurement quality, not "
       "agent behaviour); declared-not-scored confidence block (ChatGPT); "
       "no-silent-reinterpretation invariant stated in publish_contract "
       "(ChatGPT); machine-readable metric contract at /contract (Copilot); "
       "integer version format documented, semver declined — any meaning "
       "change bumps, so versions are MAJOR-only by construction",
}

# Round-3 rule 2, made operational: a metric renders only when its whole
# semantic contract is declared. Missing any of these ⇒ UNPUBLISHABLE.
PUBLISH_CONTRACT_FIELDS = (
    "definition", "observation", "assumptions", "definition_version",
    "definition_changelog", "consumers", "unit", "source",
)

# Round-3 rule 1: fixed narrative order, learning LAST.
SECTION_ORDER = ("reach", "activation", "planner_adoption",
                 "execution_quality", "learning")

SECTION_QUESTIONS = {
    "reach": "Which ecosystems discovered DC Hub?",
    "activation": "Did connected agents actually call?",
    "planner_adoption": "Did agents choose the intended entry point?",
    "execution_quality": "Did workflows complete with integrity?",
    "learning": "What changed this week that improved or degraded behaviour?",
}

# Round-3 rule 3. Present in the payload so its absence can never be read as
# an oversight, and pinned by a test so nobody "helpfully" adds one.
NO_COMPOSITE_POLICY = (
    "REFUSED by design (round-3 partner rule): no blended 'agent success "
    "score' exists on this surface. Observation is separated from policy, "
    "execution from integrity, adoption from orchestration — a composite "
    "would collapse exactly those distinctions."
)

_CONSUMERS_DEFAULT = [
    "AI-partner weekly rounds (7 platforms)",
    "public consumers of /api/v1/reports/agent-success",
]

# ── Crawler exclusions for the EPISODE queries (mcp_call_log) ──────────────
# The identity view already carries the exclusions for everything read from
# mcp_calls_identity. The episode queries read mcp_call_log WITH bound params,
# so they get the regex twins of the same shared family constants. Regex form
# is load-bearing: the LIKE form's literal % would be consumed by psycopg2
# paramstyle substitution (the trap that took /api/v1/map down 2026-07-17).
CRAWLER_EXCLUSION_WHERE = (
    " AND " + internal_tag_regex_predicate("platform")
    + " AND " + real_ua_predicate("user_agent")
)

# ── Per-platform attribution gate ──────────────────────────────────────────
# The fix that makes per-platform splits meaningful (client_name_raw threaded
# on the stateless path + _PLATFORM_RECALL) landed in dchub-mcp-server on this
# date. Before it, ~88-90% of real calls landed in the generic bucket — a
# split over that would be a split of the unattributed remainder, i.e. noise
# published with confidence.
#
# ★ THE BUCKET RENAMED THE SAME DAY. The baseline was measured when the
# classifier labelled generic traffic 'mcp'; a 07-28 PLATFORM_CASE change
# routes client_name IN ('mcp','mcp-client','client','default') into its own
# real bucket 'mcp-generic-client'. Gate on the FAMILY, not the old label —
# `= 'mcp'` reads 0.0% against live data (verified 2026-07-30: the live split
# shows mcp-generic-client 3,436 of 4,416 calls = 77.8%, and zero 'mcp' rows)
# and would have silently false-opened the gate on day 7.
GENERIC_BUCKETS = ("mcp", "mcp-generic-client")
ATTRIBUTION_FIX_DATE = date(2026, 7, 28)
ATTRIBUTION_MIN_ACCUMULATION_DAYS = 7          # full window must be post-fix
MCP_BUCKET_SHARE_PRE_FIX = 0.88                # 3,179/3,623 measured 07-28
MCP_BUCKET_MAX_SHARE_TO_PUBLISH = 0.80         # "actually dropped" threshold


def _attribution_gate(days_since_fix, mcp_share):
    """(passed, status, reason). Pure — fully unit-testable.

    Both conditions must hold: the trailing window is entirely post-fix, AND
    the generic-client bucket share measurably dropped below the pre-fix
    baseline. Either alone is not evidence the split is honest: early data
    still measures the old writer, and an aged window with an unchanged share
    means the fix did not take."""
    if days_since_fix < ATTRIBUTION_MIN_ACCUMULATION_DAYS:
        return (False, "GATED_ACCUMULATING",
                f"attribution fix landed {ATTRIBUTION_FIX_DATE.isoformat()}; "
                f"{days_since_fix}d of ≥{ATTRIBUTION_MIN_ACCUMULATION_DAYS}d "
                "post-fix accumulation — a 7d split computed now would mostly "
                "measure pre-fix writes")
    if mcp_share is None:
        return (False, "GATED_ATTRIBUTION_UNVERIFIED",
                "generic-client bucket share could not be measured — cannot "
                "verify the attribution fix took, so the split stays gated")
    if mcp_share > MCP_BUCKET_MAX_SHARE_TO_PUBLISH:
        return (False, "GATED_ATTRIBUTION_UNVERIFIED",
                f"generic-client bucket ({'/'.join(GENERIC_BUCKETS)}) still holds "
                f"{round(100 * mcp_share, 1)}% of real calls (pre-fix baseline "
                f"~{round(100 * MCP_BUCKET_SHARE_PRE_FIX)}%, publish threshold "
                f"≤{round(100 * MCP_BUCKET_MAX_SHARE_TO_PUBLISH)}%) — the share "
                "has not verifiably dropped, so a per-platform split would "
                "still be a split of the unattributed remainder")
    return (True, "MEASURED", "attribution accumulation and bucket-share gates passed")


def _wow_pct(cur_v, prev_v):
    """Week-over-week delta, or None when the prior window is empty — a delta
    off nothing is not 100%, it is unmeasurable."""
    if cur_v is None or not prev_v:
        return None
    return round(100.0 * (cur_v - prev_v) / prev_v, 1)


# ── Metric registry — the versioned contract this surface publishes ────────
# Every metric block in the payload is rendered from THIS registry through
# _metric_block(), which fail-closes on an incomplete contract. Bump a
# metric's version whenever its MEANING changes, even if name and type do not
# — the name/type-stable case is exactly the one that bit agent_adoption's
# planner_first consumer.
_EPISODE_ASSUMPTIONS = [
    "execute_plan availability is ASSUMED, not measured — a client's "
    "allowed_tools scoping is invisible to the server",
    "multi-capability intent is INFERRED from behaviour; the user's question "
    "is never logged",
    "hand-off is APPROXIMATED from params (a later call carrying a chaining "
    "key the first call lacked); results are not logged, so true dataflow is "
    "unobservable",
]

METRICS = {
    "tool_calls_7d": {
        "definition": "COUNT(*) over mcp_calls_identity WHERE is_real_external, "
                      "trailing 7 days. Counts tool CALLS, never sessions.",
        "observation": "rows in the crawler-excluded identity view inside the "
                       "window — direct count, nothing derived",
        "assumptions": ["a call is one tracked tool invocation; client retries "
                        "are separate calls"],
        "unit": "calls",
        "source": "mcp_calls_identity (crawler-excluded view over mcp_tool_calls)",
        "consumers": _CONSUMERS_DEFAULT,
        "definition_version": 1,
        "definition_changelog": {
            1: "initial — identity-view population (internal traffic, scripted "
               "UAs, QA tags and registry/health/scanner crawlers excluded; "
               "r-registry-crawlers families applied 2026-07-28)",
        },
    },
    "active_agents_7d": {
        "definition": "COUNT(DISTINCT agent_id) over mcp_calls_identity WHERE "
                      "is_real_external AND is_public_ip, trailing 7 days.",
        "observation": "distinct md5(first public X-Forwarded-For hop) values "
                       "among real rows; Cloudflare-POP hops carry NULL "
                       "agent_id and are never counted (their calls still are)",
        "assumptions": ["one NAT/proxy egress IP = one agent — a shared egress "
                        "undercounts, a rotating one overcounts"],
        "unit": "distinct agents",
        "source": "mcp_calls_identity",
        "consumers": _CONSUMERS_DEFAULT,
        "definition_version": 1,
        "definition_changelog": {
            1: "initial — same agent grain as /api/v1/reach real_agents_7d "
               "(never session_id, which rotates per connection)",
        },
    },
    "planner_adoption_pct": {
        "definition": "Of agent-day episodes with 2+ calls (opportunities), the "
                      "share whose FIRST call was execute_plan. Pure observation "
                      "— no judgement about whether the planner SHOULD have "
                      "been used.",
        "observation": "first tool name per agent-day episode, episodes with "
                       "2+ calls in the window",
        "assumptions": _EPISODE_ASSUMPTIONS,
        "unit": "% of opportunity episodes",
        "source": f"planner-bypass episode model v{EPISODE_MODEL_VERSION} "
                  "(mcp_call_log, agent-day unit) + registry-crawler exclusions",
        "consumers": _CONSUMERS_DEFAULT + ["activation board (specced)"],
        "definition_version": 1,
        "definition_changelog": {
            1: "initial — episode model v2 semantics (agent-day unit; durable "
               "api_key first, session fallback) with the registry-crawler "
               "exclusions ADDED. Population therefore differs from "
               "/api/v1/admin/planner-bypass, which publishes the un-excluded "
               "fleet view under its own version.",
        },
    },
    "manual_orchestration_pct": {
        "definition": "Of opportunity episodes, the share that hand-chained: 2+ "
                      "DISTINCT tools AND a hand-off signal (a later call carrying "
                      "a planner-contract chaining key the first call lacked), "
                      "without execute_plan first. Benign fan-out (same tool, "
                      "different args) and single-capability lookups are excluded "
                      "and reported separately — they are correct usage, not "
                      "bypass.",
        "observation": "distinct-tool counts and param-key hand-off signals per "
                       "agent-day episode",
        "assumptions": _EPISODE_ASSUMPTIONS,
        "unit": "% of opportunity episodes",
        "source": f"planner-bypass episode model v{EPISODE_MODEL_VERSION} "
                  "(mcp_call_log, agent-day unit) + registry-crawler exclusions",
        "consumers": _CONSUMERS_DEFAULT,
        "definition_version": 1,
        "definition_changelog": {
            1: "initial — episode model v2 semantics with the registry-crawler "
               "exclusions ADDED (population differs from the admin endpoint); "
               "observed shape only, the bypass JUDGEMENT stays on the admin "
               "surface",
        },
    },
    "median_time_to_first_result_ms": {
        "definition": "Per agent-day episode: milliseconds from the episode's "
                      "first tool call to completion of its first SUCCESSFUL "
                      "call (arrival gap + that call's response_time_ms). "
                      "Median across episodes with at least one successful "
                      "call; episodes with none are counted separately, never "
                      "averaged in as zero.",
        "observation": "per-episode first-call timestamp, first-success "
                       "timestamp and its response_time_ms, identity grain",
        "assumptions": [
            "success is writer-reported: a tracker payload with no status "
            "field records success=TRUE, so 'first result' means 'first call "
            "not explicitly reported failed'",
            "response_time_ms=0 rows contribute the arrival gap alone",
        ],
        "unit": "milliseconds (median)",
        "source": "mcp_calls_identity (agent_id × day grain)",
        "consumers": _CONSUMERS_DEFAULT + ["activation board (specced)"],
        "definition_version": 1,
        "definition_changelog": {
            1: "initial — Perplexity's activation-board metric, computed off "
               "the episode model as converged in round 3",
        },
    },
    "second_recipe_take_up_pct": {
        "definition": "Of agent-day episodes that invoked execute_plan at least "
                      "once, the share that invoked it AGAIN with a different "
                      "non-empty intent inside the same episode — the starter "
                      "pack's next_recipe actually taken.",
        "observation": "distinct non-empty execute_plan intent strings per "
                       "agent-day episode",
        "assumptions": [
            "'recipe' = a distinct execute_plan intent string; two phrasings "
            "of one question read as two recipes",
            "cross-day returns are retention, not take-up — this measures "
            "within the same agent-day episode",
            "empty intents never count as a distinct recipe",
        ],
        "unit": "% of episodes with ≥1 execute_plan call",
        "source": "mcp_call_log (planner-bypass episode identity) + "
                  "registry-crawler exclusions",
        "consumers": _CONSUMERS_DEFAULT + ["activation board (specced)"],
        "definition_version": 1,
        "definition_changelog": {
            1: "initial — the second Perplexity activation metric computable "
               "without schema changes. Recipe-LIFECYCLE logging (did the "
               "recipe COMPLETE) needs a schema change and is explicitly out "
               "of scope here.",
        },
    },
    "episode_result_rate": {
        "definition": "Share of agent-day episodes (identity grain) whose "
                      "calls produced at least one successful result in the "
                      "window.",
        "observation": "episodes vs episodes-with-a-success from the same "
                       "query that feeds median_time_to_first_result_ms",
        "assumptions": [
            "identity grain (md5 of first public XFF hop), NOT the planner "
            "episode grain — the two units are declared separately on purpose",
            "success is writer-reported (see median_time_to_first_result_ms)",
        ],
        "unit": "% of agent-day episodes",
        "source": "mcp_calls_identity (agent_id × day grain)",
        "consumers": _CONSUMERS_DEFAULT,
        "definition_version": 1,
        "definition_changelog": {
            1: "initial — execution-quality floor: a workflow that never got "
               "one successful result back did not complete with integrity",
        },
    },
    "tool_calls_wow_pct": {
        "definition": "Percent change of crawler-excluded tool calls vs the "
                      "prior rolling 7-day window (days 8-14).",
        "observation": "the two window counts; the delta is arithmetic on top",
        "assumptions": [
            "both windows measured under the SAME definitions — a definition "
            "change between windows would masquerade as behaviour change, "
            "which is what definition_version exists to catch",
        ],
        "unit": "% change week-over-week",
        "source": "mcp_calls_identity",
        "consumers": _CONSUMERS_DEFAULT,
        "definition_version": 1,
        "definition_changelog": {
            1: "initial — None (UNMEASURED) when the prior window is empty; a "
               "delta off nothing is not +100%",
        },
    },
    "active_agents_wow_pct": {
        "definition": "Percent change of distinct real agents vs the prior "
                      "rolling 7-day window (days 8-14).",
        "observation": "the two window distinct-agent counts",
        "assumptions": [
            "same-definition windows (see tool_calls_wow_pct)",
            "agent identity is IP-derived; egress churn between windows adds "
            "noise both directions",
        ],
        "unit": "% change week-over-week",
        "source": "mcp_calls_identity",
        "consumers": _CONSUMERS_DEFAULT,
        "definition_version": 1,
        "definition_changelog": {
            1: "initial — None (UNMEASURED) when the prior window is empty",
        },
    },
}

# ── SQL — identity-view queries run with NO bound params ───────────────────
# Window and thresholds are trusted module constants inlined as literals, so
# psycopg2 performs no %-substitution and PLATFORM_CASE's ILIKE '%…%' literals
# are safe exactly as they are in mcp_calls_deloop itself.
_W = f"created_at >= NOW() - ({WINDOW_DAYS} * INTERVAL '1 day')"
_W_PREV = (f"created_at >= NOW() - ({2 * WINDOW_DAYS} * INTERVAL '1 day') "
           f"AND created_at < NOW() - ({WINDOW_DAYS} * INTERVAL '1 day')")

_SQL_TOTALS = f"""
SELECT COUNT(*) FILTER (WHERE is_real_external)                        AS tool_calls,
       COUNT(DISTINCT agent_id) FILTER (WHERE is_real_external
                                          AND is_public_ip)            AS active_agents
  FROM mcp_calls_identity
 WHERE {_W}
"""

_SQL_TOTALS_PREV = f"""
SELECT COUNT(*) FILTER (WHERE is_real_external)                        AS tool_calls,
       COUNT(DISTINCT agent_id) FILTER (WHERE is_real_external
                                          AND is_public_ip)            AS active_agents
  FROM mcp_calls_identity
 WHERE {_W_PREV}
"""

_SQL_TTFR = f"""
WITH rows7 AS (
  SELECT agent_id, created_at::date AS day, created_at, success,
         COALESCE(response_time_ms, 0) AS rt
    FROM mcp_calls_identity
   WHERE {_W}
     AND is_real_external AND is_public_ip AND agent_id IS NOT NULL
),
ep AS (
  SELECT agent_id, day, MIN(created_at) AS first_ts
    FROM rows7 GROUP BY agent_id, day
),
ok1 AS (
  SELECT DISTINCT ON (agent_id, day) agent_id, day, created_at AS ok_ts, rt
    FROM rows7 WHERE success ORDER BY agent_id, day, created_at ASC
)
SELECT COUNT(*)                                          AS episodes,
       COUNT(o.ok_ts)                                    AS episodes_with_result,
       PERCENTILE_CONT(0.5) WITHIN GROUP (
         ORDER BY EXTRACT(EPOCH FROM (o.ok_ts - e.first_ts)) * 1000.0 + o.rt)
         FILTER (WHERE o.ok_ts IS NOT NULL)              AS median_ttfr_ms
  FROM ep e LEFT JOIN ok1 o USING (agent_id, day)
"""

# Gate evidence: the share of real calls whose canonical bucket is the generic
# FAMILY (see GENERIC_BUCKETS — the 07-28 classifier renamed the old 'mcp'
# label to 'mcp-generic-client'; matching only the old label reads 0.0%).
_GENERIC_IN = ", ".join(f"'{b}'" for b in GENERIC_BUCKETS)
_SQL_MCP_SHARE = f"""
SELECT COUNT(*)                                             AS real_calls,
       COUNT(*) FILTER (WHERE ({PLATFORM_CASE.strip()})
                          IN ({_GENERIC_IN}))               AS generic_bucket_calls
  FROM mcp_calls_identity
 WHERE {_W} AND is_real_external
"""

_SQL_PLATFORM_SPLIT = f"""
SELECT ({PLATFORM_CASE.strip()})                                  AS platform,
       COUNT(*)                                                   AS calls,
       COUNT(DISTINCT agent_id) FILTER (WHERE is_public_ip)       AS agents
  FROM mcp_calls_identity
 WHERE {_W} AND is_real_external
 GROUP BY 1 ORDER BY calls DESC LIMIT 15
"""

# ── Second-recipe take-up — episode grain over mcp_call_log, WITH params ───
# Built from the planner-bypass episode identity + synthetic exclusions plus
# the crawler regex predicates, so this and the adoption metrics cannot
# diverge on population. Runs with bound params (days, FRONT_DOOR) through
# _bounded_params — NEVER through _bounded, and PLATFORM_CASE must never
# appear here (its ILIKE literals are unsafe next to substitution).
_SQL_SECOND_RECIPE = """
WITH scoped AS (
  SELECT {episode} AS episode_id,
         timestamp::date AS day,
         COALESCE(params->>'intent', '') AS intent
    FROM mcp_call_log
   WHERE timestamp > NOW() - make_interval(days => %s)
     AND tool = %s
     AND ({episode}) <> 'sess:'
     {synth}
),
ep AS (
  SELECT episode_id, day,
         COUNT(*) AS plan_calls,
         COUNT(DISTINCT intent) FILTER (WHERE intent <> '') AS distinct_intents
    FROM scoped GROUP BY episode_id, day
)
SELECT COUNT(*)                                       AS plan_episodes,
       COUNT(*) FILTER (WHERE distinct_intents >= 2)  AS second_recipe_episodes
  FROM ep
"""


def _second_recipe_sql() -> str:
    return _SQL_SECOND_RECIPE.format(
        episode=_EPISODE_ID, synth=_SYNTH_NOT_LIKE + CRAWLER_EXCLUSION_WHERE)


# ── Learning notes — dated, sourced, kinds: improved | degraded |
# measurement | observed. Curated editorial state, deliberately versioned in
# git rather than invented at runtime: "what changed" is a claim about
# causes, and causes don't come out of a GROUP BY. Dated facts stay true
# after the week moves on.
#
# Round-2 (ChatGPT): every note also carries `class` — measurement_integrity
# vs behaviour — and the section renders the two groups separately, so a
# telemetry repair can never be mistaken for a change in what agents did.
LEARNING_NOTES = (
    {"date": "2026-07-28", "kind": "measurement", "class": "measurement_integrity",
     "note": "Registry-crawler exclusions applied to the identity views — 12 "
             "named crawlers/indexers/health checkers plus 9 family patterns "
             "removed from the 'real agent' population (measured impact: "
             "calls −4.1%, agents −3). Numbers spanning this date are not "
             "comparable without this note.",
     "source": "dchub-backend #1865/#1866, applied 2026-07-28 22:12Z"},
    {"date": "2026-07-28", "kind": "measurement", "class": "measurement_integrity",
     "note": "Generic client_name bucket split: rows named "
             "'mcp'/'mcp-client'/'client'/'default' now classify as "
             "'mcp-generic-client' (real traffic, kept) or 'internal-dchub' "
             "(self-traffic, excluded) instead of passing through verbatim as "
             "a fake platform.",
     "source": "dchub-backend mcp_calls_deloop, 2026-07-28"},
    {"date": "2026-07-28", "kind": "improved", "class": "measurement_integrity",
     "note": "Platform-attribution fix: clientInfo.name now survives the "
             "stateless call path. Generic-bucket share moved 88% → 77.8% in "
             "the first two days; per-platform reach publishes here once the "
             "share holds ≤80% across a fully post-fix week (earliest "
             "2026-08-04). This is a TELEMETRY repair — the agents did not "
             "change, our ability to see who they are did.",
     "source": "dchub-mcp-server (client_name_raw + platform recall)"},
    {"date": "2026-07-30", "kind": "improved", "class": "behaviour",
     "note": "Planner v5.6: tax-incentive intents route to get_tax_incentives "
             "— the tool was registered but unroutable (register ≠ routable), "
             "so Meta's #1 intent had been landing on facility_search. This "
             "changes what agents GET, so downstream behaviour may shift.",
     "source": "dchub-mcp-server #106"},
    {"date": "2026-07-30", "kind": "measurement", "class": "measurement_integrity",
     "note": "This report's episode metrics gained the same crawler "
             "exclusions as the identity views — the bound-params regex "
             "predicate had silently missed all nine crawler families for two "
             "days, so bound-params surfaces were still counting registry "
             "crawlers the identity view excluded.",
     "source": "dchub-backend #1954"},
    {"date": "2026-07-30", "kind": "observed", "class": "behaviour",
     "note": "First reading of the week: planner adoption 0.0% (0 of 202 "
             "opportunity episodes led with execute_plan) while manual "
             "orchestration ran 72.8%. The fleet is dominated by one "
             "generic-client cohort, so this may move sharply once "
             "attribution resolves who these agents are — read it with the "
             "confidence block, not alone.",
     "source": "this report, first live build 2026-07-30T08:09Z"},
    # Round-3 (ChatGPT): "the assistant's citations become probes for
    # documentation freshness … public representations are contracts too."
    # A stale landing page is the same defect class as a duplicated test
    # fragment or a stale operator prompt — a secondary representation of a
    # canonical truth. Capture such observations here whenever they occur.
    {"date": "2026-07-30", "kind": "observed", "class": "measurement_integrity",
     "note": "A partner's citation card exposed public-representation drift: "
             "/agent was serving '73 tools' live (canon: 81) while ChatGPT's "
             "cached citation card still read '48'. Nobody ran a validator — "
             "a normal answer's citation was the probe. Fix + heal-coverage "
             "for the page were spun off the same day; citation-exposed "
             "drift gets recorded here as a first-class signal whenever a "
             "partner's footer catches one of our surfaces stale.",
     "source": "ChatGPT round-3 exchange; /agent heal task, 2026-07-30"},
)


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


def _bounded(cur, sql, fetch="one"):
    """One aggregate per explicit transaction with SET LOCAL statement_timeout
    — the only form that sticks on Neon's pooled endpoint (see
    flask_mcp_endpoints._reach_bounded, verified live 2026-07-01). ROLLBACK on
    error so a timed-out query never poisons the next one. NO params by
    design: the identity queries inline PLATFORM_CASE, whose ILIKE '%…%'
    literals are only safe when no substitution runs."""
    cur.execute("BEGIN")
    try:
        cur.execute("SET LOCAL statement_timeout = %d" % _STMT_TIMEOUT_MS)
        cur.execute(sql)
        result = cur.fetchone() if fetch == "one" else cur.fetchall()
        cur.execute("COMMIT")
        return result
    except Exception:
        try:
            cur.execute("ROLLBACK")
        except Exception:
            pass
        raise


def _bounded_params(cur, sql, params, fetch="one"):
    """The WITH-params sibling of _bounded, for the mcp_call_log episode
    queries only. Their SQL must stay free of single-percent literals (the
    synthetic exclusions are %%-doubled; the crawler predicates are regex
    form) — tests emulate the substitution to hold that."""
    cur.execute("BEGIN")
    try:
        cur.execute("SET LOCAL statement_timeout = %d" % _STMT_TIMEOUT_MS)
        cur.execute(sql, params)
        result = cur.fetchone() if fetch == "one" else cur.fetchall()
        cur.execute("COMMIT")
        return result
    except Exception:
        try:
            cur.execute("ROLLBACK")
        except Exception:
            pass
        raise


def _metric_block(key, value, status, **extra):
    """Render one metric with its full publish contract attached — and refuse
    to render a value whose contract is incomplete (round-3 rule 2). A missing
    contract field is a build-time defect (tests assert the registry is
    complete), so UNPUBLISHABLE should never appear in production — but the
    gate is enforced here, not remembered."""
    entry = METRICS[key]
    missing = [f for f in PUBLISH_CONTRACT_FIELDS if not entry.get(f)]
    if missing:
        # Round-2 (ChatGPT): the block must be auditable by a HUMAN at a
        # glance, not reverse-engineered from a field list.
        reason = "missing contract declaration: " + ", ".join(missing)
        logger.error("[agent-success] %s UNPUBLISHABLE — %s", key, reason)
        return {"value": None, "status": "UNPUBLISHABLE",
                "publish_block_reason": reason,
                "missing_contract_fields": missing}
    block = {"value": value, "status": status}
    block.update(extra)
    block.update(entry)
    return block


def _build_report() -> dict:
    now = datetime.now(timezone.utc)
    days_since_fix = (now.date() - ATTRIBUTION_FIX_DATE).days
    out = {
        "ok": True,
        "report": "agent-success-weekly",
        "as_of": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "window_days": WINDOW_DAYS,
        "definition_version": REPORT_DEFINITION_VERSION,
        "definition_changelog": REPORT_DEFINITION_CHANGELOG,
        "publish_contract": {
            "rule": "no derived metric may appear unless its observation, "
                    "assumptions, definition version, and consumer are all "
                    "declared (round-3 partner rule; enforced fail-closed)",
            "required_fields": list(PUBLISH_CONTRACT_FIELDS),
            # Round-2 (ChatGPT): the cross-release invariant, stated where
            # consumers read, not remembered where authors edit.
            "invariant": "a definition_version change must never silently "
                         "alter historical interpretation: either history is "
                         "recomputed under the new definition (our default — "
                         "the identity views classify ALL history at read "
                         "time, so every window in this payload carries "
                         "current definitions), or history stays frozen and "
                         "the changelog marks the discontinuity explicitly",
            # Round-3 (Meta cited "tool_calls_7d … def_v2" — that is the
            # ENVELOPE version, not the metric's): say it where citers read.
            "version_scopes": "two version scopes — do not conflate when "
                              "citing. The top-level definition_version is "
                              "the ENVELOPE (payload shape); each metric "
                              "block carries its OWN definition_version (its "
                              "meaning). Pin the metric's version when citing "
                              "a metric; pin the envelope version when "
                              "parsing the payload.",
            "machine_readable": "/api/v1/reports/agent-success/contract",
        },
        "no_composite_score": NO_COMPOSITE_POLICY,
        "population": (
            "real external agents only. Internal/self traffic, scripted UAs, QA "
            "tags AND MCP registry/health/scanner crawlers are excluded "
            "(mcp_calls_deloop families, r-registry-crawlers 2026-07-28). "
            "Ambiguous gateways that proxy real users (smithery, glama, "
            "agent-toolscloud) are deliberately KEPT."
        ),
        "sources": {
            "identity_view": "mcp_calls_identity (crawler exclusions applied to "
                             "Neon 2026-07-28 22:12Z, 9/9 families verified)",
            "episode_model": f"routes/planner_bypass.py definition_version "
                             f"{EPISODE_MODEL_VERSION} over mcp_call_log, with "
                             "the same crawler exclusions added (regex form)",
        },
        "section_order": list(SECTION_ORDER),
    }

    blocks = {}
    mcp_share = None
    platform_rows = None
    split_error = None
    prev_calls = prev_agents = None
    cur_calls = cur_agents = None

    c = _conn()
    if c is None:
        out["error"] = "no database connection"
    else:
        try:
            with c.cursor() as cur:
                # ── totals off the identity view ────────────────────────────
                try:
                    calls, agents = _bounded(cur, _SQL_TOTALS)
                    cur_calls, cur_agents = int(calls or 0), int(agents or 0)
                    blocks["tool_calls_7d"] = _metric_block(
                        "tool_calls_7d", cur_calls, "MEASURED")
                    blocks["active_agents_7d"] = _metric_block(
                        "active_agents_7d", cur_agents, "MEASURED")
                except Exception as e:
                    logger.warning("[agent-success] totals: %s", str(e)[:150])
                try:
                    pcalls, pagents = _bounded(cur, _SQL_TOTALS_PREV)
                    prev_calls, prev_agents = int(pcalls or 0), int(pagents or 0)
                except Exception as e:
                    logger.warning("[agent-success] prev totals: %s", str(e)[:150])

                # ── time-to-first-result + result rate (one query) ──────────
                try:
                    episodes, with_result, ttfr = _bounded(cur, _SQL_TTFR)
                    episodes, with_result = int(episodes or 0), int(with_result or 0)
                    measured = with_result > 0 and ttfr is not None
                    block = _metric_block(
                        "median_time_to_first_result_ms",
                        round(float(ttfr), 1) if measured else None,
                        "MEASURED" if measured else "UNMEASURED",
                        episodes=episodes,
                        episodes_with_result=with_result,
                        episodes_without_result=episodes - with_result,
                    )
                    if not measured:
                        block["unmeasured_reason"] = (
                            "no agent-day episode in the window recorded a "
                            "successful call — nothing to take a median over")
                    blocks["median_time_to_first_result_ms"] = block
                    blocks["episode_result_rate"] = _metric_block(
                        "episode_result_rate",
                        _rate(with_result, episodes),
                        "MEASURED" if episodes else "UNMEASURED",
                        numerator=with_result, denominator=episodes)
                except Exception as e:
                    logger.warning("[agent-success] ttfr: %s", str(e)[:150])

                # ── second-recipe take-up (episode grain, WITH params) ──────
                try:
                    plan_eps, second_eps = _bounded_params(
                        cur, _second_recipe_sql(), (WINDOW_DAYS, FRONT_DOOR))
                    plan_eps, second_eps = int(plan_eps or 0), int(second_eps or 0)
                    blocks["second_recipe_take_up_pct"] = _metric_block(
                        "second_recipe_take_up_pct",
                        _rate(second_eps, plan_eps),
                        "MEASURED" if plan_eps else "UNMEASURED",
                        numerator=second_eps, denominator=plan_eps,
                        denominator_definition="agent-day episodes with ≥1 "
                                                "execute_plan call")
                    if not plan_eps:
                        blocks["second_recipe_take_up_pct"]["unmeasured_reason"] = (
                            "no episode in the window called execute_plan — "
                            "take-up of a second recipe is unmeasurable, not 0%")
                except Exception as e:
                    logger.warning("[agent-success] second recipe: %s", str(e)[:150])

                # ── generic-bucket share (the attribution gate's evidence) ──
                try:
                    real_calls, generic_calls = _bounded(cur, _SQL_MCP_SHARE)
                    real_calls, generic_calls = int(real_calls or 0), int(generic_calls or 0)
                    if real_calls:
                        mcp_share = round(generic_calls / real_calls, 4)
                except Exception as e:
                    logger.warning("[agent-success] generic share: %s", str(e)[:150])

                # ── the split itself — computed ONLY behind the gate ────────
                if _attribution_gate(days_since_fix, mcp_share)[0]:
                    try:
                        rows = _bounded(cur, _SQL_PLATFORM_SPLIT, fetch="all")
                        platform_rows = [
                            {"platform": p, "calls": int(n or 0), "agents": int(a or 0)}
                            for (p, n, a) in (rows or [])
                        ]
                    except Exception as e:
                        logger.warning("[agent-success] split: %s", str(e)[:150])
                        split_error = str(e)[:150]
        except Exception as e:
            logger.warning("[agent-success] identity block: %s", str(e)[:200])
        finally:
            try:
                c.close()
            except Exception:
                pass

    # ── episode metrics (planner adoption / manual orchestration) ──────────
    episode_context = {}
    try:
        ep = _episode_measure(WINDOW_DAYS, extra_where=CRAWLER_EXCLUSION_WHERE)
        if not (ep.get("ok") and ep.get("status") in ("MEASURED", "UNMEASURED")):
            raise RuntimeError(ep.get("error") or "episode measure failed")
        opportunities = ep.get("opportunities")
        shared = {
            "denominator": opportunities,
            "denominator_definition": "agent-day episodes with 2+ calls in the window",
            "episodes_total": ep.get("episodes"),
        }
        measured = ep["status"] == "MEASURED"
        adoption = _metric_block(
            "planner_adoption_pct",
            ep.get("planner_adoption_pct") if measured else None,
            ep["status"], numerator=ep.get("planner_adopted"), **shared)
        manual = _metric_block(
            "manual_orchestration_pct",
            ep.get("manual_orchestration_pct") if measured else None,
            ep["status"], numerator=ep.get("manual_orchestration"), **shared)
        if not measured:
            for b in (adoption, manual):
                b["unmeasured_reason"] = ep.get("unmeasured_reason") or (
                    "no opportunity episodes in the window")
        blocks["planner_adoption_pct"] = adoption
        blocks["manual_orchestration_pct"] = manual
        episode_context = {
            "front_door": ep.get("front_door"),
            "legacy_door_first_episodes": ep.get("legacy_door_first"),
            "not_counted_as_bypass": {
                "benign_fanout": ep.get("benign_fanout"),
                "benign_direct_single_call": ep.get("benign_direct_single_call"),
                "independent_multi_no_handoff": ep.get("independent_multi_no_handoff"),
            },
            "episode_model_assumptions": ep.get("assumptions"),
        }
    except Exception as e:
        logger.warning("[agent-success] episodes: %s", str(e)[:150])

    # A partial failure must degrade to explicit UNAVAILABLE blocks, never to
    # a 500 and never to invented zeros.
    for key in METRICS:
        blocks.setdefault(key, _metric_block(
            key, None, "UNAVAILABLE", error="source query failed"))

    # ── per-platform block — the gate IS the payload, present either way ───
    passed, gate_status, reason = _attribution_gate(days_since_fix, mcp_share)
    per_platform = {
        "status": gate_status,
        "reason": reason,
        "definition_version": 1,
        "definition_changelog": {
            1: "initial — born gated: publishes only after "
               f"{ATTRIBUTION_MIN_ACCUMULATION_DAYS}d of post-fix "
               "accumulation AND a verified generic-bucket drop",
        },
        "gate": {
            "attribution_fix_date": ATTRIBUTION_FIX_DATE.isoformat(),
            "attribution_fix": "client_name_raw threaded on the stateless "
                               "path + platform recall (dchub-mcp-server, "
                               "2026-07-28)",
            "min_accumulation_days": ATTRIBUTION_MIN_ACCUMULATION_DAYS,
            "days_since_fix": days_since_fix,
            "earliest_eligible": "2026-08-04",
            "generic_buckets": list(GENERIC_BUCKETS),
            "generic_bucket_share_7d": mcp_share,
            "generic_bucket_share_pre_fix_baseline": MCP_BUCKET_SHARE_PRE_FIX,
            "max_share_to_publish": MCP_BUCKET_MAX_SHARE_TO_PUBLISH,
            "share_note": "share of real 7d calls whose canonical platform "
                          "bucket is generic (the pre-fix 'mcp' label, renamed "
                          "'mcp-generic-client' by the 07-28 classifier) — an "
                          "attribution-quality gauge, not a usage split. When "
                          "the split publishes, the generic bucket stays "
                          "visible as its own row, never redistributed.",
            "passed": passed,
        },
    }
    if passed:
        if platform_rows is not None:
            per_platform["platforms"] = platform_rows
        else:
            per_platform["status"] = "UNAVAILABLE"
            per_platform["error"] = split_error or "split query failed"

    # ── week-over-week (learning's computed evidence) ───────────────────────
    # Status taxonomy matters here: UNAVAILABLE = the window queries never
    # ran (no verdict at all); UNMEASURED = they ran but the prior window is
    # empty (a delta off nothing is not +100%); MEASURED otherwise.
    def _wow_status(cur_v, prev_v):
        if cur_v is None or prev_v is None:
            return "UNAVAILABLE"
        return "MEASURED" if prev_v else "UNMEASURED"

    blocks["tool_calls_wow_pct"] = _metric_block(
        "tool_calls_wow_pct", _wow_pct(cur_calls, prev_calls),
        _wow_status(cur_calls, prev_calls),
        current_window=cur_calls, prior_window=prev_calls)
    blocks["active_agents_wow_pct"] = _metric_block(
        "active_agents_wow_pct", _wow_pct(cur_agents, prev_agents),
        _wow_status(cur_agents, prev_agents),
        current_window=cur_agents, prior_window=prev_agents)

    if mcp_share is None:
        trend_direction = "unmeasured"
    elif mcp_share <= MCP_BUCKET_SHARE_PRE_FIX - 0.02:
        trend_direction = "improving"
    elif mcp_share <= MCP_BUCKET_SHARE_PRE_FIX + 0.02:
        trend_direction = "flat"
    else:
        trend_direction = "worsening"

    # ── the five sections, learning LAST (round-3 rule 1) ───────────────────
    out["sections"] = [
        {
            "section": "reach",
            "question": SECTION_QUESTIONS["reach"],
            "per_platform": per_platform,
            "note": "ecosystem-level reach stays gated until platform "
                    "attribution verifies; the gate block above — with its "
                    "live generic-bucket gauge — is the honest answer today",
        },
        {
            "section": "activation",
            "question": SECTION_QUESTIONS["activation"],
            "metrics": {
                "tool_calls_7d": blocks["tool_calls_7d"],
                "active_agents_7d": blocks["active_agents_7d"],
                "median_time_to_first_result_ms":
                    blocks["median_time_to_first_result_ms"],
                "second_recipe_take_up_pct":
                    blocks["second_recipe_take_up_pct"],
            },
            "out_of_scope": "recipe COMPLETION (lifecycle logging) needs a "
                            "schema change — queued separately, absence here "
                            "is scope, not oversight",
        },
        {
            "section": "planner_adoption",
            "question": SECTION_QUESTIONS["planner_adoption"],
            "metrics": {
                "planner_adoption_pct": blocks["planner_adoption_pct"],
            },
            "context": episode_context or None,
        },
        {
            "section": "execution_quality",
            "question": SECTION_QUESTIONS["execution_quality"],
            "metrics": {
                "manual_orchestration_pct": blocks["manual_orchestration_pct"],
                "episode_result_rate": blocks["episode_result_rate"],
            },
            "note": "manual orchestration is the OBSERVED hand-chaining shape "
                    "(the judgement 'bypass' stays on the admin surface); "
                    "result rate is the completion floor",
        },
        {
            "section": "learning",
            "question": SECTION_QUESTIONS["learning"],
            # Round-2 (ChatGPT): the split exists so a telemetry repair can
            # never be read as a change in agent behaviour. attribution_trend
            # sits under measurement_integrity deliberately — it gauges OUR
            # ability to see, not what agents did.
            "why_split": "measurement_integrity = changes to how we see; "
                         "behaviour = changes in what agents did (or what "
                         "the product does to them). Never conflate.",
            "measurement_integrity": {
                "notes": [n for n in LEARNING_NOTES
                          if n["class"] == "measurement_integrity"],
                "attribution_trend": {
                    "baseline_2026_07_28": MCP_BUCKET_SHARE_PRE_FIX,
                    "generic_bucket_share_7d": mcp_share,
                    "direction": trend_direction,
                    "note": "generic-bucket share of real calls — the gauge "
                            "the per-platform gate watches; falling means "
                            "attribution is genuinely improving",
                },
            },
            "behaviour": {
                "computed": {
                    "tool_calls_wow_pct": blocks["tool_calls_wow_pct"],
                    "active_agents_wow_pct": blocks["active_agents_wow_pct"],
                },
                "notes": [n for n in LEARNING_NOTES
                          if n["class"] == "behaviour"],
            },
        },
    ]

    # ── confidence — DECLARED, never scored (round-2, ChatGPT) ─────────────
    # States are words with a why; no synthetic percentage exists or may be
    # added (same philosophy as refusing composite scores: don't replace
    # uncertainty with aesthetics).
    opportunities = blocks["planner_adoption_pct"].get("denominator")
    second_den = blocks["second_recipe_take_up_pct"].get("denominator")
    out["confidence"] = {
        "note": "declared, not scored — each axis states WHY to trust or "
                "doubt it; there is deliberately no confidence percentage",
        "platform_attribution": {
            "state": "partial" if (mcp_share is None or
                                   mcp_share > MCP_BUCKET_MAX_SHARE_TO_PUBLISH
                                   or days_since_fix <
                                   ATTRIBUTION_MIN_ACCUMULATION_DAYS)
                     else "verified",
            "why": ("generic bucket holds "
                    + (f"{round(100 * mcp_share, 1)}%" if mcp_share is not None
                       else "an unmeasured share")
                    + " of real calls; per-platform stays gated until the "
                      "share verifies ≤80% across a fully post-fix week"),
        },
        "planner_denominator": {
            "state": ("unavailable" if opportunities is None
                      else "sparse" if opportunities < 30 else "stable"),
            "why": f"{opportunities} opportunity episodes in the window"
                   if opportunities is not None else
                   "episode source unavailable this build",
        },
        "second_recipe_denominator": {
            "state": ("unavailable" if second_den is None
                      else "sparse" if second_den < 30 else "stable"),
            "why": f"{second_den} episode(s) invoked the planner at all — "
                   "read the rate as a count, not a trend"
                   if second_den is not None else
                   "episode source unavailable this build",
        },
        "definition_versions": {
            "state": "locked",
            "why": "every metric carries an integer definition_version + "
                   "changelog; meaning changes arrive only as version bumps "
                   "(see publish_contract.invariant)",
        },
        "observation_window": {
            "state": "7d rolling",
            "why": "both WoW windows are recomputed under current "
                   "definitions at read time — a definition change cannot "
                   "masquerade as a week-over-week move",
        },
    }

    # ok = at least one metric actually measured (or honestly UNMEASURED).
    # A build where every block failed is a failed build — the route must not
    # cache it, and a consumer must not mistake it for a quiet week.
    out["ok"] = any(b.get("status") not in ("UNAVAILABLE", "UNPUBLISHABLE")
                    for b in blocks.values())
    if not out["ok"]:
        out.setdefault("error", "all metrics unavailable")
    return out


# ── Stale-while-revalidate cache, single-flight refresh ────────────────────
_CACHE = {"data": None, "ts": 0.0}
_CACHE_TTL_S = 900
_REFRESH_LOCK = threading.Lock()
_REFRESH_RUNNING = False


def _refresh_cache():
    global _REFRESH_RUNNING
    try:
        data = _build_report()
        if data.get("ok"):
            _CACHE["data"] = data
            _CACHE["ts"] = time.time()
    except Exception as e:
        logger.warning("[agent-success] refresh failed: %s", str(e)[:200])
    finally:
        with _REFRESH_LOCK:
            _REFRESH_RUNNING = False


def _disabled() -> bool:
    return (os.environ.get("AGENT_SUCCESS_REPORT_DISABLE") or "").strip() == "1"


@agent_success_report_bp.route("/api/v1/reports/agent-success", methods=["GET"])
def agent_success_report():
    global _REFRESH_RUNNING
    if _disabled():
        return jsonify(ok=False, error="disabled"), 503
    now = time.time()
    data = _CACHE["data"]
    if data is not None:
        age = now - _CACHE["ts"]
        if age >= _CACHE_TTL_S:
            with _REFRESH_LOCK:
                if not _REFRESH_RUNNING:
                    _REFRESH_RUNNING = True
                    threading.Thread(target=_refresh_cache,
                                     name="agent-success-refresh",
                                     daemon=True).start()
        payload = dict(data)
        payload["cache_age_s"] = round(age, 1)
        return jsonify(payload), 200, {"Cache-Control": "public, max-age=300"}
    # First build since boot — bounded inline (per-query statement_timeout).
    data = _build_report()
    if data.get("ok"):
        _CACHE["data"] = data
        _CACHE["ts"] = time.time()
        payload = dict(data)
        payload["cache_age_s"] = 0.0
        return jsonify(payload), 200, {"Cache-Control": "public, max-age=300"}
    return jsonify(data), 503


def _contract_payload() -> dict:
    """The metric publish contract, machine-readable (round-2, Copilot).

    GENERATED from PUBLISH_CONTRACT_FIELDS — a hand-written schema would be a
    second copy of the contract, and second copies drift (the exact class
    this repo hit three times in July). A consumer that validates blocks
    against this schema and pins definition_version gets semantic-drift
    detection for free: any meaning change arrives as a version bump with a
    changelog entry, per publish_contract.invariant."""
    props = {
        "value": {
            "type": ["number", "integer", "null"],
            "description": "null whenever status is not MEASURED — a rate "
                           "off an empty denominator is null, never 0 or 100",
        },
        "status": {
            "enum": ["MEASURED", "UNMEASURED", "UNAVAILABLE", "UNPUBLISHABLE"],
            "description": "UNMEASURED = source ran, nothing to divide; "
                           "UNAVAILABLE = source never ran; UNPUBLISHABLE = "
                           "contract incomplete (see publish_block_reason)",
        },
        "definition": {"type": "string"},
        "observation": {
            "type": "string",
            "description": "the raw observed thing, before any derivation",
        },
        "assumptions": {"type": "array", "items": {"type": "string"},
                        "minItems": 1},
        "definition_version": {
            "type": "integer", "minimum": 1,
            "description": "INTEGER, deliberately not semver: any change in "
                           "MEANING bumps the version (even if name and type "
                           "hold), so every bump is MAJOR by construction — "
                           "minor/patch lanes would carry no information, "
                           "and a format migration would break pinned "
                           "consumers for zero semantic gain",
        },
        "definition_changelog": {
            "type": "object",
            "description": "one entry per version 1..definition_version; a "
                           "version without a changelog entry fails CI",
        },
        "consumers": {"type": "array", "items": {"type": "string"},
                      "minItems": 1},
        "unit": {"type": "string"},
        "source": {"type": "string"},
    }
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "https://dchub.cloud/api/v1/reports/agent-success/contract",
        "title": "DC Hub Agent Success Report — metric publish contract",
        "description": "Every metric block in /api/v1/reports/agent-success "
                       "validates against this object. Enforcement is "
                       "fail-closed at render: an incomplete contract yields "
                       "status UNPUBLISHABLE with publish_block_reason and "
                       "no value.",
        "type": "object",
        "required": ["value", "status"] + list(PUBLISH_CONTRACT_FIELDS),
        "properties": props,
        "report_definition_version": REPORT_DEFINITION_VERSION,
    }


@agent_success_report_bp.route("/api/v1/reports/agent-success/contract",
                               methods=["GET"])
def agent_success_contract():
    """Static, DB-free, cacheable — the contract changes only with a deploy."""
    return jsonify(_contract_payload()), 200, {
        "Cache-Control": "public, max-age=3600"}
