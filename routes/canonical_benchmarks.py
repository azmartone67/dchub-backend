"""Canonical benchmark suite — GET /api/v1/reports/canonical-benchmarks.

Public, machine-readable, no auth. Built 2026-08-04 in response to a direct
request from OpenAI's assistant during the partner round: publish a living
table of the canonical questions with last successful execution, workflow
time, coverage and maturity, so that "the reasoning layer is functioning as
intended" is demonstrated continuously rather than asserted.

★ THE DESIGN RULE THIS FILE EXISTS TO ENFORCE ★
Every cell is measured or it says it is not. On 2026-08-04 we removed an /ai
activity feed that rendered "Just now" for any platform with a nonzero
lifetime request count — HuggingFace, with a single real request ever,
rendered as live traffic. A benchmark table that invents a plausible "2.4s"
is that same defect in better clothes. So:

  - Rows come from _REPLAY_INTENTS (the capture CONTRACT — every canonical
    question we claim to answer), left-joined onto _BAKED (what a real keyed
    run actually captured). A contracted intent with no capture renders
    status="never_run" with nulls. It fails in public. That is the point.
  - workflow_ms is a SINGLE CAPTURED RUN, and the field is named for that.
    It is NOT a median. The one honest median we hold — execute_plan p50
    over 30d, with DC Hub's own platforms and scripted UAs excluded — is
    published separately, at the top level, with its sample count attached
    so nobody quotes a p50 built on n=3.
  - coverage/maturity are computed from deferred steps against a stated
    definition (see _DEFINITIONS), never hand-assigned.
  - retrieved_at is the refresh's stamp, and scripts/refresh_meta_replays.py
    only advances it on a FULLY successful capture of every intent. A
    refresh that re-stamps on failure is how dates lie.

Reads no request-scoped state, so it is safe to cache at the edge; it changes
only when the weekly refresh re-bakes.
"""
from __future__ import annotations

import datetime as _dt

from flask import Blueprint, jsonify

from mcp_calls_deloop import external_platform_predicate, real_ua_predicate
from routes.brain_ascension_master_shell import _conn
from routes.meta_replays import _BAKED, _REPLAY_INTENTS, _RETRIEVED_AT, _TIER

canonical_benchmarks_bp = Blueprint("canonical_benchmarks", __name__)

# Stated once, published in the payload, and used by the code below — so a
# reader can recompute every verdict from the raw fields without trusting us.
_DEFINITIONS = {
    "status": {
        "measured": "a keyed capture of this exact intent succeeded; all "
                    "other fields on the row come from that run",
        "never_run": "the intent is in the capture contract but no capture "
                     "exists — every measured field is null, deliberately",
    },
    "coverage": {
        "complete": "every step the planner recorded resolved and executed",
        "partial": "at least one recorded step was deferred unresolved; "
                   "deferred_steps names them",
    },
    "maturity": {
        "mature": "status=measured AND coverage=complete",
        "expanding": "status=measured AND coverage=partial",
        "unproven": "status=never_run",
    },
    "workflow_ms": "wall-clock of ONE captured keyed run of the whole plan. "
                   "Not a median, not an average, not a service-level claim.",
    "execute_plan_p50_ms_30d": "median duration_ms across execute_plan calls "
                               "in 30d, EXCLUDING DC Hub's own platforms and "
                               "scripted/internal user-agents (the canonical "
                               "external predicates, imported not copied). "
                               "Null with a stated reason when the sample is "
                               "too small or the count is unknown.",
}

# Below this, a median is a coincidence. 30d of real calls or we publish null
# and say why — the flattering-zero lesson: an unknown must never render as a
# confident number.
_MIN_P50_SAMPLES = 20


def _baked_by_intent() -> dict:
    return {b.get("intent"): b for b in _BAKED if b.get("intent")}


