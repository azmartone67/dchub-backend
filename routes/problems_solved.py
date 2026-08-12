"""Problems Solved report — GET /api/v1/reports/problems-solved (+ HTML).

Public, keyless, machine-readable. Answers the question an enterprise buyer
actually asks, which is not "how many tools do you have":

    "Which of MY problems does one DC Hub workflow actually close — and
     how do you know?"

★ THE DESIGN RULE THIS FILE EXISTS TO ENFORCE ★
This is CUSTOMER-FACING, so the bar is higher than an internal board. A buyer
will ask "94% of what, measured how, over how many runs, since when?" and every
cell has to answer that with no human in the loop. The honesty model is not
invented here — it is the one routes/canonical_benchmarks.py already proved, and
it is REUSED rather than re-derived, because a second, weaker honesty model is
how two surfaces come to disagree about the same fact:

  - Rows walk the CONTRACT (routes/problem_taxonomy.IN_SCOPE — the canonical
    published problem taxonomy), never the successes. A problem class with zero
    runs renders NULLS and STAYS A ROW. Dropping it would let this table
    silently flatter itself by showing only what worked.
  - A thin sample WITHHOLDS with a stated reason instead of publishing a
    coincidence (_rate_verdict / _MIN_RUNS, the _p50_verdict discipline). A 94%
    over 7 runs is a coincidence wearing a percentage sign, and it is the single
    most likely way this report becomes a liability in a sales conversation.
  - The published population is BUILT FROM the executed filters (_run_filters is
    both joined into the WHERE and published verbatim), so the description
    cannot drift from the query.
  - A step with no status FAILS CLOSED. It does not count as executed.
  - A single capture is never called a median, and a median is never published
    over a bimodal distribution — the histogram ships so a reader can check.

★ WHY recipe_executions AND NOT mcp_call_log/execute_plan_steps
Both sources carry per-step status. They are NOT interchangeable for THIS
report, and the difference is requirement-level:

    recipe_executions            platform ✓  user_agent ✓  steps_planned ✓
                                 constraint_rejects ✗
    mcp_call_log/…_steps         platform ✓  user_agent ✗  steps_planned ✗
                                 constraint_rejects ✓

The MCP server's execute_plan_steps tracking payload (server.mjs ~9588) carries
no `user_agent` key at all, so `user_agent` is NULL on every such row and
real_ua_predicate — which keeps NULL, correctly, for real anonymous agents —
degrades to a NO-OP there. Publishing a self-traffic exclusion that cannot
actually fire is worse than publishing none, because it buys credibility it has
not earned. _recipeLifecyclePayload (server.mjs ~844) DOES send platform,
user_agent and ip_address, so on recipe_executions BOTH canonical predicates
bite. Self-traffic exclusion is requirement-critical for a customer-facing
number, so the source that can actually do it wins.

The cost of that choice is `constraint_rejects`, and it is DISCLOSED rather than
glossed: see _CONSTRAINT_COVERAGE below. Of the planner's two constraint_check
rows, C2 (every consumed artifact was produced by an earlier step) IS fully
evaluated here — it fails exactly when a step carries status skipped_unresolved,
which status_counts records. C1 (every minted ISO satisfies the intent
geography) is NOT evaluable on this source, so it is named as unevaluated and
its magnitude is measured separately and labelled as a different population. An
unknown that is bounded and labelled is a finding; an unknown rendered as a pass
is the flattering-zero defect.
"""
from __future__ import annotations

import datetime as _dt
import statistics
from html import escape as _esc

from flask import Blueprint, Response, jsonify

from mcp_calls_deloop import external_platform_predicate, real_ua_predicate
from routes.brain_ascension_master_shell import _conn
from routes.problem_taxonomy import (COVERAGE, IN_SCOPE, TAXONOMY_VERSION,
                                     contract_hash)

problems_solved_bp = Blueprint("problems_solved", __name__)

_WINDOW_DAYS = 30

# Below this, a completion rate is a coincidence. Same floor and same reasoning
# as canonical_benchmarks._MIN_P50_SAMPLES — deliberately NOT imported, because
# that constant governs a latency percentile and this one governs a rate; they
# are free to move independently, and silently coupling them would mean a future
# latency tuning change quietly re-published a withheld sales number.
_MIN_RUNS = 20

# A step that ran and produced something. A gated preview IS a working step —
# that is the paywall answering, not the graph breaking. This is the canonical
# verdict already used by routes/planner_quality.py and the adoption shell; it
# is reused rather than re-decided. Because a gated step is nonetheless a step
# where the BUYER saw a preview instead of the data, every row also publishes
# runs_with_gated_steps, so "completed" can never quietly lean on the paywall.
_GOOD_STATUSES = ("executed", "gated_preview")

