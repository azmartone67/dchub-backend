"""canon_selftest.py — the machine-readable self-test any agent can run (2026-08-11).

★★ WHY THIS EXISTS

For a week we briefed eight external agents by hand. Seven wrote back agreeing
with the brief. The one that actually CALLED THE TOOLS found a defect nobody
else had (an empty state shortlist reading as a broken execution). Prose review
does not surface a -32602, a Texas query returning Virginia, or a geography
field with two shapes.

That is a process problem, not an agent problem: a brief is a chat message. It
expires when the session ends, it reaches only whoever was pasted it, and it
cannot be re-run. So the benchmark stops being something we SEND and becomes
something we PUBLISH — one fetch, no briefing, no operator in the loop, and it
is still there next month.

    "Is this working?" should be a GET, not a conversation.

★ ASSERTIONS ARE SPLIT BY VOLATILITY, AND THAT SPLIT IS THE WHOLE DESIGN.

The obvious version of this file encodes "top row = Midland-Odessa" and is
worthless within a week: DCPI rescores nightly, the row changes, every agent
reports a false failure, and the benchmark trains people to ignore it. A
self-test that cries wolf is worse than none.

So every check carries two lists:

  invariant     Must hold on every run, forever. These encode CLASSES of
                correctness — "every returned row is inside the requested
                geography" — never a specific value. A failure here is a real
                defect and we want to hear about it.
  informational Reported, never asserted. Today's #1 market, today's score.
                Useful context for a human reading the output; never a pass or
                fail signal.

The invariant for the Texas defect is not "returns Midland-Odessa". It is
"every row has state == TX". That statement was FALSE on 2026-08-10 (it
returned Ashburn, Virginia) and is true today, and it will still be the right
assertion when every market in the table has been rescored.

★ known_gaps IS LOAD-BEARING, NOT A DISCLAIMER.

A self-test without a published defect list generates duplicate reports: an
agent finds a thing we already know, files it, and the signal-to-noise of the
whole channel drops until people stop reading it. Publishing what is already
broken is what makes "I found something" mean something.

Owner: this file. Consumers derive; nothing transcribes.
"""
from __future__ import annotations

import hashlib
import json

from flask import Blueprint, jsonify

# Bump when the MEANING of a check changes (an assertion added/removed/
# retargeted). Consumers key cache invalidation on contract_hash().
SELFTEST_VERSION = 1

