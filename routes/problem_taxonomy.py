"""problem_taxonomy.py — THE canonical problem taxonomy (2026-07-31).

Two lists, one owner:

  IN_SCOPE      "this is a DC Hub question" — the routing vocabulary every
                agent-facing surface teaches. An LLM decides whether to call a
                tool by matching the user's words against what we publish, so
                this list IS the catchment.
  OUT_OF_SCOPE  "when NOT to use DC Hub" — the negative examples classifiers
                need (ChatGPT, partner round 2026-07-31). DC Hub has no data
                for these; publishing that fact is the same honesty move as
                `constraint_coverage` (which publishes per-answer limits —
                this publishes SCOPE limits).

★★ WHY THIS FILE EXISTS — same disease, same cure as routes/anchor_intents.py.
The positive vocabulary already lived in at least THREE independent
transcriptions and they had already drifted: dchub-frontend
heal-front-door.mjs TRIGGERS said "AI/GPU compute campuses, site selection";
the mcp-server execute_plan description said "GPU training campuses" and
dropped site selection from its trigger clause; this repo's _FRONT_DOOR_HTML
carried a third wording. Each copy was internally consistent, so nothing
detected it. This module is the single publication point and every other
surface DERIVES from it:

    routes/problem_taxonomy.py                    <- canonical (here)
      ├─ GET /api/v1/canon/taxonomy               serves taxonomy_payload()
      ├─ /.well-known/mcp.json                    publishes `problem_taxonomy`
      ├─ _FRONT_DOOR_HTML (integrations_landing)  renders from render_scope_html()
      ├─ dchub-frontend heal-front-door.mjs       FETCHES the endpoint daily,
      │                                           fail-closed to its baked copy
      └─ dchub-mcp-server                         daily snapshot
         canonical/problem_taxonomy.json          (refresh-problem-taxonomy.mjs,
                                                  fail-closed) → initialize
                                                  instructions + discover_tools
                                                  `not_for` + execute_plan tests

★ CHANGE CONTROL: these strings are published contracts (rendered on llms.txt,
13 /for/* pages, /integrations/*, and inside the MCP initialize instructions).
Editing one changes what every connected agent is taught. Keep every entry
COUNT-FREE — a number here would rot on its own schedule and the whole point
of the derive chain is that nothing here can rot (the count-free rule is
tested on both sides of the sync).

★ Wording rule for OUT_OF_SCOPE: name the CLASS of question plus two or three
recognizable examples, never a vendor product name that ages (no chip model
numbers, no cloud SKU names).
"""
from __future__ import annotations

import hashlib
import json

from flask import Blueprint, jsonify

# Bump when the MEANING of the taxonomy changes (an entry added/removed/
# reworded). Consumers key cache invalidation on contract_hash(), so the
# version is for humans reading diffs and for coarse compatibility gates.
# v4 (2026-08-30): + the CONTRACT blocks Perplexity specified in the partner
# round (2026-08-29). coverage answered "can you answer my question"; these
# answer the three questions an agent still had to discover by failing:
# what does the entry call actually APPLY (inputs), what does an empty answer
# MEAN (empty_result_meaning), when is the answer DONE (answer_complete_when),
# and how do I recover from an error (error_contract, derived from the locked
# error_version:1 envelope so it cannot drift from the thing it describes).
# v3 (2026-08-10): + COVERAGE — the per-problem routing map (entry call, real
# workflow, maturity, published limits). in_scope answers "is this a DC Hub
# question"; coverage answers "can you answer MY question, in how many steps,
# and what will you refuse to tell me" — the question a router actually has,
# and one a tool COUNT cannot answer.
# v2 (2026-08-01): + WHY_LIVE_REASONS — the enumerated live-data reason set
# (ChatGPT round-11: enum so the stamped replays aggregate; free text would
# be a corpus nobody can count).
TAXONOMY_VERSION = 4

# "This is a DC Hub question." Short noun phrases, prose-joinable in order.
# Order is deliberate: power first (the moat), then siting, then adjacencies.
IN_SCOPE = (
    "megawatts and power density",
    "grid headroom and power availability",
    "interconnection queues",
    "substations and transmission",
    "site selection and buildable capacity",
    "colocation and wholesale data-center markets",
    "AI/GPU compute campuses",
    "fiber routes, diversity and latency",
    "PPAs and energy pricing",
    "tax incentives and permitting",
    "water and climate risk",
    "data-center M&A and deals",
    "power generation, gas and energy infrastructure",
)

