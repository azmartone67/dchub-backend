"""
routes/adoption_master_shell.py — ADOPTION MASTER SHELL (#52, 2026-08-12).

The four questions the 08-12 funnel round left open, on one board. Every lane
is a WORK ORDER, not a status light: lanes 1 and 3 are RED BY DESIGN because
the work behind them is real and unstarted, and neither can go green from a
copy change.

★ Lane 4 was ALSO expected born-red and the first live tick measured it PASS
(4 of 4 measurable canonical problems close a majority of their runs in one
workflow). The expectation is published as `red_by_design`; the verdicts stay
where the data puts them. A board that keeps asserting a colour its own data
contradicts is a guard that cannot fail, pointing the other way. The real gap
lane 4 surfaced is different and would have been invisible to a forced red:
fiber+power pairing — a PUBLISHED anchor intent — had ZERO workflows in the
window, which renders UNMEASURED, never "0% closed".

  1. IDENTITY DURABILITY — OAuth keys return across ISO weeks ~40x more often
     than free keys. The lane is red while durable identity is a MINORITY of
     the agents that actually come back. It goes green on the composition of
     returners, not on the rate of any single cohort and not on a nudge.
  2. ACTIVATION — the mint→first-call cliff. "Ever called" is answerable today
     (last_used_at); the LATENCY that gives the cliff its shape is not, because
     nothing stamps the first call. Rendered None with the reason, never a
     guess — a modelled cliff would be a fabricated number.
  3. CONVERSION — machine_paid, human_paid and abandoned-at-the-gate, reported
     SEPARATELY and gated SEPARATELY. One combined "conversions" number would
     hide exactly the thing this board exists to show: the human path is
     structurally zero (delivery, not appeal — 4,114 relay claims per 30d into
     sessions whose median inter-mint gap is seconds is a machine looping), and
     the machine path is a different rail with a different failure mode.
     ★ THE TOKEN RACE WAS A REAL, SECOND PROBLEM — CLOSED 2026-08-16. The
     2026-07-30 artifact split did NOT fix it: mcp-server #193 measured ~96%
     of minted claims machine-redeemed by the server's OWN _autoRedeemClaim
     in median <1s (the paywall was operating as a free-key dispenser).
     Auto-redeem is opt-in-off since 08-16 and post-fix mints show zero
     machine redemptions. The funnel's human_acted definition is NOT restated
     here — this file said "DEFINITION v3" while the check below said "v2" and
     the API published v4, three answers in one board. The version and its
     description are rendered from routes/handoff_definition, which is the
     same block the API publishes; the remaining open question is DELIVERY —
     do agents SHOW the for_your_human link — watched via relay_opens +
     human_view_first_opened_at, not asserted.
  4. QUESTIONS RETIRED — per canonical problem, how many tools a workflow
     burns and whether ONE workflow CLOSED the question (complete / partial /
     failed). This is the only lane that measures customer value rather than
     usage volume: a question answered in one call is worth more than ten calls
     that answer none, and call-count reporting cannot tell those apart.
  5. LOOKUP vs WORKFLOW (2026-08-12) — are agents solving PROBLEMS or doing
     single LOOKUPS? Both sides on ONE rolling window from ONE table under the
     canonical predicates IMPORTED from mcp_calls_deloop. The verdict is
     PER-PLATFORM on purpose: one integration doing workflows while six do
     lookups is a ROUTING problem (fix tool descriptions / discovery); all of
     them doing lookups would be a DEMAND problem, and completely different
     work. ★ The self-inflation trap is MEASURED, not assumed — execute_plan
     runs each step as a real tools/call to 127.0.0.1, so a six-step workflow
     writes six extra rows; counted as lookups they would make the front door
     look LESS used the more it was used. lw_subcall_guard proves the
     exclusion every tick and cannot pass on an empty population.
  6. CITATION SURVIVAL (2026-08-12) — UNMEASURED BY ARCHITECTURE, and PASS is
     unreachable in that lane by construction. We observe what we SEND; what
     the agent renders to its human happens inside the client and no
     server-side instrument reaches it. The lane publishes the send-side
     PRECONDITION (which is measurable, and currently failing), the exact
     boundary, and the instrumentation that would settle it — it does NOT
     publish a citation number. ai_citations / citation_probes are our own
     probes of public LLM answers about traffic that never touched MCP; a
     guard FAILS the lane if one is ever wired in as the answer.

★ THREE-VALUED EVERYWHERE. PASS / FAIL / "?" (UNMEASURED). Could-not-measure is
never "fine" and never "broken", and a lane whose checks were all indeterminate
renders "?" — never a confident PASS. UNREADABLE IS NOT A FINDING.

★ EVERY NUMBER CARRIES ITS BASIS AND WINDOW, and every window says whether it
is ROLLING or FIXED. A rolling-vs-fixed mix-up manufactured a false -55%
collapse this week; the board states the kind inline so the next reader cannot
repeat it.

★ NOTHING IS RESTATED FROM MEMORY. The canonical problem list is IMPORTED from
routes/anchor_intents.py (the single publication point), the abandonment
threshold from recipe_lifecycle, canonical counts from canonical_stats. A
transcribed constant here would rot on its own schedule.

Shared primitives are IMPORTED from routes/brain_ascension_master_shell
(_admin_ok, _check, _conn, _lane_verdict, _safe_lane) — never copied. Note the
verified signatures: _check(...) returns a dict keyed "pass"; _lane_verdict
returns "FAIL" | "?" | "PASS" (NOT "RED"/"GREEN" — a previous shell shipped a
comparison against invented RED/GREEN literals that could never match);
_safe_lane returns the CHECKS LIST, so the lane dict is built here.

READ-ONLY / DIAGNOSTIC: no writes, no sends, no actuation. Every lane names the
work order it is holding open.

Endpoints:
  GET/POST /api/v1/admin/adoption/master-tick   JSON scoreboard (4 lanes)
  GET      /api/v1/admin/adoption               JSON board (CF-bypass alias)
  GET      /admin/adoption                      HTML dashboard (60s refresh)

Auth: X-Admin-Key header or ?admin_key= vs DCHUB_ADMIN_KEY (falls back to
DCHUB_INTERNAL_KEY) — the imported _admin_ok, same gate as every master shell.
Kill: ADOPTION_SHELL_DISABLE=1 → 404 (never 5xx: the CF worker reads ANY 5xx
from Railway as a dead origin and fails the whole site over to stale Render).
"""
from __future__ import annotations

import datetime
import json
import logging
import os
import statistics
from html import escape as _esc

from flask import Blueprint, Response, jsonify, request

# ★ IMPORTED, never copied. Verified signatures:
#   _check(cid, name, passed, detail, critical=False) -> dict keyed "pass"
#   _lane_verdict(checks) -> "FAIL" | "?" | "PASS"
#   _safe_lane(fn, *a) -> the CHECKS LIST (the lane envelope is built here)
from routes.brain_ascension_master_shell import (  # noqa: F401
    _admin_ok, _check, _conn, _lane_verdict, _safe_lane,
)

# ★ IMPORTED, never restated. The human_acted stage has been redefined four
# times; every time, one file was updated and the surfaces that had typed the
# version into prose were not. This board was carrying two stale answers at
# once (docstring "v3", cv_gate "v2") against a published v4.
from routes.handoff_definition import (
    human_acted_count_sql as _handoff_acted_sql,
    human_acted_sentence as _handoff_sentence,
    human_acted_session_predicate as _handoff_acted_predicate,
)

logger = logging.getLogger(__name__)

adoption_master_shell_bp = Blueprint("adoption_master_shell", __name__)

SHELL_ID = "adoption-52"

# ── windows ───────────────────────────────────────────────────────────
# Stated, not implied. ROLLING means "ending at now()", so two ticks an hour
# apart cover different periods; a delta between them is NOT a week-over-week
# change. FIXED-week comparisons (cross-ISO-week return) say so separately.
WINDOW_DAYS = 30
MATURITY_DAYS = 7   # a key minted <7d ago has not had a full later ISO week

_WINDOW_NOTE = (
    f"ROLLING {WINDOW_DAYS}d ending now() — NOT a fixed calendar window. "
    "Two ticks cover different periods; never diff them as a trend. "
    "Cross-week return is computed over a MATURE cohort (minted "
    f"{MATURITY_DAYS}–{WINDOW_DAYS}d ago) so every key has had at least one "
    "full subsequent ISO week in which to return — including the last "
    f"{MATURITY_DAYS} days would right-censor the rate into a fake decline."
)

# The canonical problem set. Keys are the recipe ids published in
# routes/anchor_intents.py ANCHORS; values are the planner intent_class ids
# (dchub-mcp-server plan_query vocabulary) whose executions answer that
# problem. The MAP is the only thing declared here — the PROBLEM LIST is
# imported, and _lane_questions_retired FAILS if an anchor recipe appears
# with no mapping (contract-drift guard: a new published problem cannot go
# silently unmeasured).
_PROBLEM_CLASSES = {
    "market_selection":    ("market_ranking",),
    "grid_and_queue":      ("grid_headroom", "interconnection_queue",
                            "power_timeline"),
    "compare_markets":     ("market_comparison",),
    "site_analysis":       ("site_analysis", "capacity_search",
                            "hosting_capacity"),
    "fiber_power_pairing": ("fiber_power_pairing",),
}

# Human-readable names for the board (the operator's words, mapped to the
# canonical recipe ids so the mapping itself is auditable).
_PROBLEM_LABELS = {
    "market_selection":    "market selection",
    "grid_and_queue":      "grid headroom",
    "compare_markets":     "A-vs-B comparison",
    "site_analysis":       "site analysis",
    "fiber_power_pairing": "fiber + power pairing",
}

# A step that ran and produced something. A gated preview IS a working step —
# it is the paywall answering, not the graph breaking.
_GOOD_STATUSES = ("executed", "gated_preview")


# ── kill switch ───────────────────────────────────────────────────────

def _disabled() -> bool:
    return (os.environ.get("ADOPTION_SHELL_DISABLE") or "").strip() == "1"


# ── tiny read helpers (literal SQL only) ──────────────────────────────
# ★ No params tuple is ever passed by these helpers, so a literal % in a LIKE
# pattern is safe (psycopg2 only performs %-substitution when args are given —
# the 2026-07-17 /api/v1/map outage class). Any query needing parameters must
# not use these.

def _row(c, sql: str):
    """Fail-soft single row. None on error — never raises, never 500s a tick."""
    if c is None:
        return None
    try:
        with c.cursor() as cur:
            cur.execute(sql)
            return cur.fetchone()
    except Exception as e:  # noqa: BLE001
        logger.debug("[adoption] row failed: %s -- %s", sql[:90], e)
        try:
            c.rollback()
        except Exception:
            pass
        return None


