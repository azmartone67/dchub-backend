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
     ★ NOT A TOKEN PROBLEM. The relay token race was fixed 2026-07-30 (agent
     token + high_intent_human_url split; human_acted redefined onto the human
     link open stamp). This board reads the POST-fix instrument. Do not propose
     re-splitting the token — that question is closed.
  4. QUESTIONS RETIRED — per canonical problem, how many tools a workflow
     burns and whether ONE workflow CLOSED the question (complete / partial /
     failed). This is the only lane that measures customer value rather than
     usage volume: a question answered in one call is worth more than ten calls
     that answer none, and call-count reporting cannot tell those apart.

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

    gate = _row(c, f"""
        SELECT COUNT(*) FILTER (
                 WHERE last_hit_at >= now() - interval '{WINDOW_DAYS} days'),
               COUNT(*) FILTER (
                 WHERE claim_minted_at >= now() - interval '{WINDOW_DAYS} days'),
               COUNT(*) FILTER (
                 WHERE human_view_first_opened_at
                       >= now() - interval '{WINDOW_DAYS} days'),
               COUNT(*) FILTER (
                 WHERE claim_email IS NOT NULL AND claim_email <> ''
                   AND last_hit_at >= now() - interval '{WINDOW_DAYS} days'),
               COUNT(*) FILTER (
                 WHERE claim_minted_at >= now() - interval '{WINDOW_DAYS} days'
                   AND human_view_first_opened_at IS NULL
                   AND (claim_email IS NULL OR claim_email = ''))
          FROM mcp_high_intent_sessions
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
            "human_acted is definition v2 — the human-link first-open stamp, "
            "post the 2026-07-30 two-artifact fix, so this is measuring the "
            f"REPAIRED instrument. basis: mcp_high_intent_sessions, rolling "
            f"{WINDOW_DAYS}d"))

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
            "REPORTED SEPARATELY from the machine path on purpose. The root "
            "cause here is DELIVERY, not appeal — the offer has never reached "
            "a person — and the relay token race was already fixed 2026-07-30; "
            "DO NOT re-split the token. WORK ORDER: get the human artifact in "
            "front of a human. basis: email join, rolling window",
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
        "red_by_design": ["identity_durability", "conversion"],
        "red_by_design_note": (
            "a red-by-design lane is a WORK ORDER, not a defect: neither can "
            "be turned green by a cosmetic or copy change — identity moves on "
            "the COMPOSITION of returners, conversion on a path actually "
            "paying. questions_retired was ALSO expected red and measured "
            "otherwise; the expectation was wrong and is recorded as such "
            "rather than forced onto the data."),
        "red_now": [ln["id"] for ln in lanes if ln["verdict"] == "FAIL"],
        "unmeasured_now": [ln["id"] for ln in lanes if ln["verdict"] == "?"],
        "canon": _canon(),
        "lanes": lanes,
        "questions_retired": _questions_retired_block(
            (qr or {}).get("checks") or []),
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