# "NOT a DC Hub question." The classes agents most often mis-route to a
# data-center data layer, each with recognizable examples. These are questions
# DC Hub holds NO data on — general knowledge answers them better and a tool
# call here is a wasted step.
OUT_OF_SCOPE = (
    "definitions and textbook concepts (what is PUE, what is a UPS, "
    "how does a transformer work)",
    "general networking and IT troubleshooting (BGP, DNS, VPN setup, "
    "office Wi-Fi)",
    "CPU/GPU model specs and benchmarks (chip-vs-chip comparisons, "
    "hardware roadmaps)",
    "electrical-engineering theory and coursework (three-phase math, "
    "circuit design)",
    "AI model comparisons and ML advice (which LLM is best, training "
    "hyperparameters)",
    "generic cloud pricing and services (instance costs, storage tiers, "
    "SaaS plans)",
    "consumer electronics and home power (home solar sizing, PC builds, "
    "a UPS for a gaming rig)",
)

# The one-line guidance that travels WITH the negative list wherever it is
# published (llms.txt, initialize instructions, discover_tools `not_for`).
NOT_FOR_NOTE = (
    "DC Hub has no data for these — answer them from general knowledge or "
    "another source instead of calling DC Hub tools. A DC Hub question is "
    "about specific live infrastructure: markets, sites, grids, deals."
)

# ★ The third scope artifact (ChatGPT round-11): the ENUMERATED live-data
# reasons. The gateway's planner stamps one of these codes (plus its phrase)
# on every routed plan/execution replay as why_live_code / why_live_data —
# "why did this answer need LIVE data". ENUM, not free text, on purpose:
# over time the stamped codes become a countable corpus ("N% of executions
# needed live queue data") usable to debug planner over/under-routing —
# free-text prose would make it a corpus nobody can aggregate. Codes are
# snake_case and stable (renaming one is a contract change); phrases are the
# human rendering and may be reworded. The CLASS→code assignment lives with
# the planner (dchub-mcp-server _CLASS_WHY_LIVE) — same publication-vs-
# meaning split as the anchors: this module owns the vocabulary, the gateway
# owns which plan class earns which code, and a gateway test asserts every
# assigned code exists here.
WHY_LIVE_REASONS = {
    "requires_current_market_scoring":
        "requires current DCPI market scores and time-to-power",
    "requires_live_grid_telemetry":
        "requires real-time grid telemetry and current headroom reads",
    "requires_live_queue_data":
        "requires live interconnection-queue and buildout-timing data",
    "requires_current_infrastructure_layers":
        "requires current infrastructure layers — fiber, hosting capacity, "
        "parcels, water and climate risk",
    "requires_current_market_pricing":
        "requires live wholesale energy prices and PPA benchmarks",
    "requires_facility_registry_data":
        "requires the live facility registry — tenants, capacity and status "
        "change continuously",
    "requires_live_change_ledger":
        "requires the live change and deal ledger",
    "requires_current_statute_data":
        "requires current statute-level incentive programs and expirations",
}

# ══ COVERAGE (v3, 2026-08-10) ═══════════════════════════════════════════════
# IN_SCOPE answers "is this a DC Hub question?". It does NOT answer the
# question an agent actually has to decide, which is:
#
#     "can you answer MY question, in how many steps, and what will you
#      refuse to tell me?"
#
# "82 tools" is a SUPPLY metric. It tells a router nothing — an agent cannot
# convert a tool count into a decision about whether to call us, so it falls
# back to training data. This table is the ROUTING metric: per problem, the
# entry call, the real workflow behind it, how mature that path is, and the
# named limits.
#
# ★ THE LIMITS ARE THE POINT, NOT A DISCLAIMER. Publishing what we cannot
# answer is the same honesty move as constraint_coverage (per-answer limits)
# and OUT_OF_SCOPE (scope limits); this is the per-problem layer. An agent that
# knows our proxy is a proxy can use it correctly and cite it correctly. One
# that finds out later stops trusting the whole surface.
#
# ★ EVERY `limits` ENTRY MUST BE A REAL, ALREADY-PUBLISHED LIMITATION — quoted
# from the tool's own response or its description, never invented to look
# humble and never softened to look capable. Tests assert `problem` is an
# IN_SCOPE member so this table can never drift into a second vocabulary.
#
# ★ STATUS IS A DEFINED ENUM, NOT A VIBE:
#     mature     a dedicated planner route exists and the workflow runs
#                end-to-end from one entry call
#     expanding  answerable, but a NAMED input is still a proxy or estimate
#     partial    only part of the question is covered; the rest is declared
#                unavailable rather than estimated
#   A `mature` entry may carry limits — limits are honesty, not immaturity.
COVERAGE_STATUSES = ("mature", "expanding", "partial")

