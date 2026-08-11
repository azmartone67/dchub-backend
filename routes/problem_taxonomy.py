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
# v3 (2026-08-10): + COVERAGE — the per-problem routing map (entry call, real
# workflow, maturity, published limits). in_scope answers "is this a DC Hub
# question"; coverage answers "can you answer MY question, in how many steps,
# and what will you refuse to tell me" — the question a router actually has,
# and one a tool COUNT cannot answer.
# v2 (2026-08-01): + WHY_LIVE_REASONS — the enumerated live-data reason set
# (ChatGPT round-11: enum so the stamped replays aggregate; free text would
# be a corpus nobody can count).
TAXONOMY_VERSION = 3

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
         coverage_payload()],
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
    }
    resp = jsonify(body)
    resp.headers["Cache-Control"] = "public, max-age=3600"
    return resp
