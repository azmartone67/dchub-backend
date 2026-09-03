"""DC Hub — AGENTIC LOOP master shell (#65, 2026-08-22).

★ WHY THIS SHELL EXISTS

Owner decision 2026-08-22, after the claim-loop build: "ok lets do them 1-4
master shell." The four items, verbatim from the assessment the owner approved:

  1. Graduate action classes on TRACK RECORD, not prompts — autonomy grows by
     earned vocabulary.
  2. Clear the human queues or delegate them to a class — platform updates
     pending, inbox rows awaiting a decision, strategic recs unacted: where the
     brain's output dies today.
  3. Close the learn station — negative results (refuted/retracted claims,
     rejected proposals, failed fixes) become what the planner and lane driver
     RECALL.
  4. Detectors-with-the-fix stays a merge rule (shipped as #3054) — MEASURE it,
     do not re-build it.

Each item is a mechanism built elsewhere (part B: routes/squasher_action_classes
+ routes/squasher_queue; part C: routes/brain_rag + the planner). This shell is
the scoreboard over all four, in the house pattern of #55 (report-only lanes)
and #64 (beats, scheduled). It is the compounding metric's one home:

    claims confirmed · refuted-and-kept · retracted · granted classes ·
    recurrence rate (with a 7-day delta)

★ EACH LANE PINS AN INVARIANT, NEVER A VALUE (cf. contract healer #44). "Oldest
decision is 3 days old" is not the invariant; "no decision waits past a DECLARED
ceiling" is — and the ceiling is a working-practice bound written here, not a
number read off today's queue.

★ THIS SHELL IS BORN RED, and that is correct (cf. #45 BORN RED). On the day it
shipped there was no candidate class registry, the platform feed published
withheld entries with neither an age nor a decision URL, no digest named the
stale strategic recs, and the learn-station corpus did not exist. A green lane
on day one would mean the invariant was written to fit the defect.

★ "?" IS A REAL VERDICT, NOT A SOFT PASS. Parts B and C land in their own PRs;
until they do, the checks that need `graduation_report()`, `queue_ages()`,
`recall_negative_lessons()` or the `claim_lessons` corpus read `?` — unverified,
never assumed fine. Every sibling import is lazy and every failed read is `?`.
A lane whose every read failed must not render PASS (tested).

★ THE READ IS BOUNDED. worker.js gives this prefix ROUTE_TIMEOUTS.DEFAULT =
15s and retries /api/v1/* GETs that time out, and a 5xx from Railway fails the
whole site over to stale Render. The first cut of this shell measured 77.9s
against live Neon (a 35-55s email render on a read path). So every read carries
a wall-clock deadline (READ_BUDGET_S) and renders `?` for the lanes it did not
reach — a board written to catch outages must not become one.

★ THE GET NEVER ACTS AND NEVER BEATS. The JSON read and the HTML board are pure
reports. Only the scheduled POST tick writes, and only two things:
  * its own ledger row (agentic_loop_shell_ledger — the daily metric snapshot
    that gives recurrence_rate its 7-day delta, and the filing budget), and
  * the dead-man beat.
Under AGENTIC_LOOP_ARM=1 the tick may ALSO call part B's graduation_report()
filing — which files "Grant class X?" decision rows into the inbox — bounded to
FILE_CAP_PER_DAY rows/day by the same ledger. Nothing else acts. A grant itself
is always a human POST to /api/v1/brain/squasher/grant; this shell cannot grant.

★ graduation_report() FILES ROWS BY CONTRACT (part B). A report-only read may
not call a function that writes, so the read path calls it ONLY when its
signature exposes a dry-run/filing parameter it can set to "do not file"; when
it exposes none, the read refuses and renders `?` with the reason, and only the
armed tick calls it. Guessing that a writer is harmless is how a GET acts.

Lanes
  1 graduation          granted classes pass grant_allowed (critical); no tripped
                        class executed in 7d (critical); graduation_report()
                        readable; ≥1 candidate row; eligible candidates have an
                        open decision row (not silently waiting)
  2 human_queues        queue_ages() readable; oldest awaiting_decision under the
                        declared ceiling; platform pending+withheld carry an age
                        and a decision URL; stale strategic recs can be reached by
                        the digest that mails them (decided from its SELECTION
                        WINDOW, never by re-rendering a 35-55s email on a read
                        path) and the decision digest's last run is green;
                        collapse ratio published
  3 learn               claim_lessons corpus registered, NOT public, and embedded
                        within one reindex cycle of its newest row (critical once
                        a refuted claim exists); recall_negative_lessons()
                        self-test (critical once one exists); planner prompt names
                        the section when lessons exist; effect bandit non-empty
                        once the sample floor is met
  4 detectors_with_fix  brain_prs_with_detector readable this week; the three
                        product detectors are in scan_all()'s tuple (AST, via the
                        shared rule — never re-implemented here) and have fired or
                        read `measuring`; recurrence_rate published with a 7-day
                        delta (published, not judged)

Routes (registered via agentic_loop_master_shell_bp in main.py, own try/except):
  GET  /admin/agentic-loop                      HTML board (admin key)
  GET  /api/v1/brain/agentic-loop               JSON verdicts + metric row +
                                                "decide today" (admin key) —
                                                never acts, never beats
  POST /api/v1/brain/agentic-loop/master-tick   the scheduled tick (admin key):
                                                REPORT-ONLY; snapshot + beat;
                                                filing only under AGENTIC_LOOP_ARM=1
  The /api/v1/brain/ prefix carries the Cloudflare bypass — /api/v1/admin/* GETs
  are edge-cached 17–42 min, which would freeze a board whose whole claim is
  that its verdicts are re-derived now.

Kill: AGENTIC_LOOP_SHELL_DISABLE=1
  ★ returns 404, never 503 — the CF worker reads any 5xx from Railway as a dead
  origin and fails the whole site over to stale Render. See
  tests/test_shell_killswitch_never_5xx.py.
Beat: agentic-loop-shell-daily · cadence 24h · ONE writer (this file's tick) ·
  idle beat rows_inserted=1. Three statuses, and the difference matters to OTHER
  monitors: `success` (all lanes green) · `lanes_failing` (the tick RAN and
  measured red lanes — the normal state of a BORN RED board) · `error` (the tick
  itself failed, i.e. a genuinely broken producer). ingestion-integrity-tick's
  producer_liveness lane asserts "no producer is reporting status=error", so
  beating `error` for a red board makes a working shell read as a crashed one and
  fails that workflow. lanes_failing is still non-success and still marks this
  feed overdue on /api/v1/ops/deadman — nothing is softened.
Schedule: routes/cron_heartbeat.py _DISPATCH `agentic_loop_shell_daily`
  (11:xx UTC, POST through _hit() which attaches X-Admin-Key) — the same
  mechanism that drives surface-truth (08:xx) and relay-closure (09:xx).
  Registration is not scheduling (tests/test_shell_scheduler_coverage.py).
"""
from __future__ import annotations

import ast
import datetime as _dt
import inspect
import json
import logging
import os
import time
from html import escape as _esc

import requests
from flask import Blueprint, jsonify, request

logger = logging.getLogger(__name__)
agentic_loop_master_shell_bp = Blueprint("agentic_loop_master_shell", __name__)

SHELL_NUMBER = 65
FEED = "agentic-loop-shell-daily"
CADENCE_HOURS = 24

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_REPO = "azmartone67/dchub-backend"
_TIMEOUT = 6            # per GitHub read; the whole tick must stay well inside CF's window

# ── declared bounds — WORKING-PRACTICE targets, never measurements ─────────
# A decision that has waited a week is not "pending", it is dropped. The
# needs-decision digest uses 14d for issues; the inbox is the brain's own
# hand-off and gets the tighter bound.
DECISION_AGE_CEILING_DAYS = 7
# A strategic rec still `new` after two weeks has left every planner's horizon.
REC_STALE_DAYS = 14
# brain_rag_reindex_4h runs at :20 every 4h; a lesson newer than one cycle
# plus this grace is not yet DUE to be embedded — unverified, not failing.
REINDEX_CYCLE_HOURS = 4
REINDEX_GRACE_HOURS = 1
# ── the shell's own wall-clock budget ─────────────────────────────────────
# ★ worker.js ROUTE_TIMEOUTS gives /api/v1/brain/… and /admin/… the DEFAULT of
#   15_000 ms, and /api/v1/* GETs are in RETRYABLE_PREFIXES — so a read that
#   overruns is not merely slow: it is RETRIED (double the load on an already
#   struggling origin) and then answered 503, and the worker reads any 5xx from
#   Railway as a dead origin and fails the whole site over to stale Render.
#   A board written to catch that class must not become it. So the read carries
#   a deadline well inside the window and renders `?` for the lanes it did not
#   reach — "I ran out of budget" is a real verdict; a 503 is not.
#   The scheduled tick does NOT cross the edge (cron_heartbeat's BASE is the
#   loopback on Railway) and gets a budget inside _hit()'s own 30s timeout.
#   ★ HONEST LIMIT: the deadline governs when new work may START; it cannot
#   abort a call already in flight, so it is a bound on the overrun, not a hard
#   cap. Per-call timeouts (_TIMEOUT for GitHub, connect_timeout on the DB) are
#   what cap a single read.
READ_BUDGET_S = 11
TICK_BUDGET_S = 25
# Budget a heavy read refuses to start under (it would overrun, not finish).
#
# ★ A PRE-GATE LOOSER THAN _q()'s OWN REFUSAL IS A GATE THAT NEVER FIRES
#   (2026-08-23). _DETECTOR_MIN_S used to sit BELOW _QUERY_MIN_S, so for every
#   budget in the band between them the detector pre-gate PASSED and _q() then
#   refused the read — and the check published
#
#       d_fired_check_stored_slug_resolves  ?  brain_findings unreadable
#
#   naming a table that was never touched. Measured on prod that morning:
#   tick_ms=9398 against READ_BUDGET_S, so lane 4 reached these reads with
#   ~1.6s left — inside the dead band, on every tick. All three product
#   detectors blamed a table for a spent budget, and the one honest message
#   ("the shell's budget was spent") could not fire at all.
#
#   Costing a wrong diagnosis is worse than costing nothing. The invariant is
#   asserted below, once both constants exist.
_BANDIT_MIN_S = 3.0
_DETECTOR_MIN_S = 3.0
# ★ AND THE SAME GATE FOR THE ONE READ THAT IS NOT A QUERY (2026-08-23).
#   _q() composes with the budget because it refuses to START under
#   _QUERY_MIN_S; _gh() had NO pre-gate at all, and it is the single most
#   expensive call in the tick: _TIMEOUT = 6s, more than twice statement_timeout.
#   So a GitHub read could start at deadline-e and run six seconds past it —
#   READ_BUDGET_S + _TIMEOUT = 17s against worker.js's 15s. Measured on prod
#   that day: tick_ms=16102 on the first hit after an idle gap, and 8665-10141ms
#   warm, i.e. warm ticks already sat within ONE GitHub timeout of the edge. A
#   slow (not failing) api.github.com was enough to 503 the route — and a 5xx
#   from Railway fails the whole site over to the stale Render mirror.
#   Gated at its own timeout, a GitHub read that STARTS still lands inside the
#   deadline, exactly as _QUERY_MIN_S == _STATEMENT_TIMEOUT_S does for the DB.
_GH_MIN_S = float(_TIMEOUT)
# ★ THE DB BOUNDS, and the arithmetic that makes READ_BUDGET_S a real ceiling
#   rather than a wish. A hanging (not refusing) Neon is the shape that hurts:
#   nothing raises, so nothing renders `?`, and the read just… keeps going.
#     · _CONNECT_TIMEOUT_S caps the ONE connect this tick makes, and it is
#       charged against the budget (the deadline clock starts before it).
#     · _STATEMENT_TIMEOUT_S is armed per read, as SET LOCAL inside that read's
#       own transaction, so the SERVER kills a wedged query. Python cannot abort
#       a psycopg2 call in flight; Postgres can. (SET LOCAL, not a connect-time
#       option and not a session SET: on Neon's pooled endpoint pgbouncer
#       rejects the first and routes the second to a different backend than the
#       query — flask_mcp_endpoints._reach_bounded, verified live 2026-07-01.)
#     · _QUERY_MIN_S == _STATEMENT_TIMEOUT_S is why the two compose: _q refuses
#       to START a read with less than one statement_timeout of budget left, so
#       any read it DOES start still finishes inside the deadline.
#   Worst case therefore stays inside READ_BUDGET_S = 11s, which sits inside
#   worker.js's 15s — instead of the 16 x 8s = ~128s the fallback used to allow.
_CONNECT_TIMEOUT_S = 5
_STATEMENT_TIMEOUT_S = 3.0
_QUERY_MIN_S = 3.0
# ★ The invariant that keeps the pre-gates honest, asserted at import rather
# than left as a comment: a gate that admits a read _q() will refuse does not
# save the budget, it only relabels the refusal as a failure of whatever the
# read was about. Lowering either constant without the other reintroduces the
# dead band that made three detectors blame brain_findings for a spent budget.
assert _DETECTOR_MIN_S >= _QUERY_MIN_S, (
    "_DETECTOR_MIN_S must be >= _QUERY_MIN_S: a pre-gate looser than _q()'s own "
    "refusal lets a read through only to be refused, and the check then reports "
    "the wrong cause")
assert _BANDIT_MIN_S >= _QUERY_MIN_S, (
    "_BANDIT_MIN_S must be >= _QUERY_MIN_S for the same reason")
# ★ The GitHub read is bounded by ITS OWN timeout, not by statement_timeout, so
#   its pre-gate is pinned to _TIMEOUT rather than _QUERY_MIN_S. Lowering it
#   below _TIMEOUT re-opens the 17s worst case that overruns worker.js's 15s.
assert _GH_MIN_S >= _TIMEOUT, (
    "_GH_MIN_S must be >= _TIMEOUT: a GitHub read admitted with less budget "
    "left than its own timeout can outlive the deadline by the difference, and "
    "READ_BUDGET_S stops being a ceiling")