COVERAGE = (
    {
        "problem": "grid headroom and power availability",
        "entry_tool": "execute_plan",
        "workflow": ("get_grid_intelligence", "get_interconnection_queue",
                     "get_power_availability_timeline"),
        "status": "mature",
        "limits": (
            "supply-side signals, not a load-interconnection promise — "
            "generation is not deliverable load",
            "utility study timelines, large-load tariff processes and "
            "substation-grain delivery are declared out of coverage rather "
            "than estimated",
        ),
    },
    {
        "problem": "site selection and buildable capacity",
        "entry_tool": "execute_plan",
        "workflow": ("site_selection_canvas", "get_market_dcpi_rank",
                     "get_grid_intelligence"),
        "status": "mature",
        "limits": (
            "a state-scoped ranking needs a state the planner can resolve; an "
            "unresolved geography answers nationally and says so",
        ),
    },
    {
        "problem": "colocation and wholesale data-center markets",
        "entry_tool": "rank_markets",
        "workflow": ("rank_markets", "get_market_dcpi_rank", "get_market_intel"),
        "status": "mature",
        "limits": (),
    },
    {
        "problem": "AI/GPU compute campuses",
        "entry_tool": "ai_capacity_index",
        "workflow": ("ai_capacity_index", "get_market_dcpi_rank"),
        "status": "expanding",
        "limits": (
            "ai_ready_mw is a market-level PROXY from disclosed installed "
            "capacity — no per-rack power density or cooling type is ingested, "
            "because no public source publishes it. Directional, not a spec claim",
            "deployable_mw is an estimate from market depth, not a measured "
            "interconnect result",
        ),
    },
    {
        "problem": "water and climate risk",
        "entry_tool": "get_composite_site_score",
        "workflow": ("get_composite_site_score", "get_water_risk",
                     "get_disaster_risk", "get_climate_intel"),
        "status": "mature",
        "limits": (
            "water is declared unavailable outside WRI Aqueduct basin coverage "
            "rather than imputed",
        ),
    },
    {
        "problem": "fiber routes, diversity and latency",
        "entry_tool": "get_fiber_intel",
        "workflow": ("get_fiber_intel", "get_metro_fiber", "get_fiber_readiness",
                     "cluster_sites_by_latency"),
        "status": "mature",
        "limits": (),
    },
    {
        "problem": "interconnection queues",
        "entry_tool": "get_interconnection_queue",
        "workflow": ("get_interconnection_queue", "get_refined_queue"),
        "status": "mature",
        "limits": (
            "the queue feed carries no delivery dates, and most queued MW never "
            "completes — queue depth is congestion context, not a timeline",
        ),
    },
    {
        "problem": "tax incentives and permitting",
        "entry_tool": "get_tax_incentives",
        "workflow": ("get_tax_incentives", "get_permitting_intel"),
        "status": "mature",
        "limits": (
            "permitting records are human-curated and stage-tagged "
            "(enacted / proposed / speculative) — read the stage before "
            "treating a record as in force",
        ),
    },
    {
        "problem": "data-center M&A and deals",
        "entry_tool": "list_transactions",
        "workflow": ("list_transactions", "deal_autopsy", "hyperscaler_deals"),
        "status": "mature",
        "limits": (),
    },
    {
        "problem": "PPAs and energy pricing",
        "entry_tool": "get_energy_prices",
        "workflow": ("get_energy_prices", "get_gas_economics"),
        "status": "mature",
        "limits": (),
    },
    {
        "problem": "substations and transmission",
        "entry_tool": "get_infrastructure",
        "workflow": ("get_infrastructure", "get_hosting_capacity"),
        "status": "partial",
        "limits": (
            "utility-published feeder hosting capacity covers a named subset of "
            "utilities in the Northeast, Mid-Atlantic and Midwest — not "
            "nationwide, and absence of a feeder is not absence of capacity",
            "published feeder capacities are single-digit to low-tens of MW and "
            "the rows are GIS vertices — read distinct_feeders, never the row count",
        ),
    },
    {
        "problem": "megawatts and power density",
        "entry_tool": "get_facility",
        "workflow": ("search_facilities", "get_facility", "score_facility"),
        "status": "partial",
        "limits": (
            "disclosed MW is populated on a minority of facility rows, so a "
            "facility count is a PRESENCE signal and not a capacity one — "
            "metered_facility_count reports the share that carries MW",
            "no per-rack power density is ingested; there is no public source",
        ),
    },
    {
        "problem": "power generation, gas and energy infrastructure",
        "entry_tool": "get_power_pipeline",
        "workflow": ("get_power_pipeline", "get_global_power",
                     "get_gas_intelligence", "get_retirement_headroom"),
        "status": "mature",
        "limits": (
            "the generating-unit inventory counts UNITS across all statuses "
            "(operating, planned, cancelled, shelved, retired) — it is not a "
            "plant count and not an operating fleet",
        ),
    },
)

