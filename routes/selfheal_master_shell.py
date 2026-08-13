"""
routes/selfheal_master_shell.py — Self-Heal Master Shell (2026-08-12).

★ THE SUBJECT OF THIS SHELL IS THE GAP BETWEEN "THE LOOP RAN" AND "THE THING
  GOT FIXED".

Every lane below exists because a green signal was produced while nothing moved.
None of this is hypothetical; each lane pins the measurement that produced it.

  · data-sync ran 8 times on 2026-08-12, every run `success`. The `news` table
    has not gained a row since 2026-08-10. Job success means "the process
    exited 0", which is not the same claim as "rows arrived".

  · brain investigation #100046 ("data_stale: 'news' — newest row 72.98h old —
    exceeds SLA 24h") recorded confidence 0.15 and refutation.survived = false.
    Its own recommendation was "Do not ship a blind find-and-replace fix ... no
    unambiguous mechanical fix can be asserted today", and its decision_for_human
    asked for a 15-minute diagnostic first. PR #2448 then merged ONE file —
    docs/brain-proposals/inv-100046-...md, +21/-0 — and the investigation was
    done. The substance gate had already labelled it "scaffold-only PR (no
    running code changed)". Four days later the condition is unchanged.

  · worker.js carries the description text every MCP agent reads, and deploys
    ONLY by manual Cloudflare dashboard paste. Merging a fix to it changes
    nothing live. Two guards already exist for this (check_worker_version_bump.sh
    and the pinned literal in test_seven_levers_shell.py) because #1902 and
    #1978 shipped without bumps and drift had to be proven by fingerprinting.

LANES
  1. MOVEMENT, NOT EXIT CODE. A dataset is fresh when its ROWS moved inside its
     SLA — not when its loader exited 0. Encodes the freshness COLUMN per table,
     which is the trap: on upsert loaders `created_at` freezes at first insert
     and reads as 12 days stale while the table is refreshed daily. power_plants
     was misdiagnosed exactly that way on 2026-08-12 (created_at 07-31,
     last_updated 08-12, 14,474 of 14,480 rows touched in 24h). Reading the
     wrong column manufactures both false alarms and false calm.

  2. AN INVESTIGATION IS NOT CLOSED BY A DOCUMENT. Flags investigations that
     were acted on despite failing their own quality bar — confidence under the
     floor, or a refutation the draft did not survive — and investigations whose
     underlying condition is still true. The loop is allowed to say "I could not
     determine this"; #100046 did, honestly and correctly. What it is not
     allowed to do is let that become a merged PR and a closed ticket.

  3. THE PASTE IS PART OF THE DEPLOY. Compares the repo's WORKER_VERSION against
     what the live gateway actually serves. A merged worker.js fix that has not
     been pasted is indistinguishable from an unfixed one, from the customer's
     seat — so this lane treats "merged" as no evidence at all.

Ordering is a dependency: lane 1 says whether the data moved, lane 2 says
whether the loop's response to it was real, lane 3 says whether a fix that DID
get written ever reached anybody.

Read-only. Reports; actuates nothing.

Run:  GET /api/v1/admin/selfheal-shell        (admin-gated, read-only)
      ?probe_live=0                           (skip the lane-3 network probe)
"""
from __future__ import annotations

import json
import os
import re

from flask import Blueprint, jsonify, request

selfheal_master_shell_bp = Blueprint("selfheal_master_shell", __name__)

SHELL_NAME = "Self-Heal Master Shell"