def _rows(c, sql: str, cap: int = 50000):
    """Fail-soft multi-row. None on error, [] on empty — the two are DIFFERENT
    and the callers must not conflate them (a flattering zero is a bug)."""
    if c is None:
        return None
    try:
        with c.cursor() as cur:
            cur.execute(sql)
            return cur.fetchmany(cap)
    except Exception as e:  # noqa: BLE001
        logger.debug("[adoption] rows failed: %s -- %s", sql[:90], e)
        try:
            c.rollback()
        except Exception:
            pass
        return None


def _table_exists(c, name: str) -> bool | None:
    r = _row(c, f"SELECT to_regclass('public.{name}') IS NOT NULL")
    return None if r is None else bool(r[0])


def _column_exists(c, table: str, column: str) -> bool | None:
    r = _row(c, "SELECT COUNT(*) FROM information_schema.columns "
                f"WHERE table_name = '{table}' AND column_name = '{column}'")
    return None if r is None else bool(int(r[0] or 0) > 0)


def _pct(n, d):
    try:
        return round(100.0 * float(n) / float(d), 1) if d else None
    except Exception:
        return None


def _i(v) -> int:
    try:
        return int(v or 0)
    except Exception:
        return 0


# ══ LANE 1 · identity durability ══════════════════════════════════════

def _cohort(c, sql: str) -> dict | None:
    r = _row(c, sql)
    if r is None:
        return None
    return {"mature": _i(r[0]), "returned": _i(r[1])}


def _lane_identity_durability(c) -> list[dict]:
    """OAuth vs free cross-week return.

    BORN RED, and the green condition is deliberately NOT "the free rate went
    up". It is COMPOSITION: durable identities (OAuth, email-bound) must be the
    MAJORITY of the agents that actually come back. A copy change that lifts
    free-key reuse a point cannot satisfy it; shipping durable identity can.

    A return = a key whose last use falls in a LATER ISO week than its mint.
    That is a true cross-SESSION return: same-week reuse is one conversation.
    """
    checks: list[dict] = []
    if c is None:
        return [_check("id_db", "identity tables readable", None,
                       "no database connection — UNMEASURED, not zero",
                       critical=True)]

    # dch_oauth_ identities live in mcp_dev_keys (NOT auto_trial_keys).
    oauth = _cohort(c, f"""
        SELECT COUNT(*) FILTER (
                 WHERE created_at < now() - interval '{MATURITY_DAYS} days'),
               COUNT(*) FILTER (
                 WHERE created_at < now() - interval '{MATURITY_DAYS} days'
                   AND last_used_at IS NOT NULL
                   AND date_trunc('week', last_used_at)
                       > date_trunc('week', created_at))
          FROM mcp_dev_keys
         WHERE api_key LIKE 'dch_oauth_%'
           AND created_at >= now() - interval '{WINDOW_DAYS} days'
    """)
    free = _cohort(c, f"""
        SELECT COUNT(*) FILTER (
                 WHERE created_at < now() - interval '{MATURITY_DAYS} days'),
               COUNT(*) FILTER (
                 WHERE created_at < now() - interval '{MATURITY_DAYS} days'
                   AND last_used_at IS NOT NULL
                   AND date_trunc('week', last_used_at)
                       > date_trunc('week', created_at))
          FROM mcp_dev_keys
         WHERE api_key LIKE 'dch_live_%'
           AND created_at >= now() - interval '{WINDOW_DAYS} days'
    """)
    # Trial keys, split by whether an operator email makes the identity durable.
    trial_bound = _cohort(c, f"""
        SELECT COUNT(*) FILTER (
                 WHERE minted_at < now() - interval '{MATURITY_DAYS} days'),
               COUNT(*) FILTER (
                 WHERE minted_at < now() - interval '{MATURITY_DAYS} days'
                   AND last_used_at IS NOT NULL
                   AND date_trunc('week', last_used_at)
                       > date_trunc('week', minted_at))
          FROM auto_trial_keys
         WHERE minted_at >= now() - interval '{WINDOW_DAYS} days'
           AND operator_email IS NOT NULL AND operator_email <> ''
    """)
    trial_only = _cohort(c, f"""
        SELECT COUNT(*) FILTER (
                 WHERE minted_at < now() - interval '{MATURITY_DAYS} days'),
               COUNT(*) FILTER (
                 WHERE minted_at < now() - interval '{MATURITY_DAYS} days'
                   AND last_used_at IS NOT NULL
                   AND date_trunc('week', last_used_at)
                       > date_trunc('week', minted_at))
          FROM auto_trial_keys
         WHERE minted_at >= now() - interval '{WINDOW_DAYS} days'
           AND (operator_email IS NULL OR operator_email = '')
    """)

    unreadable = [n for n, v in (("mcp_dev_keys/oauth", oauth),
                                 ("mcp_dev_keys/free", free),
                                 ("auto_trial_keys/email_bound", trial_bound),
                                 ("auto_trial_keys/key_only", trial_only))
                  if v is None]
    if unreadable:
        checks.append(_check(
            "id_read", "every identity cohort readable", None,
            "UNREADABLE (not zero): " + ", ".join(unreadable)
            + " — the composition gate below is withheld rather than computed "
              "on a partial population",
            critical=True))
        return checks

    durable_ret = oauth["returned"] + trial_bound["returned"]
    fragile_ret = free["returned"] + trial_only["returned"]
    returners = durable_ret + fragile_ret

    # ★ THE GATE. Composition, not rate. Born red at ~40% durable.
    if returners == 0:
        checks.append(_check(
            "id_durable_majority",
            "durable identity is the MAJORITY of returning agents", None,
            "no cross-week returners in the mature cohort at all — "
            "UNMEASURED, and a zero denominator is never a pass. "
            f"basis: mature cohort minted {MATURITY_DAYS}–{WINDOW_DAYS}d ago",
            critical=True))
    else:
        share = _pct(durable_ret, returners)
        checks.append(_check(
            "id_durable_majority",
            "durable identity is the MAJORITY of returning agents",
            durable_ret * 2 > returners,
            f"{durable_ret} of {returners} cross-week returners carry a "
            f"durable identity ({share}%) — OAuth {oauth['returned']} + "
            f"email-bound {trial_bound['returned']} vs key-only "
            f"{fragile_ret}. GREEN CONDITION: >50%. This cannot be moved by "
            "copy: it moves when connecting agents actually land on a durable "
            "identity. WORK ORDER: OAuth/email-bind as the default connect "
            f"path. basis: mcp_dev_keys + auto_trial_keys, mature cohort "
            f"minted {MATURITY_DAYS}–{WINDOW_DAYS}d ago, return = last use in "
            "a LATER ISO week than mint (fixed ISO weeks, not a rolling 7d)",
            critical=True))

    # Diagnostic: the ~40x gap itself, reported with both denominators.
    o_rate, f_rate = _pct(oauth["returned"], oauth["mature"]), \
        _pct(free["returned"], free["mature"])
    if o_rate is None or f_rate is None or not f_rate:
        checks.append(_check(
            "id_gap", "OAuth-vs-free return gap", None,
            f"gap UNMEASURED — oauth {oauth['returned']}/{oauth['mature']}, "
            f"free {free['returned']}/{free['mature']}; a zero denominator "
            "yields no ratio (reported as unmeasured, never as parity)"))
    else:
        checks.append(_check(
            "id_gap", "OAuth-vs-free return gap", True,
            f"OAuth {oauth['returned']}/{oauth['mature']} = {o_rate}% vs free "
            f"{free['returned']}/{free['mature']} = {f_rate}% — "
            f"{round(o_rate / f_rate, 1)}x. MEASURED, not a target: the gap is "
            "the evidence for the work order above, and the lane's verdict "
            "hangs on composition, not on this ratio"))

    checks.append(_check(
        "id_volume", "cohort sizes behind the rates", True,
        f"mature cohorts — oauth {oauth['mature']} · free {free['mature']} · "
        f"trial email-bound {trial_bound['mature']} · trial key-only "
        f"{trial_only['mature']}. Small denominators are stated, not hidden: "
        f"read the composition gate, not a rate computed off "
        f"{oauth['mature']} OAuth key(s)"))
    return checks


# ══ LANE 2 · activation ═══════════════════════════════════════════════

# Candidate stamps a mint→first-call instrument would add. Probed rather than
# assumed so the lane self-heals the moment the instrumentation lands.
_FIRST_CALL_STAMPS = (
    ("mcp_dev_keys", "first_call_at"),
    ("mcp_dev_keys", "first_used_at"),
    ("mcp_dev_keys", "activated_at"),
    ("auto_trial_keys", "first_call_at"),
    ("auto_trial_keys", "first_used_at"),
    ("auto_trial_keys", "activated_at"),
)