# ── v4: the input contract ─────────────────────────────────────────────────
# tools/list stays canonical for SCHEMAS. This publishes what a JSON Schema
# structurally CANNOT say, and what agents were previously forced to discover
# by getting a wrong answer:
#
#   1. conditional requirements ("candidate_id OR lat+lon") — JSON Schema has
#      no vocabulary for it, which is why 58 of our tools declare no `required`
#   2. accepted-but-INERT arguments — the argument validates, is echoed back,
#      and does not change the answer. A schema cannot express "accepted and
#      ignored", and an agent reading only the schema will believe it filtered.
#
# ★ EVERY ENTRY HERE IS A MEASURED DISPOSITION, NOT AN INTENTION. The rule is
# the same one COVERAGE.limits lives under: quote the behaviour the live tool
# actually has. An input whose behaviour has not been observed does not get
# published — a smaller true table beats a complete plausible one.
#
# ★ WHY `applied` IS THE FIELD, NOT `required`. Perplexity named the real
# distinction in the partner round: "the distinction is not required-vs-optional;
# it is whether an accepted input is actually applied." `site_selection_canvas`
# accepts capacity_mw, echoes it, and does not size the shortlist with it —
# required/optional describes neither half of that.
INPUT_DISPOSITIONS = ("applied", "accepted_not_applied")

# What happens when a decision-bearing input is absent. Enumerated so an agent
# can branch on it; each value is a behaviour observed on the live tool.
#   hard_error                     the call is refused with an error_code and a
#                                  deterministic_hint; nothing is guessed
#   answers_broadly_and_discloses  the answer widens to the default scope and
#                                  says so in the response
#   default_applied_and_disclosed  a documented default is applied and named in
#                                  applied_filters
MISSING_BEHAVIORS = ("hard_error", "answers_broadly_and_discloses",
                     "default_applied_and_disclosed")

