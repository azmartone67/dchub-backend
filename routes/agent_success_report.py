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

V5 (2026-07-31 partner round, ChatGPT): three derived metrics —
calls_per_active_agent_7d on the CANONICAL external-activity basis (numerator
and denominator from mcp_calls_deloop.canonical_external_activity_sql, the one
importable agent-count query, r-agent-parity backend #2038); agent_cohorts_7d
over the identity unit (first_week_ever / returning / reactivated, 14-day
reactivation gap); planner_penetration_by_cohort_pct (share of each cohort
whose first call of the window was execute_plan). Adopted WITH the proposer's
own caveat as a copy rule: nothing on this surface optimizes or encourages raw
call volume — the north star stays "solve with the minimum necessary work".

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
    CANONICAL_AGENTS_BASIS,
    PLATFORM_CASE,
    canonical_external_activity_sql,
    internal_tag_regex_predicate,
    real_ua_predicate,
)
from recipe_lifecycle import (
    ABANDONED_AFTER_MINUTES as RECIPE_ABANDONED_AFTER_MINUTES,
    SOURCE_EXECUTE_PLAN as RECIPE_SOURCE,
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
REPORT_DEFINITION_VERSION = 6
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
    4: "recipe lifecycle becomes FIRST-CLASS (Perplexity round-5, "
       "2026-07-30): execution_quality gains recipe_completion_rate, read "
       "from the new recipe_executions table — gateway-emitted "
       "started/completed events with a shared recipe_execution_id, "
       "agent-day identity columns (durable api_key first; session_id "
       "stored for forensics only), and abandonment DERIVED from a missing "
       "completion event. Replaces the round-3 'recipe completion is out of "
       "scope' placeholder; second_recipe_take_up_pct is UNCHANGED (still "
       "call-row intents) so its version does not move",
    5: "2026-07-31 partner round (ChatGPT): activation gains "
       "calls_per_active_agent_7d (canonical external-activity basis — "
       "numerator AND denominator from the one importable query, "
       "r-agent-parity backend #2038) and agent_cohorts_7d (first_week_ever "
       "/ returning / reactivated over the identity unit; reactivated = "
       "gone >=14d then back); planner_adoption gains "
       "planner_penetration_by_cohort_pct (share of each cohort whose "
       "first call of the window was execute_plan, identity grain). "
       "Adopted with the proposer's own caveat as a copy rule: no metric "
       "on this surface encourages raw call volume — the north star stays "
       "'solve with the minimum necessary work'",
    6: "2026-08-05 population collision: tool_calls_7d (metric "
       "definition_version 2) and the per-platform gate + split (block "
       "definition_version 2) move onto the canonical identity population "
       "is_public_ip AND is_real_external. Before this, ONE payload carried "
       "two answers to 'real external calls, 7d' — tool_calls_7d 7,090 and "
       "calls_per_active_agent_7d's numerator 6,758 — three keys apart in the "
       "same section, and the gate's own generic-bucket evidence disagreed "
       "with the agent-expansion shell's reading of the same question (25.1% "
       "vs 21.6%) for the same reason. Both are now the canonical population, "
       "the one the funnel and the press headline already quote. The step "
       "down in tool_calls_7d at this bump is the definition change, not a "
       "traffic change",
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

# ── Cohorts + calls-per-agent (v5, 2026-07-31 partner round) ───────────────
# Identity unit throughout: agent_id from mcp_calls_identity — never
# session_id, which rotates per MCP connection (~1.2 calls each), so a
# session-keyed cohort would misread every returning agent as new.
# Reactivation gap is the partner-round rule: seen before, gone >= 14 days,
# then back this window = won back, not merely retained.
REACTIVATION_GAP_DAYS = 14
# "ever" needs history, and history costs a scan. The lookback bounds the
# cohort query inside the statement timeout once the table ages; the bound is
# DECLARED in the metric's assumptions — an agent last seen before it
# misclassifies as first_week_ever rather than silently blowing the budget.
COHORT_HISTORY_LOOKBACK_DAYS = 180
COHORT_NAMES = ("first_week_ever", "returning", "reactivated")

# ChatGPT's observed baseline from the 2026-07-31 partner round — a
# point-in-time REFERENCE for readers of the first published values, never a
# target. Raw call volume is not optimized or encouraged anywhere on this
# surface (the proposer's own caveat, adopted as a copy rule).
CALLS_PER_AGENT_BASELINE = {
    "value": 59.1,
    "measured": "2026-07-31",
    "note": "point-in-time reference from the partner-round proposal, not a "
            "target — neither direction of this gauge is 'better' (see the "
            "metric definition)",
}


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


def _cohort_rollup(rows):
    """(cohorts, active_total, planner_first_total) from the cohort query's
    rows. Pure — fully unit-testable. Absent cohorts are measured ZEROS (the
    query ran; that cohort was empty). Strict on labels: an unknown cohort
    name means the SQL and this rollup drifted — raise, so the blocks degrade
    to an honest UNAVAILABLE instead of silently dropping agents from the
    partition."""
    cohorts = {name: {"agents": 0, "planner_first_agents": 0}
               for name in COHORT_NAMES}
    for name, agents, planner_first in rows or []:
        if name not in cohorts:
            raise ValueError(f"unknown cohort label {name!r}")
        cohorts[name] = {"agents": int(agents or 0),
                         "planner_first_agents": int(planner_first or 0)}
    total = sum(c["agents"] for c in cohorts.values())
    planner_total = sum(c["planner_first_agents"] for c in cohorts.values())
    return cohorts, total, planner_total


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
        "definition": "COUNT(*) over mcp_calls_identity WHERE is_real_external "
                      "AND is_public_ip, trailing 7 days. Counts tool CALLS, "
                      "never sessions. THE canonical weekly call figure — the "
                      "same population as active_agents_7d, "
                      "calls_per_active_agent_7d, the funnel's "
                      "real_external_calls_7d and /api/v1/reports/"
                      "weekly-series, so no two surfaces can publish a "
                      "different 'real external calls, 7d'.",
        "observation": "rows in the crawler-excluded identity view inside the "
                       "window — direct count, nothing derived",
        "assumptions": ["a call is one tracked tool invocation; client retries "
                        "are separate calls",
                        "Cloudflare-POP rows (no resolvable agent identity) "
                        "are OUT, as they are for every other aggregate on "
                        "this surface — see definition_version 2"],
        "unit": "calls",
        "source": "mcp_calls_identity (crawler-excluded view over mcp_tool_calls)",
        "consumers": _CONSUMERS_DEFAULT,
        "definition_version": 2,
        "definition_changelog": {
            1: "initial — identity-view population (internal traffic, scripted "
               "UAs, QA tags and registry/health/scanner crawlers excluded; "
               "r-registry-crawlers families applied 2026-07-28)",
            2: "2026-08-05 POPULATION COLLISION FIXED: adds is_public_ip, "
               "which every other aggregate in this payload already applied. "
               "v1 counted 7,090 while calls_per_active_agent_7d's numerator "
               "on the canonical basis read 6,758 — two answers to 'real "
               "external calls, 7d' inside ONE payload, three keys apart. The "
               "326-row gap is Cloudflare-POP traffic: real calls whose agent "
               "grain is unknowable, previously counted here but invisible to "
               "every agent-keyed metric beside them. v1's wider count was "
               "never the publicly quoted one (the press headline and the "
               "funnel both bind the canonical basis), so the collision is "
               "resolved by moving this metric onto that basis rather than by "
               "labelling the disagreement. EXPECT A STEP DOWN OF ~4.9% AT "
               "THIS BUMP — it is a definition change, not a traffic change, "
               "which is what definition_version exists to say out loud",
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
    "calls_per_active_agent_7d": {
        "definition": "real_external_calls_7d / real_external_agents_7d, both "
                      "aggregates from THE canonical external-activity query "
                      "(one SQL statement, so numerator and denominator can "
                      "never come from different identity x exclusion x "
                      "window tuples). A workload-intensity CONTEXT gauge — "
                      "NEITHER DIRECTION IS A TARGET: the north star for "
                      "agent workflows stays 'solve with the minimum "
                      "necessary work' (the proposer's own caveat, adopted "
                      "2026-07-31). A rise can be deeper multi-step work or "
                      "wasted hand-chaining; a fall can be the planner "
                      "absorbing steps or disengagement.",
        "observation": "the canonical query's two aggregates — COUNT(*) and "
                       "COUNT(DISTINCT agent_id) over mcp_calls_identity "
                       "WHERE is_public_ip AND is_real_external, trailing 7 "
                       "days — divided; nothing else enters",
        "assumptions": [
            "numerator EQUALS tool_calls_7d as of 2026-08-05 (that metric's "
            "definition_version 2): both apply is_public_ip, so CF-POP rows "
            "(real calls whose agent grain is unknowable) are excluded from "
            "both. Until then this assumption declared the opposite — the two "
            "differed 'by design' — and a labelled disagreement between two "
            "keys of one payload turned out to be a defect wearing a label",
            "agent identity is IP-derived: NAT under-counts agents and "
            "inflates this ratio; rotating egress over-counts and deflates it",
            "a MEAN, not a median — one heavy agent moves it; read with "
            "agent_cohorts_7d and the planner metrics, never alone, and "
            "never as a number to push up",
        ],
        "unit": "calls per active agent (mean, 7d)",
        "source": "mcp_calls_deloop.canonical_external_activity_sql(7) — the "
                  "one importable agent-count query (r-agent-parity "
                  "2026-07-31, backend #2038)",
        "consumers": _CONSUMERS_DEFAULT,
        "definition_version": 1,
        "definition_changelog": {
            1: "initial — 2026-07-31 partner round (ChatGPT). Canonical "
               "basis only; published as context for the cohort and planner "
               "metrics with the volume caveat adopted verbatim: never "
               "optimize or encourage raw call volume — the goal is solving "
               "with the minimum necessary work, and this gauge has no "
               "'good' direction",
        },
    },
    "agent_cohorts_7d": {
        "definition": "Partition of the window's active agents (identity "
                      "unit: agent_id) into exactly three cohorts by their "
                      "own history: first_week_ever = no real-external "
                      "activity before their first call of the window; "
                      f"returning = seen within {REACTIVATION_GAP_DAYS} days "
                      "before that first call; reactivated = seen before, "
                      f"then gone >= {REACTIVATION_GAP_DAYS} days, then back "
                      "this window. Distinct agent counts per cohort; the "
                      "value is the partition total.",
        "observation": "per active agent: the first call of the window and "
                       "the most recent real-external activity before it, "
                       "both from mcp_calls_identity — the cohort label is "
                       "arithmetic on those two timestamps",
        "assumptions": [
            "identity unit is agent_id (md5 of first public XFF hop), never "
            "session_id (rotates per connection, ~1.2 calls); NAT/egress "
            "churn can misfile an agent across cohorts in both directions",
            f"'ever' is bounded by a {COHORT_HISTORY_LOOKBACK_DAYS}-day "
            "history lookback (and by table retention): an agent last seen "
            "before the lookback misclassifies as first_week_ever",
            "history is classified under CURRENT exclusions at read time "
            "(the recompute resolution of the publish invariant): prior "
            "activity that today's rules call internal or crawler does not "
            "count as having been seen",
            "the partition total must reconcile with active_agents_7d — "
            "same view, same filters; a persistent gap between the two is "
            "a defect, not a cohort signal",
        ],
        "unit": "distinct agents (partitioned)",
        "source": "mcp_calls_identity (window + per-agent history within "
                  "the lookback)",
        "consumers": _CONSUMERS_DEFAULT,
        "definition_version": 1,
        "definition_changelog": {
            1: "initial — 2026-07-31 partner round (ChatGPT). Cohorts "
               "answer WHO is active — new, retained, or won back — so "
               "activation is never read as one undifferentiated count; "
               f"reactivation gap fixed at {REACTIVATION_GAP_DAYS} days. "
               "PLANNED, deliberately NOT built yet (the proposer's own "
               "scoping: 'not now — once you have a few months of "
               "history'): an expansion ratio (work done by returning "
               "agents / work done by new agents) as a workflow-embedding "
               "proxy, gated on accumulated weeks of cohort history and "
               "arriving as its own versioned metric — named here so the "
               "deferral is on the record, not rediscovered",
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
    "planner_penetration_by_cohort_pct": {
        "definition": "Of the window's active agents (identity unit), the "
                      "share whose FIRST call of the window was execute_plan "
                      "— overall (the value) and per cohort (by_cohort). "
                      "First-call selection reuses the planner-bypass "
                      "episode model's discipline (earliest timestamp wins) "
                      "at the identity grain; identity is agent_id, never "
                      "session_id. Differs from planner_adoption_pct ON "
                      "PURPOSE: adoption conditions on opportunity episodes "
                      "(2+ calls, agent-day grain, mcp_call_log); "
                      "penetration is unconditional over active agents, so "
                      "single-call agents count — two grains, two questions.",
        "observation": "per active agent, the tool_name of the earliest call "
                       "in the window (DISTINCT ON, ordered by created_at "
                       "ASC), joined to the same cohort partition as "
                       "agent_cohorts_7d — one query feeds both metrics, so "
                       "cohort and penetration populations cannot diverge",
        "assumptions": [
            "identity grain, NOT the agent-day episode grain: an agent "
            "active five days gets ONE verdict, from its first call of the "
            "whole window",
            "execute_plan availability is ASSUMED, not measured — a "
            "client's allowed_tools scoping is invisible to the server",
            "per-cohort rates are None when the cohort is empty (never 0% "
            "or 100% off nothing); small cohorts read as counts, not trends",
        ],
        "unit": "% of active agents (overall; per-cohort in by_cohort)",
        "source": "mcp_calls_identity — the same single query as "
                  "agent_cohorts_7d",
        "consumers": _CONSUMERS_DEFAULT,
        "definition_version": 1,
        "definition_changelog": {
            1: "initial — 2026-07-31 partner round (ChatGPT). Whether the "
               "intended entry point reaches first_week_ever agents before "
               "habits form, holds returning ones, and greets reactivated "
               "ones — sliced by the same cohort partition; a share of "
               "agents, never a volume metric",
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
    "recipe_completion_rate": {
        "definition": "Of execute_plan recipe executions STARTED in the "
                      "window that reached a TERMINAL state, the share whose "
                      "outcome is 'completed' (the envelope returned with at "
                      "least one step executed or gated_preview). Terminal = "
                      "completed | failed | abandoned; abandoned = a started "
                      "execution with no completion event after "
                      f"{RECIPE_ABANDONED_AFTER_MINUTES} minutes (the run "
                      "budget is 40s). Still-in-flight executions are "
                      "excluded from the denominator and reported alongside.",
        "observation": "first-class lifecycle rows in recipe_executions — "
                       "the gateway emits started and completed events with "
                       "a shared recipe_execution_id at the two ends of each "
                       "server-side graph run. This is a direct record of "
                       "the lifecycle, NOT an inference over tool-call rows "
                       "(what this metric replaces).",
        "assumptions": [
            "one recipe execution = one execute_plan invocation; the gateway "
            "runs the whole graph server-side, so start and end are both "
            "observable in one place",
            "'completed' asserts the envelope returned with ≥1 usable step "
            "result (gated previews count — they are working results at that "
            "tier); it does not assert the answer satisfied the caller",
            "abandonment is derived at read time (outcome still NULL past "
            "the threshold); a crash between start and completion is "
            "indistinguishable from abandonment and counts as abandoned",
            "identity columns follow the agent-day unit (durable api_key "
            "first, session fallback) — session_id is stored for forensics, "
            "never used as the identity key (it rotates per connection)",
            "lifecycle events are fire-and-forget telemetry: a dropped "
            "completed event undercounts completion (reads abandoned), a "
            "dropped started event is healed by the completion upsert",
        ],
        "unit": "% of terminal recipe executions",
        "source": "recipe_executions (gateway lifecycle events via "
                  "/api/v1/mcp/track, event=recipe_lifecycle) + synthetic "
                  "and registry-crawler exclusions",
        "consumers": _CONSUMERS_DEFAULT + ["activation board (specced)",
                                           "Perplexity round-5 ask (2026-07-30)"],
        "definition_version": 1,
        "definition_changelog": {
            1: "initial — first-class lifecycle logging shipped 2026-07-30 "
               "(backend recipe_executions + dchub-mcp-server "
               "started/completed emission), replacing the round-3 'needs a "
               "schema change, out of scope' placeholder. Reads UNMEASURED "
               "until the migration is applied and events accumulate — "
               "never 0%.",
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

# ★2026-08-05 POPULATION COLLISION (definition_version 2). The call counts
# below ran on is_real_external ALONE while every aggregate beside them —
# active_agents_7d, calls_per_active_agent_7d, the funnel's
# real_external_calls_7d, /api/v1/reports/weekly-series and the press headline
# — ran on is_public_ip AND is_real_external. Two "real external calls, 7d"
# numbers, 7,090 and 6,764, sat in ONE payload (sections[1].metrics carries
# both), which is the same side-by-side collision PR #2254 failed to fix by
# labelling. Measured 2026-08-05: the gap is 326 Cloudflare-POP rows, all of
# them landing in the generic attribution bucket, so the wider population also
# skewed the gate's own evidence (25.1% vs 21.6% — defect 2).
#
# Unified onto the CANONICAL basis (is_public_ip), not the wider one: that is
# the population already quoted publicly. CF-POP rows are real calls whose
# agent grain is unknowable; counting them here while the agent count cannot
# see them was what made the two numbers incomparable.
_POP = "is_real_external AND is_public_ip"

_SQL_TOTALS = f"""
SELECT COUNT(*) FILTER (WHERE {_POP})                                  AS tool_calls,
       COUNT(DISTINCT agent_id) FILTER (WHERE {_POP})                  AS active_agents
  FROM mcp_calls_identity
 WHERE {_W}
"""

_SQL_TOTALS_PREV = f"""
SELECT COUNT(*) FILTER (WHERE {_POP})                                  AS tool_calls,
       COUNT(DISTINCT agent_id) FILTER (WHERE {_POP})                  AS active_agents
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
# ★ Population is _POP — the same one tool_calls_7d counts, so the share's
# denominator IS the published call total and the split sums to it exactly.
# See the _POP note: before 2026-08-05 these two ran without is_public_ip and
# the gate read 25.1% while the expansion shell, running the canonical
# population, read 21.6% for the identical question.
_SQL_MCP_SHARE = f"""
SELECT COUNT(*)                                             AS real_calls,
       COUNT(*) FILTER (WHERE ({PLATFORM_CASE.strip()})
                          IN ({_GENERIC_IN}))               AS generic_bucket_calls
  FROM mcp_calls_identity
 WHERE {_W} AND {_POP}
"""

_SQL_PLATFORM_SPLIT = f"""
SELECT ({PLATFORM_CASE.strip()})                                  AS platform,
       COUNT(*)                                                   AS calls,
       COUNT(DISTINCT agent_id)                                   AS agents
  FROM mcp_calls_identity
 WHERE {_W} AND {_POP}
 GROUP BY 1 ORDER BY calls DESC LIMIT 15
"""


def measure_generic_bucket_share(cur):
    """(generic_calls, real_calls, share_fraction) — THE generic-bucket read.

    Exported so every board runs the identical statement instead of restating
    it. The 2026-08-04 fix imported GENERIC_BUCKETS into the expansion shell
    to stop the bucket LIST desyncing, then let that shell keep its own hand-
    written query — and the desync simply moved one layer down, into the
    population (is_public_ip) and the canonicaliser (PLATFORM_CASE vs the raw
    `platform` column). Import the QUERY, not the ingredients.

    share is a FRACTION (0-1), the unit _attribution_gate compares against
    MCP_BUCKET_MAX_SHARE_TO_PUBLISH. None when the window holds no real calls
    — a share off nothing is not 0%.
    """
    real_calls, generic_calls = _bounded(cur, _SQL_MCP_SHARE)
    real_calls, generic_calls = int(real_calls or 0), int(generic_calls or 0)
    share = round(generic_calls / real_calls, 4) if real_calls else None
    return generic_calls, real_calls, share


# ── OUR OWN CI, counted as external demand (r-ci-selftag, 2026-08-18) ───────
#
# The generic bucket was never mostly third-party demand. Every IP in the
# canonical population, tested against api.github.com/meta `actions`:
#     7d  — 1,700 of 2,114 calls (80.4%), 49 of 68 agents (72.1%)
#     30d — 8,138 of 15,665 calls (52.0%), 183 of 252 agents (72.6%)
# It is dchub-mcp-server's live smoke suite, which runs against
# https://dchub.cloud/mcp on every push. It self-identifies via clientInfo.name,
# but clientInfo arrives ONCE at initialize and is remembered only in an
# in-process Map — so a tools/call served by a process that never saw that
# initialize lost the tag and was written as an anonymous external agent
# (mcp_call_log, inside ONE session: 2 calls 'dchub-internal', 48 calls 'mcp').
# Runner IPs rotate and agent_id = md5(first XFF token), so every CI run minted
# a brand-new "distinct agent". mcp PR #202 moves the self-tag onto a
# per-request header so routing cannot lose it.
#
# ★ THIS CHECK EXISTS BECAUSE THE TAG IS THE WEAK PART. #202 fixes the two
# suites we know about; it cannot stop the NEXT harness from arriving untagged,
# and that harness would again read as demand. IP origin is independent of
# anything the caller chooses to tell us — which is the whole point.
# ★ Deliberately NOT a filter on the published population. Narrowing what
# `is_real_external` means is a definition change to numbers we publish; this
# only measures, and names the share.
_SQL_CI_ORIGIN = f"""
SELECT ip_address                    AS ip,
       COUNT(*)                      AS calls,
       COUNT(DISTINCT agent_id)      AS agents
  FROM mcp_calls_identity
 WHERE {_W} AND {_POP}
   AND ip_address IS NOT NULL AND ip_address <> ''
 GROUP BY 1
"""

_GH_META_URL = "https://api.github.com/meta"
_GH_RANGE_TTL_S = 6 * 3600
_gh_ranges_cache = {"at": 0.0, "nets": None}


def github_actions_ranges(force: bool = False):
    """GitHub's published Actions egress ranges, or None if unreadable.

    None is NOT an empty list. An unreadable range list means we cannot tell CI
    from demand — the caller must render that as UNMEASURED, never as "0% CI".
    Same discipline as the registry lane's `unreadable != absent`.

    ★ `requests`, never `urllib.request` (regression_lint blocks it, and the CF
    edge 1010s a bare urllib UA).
    """
    import time
    now = time.time()
    if (not force and _gh_ranges_cache["nets"] is not None
            and now - _gh_ranges_cache["at"] < _GH_RANGE_TTL_S):
        return _gh_ranges_cache["nets"]
    try:
        import ipaddress
        import requests
        # ★ 3s, not 10s. The shell's master-tick already runs ~11.9s measured
        # live, against Cloudflare's 15s admin ROUTE_TIMEOUTS ceiling — a 10s
        # cold fetch here would turn the whole tick into a 503. A miss costs a
        # single UNMEASURED render, which is a state this shell renders honestly.
        r = requests.get(_GH_META_URL, timeout=3,
                         headers={"User-Agent": "dchub-growth-integrity/1.0"})
        if r.status_code != 200:
            return _gh_ranges_cache["nets"]
        raw = (r.json() or {}).get("actions")
        if not isinstance(raw, list) or not raw:
            # A present-but-empty list would silently clear every classification.
            return _gh_ranges_cache["nets"]
        nets = []
        for cidr in raw:
            try:
                nets.append(ipaddress.ip_network(cidr))
            except Exception:
                continue
        if not nets:
            return _gh_ranges_cache["nets"]
        _gh_ranges_cache.update({"at": now, "nets": nets})
        return nets
    except Exception as e:
        logger.warning("[ci-origin] github meta read failed: %s", str(e)[:120])
        return _gh_ranges_cache["nets"]


def measure_ci_origin_share(cur):
    """{calls, ci_calls, agents, ci_agents, share, agent_share} or None.

    share/agent_share are FRACTIONS (0-1). None anywhere means UNMEASURED:
    either the range list was unreadable or the window holds no real calls. A
    share computed off nothing is not 0% — #1858.
    """
    import ipaddress
    nets = github_actions_ranges()
    if not nets:
        return None
    v4 = [n for n in nets if n.version == 4]
    v6 = [n for n in nets if n.version == 6]

    def _is_ci(ip):
        try:
            a = ipaddress.ip_address((ip or "").strip())
        except Exception:
            return False
        return any(a in n for n in (v4 if a.version == 4 else v6))

    rows = _bounded(cur, _SQL_CI_ORIGIN, fetch="all") or []
    calls = ci_calls = ci_agents = 0
    agents = 0
    for row in rows:
        ip, n, a = row[0], int(row[1] or 0), int(row[2] or 0)
        calls += n
        agents += a
        if _is_ci(ip):
            ci_calls += n
            ci_agents += a
    if not calls:
        return None
    # agents is a sum over IPs, so one agent seen on two IPs counts twice. That
    # OVERSTATES the denominator and therefore UNDERSTATES the CI share — the
    # safe direction for a number whose job is to raise an alarm.
    return {"calls": calls, "ci_calls": ci_calls,
            "agents": agents, "ci_agents": ci_agents,
            "share": round(ci_calls / calls, 4),
            "agent_share": round(ci_agents / agents, 4) if agents else None}

# ── Canonical external activity (v5) — IMPORTED, never transcribed ─────────
# THE one agent-count query (r-agent-parity 2026-07-31, backend #2038).
# calls_per_active_agent_7d divides its two aggregates, so numerator and
# denominator share one (identity x exclusion x window) tuple by
# construction. Literal-only fragment — runs through _bounded (no params).
_SQL_CANONICAL_ACTIVITY = canonical_external_activity_sql(WINDOW_DAYS)

# ── Cohorts + planner penetration — identity grain, ONE query (v5) ─────────
# Runs through _bounded (NO params): window, reactivation gap, lookback and
# the front-door tool name are trusted module constants inlined as literals.
# Crawler exclusions come from the canonical view ONLY (is_real_external) —
# nothing here re-lists them. first_call reuses the planner-bypass episode
# model's first-call discipline (earliest timestamp wins — its rn=1 pick, in
# DISTINCT ON form) at the identity grain; identity is agent_id, NEVER
# session_id. hist is bounded by the lookback so the scan stays inside the
# statement timeout once the table ages; the bound is declared in the
# metric's assumptions.
_SQL_COHORT_PENETRATION = f"""
WITH win AS (
  SELECT agent_id, MIN(created_at) AS first_in_window
    FROM mcp_calls_identity
   WHERE {_W}
     AND is_real_external AND is_public_ip AND agent_id IS NOT NULL
   GROUP BY agent_id
),
first_call AS (
  SELECT DISTINCT ON (agent_id) agent_id, tool_name AS first_tool
    FROM mcp_calls_identity
   WHERE {_W}
     AND is_real_external AND is_public_ip AND agent_id IS NOT NULL
   ORDER BY agent_id, created_at ASC
),
hist AS (
  SELECT w.agent_id, w.first_in_window, MAX(h.created_at) AS last_before
    FROM win w
    LEFT JOIN mcp_calls_identity h
      ON h.agent_id = w.agent_id
     AND h.created_at < w.first_in_window
     AND h.created_at >= NOW() - ({COHORT_HISTORY_LOOKBACK_DAYS} * INTERVAL '1 day')
     AND h.is_real_external AND h.is_public_ip
   GROUP BY w.agent_id, w.first_in_window
)
SELECT CASE
         WHEN h.last_before IS NULL THEN 'first_week_ever'
         WHEN h.last_before <= h.first_in_window
                - ({REACTIVATION_GAP_DAYS} * INTERVAL '1 day') THEN 'reactivated'
         ELSE 'returning'
       END                                                    AS cohort,
       COUNT(*)                                               AS agents,
       COUNT(*) FILTER (WHERE f.first_tool = '{FRONT_DOOR}')  AS planner_first
  FROM hist h JOIN first_call f USING (agent_id)
 GROUP BY 1
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


# ── Recipe lifecycle — first-class rows over recipe_executions, WITH params ─
# The v4 metric (Perplexity round-5). recipe_executions carries the same
# platform/user_agent columns as mcp_call_log, so it takes the SAME synthetic
# + crawler exclusions as the episode queries — one population rule, no third
# copy. Runs with bound params, so only %%-doubled LIKE and regex predicates
# are legal here (never PLATFORM_CASE). Abandonment is DERIVED in this query
# — outcome still NULL past the threshold — never backfilled into the table.
_SQL_RECIPE_LIFECYCLE = """
SELECT COUNT(*)                                              AS started,
       COUNT(*) FILTER (WHERE outcome = 'completed')         AS completed,
       COUNT(*) FILTER (WHERE outcome = 'failed')            AS failed,
       COUNT(*) FILTER (WHERE outcome = 'abandoned')         AS abandoned_marked,
       COUNT(*) FILTER (WHERE outcome IS NULL
                          AND started_at <  NOW() - make_interval(mins => %s))
                                                             AS abandoned_derived,
       COUNT(*) FILTER (WHERE outcome IS NULL
                          AND started_at >= NOW() - make_interval(mins => %s))
                                                             AS in_flight
  FROM recipe_executions
 WHERE started_at > NOW() - make_interval(days => %s)
   AND source = %s
   {synth}
"""


def _recipe_lifecycle_sql() -> str:
    return _SQL_RECIPE_LIFECYCLE.format(
        synth=_SYNTH_NOT_LIKE + CRAWLER_EXCLUSION_WHERE)


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
    {"date": "2026-07-30", "kind": "measurement", "class": "measurement_integrity",
     "note": "Recipe lifecycle became FIRST-CLASS (Perplexity round-5): the "
             "gateway now emits started/completed events per execute_plan "
             "run into recipe_executions (shared execution id; outcome "
             "completed|failed; abandonment derived from a missing "
             "completion). recipe_completion_rate reads that table from v4. "
             "Prior completion reads were inferences over call rows; "
             "second_recipe_take_up_pct still uses call-row intents and is "
             "unchanged. The metric reads UNMEASURED until the migration is "
             "applied and events accumulate — never 0%.",
     "source": "dchub-backend recipe_lifecycle.py + dchub-mcp-server "
               "lifecycle emission, 2026-07-30"},
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
    {"date": "2026-07-31", "kind": "measurement", "class": "measurement_integrity",
     "note": "v5 adds three derived metrics from the 07-31 partner round "
             "(ChatGPT): calls_per_active_agent_7d on the canonical "
             "external-activity basis (numerator and denominator from the "
             "one importable agent-count query, r-agent-parity backend "
             "#2038), agent_cohorts_7d (first_week_ever / returning / "
             "reactivated, 14-day reactivation gap, identity unit), and "
             "planner_penetration_by_cohort_pct (first call of the window, "
             "identity grain). The proposer's own caveat is adopted as a "
             "copy rule: calls-per-agent is context, never a target — the "
             "north star stays solving with the minimum necessary work. New "
             "instruments, not new behaviour: first readings say what we "
             "can now SEE.",
     "source": "2026-07-31 partner round (ChatGPT); dchub-backend v5"},
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
            "recipe_lifecycle": "recipe_executions (v4) — first-class "
                                "started/completed events from the gateway's "
                                "execute_plan path, same synthetic + crawler "
                                "exclusions as the episode queries",
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

                # ── recipe lifecycle (first-class rows, v4) ─────────────────
                # UndefinedTable before the migration is applied lands in this
                # except → the block degrades to UNAVAILABLE, never a 500 and
                # never an invented 0%.
                try:
                    (rl_started, rl_completed, rl_failed, rl_ab_marked,
                     rl_ab_derived, rl_in_flight) = [
                        int(x or 0) for x in _bounded_params(
                            cur, _recipe_lifecycle_sql(),
                            (RECIPE_ABANDONED_AFTER_MINUTES,
                             RECIPE_ABANDONED_AFTER_MINUTES,
                             WINDOW_DAYS, RECIPE_SOURCE))]
                    rl_abandoned = rl_ab_marked + rl_ab_derived
                    rl_terminal = rl_completed + rl_failed + rl_abandoned
                    blocks["recipe_completion_rate"] = _metric_block(
                        "recipe_completion_rate",
                        _rate(rl_completed, rl_terminal),
                        "MEASURED" if rl_terminal else "UNMEASURED",
                        numerator=rl_completed, denominator=rl_terminal,
                        denominator_definition="terminal recipe executions "
                                               "(completed + failed + "
                                               "abandoned); in-flight excluded",
                        executions_started=rl_started,
                        outcome_counts={
                            "completed": rl_completed,
                            "failed": rl_failed,
                            "abandoned": rl_abandoned,
                            "in_flight": rl_in_flight,
                        })
                    if not rl_terminal:
                        blocks["recipe_completion_rate"]["unmeasured_reason"] = (
                            "no recipe execution reached a terminal state in "
                            "the window — either none started (lifecycle "
                            "emission ships with dchub-mcp-server 2026-07-30 "
                            "and the table fills from there) or all are "
                            "still in flight. Completion is unmeasurable, "
                            "not 0%.")
                except Exception as e:
                    logger.warning("[agent-success] recipe lifecycle: %s",
                                   str(e)[:150])

                # ── calls per active agent — canonical basis (v5) ───────────
                # The canonical query aliases agents FIRST, calls second.
                try:
                    can_agents, can_calls = _bounded(cur, _SQL_CANONICAL_ACTIVITY)
                    can_agents, can_calls = int(can_agents or 0), int(can_calls or 0)
                    blocks["calls_per_active_agent_7d"] = _metric_block(
                        "calls_per_active_agent_7d",
                        round(can_calls / can_agents, 1) if can_agents else None,
                        "MEASURED" if can_agents else "UNMEASURED",
                        real_external_calls_7d=can_calls,
                        real_external_agents_7d=can_agents,
                        basis=CANONICAL_AGENTS_BASIS,
                        baseline_observed=dict(CALLS_PER_AGENT_BASELINE))
                    if not can_agents:
                        blocks["calls_per_active_agent_7d"]["unmeasured_reason"] = (
                            "no real external agent in the window — a "
                            "per-agent mean off nobody is unmeasurable, not 0")
                except Exception as e:
                    logger.warning("[agent-success] canonical activity: %s",
                                   str(e)[:150])

                # ── cohorts + planner penetration (one query, v5) ───────────
                try:
                    rows = _bounded(cur, _SQL_COHORT_PENETRATION, fetch="all")
                    cohorts, coh_total, planner_total = _cohort_rollup(rows)
                    blocks["agent_cohorts_7d"] = _metric_block(
                        "agent_cohorts_7d", coh_total, "MEASURED",
                        cohorts={n: c["agents"] for n, c in cohorts.items()},
                        reactivation_gap_days=REACTIVATION_GAP_DAYS,
                        history_lookback_days=COHORT_HISTORY_LOOKBACK_DAYS)
                    blocks["planner_penetration_by_cohort_pct"] = _metric_block(
                        "planner_penetration_by_cohort_pct",
                        _rate(planner_total, coh_total),
                        "MEASURED" if coh_total else "UNMEASURED",
                        numerator=planner_total, denominator=coh_total,
                        denominator_definition="active agents in the window "
                                               "(identity unit)",
                        front_door=FRONT_DOOR,
                        by_cohort={
                            n: {**c, "planner_first_pct":
                                _rate(c["planner_first_agents"], c["agents"])}
                            for n, c in cohorts.items()})
                    if not coh_total:
                        blocks["planner_penetration_by_cohort_pct"][
                            "unmeasured_reason"] = (
                            "no active agent in the window — penetration is "
                            "unmeasurable, not 0%")
                except Exception as e:
                    logger.warning("[agent-success] cohorts: %s", str(e)[:150])

                # ── generic-bucket share (the attribution gate's evidence) ──
                try:
                    generic_calls, real_calls, mcp_share = (
                        measure_generic_bucket_share(cur))
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
        "definition_version": 2,
        "definition_changelog": {
            1: "initial — born gated: publishes only after "
               f"{ATTRIBUTION_MIN_ACCUMULATION_DAYS}d of post-fix "
               "accumulation AND a verified generic-bucket drop",
            2: "2026-08-05: the share and the split move onto the canonical "
               "population (is_public_ip AND is_real_external) — the same "
               "population tool_calls_7d now counts, so platforms[].calls "
               "still sums to it exactly. v1's wider population read the "
               "generic share as 25.1% while the agent-expansion shell, "
               "running the canonical one, read 21.6% for the identical "
               "question on the identical minute; nearly the whole gap was "
               "CF-POP traffic, which lands in the generic bucket by "
               "construction. The gate's evidence now comes from ONE exported "
               "query (measure_generic_bucket_share) that both boards call",
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
                "calls_per_active_agent_7d":
                    blocks["calls_per_active_agent_7d"],
                "agent_cohorts_7d": blocks["agent_cohorts_7d"],
                "median_time_to_first_result_ms":
                    blocks["median_time_to_first_result_ms"],
                "second_recipe_take_up_pct":
                    blocks["second_recipe_take_up_pct"],
            },
            "note": "recipe COMPLETION became first-class in v4 (the round-3 "
                    "'needs a schema change' placeholder is retired) — it "
                    "lives under execution_quality as recipe_completion_rate, "
                    "because completing is a quality property, not a "
                    "discovery one",
        },
        {
            "section": "planner_adoption",
            "question": SECTION_QUESTIONS["planner_adoption"],
            "metrics": {
                "planner_adoption_pct": blocks["planner_adoption_pct"],
                "planner_penetration_by_cohort_pct":
                    blocks["planner_penetration_by_cohort_pct"],
            },
            "note": "two grains on purpose: adoption conditions on "
                    "opportunity episodes (2+ calls, agent-day grain, "
                    "mcp_call_log); penetration is unconditional over active "
                    "agents at the identity grain, sliced by cohort — "
                    "whether the front door reaches new agents before "
                    "habits form, holds returning ones, and greets "
                    "reactivated ones",
            "context": episode_context or None,
        },
        {
            "section": "execution_quality",
            "question": SECTION_QUESTIONS["execution_quality"],
            "metrics": {
                "manual_orchestration_pct": blocks["manual_orchestration_pct"],
                "episode_result_rate": blocks["episode_result_rate"],
                "recipe_completion_rate": blocks["recipe_completion_rate"],
            },
            "note": "manual orchestration is the OBSERVED hand-chaining shape "
                    "(the judgement 'bypass' stays on the admin surface); "
                    "result rate is the completion floor at the call grain; "
                    "recipe_completion_rate (v4) is the first-class lifecycle "
                    "read — did the whole recipe reach a terminal state, and "
                    "which one",
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
    recipe_den = blocks["recipe_completion_rate"].get("denominator")
    coh_den = blocks["planner_penetration_by_cohort_pct"].get("denominator")
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
        "cohort_denominator": {
            "state": ("unavailable" if coh_den is None
                      else "sparse" if coh_den < 30 else "stable"),
            "why": f"{coh_den} active agent(s) partitioned into cohorts — "
                   "per-cohort penetration slices are smaller still; read "
                   "them as counts, not trends"
                   if coh_den is not None else
                   "cohort source unavailable this build",
        },
        "recipe_lifecycle_denominator": {
            "state": ("unavailable" if recipe_den is None
                      else "sparse" if recipe_den < 30 else "stable"),
            "why": f"{recipe_den} terminal recipe execution(s) in the window "
                   "— read the completion rate as a count, not a trend"
                   if recipe_den is not None else
                   "recipe_executions unavailable this build (the table "
                   "fills only after the schema migration is applied and "
                   "the gateway emission deploys, both 2026-07-30)",
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