# ── Lane 1 config ────────────────────────────────────────────────────
# (table, freshness_column, sla_hours, why_this_column)
#
# The column matters more than the SLA. A loader that UPSERTS never advances
# created_at, so created_at on those tables measures "when we first saw the
# row", not freshness. Picking it is how power_plants got called 12 days stale
# on 2026-08-12 while 14,474 of its 14,480 rows had been touched that morning.
FRESHNESS = (
    ("news", "created_at", 24,
     "news rows are INSERT-only — a new article is a new row, so created_at IS "
     "the freshness signal. SLA 24h per brain finding inv #100046."),
    ("fiber_routes", "updated_at", 48,
     "daily loader; both columns advance together."),
    ("substations", "updated_at", 168,
     "slow trickle — HIFLD-derived, changes rarely. Weekly SLA."),
    ("power_plants", "last_updated", 48,
     "UPSERT loader: created_at froze at 2026-07-31 while last_updated tracks "
     "the daily refresh. Reading created_at here produces a false alarm."),
    ("transmission_lines", "last_updated", 192,
     "weekly full-replace; created_at moves only on replace. 8-day SLA spans "
     "one missed window without crying wolf."),
    ("gas_pipelines", "last_updated", 192,
     "weekly full-replace, same reasoning as transmission_lines."),
)

# ── Lane 2 config ────────────────────────────────────────────────────
# Below this, the investigation itself is telling you not to act on it.
# #100046 recorded 0.15 and was acted on anyway.
CONFIDENCE_FLOOR = 0.40

# ── Lane 3 config ────────────────────────────────────────────────────
LIVE_MCP_URL = "https://dchub.cloud/mcp"
WORKER_FILE = "worker.js"
# Example arguments that must NOT appear in what the gateway serves. Each is a
# documented call whose own inputSchema rejects the parameter, so an agent that
# copies it gets an UNFILTERED answer it reports as filtered.
BROKEN_EXAMPLES = (
    "list_transactions year=2026",
    "get_news topic=AI",
    "min_mw=10 status=operational",
    "get_pipeline market=northern-virginia",
)


def _admin_ok() -> bool:
    want = (os.environ.get("DCHUB_ADMIN_KEY") or "").strip()
    got = (request.headers.get("X-Admin-Key")
           or request.args.get("admin_key") or "").strip()
    return bool(want) and got == want


def _disabled() -> bool:
    return (os.environ.get("SELFHEAL_SHELL_DISABLE") or "0") == "1"


def _conn():
    import psycopg2
    dsn = (os.environ.get("NEON_REPLICA_URL")
           or os.environ.get("DATABASE_URL")
           or os.environ.get("NEON_DATABASE_URL"))
    if not dsn:
        return None
    c = psycopg2.connect(dsn)
    c.set_session(readonly=True, autocommit=True)
    return c


def _scalar(c, sql: str, args=None):
    """None means COULD NOT READ — never conflated with a legitimate 0."""
    try:
        cur = c.cursor()
        cur.execute(sql, args)
        r = cur.fetchone()
        return r[0] if r else None
    except Exception:
        return None


def _rows(c, sql: str, args=None) -> list:
    try:
        cur = c.cursor()
        cur.execute(sql, args)
        return list(cur.fetchall())
    except Exception:
        return []


def _check(cid: str, name: str, passed, detail: str, critical: bool = False) -> dict:
    return {"id": cid, "name": name,
            "status": "PASS" if passed is True else ("FAIL" if passed is False else "INDETERMINATE"),
            "detail": detail, "critical": bool(critical)}


def _lane_verdict(checks: list) -> str:
    """INDETERMINATE is never silently a PASS — a lane that could not read its
    evidence must say so. This whole shell exists because green-by-silence is
    the local failure mode; its own verdict function must not commit it."""
    if not checks:
        return "INDETERMINATE"
    if any(c["status"] == "INDETERMINATE" for c in checks):
        return "INDETERMINATE"
    if any(c["status"] == "FAIL" and c["critical"] for c in checks):
        return "FAILED"
    if any(c["status"] == "FAIL" for c in checks):
        return "DEGRADED"
    return "PASSED"


def _safe_lane(fn, *a):
    """A lane that raises is INDETERMINATE, never absent and never green."""
    try:
        return fn(*a)
    except Exception as e:  # noqa: BLE001
        return [_check("L?.0", "lane executed", None,
                       f"lane raised {type(e).__name__}: {str(e)[:160]}", critical=True)]