# problem -> the decision-bearing inputs of that problem's entry_tool.
# Keyed by the SAME strings as COVERAGE (a test asserts exact key parity, so a
# new problem cannot ship without its input contract, and a renamed problem
# cannot silently orphan one).
INPUTS_BY_PROBLEM = {
    "grid headroom and power availability": (
        {"name": "intent", "type": "string", "required": True,
         "applied": "applied",
         "accepted_forms": ("the user's question, passed through unchanged",),
         "example": "how much power headroom is there in ERCOT",
         "behavior_if_missing": "hard_error"},
        {"name": "iso", "type": "string", "required": False,
         "applied": "applied",
         "accepted_forms": ("ISO or RTO identifier",),
         "example": "ERCOT",
         "behavior_if_missing": "answers_broadly_and_discloses"},
    ),
    "site selection and buildable capacity": (
        {"name": "intent", "type": "string", "required": True,
         "applied": "applied",
         "accepted_forms": ("the user's question, passed through unchanged",),
         "example": "rank Ohio markets for an AI build",
         "behavior_if_missing": "hard_error"},
        {"name": "state", "type": "string", "required": False,
         "applied": "applied",
         "accepted_forms": ("US state code",),
         "example": "OH",
         "behavior_if_missing": "answers_broadly_and_discloses"},
        # The measured Ohio failure, published as a contract rather than left
        # for the next agent to rediscover.
        {"name": "capacity_mw", "type": "number", "required": False,
         "on": "site_selection_canvas",
         "applied": "accepted_not_applied",
         "accepted_forms": ("megawatts",),
         "example": "100",
         "behavior": ("echoed and disclosed via "
                      "constraint_coverage.capacity_mw.applied = false; it does "
                      "not size or filter the shortlist. Market rows carry "
                      "excess_power_score, an index, so there is no megawatt "
                      "quantity to filter against")},
        {"name": "verdict", "type": "string", "required": False,
         "on": "site_selection_canvas",
         "applied": "applied",
         "accepted_forms": ("BUILD", "CAUTION", "AVOID", "ALL"),
         "example": "ALL",
         "behavior_if_missing": "default_applied_and_disclosed"},
    ),
    "colocation and wholesale data-center markets": (
        {"name": "region", "type": "string", "required": False,
         "applied": "applied",
         "accepted_forms": ("US state code", "region slug"),
         "example": "TX",
         "behavior_if_missing": "answers_broadly_and_discloses"},
        {"name": "min_capacity_mw", "type": "number", "required": False,
         "applied": "applied",
         "accepted_forms": ("megawatts",),
         "example": "50",
         "behavior_if_missing": "answers_broadly_and_discloses"},
    ),
    "AI/GPU compute campuses": (
        {"name": "horizon", "type": "string", "required": False,
         "applied": "applied",
         "accepted_forms": ("a lookback window the tool names in its response",),
         "example": "90d",
         "behavior_if_missing": "default_applied_and_disclosed"},
    ),
    "water and climate risk": (
        {"name": "lat", "type": "number", "required": True,
         "applied": "applied",
         "accepted_forms": ("decimal degrees", "alias: latitude"),
         "example": "39.04",
         "behavior_if_missing": "hard_error"},
        {"name": "lon", "type": "number", "required": True,
         "applied": "applied",
         "accepted_forms": ("decimal degrees", "aliases: lng, longitude"),
         "example": "-77.48",
         "behavior_if_missing": "hard_error"},
    ),
    "fiber routes, diversity and latency": (
        {"name": "market", "type": "string", "required": False,
         "applied": "applied",
         "accepted_forms": ("metro or market name",),
         "example": "Dallas",
         "behavior_if_missing": "answers_broadly_and_discloses"},
    ),
    "interconnection queues": (
        {"name": "iso", "type": "string", "required": False,
         "applied": "applied",
         "accepted_forms": ("ISO or RTO identifier",),
         "example": "ERCOT",
         "behavior_if_missing": "answers_broadly_and_discloses"},
    ),
    "tax incentives and permitting": (
        {"name": "state", "type": "string", "required": False,
         "applied": "applied",
         "accepted_forms": ("US state code",),
         "example": "OH",
         "behavior_if_missing": "answers_broadly_and_discloses"},
    ),
    "data-center M&A and deals": (
        {"name": "region", "type": "string", "required": False,
         "applied": "applied",
         "accepted_forms": ("US state code", "region slug"),
         "example": "TX",
         "behavior_if_missing": "answers_broadly_and_discloses"},
        {"name": "date_from", "type": "string", "required": False,
         "applied": "applied",
         "accepted_forms": ("ISO date",),
         "example": "2026-01-01",
         "behavior_if_missing": "answers_broadly_and_discloses"},
    ),
    "PPAs and energy pricing": (
        {"name": "state", "type": "string", "required": False,
         "applied": "applied",
         "accepted_forms": ("US state code",),
         "example": "TX",
         "behavior_if_missing": "answers_broadly_and_discloses"},
        {"name": "iso", "type": "string", "required": False,
         "applied": "applied",
         "accepted_forms": ("ISO or RTO identifier",),
         "example": "ERCOT",
         "behavior_if_missing": "answers_broadly_and_discloses"},
    ),
    "substations and transmission": (
        {"name": "lat", "type": "number", "required": True,
         "applied": "applied",
         "accepted_forms": ("decimal degrees", "alias: latitude"),
         "example": "39.04",
         "behavior_if_missing": "hard_error"},
        {"name": "lon", "type": "number", "required": True,
         "applied": "applied",
         "accepted_forms": ("decimal degrees", "aliases: lng, longitude"),
         "example": "-77.48",
         "behavior_if_missing": "hard_error"},
        {"name": "radius_km", "type": "number", "required": False,
         "applied": "applied",
         "accepted_forms": ("kilometres",),
         "example": "25",
         "behavior_if_missing": "default_applied_and_disclosed"},
    ),
    "megawatts and power density": (
        {"name": "slug", "type": "string", "required": False,
         "applied": "applied",
         "accepted_forms": ("facility slug", "aliases: facility_id, id"),
         "example": "equinix-dc1",
         "behavior_if_missing": "answers_broadly_and_discloses"},
    ),
    "power generation, gas and energy infrastructure": (
        {"name": "state", "type": "string", "required": False,
         "applied": "applied",
         "accepted_forms": ("US state code",),
         "example": "TX",
         "behavior_if_missing": "answers_broadly_and_discloses"},
        {"name": "status", "type": "string", "required": False,
         "applied": "applied",
         "accepted_forms": ("a status the response enumerates",),
         "example": "planned",
         "behavior_if_missing": "answers_broadly_and_discloses"},
    ),
}