# ── the row CONTRACT ────────────────────────────────────────────────────────
# problem (canonical, imported) → the planner intent classes that serve it.
#
# ★ The problem LIST is IMPORTED from routes/problem_taxonomy.py and is NOT
# editable here — this module owns only the measurement mapping. _contract_drift
# FAILS the report closed if a published problem has no mapping, so a new
# customer problem cannot go silently unmeasured (the adoption shell's
# qr_contract guard, applied to the taxonomy rather than to anchor recipes).
#
# ★ Class ids are the planner's own, from dchub-mcp-server server.mjs
# _PLAN_CLASSES / _CLASS_WHY_LIVE (16 ids). They are NOT invented here. Every
# one of the 16 is mapped, each to exactly one problem — the classes must
# PARTITION, never overlap, or a single run would be counted under two problems
# and both rows would be inflated. _contract_drift asserts the disjointness, and
# any class OBSERVED in the data but absent from this map is published in
# unattributed_classes rather than dropped, so drift surfaces in public instead
# of shrinking a denominator quietly.
_PROBLEM_CLASSES: dict[str, tuple[str, ...]] = {
    "megawatts and power density": ("facility_search",),
    "grid headroom and power availability": ("grid_headroom", "power_timeline"),
    "interconnection queues": ("interconnection_queue",),
    "substations and transmission": ("hosting_capacity",),
    "site selection and buildable capacity": ("site_analysis", "capacity_search"),
    "colocation and wholesale data-center markets": ("market_ranking",
                                                     "market_comparison"),
    "AI/GPU compute campuses": (),
    "fiber routes, diversity and latency": ("fiber", "fiber_power_pairing"),
    "PPAs and energy pricing": ("price",),
    "tax incentives and permitting": ("incentives_tax",),
    "water and climate risk": ("water_climate",),
    "data-center M&A and deals": ("deals_ma", "changes_delta"),
    "power generation, gas and energy infrastructure": (),
}

# Two published problems have NO dedicated planner class. That is a real,
# checkable fact about the router, not a gap in this report, and stating it is
# more useful to a buyer than inventing a class id that would match nothing and
# render an eternal, unexplained zero. Both reasons are verifiable in
# server.mjs.
_NO_ROUTE_REASON: dict[str, str] = {
    "AI/GPU compute campuses": (
        "execute_plan has no dedicated intent class for this problem. An "
        "AI-campus intent routes into market_ranking as a lead-tool branch "
        "(it leads with ai_capacity_index), so its runs are recorded under "
        "market_ranking and are NOT separably attributable here. The workflow "
        "runs; this report cannot isolate its runs, which is a measurement "
        "limit and is published as one rather than shown as a zero"),
    "power generation, gas and energy infrastructure": (
        "execute_plan has no dedicated intent class for this problem — the "
        "entry tool (get_power_pipeline) is called directly rather than "
        "through a planned workflow, so no execute_plan replay carries it. "
        "Zero runs here means zero PLANNED WORKFLOWS, not zero usage"),
}

# ── what "completed" means, stated in the payload and used by the code ───────
_DEFINITIONS = {
    "completed": (
        "one execute_plan workflow in which EVERY PLANNED STEP EXECUTED and "
        "the evaluable constraint_check passed. Operationally: the run reached "
        "a terminal outcome of 'completed'; steps_planned > 0; and the number "
        "of steps carrying a status in "
        + "/".join(_GOOD_STATUSES) +
        " equals steps_planned exactly. It is defined from POSITIVE replay "
        "evidence, never from an absence of errors — a run with no per-step "
        "status recorded is NOT completed"),
    "partial": (
        "the workflow answered some of the question: it ran, but at least one "
        "planned step did not execute (unresolved hand-off, failed step, or "
        "budget exhausted) or an evaluable constraint_check failed. "
        "partial_reasons names what was missing, by frequency. Partial is NOT "
        "a closed problem and is never added to completed"),
    "failed": (
        "the workflow produced nothing usable: no step executed, or the run "
        "reached outcome='failed', or it recorded no per-step status at all "
        "and therefore cannot be verified (unverifiable fails CLOSED)"),
    "in_flight_excluded": (
        "a run with no terminal outcome that is younger than the canonical "
        "abandonment threshold is still running. It is excluded from n "
        "entirely — counting a running workflow as a failure is a manufactured "
        "red, and counting it as a success is worse"),
    "median_workflow_steps": (
        "median number of steps that ACTUALLY EXECUTED (status in "
        + "/".join(_GOOD_STATUSES) + "), across completed and partial runs. "
        "Not steps planned. A failed run's step count measures the failure, "
        "not the question, so it is excluded. Withheld with a reason when n is "
        "below the minimum or the distribution is bimodal — the full histogram "
        "is published either way so the midpoint can be checked"),
    "withheld": (
        "n is below the published minimum, so every rate on the row is null "
        "with a reason. n itself is published because a count is measured; a "
        "percentage over that count would not be"),
    "never_run": (
        "the problem is in the published taxonomy and a planner class serves "
        "it, but zero qualifying external workflows ran in the window. Every "
        "measured field is null, deliberately. Zero runs is an absence of "
        "demand or of routing — it is NOT a 0% completion rate"),
    "no_planner_route": (
        "the problem is published as in scope, but execute_plan has no "
        "dedicated intent class for it, so no workflow replay can be "
        "attributed to it at all. no_route_reason states why. This is a "
        "measurement limit, not a capability claim"),
}