# ── The checks ─────────────────────────────────────────────────────────────
# Each entry: what to call, why it exists (the defect it would have caught),
# what must ALWAYS be true, and what is merely reported.
#
# `defect_origin` is deliberate. A check whose origin nobody remembers gets
# deleted by a future cleanup as "probably redundant". Naming the day it was
# false is what keeps it alive.
CHECKS = (
    {
        "id": "geography-is-honored",
        "tool": "execute_plan",
        "args": {"intent": "rank markets for a 200 MW AI campus in Texas within 24 months"},
        "why": (
            "On 2026-08-10 this exact intent returned Ashburn, Virginia. The "
            "planner extracted the capacity and dropped the state, because no "
            "ranking tool could carry a US state — so every state-scoped "
            "ranking was answered nationally by construction."
        ),
        "defect_origin": "2026-08-10 — planner answered Texas with Virginia",
        "invariant": (
            {"path": "planner_version", "op": "gte", "value": "5.11",
             "means": "the geography-scoping planner is live"},
            {"path": "constraint_iso", "op": "is_array",
             "means": "the geography proof is a list, not a bare string"},
            {"path": "constraint_iso", "op": "contains", "value": "ERCOT",
             "means": "Texas resolved to a Texas market"},
            {"path": "executed[0].args.region", "op": "equals", "value": "TX",
             "means": "the state reached the tool as a real argument"},
            {"path": "executed[*].result.shortlist[*].state", "op": "all_equal", "value": "TX",
             "means": "THE ONE THAT MATTERS — every returned row is inside the "
                      "geography that was asked for. This was false on 2026-08-10."},
        ),
        "informational": (
            {"path": "executed[0].result.shortlist[0].market",
             "note": "today's top market — changes as DCPI rescores; never a failure"},
            {"path": "executed[0].result.shortlist[0].composite_score",
             "note": "today's score — informational only"},
        ),
    },
    {
        "id": "empty-is-explained",
        "tool": "execute_plan",
        "args": {"intent": "rank markets for a 200 MW AI campus in Ohio"},
        "why": (
            "Ohio has tracked markets but none carrying a BUILD or CAUTION "
            "verdict, so the shortlist is legitimately empty. Returning a bare "
            "[] made a correct answer indistinguishable from 'no data for Ohio' "
            "and from 'the tool broke' — and the caller's next step then failed "
            "on a missing market_slug, so a truthful result read as a broken run."
        ),
        "defect_origin": "2026-08-11 — reported by an external agent running the live planner",
        "invariant": (
            {"path": "executed[0].args.region", "op": "equals", "value": "OH",
             "means": "the state reached the tool"},
            {"path": "executed[0].result", "op": "empty_shortlist_implies_explanation",
             "means": "an empty shortlist MUST carry empty_result naming which of "
                      "the two causes applies — scored-out vs not-covered"},
        ),
        "informational": (
            {"path": "executed[0].result.empty_result.markets_in_region",
             "note": "how many Ohio markets exist below the verdict bar; moves with rescoring"},
        ),
    },
    {
        "id": "citation-shape-is-tolerated",
        "tool": "site_selection_canvas",
        "args": {"capacity_mw": 200, "region": "TX"},
        "why": (
            "This tool returned MCP -32602 to every strict client, because the "
            "origin emitted citation as a string where the declared schema said "
            "object. Not gated — uncallable."
        ),
        "defect_origin": "2026-08-10 — -32602 on every strict MCP client",
        "invariant": (
            {"path": "$response", "op": "not_protocol_error",
             "means": "the call completes; a -32602 here is the original outage"},
            {"path": "citation", "op": "is_string_or_object",
             "means": "BOTH shapes are valid. On anonymous calls this is still a "
                      "STRING — do not assume citation.cite_as exists."},
        ),
        "informational": (),
    },
    {
        "id": "limits-are-published",
        "tool": "GET /api/v1/canon/coverage",
        "args": {},
        "why": (
            "Routing on a tool COUNT is not possible — an agent cannot convert "
            "a bare tool count into a decision. Coverage is the routing contract: per "
            "problem, the entry call, and what we refuse to answer."
        ),
        "defect_origin": "2026-08-10 — agents were routing on tool count",
        "invariant": (
            {"path": "coverage", "op": "non_empty_array",
             "means": "the routing map is served"},
            {"path": "coverage[*].entry_tool", "op": "all_present",
             "means": "every problem names the ONE call to make"},
            {"path": "coverage[*]", "op": "status_partial_or_expanding_has_limits",
             "means": "a partial/expanding claim without a published limit is an "
                      "empty label"},
        ),
        "informational": (),
    },
)