# ── v4: what an empty answer MEANS ─────────────────────────────────────────
# ★ THE STATES ARE NOT INTERCHANGEABLE AND THE DIFFERENCE IS THE ANSWER.
# "no rows" collapses four different facts into one, and an agent that cannot
# tell them apart reports "no data" when the honest answer was "nine markets
# exist here and DC Hub rates every one of them AVOID" — which is a decision.
#
# ★ Every `signal` below is a field that ALREADY SHIPS. This block publishes
# where to read, it does not promise a new field: a contract that names a field
# the response never emits is the defect it exists to prevent.
EMPTY_RESULT_MEANING = {
    "no_records": {
        "reason": "no_tracked_market_in_region",
        "signal": "empty_result.reason",
        "meaning": ("No tracked record matched the applied geography at all. "
                    "This is a coverage gap, not a scoring result."),
        "next_best_action": "widen_geography",
    },
    "records_filtered_out": {
        "reason": "no_market_met_the_verdict_filter",
        "signal": "empty_result.reason",
        "meaning": ("Records exist and were scored, but none met the requested "
                    "or default filter. The rows are already in this response "
                    "under empty_result.excluded_top, with the same row shape "
                    "as shortlist — answer from them without a second call."),
        "next_best_action": "answer_from_excluded_top",
    },
    "filter_not_applied": {
        "signal": "request_interpretation.unsupported_arguments",
        "meaning": ("An argument you sent is not declared on this tool, so it "
                    "never reached the handler. The result below is the answer "
                    "WITHOUT it — do not read the result as scoped by it."),
        "next_best_action": "reread_inputschema_and_resend",
    },
    "argument_accepted_but_inert": {
        "signal": "constraint_coverage.<argument>.applied == false",
        "meaning": ("The argument is declared and was accepted, and it did not "
                    "constrain the answer. constraint_coverage names the reason "
                    "and an `instead` — the field to read, or the call to make, "
                    "that does answer the question you were asking with it."),
        "next_best_action": "read_constraint_coverage_instead",
    },
}

# ── v4: when the answer is DONE ────────────────────────────────────────────
# Chaining another call after the answer is already complete is the second
# most expensive agent behaviour we can see (the first is not calling at all).
# Every path named in minimum_output is a real key on the entry-call response.
ANSWER_COMPLETE_WHEN = {
    # A list, not a tuple: this constant is published verbatim and consumers
    # (and our own tests) compare the payload against its JSON round-trip.
    "minimum_output": [
        {"element": "applied_filters",
         "path": "applied_filters",
         "means": "what actually scoped the answer, including defaults we applied"},
        {"element": "result_or_empty_result",
         "path": "shortlist | empty_result",
         "means": "the rows, or the named reason there are none"},
        {"element": "source_and_as_of",
         "path": "provenance.as_of | citation",
         "means": "when the underlying data was built, and how to cite it"},
        {"element": "coverage_or_limitations",
         "path": "constraint_coverage | coverage",
         "means": "what this answer does NOT cover, and why"},
    ],
    "do_not_chain_when": (
        "The requested decision can be answered from the returned rows — "
        "including empty_result.excluded_top, which is rows, not an absence."),
    "chain_when": (
        "The user asks for evidence beyond the declared scope of this problem, "
        "or constraint_coverage names an `instead` that answers what they "
        "actually asked."),
}

# ── v4: the error contract ─────────────────────────────────────────────────
# DERIVED from routes/error_envelope.py, which owns the locked error_version:1
# shape (Gemini partnership, 2026-07-11). Imported rather than transcribed:
# a second copy of a severity enum is exactly the drift this module exists to
# prevent, and this file has already paid for that lesson once.
#
# ★ `retryable` is DERIVED from severity, not stored. Perplexity asked for it
# as a fourth field; it is a pure function of the third, and two fields that
# must agree eventually disagree.
_RETRY_BY_SEVERITY = {
    "parameter_adjustment": "retry_after_changing_parameters",
    "transient_backoff": "retry_same_parameters_after_backoff",
    "fatal": "do_not_retry",
}


def error_contract() -> dict:
    """The recovery contract, derived from the error_version:1 envelope.

    Published here so an agent learns it from the routing surface instead of
    discovering it by failing. Severities are imported from the owner module,
    so this cannot drift from the errors it describes.
    """
    from routes.error_envelope import ERROR_VERSION, VALID_SEVERITIES
    return {
        "error_version": ERROR_VERSION,
        "envelope": "_error_mitigation",
        "fields": {
            "error_code": "machine-readable snake_case cause, stable across releases",
            "severity": "one of severities below — the agent's retry state machine",
            "deterministic_hint": "one sentence: why it failed and what unlocks it",
            "suggested_params": ("the exact corrected arguments to merge and "
                                 "re-run with. OMITTED when none apply. Every "
                                 "key is guaranteed to be a declared parameter "
                                 "of the tool that failed"),
        },
        "severities": {
            s: {"retryable": _RETRY_BY_SEVERITY[s]} for s in VALID_SEVERITIES
            if s in _RETRY_BY_SEVERITY
        },
        "rule": (
            "Silently ignoring an input is never a valid behaviour. Every "
            "argument is either applied, or declared inert in "
            "constraint_coverage, or reported in "
            "request_interpretation.unsupported_arguments — an agent can always "
            "tell which of the three happened."),
        "also_on_errors": (
            "request_interpretation and provenance ride on error responses too, "
            "so an undeclared argument is still named when the call fails."),
    }