# ★ The constraint_check disclosure. The planner emits two constraint_check
# rows per run (server.mjs ~9554). One of them is fully evaluated here and one
# is not, and a customer-facing "completed" must say which.
_CONSTRAINT_COVERAGE = {
    "evaluated": [{
        "id": "C2",
        "decision": "every consumed artifact was produced by an earlier step",
        "how": ("fails exactly when a step carries status "
                "'skipped_unresolved', which status_counts records — so this "
                "check is evaluated on 100% of the published runs and a "
                "failure makes the run partial, never completed"),
    }],
    "not_evaluated": [{
        "id": "C1",
        "decision": ("every minted ISO must satisfy the intent geography "
                     "(an answer must not drift outside the region asked "
                     "about)"),
        "why": ("the count lives in mcp_call_log.params.constraint_rejects; "
                "recipe_executions has no such column. This report reads "
                "recipe_executions because it is the ONLY source carrying "
                "user_agent, without which DC Hub's own traffic cannot be "
                "excluded — see the module docstring. A geography drift can "
                "therefore make a run look completed here"),
        "blind_spot_bound": ("measured separately over the call-log "
                             "population and published as "
                             "c1_geography_blind_spot — a bounded unknown, "
                             "not a silent pass"),
    }],
}


# ── population: declared BY BEING the query that runs ───────────────────────
_SOURCE_TABLE = "recipe_executions"


def _run_filters() -> list[str]:
    """Every WHERE clause applied to the published population, in order.

    Returned as a list so it is BOTH joined into the query and published
    verbatim — a hand-written description of a filter is a second source of
    truth, and second sources drift. If someone edits a predicate, the
    published population changes in the same edit.

    external_platform_predicate contains a literal % (LIKE): safe ONLY because
    the execute() below passes no bound params — psycopg2 interprets % only
    when parameters are supplied. Do not add a parameter to that call without
    doubling the literals.
    """
    return [
        "source = 'execute_plan'",
        f"started_at >= now() - interval '{_WINDOW_DAYS} days'",
        external_platform_predicate("platform"),
        real_ua_predicate("user_agent"),
    ]


def _population() -> dict:
    """What is counted, in prose and in the exact SQL that counts it."""
    return {
        "observations": "execute_plan workflow runs (one row per workflow)",
        "window": f"{_WINDOW_DAYS} days, rolling, ending now",
        "window_kind": ("ROLLING, not a fixed calendar window — two readings "
                        "cover different periods and must never be diffed as "
                        "a trend"),
        "source": _SOURCE_TABLE,
        "includes": "external callers only, keyed and keyless alike",
        "excludes": (
            "DC Hub's own platforms (dchub-*, probes, QA harnesses, registry "
            "crawlers) and scripted/internal user-agents — the canonical "
            "externality predicates from mcp_calls_deloop, IMPORTED not "
            "copied, so this report cannot drift from every other de-looped "
            "surface. Includes DC Hub's own weekly refresh and benchmark "
            "captures, which are self-traffic like any other"),
        "why_it_matters": (
            "the internal derivation this report builds on applies NO "
            "externality filter at all. On the canonical benchmark suite the "
            "same omission had left ~3/4 of an execute_plan population as DC "
            "Hub's own traffic. runs_before_exclusions vs runs_after_"
            "exclusions below publishes the size of that effect here rather "
            "than asserting it"),
        "sql_filters": _run_filters(),
    }


# ── pure verdicts (no DB — so every rule is testable) ───────────────────────

def _int(v) -> int:
    try:
        return int(v or 0)
    except Exception:
        return 0


def _classify_run(steps_planned, status_counts, outcome, age_min: float,
                  abandon_min: int) -> tuple[str, list[str], int]:
    """One workflow → (verdict, deficits, steps_actually_executed).

    ★ FAILS CLOSED, three ways, all of them in the direction that does NOT
    flatter us:

      1. `good` is summed over an ALLOW-LIST of statuses and must EQUAL
         steps_planned. Deficits are anything else. A status this code has
         never heard of — including the literal "undefined" that the gateway
         writes when a step carries no status at all — lands outside the
         allow-list, so it counts as NOT executed and is NAMED. A deny-list
         would fail OPEN the day the gateway adds a status.
      2. An absent/empty status_counts is unverifiable, not complete.
      3. outcome must be exactly 'completed'. The internal derivation this
         builds on accepts `outcome in ("completed", None)`, so a NULL outcome
         on an old row can resolve to complete there. A NULL outcome past the
         abandonment threshold is an ABANDONED run, and abandoned is not
         closed.
    """
    planned = _int(steps_planned)
    counts_raw = status_counts if isinstance(status_counts, dict) else {}
    counts = {str(k): _int(v) for k, v in counts_raw.items()}

    if outcome is None and age_min < abandon_min:
        return "in_flight", [], 0

    good = sum(n for st, n in counts.items() if st in _GOOD_STATUSES)
    total = sum(counts.values())

    deficits: list[str] = []
    for st, n in sorted(counts.items()):
        if st not in _GOOD_STATUSES and n > 0:
            deficits.append(st)

    if total <= 0:
        return "failed", ["no_step_status_recorded"], 0
    if outcome == "failed" or good <= 0:
        return "failed", deficits or ["no_step_executed"], good
    if planned <= 0:
        # Steps ran but the plan size was never recorded, so "every PLANNED
        # step executed" is unverifiable. Unverifiable is never completed.
        return "partial", deficits + ["steps_planned_not_recorded"], good
    if good == planned and not deficits and outcome == "completed":
        return "completed", [], good
    if good < planned:
        deficits = deficits + ["planned_steps_unaccounted"]
    if outcome != "completed":
        deficits = deficits + [f"outcome_{outcome or 'abandoned'}"]
    return "partial", deficits, good