def _lane_activation(c) -> list[dict]:
    """The mint-to-first-call cliff.

    DELIBERATELY UNMEASURED. "Did this key ever call?" is answerable today and
    is reported below. "How long after minting did the first call happen?" is
    NOT: last_used_at is a LAST-use stamp that every subsequent call overwrites,
    so it cannot yield a first-call latency, and no first-call column exists.
    Modelling the cliff from last_used_at would be a fabricated distribution.
    The critical check therefore renders None with the reason — the lane is "?",
    which is a work order for the instrumentation, not a defect and not a pass.
    """
    checks: list[dict] = []
    if c is None:
        return [_check("act_db", "key tables readable", None,
                       "no database connection — UNMEASURED, not zero",
                       critical=True)]

    present = []
    unknown = []
    for tbl, col in _FIRST_CALL_STAMPS:
        got = _column_exists(c, tbl, col)
        if got is True:
            present.append(f"{tbl}.{col}")
        elif got is None:
            unknown.append(f"{tbl}.{col}")

    if present:
        # The instrumentation landed — say so loudly and hand the measurement
        # to whoever wires the percentiles. Still not a PASS: this shell has
        # not yet been taught to read the new column, and claiming a measured
        # cliff off an unread column is exactly the class of error this board
        # exists to prevent.
        checks.append(_check(
            "act_cliff", "mint→first-call latency measured", None,
            "INSTRUMENTATION PRESENT (" + ", ".join(present) + ") but this "
            "lane has not been taught to read it. UNMEASURED until the "
            "percentile read is wired here — a first-call column that no "
            "board reads is not a measurement",
            critical=True))
    else:
        checks.append(_check(
            "act_cliff", "mint→first-call latency measured", None,
            "UNMEASURED — no first-call stamp exists on mcp_dev_keys or "
            "auto_trial_keys (probed: "
            + ", ".join(f"{t}.{col}" for t, col in _FIRST_CALL_STAMPS)
            + (f"; {len(unknown)} probe(s) unreadable" if unknown else "")
            + "). last_used_at is a LAST-use stamp overwritten by every "
            "subsequent call, so it cannot yield a first-call latency, and a "
            "cliff modelled from it would be a fabricated distribution. WORK "
            "ORDER: stamp first_call_at at mint-key first use (Task 2). This "
            "renders None on purpose: could-not-measure is neither fine nor "
            "broken",
            critical=True))

    # What IS answerable today, with its exact basis — reported so the lane is
    # informative while its headline stays honestly indeterminate.
    ever = _row(c, f"""
        SELECT COUNT(*), COUNT(*) FILTER (WHERE last_used_at IS NULL)
          FROM mcp_dev_keys
         WHERE created_at >= now() - interval '{WINDOW_DAYS} days'
    """)
    ever_t = _row(c, f"""
        SELECT COUNT(*), COUNT(*) FILTER (WHERE last_used_at IS NULL)
          FROM auto_trial_keys
         WHERE minted_at >= now() - interval '{WINDOW_DAYS} days'
    """)
    if ever is None and ever_t is None:
        checks.append(_check(
            "act_ever", "share of minted keys that ever called", None,
            "UNREADABLE — both key tables failed; NOT reported as zero"))
    else:
        minted = _i(ever[0] if ever else 0) + _i(ever_t[0] if ever_t else 0)
        silent = _i(ever[1] if ever else 0) + _i(ever_t[1] if ever_t else 0)
        partial = " (PARTIAL: one key table unreadable)" \
            if (ever is None or ever_t is None) else ""
        checks.append(_check(
            "act_ever", "share of minted keys that ever called",
            None if minted == 0 else True,
            (f"{silent} of {minted} keys minted in the window have NO recorded "
             f"use ({_pct(silent, minted)}%) — the largest absolute loss in "
             "the funnel. THIS IS 'ever called', NOT the cliff: it carries no "
             "timing, so it cannot say whether the silent keys died at second "
             "0 or at hour 6, which is the question the fix needs answered. "
             f"basis: mcp_dev_keys.created_at + auto_trial_keys.minted_at, "
             f"rolling {WINDOW_DAYS}d{partial}")
            if minted else
            f"no keys minted in the rolling {WINDOW_DAYS}d window — "
            "UNMEASURED, not 0%"))
    return checks


# ══ LANE 3 · conversion ═══════════════════════════════════════════════

def _lane_conversion(c) -> list[dict]:
    """machine_paid vs human_paid vs abandoned-at-the-gate.

    The two paid paths are gated SEPARATELY and on purpose. A single
    "conversions" number would average a structurally-zero human path against a
    machine path with a completely different failure mode, and the average
    would be the one number that hides both.
    """
    checks: list[dict] = []
    if c is None:
        return [_check("cv_db", "conversion tables readable", None,
                       "no database connection — UNMEASURED, not zero",
                       critical=True)]

    # ★ human_acted IS NOT RE-DERIVED HERE (r-definition-one-writer,
    # 2026-08-18). This query used to count `human_view_first_opened_at IS NOT
    # NULL` and render it as "HUMAN ACTED" — that is the v2 instrument, the
    # /relay stamp alone, with no relay_opens union, no probe exclusion and no
    # operator exclusion. So this board's headline handoff number and the
    # funnel's disagreed BY CONSTRUCTION while both called themselves
    # human_acted, and the prose beside it named a third version again. The
    # canonical count comes from routes/handoff_definition, byte-identical to
    # the funnel's own assembly (proved in
    # tests/test_published_definition_not_restated.py), and `abandoned` reads
    # the same instrument so "minted but did not act" cannot mean one thing in
    # the numerator and another in the denominator.
    _acted = _handoff_acted_predicate("s")
    gate = _row(c, f"""
        SELECT COUNT(*) FILTER (
                 WHERE s.last_hit_at >= now() - interval '{WINDOW_DAYS} days'),
               COUNT(*) FILTER (
                 WHERE s.claim_minted_at >= now() - interval '{WINDOW_DAYS} days'),
               ({_handoff_acted_sql(f"{WINDOW_DAYS} days")}),
               COUNT(*) FILTER (
                 WHERE s.claim_email IS NOT NULL AND s.claim_email <> ''
                   AND s.last_hit_at >= now() - interval '{WINDOW_DAYS} days'),
               COUNT(*) FILTER (
                 WHERE s.claim_minted_at >= now() - interval '{WINDOW_DAYS} days'
                   AND NOT {_acted}
                   AND (s.claim_email IS NULL OR s.claim_email = ''))
          FROM mcp_high_intent_sessions s
    """)
    if gate is None:
        checks.append(_check(
            "cv_gate", "gate instrument readable", None,
            "mcp_high_intent_sessions unreadable — the human path is "
            "UNMEASURED, which is NOT the same as the measured structural "
            "zero it usually shows", critical=True))
        hi = minted = human_acted = identified = abandoned = None
    else:
        hi, minted, human_acted, identified, abandoned = [_i(x) for x in gate]
        checks.append(_check(
            "cv_gate", "gate instrument readable", True,
            f"paywall high-intent sessions {hi} → relay minted {minted} → "
            f"HUMAN ACTED {human_acted} → identified {identified}; "
            f"abandoned at the gate (minted, no human open, no email) "
            f"{abandoned} ({_pct(abandoned, minted)}% of mints). "
            # DERIVED, never restated: the sentence is built from the same
            # definition block the API publishes, so this board cannot go on
            # describing a version the funnel has moved off.
            + _handoff_sentence() +
            f" basis: mcp_high_intent_sessions, rolling {WINDOW_DAYS}d; the "
            f"human_acted window is first_hit_at (as in the funnel), the "
            f"other stages' is their own stamp"))

    # ── human path ────────────────────────────────────────────────────
    human_paid = None
    hp = _row(c, f"""
        SELECT COUNT(DISTINCT s.id)
          FROM mcp_high_intent_sessions s
          JOIN mcp_conversions v
            ON lower(v.user_email) = lower(s.claim_email)
         WHERE s.claim_email IS NOT NULL AND s.claim_email <> ''
           AND v.created_at >= now() - interval '{WINDOW_DAYS} days'
    """)
    if hp is None:
        checks.append(_check(
            "cv_human_paid", "the HUMAN path produced a paid outcome", None,
            "UNMEASURED — the mcp_high_intent_sessions→mcp_conversions join "
            "failed; the human path's zero is not asserted from a failed read",
            critical=True))
    else:
        human_paid = _i(hp[0])
        checks.append(_check(
            "cv_human_paid", "the HUMAN path produced a paid outcome",
            human_paid > 0,
            f"human_paid = {human_paid} (identified gate sessions whose email "
            f"appears in mcp_conversions within the rolling {WINDOW_DAYS}d). "
            "REPORTED SEPARATELY from the machine path on purpose. Two causes, "
            "one fixed: the 2026-07-30 artifact split did NOT close the token "
            "race — mcp-server #193 (2026-08-16) measured ~96% of minted "
            "claims machine-redeemed by the server's own _autoRedeemClaim; "
            "auto-redeem is opt-in-off since 08-16. What remains is DELIVERY "
            "— whether agents SHOW the for_your_human link; human_acted "
            "now reads both human artifacts (relay_opens + "
            "human_view_first_opened_at), so watch those. WORK ORDER: get "
            "the human artifact in front of a human. basis: email join, "
            "rolling window",
            critical=True))

    # ── machine path ──────────────────────────────────────────────────
    rail = _table_exists(c, "x402_unlocks")
    try:
        from routes.x402_payments import x402_bp  # noqa: F401
        rail_wired = True
        rail_detail = "routes.x402_payments importable (agent-pay rail present)"
    except Exception as e:  # noqa: BLE001
        rail_wired = False
        rail_detail = f"routes.x402_payments NOT importable: {type(e).__name__}"

    if rail is None:
        checks.append(_check(
            "cv_machine_rail", "machine-pay rail is instrumented", None,
            f"x402_unlocks existence UNREADABLE; {rail_detail}", critical=True))
    else:
        checks.append(_check(
            "cv_machine_rail", "machine-pay rail is instrumented",
            bool(rail) and rail_wired,
            (f"x402_unlocks ledger {'present' if rail else 'ABSENT'}; "
             f"{rail_detail}. An unpaid rail and an UNINSTRUMENTED rail look "
             "identical from the outside — this check separates them"),
            critical=True))

    machine_paid = None
    if rail:
        mp = _row(c, f"""
            SELECT COUNT(*), COUNT(DISTINCT tool),
                   COALESCE(SUM(price_usd), 0)
              FROM x402_unlocks
             WHERE created_at >= now() - interval '{WINDOW_DAYS} days'
        """)
        if mp is not None:
            machine_paid = _i(mp[0])
            checks.append(_check(
                "cv_machine_paid", "the MACHINE path produced a paid outcome",
                machine_paid > 0,
                f"machine_paid = {machine_paid} settled unlocks across "
                f"{_i(mp[1])} tool(s), ${float(mp[2] or 0):.2f} — REPORTED "
                "SEPARATELY from human_paid; never sum them into one "
                "'conversions' figure, because the combined number is exactly "
                "what hides which path is broken. This is the in-turn rail "
                "(a connected agent pays and continues — no human, no relay, "
                "no summarization risk), the fix two independent platform "
                f"reviews named. basis: x402_unlocks, rolling {WINDOW_DAYS}d",
                critical=True))
        else:
            checks.append(_check(
                "cv_machine_paid", "the MACHINE path produced a paid outcome",
                None, "x402_unlocks unreadable — UNMEASURED, not zero",
                critical=True))
    else:
        checks.append(_check(
            "cv_machine_paid", "the MACHINE path produced a paid outcome",
            None,
            "no x402_unlocks ledger — the machine path cannot be measured at "
            "all, which is a DIFFERENT finding from 'measured zero' and must "
            "not be rendered as one", critical=True))

    checks.append(_check(
        "cv_separation", "the two paid paths are never combined", True,
        f"human_paid={human_paid} · machine_paid={machine_paid} · "
        f"abandoned_at_gate={abandoned} — three numbers, three denominators, "
        "no total. Kept apart by construction so neither path can be flattered "
        "by the other"))
    return checks


# ══ LANE 4 · questions retired ════════════════════════════════════════

def _classify(tools_used: int, failed: int, skipped: int, not_run: int,
              rejects: int, outcome: str | None) -> str:
    """complete / partial / failed — one workflow, one question.

    complete: the workflow ran end to end, every hand-off resolved, nothing
              deferred, no constraint violated. THIS is a retired question.
    failed:   the workflow produced nothing usable at all.
    partial:  it answered some of it — deferred steps, a failed step, or an
              invariant violation. Partial is NOT retired; counting it as
              closure is how usage volume gets mistaken for value.
    """
    if outcome == "failed" or tools_used <= 0:
        return "failed"
    if failed == 0 and skipped == 0 and not_run == 0 and rejects == 0 \
            and outcome in ("completed", None):
        return "complete"
    return "partial"