assert READ_BUDGET_S + _STATEMENT_TIMEOUT_S <= 15, (
    "READ_BUDGET_S plus the longest read that may still be IN FLIGHT at the "
    "deadline must stay inside worker.js ROUTE_TIMEOUTS.DEFAULT (15s)")

# ── WHAT THE TICK ACTUALLY SPENT, AND ON WHAT ────────────────────────────
#
# ★ THE BUDGET GOVERNED 12 OF 103 ROUND TRIPS. Measured 2026-08-23 against
#   prod Neon: ONE tick opened TWENTY-NINE database connections and made 103
#   statement round trips. _q() — the read this shell bounds so carefully that
#   its docstring outruns its body — was ONE of those connections and twelve of
#   those round trips. The other twenty-eight connections were opened inside
#   the siblings this shell calls through _call(): each has its own _conn(),
#   and none of them knows this deadline exists.
#
#   "THIS FUNCTION NEVER OPENS A CONNECTION" is scrupulously true of _q() and
#   was almost beside the point: a deadline over 12% of the spend is a deadline
#   over the part that was already cheap. Twenty-one of those connections came
#   from ONE sibling loop reading the same six numbers four times over.
#
#   So the spend is now MEASURED and PUBLISHED per read, under budget.spent.
#   This block is the METER, not the fix — its whole job is that the next
#   person to ask where the tick goes reads the answer instead of guessing it,
#   which is how a wrong hypothesis ("~25-30 _q round trips") survived a day.
_SPEND_CAP = 120        # a ledger that grows without bound becomes the cost it measures


def _spent(ctx, kind: str, what: str, ms: float) -> None:
    """Record one read's wall cost on this tick's ledger. NEVER raises: a meter
    that can break the thing it measures is worse than no meter."""
    try:
        if ctx is None:
            return
        led = ctx.setdefault("spend", [])
        if len(led) < _SPEND_CAP:
            led.append({"ms": round(float(ms), 1), "kind": kind, "what": str(what)[:80]})
        else:
            ctx["spend_truncated"] = True
    except Exception:  # noqa: BLE001
        pass


def _spend_report(ctx: dict, tick_ms: int) -> dict:
    """The tick's own cost, attributed. Sibling rows are WALL time and include
    whatever DB work that sibling did on its OWN connection — which is the
    point: it is the only place that cost is visible at all."""
    led = list(ctx.get("spend") or [])
    by: dict = {}
    for e in led:
        b = by.setdefault(e["kind"], {"calls": 0, "ms": 0.0})
        b["calls"] += 1
        b["ms"] = round(b["ms"] + e["ms"], 1)
    measured = round(sum(e["ms"] for e in led), 1)
    return {
        "tick_ms": tick_ms,
        "measured_ms": measured,
        "unmeasured_ms": round(max(0.0, tick_ms - measured), 1),
        "by_kind": by,
        "top": sorted(led, key=lambda e: -e["ms"])[:8],
        "truncated": bool(ctx.get("spend_truncated")),
        "why": (
            "sibling = one call through _call() into part B/C, WALL time, and it "
            "includes DB work that sibling did on a connection of its own that this "
            "shell's deadline never saw; db_read = _q()/_bounded() on THIS tick's "
            "connection, the only reads READ_BUDGET_S actually governs; connect = "
            "this tick's own psycopg2.connect. unmeasured_ms is Python: AST parses, "
            "repo file reads, formatting. db_refused counts reads NOT DONE — "
            "refused inside _q()/_bounded(), or skipped by a pre-gate that never "
            "reaches them — because what a budget costs is the reads it stopped."),
    }


# Armed filing budget: decision rows the tick may file per UTC day.
FILE_CAP_PER_DAY = 3
LEDGER_TABLE = "agentic_loop_shell_ledger"
# Part C's corpus name (brief 2026-08-22): brain_predictions_log WHERE outcome
# IN ('refuted','retracted'). Must be in LESSON_CORPORA, NEVER in PUBLIC_CORPORA.
CLAIM_LESSON_CORPUS = "claim_lessons"
# The three product detectors #3054 registered in brain_consistency_radar
# .scan_all()'s tuple, with the `issue` key each one files under (the findings
# writer stamps detector='consistency_radar' on every radar finding, so the
# issue text is the only thing that identifies WHICH detector fired).
PRODUCT_DETECTORS = {
    "check_measurement_definition_changed": "measurement_definition_changed",
    "check_stored_slug_resolves": "stored_slug_404",
    "check_funnel_adjacent_step_collapse": "funnel_step_collapse",
}
# The decision digest: sweeps for decisions nobody owns, weekly, and goes RED
# BY DESIGN when one has waited >14d (delivery is the failing job).
DIGEST_WORKFLOW = "needs-decision-digest.yml"
DIGEST_MAX_AGE_DAYS = 8


def _disabled() -> bool:
    return (os.environ.get("AGENTIC_LOOP_SHELL_DISABLE") or "").strip() == "1"


def _armed() -> bool:
    return (os.environ.get("AGENTIC_LOOP_ARM") or "").strip() == "1"


def _admin_ok() -> bool:
    keys = {v for n in ("DCHUB_INTERNAL_KEY", "INTERNAL_KEY", "DCHUB_ADMIN_KEY")
            for v in [os.environ.get(n)] if v}
    sent = (request.headers.get("X-Admin-Key")
            or request.headers.get("X-Internal-Key")
            or request.args.get("admin_key") or "").strip()
    return bool(sent) and sent in keys


def _check(cid: str, name: str, passed, detail: str, critical: bool = False) -> dict:
    return {"id": cid, "name": name, "pass": passed, "detail": detail,
            "critical": critical}


def _lane_verdict(checks: list) -> str:
    """FAIL on any false; `?` when nothing was actually verified.

    Same contract as #54/#55: a lane whose reads all failed must never render
    green, because "I could not measure it" is not "it is fine".
    """
    if any(k["pass"] is False for k in checks):
        return "FAIL"
    crits = [k for k in checks if k.get("critical")]
    if any(k["pass"] is None for k in crits):
        return "?"
    if any(k["pass"] is None for k in checks) and not any(k["pass"] is True for k in checks):
        return "?"
    return "PASS"


# ── plumbing ──────────────────────────────────────────────────────────────

def _gh_token() -> str:
    # ★ PR_SUBMIT_TOKEN, then GITHUB_TOKEN, then GH_TOKEN — in that order ON
    # PURPOSE (#55): a present-but-broken GH_TOKEN must not blind this shell.
    for n in ("PR_SUBMIT_TOKEN", "GITHUB_TOKEN", "GH_TOKEN"):
        v = (os.environ.get(n) or "").strip()
        if v:
            return v
    return ""


def _gh(path: str, ctx: dict | None = None):
    """ONE GitHub read, inside THIS tick's budget. (value, 'ok') or (None, why).

    ★ IT NEVER STARTS A CALL IT CANNOT FINISH IN TIME. This is _q()'s bound for
      the read that is not a query. _gh() used to consult no deadline at all,
      which made READ_BUDGET_S a ceiling for the DB and a suggestion for
      GitHub — see _GH_MIN_S.

    ★ AND IT SAYS WHICH FAILURE THIS WAS. It used to return a bare None for
      five different causes — no token, non-200, timeout, transport error,
      bad JSON — and its one caller published a single hard-coded reason for
      all of them: "no GitHub token available (prod has PR_SUBMIT_TOKEN /
      GITHUB_TOKEN)". That names a MISSING PRODUCTION CREDENTIAL for what is
      usually a slow or rate-limited api.github.com, and sends a reader to
      Railway's env vars to look for something that is already there. Same
      class as the three detectors that blamed brain_findings for a spent
      budget (#3093): a read that did not happen must name why it did not.
    """
    if ctx is not None:
        left = _budget_left(ctx)
        if left <= _GH_MIN_S:
            return None, ("the shell's budget was spent before this read "
                          "(%0.1fs left, a GitHub read needs %0.1fs) — "
                          "api.github.com was never called; unverified, NOT "
                          "assumed fine" % (left, _GH_MIN_S))
    tok = _gh_token()
    if not tok:
        return None, ("no GitHub token on this deploy (PR_SUBMIT_TOKEN / "
                      "GITHUB_TOKEN / GH_TOKEN all unset) — unverified, NOT "
                      "assumed fine")
    # requests, not urllib (urllib is blocked repo-wide — CF error 1010).
    try:
        r = requests.get(
            f"https://api.github.com{path}",
            headers={"Authorization": f"Bearer {tok}",
                     "Accept": "application/vnd.github+json",
                     "User-Agent": "dchub-agentic-loop-shell/1.0"},
            timeout=_TIMEOUT)
        if r.status_code != 200:
            logger.info("[agentic_loop] gh %s -> %s", path, r.status_code)
            # 403/429 is the rate limit, and it is the cause most likely to be
            # misread as a missing token. Name the status.
            return None, ("api.github.com answered HTTP %s (a token IS present; "
                          "403/429 is the rate limit, not a missing credential) "
                          "— unverified, NOT assumed fine" % r.status_code)
        return r.json(), "ok"
    except Exception as e:  # noqa: BLE001
        logger.info("[agentic_loop] gh %s failed: %s", path, e)
        return None, ("the GitHub read failed after %ss: %s (a token IS "
                      "present) — unverified, NOT assumed fine"
                      % (_TIMEOUT, type(e).__name__))


def _read(rel: str) -> str:
    """Read a repo file that ships with the deploy. '' on failure."""
    try:
        with open(os.path.join(_ROOT, rel), encoding="utf-8") as fh:
            return fh.read()
    except Exception:  # noqa: BLE001
        return ""


def _conn():
    """THE connection for this tick — opened ONCE, in _tick(), and nowhere else.

    None when there is no DB, or when the connect itself failed: every read then
    renders `?`. Autocommit so a failed read can never leave the next one inside
    an aborted transaction.

    ★ connect_timeout is the FIRST of the two hard bounds that keep this read
      inside worker.js's 15s window. It caps a Neon that HANGS rather than
      refuses, and it is charged against this tick's budget because _tick()
      starts the deadline clock BEFORE it calls this. The second bound is the
      per-query statement_timeout _q() arms — NOT a connect-time option: on
      Neon's POOLED endpoint pgbouncer rejects startup options at connect and a
      plain session SET lands on a different backend than the query
      (flask_mcp_endpoints._reach_bounded, verified live 2026-07-01).
    """
    import psycopg2
    db = os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL")
    if not db:
        return None
    try:
        c = psycopg2.connect(db, sslmode="require",
                             connect_timeout=_CONNECT_TIMEOUT_S)
        c.autocommit = True
    except Exception:  # noqa: BLE001
        return None
    # Belt and braces for the writes that do NOT go through _q()/_bounded():
    # the acting tick's ledger DDL and upsert. A session SET is a no-op on a
    # pooled endpoint (it lands on a different backend than the statement),
    # which is exactly why the reads arm SET LOCAL of their own — this one is
    # free and covers the rest, so it is best-effort and never fatal.
    try:
        with c.cursor() as cur:
            cur.execute("SET statement_timeout = %d" % int(_STATEMENT_TIMEOUT_S * 1000))
    except Exception:  # noqa: BLE001
        pass
    return c


def _q(sql: str, params=None, conn=None, ctx=None):
    """ONE read, on THIS tick's connection, inside THIS tick's budget.

    Returns a list of rows, or None on ANY failure — never an empty list
    masquerading as 'nothing there'.

    ★ THIS FUNCTION NEVER OPENS A CONNECTION. It used to fall back to _conn()
      whenever the one it was handed was None — and every one of the 16 call
      sites hands it `ctx["conn"]`, which is None precisely when _conn() has
      just failed. Against a HANGING (not refusing) Neon that made one read
      into sixteen fresh connect attempts: 16 x connect_timeout ≈ 128s on a
      route the CF worker gives 15s, retries, then answers 503 — and a 5xx from
      Railway is read as a dead origin and fails the whole site over to the
      stale Render mirror. The board becomes the outage it exists to detect.
      No connection is an UNREADABLE read (`?`), not a reason to dial again.

    ★ AND IT NEVER STARTS A QUERY IT CANNOT FINISH IN TIME. The deadline used
      to be consulted only between lanes and at two in-lane points; lane 2 alone
      fires four back-to-back reads with nothing checked between them. Now every
      read asks first, and refuses below _QUERY_MIN_S — which is exactly
      statement_timeout, so a query that IS started still lands inside the
      budget. That makes the read bounded on every path, not on the lucky ones.

    ★ AND POSTGRES ENFORCES THE REST. Python cannot abort a psycopg2 call in
      flight, so a deadline checked before the query is only half a bound: a
      wedged read still blocks for as long as the server allows. Each read
      therefore runs inside its own explicit transaction with SET LOCAL
      statement_timeout — the only form that sticks on Neon's POOLED endpoint
      (pgbouncer rejects startup options at connect, and a plain session SET
      lands on a different backend than the query; the pattern and the live
      2026-07-01 verification are flask_mcp_endpoints._reach_bounded's). The
      connection is autocommit, so BEGIN/COMMIT are explicit, and ROLLBACK on
      error keeps a timed-out read from poisoning the next one.
    """
    if ctx is not None:
        if _budget_left(ctx) <= _QUERY_MIN_S:
            _spent(ctx, "db_refused", " ".join(str(sql).split())[:80], 0.0)
            return None
        conn = ctx.get("conn")
    if conn is None:
        return None
    # %-formatted in PYTHON, never handed to psycopg2 with params: a literal %
    # reaching cur.execute() alongside params is the repo's documented 500.
    set_timeout = "SET LOCAL statement_timeout = %d" % int(_STATEMENT_TIMEOUT_S * 1000)
    _t0 = time.time()
    try:
        with conn.cursor() as cur:
            cur.execute("BEGIN")
            try:
                cur.execute(set_timeout)
                cur.execute(sql, params if params is not None else None)
                rows = list(cur.fetchall() or [])
                cur.execute("COMMIT")
                return rows
            except Exception:  # noqa: BLE001
                try:
                    cur.execute("ROLLBACK")
                except Exception:  # noqa: BLE001
                    pass
                raise
    except Exception as e:  # noqa: BLE001
        logger.info("[agentic_loop] read failed: %s: %s", type(e).__name__, str(e)[:120])
        return None
    finally:
        _spent(ctx, "db_read", " ".join(str(sql).split())[:80], (time.time() - _t0) * 1000)