# What a deficit MEANS, in a buyer's words. "39% partial" is useless; "39%
# partial, most commonly an unresolved hand-off" is a credibility asset.
# Unknown keys fall through to a literal, honest rendering rather than being
# dropped — a deficit this table cannot name must still be visible.
_DEFICIT_PHRASES = {
    "skipped_unresolved": ("a step needed an input that no earlier step "
                           "produced (unresolved hand-off) — the planner's C2 "
                           "constraint_check failing"),
    "failed": "a step ran and errored",
    "not_run": ("the workflow hit its ~40s step budget before this step ran; "
                "the response returns the exact tool and args to continue"),
    "no_step_status_recorded": ("no per-step status was recorded, so the run "
                                "cannot be verified — it fails closed rather "
                                "than counting as completed"),
    "no_step_executed": "the workflow recorded steps but none executed",
    "steps_planned_not_recorded": ("the plan size was not recorded, so "
                                   "'every planned step executed' could not "
                                   "be verified"),
    "planned_steps_unaccounted": ("fewer steps carry a status than the plan "
                                  "declared — the difference is unaccounted "
                                  "for and counts as not executed"),
}


def _phrase_deficit(key: str) -> str:
    if key in _DEFICIT_PHRASES:
        return _DEFICIT_PHRASES[key]
    if key.startswith("outcome_"):
        return (f"the run reached outcome '{key[8:]}' rather than completed "
                "(an abandoned workflow is not a closed problem)")
    return (f"step status '{key}' is not on the executed allow-list, so it "
            "counts as not executed (fails closed)")


def _rate_verdict(part: int, n: int) -> tuple[float | None, str | None]:
    """A percentage, or null and why. The _p50_verdict discipline for rates."""
    if n < _MIN_RUNS:
        return None, (
            f"only {n} qualifying external workflow(s) in {_WINDOW_DAYS}d "
            f"(minimum {_MIN_RUNS}) — a percentage over this sample would be "
            "a coincidence, so it is withheld rather than published")
    return round(100.0 * part / n, 1), None


def _median_verdict(step_counts: list[int]) -> dict:
    """Median steps EXECUTED, or null with a reason.

    Withheld on a thin sample AND on a bimodal distribution: a midpoint between
    two clusters describes no run that actually happened. The histogram is
    published in every branch so a reader can compute their own.
    """
    n = len(step_counts)
    hist = {str(v): step_counts.count(v) for v in sorted(set(step_counts))}
    out = {"median_steps": None, "basis_runs": n, "distribution": hist,
           "shape": None, "reason": None}
    if n == 0:
        out["reason"] = ("no completed or partial runs — a median over failed "
                         "runs would measure the failure, not the question")
        return out
    if n < _MIN_RUNS:
        out["shape"] = "unknown"
        out["reason"] = (
            f"only {n} non-failed workflow(s) (minimum {_MIN_RUNS}) — the "
            "distribution is published instead of a midpoint that would imply "
            "a typical run we cannot evidence")
        return out

    ranked = sorted(hist.items(), key=lambda kv: (-kv[1], int(kv[0])))
    if len(ranked) >= 2:
        (v1, c1), (v2, c2) = ranked[0], ranked[1]
        if c1 >= 0.25 * n and c2 >= 0.25 * n and abs(int(v1) - int(v2)) >= 2:
            out["shape"] = "bimodal"
            out["reason"] = (
                f"bimodal — {c1} run(s) at {v1} steps and {c2} at {v2} steps. "
                "A median between two clusters describes no run that actually "
                "happened, so the distribution is published instead")
            return out
    out["shape"] = "unimodal"
    out["median_steps"] = statistics.median(step_counts)
    return out


def _contract_drift() -> list[str]:
    """Every published problem must have a mapping, and classes must partition.

    Returns the list of violations; a non-empty list makes the report say so
    in public rather than quietly measuring 11 problems out of 13.
    """
    problems: list[str] = []
    unmapped = [p for p in IN_SCOPE if p not in _PROBLEM_CLASSES]
    if unmapped:
        problems.append(
            "published problem(s) with no measurement mapping, which would go "
            "silently unmeasured: " + ", ".join(unmapped))
    stale = [p for p in _PROBLEM_CLASSES if p not in IN_SCOPE]
    if stale:
        problems.append(
            "mapping(s) for problems no longer in the published taxonomy: "
            + ", ".join(stale))
    seen: dict[str, str] = {}
    for prob, classes in _PROBLEM_CLASSES.items():
        for cls in classes:
            if cls in seen:
                problems.append(
                    f"intent class {cls!r} is mapped to BOTH {seen[cls]!r} and "
                    f"{prob!r} — a single run would be counted under two "
                    "problems and both rows inflated")
            seen[cls] = prob
    for prob, classes in _PROBLEM_CLASSES.items():
        if not classes and prob not in _NO_ROUTE_REASON:
            problems.append(
                f"{prob!r} maps to no intent class and states no reason — an "
                "unexplained permanent zero")
    covered = {c["problem"] for c in COVERAGE}
    missing_cov = [p for p in IN_SCOPE if p not in covered]
    if missing_cov:
        problems.append("no taxonomy COVERAGE entry for: "
                        + ", ".join(missing_cov))
    # The buyer-facing rendering must cover every published problem, and must
    # not carry entries for anything else — otherwise it is a second, rival
    # problem list rather than a rendering of the canonical one.
    missing_q = [p for p in IN_SCOPE if p not in _CUSTOMER_QUESTIONS]
    if missing_q:
        problems.append("no customer-facing phrasing for: "
                        + ", ".join(missing_q))
    extra_q = [p for p in _CUSTOMER_QUESTIONS if p not in IN_SCOPE]
    if extra_q:
        problems.append(
            "customer phrasing for problem(s) not in the published taxonomy, "
            "which would make this a rival list: " + ", ".join(extra_q))
    return problems