def _replays_from_recipe_executions(c, abandon_min: int):
    """(rows, source, note) — rows are (intent_class, tools, cls, in_flight)."""
    if _table_exists(c, "recipe_executions") is not True:
        return None, None, "recipe_executions absent"
    raw = _rows(c, f"""
        SELECT intent_class,
               COALESCE(steps_executed, 0) + COALESCE(steps_gated, 0),
               COALESCE(steps_failed, 0),
               COALESCE(steps_skipped, 0),
               COALESCE(steps_not_run, 0),
               outcome,
               EXTRACT(EPOCH FROM (now() - started_at)) / 60.0
          FROM recipe_executions
         WHERE started_at >= now() - interval '{WINDOW_DAYS} days'
           AND source = 'execute_plan'
    """)
    if raw is None:
        return None, None, "recipe_executions unreadable"
    out = []
    for r in raw:
        cls_id = r[0]
        tools, f, sk, nr = _i(r[1]), _i(r[2]), _i(r[3]), _i(r[4])
        outcome = r[5]
        age_min = float(r[6] or 0)
        # In flight: no terminal event yet and younger than the canonical
        # abandonment threshold. Excluded from the denominator — counting a
        # running workflow as a failure is a manufactured red.
        if outcome is None and age_min < abandon_min:
            out.append((cls_id, tools, None, True))
            continue
        out.append((cls_id, tools,
                    _classify(tools, f, sk, nr, 0, outcome), False))
    return out, "recipe_executions", (
        "one row per execute_plan workflow (gateway lifecycle events). "
        "constraint_rejects is not a column here, so an ISO-boundary "
        "violation is NOT counted against closure in this source")


def _replays_from_call_log(c):
    if _table_exists(c, "mcp_call_log") is not True:
        return None, None, "mcp_call_log absent"
    raw = _rows(c, f"""
        SELECT params FROM mcp_call_log
         WHERE tool = 'execute_plan_steps'
           AND timestamp >= now() - interval '{WINDOW_DAYS} days'
    """)
    if raw is None:
        return None, None, "mcp_call_log unreadable"
    out = []
    for (p,) in raw:
        if isinstance(p, str):
            try:
                p = json.loads(p)
            except Exception:
                continue
        if not isinstance(p, dict):
            continue
        counts = p.get("status_counts") or {}
        if not isinstance(counts, dict):
            counts = {}
        tools = sum(_i(counts.get(k)) for k in _GOOD_STATUSES)
        out.append((p.get("intent_class"), tools,
                    _classify(tools, _i(counts.get("failed")),
                              _i(counts.get("skipped_unresolved")),
                              _i(counts.get("not_run")),
                              _i(p.get("constraint_rejects")), "completed"),
                    False))
    return out, "mcp_call_log/execute_plan_steps", (
        "one row per execution, params carry per-step status_counts AND "
        "constraint_rejects — so an answer that drifted outside the requested "
        "geography counts as NOT closed")


def _lane_questions_retired(c) -> list[dict]:
    """Per canonical problem: median tools used, and did ONE workflow close it.

    This is the customer-value lane. Call volume says a question was ASKED;
    only closure says it was ANSWERED, and the two move in opposite directions
    when the planner is bad (a broken graph burns more tools, not fewer).
    """
    checks: list[dict] = []

    # ★ Contract-drift guard. The problem list is IMPORTED; if a new anchor
    # recipe is published with no class mapping here, this FAILS rather than
    # quietly measuring four problems out of six.
    try:
        from routes.anchor_intents import ANCHORS, contract_hash
        recipes = []
        for a in ANCHORS:
            if a["recipe"] not in recipes:
                recipes.append(a["recipe"])
        unmapped = [r for r in recipes if r not in _PROBLEM_CLASSES]
        stale = [r for r in _PROBLEM_CLASSES if r not in recipes]
        checks.append(_check(
            "qr_contract", "every published problem has a measurement mapping",
            not unmapped and not stale,
            (f"canonical anchors {len(recipes)} · contract {contract_hash()} — "
             "all mapped to planner intent classes"
             if not unmapped and not stale else
             (("UNMAPPED published problem(s): " + ", ".join(unmapped) + " — "
               "they would go silently unmeasured. ") if unmapped else "")
             + (("STALE mapping(s) with no published anchor: "
                 + ", ".join(stale) + ". ") if stale else "")
             + "Fix _PROBLEM_CLASSES in this file; the problem LIST is "
               "imported from routes/anchor_intents.py and is not editable "
               "here"),
            critical=True))
    except Exception as e:  # noqa: BLE001
        return [_check("qr_contract",
                       "every published problem has a measurement mapping",
                       None,
                       f"routes.anchor_intents unimportable: {type(e).__name__}"
                       " — the canonical problem list could not be read, so "
                       "nothing below is measurable", critical=True)]

    if c is None:
        checks.append(_check("qr_db", "replay source readable", None,
                             "no database connection — UNMEASURED, not zero",
                             critical=True))
        return checks

    try:
        from recipe_lifecycle import ABANDONED_AFTER_MINUTES as _abandon
    except Exception:  # noqa: BLE001
        _abandon = 15

    rows, source, note = _replays_from_recipe_executions(c, _abandon)
    if not rows:
        rows2, source2, note2 = _replays_from_call_log(c)
        if rows2:
            rows, source, note = rows2, source2, note2
        elif rows is None:
            rows, source, note = rows2, source2, f"{note} / {note2}"

    if rows is None:
        checks.append(_check("qr_source", "execute_plan replays readable", None,
                             f"UNMEASURED — {note}", critical=True))
        return checks

    checks.append(_check(
        "qr_source", "execute_plan replays readable", True,
        f"source = {source or 'none'} · {len(rows)} workflow(s) in the rolling "
        f"{WINDOW_DAYS}d window · {note}"))

    # Bucket by canonical problem.
    cls_to_problem = {cid: prob
                      for prob, ids in _PROBLEM_CLASSES.items() for cid in ids}
    buckets: dict[str, dict] = {p: {"tools": [], "complete": 0, "partial": 0,
                                    "failed": 0, "in_flight": 0}
                                for p in _PROBLEM_CLASSES}
    other = 0
    for cls_id, tools, verdict, in_flight in rows:
        prob = cls_to_problem.get(cls_id or "")
        if prob is None:
            other += 1
            continue
        b = buckets[prob]
        if in_flight:
            b["in_flight"] += 1
            continue
        b[verdict] += 1
        if verdict != "failed":
            b["tools"].append(tools)

    measured, closed = 0, 0
    for prob in _PROBLEM_CLASSES:
        b = buckets[prob]
        runs = b["complete"] + b["partial"] + b["failed"]
        label = _PROBLEM_LABELS.get(prob, prob)
        if runs == 0:
            checks.append(_check(
                f"qr_{prob}", f"{label} — one workflow closes it", None,
                f"UNMEASURED — 0 completed workflows in the rolling "
                f"{WINDOW_DAYS}d window"
                + (f" ({b['in_flight']} still in flight)"
                   if b["in_flight"] else "")
                + f". intent classes: {', '.join(_PROBLEM_CLASSES[prob])}. "
                  "Zero runs is an absence of demand or of routing, NOT a "
                  "closure rate of 0%"))
            continue
        measured += 1
        med = statistics.median(b["tools"]) if b["tools"] else None
        ok = b["complete"] * 2 > runs
        if ok:
            closed += 1
        checks.append(_check(
            f"qr_{prob}", f"{label} — one workflow closes it", ok,
            f"median tools used {med if med is not None else '?'} · "
            f"complete {b['complete']} / partial {b['partial']} / failed "
            f"{b['failed']} of {runs} run(s) "
            f"({_pct(b['complete'], runs)}% closed in ONE workflow)"
            + (f" · {b['in_flight']} in flight, excluded" if b["in_flight"]
               else "")
            + f". GREEN CONDITION: a majority of runs CLOSE. median is over "
              "non-failed runs (a failed run's tool count measures the "
              "failure, not the question)"))

    if measured == 0:
        checks.append(_check(
            "qr_rollup", "canonical problems close in ONE workflow", None,
            f"UNMEASURED — no canonical problem had a completed workflow in "
            f"the rolling {WINDOW_DAYS}d window ({other} run(s) routed to "
            "classes outside the canonical set). Not a zero closure rate",
            critical=True))
    else:
        checks.append(_check(
            "qr_rollup", "canonical problems close in ONE workflow",
            closed * 2 > measured,
            f"{closed} of {measured} measurable canonical problem(s) close a "
            f"majority of their runs in one workflow "
            f"({len(_PROBLEM_CLASSES) - measured} problem(s) had no runs and "
            f"stay UNMEASURED; {other} run(s) routed outside the canonical "
            "set). THIS IS THE CUSTOMER-VALUE METRIC: a question retired in "
            "one workflow, not a call count",
            critical=True))
    return checks


# ── questions-retired structured block (same data, machine-readable) ──

def _questions_retired_block(lane_checks: list[dict]) -> dict:
    """The metric, lifted out of the lane so a consumer never has to parse
    prose. Derived from the SAME checks the board renders — one computation,
    two presentations, so they cannot disagree."""
    per = {}
    for k in lane_checks:
        if not k["id"].startswith("qr_") or k["id"] in (
                "qr_contract", "qr_source", "qr_rollup", "qr_db"):
            continue
        per[k["id"][3:]] = {"verdict": ("closed" if k["pass"] is True else
                                        ("not_closed" if k["pass"] is False
                                         else "UNMEASURED")),
                            "detail": k["detail"]}
    roll = next((k for k in lane_checks if k["id"] == "qr_rollup"), None)
    return {
        "definition": ("per canonical problem: median tools used by a "
                       "workflow, and whether ONE execute_plan workflow fully "
                       "closed the question (complete / partial / failed). "
                       "Complete = every hand-off resolved, nothing deferred, "
                       "no constraint violation. Partial is NOT retired."),
        "why": ("measures customer value, not usage volume — a broken planner "
                "burns MORE tools, so call counts move the wrong way"),
        "window": {"kind": "rolling", "days": WINDOW_DAYS, "note": _WINDOW_NOTE},
        "problems_source": "routes/anchor_intents.py ANCHORS (imported)",
        "per_problem": per,
        "rollup": (roll or {}).get("detail"),
        "rollup_verdict": ("PASS" if (roll or {}).get("pass") is True else
                           ("FAIL" if (roll or {}).get("pass") is False
                            else "UNMEASURED")),
    }