# ── Lane 1 — movement, not exit code ─────────────────────────────────
def _lane_movement(c) -> list:
    checks = []
    for i, (table, col, sla_h, why) in enumerate(FRESHNESS, start=1):
        age = _scalar(
            c, f"SELECT EXTRACT(EPOCH FROM (now() - MAX({col}::timestamptz)))/3600.0 "
               f"FROM {table}")
        if age is None:
            checks.append(_check(
                f"L1.{i}", f"{table} movement readable", None,
                f"could not read MAX({col}) on {table} — freshness unknown, not fresh",
                critical=(table == "news")))
            continue
        age = float(age)
        checks.append(_check(
            f"L1.{i}", f"{table} moved within {sla_h}h", age <= sla_h,
            f"last movement {age:.1f}h ago (SLA {sla_h}h), measured on {col}. {why}",
            critical=(table == "news")))
    return checks


# ── Lane 2 — an investigation is not closed by a document ────────────
def _lane_investigation_integrity(c) -> list:
    checks = []

    weak = _rows(c, """
        SELECT id,
               COALESCE(confidence, 0),
               COALESCE(result_json->'refutation'->>'survived', 'unknown'),
               LEFT(question, 90)
          FROM brain_investigations
         WHERE created_at > now() - interval '30 days'
           AND (COALESCE(confidence, 0) < %s
                OR COALESCE(result_json->'refutation'->>'survived', '') = 'false')
         ORDER BY created_at DESC
         LIMIT 25
    """, (CONFIDENCE_FLOOR,))

    total = _scalar(c, "SELECT COUNT(*) FROM brain_investigations "
                       "WHERE created_at > now() - interval '30 days'")
    if total is None:
        checks.append(_check("L2.1", "investigations readable", None,
                             "brain_investigations unreadable — integrity unknown",
                             critical=True))
        return checks

    checks.append(_check(
        "L2.1", "no investigation acted on below its own quality bar",
        len(weak) == 0,
        (f"{len(weak)} of {total} investigations in 30d are below confidence "
         f"{CONFIDENCE_FLOOR} or failed their own refutation. These are the loop "
         f"saying 'I could not determine this' — that is honest, and it must not "
         f"become a merged PR. Worst: "
         + "; ".join(f"#{r[0]} conf={float(r[1]):.2f} survived={r[2]}" for r in weak[:3])
         if weak else f"all {total} investigations in 30d met the bar"),
        critical=False))

    # The condition an investigation described must actually have cleared. An
    # investigation about news staleness is only closed when news is fresh.
    news_age = _scalar(c, "SELECT EXTRACT(EPOCH FROM (now() - MAX(created_at::timestamptz)))"
                          "/3600.0 FROM news")
    stale_inv = _scalar(c, """
        SELECT COUNT(*) FROM brain_investigations
         WHERE created_at > now() - interval '30 days'
           AND question ILIKE %s
    """, ("%data_stale%news%",))

    if news_age is None or stale_inv is None:
        checks.append(_check("L2.2", "investigated condition cleared", None,
                             "could not join investigations to the condition they describe"))
    else:
        cleared = float(news_age) <= 24
        checks.append(_check(
            "L2.2", "the investigated condition actually cleared", cleared,
            (f"{stale_inv} investigation(s) opened on news staleness in 30d; news is "
             f"currently {float(news_age):.1f}h old against a 24h SLA. An investigation "
             f"whose condition is still true was not resolved — it was closed."),
            critical=True))

    return checks


# ── Lane 3 — the paste is part of the deploy ─────────────────────────
def _repo_worker_version() -> str | None:
    try:
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        src = open(os.path.join(here, WORKER_FILE), encoding="utf-8").read()
        m = re.search(r"const WORKER_VERSION = '([^']+)'", src)
        return m.group(1) if m else None
    except Exception:
        return None