def _bounded(ctx: dict, fn):
    """Run fn(cur) as ONE bounded read on this tick's connection.

    The same envelope _q() gives a SQL string of ours — deadline checked before,
    SET LOCAL statement_timeout during — for the read that is NOT one: part B's
    class_rows(cur). "Deadline-bounded on every path" has to mean every path,
    and a sibling's reader on our connection is a path.

    (value, 'ok') or (None, why).
    """
    if _budget_left(ctx) <= _QUERY_MIN_S:
        _spent(ctx, "db_refused", getattr(fn, "__name__", "sibling reader"), 0.0)
        return None, ("the shell's read budget was spent before this read — "
                      "unverified, NOT assumed fine")
    conn = ctx.get("conn")
    if conn is None:
        return None, "no DB connection"
    set_timeout = "SET LOCAL statement_timeout = %d" % int(_STATEMENT_TIMEOUT_S * 1000)
    _t0 = time.time()
    try:
        with conn.cursor() as cur:
            cur.execute("BEGIN")
            try:
                cur.execute(set_timeout)
                value = fn(cur)
                cur.execute("COMMIT")
                return value, "ok"
            except Exception:  # noqa: BLE001
                try:
                    cur.execute("ROLLBACK")
                except Exception:  # noqa: BLE001
                    pass
                raise
    except Exception as e:  # noqa: BLE001
        return None, f"{type(e).__name__}: {str(e)[:100]}"
    finally:
        _spent(ctx, "db_read", "bounded: %s" % getattr(fn, "__name__", "sibling reader"),
               (time.time() - _t0) * 1000)


def _module(name: str):
    """Lazy sibling module. None when absent — parts B and C land in their
    own PRs, and a missing mechanism is `?`, never PASS."""
    try:
        import importlib
        return importlib.import_module(name)
    except Exception:  # noqa: BLE001
        return None


def _import_attr(module: str, name: str):
    """Lazy sibling callable. None when the module OR the attribute is absent."""
    m = _module(module)
    fn = getattr(m, name, None) if m is not None else None
    return fn if callable(fn) else None


def _call(fn, *a, **kw):
    """(value, 'ok') or (None, 'why') — a raising read is an unreadable read.

    ★ Every sibling read this shell makes comes through here, which makes it
      the ONE place their cost is visible. They open their own connections on
      their own schedule; `ctx` is passed only so the meter has somewhere to
      write, and is never forwarded to the sibling."""
    ctx = kw.pop("_ctx", None)
    if fn is None:
        return None, "unavailable"
    _t0 = time.time()
    try:
        return fn(*a, **kw), "ok"
    except Exception as e:  # noqa: BLE001
        return None, f"{type(e).__name__}: {str(e)[:120]}"
    finally:
        _spent(ctx, "sibling",
               "%s.%s" % (getattr(fn, "__module__", "?").split(".")[-1],
                          getattr(fn, "__name__", "?")),
               (time.time() - _t0) * 1000)


_FAILSOFT_FLAGS = ("known", "ok")


def _readable(value, why: str = "ok"):
    """(value, 'ok') or (None, why) — a sibling's FAIL-SOFT envelope is an
    UNREADABLE read, not a successful one.

    ★ THIS IS THE GREEN-BY-SILENCE HOLE THIS SHELL EXISTS TO CATCH, and the
      first cut of it had the hole. Parts B and C do not raise when their own
      DB read fails; they ANSWER, with `{"known": False, "error": "…"}` or
      `{"ok": False, "reason": "…"}` and an empty payload beside it. A caller
      that only tests `is None` reads that as a successful call, renders the
      check green, and prints the sibling's own error message as the DETAIL of
      a PASS. Worse, the empty payload beside the flag reads as reassurance:
      `withheld == []` becomes "the platform queue is clean" during an outage.
      So the flag is read HERE, once, for every sibling envelope this shell
      consumes — `?`, never PASS, and never a comforting zero.
    """
    if value is None:
        return None, why
    if isinstance(value, dict):
        for flag in _FAILSOFT_FLAGS:
            if flag in value and value.get(flag) is False:
                msg = str(value.get("error") or value.get("reason")
                          or value.get("why") or "").strip()
                return None, (f"the sibling answered {flag}=false"
                              + (f": {msg[:120]}" if msg else "")
                              + " — ITS read failed, so this is unverified, NOT "
                                "assumed fine (an empty payload beside a false "
                                "flag is not 'nothing pending')")
    return value, why


def _now() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def _budget_left(ctx: dict) -> float:
    """Seconds left before the read must answer. Unbounded (a huge number) when
    no deadline was set, so pure-unit callers are unaffected.

    ★ Lane-level budgeting alone does NOT bind: measured 2026-08-22 the four
    lanes cost 0.1s / 1s / 9s / 12s, and a deadline checked only BETWEEN lanes
    let the last one start at 10s and run to 22s. A budget that never cuts
    anything is a decorative guard. So the two reads that dominate — the effect
    bandit (one fresh DB connection PER CLASS inside brain_work_selector) and
    the per-detector findings loop — ask how much is left before they spend it,
    and render `?` when it is gone."""
    d = ctx.get("deadline")
    return 1e9 if d is None else (d - time.monotonic())


def _hours_since(ts) -> float | None:
    if ts is None:
        return None
    try:
        if isinstance(ts, str):
            ts = _dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=_dt.timezone.utc)
        return round((_now() - ts).total_seconds() / 3600.0, 1)
    except Exception:  # noqa: BLE001
        return None


def _short(v, n: int = 220) -> str:
    try:
        s = json.dumps(v, default=str, sort_keys=True)
    except Exception:  # noqa: BLE001
        s = str(v)
    return s if len(s) <= n else s[:n] + "…"


# ── memoised per-tick reads ───────────────────────────────────────────────

def _feed(ctx: dict):
    """/api/v1/ops/claims, read IN-PROCESS (read_feed is the route's own
    function — same numbers, no loopback, no edge cache)."""
    if "feed" not in ctx:
        fn = _import_attr("routes.ops_claims", "read_feed")
        v, why = _readable(*_call(fn, limit=1, _ctx=ctx))
        ctx["feed"] = v if isinstance(v, dict) else None
        ctx["feed_why"] = why
    return ctx.get("feed")


def _conv(ctx: dict):
    if "conv" not in ctx:
        fn = _import_attr("routes.squasher_queue", "convergence")
        v, why = _call(fn, 30, _ctx=ctx)
        ctx["conv"] = v if isinstance(v, dict) and v.get("ok") else None
        ctx["conv_why"] = why if v is None else str((v or {}).get("error") or why)
    return ctx.get("conv")


# ── the open decision inbox, read ONCE per tick ───────────────────────────
#
# ★ THREE READERS, ONE READ (2026-08-23). squasher_work_queue was read three
#   separate times a tick: lane 1 asked "does this eligible class have a
#   decision row?" once PER ELIGIBLE CLASS, lane 2 counted the open rows for the
#   collapse ratio, and _decide_today re-selected the same rows LAST, on
#   whatever budget was left — and lost. Measured on prod 2026-08-23:
#   tick_ms 8766-9398 against READ_BUDGET_S = 11, so the decide-today read was
#   refused on EVERY tick while lane 2, four checks earlier, had just counted 11
#   open rows in the same two statuses. The board published
#
#       inbox UNREADABLE this tick — 1.7s of the 11s budget left
#
#   next to its own successful count of the same table in the same second.
#   Naming the omission (#3091) made that honest; it did not make the rows
#   reachable. One read now serves all three, so they cost one budget slot
#   instead of three and cannot disagree about a queue they describe at the
#   same instant.
#
#   Every caller goes through _open_inbox(), so "nobody looked" cannot become
#   a silent "nothing there": the first caller pays for the read and the rest
#   get it free, and a caller that arrives with the budget already spent gets
#   None — the refusal — not an empty list.
#
# The cap bounds THIS READ, never the claim: COUNT(*) OVER () is computed before
# LIMIT, so a truncated read is DETECTED (open_rows > len(rows)) and published as
# a floor, instead of silently reporting a smaller queue than exists.
_INBOX_ROW_CAP = 200
(I_ID, I_TITLE, I_STATUS, I_CLASS, I_ACTION_URL, I_REQUESTED,
 I_KEY, I_OPEN_ROWS, I_CLASSIFIED) = range(9)


def _open_inbox(ctx: dict):
    """The open rows of squasher_work_queue, read once per tick and shared.

    None when the read failed or the budget refused it — NEVER []: an
    unreadable inbox is not an empty one."""
    if "inbox_rows" in ctx:
        return ctx["inbox_rows"]
    rows = _q("SELECT id, title, status, action_class, action_url, requested_at, "
              "       finding_key, COUNT(*) OVER (), COUNT(action_class) OVER () "
              "  FROM squasher_work_queue "
              " WHERE status IN ('awaiting_decision', 'awaiting_ops') "
              " ORDER BY (status = 'awaiting_decision') DESC, requested_at ASC "
              " LIMIT %s", (_INBOX_ROW_CAP,), ctx=ctx)
    ctx["inbox_rows"] = rows
    return rows


# ── the shell's own ledger (the budget IS the ledger, cf. #autonomy) ──────

# ★ Plain strings with the table name LITERAL, not f-strings: the report-only
# guard (tests) and regression_lint both read these as AST/raw text, and an
# f-string fragment hides the table from both.
_LEDGER_DDL = """
    CREATE TABLE IF NOT EXISTS agentic_loop_shell_ledger (
        kind       TEXT NOT NULL,
        day        DATE NOT NULL,
        n          INTEGER NOT NULL DEFAULT 0,
        payload    JSONB,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        PRIMARY KEY (kind, day)
    )"""

_LEDGER_UPSERT = """
    INSERT INTO agentic_loop_shell_ledger (kind, day, n, payload)
    VALUES (%s, CURRENT_DATE, %s, %s::jsonb)
    ON CONFLICT (kind, day) DO UPDATE
       SET n = agentic_loop_shell_ledger.n + EXCLUDED.n,
           payload = EXCLUDED.payload,
           updated_at = NOW()"""


def _ensure_ledger(ctx: dict) -> bool:
    """Lazy DDL, ONLY from the acting tick — never at boot, never from a GET."""
    c = ctx.get("conn")
    if c is None:
        return False
    try:
        with c.cursor() as cur:
            cur.execute(_LEDGER_DDL)
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning("[agentic_loop] ledger DDL failed: %s", e)
        return False


def _ledger_add(ctx: dict, kind: str, n: int, payload) -> bool:
    c = ctx.get("conn")
    if c is None:
        return False
    try:
        with c.cursor() as cur:
            cur.execute(_LEDGER_UPSERT,
                        (kind, int(n), json.dumps(payload or {}, default=str)))
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning("[agentic_loop] ledger write failed: %s", e)
        return False


def _ledger_present(ctx: dict) -> bool | None:
    r = _q(f"SELECT to_regclass('public.{LEDGER_TABLE}')", ctx=ctx)
    if r is None:
        return None
    return bool(r and r[0] and r[0][0])


def _filed_today(ctx: dict) -> int | None:
    """Decision rows filed today by the armed tick. None = ledger unreadable,
    which means NO budget (never fire blind)."""
    if not _ensure_ledger(ctx):
        return None
    r = _q(f"SELECT COALESCE(SUM(n), 0) FROM {LEDGER_TABLE} "
           f" WHERE kind = 'graduation_file' AND day = CURRENT_DATE",
           ctx=ctx)
    if r is None:
        return None
    return int((r[0][0] if r else 0) or 0)


def _rate_7d_ago(ctx: dict):
    """recurrence_rate from the newest daily snapshot at least 7 days old.
    None until the shell has a week of ticks behind it — null, never 0."""
    present = _ledger_present(ctx)
    if not present:
        return None
    r = _q(f"SELECT payload, day FROM {LEDGER_TABLE} "
           f" WHERE kind = 'tick' AND day <= CURRENT_DATE - 7 "
           f" ORDER BY day DESC LIMIT 1", ctx=ctx)
    if not r:
        return None
    payload = r[0][0]
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except ValueError:
            return None
    if not isinstance(payload, dict):
        return None
    return payload.get("recurrence_rate")


# ── part B: graduation_report(), called honestly ──────────────────────────

_DRY_PARAMS = ("dry_run", "report_only")
_FILING_PARAMS = ("file_rows", "file", "apply", "act", "write")
# ★ `max_file` FIRST and non-negotiable: it is part B's ACTUAL cap parameter —
#   PR #3073 ships `graduation_report(file=False, max_file=3, by="graduation")`.
#   Without it in this tuple the shell's remaining daily budget is silently
#   dropped and part B files up to its OWN default 3: with 2 rows already
#   ledgered today an armed tick files 3 more = 5 on the day, against a declared
#   ceiling of 3. The shell noticed only afterwards (over_budget=True), which is
#   a receipt for an overspend, not a cap. A budget that does not reach the
#   callee is not a budget.
_BUDGET_PARAMS = ("max_file", "max_rows", "limit", "cap", "budget")


def _graduation_report(file_rows: bool, budget: int = 0, ctx: dict = None):
    """(report, 'ok') or (None, why).

    ★ The read path may only call graduation_report() when its signature lets
    this shell switch filing OFF. The function files inbox rows by contract;
    calling it from a GET on the hope that it is harmless is a GET acting.
    """
    fn = _import_attr("routes.squasher_action_classes", "graduation_report")
    if fn is None:
        return None, ("graduation_report() is not on this deploy (part B) — "
                      "unverified, NOT assumed fine")
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        params = {}
    kw = {}
    has_mode = False
    for p in _DRY_PARAMS:
        if p in params:
            kw[p] = not file_rows
            has_mode = True
    for p in _FILING_PARAMS:
        if p in params:
            kw[p] = bool(file_rows)
            has_mode = True
    if file_rows:
        for p in _BUDGET_PARAMS:
            if p in params:
                kw[p] = int(budget)
                break
    if not file_rows and not has_mode:
        return None, ("graduation_report() exposes no dry_run/file parameter — "
                      "NOT called from a read (it files inbox rows); only the "
                      "armed tick calls it")
    # ★ part B answers {"known": False, "error": …} instead of raising when its
    #   own read fails — that is an unreadable report, not an empty one. Without
    #   this, lane 1 renders "0 class(es) reported; 0 eligible" and "no class is
    #   eligible for a grant yet — nothing can be silently waiting" off a DB
    #   outage, and goes fully green having reported nothing.
    return _readable(*_call(fn, _ctx=ctx, **kw))