def _coverage_by_problem() -> dict:
    return {c["problem"]: c for c in COVERAGE}


def _row_for(problem: str, cov: dict | None, agg: dict | None) -> dict:
    """One customer problem. Nulls wherever nothing was measured."""
    classes = _PROBLEM_CLASSES.get(problem, ())
    cov = cov or {}
    published_limits = list(cov.get("limits") or ())

    row = {
        "problem": problem,
        "customer_question": _CUSTOMER_QUESTIONS.get(problem),
        "intent_classes": list(classes),
        "entry_tool": cov.get("entry_tool"),
        "taxonomy_status": cov.get("status"),
        "published_limits": published_limits,
        "status": None,
        "runs": None,
        "completed_pct": None,
        "partial_pct": None,
        "failed_pct": None,
        "median_workflow": None,
        "partial_reasons": [],
        "runs_with_gated_steps": None,
        "in_flight_excluded": None,
        "reason": None,
        "no_route_reason": None,
    }

    if not classes:
        row["status"] = "no_planner_route"
        row["no_route_reason"] = _NO_ROUTE_REASON.get(problem)
        row["reason"] = _DEFINITIONS["no_planner_route"]
        return row

    n = _int((agg or {}).get("n"))
    row["in_flight_excluded"] = _int((agg or {}).get("in_flight"))
    if not agg or n == 0:
        row["status"] = "never_run"
        row["runs"] = 0
        row["reason"] = _DEFINITIONS["never_run"]
        # The median block still ships so the row has the same shape as a
        # measured one — a consumer must not have to branch on presence.
        row["median_workflow"] = _median_verdict([])
        return row

    row["runs"] = n
    row["runs_with_gated_steps"] = _int(agg.get("gated_runs"))
    row["median_workflow"] = _median_verdict(list(agg.get("exec_steps") or []))

    comp, part, fail = _int(agg.get("completed")), _int(agg.get("partial")), \
        _int(agg.get("failed"))
    c_pct, c_reason = _rate_verdict(comp, n)
    row["completed_pct"] = c_pct
    row["partial_pct"] = _rate_verdict(part, n)[0]
    row["failed_pct"] = _rate_verdict(fail, n)[0]

    # partial_reasons names what was missing, ranked. Published even when the
    # rates are withheld: the COUNTS are measured, and they are the most
    # actionable thing on a thin row.
    deficits = agg.get("deficits") or {}
    row["partial_reasons"] = [
        {"reason": _phrase_deficit(k), "code": k, "runs_affected": v}
        for k, v in sorted(deficits.items(), key=lambda kv: (-kv[1], kv[0]))
    ]
    # A structural limit is WHY a class of question stays partial, and it comes
    # from canon rather than from this file's opinion.
    if published_limits:
        row["partial_context"] = (
            "published limits on this problem, from the canonical taxonomy — "
            "these are why some runs cannot close completely")

    if c_reason:
        row["status"] = "withheld"
        row["reason"] = c_reason
        row["counts_only"] = {"completed": comp, "partial": part,
                              "failed": fail}
    else:
        row["status"] = "measured"
    return row


# The taxonomy phrases a problem the way DC HUB names it. A buyer phrases it as
# a question. These are renderings of the SAME canonical entries — the mapping
# is keyed on IN_SCOPE membership and _contract_drift fails if one is missing,
# so this can never become a second, rival problem list.
_CUSTOMER_QUESTIONS = {
    "megawatts and power density":
        "How much power does this facility actually have?",
    "grid headroom and power availability":
        "Can this location get the megawatts I need, and when?",
    "interconnection queues":
        "What is ahead of me in the interconnection queue here?",
    "substations and transmission":
        "What transmission and substation capacity is near this site?",
    "site selection and buildable capacity":
        "Where can I actually build this campus?",
    "colocation and wholesale data-center markets":
        "Which market should I choose?",
    "AI/GPU compute campuses":
        "Where can I land an AI/GPU campus of this size?",
    "fiber routes, diversity and latency":
        "Does this site have the fiber routes and latency I need?",
    "PPAs and energy pricing":
        "What will power cost me here?",
    "tax incentives and permitting":
        "What incentives and permitting apply to this site?",
    "water and climate risk":
        "What is the water and climate risk at this site?",
    "data-center M&A and deals":
        "Who is buying and building in this market?",
    "power generation, gas and energy infrastructure":
        "What generation and gas infrastructure backs this region?",
}


# ── measurement ─────────────────────────────────────────────────────────────