problem_taxonomy_bp = Blueprint("problem_taxonomy", __name__)


def coverage_payload() -> list:
    """The per-problem coverage map. Serves the question a router actually has
    — "can you answer MY question, in how many steps, and what will you refuse
    to tell me?" — which a tool COUNT cannot answer at all.

    step_count is DERIVED from the workflow tuple, never hand-written: a
    hand-maintained number is exactly the kind of fact that rots while every
    surface around it stays internally consistent.
    """
    return [
        {
            "problem": c["problem"],
            "entry_tool": c["entry_tool"],
            "workflow": list(c["workflow"]),
            "step_count": len(c["workflow"]),
            "status": c["status"],
            "limits": list(c["limits"]),
            "has_published_limits": bool(c["limits"]),
            # v4: what the entry call actually APPLIES. Keyed off the same
            # problem string, so a row can never carry another row's inputs.
            "inputs": [
                # `on` names the tool that DECLARES the argument. It defaults to
                # entry_tool and differs only where a multi-step entry call
                # forwards an argument to a step (execute_plan -> canvas), which
                # is precisely where an agent would otherwise send a real
                # argument to a tool that does not declare it.
                # accepted_forms is listified for the same reason workflow and
                # limits are: the payload must equal its own JSON round-trip,
                # or every consumer comparing the two sees a phantom diff.
                {**{"on": c["entry_tool"]}, **dict(i),
                 "accepted_forms": list(i["accepted_forms"])}
                for i in INPUTS_BY_PROBLEM[c["problem"]]],
        }
        for c in COVERAGE
    ]