def _report_rows(report) -> list:
    """Per-class entries out of whatever shape part B returns: a list, a dict
    under classes/rows/report/items, or a dict keyed by class name."""
    if isinstance(report, list):
        return [r for r in report if isinstance(r, dict)]
    if not isinstance(report, dict):
        return []
    for k in ("classes", "rows", "report", "items"):
        v = report.get(k)
        if isinstance(v, list):
            return [r for r in v if isinstance(r, dict)]
        if isinstance(v, dict):
            return [dict(val, **({"class": key} if "class" not in val else {}))
                    for key, val in v.items() if isinstance(val, dict)]
    return [dict(val, **({"class": key} if "class" not in val else {}))
            for key, val in report.items() if isinstance(val, dict)]


def _eligible_classes(report) -> list | None:
    """Names of classes the report marks eligible_for_grant; None when the
    report is absent (the caller renders `?`, not 'nothing eligible')."""
    if report is None:
        return None
    out = []
    for r in _report_rows(report):
        if r.get("eligible_for_grant") and r.get("class"):
            out.append(str(r["class"]))
    return out


def _filed_count(report, fallback: int) -> int:
    """How many rows the filing call says it filed; the budget when it does not
    say (conservative — an unreported write still spends the budget)."""
    if isinstance(report, dict):
        for k in ("filed", "rows_filed", "inbox_rows_filed", "filed_rows", "filed_classes"):
            v = report.get(k)
            if isinstance(v, bool):
                continue
            if isinstance(v, int):
                return max(0, v)
            if isinstance(v, (list, tuple)):
                return len(v)
    return max(0, int(fallback))


def _has_decision_row(rows, cls: str, key_of=None) -> bool:
    """Is an OPEN awaiting_decision row filed for this class's grant proposal?

    ★ MATCHED ON THE FILER'S OWN IDENTITY, NOT ON A GUESSED TITLE (2026-08-23).
      The title arm looked for "Grant class <cls>" while part B files the row as
      "Grant action class <cls>?" — the word `action` sits between them, so that
      arm could never match a row this system has ever written. It read as a
      second safety net and was one that could not fire: file_decision_row's
      REFRESH path does not backfill action_class, so on a row whose class is
      NULL the check would report a candidate as silently waiting while its
      decision row sat in the human's inbox — a false RED on the board written
      to catch false GREENs. The identity part B files under is the finding_key
      from its own proposal_key(); it is imported here, never restated. The
      title arm is kept, corrected, as the last resort it was meant to be.
    """
    key = None
    if callable(key_of):
        try:
            key = key_of(cls)
        except Exception:  # noqa: BLE001
            key = None
    title_arm = "Grant action class %s" % cls
    for r in rows:
        if r[I_STATUS] != "awaiting_decision":
            continue
        if key and r[I_KEY] == key:
            return True
        if r[I_CLASS] == cls:
            return True
        if title_arm in (r[I_TITLE] or ""):
            return True
    return False


# ── lane 1 — graduation on track record ───────────────────────────────────

def _registry_rows(ctx: dict):
    """(rows, 'ok') from brain_action_classes via part B's own reader — the
    registry is never re-described here. (None, why) when unreadable."""
    class_rows = _import_attr("routes.squasher_action_classes", "class_rows")
    if class_rows is None:
        return None, "routes.squasher_action_classes.class_rows unavailable"
    rows, why = _bounded(ctx, lambda cur: list(class_rows(cur) or []))
    if rows is None and ":" in why:          # an exception, not "no conn"/"no budget"
        why += (" — brain_action_classes is created lazily by part B; "
                "absent until it seeds")
    return rows, why


def _breaker_violations(runs, threshold: int, window_start=None) -> dict:
    """Pure. A DELIBERATE SECOND COPY of part B's breaker state machine.

    ★ Named as a copy because this file says two paragraphs above that the
      registry "is never re-described here" and that the detector AST rule is
      used "via the shared rule — never inlined". This is the exception, and it
      is one on purpose: the shell must be able to say "a class with a tripped
      breaker executed anyway" from the RUN LEDGER, i.e. without asking the
      component whose bypass it is checking. The cost is that it will drift
      silently if part B changes its threshold semantics — so it re-reads
      _BREAKER_THRESHOLD from part B at call time rather than pinning 3, and a
      change to _update_class's reset/increment rules must be mirrored here.

    Mirrors squasher_action_classes._update_class: each executed
    non-dry run that did not verify bumps a class's consecutive counter, a
    verified run resets it, and the breaker trips when the counter reaches
    `threshold`. Returns {class: executed runs AFTER the trip (inside
    window_start when given)} — anything above 0 means eligible() was bypassed.

    runs: iterable of (class, started_at, executed, verified, dry_run), any order.
    """
    by: dict = {}
    for r in runs or []:
        cls, started, executed, verified, dry = r[0], r[1], bool(r[2]), bool(r[3]), bool(r[4])
        if not executed or dry:
            continue
        by.setdefault(cls, []).append((started, verified))
    out: dict = {}
    for cls, seq in by.items():
        seq.sort(key=lambda x: x[0])
        consecutive = 0
        tripped = False
        after = 0
        for started, verified in seq:
            if tripped:
                if window_start is None or started >= window_start:
                    after += 1
                continue
            consecutive = 0 if verified else consecutive + 1
            if consecutive >= threshold:
                tripped = True
        out[cls] = after
    return out


def _lane_graduation(ctx: dict) -> list:
    """★ Autonomy grows by EARNED vocabulary.

    A class runs only after a human grants it, a grant is refused unless the
    class is reversible AND verified AND parameter-bound (grant_allowed), and
    a tripped breaker takes a class out of eligible(). This lane re-derives
    those three facts from the registry and the run ledger every tick, then
    asks the track-record question part B answers: which candidates have
    EARNED a grant proposal, and is that proposal sitting in front of a human
    rather than silently waiting?
    """
    rows, why = _registry_rows(ctx)
    if rows is None:
        return [_check("a_read", "action-class registry readable", None, why,
                       critical=True)]
    mod = _module("routes.squasher_action_classes")
    grant_allowed = getattr(mod, "grant_allowed", None)
    threshold = int(getattr(mod, "_BREAKER_THRESHOLD", 3) or 3)
    granted = [r for r in rows if r.get("granted")]
    candidates = [r for r in rows if not r.get("granted")]
    out = [_check("a_read", "action-class registry readable", True,
                  f"{len(rows)} class row(s): {len(granted)} granted, "
                  f"{len(candidates)} candidate(s) (granted=false)", critical=True)]

    # every granted class passes grant_allowed — a row edited straight into the
    # table gets no free pass (the drain re-runs the same test)
    if not callable(grant_allowed):
        out.append(_check("a_granted_pass_gate", "every granted class passes grant_allowed",
                          None, "grant_allowed unavailable — unverified", critical=True))
    elif not granted:
        out.append(_check("a_granted_pass_gate", "every granted class passes grant_allowed",
                          None, "no class is granted yet — the gate has never been "
                          "exercised; unverified, not assumed", critical=True))
    else:
        bad = []
        for r in granted:
            try:
                ok, reason = grant_allowed(r)
            except Exception as e:  # noqa: BLE001
                ok, reason = False, f"grant_allowed raised {type(e).__name__}"
            if not ok:
                bad.append(f"{r.get('class')}: {reason}")
        out.append(_check("a_granted_pass_gate", "every granted class passes grant_allowed",
                          not bad,
                          ("all %d granted class(es) pass: %s" % (
                              len(granted), ", ".join(str(r.get("class")) for r in granted)))
                          if not bad else "GRANTED BUT REFUSED BY THE GATE: " + "; ".join(bad),
                          critical=True))

    # no class with a tripped breaker executed in 7d
    tripped = [str(r.get("class")) for r in rows if r.get("breaker_tripped")]
    if not tripped:
        out.append(_check("a_breaker_no_exec", "no tripped class executed in 7d", True,
                          f"no class has a tripped breaker ({len(rows)} row(s) read)",
                          critical=True))
    else:
        runs = _q("SELECT class, started_at, executed, verified, dry_run "
                  "  FROM brain_action_class_runs "
                  " WHERE class = ANY(%s) AND started_at > NOW() - INTERVAL '90 days' "
                  " ORDER BY started_at LIMIT 5000", (tripped,), ctx=ctx)
        if runs is None:
            out.append(_check("a_breaker_no_exec", "no tripped class executed in 7d", None,
                              f"run ledger unreadable for tripped {tripped}", critical=True))
        else:
            viol = _breaker_violations(runs, threshold,
                                       window_start=_now() - _dt.timedelta(days=7))
            bad = {k: v for k, v in viol.items() if v}
            untraced = [c for c in tripped if c not in viol]
            out.append(_check(
                "a_breaker_no_exec", "no tripped class executed in 7d", not bad,
                (f"tripped: {tripped}; executions after the trip in 7d: {bad or 'none'}"
                 + (f"; trip point not in the ledger for {untraced} (tripped by hand?)"
                    if untraced else "")),
                critical=True))

    # part B: the track record → proposal
    report, rwhy = _graduation_report(file_rows=False, ctx=ctx)
    if report is None:
        out.append(_check("a_report", "graduation_report() readable", None, rwhy))
    else:
        rr = _report_rows(report)
        out.append(_check("a_report", "graduation_report() readable", True,
                          f"{len(rr)} class(es) reported; "
                          f"{sum(1 for r in rr if r.get('eligible_for_grant'))} eligible"))
    ctx["graduation_report"] = report

    out.append(_check("a_candidate_exists", "at least one candidate class is registered",
                      len(candidates) >= 1,
                      ("candidates: " + ", ".join(str(r.get("class")) for r in candidates))
                      if candidates else
                      "0 rows with granted=false — nothing can graduate from an "
                      "empty bench (part B seeds the registry in DATA, not prompts)"))

    eligible = _eligible_classes(report)
    if eligible is None:
        out.append(_check("a_eligible_decision_row",
                          "every eligible candidate has an open decision row", None,
                          "needs graduation_report() (part B) to name the eligible classes"))
    elif not eligible:
        out.append(_check("a_eligible_decision_row",
                          "every eligible candidate has an open decision row", True,
                          "no class is eligible for a grant yet — nothing can be "
                          "silently waiting"))
    else:
        rows = _open_inbox(ctx)
        if rows is None:
            out.append(_check(
                "a_eligible_decision_row",
                "every eligible candidate has an open decision row", None,
                f"eligible: {eligible}; squasher_work_queue unreadable this tick — "
                f"unverified. NOT 'silently waiting' and NOT fine: this check "
                f"cannot tell a missing decision row from a missed read"))
        else:
            key_of = _import_attr("routes.squasher_action_classes", "proposal_key")
            missing = [c for c in eligible if not _has_decision_row(rows, c, key_of)]
            # ★ NAME THE FILER WHEN THE ROW IS ABSENT. Nothing files these rows
            #   except part B's graduation_report(file=True), and the ONLY caller
            #   of that is the scheduled tick under AGENTIC_LOOP_ARM=1. A red
            #   here with armed=False is not a mystery, it is a disarmed filer —
            #   say so on the board instead of leaving the next reader to find it.
            why = ""
            if missing:
                why = (f" — the only writer of these rows is part B's "
                       f"graduation_report(file=True), called only by the "
                       f"scheduled tick under AGENTIC_LOOP_ARM=1; armed="
                       f"{_armed()}"
                       + (", so nothing can have filed them" if not _armed() else ""))
            out.append(_check(
                "a_eligible_decision_row", "every eligible candidate has an open decision row",
                not missing,
                f"eligible: {eligible}; without an open awaiting_decision row: "
                f"{missing or 'none'}{why}"))
    return out


# ── lane 2 — the human queues ─────────────────────────────────────────────

def _oldest_age_hours(ages, status: str):
    """Pull 'oldest age in hours for `status`' out of whatever shape part B's
    queue_ages() returns: rows with a status key, or a dict keyed by status.
    None when the shape carries no such number."""
    best = None

    def _num(d: dict):
        for k in ("oldest_age_hours", "oldest_hours", "age_hours", "oldest_h"):
            v = d.get(k)
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                return float(v)
        for k in ("oldest_age_days", "oldest_days", "age_days"):
            v = d.get(k)
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                return float(v) * 24.0
        return None

    def _walk(node):
        nonlocal best
        if isinstance(node, dict):
            if str(node.get("status") or "") == status:
                v = _num(node)
                if v is not None:
                    best = v if best is None else max(best, v)
            for k, v in node.items():
                if k == status and isinstance(v, dict):
                    n = _num(v)
                    if n is not None:
                        best = n if best is None else max(best, n)
                    for sub in v.values():
                        if isinstance(sub, dict):
                            n2 = _num(sub)
                            if n2 is not None:
                                best = n2 if best is None else max(best, n2)
                elif isinstance(v, (dict, list)):
                    _walk(v)
        elif isinstance(node, list):
            for v in node:
                _walk(v)

    _walk(ages)
    return best