# ══ LANE 5 · lookup vs workflow ═══════════════════════════════════════
#
# THE QUESTION. ~2,300 tool calls a week against a handful of execute_plan
# runs a month reads as "agents do single LOOKUPS, not problems". That matters
# commercially because gating, depth and the payment wall all live in
# WORKFLOWS — a lookup is free BY DESIGN — so the funnel can read "nobody
# pays" when the truth is "almost nobody enters the part of the product worth
# paying for". Three stories fit that shape and need completely different
# work: A DEMAND (nobody is asking), B ROUTING (they ask, the agent answers
# with one lookup instead of the front door), C VISIBILITY (workflows run, the
# attribution never reaches the human — lane 6). This lane separates A from B.
#
# ★ ONE WINDOW. The 2,300-vs-16 figure that motivated this lane compared a
# ROLLING 7d against a ROLLING 30d. That is not a ratio, it is two numbers.
# Both sides here are counted over the SAME rolling WINDOW_DAYS window, from
# the SAME table, under the SAME canonical predicates. If either side cannot
# be computed on that window the lane renders UNMEASURED rather than mixing.

# ★ IMPORTED, never re-listed. A second hand-written exclusion list is the
# drift class mcp_calls_deloop exists to retire.
from mcp_calls_deloop import (  # noqa: E402
    CANONICAL_AGENTS_BASIS, PLATFORM_CASE,
)

_WORKFLOW_TOOL = "execute_plan"

# NAVIGATION — neither side. See _CLASS_DEFS for the reasoning, which is
# published in the payload rather than buried here.
_NAV_TOOLS = ("plan_query", "discover_tools")
_NAV_SQL = "('" + "','".join(_NAV_TOOLS) + "')"

# ★ NOT AGENT CALLS AT ALL. `execute_plan_steps` is a SYNTHETIC telemetry row
# the MCP server writes about a run it just finished (server.mjs ~9966), and
# `recipe:*` rows are prompts/get fetches. Both land in mcp_tool_calls with a
# tool_name and would otherwise be counted as lookups — 19 + 27 rows in the
# live 30d window, i.e. a workflow finishing would INCREMENT the lookup side.
_PSEUDO_SQL = ("(tool_name = 'execute_plan_steps' "
               "OR tool_name LIKE 'recipe:%')")

# A workflow sub-call arrives over the loopback interface: execute_plan runs
# each step by POSTing a real tools/call to http://127.0.0.1:PORT/mcp
# (server.mjs _execLoopbackCall ~11630) and forwards no X-Forwarded-For, so
# the row is stamped with the loopback address, not the agent's IP.
_LOOPBACK_SQL = r"client_ip ~ '^(127\.|::1)'"

# ★ ONE-LINE OPERATIONAL DEFINITIONS, published in the payload. Requirement:
# a reader must be able to tell which side any given row lands on WITHOUT
# reading this file.
_CLASS_DEFS = {
    "workflow": (
        "an execute_plan run — the planner routes and then EXECUTES a "
        "multi-step graph. Counted as exactly ONE workflow, at the row where "
        "the agent called execute_plan."),
    "lookup": (
        "a direct single tools/call the agent made itself — one tool, one "
        "answer, no planner. Every real external call that is not a workflow, "
        "not navigation and not a synthetic row."),
    "workflow_sub_calls": (
        "the tools a workflow runs INTERNALLY (six-step graph = six sub-"
        "calls). They land on NEITHER side. execute_plan executes each step "
        "by POSTing a real tools/call to 127.0.0.1, so every sub-call writes "
        "its own mcp_tool_calls row — if they were counted as lookups the "
        "ratio would measure ITSELF: running more workflows would raise the "
        "lookup count and make the front door look less used the more it was "
        "used. They are excluded because the canonical identity basis already "
        "drops them (loopback fails is_public_ip), and lw_subcall_guard "
        "MEASURES that exclusion every tick instead of assuming it."),
    "plan_query": (
        "NAVIGATION, not a lookup and not a workflow. It returns a plan and "
        "executes nothing, so counting it as a workflow would credit intent "
        "as execution; it answers no infrastructure question, so counting it "
        "as a lookup would inflate the lookup side with front-door-SEEKING "
        "behaviour — the opposite of what this lane measures."),
    "discover_tools": (
        "NAVIGATION, same treatment as plan_query. It is an agent reading the "
        "menu, not ordering from it. Published separately so the choice is "
        "visible and reversible rather than silently folded into either side."),
}

# The withholding discipline is IMPORTED from the report that already owns it
# (routes/problems_solved.py). A ratio over a handful of calls is a
# coincidence wearing a percentage sign; a SECOND, weaker rule invented here
# would be the drift this codebase keeps paying for.
#
# ★ The module HANDLE is kept, and _MIN_RUNS / _WINDOW_DAYS are read through
# it at CALL time — never snapshotted into a local at import. A copied
# constant rots on its own schedule: if problems_solved raised its minimum,
# a snapshot here would keep gating `measurable` at the old floor while
# _rate_verdict withheld at the new one, and the lane would publish a platform
# verdict built on a sample its own withholding rule had refused.
try:
    from routes import problems_solved as _ps  # noqa: E402
    _lw_rate_verdict = _ps._rate_verdict
    _LW_WITHHOLD_IMPORT = None
except Exception as _e:  # noqa: BLE001
    _ps = None
    _lw_rate_verdict = None
    _LW_WITHHOLD_IMPORT = f"{type(_e).__name__}: {str(_e)[:100]}"


def _lw_min_runs():
    """The withholding floor, read LIVE from the module that owns it."""
    return None if _ps is None else _ps._MIN_RUNS


def _lw_ps_window_days():
    """The window the imported withheld-reason text names, read LIVE."""
    return None if _ps is None else _ps._WINDOW_DAYS

# GREEN CONDITION for the lane, stated once and used by the critical check.
# Deliberately NOT "the global workflow share went up": one integration doing
# every workflow while six platforms do none is a DIFFERENT problem from all
# seven doing lookups, and a global percentage cannot tell them apart.
_LW_PLATFORM_FLOOR_PCT = 5.0


def _lw_population_sql(extra: str = "") -> str:
    """The one population both sides are counted over. Canonical identity
    basis (mcp_calls_identity, is_public_ip AND is_real_external) — the same
    view every public 'distinct agents' surface reads."""
    return (f"FROM mcp_calls_identity "
            f"WHERE created_at >= now() - interval '{WINDOW_DAYS} days' "
            f"AND is_public_ip AND is_real_external {extra}")


def _lw_subcall_guard(c) -> dict:
    """MEASURED, never assumed: are workflow fan-out sub-calls kept out of the
    lookup count?

    ★ THIS GUARD CANNOT PASS VACUOUSLY. If the window contains no loopback
    rows at all there is nothing to prove excluded, and the guard returns
    UNMEASURED — a guard that goes green on an empty population is worse than
    no guard. Returns {'state': True|False|None, 'loopback': n, 'leaked': n}.
    """
    r = _row(c, f"""
        SELECT COUNT(*) FILTER (WHERE {_LOOPBACK_SQL}),
               COUNT(*) FILTER (WHERE {_LOOPBACK_SQL} AND is_public_ip)
          FROM mcp_calls_identity
         WHERE created_at >= now() - interval '{WINDOW_DAYS} days'
    """)
    if r is None:
        return {"state": None, "loopback": None, "leaked": None,
                "why": "population unreadable"}
    loop, leaked = _i(r[0]), _i(r[1])
    if loop == 0:
        return {"state": None, "loopback": 0, "leaked": 0,
                "why": ("NO loopback rows in the window — the exclusion has "
                        "nothing to bite on, so this guard is UNMEASURED "
                        "rather than green. A guard that passes on an empty "
                        "population proves nothing")}
    return {"state": leaked == 0, "loopback": loop, "leaked": leaked,
            "why": ""}