def _row_for(contract: dict, baked: dict | None) -> dict:
    """One canonical question. Nulls where nothing was measured."""
    question = contract.get("intent")
    expect = contract.get("expect_class")

    if not baked:
        return {
            "question": question,
            "intent_class_expected": expect,
            "intent_class_actual": None,
            "status": "never_run",
            "last_successful_execution": None,
            "workflow_ms": None,
            "steps_planned": None,
            "steps_executed": None,
            "deferred_steps": [],
            "coverage": None,
            "maturity": "unproven",
            "planner_version": None,
            "lead_tool": None,
        }

    executed = baked.get("executed") or []
    # ★ 2026-08-05: this read `(s.get("status") or "executed")` — an ABSENT
    # status silently defaulted to the FLATTERING literal. If the MCP server
    # ever stopped emitting `status` on a step, every step would count as
    # executed, flipping coverage partial→complete and maturity
    # expanding→mature, silently, in the direction that makes us look better.
    # That is precisely the defect this file's own docstring swears off. A
    # missing status is UNKNOWN, and unknown must never resolve to success —
    # it counts as not-executed and is named in deferred_steps so a reader
    # sees it. Fails closed.
    ran = [s for s in executed if s.get("status") == "executed"]
    deferred = [s.get("tool") for s in executed if s.get("status") != "executed"]
    coverage = "partial" if deferred else "complete"

    return {
        "question": question,
        "intent_class_expected": expect,
        "intent_class_actual": baked.get("intent_class"),
        # The refresh fails loudly on a class drift, so these agree by
        # construction — published anyway so a reader can check rather than
        # take our word for it.
        "routing_matches_contract": baked.get("intent_class") == expect,
        "status": "measured",
        "last_successful_execution": _RETRIEVED_AT,
        "workflow_ms": baked.get("ms"),
        "steps_planned": baked.get("steps"),
        "steps_executed": len(ran),
        "deferred_steps": deferred,
        "coverage": coverage,
        "maturity": "mature" if coverage == "complete" else "expanding",
        "planner_version": baked.get("planner_version"),
        "lead_tool": baked.get("lead_tool"),
    }


def _execute_plan_p50() -> dict:
    """Median execute_plan latency over 30d, DC Hub's own traffic excluded.

    Null-with-a-reason on any failure or thin sample. mcp_call_log carries
    `tool` and `duration_ms` (the identity VIEW carries neither — it has
    tool_name and no duration), which is why this reads the log table on
    purpose. See the 0804 tool/tool_name incident before "fixing" it.

    ★ Reading the raw log does NOT mean accepting a raw population. The
    externality verdict is applied here explicitly, imported from the
    canonical de-loop module — see the comment on the query.
    """
    out = {"p50_ms": None, "samples": None, "reason": None}
    # _conn() returns a RAW connection or None — it is not a context manager,
    # and `with _conn() as c` would raise on the None branch and would not
    # close the socket on the happy path. Same shape every shell lane uses.
    c = _conn()
    if c is None:
        # samples stays None, NOT 0. An unreachable database means the count is
        # UNKNOWN; rendering it as 0 states "we measured, and found none".
        out["reason"] = "db unavailable — sample count unknown, not zero"
        return out
    try:
        with c.cursor() as cur:
            # ★ 2026-08-05: this query originally had NO externality filter at
            # all, and an audit proved it causally — three execute_plan calls
            # made under a `dchub-` UA raised `samples` 152→155 and moved the
            # PUBLISHED p50 from 2,808 to 2,996 ms. About three quarters of the
            # population was our own traffic, including the weekly refresh that
            # BUILDS this very table (UA "dchub-replay-refresh/1.0"). So the
            # headline "median workflow time" was measuring DC Hub probing
            # itself. That is the self-traffic-counted-as-adoption defect, in
            # the one number a partner is most likely to quote as a service
            # level.
            #
            # The original docstring said the identity VIEW lacks `tool` and
            # `duration_ms` so the raw log had to be read. True — but the
            # conclusion ("therefore the wide population is unavoidable") did
            # not follow. These two predicates take a column name precisely so
            # another table can reuse the CANONICAL verdict instead of keeping
            # a second hand-maintained exclusion list, which is how regex twins
            # drift apart. Imported, never copied.
            #
            # external_platform_predicate contains a literal % (LIKE). That is
            # safe HERE only because this execute() passes no bound params —
            # psycopg2 interprets % only when parameters are supplied. Do not
            # add a parameter to this call without doubling the literals.
            cur.execute(
                "SELECT percentile_disc(0.5) WITHIN GROUP "
                "         (ORDER BY duration_ms)::int, COUNT(*)"
                "  FROM mcp_call_log"
                " WHERE tool = 'execute_plan'"
                "   AND timestamp >= now() - interval '30 days'"
                "   AND duration_ms IS NOT NULL AND duration_ms > 0"
                "   AND " + external_platform_predicate("platform") +
                "   AND " + real_ua_predicate("user_agent")
            )
            row = cur.fetchone()
    except Exception as exc:  # pragma: no cover - defensive
        out["reason"] = f"query failed: {type(exc).__name__}"
        return out
    finally:
        try:
            c.close()
        except Exception:
            pass

    if not row:
        out["reason"] = "no rows returned"
        return out

    return _p50_verdict(row[0], int(row[1] or 0))