def _digest_reaches_aged_recs(src: str):
    """Can the weekly strategic digest name a rec that aged out of its own
    ISO week? True / False / None (function not found — the caller renders `?`).

    ★ 2026-08-23. This replaces _digest_rec_window_is_one_week, which asked the
    inverse question — "does render_weekly_digest call _read_recs_for(week_of)?"
    — and answered it correctly: it did, so a rec older than that week was
    structurally outside the only artifact that mails recommendations to a
    human. 298 rows sat unreachable behind a green digest workflow. The fix
    added a SECOND, age-based selection, so the detector now has to decide
    whether that selection is really there rather than whether the old one is.

    AST, not grep, and deliberately hard to satisfy with a stub — BOTH must
    hold, because either one alone is satisfiable while the defect stands:

      1. render_weekly_digest() actually CALLS _read_stale_recs(). A comment,
         a docstring, or a helper defined and never called cannot satisfy this.
      2. _read_stale_recs() itself is WEEK-INDEPENDENT: its SQL selects on
         status + an age bound and carries no `week_of =` filter. A reader that
         reintroduced a week bound would restore the original defect while
         still passing (1).

    Returns False — not None — when render_weekly_digest exists but does not
    reach aged rows. That is the defect, and it must read FAIL rather than
    "unverified".
    """
    try:
        tree = ast.parse(src)
    except Exception:  # noqa: BLE001
        return None

    render = None
    reader = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            if node.name == "render_weekly_digest":
                render = node
            elif node.name == "_read_stale_recs":
                reader = node
    if render is None:
        return None

    calls_reader = any(
        isinstance(sub, ast.Call) and (
            getattr(sub.func, "id", None)
            or getattr(sub.func, "attr", None)) == "_read_stale_recs"
        for sub in ast.walk(render))
    if not calls_reader:
        return False
    if reader is None:
        return False

    sql = " ".join(
        n.value for n in ast.walk(reader)
        if isinstance(n, ast.Constant) and isinstance(n.value, str))
    week_bound = ("week_of =" in sql) or ("week_of=" in sql)
    age_bound = ("make_interval" in sql) or ("INTERVAL" in sql.upper())
    return bool(age_bound and not week_bound)


def _withheld_carries(w: dict) -> tuple[bool, bool]:
    """(has_age, has_decision_url) for one platform pending/withheld entry."""
    age = any(w.get(k) for k in ("announced", "age_hours", "age_days", "staged_at",
                                 "created_at", "opened_at", "since"))
    url = any(w.get(k) for k in ("decision_url", "decide_url", "pr_url", "url", "href"))
    return bool(age), bool(url)


def _lane_human_queues(ctx: dict) -> list:
    """★ Where the brain's output dies today.

    On 2026-08-22: 43 inbox rows (23 awaiting_decision), 9 platform updates
    pending + 9 withheld, ~45 strategic recs at status=new. None of them had
    an age, a deadline or a one-click decision in front of a human. The lane
    does not judge whether the human is fast; it asks whether a decision can
    REACH one and whether any has waited past a declared ceiling.

    ★ THE THREE QUEUES ARE `critical=True`, and that is the whole point of the
      lane. It shipped with ZERO critical checks, so _lane_verdict fell through
      to its weakest rule — `?` only when NOTHING was True — and the board read

          PASS  every human queue has an age, a ceiling and a one-click decision

      with the database down, no GitHub token, and part B fail-softing: five
      checks `?`, ONE True (a fail-soft envelope mistaken for a successful
      read), verdict PASS. Green by silence, published as health, on the one
      board written to catch exactly that. A lane cannot read PASS when it could
      not check. So each of the three queues this lane claims to cover — the
      decision inbox, the platform feed, the strategic recs — now blocks a PASS
      on its own when it is unreadable.

      b_collapse_ratio stays non-critical ON PURPOSE: it is published, never
      judged (`pass` is always None), so making it critical would pin the lane
      at `?` forever and mean nothing. b_digest_run stays non-critical because
      it is a reach proof for a DIFFERENT artifact than the recs — see the note
      on that check.
    """
    out = []

    qa = _import_attr("routes.squasher_queue", "queue_ages")
    # ★ NOT just `is None`: part B answers {"known": False, "error": …} when the
    #   queue is unreadable, and the shipped shell read that as a successful
    #   call — rendering `PASS queue_ages() readable` with part B's own
    #   "too many connections" as the DETAIL of the pass.
    ages, why = _readable(*_call(qa, _ctx=ctx))
    if ages is None:
        out.append(_check("b_queue_ages", "queue_ages() readable", None,
                          why if qa is not None else
                          "queue_ages() is not on this deploy (part B) — unverified"))
    else:
        out.append(_check("b_queue_ages", "queue_ages() readable", True, _short(ages)))
    ctx["queue_ages"] = ages

    ceiling_h = DECISION_AGE_CEILING_DAYS * 24.0
    oldest = _oldest_age_hours(ages, "awaiting_decision") if ages is not None else None
    basis = "queue_ages()"
    empty = False
    if oldest is None:
        r = _q("SELECT MIN(requested_at), COUNT(*) FROM squasher_work_queue "
               " WHERE status = 'awaiting_decision'", ctx=ctx)
        if r is not None:
            basis = "squasher_work_queue.requested_at (direct read; queue_ages() gave no age)"
            n = int(r[0][1] or 0)
            if n == 0:
                empty, oldest = True, 0.0
            else:
                oldest = _hours_since(r[0][0])
    out.append(_check(
        "b_oldest_decision",
        f"no awaiting_decision row is older than the declared {DECISION_AGE_CEILING_DAYS}d ceiling",
        None if oldest is None else (oldest < ceiling_h),
        ("awaiting_decision is empty" if empty else
         f"oldest awaiting_decision = {oldest}h vs ceiling {ceiling_h:.0f}h")
        + f" (basis: {basis})" if oldest is not None else f"unreadable (basis: {basis})",
        critical=True))

    # platform updates: pending + withheld must each carry an age and a decision URL
    pu = _import_attr("routes.platform_updates", "published_updates")
    # ★ NOT just `isinstance(block, dict)`: published_updates() fail-softs to
    #   {"ok": False, "cards": [], "withheld_count": 0, "withheld": [],
    #    "reason": "platform updates unavailable (…)"} on ANY exception. That
    #   dict passed the type test, `withheld == []` gave `lacking == []`, and a
    #   platform-updates outage was published as
    #   "pending=0 withheld=0 … every entry carries both" — a green check AND a
    #   reassuring zero. Probed live 2026-08-22: True.
    block, pwhy = _readable(*_call(pu, _ctx=ctx))
    if not isinstance(block, dict):
        out.append(_check("b_platform_items", "platform pending+withheld carry an age and a decision URL",
                          None, pwhy if pu is not None else "routes.platform_updates unavailable",
                          critical=True))
    else:
        withheld = [w for w in (block.get("withheld") or []) if isinstance(w, dict)]
        # ★ PENDING ≠ WITHHELD. Read the feed's explicit `awaiting_decision`
        # when it publishes one; the reason-string fallback is for a feed that
        # predates it. Every non-published entry used to yield the one reason
        # "not approved", so nine cards ARCHIVED on 2026-08-17 (PR #2804,
        # "archive pre-August wave") were counted as nine decisions an owner
        # still owed. Retired is a decision TAKEN. A queue that counts settled
        # items as outstanding manufactures an owner backlog out of finished
        # work — the same class of false reading this lane exists to catch.
        pending = [w for w in withheld
                   if (bool(w.get("awaiting_decision"))
                       if "awaiting_decision" in w
                       else "not approved" in str(w.get("reason") or ""))]
        lacking = [w for w in withheld if not all(_withheld_carries(w))]
        out.append(_check(
            "b_platform_items", "platform pending+withheld carry an age and a decision URL",
            not lacking,
            f"awaiting_decision={len(pending)} retired={len(withheld) - len(pending)} "
            f"withheld={len(withheld)} (feed's own definitions); "
            + ("every entry carries an age and a decision URL" if not lacking else
               f"{len(lacking)} carry neither — the feed publishes only {{id, reason}}, "
               f"so a human cannot see how long it has waited or where to decide: "
               + ", ".join(str(w.get('id')) for w in lacking[:6])),
            critical=True))
        ctx["platform_withheld"] = withheld

    # Strategic recs at status=new past REC_STALE_DAYS must be NAMED in an
    # artifact a human reads.
    #
    # ★ REACH IS DECIDED FROM THE ARTIFACT'S SELECTION WINDOW, NOT BY RENDERING
    #   IT. The first cut of this lane called
    #   brain_weekly_digest.render_weekly_digest() here to grep its body. That
    #   call measured 35-55s against live Neon on 2026-08-22 (and returned
    #   rec_count=0, so it was slow for nothing). A read path that slow trips the
    #   Cloudflare route timeout, and a 5xx from Railway is read by the worker as
    #   a dead origin and fails the whole site over to stale Render — the exact
    #   hazard the kill-switch note at the top of this file exists for. A board
    #   that has to re-run an email to answer a question is a board that takes
    #   the site down. Decide it from the code instead: ONE file read + AST.
    rec_name = (f"strategic recs at status=new for >{REC_STALE_DAYS}d reach a human "
                f"(the digest's window can contain them)")
    counted = _q("SELECT COUNT(*), MIN(created_at) FROM brain_strategic_recommendations "
                 " WHERE status = 'new' "
                 f"  AND created_at < NOW() - INTERVAL '{int(REC_STALE_DAYS)} days'",
                 ctx=ctx)
    # a SAMPLE for the decide-today list — never the basis for the count, which
    # a LIMIT would silently truncate ("0/100 named" is a limit, not a total)
    ctx["stale_recs"] = _q(
        "SELECT id, title, created_at FROM brain_strategic_recommendations "
        " WHERE status = 'new' "
        f"  AND created_at < NOW() - INTERVAL '{int(REC_STALE_DAYS)} days' "
        " ORDER BY created_at LIMIT 10", ctx=ctx)
    sample = ctx.get("stale_recs") or []
    if counted is None:
        out.append(_check("b_stale_recs_named", rec_name, None,
                          "brain_strategic_recommendations unreadable", critical=True))
    else:
        n_stale = int((counted[0][0] if counted else 0) or 0)
        oldest_d = ((_hours_since(counted[0][1]) or 0.0) / 24.0) if counted else 0.0
        names = "; ".join(str(r[1])[:50] for r in sample[:3])
        reaches = _digest_reaches_aged_recs(_read("routes/brain_weekly_digest.py"))
        if not n_stale:
            out.append(_check("b_stale_recs_named", rec_name, True,
                              f"no rec has sat at status=new for more than {REC_STALE_DAYS}d",
                              critical=True))
        elif reaches is True:
            out.append(_check(
                "b_stale_recs_named", rec_name, True,
                f"{n_stale} rec(s) at status=new for >{REC_STALE_DAYS}d, oldest "
                f"{oldest_d:.0f}d: {names} — but they are no longer unreachable: "
                f"brain_weekly_digest.render_weekly_digest runs a SECOND, "
                f"age-based selection (_read_stale_recs) that carries no week "
                f"bound, names them oldest-first with the true total beside the "
                f"listed count, and mails them even in a week that produced no "
                f"new synthesis. ★ THIS ASSERTS REACH, NOT TRIAGE: the rows are "
                f"inside the artifact that mails recs to a human; that a human "
                f"then decides them is not measured here, and delivery is the "
                f"weekly send (idempotent per ISO week)",
                critical=True))
        elif reaches is False:
            out.append(_check(
                "b_stale_recs_named", rec_name, False,
                f"{n_stale} rec(s) at status=new for >{REC_STALE_DAYS}d, oldest "
                f"{oldest_d:.0f}d: {names} — the only artifact that mails recs to a "
                f"human (brain_weekly_digest.render_weekly_digest, via "
                f"_read_recs_for(week_of)) selects ONE ISO week, so a rec that aged "
                f"out of that week can never be named in it however green the digest "
                f"workflow runs", critical=True))
        else:
            out.append(_check(
                "b_stale_recs_named", rec_name, None,
                f"{n_stale} rec(s) at status=new for >{REC_STALE_DAYS}d, oldest "
                f"{oldest_d:.0f}d: {names} — "
                + "render_weekly_digest() is not in routes/brain_weekly_digest.py, "
                  "so the delivery artifact could not be read"
                + "; unverified, NOT assumed fine", critical=True))

    # the decision digest's last run — reach is proven by a run, not a file
    run, gwhy = _gh(f"/repos/{_REPO}/actions/workflows/{DIGEST_WORKFLOW}/runs"
                    f"?per_page=1&exclude_pull_requests=true", ctx=ctx)
    if not isinstance(run, dict):
        out.append(_check("b_digest_run", f"{DIGEST_WORKFLOW} last run is green and recent",
                          None, gwhy if run is None else
                          "api.github.com answered a non-object body — unverified, "
                          "NOT assumed fine"))
    else:
        runs = run.get("workflow_runs") or []
        if not runs:
            out.append(_check("b_digest_run", f"{DIGEST_WORKFLOW} last run is green and recent",
                              False, "the workflow has never run"))
        else:
            r0 = runs[0]
            concl = r0.get("conclusion")
            age_h = _hours_since(r0.get("created_at"))
            recent = age_h is not None and age_h <= DIGEST_MAX_AGE_DAYS * 24
            out.append(_check(
                "b_digest_run", f"{DIGEST_WORKFLOW} last run is green and recent",
                (concl == "success") and recent,
                f"last run {concl} at {r0.get('created_at')} ({age_h}h ago; max "
                f"{DIGEST_MAX_AGE_DAYS}d) — this workflow goes RED BY DESIGN when a "
                f"decision has waited >14d, so red = stale decisions reached a human, "
                f"which is still this lane's failure. ★ THIS IS A DIFFERENT "
                f"ARTIFACT FROM b_stale_recs_named's: it is the GitHub-issues "
                f"orphan-decision digest, not brain_weekly_digest, which is what "
                f"mails strategic recs. Two halves of reach, two artifacts — read "
                f"them separately, not as one proof"))

    # collapse ratio = distinct classes / CLASSIFIED open rows — published, not judged
    #
    # ★ THE RATIO USED TO REPORT ITS BEST NUMBER IN ITS WORST STATE (2026-08-23).
    #   It counted COUNT(DISTINCT COALESCE(action_class, 'unclassified')) over
    #   ALL open rows. When NOTHING is classified, every row collapses into the
    #   one synthetic 'unclassified' bucket, so classes=1 over 11 rows published
    #
    #       1/11 = 0.09 — lower means one class decision clears many rows
    #
    #   which reads as near-perfect collapse. Measured that morning: all 11 rows
    #   had action_class NULL and were ten DIFFERENT kinds of finding — a drip
    #   CTA pricing bug, unmarked facility duplicates, a slow endpoint, a stale
    #   news feed. No class decision could clear ANY of them, because none of
    #   them carried a class. The metric said "one decision clears eleven rows"
    #   at the exact moment the true answer was "no decision can clear one".
    #
    #   A synthetic bucket is not a class. The ratio is now computed over the
    #   rows that actually carry one, and the unclassified remainder is
    #   published beside it rather than folded into it.
    rows = _open_inbox(ctx)
    name = ("collapse ratio = distinct classes / CLASSIFIED open rows "
            "(published, not judged)")
    if rows is None:
        out.append(_check("b_collapse_ratio", name, None,
                          "squasher_work_queue unreadable"))
    else:
        # The two totals ride on every row as window functions, so they count
        # the whole open queue even when the row read itself was capped.
        open_rows = int(rows[0][I_OPEN_ROWS] or 0) if rows else 0
        classified = int(rows[0][I_CLASSIFIED] or 0) if rows else 0
        classes = len({r[I_CLASS] for r in rows if r[I_CLASS]})
        truncated = len(rows) < open_rows
        unclassified = open_rows - classified
        ratio = round(classes / classified, 2) if classified else None
        ctx["collapse"] = {"open_rows": open_rows, "classified_rows": classified,
                           "unclassified_rows": unclassified, "classes": classes,
                           "ratio": ratio, "rows_read": len(rows),
                           "truncated": truncated}
        # ★ A capped read makes the distinct-class count a FLOOR, so the ratio
        #   built on it is a floor too. Say which, rather than publish a number
        #   whose basis the reader cannot see.
        floor = (f" ★ READ CAPPED at {len(rows)} of {open_rows} open row(s), so "
                 f"the distinct-class count — and the ratio — are FLOORS, not "
                 f"the queue's true values" if truncated else "")
        if not open_rows:
            detail = "no open rows"
        elif not classified:
            detail = (f"0 of {open_rows} open row(s) carry an action_class, so "
                      f"the ratio is UNDEFINED, not good — no class decision can "
                      f"clear any row. Classify first "
                      f"(POST /api/v1/brain/squasher/classify); a collapse ratio "
                      f"cannot be earned by a queue with nothing to collapse")
        else:
            detail = (f"{classes}/{classified} = {ratio} — 1.0 means every "
                      f"classified row needs its own decision; lower means one "
                      f"class decision clears many rows"
                      + (f". {unclassified} of {open_rows} open row(s) carry NO "
                         f"class and are outside this ratio entirely"
                         if unclassified else "")
                      + floor)
        out.append(_check("b_collapse_ratio", name, None, detail))
    return out