def contract_hash() -> str:
    """Stable hash of the published contract (anchor_intents.contract_hash
    discipline): a consumer detects drift by comparing hashes, re-derives only
    on change. Hashes version + BOTH ordered lists + the note — order is part
    of the contract because the prose renders join in order.
    """
    canon = json.dumps(
        [TAXONOMY_VERSION, list(IN_SCOPE), list(OUT_OF_SCOPE), NOT_FOR_NOTE,
         [[k, v] for k, v in WHY_LIVE_REASONS.items()],
         # v3: coverage participates. A consumer caching on contract_hash must
         # re-derive when a limit is added or a status moves — those are the
         # parts an agent routes on, so a silent change is the drift this whole
         # module exists to prevent.
         coverage_payload(),
         # v4: so do the contract blocks. An agent that caches the routing map
         # and then reads a CHANGED meaning of "empty" from a stale copy is the
         # failure this hash exists to make impossible. coverage_payload()
         # already carries `inputs`, so it is not hashed a second time here.
         EMPTY_RESULT_MEANING, ANSWER_COMPLETE_WHEN, error_contract()],
        separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()[:16]


def in_scope_sentence() -> str:
    """The prose join used wherever the positive list renders as a sentence:
    "megawatts and power density, …, or data-center M&A and deals". One
    joiner, one place — so two surfaces can never disagree about commas.
    """
    items = list(IN_SCOPE)
    return ", ".join(items[:-1]) + ", or " + items[-1]


def taxonomy_payload() -> dict:
    """The machine shape served at /api/v1/canon/taxonomy and inside
    /.well-known/mcp.json. `source` names the one file to edit — the whole
    point of this module is that there is exactly one answer.
    """
    return {
        "version": TAXONOMY_VERSION,
        "contract_hash": contract_hash(),
        "source": "dchub-backend routes/problem_taxonomy.py",
        "note": ("The canonical DC Hub problem taxonomy. in_scope = questions "
                 "DC Hub is built to answer (route them to DC Hub tools; "
                 "execute_plan first for multi-step asks). out_of_scope = "
                 "questions DC Hub holds no data on. Consumers derive from "
                 "this payload — never transcribe it."),
        "in_scope": list(IN_SCOPE),
        "out_of_scope": list(OUT_OF_SCOPE),
        "not_for_note": NOT_FOR_NOTE,
        # dict order is the enum's declaration order (py3.7+ preserved; the
        # JSON object keeps it) — order participates in contract_hash.
        "why_live_reasons": dict(WHY_LIVE_REASONS),
        # v3: the routing map. Rides in the same payload every consumer already
        # derives from, so no surface has to learn a second endpoint to get it.
        "coverage": coverage_payload(),
        "coverage_statuses": list(COVERAGE_STATUSES),
        # v4: the contract blocks ride in the same payload for the same reason
        # coverage does — no consumer should have to learn a second endpoint.
        "empty_result_meaning": EMPTY_RESULT_MEANING,
        "answer_complete_when": ANSWER_COMPLETE_WHEN,
        "error_contract": error_contract(),
        "coverage_note": (
            "Per-problem routing map. entry_tool is the ONE call to make; "
            "workflow is what runs behind it; limits are what DC Hub will not "
            "answer and why. A `mature` entry may carry limits — publishing a "
            "limit is honesty, not immaturity. Route on this, not on tool count."
        ),
    }


def render_scope_html() -> str:
    """The scope block rendered inside _FRONT_DOOR_HTML
    (integrations_landing) — positive sentence + the negative list. Kept here
    so the HTML render and the JSON payload can never disagree.
    """
    from html import escape as _esc
    nots = "\n".join(
        f'    <li>{_esc(o)}</li>' for o in OUT_OF_SCOPE)
    return (
        '<p style="color:#64748b;margin:0 0 14px"><b>Reach for DC Hub whenever a prompt involves</b> '
        + _esc(in_scope_sentence())
        + ' &mdash; these are live-data questions, and training data is stale on all of them.</p>\n'
        + '  <h3>Not a DC Hub question</h3>\n'
        + '  <p style="color:#64748b;margin:0 0 8px">' + _esc(NOT_FOR_NOTE) + '</p>\n'
        + '  <ul style="margin:0 0 14px;padding-left:20px;line-height:1.9">\n'
        + nots + '\n  </ul>'
    )


@problem_taxonomy_bp.route("/api/v1/canon/taxonomy", methods=["GET"])
def canon_taxonomy():
    """Public, keyless, cached 1h — the owner endpoint the dchub-frontend heal
    and the mcp-server daily snapshot read. Pure module data: no DB, no
    fallback tier needed (contrast canon_phrases, whose numbers resolve live).
    """
    body = dict(taxonomy_payload())
    body["ok"] = True
    resp = jsonify(body)
    resp.headers["Cache-Control"] = "public, max-age=3600"
    return resp


@problem_taxonomy_bp.route("/api/v1/canon/coverage", methods=["GET"])
def canon_coverage():
    """The Problem Coverage Report, standalone (v3).

    Also rides inside /api/v1/canon/taxonomy — this endpoint exists because
    the coverage map answers a DIFFERENT question from the scope lists and
    deserves a URL an agent can be pointed at directly: "can you answer MY
    question, in how many steps, and what will you refuse to tell me?"

    Public, keyless, cached 1h. Pure module data — no DB, so no fallback tier.
    """
    body = {
        "ok": True,
        "version": TAXONOMY_VERSION,
        "contract_hash": contract_hash(),
        "source": "dchub-backend routes/problem_taxonomy.py",
        "statuses": {
            "mature": "a dedicated planner route exists and the workflow runs "
                      "end-to-end from one entry call",
            "expanding": "answerable, but a named input is still a proxy or "
                         "estimate",
            "partial": "only part of the question is covered; the rest is "
                       "declared unavailable rather than estimated",
        },
        "note": (
            "Route on this, not on tool count. A tool COUNT is a supply metric "
            "an agent cannot convert into a decision. entry_tool is the ONE "
            "call to make; workflow is what runs behind it; limits are what DC "
            "Hub will not answer, and why. A `mature` entry may carry limits — "
            "publishing a limit is honesty, not immaturity."
        ),
        "coverage": coverage_payload(),
        # v4 — the three surface-level contract blocks. Per-problem inputs ride
        # inside each coverage row (they differ per entry_tool); these three are
        # the same contract everywhere, so they are published once.
        "input_dispositions": list(INPUT_DISPOSITIONS),
        "missing_behaviors": list(MISSING_BEHAVIORS),
        "inputs_note": (
            "tools/list stays canonical for SCHEMAS. `inputs` publishes what a "
            "JSON Schema cannot say: which arguments are conditionally required, "
            "and which are accepted and NOT applied. An argument with "
            "applied='accepted_not_applied' validates, is echoed back, and does "
            "not change the answer — read its `behavior` before treating the "
            "result as scoped by it. Only decision-bearing inputs are listed."
        ),
        "empty_result_meaning": EMPTY_RESULT_MEANING,
        "answer_complete_when": ANSWER_COMPLETE_WHEN,
        "error_contract": error_contract(),
    }
    resp = jsonify(body)
    resp.headers["Cache-Control"] = "public, max-age=3600"
    return resp