def _p50_verdict(p50, n: int) -> dict:
    """Pure decision: publish the median, or withhold it and say why.

    Split out from the query so the withholding rule is testable without a
    database — a gate whose behaviour is only exercised in production is a
    gate nobody has checked.
    """
    out = {"p50_ms": None, "samples": n, "reason": None}
    if n < _MIN_P50_SAMPLES:
        out["reason"] = (
            f"only {n} timed execute_plan calls in 30d "
            f"(minimum {_MIN_P50_SAMPLES}) — a median here would be a "
            "coincidence, so it is withheld rather than guessed"
        )
        return out
    if p50 is None:
        out["reason"] = "duration_ms present but percentile resolved null"
        return out
    out["p50_ms"] = int(p50)
    return out


@canonical_benchmarks_bp.route(
    "/api/v1/reports/canonical-benchmarks", methods=["GET"])
def canonical_benchmarks():
    baked = _baked_by_intent()
    rows = [_row_for(c, baked.get(c.get("intent"))) for c in _REPLAY_INTENTS]

    measured = [r for r in rows if r["status"] == "measured"]
    never = [r for r in rows if r["status"] == "never_run"]
    partial = [r for r in measured if r["coverage"] == "partial"]

    return jsonify({
        "report": "canonical-benchmarks",
        "version": "1.0",
        "generated_at": _dt.datetime.now(_dt.timezone.utc)
                           .isoformat(timespec="seconds").replace("+00:00", "Z"),
        "what_this_is": (
            "The canonical questions DC Hub claims to answer, each with the "
            "result of a real keyed run. Rows that have never been captured "
            "say so and carry nulls; nothing on this page is estimated."
        ),
        "capture": {
            "retrieved_at": _RETRIEVED_AT,
            "tier": _TIER,
            "cadence": "weekly (.github/workflows/refresh-meta-replays.yml)",
            "stamp_rule": (
                "retrieved_at advances only on a fully successful capture of "
                "every contracted intent; a failed refresh leaves the "
                "previous stamp in place rather than re-stamping"
            ),
            "routing_contract": (
                "each intent must still route to its recorded class; a "
                "mis-route fails the refresh instead of silently re-baking"
            ),
        },
        "summary": {
            "questions_contracted": len(rows),
            "measured": len(measured),
            "never_run": len(never),
            "coverage_complete": len(measured) - len(partial),
            "coverage_partial": len(partial),
        },
        "execute_plan_p50_ms_30d": _execute_plan_p50(),
        "definitions": _DEFINITIONS,
        "rows": rows,
    })