# ── lane 3 — the learn station ────────────────────────────────────────────

def _prompt_names_refuted_claims(src: str):
    """AST, not grep: does the planner's _build_prompt name the negative-lesson
    section? True/False; None when the function cannot be found. A comment
    mentioning the key cannot satisfy this."""
    try:
        tree = ast.parse(src)
    except Exception:  # noqa: BLE001
        return None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_build_prompt":
            for sub in ast.walk(node):
                if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                    if sub.value == "refuted_claims" or "WHAT WE GOT WRONG" in sub.value:
                        return True
            return False
    return None


def _mentions(result, claim) -> bool:
    """Does one recall result refer to the claim (by id in meta, or by its
    subject / statement text)?"""
    if not isinstance(result, dict):
        return False
    cid, subject, statement = claim[0], str(claim[1] or ""), str(claim[2] or "")
    meta = result.get("meta") if isinstance(result.get("meta"), dict) else {}
    for k in ("claim_id", "id", "source_id"):
        v = meta.get(k, result.get(k))
        if v is not None and str(v) == str(cid):
            return True
    text = str(result.get("text") or "")
    if statement and statement[:60] in text:
        return True
    return bool(subject) and subject in text


def _lane_learn(ctx: dict) -> list:
    """★ A negative result that nobody can recall was never learned.

    Refuted and retracted claims are the only ground truth this platform has
    about its own over-claims. Part C turns them into a RAG corpus the planner
    and the lane driver retrieve from. This lane asks whether the corpus is
    registered and NOT public, whether it keeps up with the ledger, whether a
    refuted subject actually comes back from recall, whether the planner
    prompt has a place to put it, and whether the effect bandit has any data.
    """
    out = []
    newest = _q("SELECT id, subject, statement, outcome, outcome_at "
                "  FROM brain_predictions_log "
                " WHERE source_layer = 'CLAIM' AND outcome IN ('refuted', 'retracted') "
                " ORDER BY outcome_at DESC NULLS LAST LIMIT 1", ctx=ctx)
    count = _q("SELECT COUNT(*) FROM brain_predictions_log "
               " WHERE source_layer = 'CLAIM' AND outcome IN ('refuted', 'retracted')", ctx=ctx)
    n = int(count[0][0] or 0) if count else None
    claim = newest[0] if newest else None
    exists = bool(claim)
    # ★ critical ALWAYS. Before a negative result exists these checks carry
    # pass=None, so the lane reads `?` ("? before"); once one exists they
    # carry True/False. Non-critical here let the lane PASS on the ledger
    # read alone while nothing could be recalled — a soft pass.
    crit = True
    if newest is None:
        out.append(_check("c_ledger", "claim ledger readable (negative results counted)", None,
                          "brain_predictions_log unreadable"))
    else:
        out.append(_check("c_ledger", "claim ledger readable (negative results counted)", True,
                          (f"{n} refuted/retracted claim(s); newest #{claim[0]} {claim[3]} "
                           f"'{str(claim[1])[:60]}' at {claim[4]}") if exists else
                          "no refuted/retracted claim yet — the first live refutation is "
                          "expected 2026-08-23T04:08Z (canon:public.deals)"))
    ctx["newest_negative"] = claim

    rag = _module("routes.brain_rag")
    registered = False
    if rag is None:
        out.append(_check("c_corpus_registered", f"{CLAIM_LESSON_CORPUS} corpus registered and NOT public",
                          None, "routes.brain_rag unavailable", critical=crit))
    else:
        corpora = getattr(rag, "CORPORA", {}) or {}
        lesson = tuple(getattr(rag, "LESSON_CORPORA", ()) or ())
        public = tuple(getattr(rag, "PUBLIC_CORPORA", ()) or ())
        registered = CLAIM_LESSON_CORPUS in corpora and CLAIM_LESSON_CORPUS in lesson
        if CLAIM_LESSON_CORPUS in public:
            out.append(_check("c_corpus_registered", f"{CLAIM_LESSON_CORPUS} corpus registered and NOT public",
                              False, f"{CLAIM_LESSON_CORPUS} is in PUBLIC_CORPORA — brain internals "
                              f"retrievable keyless under CC-BY. NEVER.", critical=True))
        elif registered:
            out.append(_check("c_corpus_registered", f"{CLAIM_LESSON_CORPUS} corpus registered and NOT public",
                              True, f"in CORPORA and LESSON_CORPORA, not in PUBLIC_CORPORA "
                              f"(where: {str((corpora.get(CLAIM_LESSON_CORPUS) or {}).get('where'))[:80]})",
                              critical=crit))
        else:
            out.append(_check("c_corpus_registered", f"{CLAIM_LESSON_CORPUS} corpus registered and NOT public",
                              False if exists else None,
                              f"{CLAIM_LESSON_CORPUS} not in CORPORA/LESSON_CORPORA (part C)"
                              + (" — a refuted claim exists and nothing can recall it"
                                 if exists else " — no negative result exists yet, so nothing "
                                 "is lost yet; unverified"), critical=crit))

    # embedded within one reindex cycle of the newest negative row
    if registered and exists:
        emb = _q("SELECT MAX(updated_at), COUNT(*) FROM brain_corpus_embeddings "
                 " WHERE source_table = %s", (CLAIM_LESSON_CORPUS,), ctx=ctx)
        if emb is None:
            out.append(_check("c_embedded_fresh", "corpus embedded within one reindex cycle of its newest row",
                              None, "brain_corpus_embeddings unreadable", critical=True))
        else:
            last_emb, n_emb = emb[0]
            newest_at = claim[4]
            lag_h = _hours_since(newest_at)
            due = lag_h is not None and lag_h > (REINDEX_CYCLE_HOURS + REINDEX_GRACE_HOURS)
            fresh = bool(last_emb and newest_at and _hours_since(last_emb) is not None
                         and _hours_since(last_emb) <= (lag_h or 0))
            out.append(_check(
                "c_embedded_fresh", "corpus embedded within one reindex cycle of its newest row",
                True if fresh else (False if due else None),
                f"{int(n_emb or 0)} embedding(s); newest embed {last_emb}; newest negative "
                f"outcome {newest_at} ({lag_h}h ago; cycle {REINDEX_CYCLE_HOURS}h + "
                f"{REINDEX_GRACE_HOURS}h grace)" + ("" if fresh or due else " — not yet due"),
                critical=True))
    else:
        out.append(_check("c_embedded_fresh", "corpus embedded within one reindex cycle of its newest row",
                          None, "corpus not registered" if not registered else
                          "no negative result to embed yet", critical=crit))

    # recall self-test on the newest refuted subject
    recall = _import_attr("routes.brain_rag", "recall_negative_lessons")
    if recall is None:
        out.append(_check("c_recall_selftest", "recall_negative_lessons(<refuted subject>) returns it",
                          None, "recall_negative_lessons() is not on this deploy (part C) — unverified",
                          critical=crit))
    elif not exists:
        out.append(_check("c_recall_selftest", "recall_negative_lessons(<refuted subject>) returns it",
                          None, "nothing to recall yet", critical=crit))
    else:
        q = str(claim[2] or claim[1] or "")
        res, rwhy = _call(recall, q, k=4, _ctx=ctx)
        if res is None:
            out.append(_check("c_recall_selftest", "recall_negative_lessons(<refuted subject>) returns it",
                              None, f"recall failed: {rwhy}", critical=crit))
        else:
            hit = any(_mentions(r, claim) for r in (res or []))
            out.append(_check("c_recall_selftest", "recall_negative_lessons(<refuted subject>) returns it",
                              hit, f"{len(res or [])} result(s) for '{q[:50]}'; "
                              f"{'contains' if hit else 'does NOT contain'} claim #{claim[0]}",
                              critical=crit))

    # the planner prompt has a place to put it
    present = _prompt_names_refuted_claims(_read("routes/brain_strategic_planner.py"))
    if present is None:
        out.append(_check("c_planner_section", "planner prompt renders the negative-lesson section when lessons exist",
                          None, "routes/brain_strategic_planner._build_prompt not found"))
    elif present:
        out.append(_check("c_planner_section", "planner prompt renders the negative-lesson section when lessons exist",
                          True, "_build_prompt names refuted_claims / 'WHAT WE GOT WRONG' (AST)"))
    else:
        out.append(_check("c_planner_section", "planner prompt renders the negative-lesson section when lessons exist",
                          False if exists else None,
                          "_build_prompt does not name refuted_claims (part C); a new ctx key is "
                          "silently dropped unless the prompt names it"
                          + ("" if exists else " — no lesson exists yet; unverified")))

    # the effect bandit has data once the sample floor is met
    ws = _module("routes.brain_work_selector")
    floor = int(getattr(ws, "WORK_MIN_SAMPLES", 3) or 3) if ws else 3
    window = int(getattr(ws, "WORK_WINDOW_DAYS", 45) or 45) if ws else 45
    fix = _q("SELECT LOWER(COALESCE(klass, '')), COUNT(*) FILTER (WHERE resolved IS NOT NULL) "
             "  FROM brain_fix_outcomes "
             f" WHERE verified_at >= NOW() - INTERVAL '{window} days' "
             " GROUP BY 1 ORDER BY 2 DESC LIMIT 50", ctx=ctx)
    auto = _q("SELECT pattern_name, COUNT(*) FILTER (WHERE succeeded IS NOT NULL) "
              "  FROM autopilot_outcomes "
              f" WHERE verified_at >= NOW() - INTERVAL '{window} days' "
              " GROUP BY 1 ORDER BY 2 DESC LIMIT 50", ctx=ctx)
    if fix is None and auto is None:
        out.append(_check("c_bandit_weights", "learned_class_weights non-empty once the sample floor is met",
                          None, "brain_fix_outcomes / autopilot_outcomes unreadable"))
    else:
        pairs = [(k, int(n or 0)) for k, n in (fix or []) + (auto or []) if k]
        met = [k for k, n in pairs if n >= floor]
        top = max((n for _, n in pairs), default=0)
        if not met:
            out.append(_check("c_bandit_weights", "learned_class_weights non-empty once the sample floor is met",
                              None, f"data-starved: no class has >= {floor} outcomes in {window}d "
                              f"(max {top} across {len(pairs)} class(es)) — {{}} by construction"))
        else:
            lw = getattr(ws, "_learned_class_weights", None) if ws else None
            # each class costs its own DB connection inside brain_work_selector
            if _budget_left(ctx) <= _BANDIT_MIN_S:
                _spent(ctx, "db_refused", "pre-gate: _learned_class_weights", 0.0)
                out.append(_check("c_bandit_weights", "learned_class_weights non-empty once the sample floor is met",
                                  None, f"{len(met)} class(es) meet the floor ({floor}); weights "
                                  f"NOT read — under {_BANDIT_MIN_S}s of budget left and this read "
                                  f"opens one DB connection per class; unverified, not assumed"))
                return out
            weights, wwhy = _call(lw, met, _ctx=ctx)
            if not isinstance(weights, dict):
                out.append(_check("c_bandit_weights", "learned_class_weights non-empty once the sample floor is met",
                                  None, f"{len(met)} class(es) meet the floor; weights unreadable: {wwhy}"))
            else:
                live = {k: v for k, v in weights.items()
                        if isinstance(v, dict) and int(v.get("samples") or 0) > 0}
                out.append(_check("c_bandit_weights", "learned_class_weights non-empty once the sample floor is met",
                                  bool(live), f"{len(met)} class(es) meet the floor ({floor}); "
                                  f"{len(live)} carry a learned weight: {_short(live, 160)}"))

    status_fn = _import_attr("routes.brain_rag", "learn_station_status")
    if status_fn is not None:
        st, swhy = _call(status_fn, _ctx=ctx)
        out.append(_check("c_station_status", "learn_station_status() (part C, informational)",
                          None if st is None else True, _short(st) if st is not None else swhy))
    return out