def _measure() -> dict:
    """Read the window, apply the canonical exclusions, bucket by problem.

    Fail-soft and three-valued throughout: an unreachable database yields
    UNKNOWN (nulls plus a reason), never a flattering zero.
    """
    out: dict = {
        "ok": False, "reason": None,
        "runs_before_exclusions": None, "runs_after_exclusions": None,
        "self_traffic_excluded": None,
        "by_class": {}, "unattributed_classes": [],
    }
    try:
        from recipe_lifecycle import ABANDONED_AFTER_MINUTES as abandon
    except Exception:
        abandon = 15
    out["abandonment_threshold_minutes"] = abandon

    c = _conn()
    if c is None:
        out["reason"] = ("database unavailable — every count is UNKNOWN, not "
                         "zero. A zero here would state that we measured and "
                         "found nothing")
        return out
    try:
        with c.cursor() as cur:
            cur.execute(
                "SELECT to_regclass('public." + _SOURCE_TABLE + "') IS NOT NULL")
            if not (cur.fetchone() or [False])[0]:
                out["reason"] = (
                    f"{_SOURCE_TABLE} does not exist — the workflow replay "
                    "substrate is absent, so nothing here is measurable")
                return out

            # Unfiltered count first, so the size of our own traffic is
            # PUBLISHED rather than asserted. No bound params in either
            # execute() — required, because the platform predicate carries a
            # literal % (LIKE).
            cur.execute(
                "SELECT COUNT(*) FROM " + _SOURCE_TABLE +
                " WHERE source = 'execute_plan'"
                f"   AND started_at >= now() - interval '{_WINDOW_DAYS} days'")
            out["runs_before_exclusions"] = _int((cur.fetchone() or [0])[0])

            cur.execute(
                "SELECT intent_class, steps_planned, status_counts, outcome,"
                "       EXTRACT(EPOCH FROM (now() - started_at)) / 60.0"
                "  FROM " + _SOURCE_TABLE +
                " WHERE " + " AND ".join(_run_filters()))
            rows = cur.fetchall()
    except Exception as exc:  # pragma: no cover - defensive
        out["reason"] = (f"query failed: {type(exc).__name__} — counts are "
                         "UNKNOWN, not zero")
        return out
    finally:
        try:
            c.close()
        except Exception:
            pass

    out["runs_after_exclusions"] = len(rows)
    before = out["runs_before_exclusions"]
    if before:
        out["self_traffic_excluded"] = {
            "runs": before - len(rows),
            "pct_of_raw": round(100.0 * (before - len(rows)) / before, 1),
            "note": ("DC Hub's own probes, QA harnesses, registry crawlers and "
                     "scripted callers, removed by the canonical predicates"),
        }

    by_class: dict[str, dict] = {}
    for cls, planned, counts, outcome, age in rows:
        key = cls or "unknown"
        b = by_class.setdefault(key, {
            "n": 0, "completed": 0, "partial": 0, "failed": 0,
            "in_flight": 0, "exec_steps": [], "deficits": {},
            "gated_runs": 0})
        verdict, deficits, good = _classify_run(
            planned, counts, outcome, float(age or 0), abandon)
        if verdict == "in_flight":
            b["in_flight"] += 1
            continue
        b["n"] += 1
        b[verdict] += 1
        if verdict != "failed":
            b["exec_steps"].append(good)
        for d in deficits:
            b["deficits"][d] = b["deficits"].get(d, 0) + 1
        if isinstance(counts, dict) and _int(counts.get("gated_preview")) > 0:
            b["gated_runs"] += 1

    out["by_class"] = by_class
    mapped = {cls for classes in _PROBLEM_CLASSES.values() for cls in classes}
    out["unattributed_classes"] = sorted(
        ({"intent_class": k, "runs": v["n"] + v["in_flight"]}
         for k, v in by_class.items() if k not in mapped),
        key=lambda d: -d["runs"])
    out["ok"] = True
    return out


def _merge(classes: tuple[str, ...], by_class: dict) -> dict | None:
    """Sum the classes serving one problem. Disjointness is guaranteed by
    _contract_drift, so no run is counted twice."""
    present = [by_class[c] for c in classes if c in by_class]
    if not present:
        return None
    agg = {"n": 0, "completed": 0, "partial": 0, "failed": 0, "in_flight": 0,
           "exec_steps": [], "deficits": {}, "gated_runs": 0}
    for b in present:
        for k in ("n", "completed", "partial", "failed", "in_flight",
                  "gated_runs"):
            agg[k] += b[k]
        agg["exec_steps"].extend(b["exec_steps"])
        for d, v in b["deficits"].items():
            agg["deficits"][d] = agg["deficits"].get(d, 0) + v
    return agg


def _c1_blind_spot() -> dict:
    """Bound the ONE constraint_check this population cannot evaluate.

    Read from mcp_call_log, which carries constraint_rejects but no usable
    user_agent — so this is a DIFFERENT, WIDER population than the report's,
    and it is labelled as such in the payload. Its job is to bound an unknown,
    not to be quoted as a rate.
    """
    out = {"runs_with_geography_rejects": None, "runs_examined": None,
           "population": ("mcp_call_log execute_plan_steps, "
                          f"{_WINDOW_DAYS}d, platform exclusion only — this "
                          "row's source carries NO usable user_agent, so it is "
                          "NOT the report population and its numbers must not "
                          "be quoted as the report's"),
           "reason": None}
    c = _conn()
    if c is None:
        out["reason"] = "db unavailable — the bound is unknown, not zero"
        return out
    try:
        with c.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*),"
                "       COUNT(*) FILTER (WHERE"
                "         COALESCE((params->>'constraint_rejects')::int, 0) > 0)"
                "  FROM mcp_call_log"
                " WHERE tool = 'execute_plan_steps'"
                f"   AND timestamp >= now() - interval '{_WINDOW_DAYS} days'"
                "   AND " + external_platform_predicate("platform"))
            row = cur.fetchone()
    except Exception as exc:  # pragma: no cover - defensive
        out["reason"] = f"query failed: {type(exc).__name__} — bound unknown"
        return out
    finally:
        try:
            c.close()
        except Exception:
            pass
    if row:
        out["runs_examined"] = _int(row[0])
        out["runs_with_geography_rejects"] = _int(row[1])
    return out