def _lane_lookup_vs_workflow(c) -> list[dict]:
    """Lookups vs workflows, ONE window, canonical basis, split by platform.

    BORN RED. The green condition is a MAJORITY of measurable platforms
    clearing a workflow floor — not a global percentage, which one heavy
    integration can carry on its own.
    """
    checks: list[dict] = []
    if c is None:
        return [_check("lw_db", "call population readable", None,
                       "no database connection — UNMEASURED, not zero",
                       critical=True)]

    # ── the withholding rule must be the IMPORTED one, on THIS window ──
    if _lw_rate_verdict is None:
        checks.append(_check(
            "lw_withhold", "withholding rule imported, not reinvented", None,
            "routes.problems_solved unimportable "
            f"({_LW_WITHHOLD_IMPORT}) — every ratio below is withheld rather "
            "than computed under a weaker locally-invented rule",
            critical=True))
        return checks
    _min_runs = _lw_min_runs()
    if _lw_ps_window_days() != WINDOW_DAYS:
        checks.append(_check(
            "lw_withhold", "withholding rule imported, not reinvented", False,
            f"WINDOW MISMATCH: this lane counts a rolling {WINDOW_DAYS}d "
            f"window but the imported _rate_verdict states its reasons in "
            f"{_lw_ps_window_days()}d. The withheld-reason text would name the "
            "wrong window, so the ratios are refused rather than published "
            "with a false basis. Fix: align WINDOW_DAYS or pass the window "
            "through", critical=True))
        return checks
    checks.append(_check(
        "lw_withhold", "withholding rule imported, not reinvented", True,
        f"_rate_verdict / _MIN_RUNS={_min_runs} imported from "
        f"routes/problems_solved.py, both windows are rolling {WINDOW_DAYS}d. "
        "A ratio over fewer than "
        f"{_min_runs} classified calls is WITHHELD, never published"))

    # ── the self-inflation guard, measured ──
    g = _lw_subcall_guard(c)
    checks.append(_check(
        "lw_subcall_guard",
        "workflow sub-calls are NOT counted as lookups", g["state"],
        (f"{g['loopback']} loopback (127.0.0.1) sub-call row(s) in the window, "
         f"{g['leaked']} of them inside the counted population. execute_plan "
         "runs each step as a real tools/call to 127.0.0.1 with no "
         "X-Forwarded-For, so the canonical is_public_ip predicate drops "
         "them. GREEN CONDITION: leaked = 0 AND loopback > 0. If this ever "
         "fails, the ratio measures itself — more workflows would mean more "
         "'lookups'"
         if g["state"] is not None else f"UNMEASURED — {g['why']}"),
        critical=True))
    if g["state"] is False:
        return checks

    # ── the counts, both sides, ONE window ──
    pc = PLATFORM_CASE.strip()
    rows = _rows(c, f"""
        SELECT {pc} AS plat,
               COUNT(*) FILTER (WHERE tool_name = '{_WORKFLOW_TOOL}'),
               COUNT(*) FILTER (WHERE tool_name <> '{_WORKFLOW_TOOL}'
                                  AND tool_name NOT IN {_NAV_SQL}
                                  AND NOT {_PSEUDO_SQL}),
               COUNT(*) FILTER (WHERE tool_name IN {_NAV_SQL}),
               COUNT(DISTINCT agent_id)
          {_lw_population_sql()}
         GROUP BY 1
    """)
    if rows is None:
        checks.append(_check(
            "lw_counts", "both sides counted on ONE window", None,
            "UNMEASURED — mcp_calls_identity unreadable. BOTH sides are "
            "withheld: publishing one side of a ratio is how the 2,300-vs-16 "
            "mixed-window figure happened", critical=True))
        return checks

    per = []
    tot_wf = tot_lk = tot_nav = 0
    for plat, wf, lk, nav, agents in rows:
        wf, lk, nav, agents = _i(wf), _i(lk), _i(nav), _i(agents)
        tot_wf += wf
        tot_lk += lk
        tot_nav += nav
        per.append({"platform": str(plat), "workflows": wf, "lookups": lk,
                    "navigation": nav, "agents": agents,
                    "classified": wf + lk})
    per.sort(key=lambda p: -p["classified"])
    classified = tot_wf + tot_lk

    checks.append(_check(
        "lw_counts", "both sides counted on ONE window", True,
        f"{tot_wf} workflow(s) vs {tot_lk} lookup(s) over the SAME rolling "
        f"{WINDOW_DAYS}d window ({tot_nav} navigation call(s) held out of "
        f"both sides). basis: {CANONICAL_AGENTS_BASIS.split('.')[0]}. "
        "★ The 2,300-vs-16 figure this lane replaces compared rolling 7d "
        "against rolling 30d and must never be restated"))

    # ── the global ratio, withheld on a thin sample ──
    share, withheld = _lw_rate_verdict(tot_wf, classified)
    checks.append(_check(
        "lw_global_share", "front-door share of classified calls",
        None,  # informational by design — never a verdict. See the detail.
        (f"{tot_wf} of {classified} classified call(s) are workflows "
         f"({share}%) over a rolling {WINDOW_DAYS}d window"
         if share is not None else f"WITHHELD — {withheld}")
        + ". INFORMATIONAL ONLY, deliberately not a verdict: a single heavy "
          "integration can carry a global percentage while every other "
          "platform runs none. The verdict lives in lw_platform_majority"))

    # ── THE VERDICT: per-platform, because the shape IS the finding ──
    measurable = [p for p in per if p["classified"] >= _min_runs]
    thin = [p for p in per if p["classified"] < _min_runs]
    if not measurable:
        checks.append(_check(
            "lw_platform_majority",
            "a MAJORITY of platforms route through the front door", None,
            f"UNMEASURED — no platform reached {_min_runs} classified "
            f"calls in the rolling {WINDOW_DAYS}d window "
            f"({len(thin)} platform(s) below the floor). A ratio over a "
            "handful of calls is a coincidence, so no platform verdict is "
            "published. This is an absence of SAMPLE, never a 0% front-door "
            "share", critical=True))
    else:
        clearing = []
        for p in measurable:
            pct, _ = _lw_rate_verdict(p["workflows"], p["classified"])
            p["workflow_share_pct"] = pct
            if pct is not None and pct >= _LW_PLATFORM_FLOOR_PCT:
                clearing.append(p["platform"])
        ok = len(clearing) * 2 > len(measurable)
        top = ", ".join(
            f"{p['platform']} {p['workflows']}wf/{p['lookups']}lk"
            f" ({p['workflow_share_pct']}%)" for p in measurable[:8])
        checks.append(_check(
            "lw_platform_majority",
            "a MAJORITY of platforms route through the front door", ok,
            f"{len(clearing)} of {len(measurable)} platform(s) with a "
            f"measurable sample (>={_min_runs} classified calls) clear a "
            f"{_LW_PLATFORM_FLOOR_PCT}% workflow share"
            + (f" — clearing: {', '.join(clearing)}" if clearing
               else " — NONE clear it")
            + f". Measured: {top}"
            + (f" · {len(thin)} platform(s) below the sample floor, withheld"
               if thin else "")
            + ". GREEN CONDITION: a MAJORITY of measurable platforms clear "
              f"{_LW_PLATFORM_FLOOR_PCT}%. Stated per-platform ON PURPOSE — "
              "one platform doing workflows while six do lookups is a ROUTING "
              "problem in tool descriptions and discovery; all of them doing "
              "lookups would be a DEMAND problem, and completely different "
              "work. WORK ORDER: route the front door in the platforms that "
              "miss it — this cannot be moved by a copy change to a board",
            critical=True))

    # ── the lookup-only population: the front door being walked past ──
    lo = _rows(c, f"""
        WITH pop AS (SELECT * {_lw_population_sql()}),
             wf_agents AS (SELECT DISTINCT agent_id FROM pop
                            WHERE tool_name = '{_WORKFLOW_TOOL}'
                              AND agent_id IS NOT NULL)
        SELECT tool_name, COUNT(*), COUNT(DISTINCT agent_id)
          FROM pop
         WHERE agent_id IS NOT NULL
           AND agent_id NOT IN (SELECT agent_id FROM wf_agents)
           AND tool_name <> '{_WORKFLOW_TOOL}'
           AND tool_name NOT IN {_NAV_SQL}
           AND NOT {_PSEUDO_SQL}
         GROUP BY 1 ORDER BY 2 DESC LIMIT 10
    """)
    agent_split = _row(c, f"""
        WITH pop AS (SELECT * {_lw_population_sql()})
        SELECT COUNT(DISTINCT agent_id) FILTER (WHERE agent_id IS NOT NULL),
               COUNT(DISTINCT agent_id) FILTER (
                 WHERE tool_name = '{_WORKFLOW_TOOL}' AND agent_id IS NOT NULL)
          FROM pop
    """)
    if lo is None or agent_split is None:
        checks.append(_check(
            "lw_lookup_only", "the front door agents walk past is named", None,
            "UNMEASURED — lookup-only population unreadable"))
    else:
        all_ag, wf_ag = _i(agent_split[0]), _i(agent_split[1])
        top = "; ".join(f"{t} {_i(n)} calls/{_i(a)} agents" for t, n, a in lo[:6])
        checks.append(_check(
            "lw_lookup_only", "the front door agents walk past is named", None,
            f"{all_ag - wf_ag} of {all_ag} agent(s) NEVER ran a workflow in "
            f"the rolling {WINDOW_DAYS}d window. What they call instead, most "
            f"first: {top}. ★ THIS IS THE ACTIONABLE OUTPUT — each of these "
            "is a single-capability answer to a question execute_plan is "
            "built to answer end-to-end. INFORMATIONAL: a population, not a "
            "pass/fail"))
    return checks


def _lookup_vs_workflow_block(lane_checks: list[dict]) -> dict:
    """The lane's numbers, machine-readable, derived from the SAME checks the
    board renders — one computation, two presentations, so they cannot
    disagree."""
    by = {k["id"]: k for k in lane_checks}
    return {
        "question": ("are agents solving PROBLEMS (workflows) or doing single "
                     "LOOKUPS? Gating, depth and the payment wall all live in "
                     "workflows; a lookup is free BY DESIGN"),
        "window": {"kind": "rolling", "days": WINDOW_DAYS,
                   "same_window_both_sides": True,
                   "note": ("BOTH sides are counted over this ONE window from "
                            "ONE table. The 2,300-vs-16 figure that motivated "
                            "this lane compared rolling 7d against rolling "
                            "30d — that is two numbers, not a ratio, and it "
                            "is not reproduced here")},
        "classification": _CLASS_DEFS,
        "identity_basis": CANONICAL_AGENTS_BASIS,
        "externality_predicates": ("IMPORTED from mcp_calls_deloop "
                                   "(PLATFORM_CASE + the is_real_external "
                                   "rendering in mcp_calls_identity) — this "
                                   "lane keeps no second exclusion list"),
        "withholding": (f"_rate_verdict / _MIN_RUNS={_lw_min_runs()} imported "
                        "from routes/problems_solved.py"
                        if _lw_min_runs() is not None else
                        f"UNAVAILABLE: {_LW_WITHHOLD_IMPORT}"),
        "self_inflation_guard": (by.get("lw_subcall_guard") or {}).get("detail"),
        "counts": (by.get("lw_counts") or {}).get("detail"),
        "global_share": (by.get("lw_global_share") or {}).get("detail"),
        "per_platform_verdict": (by.get("lw_platform_majority") or {}).get(
            "detail"),
        "lookup_only_population": (by.get("lw_lookup_only") or {}).get("detail"),
        "stories": {
            "A_demand": "humans are not asking infrastructure questions",
            "B_routing": ("they ask, but agents answer with one lookup "
                          "instead of the front door — fixable in tool "
                          "descriptions / discovery"),
            "C_visibility": ("workflows run but attribution never reaches the "
                             "human — that is lane 6, and it is UNMEASURED"),
            "how_this_lane_separates_them": (
                "the PER-PLATFORM shape. Workflows concentrated in one or two "
                "platforms while high-volume platforms run none is B ROUTING. "
                "Uniformly near-zero across every platform WITH a measurable "
                "sample would be A DEMAND. This lane cannot settle C"),
        },
    }


# ══ LANE 6 · citation survival ════════════════════════════════════════
#
# ★ READ THE BOUNDARY BEFORE READING ANY NUMBER IN THIS LANE.
#
# We can observe what we SENT. We CANNOT observe what the agent rendered to
# its human — that happens entirely inside the client, on the far side of the
# MCP transport, and no server-side instrument reaches it. So "did our
# citation survive to the human?" is UNMEASURED, and this lane says so in the
# payload instead of substituting a number that measures something else.
#
# ★ PASS IS UNREACHABLE BY CONSTRUCTION in this lane, on purpose. cs_boundary
# is critical and permanently indeterminate, so the best this lane can render
# is "?" — never PASS. A lane that could go green would be claiming we had
# measured citation survival, which we have not. It renders FAIL today because
# the SEND-SIDE PRECONDITION is broken, and that is a real, fixable finding:
# if we do not send the attribution, it certainly never arrives.

# Sources this lane is ALLOWED to read. The forbidden list is not decoration:
# ai_citations / citation_probes / citation_scores are OUR OWN probes of
# public LLM answers ("did ChatGPT mention dchub.cloud when asked about data
# centres") — a completely different population, measured a completely
# different way, about traffic that never touched MCP. Reading them here
# would produce exactly the plausible-looking citation number this codebase
# has spent a month eliminating. _lane_citation_survival FAILS if one is ever
# wired in, so the refusal is a guard rather than a comment.
_CS_SOURCES = ("live MCP tools/call probe — envelope inspection only",)
_CS_FORBIDDEN = ("ai_citations", "citation_probes", "citation_scores")