# ── Known gaps ─────────────────────────────────────────────────────────────
# Published so a self-testing agent does not re-report what we already know.
# Every entry names the observable symptom, not the internal cause — the caller
# can only see the symptom, and that is what they would file.
KNOWN_GAPS = (
    {
        "id": "citation-string-on-anonymous",
        "symptom": "`citation` is a string, not an object, on unkeyed calls.",
        "status": "known",
        "why": "The normalizer runs on keyed paths only; the declared schema "
               "accepts anyOf:[object, string], so this is valid, not broken.",
        "do": "Handle both shapes. Do not assume citation.cite_as exists.",
    },
    {
        "id": "capacity-mw-not-a-filter",
        "symptom": "capacity_mw is echoed but the shortlist is not sized to it — "
                   "5 MW and 2000 MW return identical rows.",
        "status": "known — declared, will not be implemented",
        "why": "Market rows carry excess_power_score, a 0-100 index, not "
               "megawatts. Filtering against it would mean inventing a "
               "score-to-MW mapping.",
        "do": "Read constraint_coverage.capacity_mw. Judge headroom from "
              "excess_power_score and time_to_power_months, or call "
              "get_grid_intelligence for the finalist ISO.",
    },
    {
        "id": "constraint-iso-truncated-at-free-tier",
        "symptom": "constraint_iso may list fewer ISOs than actually constrained.",
        "status": "known — tier behaviour, not a defect",
        "why": "Texas resolves to four ISOs; the anonymous trim truncates the "
               "array and adds _constraint_iso_total_in_pro.",
        "do": "Read _constraint_iso_total_in_pro before treating the array as "
              "exhaustive.",
    },
    {
        "id": "empty-shortlist-fails-downstream-steps",
        "symptom": "A legitimately empty shortlist leaves later steps "
                   "skipped_unresolved with a constraint_check FAIL.",
        "status": "known — the FAIL is honest, the wording is not helpful",
        "why": "The step genuinely could not resolve a market_slug because no "
               "market cleared the verdict bar.",
        "do": "Read empty_result on the step-1 payload before reading the "
              "constraint_check as a bug.",
    },
)

canon_selftest_bp = Blueprint("canon_selftest", __name__)


def contract_hash() -> str:
    """Stable hash over checks + gaps. A consumer detects drift by comparing
    hashes and re-derives only on change (anchor_intents discipline).
    """
    canon = json.dumps([SELFTEST_VERSION, _checks(), _gaps()],
                       separators=(",", ":"), ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()[:16]


def _checks() -> list:
    return [
        {
            "id": c["id"],
            "tool": c["tool"],
            "args": dict(c["args"]),
            "why": c["why"],
            "defect_origin": c["defect_origin"],
            # Counts are DERIVED. A hand-written total is the kind of fact that
            # rots while everything around it stays internally consistent.
            "invariant_count": len(c["invariant"]),
            "invariant": [dict(a) for a in c["invariant"]],
            "informational": [dict(a) for a in c["informational"]],
        }
        for c in CHECKS
    ]


def _gaps() -> list:
    return [dict(g) for g in KNOWN_GAPS]


def selftest_payload() -> dict:
    return {
        "version": SELFTEST_VERSION,
        "contract_hash": contract_hash(),
        "source": "dchub-backend routes/canon_selftest.py",
        "note": (
            "Run these against the live server and report anything that fails an "
            "INVARIANT. Invariants encode classes of correctness and are expected "
            "to hold forever; informational fields are today's data and are never "
            "a pass/fail signal. Check known_gaps before filing — those are "
            "already known and a duplicate report costs everyone."
        ),
        "how_to_run": {
            "transport": "POST https://dchub.cloud/mcp (tools/call), or the named "
                         "GET endpoint where the check gives one",
            "auth": "none — every check below runs keyless at free-tier depth",
            "note": "Free tier truncates arrays and adds *_total_in_pro keys. That "
                    "is expected and is not a failure.",
        },
        "report_back": {
            "what": ["the intent or args verbatim", "the tool name",
                     "the invariant id that failed", "the raw response"],
            "not_wanted": "an assessment, a strategy summary, or a reformatted "
                          "version of this document — the raw facts are the value",
            "where": "https://dchub.cloud/feedback",
        },
        "checks": _checks(),
        "known_gaps": _gaps(),
    }


@canon_selftest_bp.route("/api/v1/canon/selftest", methods=["GET"])
def canon_selftest():
    """Public, keyless, cached 1h. Pure module data — no DB, no fallback tier."""
    body = dict(selftest_payload())
    body["ok"] = True
    resp = jsonify(body)
    resp.headers["Cache-Control"] = "public, max-age=3600"
    return resp