def _build() -> dict:
    drift = _contract_drift()
    m = _measure()
    cov = _coverage_by_problem()
    by_class = m.get("by_class") or {}

    rows = []
    for problem in IN_SCOPE:
        classes = _PROBLEM_CLASSES.get(problem, ())
        agg = _merge(classes, by_class) if (m.get("ok") and classes) else None
        row = _row_for(problem, cov.get(problem), agg)
        if not m.get("ok") and classes:
            # Unmeasured is NOT never-run. Overwrite the optimistic default.
            row["status"] = "unmeasured"
            row["runs"] = None
            row["reason"] = m.get("reason") or "measurement unavailable"
        rows.append(row)

    measured = [r for r in rows if r["status"] == "measured"]
    withheld = [r for r in rows if r["status"] == "withheld"]
    never = [r for r in rows if r["status"] == "never_run"]
    noroute = [r for r in rows if r["status"] == "no_planner_route"]

    return {
        "report": "problems-solved",
        "version": "1.0",
        "generated_at": _dt.datetime.now(_dt.timezone.utc)
            .isoformat(timespec="seconds").replace("+00:00", "Z"),
        "what_this_is": (
            "Which customer problems ONE DC Hub workflow actually closes. "
            "Rows are the canonical published problem taxonomy — every "
            "problem stays a row, including the ones with no runs, so this "
            "table cannot flatter itself by showing only what worked. "
            "Nothing here is estimated: a cell is measured, withheld with a "
            "reason, or null."),
        "taxonomy_source": {
            "module": "dchub-backend routes/problem_taxonomy.py",
            "list": "IN_SCOPE (imported, not transcribed)",
            "version": TAXONOMY_VERSION,
            "contract_hash": contract_hash(),
            "problems_published": len(IN_SCOPE),
        },
        "contract_ok": not drift,
        "contract_violations": drift,
        "minimum_runs_to_publish_a_rate": _MIN_RUNS,
        "population": _population(),
        "runs_before_exclusions": m.get("runs_before_exclusions"),
        "runs_after_exclusions": m.get("runs_after_exclusions"),
        "self_traffic_excluded": m.get("self_traffic_excluded"),
        "abandonment_threshold_minutes": m.get("abandonment_threshold_minutes"),
        "measurement_ok": m.get("ok"),
        "measurement_reason": m.get("reason"),
        "constraint_check_coverage": _CONSTRAINT_COVERAGE,
        "c1_geography_blind_spot": _c1_blind_spot(),
        "unattributed_classes": m.get("unattributed_classes"),
        "unattributed_note": (
            "planner intent classes observed in the window that this report "
            "maps to no published problem. Published rather than dropped: a "
            "silently discarded class is a denominator quietly shrinking"),
        "summary": {
            # ★ Counted from the TAXONOMY, not from `rows`. Deriving both sides
            # from `rows` makes "did every problem render?" a tautology — a
            # dropped row would shrink the count and the total in lockstep, so
            # the table could silently show only what worked and still look
            # self-consistent. This is the number a reader checks the row count
            # against, so it must come from the contract.
            "problems_published": len(IN_SCOPE),
            "rows_rendered": len(rows),
            "every_problem_rendered": len(rows) == len(IN_SCOPE),
            "measured": len(measured),
            "withheld_thin_sample": len(withheld),
            "never_run": len(never),
            "no_planner_route": len(noroute),
        },
        "definitions": _DEFINITIONS,
        "rows": rows,
    }


@problems_solved_bp.route("/api/v1/reports/problems-solved", methods=["GET"])
def problems_solved():
    resp = jsonify(_build())
    # Rolling window + live counts: a cached copy would publish a stale n
    # against a fresh generated_at, which is the dates-lie defect.
    resp.headers["Cache-Control"] = "public, max-age=300"
    return resp


# ── human-readable rendering ────────────────────────────────────────────────

_STATUS_STYLE = {
    "measured": ("#065f46", "#d1fae5"),
    "withheld": ("#92400e", "#fef3c7"),
    "never_run": ("#3730a3", "#e0e7ff"),
    "no_planner_route": ("#374151", "#e5e7eb"),
    "unmeasured": ("#991b1b", "#fee2e2"),
}


def _cell(v, suffix: str = "") -> str:
    if v is None:
        return '<span class="null">—</span>'
    return _esc(f"{v}{suffix}")