# ── lane 4 — detectors-with-the-fix, MEASURED ─────────────────────────────

def _lane_detectors(ctx: dict) -> list:
    """★ A merge rule is a dashboard until something counts it.

    #3054 made "a brain PR carries a detector" a merge rule. This lane reads
    the count the public feed publishes (never re-derives it), confirms the
    three product detectors are still in scan_all()'s tuple through the SAME
    AST rule the gate uses (util.brain_detector_rule — never inlined: an inline
    copy is the drift this family of shells exists to retire), and publishes
    the recurrence rate next to its 7-day delta — published, not judged.
    """
    out = []
    feed = _feed(ctx)
    week = feed.get("week") if isinstance(feed, dict) else None
    if not isinstance(week, dict):
        out.append(_check("d_prs_with_detector", "brain_prs_with_detector readable for this week", None,
                          f"/api/v1/ops/claims week unreadable: {ctx.get('feed_why')}"
                          + (f"; feed error: {feed.get('error')}" if isinstance(feed, dict) else ""),
                          critical=True))
    else:
        det = week.get("brain_prs_with_detector")
        if not isinstance(det, dict):
            out.append(_check("d_prs_with_detector", "brain_prs_with_detector readable for this week", None,
                              "field absent from the feed — the instrument is absent, not zero",
                              critical=True))
        else:
            out.append(_check("d_prs_with_detector", "brain_prs_with_detector readable for this week", True,
                              f"with_detector={det.get('with_detector')} checked={det.get('checked')} "
                              f"unknown={det.get('unknown')} prs={det.get('prs')} "
                              f"(week of {week.get('week_start')}; basis: {str(det.get('basis'))[:90]})",
                              critical=True))

    registered_checks = _import_attr("util.brain_detector_rule", "registered_checks")
    radar_src = _read("routes/brain_consistency_radar.py")
    names, nwhy = (_call(registered_checks, radar_src, _ctx=ctx) if radar_src
                   else (None, "radar source unreadable"))
    if names is None:
        out.append(_check("d_sweep_tuple", "the three product detectors are in scan_all()'s tuple (AST)",
                          None, f"shared rule unavailable: {nwhy}"))
    else:
        missing = [n for n in PRODUCT_DETECTORS if n not in set(names)]
        out.append(_check("d_sweep_tuple", "the three product detectors are in scan_all()'s tuple (AST)",
                          not missing, f"{len(names)} registered; missing: {missing or 'none'} "
                          f"(util.brain_detector_rule.registered_checks, the gate's own reader)"))

    for name, issue in PRODUCT_DETECTORS.items():
        if _budget_left(ctx) <= _DETECTOR_MIN_S:
            _spent(ctx, "db_refused", f"pre-gate: brain_findings for {name}", 0.0)
            out.append(_check(f"d_fired_{name}", f"{name} has fired or reads measuring", None,
                              "not read — the shell's budget was spent; unverified "
                              "(overrunning would 503 through the edge)"))
            continue
        r = _q("SELECT COUNT(*), MAX(last_seen) FROM brain_findings WHERE issue = %s",
               (issue,), ctx=ctx)
        if r is None:
            # ★ Say WHICH. "unreadable" pointed at brain_findings for what was a
            # budget refusal inside _q(), and cost a reader a hunt for a missing
            # table. A read that did not happen must name why it did not.
            left = _budget_left(ctx)
            why = ("the shell's budget was spent before this read (%0.1fs left, "
                   "_q refuses at or under %0.1fs) — the table was never queried"
                   % (left, _QUERY_MIN_S)
                   if left is not None and left <= _QUERY_MIN_S
                   else "brain_findings unreadable")
            out.append(_check(f"d_fired_{name}", f"{name} has fired or reads measuring",
                              None, why))
        else:
            n, last = int(r[0][0] or 0), r[0][1]
            out.append(_check(f"d_fired_{name}", f"{name} has fired or reads measuring",
                              True if n else None,
                              f"fired: {n} finding(s) as '{issue}', last seen {last}" if n else
                              f"measuring — no '{issue}' finding yet (a detector that has not "
                              f"fired is not one that cannot)"))

    cv = _conv(ctx)
    if cv is None:
        out.append(_check("d_convergence_read", "convergence readable", None,
                          f"could not compute: {ctx.get('conv_why')}", critical=True))
    else:
        rate = cv.get("recurrence_rate")
        prior = _rate_7d_ago(ctx)
        delta = (round(rate - prior, 3) if isinstance(rate, (int, float))
                 and isinstance(prior, (int, float)) else None)
        ctx["recurrence"] = {"rate": rate, "rate_7d_ago": prior, "delta_7d": delta}
        out.append(_check("d_convergence_read", "convergence readable", True,
                          f"closed={cv.get('closed')} recurred={cv.get('recurred')} rate={rate} "
                          f"(30d)", critical=True))
        out.append(_check("d_recurrence_delta", "recurrence rate published with a 7-day delta (published, not judged)",
                          None, f"rate={rate} · 7d ago={prior} · delta={delta} (basis: this "
                          f"shell's daily snapshot ledger; null until a week of ticks exists)"))
    return out


_LANES = (
    ("1", "graduation", "action classes graduate on track record, not prompts", _lane_graduation),
    ("2", "human_queues", "every human queue has an age, a ceiling and a one-click decision", _lane_human_queues),
    ("3", "learn", "negative results are what the planner recalls", _lane_learn),
    ("4", "detectors_with_fix", "the detector merge rule is measured, not rebuilt", _lane_detectors),
)


# ── headline, decide-today, tick ──────────────────────────────────────────

def _headline(ctx: dict) -> dict:
    """The compounding metric row. Every field is null when its read failed —
    null is 'not measured', never 0."""
    feed = _feed(ctx)
    week = feed.get("week") if isinstance(feed, dict) else None
    week = week if isinstance(week, dict) else {}
    cv = _conv(ctx) or {}
    rec = ctx.get("recurrence") or {}
    rate = rec.get("rate", cv.get("recurrence_rate"))
    prior = rec.get("rate_7d_ago")
    return {
        "claims_confirmed": week.get("confirmed"),
        "refuted_kept": week.get("refuted_kept"),
        "retracted": week.get("retracted"),
        "granted_classes": week.get("granted_action_classes"),
        "recurrence_rate": rate,
        "recurrence_rate_7d_ago": prior,
        "recurrence_delta_7d": rec.get("delta_7d"),
        "week_start": week.get("week_start"),
        "basis": ("claims: /api/v1/ops/claims week cohort (routes.ops_claims.read_feed, "
                  "in-process) · granted: brain_action_classes WHERE granted · recurrence: "
                  "routes.squasher_queue.convergence(30) · delta: this shell's daily snapshot"),
    }


def _decide_today(ctx: dict, limit: int = 25) -> list:
    """The queue items, oldest decision first, each with its one-click URL —
    or decide_url null when no decision endpoint exists (that is a finding).

    ★ AN UNREADABLE INBOX IS NOT AN EMPTY ONE (2026-08-23). This list was built
    AFTER all four lanes, on whatever was left of the tick budget, and _q()
    returns None — never [] — when the budget is spent. `for r in rows or []`
    turned that None into no rows at all, so the ONE queue here that has a real
    one-click endpoint (/api/v1/brain/squasher/resolve) was the first thing to
    vanish, and it vanished SILENTLY: the board rendered "nothing to decide"
    where the truth was "I ran out of time to look".

    Measured on prod that morning: tick_ms=9398 against budget.seconds=11, so
    the read was refused; lane 2's own b_collapse_ratio — which runs earlier,
    inside the budget — counted 11 open rows in the same two statuses at the
    same moment. Eleven decisions, none of them on the decide-today list.

    ★ AND IT NO LONGER READS LAST (2026-08-23). Naming the omission made the
    board honest; it did not put the rows on the list. The inbox is now read
    ONCE per tick by _open_inbox() — lane 1 needs it, lane 2's collapse ratio
    needs it, and this list reuses what they already paid for, so the queue
    with the only real one-click endpoint is read INSIDE the budget instead of
    on what survives it. See _open_inbox for the three states of ctx.
    """
    items = []
    # Memoised: lanes 1 and 2 both need these rows and run first, so this is
    # normally free and INSIDE the budget. When neither lane got that far it
    # still tries here — and a refusal then is a refusal, reported as one.
    rows = _open_inbox(ctx)
    if rows is None:
        left = _budget_left(ctx)
        items.append({
            "kind": "unreadable", "id": "squasher_work_queue", "class": None,
            "title": ("inbox UNREADABLE this tick — %0.1fs of the %ss budget "
                      "left when the read was due; this is NOT a claim that "
                      "the inbox is empty"
                      % (left if left is not None else -1.0, READ_BUDGET_S)),
            "age_hours": None, "decide_url": None, "decide_payload": None,
            "class_url": None, "action_url": None})
        rows = []
    for r in rows[:int(limit)]:
        items.append({
            "kind": f"inbox:{r[I_STATUS]}", "id": r[I_ID],
            "title": str(r[I_TITLE] or "")[:120],
            "class": r[I_CLASS], "age_hours": _hours_since(r[I_REQUESTED]),
            "decide_url": "/api/v1/brain/squasher/resolve",
            "decide_payload": {"id": r[I_ID], "decision": "<your call>"},
            "class_url": ("/api/v1/brain/squasher/resolve-class"
                          if r[I_CLASS] else None),
            "action_url": r[I_ACTION_URL],
        })
    for cls in (_eligible_classes(ctx.get("graduation_report")) or []):
        items.append({"kind": "graduation", "id": cls, "title": f"Grant class {cls}?",
                      "class": cls, "age_hours": None,
                      "decide_url": "/api/v1/brain/squasher/grant",
                      "decide_payload": {"class": cls}, "class_url": None, "action_url": None})
    # ★ Only what is genuinely AWAITING a decision belongs on a decide-today
    # list. A retired card on this list is busywork that reads as a backlog.
    for w in (ctx.get("platform_withheld") or []):
        if "awaiting_decision" in w and not w.get("awaiting_decision"):
            continue
        # age_hours is published by the feed; _hours_since() is the fallback for
        # a feed that predates it. A DATE ("2026-07-29") has no clock, so
        # prefer the feed's own whole-day arithmetic over parsing it here.
        age_h = w.get("age_hours")
        if age_h is None:
            age_h = _hours_since(w.get("announced") or w.get("staged_at"))
        items.append({"kind": "platform_update", "id": w.get("id"),
                      "title": str(w.get("reason") or "")[:120], "class": None,
                      "age_hours": age_h,
                      "decide_url": (w.get("decision_url") or w.get("decide_url")
                                     or w.get("pr_url") or None),
                      "decide_payload": None, "class_url": None, "action_url": None})
        if sum(1 for i in items if i["kind"] == "platform_update") >= 10:
            break
    for r in (ctx.get("stale_recs") or [])[:10]:
        items.append({"kind": "strategic_rec:new", "id": r[0], "title": str(r[1] or "")[:120],
                      "class": None, "age_hours": _hours_since(r[2]),
                      "decide_url": None, "decide_payload": None, "class_url": None,
                      "action_url": None})
    return items[:limit + 20]


def _armed_filing(ctx: dict) -> dict:
    """The ONLY action this shell may take, and only under AGENTIC_LOOP_ARM=1:
    part B's graduation_report() filing, bounded to FILE_CAP_PER_DAY rows per
    UTC day by this shell's own ledger. Unreadable ledger = no budget."""
    if not _armed():
        return {"armed": False, "filed": 0, "budget": None,
                "note": "AGENTIC_LOOP_ARM unset — the tick reports only"}
    used = _filed_today(ctx)
    if used is None:
        return {"armed": True, "filed": 0, "budget": None,
                "note": "filing ledger unreadable — budget unknown, NOT filing"}
    budget = FILE_CAP_PER_DAY - used
    if budget <= 0:
        return {"armed": True, "filed": 0, "budget": 0,
                "note": f"daily cap reached ({used}/{FILE_CAP_PER_DAY})"}
    report, why = _graduation_report(file_rows=True, budget=budget, ctx=ctx)
    if report is None:
        return {"armed": True, "filed": 0, "budget": budget, "note": why}
    n = _filed_count(report, fallback=budget)
    _ledger_add(ctx, "graduation_file", n, {"budget": budget, "result": _short(report, 600)})
    return {"armed": True, "filed": n, "budget": budget,
            "over_budget": n > budget,
            "note": "filed via graduation_report()" + (" — OVER BUDGET, ledgered" if n > budget else "")}


