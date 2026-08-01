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
TAXONOMY_VERSION = 1

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

problem_taxonomy_bp = Blueprint("problem_taxonomy", __name__)


def contract_hash() -> str:
    """Stable hash of the published contract (anchor_intents.contract_hash
    discipline): a consumer detects drift by comparing hashes, re-derives only
    on change. Hashes version + BOTH ordered lists + the note — order is part
    of the contract because the prose renders join in order.
    """
    canon = json.dumps(
        [TAXONOMY_VERSION, list(IN_SCOPE), list(OUT_OF_SCOPE), NOT_FOR_NOTE],
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