def _render_html(d: dict) -> str:
    rows = []
    for r in d["rows"]:
        fg, bg = _STATUS_STYLE.get(r["status"] or "", ("#374151", "#e5e7eb"))
        mw = r.get("median_workflow") or {}
        med = mw.get("median_steps")
        med_s = (_esc(f"{med:g} tools") if isinstance(med, (int, float))
                 else '<span class="null">—</span>')
        reasons = "".join(
            f'<li><b>{_esc(str(p["runs_affected"]))} run(s)</b> — '
            f'{_esc(p["reason"])}</li>' for p in (r.get("partial_reasons") or []))
        limits = "".join(f"<li>{_esc(l)}</li>"
                         for l in (r.get("published_limits") or []))
        detail = ""
        if r.get("reason"):
            detail += f'<div class="why">{_esc(r["reason"])}</div>'
        if r.get("no_route_reason"):
            detail += f'<div class="why">{_esc(r["no_route_reason"])}</div>'
        if mw.get("reason"):
            detail += ('<div class="why"><b>median workflow withheld:</b> '
                       f'{_esc(mw["reason"])}</div>')
        if reasons:
            detail += ('<div class="why"><b>what was missing:</b><ul>'
                       + reasons + "</ul></div>")
        if limits:
            detail += ('<div class="why"><b>published limits on this '
                       "problem (canonical taxonomy):</b><ul>" + limits
                       + "</ul></div>")
        rows.append(
            "<tr>"
            f'<td><div class="q">{_esc(r.get("customer_question") or "")}</div>'
            f'<div class="p">{_esc(r["problem"])}</div>{detail}</td>'
            f'<td><span class="badge" style="color:{fg};background:{bg}">'
            f'{_esc(r["status"] or "?")}</span></td>'
            f'<td class="num">{_cell(r.get("runs"))}</td>'
            f'<td class="num">{_cell(r.get("completed_pct"), "%")}</td>'
            f'<td class="num">{_cell(r.get("partial_pct"), "%")}</td>'
            f'<td class="num">{med_s}</td>'
            "</tr>")

    st = d["self_traffic_excluded"] or {}
    excl = ("no qualifying runs to compare" if not st else
            f'{st["runs"]} of {d.get("runs_before_exclusions")} raw workflow(s) '
            f'({st["pct_of_raw"]}%) were DC Hub\'s own traffic and were removed')
    return f"""<!doctype html><meta charset="utf-8">
<title>Problems Solved — DC Hub</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
 body{{font:15px/1.6 -apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
      margin:0;padding:32px 20px;color:#0f172a;background:#f8fafc}}
 .wrap{{max-width:1080px;margin:0 auto}}
 h1{{margin:0 0 6px;font-size:26px}}
 .sub{{color:#64748b;margin:0 0 22px}}
 table{{width:100%;border-collapse:collapse;background:#fff;
        border:1px solid #e2e8f0;border-radius:10px;overflow:hidden}}
 th,td{{padding:12px 14px;border-bottom:1px solid #eef2f7;
        text-align:left;vertical-align:top}}
 th{{background:#f1f5f9;font-size:12px;text-transform:uppercase;
     letter-spacing:.04em;color:#475569}}
 td.num,th.num{{text-align:right;white-space:nowrap}}
 .q{{font-weight:600}}
 .p{{color:#64748b;font-size:13px}}
 .why{{color:#475569;font-size:13px;margin-top:8px;
       border-left:3px solid #e2e8f0;padding-left:10px}}
 .why ul{{margin:4px 0 0;padding-left:18px}}
 .badge{{display:inline-block;padding:2px 9px;border-radius:99px;
         font-size:12px;font-weight:600}}
 .null{{color:#94a3b8}}
 .box{{background:#fff;border:1px solid #e2e8f0;border-radius:10px;
       padding:16px 18px;margin:0 0 20px;font-size:14px;color:#334155}}
 .box b{{color:#0f172a}}
 code{{background:#f1f5f9;padding:1px 5px;border-radius:4px;font-size:13px}}
</style>
<div class="wrap">
<h1>Problems Solved</h1>
<p class="sub">Which customer problems one DC&nbsp;Hub workflow actually
closes &mdash; measured from execute_plan replays, not asserted.</p>

<div class="box">
<p style="margin:0 0 10px"><b>Completed</b> means: one workflow in which
<b>every planned step executed</b> and the evaluable constraint_check passed.
It is defined from positive replay evidence, never from an absence of errors.
A step with no status counts as <b>not</b> executed.</p>
<p style="margin:0 0 10px"><b>Window:</b> rolling {_esc(str(_WINDOW_DAYS))}
days ending now. <b>n</b> is qualifying external workflows.
<b>Self-traffic:</b> {_esc(excl)}.</p>
<p style="margin:0"><b>Below n={_esc(str(_MIN_RUNS))} every rate is
withheld</b> with a reason rather than published &mdash; a percentage over a
handful of runs is a coincidence. Problems with no runs, and problems the
planner has no route for, stay on this table as rows so it cannot flatter
itself by showing only what worked. Machine-readable:
<code>/api/v1/reports/problems-solved</code>.</p>
</div>

<table>
<tr><th>Customer problem</th><th>Basis</th><th class="num">n (runs)</th>
<th class="num">Completed</th><th class="num">Partial</th>
<th class="num">Median workflow</th></tr>
{''.join(rows)}
</table>
<p class="sub" style="margin-top:18px">Taxonomy:
{_esc(d['taxonomy_source']['module'])} &middot; contract
{_esc(d['taxonomy_source']['contract_hash'])} &middot; generated
{_esc(d['generated_at'])}</p>
</div>"""


@problems_solved_bp.route("/reports/problems-solved", methods=["GET"])
def problems_solved_html():
    resp = Response(_render_html(_build()), mimetype="text/html")
    resp.headers["Cache-Control"] = "public, max-age=300"
    return resp