# Probed surfaces. Fast + keyless on purpose: the CF admin route trips at 15s
# and returns 503, which the worker reads as a dead origin. execute_plan is
# ~40s and is therefore NOT probed here — see cs_workflow_surface.
_CS_PROBES = (
    ("get_grid_scoreboard", {}),
    ("search_facilities", {"query": "phoenix", "limit": 2}),
)
_CS_PROBE_TIMEOUT_S = 6


def _cs_probe(base: str, tool: str, args: dict) -> dict:
    """One live tools/call, envelope inspected for attribution. Fail-soft:
    every failure mode returns error=... so the check renders UNMEASURED
    rather than a flattering zero."""
    # ★ requests, NOT urllib.request. Two independent reasons and the repo
    # lint enforces the first: (a) urllib.request on Railway is a banned
    # pattern here, (b) the default Python-urllib UA is blocked by a PREFIX
    # rule that fires BEFORE the CF worker (2026-08-10), so the probe would
    # read as a transport failure rather than as a missing envelope. The UA is
    # set explicitly regardless, so neither library's default can matter.
    import requests
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "User-Agent": "dchub-adoption-shell-probe/1.0",
        # Tagged so this probe is excluded from lane 5's population by the
        # SAME canonical predicates — a diagnostic must never enter its own
        # basis. The tag also hits both the 'dchub-' prefix and the '-probe'
        # suffix in flask_mcp_endpoints._is_selfheal_synthetic, so the
        # analytics row is skipped at WRITE time: no write amplification.
        "X-MCP-Platform": "dchub-adoption-probe",
    }
    payload_out = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                   "params": {"name": tool, "arguments": args}}
    try:
        r = requests.post(base, json=payload_out, headers=headers,
                          timeout=_CS_PROBE_TIMEOUT_S)
        raw = r.text
    except Exception as e:  # noqa: BLE001
        return {"tool": tool, "error": f"{type(e).__name__}: {str(e)[:80]}"}
    payload = None
    for line in raw.split("\n"):
        if line.startswith("data:"):
            try:
                payload = json.loads(line[5:].strip())
            except Exception:  # noqa: BLE001
                pass
            break
    if payload is None:
        try:
            payload = json.loads(raw)
        except Exception:  # noqa: BLE001
            return {"tool": tool, "error": "unparseable response"}
    sc = ((payload.get("result") or {}).get("structuredContent")) or {}
    if not isinstance(sc, dict):
        return {"tool": tool, "error": "no structuredContent"}
    blob = json.dumps(sc)
    cite = sc.get("citation")
    return {
        "tool": tool,
        # Envelope level = what an agent composing an answer actually reads.
        "envelope_citation": bool(cite) and (
            isinstance(cite, str) or (isinstance(cite, dict)
                                      and bool(cite.get("cite_as")))),
        "envelope_provenance": bool(sc.get("provenance")),
        # Anywhere = including nested inside a step result. Weaker: an agent
        # is not required to walk the tree, so this is reported but not the
        # pass condition.
        "cite_as_anywhere": "cite_as" in blob,
        "error": None,
    }


def _lane_citation_survival(c) -> list[dict]:
    """Does our attribution reach the human? UNMEASURED — and this states the
    boundary precisely rather than filling the lane with a proxy."""
    checks: list[dict] = []

    # ── 1. the decoy guard ──
    wired = [t for t in _CS_FORBIDDEN
             if any(t in s for s in _CS_SOURCES)]
    checks.append(_check(
        "cs_no_decoy", "no look-alike citation table is read as the answer",
        not wired,
        ("declared sources: " + "; ".join(_CS_SOURCES)
         + f". HELD OUT: {', '.join(_CS_FORBIDDEN)} — those are OUR OWN "
           "probes of public LLM answers (did an engine mention dchub.cloud), "
           "a different population measured a different way about traffic "
           "that never touched MCP. Reading them here would produce a "
           "confident citation percentage that measures something else"
         if not wired else
         f"DECOY WIRED IN: {', '.join(wired)} now appears in this lane's "
         "declared sources. That table does not measure MCP citation "
         "survival and this lane must not read it"),
        critical=True))

    # ── 2. what IS observable: the send side ──
    # Kill switch: the probe is the ONLY outbound call this read-only board
    # makes. Off → UNMEASURED naming the flag, never a pass and never a zero.
    # (CI runs with it off: a unit suite must not depend on the live edge.)
    if (os.environ.get("ADOPTION_CITATION_PROBE_DISABLE") or "").strip() == "1":
        checks.append(_check(
            "cs_send_side",
            "every response we SEND carries cite_as + provenance", None,
            "UNMEASURED — the live send-side probe is disabled "
            "(ADOPTION_CITATION_PROBE_DISABLE=1). Not measured is not "
            "measured: this is neither a pass nor a zero", critical=True))
        results = []
    else:
        base = (os.environ.get("DCHUB_MCP_PROBE_URL")
                or "https://dchub.cloud/mcp")
        results = [_cs_probe(base, t, a) for t, a in _CS_PROBES]
    if results:
        _cs_send_side_check(checks, base, results)
    _cs_tail_checks(checks)
    return checks


def _cs_send_side_check(checks: list[dict], base: str, results: list[dict]):
    """The send-side verdict, split out so the kill-switch path and the
    probed path cannot drift into two different phrasings."""
    errs = [r for r in results if r.get("error")]
    if len(errs) == len(results):
        checks.append(_check(
            "cs_send_side",
            "every response we SEND carries cite_as + provenance", None,
            "UNMEASURED — every probe failed to reach the MCP surface at "
            f"{base}: "
            + "; ".join(f"{r['tool']}: {r['error']}" for r in errs)
            + ". A transport failure is NOT evidence the envelope is empty",
            critical=True))
    else:
        good = [r for r in results
                if not r.get("error") and r["envelope_citation"]
                and r["envelope_provenance"]]
        detail = "; ".join(
            (f"{r['tool']}: PROBE FAILED ({r['error']})" if r.get("error")
             else f"{r['tool']}: citation={r['envelope_citation']} "
                  f"provenance={r['envelope_provenance']} "
                  f"cite_as_anywhere={r['cite_as_anywhere']}")
            for r in results)
        checks.append(_check(
            "cs_send_side",
            "every response we SEND carries cite_as + provenance",
            len(good) == len(results),
            f"{len(good)} of {len(results)} probed surface(s) carry BOTH a "
            f"top-level citation (with cite_as) and a provenance block. "
            f"{detail}. Probed live at {base}, "
            f"{_CS_PROBE_TIMEOUT_S}s timeout, tagged "
            "platform=dchub-adoption-probe so it is excluded from lane 5's "
            "population by the canonical predicates. ★ THIS IS A REAL "
            "PRECONDITION, NOT THE ANSWER: if the attribution is not in the "
            "envelope it certainly never reaches the human. Envelope-level is "
            "the pass condition because that is what an agent composing an "
            "answer reads; cite_as buried inside a step result is reported "
            "but does not count — an agent is not required to walk the tree",
            critical=True))


def _cs_tail_checks(checks: list[dict]):
    """The four checks that hold regardless of whether the probe ran: they are
    statements about the ARCHITECTURE, not about today's measurement."""
    # ── 3. the workflow surface, deliberately NOT probed here ──
    checks.append(_check(
        "cs_workflow_surface",
        "the execute_plan envelope's attribution is verified", None,
        "UNMEASURED IN THIS TICK BY DESIGN — an execute_plan probe runs ~40s "
        "against a 15s Cloudflare admin route timeout, and a 503 from this "
        "origin is read by the edge worker as a dead origin and fails the "
        "whole site over to stale Render. The workflow envelope is the one "
        "that matters most for this lane and it needs an OUT-OF-BAND probe "
        "(a job that runs it and stores the result), not a synchronous call "
        "from an admin board. Measured manually 2026-08-12: the execute_plan "
        "envelope carried NO top-level citation and NO provenance block; "
        "cite_as appeared only nested inside a step result"))

    # ── 4. what is NOT observable ──
    checks.append(_check(
        "cs_boundary", "citation survival to the human is measurable", None,
        "NOT OBSERVABLE, and this is a statement about the architecture, not "
        "a gap in our logging. What the agent renders to its human happens "
        "entirely INSIDE the client, after the MCP response leaves us. We see "
        "the request and we compose the response; we never see the turn the "
        "human reads, whether our cite_as survived the model's summarisation, "
        "or whether the human saw a link. No server-side instrument can reach "
        "across that boundary. ★ Nothing in this lane may be read as a "
        "citation-survival rate, and PASS is unreachable here BY "
        "CONSTRUCTION: this check is critical and permanently indeterminate, "
        "so the lane can render FAIL or '?' and never green. A green "
        "citation-survival lane would be a claim we cannot support",
        critical=True))

    # ── 5. retrospective measurement is impossible too ──
    checks.append(_check(
        "cs_retrospective",
        "the send side is verifiable over the historical window", None,
        "UNMEASURED — no response BODY is persisted anywhere. mcp_tool_calls "
        "and mcp_call_log store request params, status and timing; "
        "recipe_executions stores per-step status counts. None stores what we "
        "actually returned. So even the send-side claim above holds only for "
        "the instant it was probed, and cannot be extended over the rolling "
        f"{WINDOW_DAYS}d window. A percentage of historical responses that "
        "carried cite_as is NOT computable today"))

    # ── 6. what WOULD settle it (named, deliberately not built here) ──
    checks.append(_check(
        "cs_instrumentation", "the instrument that would settle it exists",
        None,
        "NOT BUILT — named so the next person does not re-derive it. Three "
        "instruments, weakest to strongest: (1) DISTINGUISHABLE CITATION URL "
        "— emit cite_url with a per-response opaque path (e.g. "
        "dchub.cloud/c/<token>) and count fetches; a hit proves a human or "
        "their client followed OUR citation, and the token ties it back to "
        "the workflow that emitted it. Measures link-follows, which is a "
        "LOWER BOUND on citation: a human who reads the attribution without "
        "clicking is invisible. (2) REFERER ON A FACILITY/MARKET PAGE after a "
        "workflow — near-worthless as built: mcp_call_log.referrer is 7,967 "
        "of 7,983 self-traffic from https://dchub.cloud, and agent clients "
        "send no referer at all. It would need the token from (1) to be "
        "worth anything. (3) SEND-SIDE COMPLETENESS OVER TIME — persist a "
        "boolean per response (did this envelope carry cite_as + provenance) "
        "rather than the body, which makes cs_retrospective measurable at "
        "negligible storage cost and is the CHEAPEST of the three. NONE of "
        "these measures what the human actually saw. That stays unobservable "
        "even after all three ship — (1) is the closest available proxy and "
        "must always be labelled as a proxy"))