def _tick(act: bool) -> dict:
    """act=False: pure report (the GET). act=True: the scheduled POST — also
    writes today's snapshot row and, under ARM, the bounded filing."""
    t0 = time.time()
    budget_s = TICK_BUDGET_S if act else READ_BUDGET_S
    deadline = time.monotonic() + budget_s
    _tc = time.time()
    ctx: dict = {"conn": _conn(), "deadline": deadline}
    _spent(ctx, "connect", "psycopg2.connect + session SET", (time.time() - _tc) * 1000)
    lanes = []
    raised = 0
    skipped = []
    try:
        for key, name, headline, fn in _LANES:
            if time.monotonic() >= deadline:
                # ★ NOT a failure and NOT a pass: unmeasured. Overrunning would
                # 503 through the CF worker, and a 5xx from Railway fails the
                # site over — see READ_BUDGET_S.
                skipped.append(key)
                checks = [_check(f"{key}_budget", "lane ran", None,
                                 f"not run — the shell's own {budget_s}s budget was "
                                 f"spent before this lane; unverified, NOT assumed fine "
                                 f"(overrunning would 503 through the edge)",
                                 critical=True)]
                lanes.append({"lane": key, "name": name, "headline": headline,
                              "verdict": _lane_verdict(checks), "checks": checks})
                continue
            try:
                checks = fn(ctx)
            except Exception as e:  # noqa: BLE001
                # A lane that raised is UNMEASURED, never green. Never let one
                # lane's failure 5xx the tick — see the kill-switch note.
                raised += 1
                logger.warning("[agentic_loop] lane %s raised: %s", key, e)
                checks = [_check(f"{key}_raised", "lane ran", None,
                                 f"{type(e).__name__}: {str(e)[:160]}", critical=True)]
            lanes.append({"lane": key, "name": name, "headline": headline,
                          "verdict": _lane_verdict(checks), "checks": checks})
        metrics = _headline(ctx)
        try:
            decide = _decide_today(ctx)
        except Exception as e:  # noqa: BLE001
            decide = [{"kind": "error", "title": f"{type(e).__name__}: {str(e)[:120]}"}]
        verdicts = [ln["verdict"] for ln in lanes]
        out = {
            "ok": True,
            "shell": "agentic_loop",
            "number": SHELL_NUMBER,
            "report_only": True,
            "acted": bool(act),
            "armed": _armed(),
            "db": ctx.get("conn") is not None,
            "summary": {"PASS": verdicts.count("PASS"), "FAIL": verdicts.count("FAIL"),
                        "?": verdicts.count("?")},
            "metrics": metrics,
            "decide_today": decide,
            "born_red": (
                "Measured 2026-08-22 before parts B and C landed: lane 1 had no candidate "
                "registry, lane 2's platform feed carried no age and no decision URL and no "
                "digest named the stale recs, lane 3's corpus did not exist. A green lane on "
                "day one would mean the invariant was written to fit the defect."),
            "reading": (
                "'?' is NOT a soft pass — the read failed, the sibling mechanism (part B/C) is "
                "not on this deploy, or there is nothing to judge yet; the lane is unverified. "
                "Lanes pin invariants, not values. The GET never acts and never beats; the "
                "POST tick writes only its snapshot row and the dead-man beat, plus part B's "
                "bounded filing under AGENTIC_LOOP_ARM=1."),
            "budget": {"seconds": budget_s, "lanes_not_run": skipped,
                        "why": ("worker.js ROUTE_TIMEOUTS DEFAULT is 15s for this "
                                "prefix and /api/v1/* GETs are retried on timeout; "
                                "a 5xx from Railway fails the site over to Render")},
            # a failed tick = NOTHING was measured (every lane raised or was cut
            # off by the budget); lanes that measured FAIL are a red board, not
            # a failed tick
            "tick_failed": (raised + len(skipped)) == len(_LANES),
            "lanes": lanes,
        }
        if act:
            snap_ok = _ensure_ledger(ctx) and _ledger_add(ctx, "tick", 1, metrics)
            out["snapshot"] = {"written": bool(snap_ok), "table": LEDGER_TABLE}
            try:
                out["graduation_filing"] = _armed_filing(ctx)
            except Exception as e:  # noqa: BLE001
                out["graduation_filing"] = {"armed": _armed(), "filed": 0,
                                            "note": f"{type(e).__name__}: {str(e)[:120]}"}
        out["tick_ms"] = int((time.time() - t0) * 1000)
        out["budget"]["spent"] = _spend_report(ctx, out["tick_ms"])
        return out
    finally:
        c = ctx.get("conn")
        if c is not None:
            try:
                c.close()
            except Exception:  # noqa: BLE001
                pass


# ── dead-man beat — ONE writer, status success|error, never warn ──────────

def _beat_status(ok: bool, tick_failed: bool) -> str:
    """The ledger status word for this tick. Pure, so it can be tested directly.

    THREE states, and the difference is load-bearing for OTHER monitors:

      success        every lane green.
      lanes_failing  the tick RAN and measured red lanes. The normal state of a
                     BORN RED board, and the word all ten sibling shells use.
      error          the tick itself failed — nothing was measured. A genuinely
                     broken producer.

    ★ WHY NOT JUST ok/not-ok (2026-09-01). batch-3 correctly stopped a
    "PASS 2 FAIL 2" run from beating `success`, but mapped the red board onto
    `error`. routes/ingestion_integrity_master_shell runs a producer_liveness
    lane asserting "no producer is reporting status=error"; this shell was the
    only feed of 199 reporting it, so ingestion-integrity-tick failed on 08-30
    and 08-31 on the word alone. A monitor whose red state is indistinguishable
    from a crash makes every other monitor lie.
    """
    if ok:
        return "success"
    return "error" if tick_failed else "lanes_failing"


def _beat_ledger(ok: bool, note: str, status: str | None = None) -> None:
    """Best-effort beat into the SHIPPED ingest_runs ledger. NEVER raises.

    `status` is the ledger vocabulary and defaults to the ok-derived pair.
    Callers pass "lanes_failing" for a tick that RAN and measured red lanes —
    see the caller for why that distinction is load-bearing.

    The house shape for a shell OMITS rows_inserted entirely (batch-3): a shell
    inserts no rows, and sending 0 on the failure path climbs ingest_runs'
    consecutive-zero counter toward a second, unrelated alarm.
    """
    try:
        body = json.dumps({
            "feed": FEED,
            "status": status or ("success" if ok else "error"),
            "cadence_hours": CADENCE_HOURS,
            "last_run": _now().isoformat(),
            "note": (note or "")[:280],
        }).encode()
        port = os.environ.get("PORT", "8080")
        admin_key = (os.environ.get("DCHUB_ADMIN_KEY")
                     or os.environ.get("DCHUB_INTERNAL_KEY")
                     or os.environ.get("ADMIN_API_KEY", ""))
        # requests, not urllib (regression_lint urllib-request-on-railway)
        requests.post("http://127.0.0.1:" + str(port) + "/api/v1/admin/ingest-runs/beat",
                      data=body, timeout=5,
                      headers={"Content-Type": "application/json",
                               "User-Agent": "dchub-agentic-loop-shell/1.0",
                               "X-Admin-Key": admin_key})
    except Exception as e:  # noqa: BLE001 — a beat error must never break the tick
        logger.debug("[agentic_loop] ledger beat failed: %s", e)


# ── routes ────────────────────────────────────────────────────────────────

# Admin GETs under /api/v1/admin/* are cached at the EDGE; /api/v1/brain/*
# carries the bypass. Belt and braces: no-store on every response anyway.
_NO_STORE = {"Cache-Control": "private, no-store, max-age=0",
             "Surrogate-Control": "no-store", "Pragma": "no-cache"}


@agentic_loop_master_shell_bp.route("/api/v1/brain/agentic-loop", methods=["GET"])
def agentic_loop_state():
    # ★ 404, never 5xx: the CF worker's proxyWithRetry reads ANY 5xx from
    # Railway as a dead origin and fails the whole site over to stale Render.
    if _disabled():
        return jsonify(ok=False, disabled=True, hint="AGENTIC_LOOP_SHELL_DISABLE=1"), 404
    if not _admin_ok():
        return jsonify(ok=False, error="admin key required"), 401
    # GET never acts, never beats.
    return jsonify(_tick(act=False)), 200, _NO_STORE


@agentic_loop_master_shell_bp.route("/api/v1/brain/agentic-loop/master-tick", methods=["POST"])
def agentic_loop_tick():
    if _disabled():
        return jsonify(ok=False, disabled=True, hint="AGENTIC_LOOP_SHELL_DISABLE=1"), 404
    if not _admin_ok():
        return jsonify(ok=False, error="admin key required"), 401
    try:
        out = _tick(act=True)
        s = out.get("summary") or {}
        # ★ batch-3/Screen D: `ok` used to be only `not tick_failed`, i.e.
        # "the tick did not RAISE". A run summarising "PASS 2 FAIL 2" beat
        # status=success and the board read this shell as healthy. Deriving
        # the status is not enough — it has to derive from the VERDICT.
        ok = not bool(out.get("tick_failed")) and not (s.get("FAIL") or 0)
        # ★2026-09-01: `ok` is right and its STATUS WORD was wrong. batch-3 above
        # correctly stopped a "PASS 2 FAIL 2" run beating success — but it mapped
        # the red board onto "error", and in this ledger "error" is reserved for a
        # producer that is BROKEN.
        #
        # That is not a wording preference. routes/ingestion_integrity_master_shell
        # runs a producer_liveness lane whose whole assertion is "no producer is
        # reporting status=error", and it went RED on this shell — so
        # ingestion-integrity-tick failed on 08-30 and 08-31 for no reason other
        # than the word. Measured on the live board 2026-09-01: this shell was the
        # ONLY feed of 199 reporting `error`; the ten sibling shells with failing
        # lanes all report `lanes_failing` (relay_closure:699, loop_control:809,
        # seven_levers:608 all use the identical
        # `"lanes_failing" if failing else "success"`). A monitor whose red state
        # is indistinguishable from a crash makes every OTHER monitor lie.
        #
        # Nothing is softened: lanes_failing is still non-success, still marks the
        # feed overdue on /api/v1/ops/deadman, and this shell stays red while its
        # lanes are red — which, being BORN RED, is the honest state.
        beat_status = _beat_status(ok, bool(out.get("tick_failed")))
        # ★ NAME the failing lanes. "PASS 2 FAIL 2 ? 0" said how many broke
        # and never which, so /ops/deadman could not triage this shell. The
        # counts are kept beside the names, not replaced by them.
        from routes.lane_triage import format_lane_verdicts
        _named = format_lane_verdicts(
            (ln.get("name"), ln.get("verdict")) for ln in (out.get("lanes") or []))
        note = (f"{_named} | PASS {s.get('PASS')} FAIL {s.get('FAIL')} "
                f"? {s.get('?')} | "
                f"filed {((out.get('graduation_filing') or {}).get('filed'))} | "
                f"rate {((out.get('metrics') or {}).get('recurrence_rate'))}")
    except Exception as e:  # noqa: BLE001
        out = {"ok": False, "shell": "agentic_loop", "number": SHELL_NUMBER,
               "error": f"{type(e).__name__}: {str(e)[:160]}", "tick_failed": True}
        ok = False
        beat_status = "error"      # the tick itself failed — a genuinely broken producer
        note = "tick raised: " + out["error"]
    _beat_ledger(ok, note, beat_status)
    logger.info("[agentic_loop] tick ok=%s %s", ok, note)
    # 200 even on a failed tick: the beat already said status=error, and a 5xx
    # here would fail the site over (see the kill-switch note).
    return jsonify(out), 200, _NO_STORE


@agentic_loop_master_shell_bp.route("/admin/agentic-loop", methods=["GET"])
def agentic_loop_board():
    if _disabled():
        return jsonify(ok=False, disabled=True, hint="AGENTIC_LOOP_SHELL_DISABLE=1"), 404
    if not _admin_ok():
        return jsonify(ok=False, error="admin key required"), 401
    t = _tick(act=False)
    colour = {"PASS": "#1a7f37", "FAIL": "#cf222e", "?": "#9a6700"}
    m = t.get("metrics") or {}

    def _v(x):
        return "?" if x is None else _esc(str(x))

    metric_row = "".join(
        f"<div style='padding:10px 14px;border:1px solid #d0d7de;border-radius:8px;min-width:120px'>"
        f"<div style='font-size:22px;font-weight:600'>{_v(val)}</div>"
        f"<div style='color:#57606a;font-size:12px'>{_esc(label)}</div></div>"
        for label, val in (("claims confirmed", m.get("claims_confirmed")),
                           ("refuted · kept", m.get("refuted_kept")),
                           ("retracted", m.get("retracted")),
                           ("granted classes", m.get("granted_classes")),
                           ("recurrence rate", m.get("recurrence_rate")),
                           ("Δ 7d", m.get("recurrence_delta_7d"))))
    rows = []
    for ln in t["lanes"]:
        checks = "".join(
            f"<li><b>{'PASS' if c['pass'] is True else 'FAIL' if c['pass'] is False else '?'}</b> "
            f"{_esc(str(c['name']))} — <span style='color:#57606a'>{_esc(str(c['detail']))}</span></li>"
            for c in ln["checks"])
        rows.append(
            f"<section style='margin:18px 0;padding:12px 14px;border:1px solid #d0d7de;border-radius:8px'>"
            f"<h3 style='margin:0 0 6px'>Lane {ln['lane']} — {_esc(ln['name'])} "
            f"<span style='color:{colour.get(ln['verdict'], '#57606a')}'>[{ln['verdict']}]</span></h3>"
            f"<div style='color:#57606a;margin-bottom:8px'>{_esc(ln['headline'])}</div>"
            f"<ul style='margin:0'>{checks}</ul></section>")
    decide = "".join(
        f"<tr><td>{_esc(str(d.get('kind')))}</td><td>{_esc(str(d.get('title')))}</td>"
        f"<td>{_v(d.get('age_hours'))}</td>"
        f"<td>{('<code>' + _esc(str(d.get('decide_url'))) + '</code>') if d.get('decide_url') else '<span style=color:#cf222e>no decision endpoint</span>'}</td></tr>"
        for d in (t.get("decide_today") or []))
    s = t["summary"]
    html = (
        "<!doctype html><meta charset='utf-8'>"
        f"<title>DC Hub — Agentic loop shell #{SHELL_NUMBER}</title>"
        "<div style='font:14px/1.5 -apple-system,Segoe UI,sans-serif;"
        "max-width:980px;margin:32px auto;padding:0 16px'>"
        f"<h1 style='margin:0'>Agentic loop master shell #{SHELL_NUMBER}</h1>"
        f"<p style='color:#57606a'>REPORT-ONLY · the GET never acts · "
        f"PASS {s['PASS']} · FAIL {s['FAIL']} · ? {s['?']} · armed={t.get('armed')}</p>"
        f"<div style='display:flex;gap:10px;flex-wrap:wrap;margin:12px 0'>{metric_row}</div>"
        f"<p style='color:#9a6700'>{_esc(t['born_red'])}</p>"
        f"<p style='color:#57606a'>{_esc(t['reading'])}</p>"
        + "".join(rows)
        + "<h2 style='margin-top:24px'>What to decide today</h2>"
        "<table style='border-collapse:collapse;width:100%' border='1' cellpadding='6'>"
        "<tr><th>kind</th><th>item</th><th>age h</th><th>one-click</th></tr>"
        + (decide or "<tr><td colspan=4>nothing waiting (or the queue was unreadable — see lane 2)</td></tr>")
        + "</table></div>")
    return html, 200, {"Content-Type": "text/html; charset=utf-8", **_NO_STORE}