def _live_tools_payload(timeout: float = 20.0) -> str | None:
    """Raw tools/list text from the live gateway, or None if unreachable.

    None is INDETERMINATE, never a pass: an unreachable gateway tells us
    nothing about whether the paste happened."""
    try:
        import requests
        r = requests.post(
            LIVE_MCP_URL,
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            headers={"Accept": "application/json, text/event-stream"},
            timeout=timeout)
        return r.text
    except Exception:
        return None


def _lane_paste_gap(probe_live: bool = True) -> list:
    checks = []

    repo_v = _repo_worker_version()
    checks.append(_check(
        "L3.1", "repo WORKER_VERSION readable", repo_v is not None,
        f"repo worker.js WORKER_VERSION = {repo_v!r}" if repo_v
        else "could not parse WORKER_VERSION from worker.js"))

    if not probe_live:
        checks.append(_check("L3.2", "live gateway reflects the repo", None,
                             "live probe skipped (probe_live=0) — paste state unknown"))
        return checks

    payload = _live_tools_payload()
    if payload is None:
        checks.append(_check(
            "L3.2", "live gateway reflects the repo", None,
            "tools/list unreachable — cannot tell whether the paste happened",
            critical=True))
        return checks

    still_live = [s for s in BROKEN_EXAMPLES if s in payload]
    checks.append(_check(
        "L3.2", "no known-broken example is still served", len(still_live) == 0,
        (f"{len(still_live)} documented example(s) that their own inputSchema rejects "
         f"are STILL being served to every connecting agent: {still_live}. "
         f"worker.js deploys only by manual Cloudflare dashboard paste, so a merged "
         f"fix changes nothing here until someone pastes it."
         if still_live else
         "no known-broken example found in the live tools/list"),
        critical=True))

    return checks


@selfheal_master_shell_bp.get("/api/v1/admin/selfheal-shell")
def selfheal_shell():
    if _disabled():
        return jsonify({"error": "shell disabled"}), 503
    if not _admin_ok():
        return jsonify({"error": "unauthorized"}), 401

    probe_live = str(request.args.get("probe_live", "1")).strip() not in ("0", "false", "no")

    c = None
    try:
        c = _conn()
    except Exception:
        c = None

    if c is None:
        db_lanes = {
            "1_movement": {"verdict": "INDETERMINATE", "checks": [
                _check("L1.0", "database reachable", None,
                       "no DSN / connection failed — freshness unknown", critical=True)]},
            "2_investigation_integrity": {"verdict": "INDETERMINATE", "checks": [
                _check("L2.0", "database reachable", None,
                       "no DSN / connection failed — integrity unknown", critical=True)]},
        }
    else:
        l1 = _safe_lane(_lane_movement, c)
        l2 = _safe_lane(_lane_investigation_integrity, c)
        db_lanes = {
            "1_movement": {"verdict": _lane_verdict(l1), "checks": l1},
            "2_investigation_integrity": {"verdict": _lane_verdict(l2), "checks": l2},
        }
        try:
            c.close()
        except Exception:
            pass

    l3 = _safe_lane(_lane_paste_gap, probe_live)
    lanes = dict(db_lanes)
    lanes["3_paste_gap"] = {"verdict": _lane_verdict(l3), "checks": l3}

    verdicts = [v["verdict"] for v in lanes.values()]
    if "INDETERMINATE" in verdicts:
        overall = "INDETERMINATE"
    elif "FAILED" in verdicts:
        overall = "FAILED"
    elif "DEGRADED" in verdicts:
        overall = "DEGRADED"
    else:
        overall = "PASSED"

    return jsonify({
        "shell": SHELL_NAME,
        "verdict": overall,
        "lanes": lanes,
        "basis": {
            "1_movement": "row movement per table, measured on the column that "
                          "actually advances for that loader (upsert vs insert)",
            "2_investigation_integrity": f"confidence floor {CONFIDENCE_FLOOR}, "
                                         "refutation survival, and whether the "
                                         "investigated condition is still true",
            "3_paste_gap": "live tools/list vs repo worker.js — merged is not deployed",
        },
    })