def _citation_survival_block(lane_checks: list[dict]) -> dict:
    """Machine-readable, derived from the SAME checks the board renders."""
    by = {k["id"]: k for k in lane_checks}
    return {
        "verdict_ceiling": ("FAIL or '?' — PASS is unreachable by "
                            "construction. cs_boundary is critical and "
                            "permanently indeterminate"),
        "what_is_observable": (by.get("cs_send_side") or {}).get("detail"),
        "what_is_not_observable": (by.get("cs_boundary") or {}).get("detail"),
        "not_retrospective": (by.get("cs_retrospective") or {}).get("detail"),
        "workflow_surface": (by.get("cs_workflow_surface") or {}).get("detail"),
        "instrumentation_needed": (
            (by.get("cs_instrumentation") or {}).get("detail")),
        "sources_read": list(_CS_SOURCES),
        "sources_refused": list(_CS_FORBIDDEN),
        "sources_refused_why": ("our own probes of PUBLIC LLM answers — a "
                                "different population, measured a different "
                                "way, about traffic that never touched MCP"),
    }


# ── tick ──────────────────────────────────────────────────────────────

def _canon() -> dict:
    """Canonical counts are FETCHED, never restated. A number typed into this
    file would rot on its own schedule."""
    out = {"source": "canonical_stats.get_canonical_stats() (live)"}
    try:
        from canonical_stats import get_canonical_stats
        out.update(get_canonical_stats() or {})
    except Exception as e:  # noqa: BLE001
        out["error"] = f"UNMEASURED: {type(e).__name__}"
    try:
        from routes.problem_taxonomy import TAXONOMY_VERSION, contract_hash
        out["taxonomy_version"] = TAXONOMY_VERSION
        out["taxonomy_contract"] = contract_hash()
    except Exception as e:  # noqa: BLE001
        out["taxonomy_error"] = f"UNMEASURED: {type(e).__name__}"
    return out


def _run_tick() -> dict:
    c = _conn()
    try:
        lanes = [
            {"id": "identity_durability",
             "name": "1 · identity durability (OAuth vs free cross-week)",
             "work_order": ("make durable identity the default connect path; "
                            "the lane is red on COMPOSITION of returners"),
             "checks": _safe_lane(_lane_identity_durability, c)},
            {"id": "activation",
             "name": "2 · activation (mint → first call)",
             "work_order": ("stamp first_call_at at first use (Task 2); the "
                            "cliff is UNMEASURED until then, never modelled"),
             "checks": _safe_lane(_lane_activation, c)},
            {"id": "conversion",
             "name": "3 · conversion (machine vs human, kept apart)",
             "work_order": ("deliver the human artifact to a human AND land "
                            "the in-turn machine rail; token re-split is "
                            "CLOSED (fixed 2026-07-30)"),
             "checks": _safe_lane(_lane_conversion, c)},
            {"id": "questions_retired",
             "name": "4 · questions retired (customer value)",
             "work_order": ("raise one-workflow closure per canonical "
                            "problem; partial answers do not retire a "
                            "question"),
             "checks": _safe_lane(_lane_questions_retired, c)},
            {"id": "lookup_vs_workflow",
             "name": "5 · lookup vs workflow (are agents solving problems?)",
             "work_order": ("route the front door in the platforms that miss "
                            "it; the lane is red on the PER-PLATFORM shape, "
                            "not on a global percentage one integration can "
                            "carry"),
             "checks": _safe_lane(_lane_lookup_vs_workflow, c)},
            {"id": "citation_survival",
             "name": "6 · citation survival (UNMEASURED by architecture)",
             "work_order": ("first put cite_as + provenance in every envelope "
                            "we SEND — that is a real precondition and it is "
                            "failing. What the human RENDERS stays "
                            "unobservable; see cs_instrumentation"),
             "checks": _safe_lane(_lane_citation_survival, c)},
        ]
    finally:
        if c is not None:
            try:
                c.close()
            except Exception:
                pass

    for ln in lanes:
        ln["verdict"] = _lane_verdict(ln["checks"])
    summary = " ".join(f"{ln['id']}={ln['verdict']}" for ln in lanes)
    qr = next((ln for ln in lanes if ln["id"] == "questions_retired"), None)
    lw = next((ln for ln in lanes if ln["id"] == "lookup_vs_workflow"), None)
    cs = next((ln for ln in lanes if ln["id"] == "citation_survival"), None)
    return {
        "ok": True,
        "shell": SHELL_ID,
        "generated_at": datetime.datetime.now(
            datetime.timezone.utc).isoformat(),
        "window": {"kind": "rolling", "days": WINDOW_DAYS,
                   "maturity_days": MATURITY_DAYS, "note": _WINDOW_NOTE},
        "verdict_values": ["PASS", "FAIL", "?"],
        "verdict_note": ("'?' is UNMEASURED — never read as fine and never as "
                         "broken. A lane whose checks were all indeterminate "
                         "renders '?', never PASS."),
        # ★ DESIGN INTENT vs MEASURED OUTCOME, kept apart (2026-08-12, first
        # live tick). The first cut declared questions_retired born red too —
        # then it measured PASS (4 of 4 measurable canonical problems close a
        # majority of their runs in one workflow). A board that keeps asserting
        # a colour its own data contradicts is the same disease as a guard that
        # cannot fail, pointing the other way. So the expectation is published
        # as an expectation, and the verdicts stay where they are measured.
        "red_by_design": ["identity_durability", "conversion",
                          "lookup_vs_workflow", "citation_survival"],
        "red_by_design_note": (
            "a red-by-design lane is a WORK ORDER, not a defect: neither can "
            "be turned green by a cosmetic or copy change — identity moves on "
            "the COMPOSITION of returners, conversion on a path actually "
            "paying. questions_retired was ALSO expected red and measured "
            "otherwise; the expectation was wrong and is recorded as such "
            "rather than forced onto the data. Lanes 5 and 6 (2026-08-12) are "
            "born red on the same terms: lane 5 moves only when platforms "
            "that today run pure lookups actually route through the front "
            "door, and lane 6 cannot render PASS AT ALL — citation survival "
            "is unobservable from the server, so its ceiling is '?' and its "
            "current FAIL is the SEND-SIDE precondition, which is fixable."),
        "red_now": [ln["id"] for ln in lanes if ln["verdict"] == "FAIL"],
        "unmeasured_now": [ln["id"] for ln in lanes if ln["verdict"] == "?"],
        "canon": _canon(),
        "lanes": lanes,
        "questions_retired": _questions_retired_block(
            (qr or {}).get("checks") or []),
        "lookup_vs_workflow": _lookup_vs_workflow_block(
            (lw or {}).get("checks") or []),
        "citation_survival": _citation_survival_block(
            (cs or {}).get("checks") or []),
        "summary": summary,
        "any_fail": any(ln["verdict"] == "FAIL" for ln in lanes),
        "any_unmeasured": any(ln["verdict"] == "?" for ln in lanes),
    }


def _no_store(resp):
    # CF Rule #3 caches /api/v1/* with mode override_origin, which IGNORES
    # no-store — the alias below exists because of it. Set the header anyway:
    # it is what every non-CF reader honours.
    resp.headers["Cache-Control"] = "no-store"
    return resp


@adoption_master_shell_bp.route("/api/v1/admin/adoption/master-tick",
                                methods=["GET", "POST"])
@adoption_master_shell_bp.route("/api/v1/admin/adoption", methods=["GET"])
def master_tick():
    if _disabled():
        # ★404, never 5xx: the CF worker's proxyWithRetry reads ANY 5xx from
        # Railway as a dead origin and fails the SITE over to stale Render.
        # Turning off one diagnostic board must not be able to do that.
        return jsonify(ok=False, error="ADOPTION_SHELL_DISABLE=1"), 404
    if not _admin_ok():
        return jsonify(ok=False, error="admin key required"), 401
    return _no_store(jsonify(_run_tick()))


@adoption_master_shell_bp.route("/admin/adoption", methods=["GET"])
def dashboard():
    if _disabled():
        return Response("adoption shell disabled", status=404,
                        mimetype="text/plain")
    if not _admin_ok():
        return Response("admin key required (?admin_key=)", status=401,
                        mimetype="text/plain")
    d = _run_tick()
    color = {"PASS": "#22c55e", "FAIL": "#ef4444", "?": "#eab308"}
    rows = []
    for ln in d["lanes"]:
        rows.append(
            f"<tr><td class='lane'>{_esc(ln['name'])}<br>"
            f"<span class='wo'>work order: {_esc(ln['work_order'])}</span></td>"
            f"<td style='color:{color.get(ln['verdict'], '#eab308')}'>"
            f"<b>{_esc(ln['verdict'])}</b></td><td>"
            + "<br>".join(
                ("&#9989; " if k["pass"] is True else
                 ("&#10060; " if k["pass"] is False else "&#10068; "))
                + _esc(k["name"]) + " — <span class='d'>" + _esc(k["detail"])
                + "</span>" for k in ln["checks"])
            + "</td></tr>")
    html = (
        "<!doctype html><meta charset='utf-8'>"
        "<meta http-equiv='refresh' content='60'>"
        "<title>Adoption Master Shell #52</title>"
        "<style>body{background:#0b1020;color:#e2e8f0;font:14px/1.5 "
        "-apple-system,Segoe UI,sans-serif;margin:2rem}table{border-collapse:"
        "collapse;width:100%;max-width:1180px}td{border-bottom:1px solid "
        "#1e293b;padding:.6rem .8rem;vertical-align:top}.lane{max-width:22rem;"
        "font-weight:600}.d{color:#94a3b8;font-weight:400}.wo{color:#64748b;"
        "font-weight:400;font-size:12px}h1{font-size:1.2rem}small{color:"
        "#64748b}</style>"
        "<h1>Adoption Master Shell #52</h1>"
        "<small>generated " + _esc(d["generated_at"]) + " · read-only · "
        "refreshes 60s · " + _esc(d["window"]["note"]) + " · &#10068; = "
        "UNMEASURED (neither fine nor broken) · red by design: "
        + _esc(", ".join(d["red_by_design"]))
        + " · kill ADOPTION_SHELL_DISABLE=1</small>"
        "<table>" + "".join(rows) + "</table>")
    return _no_store(Response(html, mimetype="text/html"))


def register_adoption_master_shell(app):
    app.register_blueprint(adoption_master_shell_bp)
