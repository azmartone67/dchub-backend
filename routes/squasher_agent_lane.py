"""routes/squasher_agent_lane.py — the squasher gets HANDS (2026-09-23).

WHY
===
Owner, 2026-09-23: "bug squasher doesnt truly fix issues it finds, it only
instructs". Measured on the live board that day: 0 fixes in 7d, last merge
never, and the queue's "decision" column read like a to-do list for a human —

    "Run `curl -i https://dchub.cloud/pricing` and capture the HTTP status..."
    "Grep the dchub-backend repo for the literal string 'SPA'..."
    "Fetch the raw HTTP response and the traceback ... and paste it back..."
    "Open full main.py, locate the /pricing route ... add the nav include"

Two causes, both structural, neither fixable by a better prompt:

  1. The investigator is ONE model call over evidence gathered in advance. It
     has no tools, so when the evidence is thin it hands its OWN next step —
     a curl, a grep, a file read — to a human as a "decision".
  2. The only code actuator is "one find string, exactly once, in one backend
     file" (squasher_queue.investigation_question). A fix that touches two
     files, or needs a test, or needs the file read past a window, is refused.

This lane takes the rows the one-shot lane handed off (awaiting_decision) or
gave up on (refused) and runs them through an AGENT that has a checkout,
read/grep/edit tools, a live-probe curl, and pytest — in GitHub Actions
(.github/workflows/squasher-agent-fix.yml), never inside this web process.
It does the investigation itself, and either opens a PR or comes back with a
human action it could NOT perform plus the evidence it gathered.

THE CONTRACT
============
  * Backend owns the ROW, the workflow owns the WORK. /agent/next claims one
    row atomically; /agent/result settles it. A claim the workflow never
    settles is reclaimed as `failed` after _STALE_RUNNING_MIN.
  * Only LIVE findings are claimed — the detector must still report the key
    (/api/v1/heal/findings, the read sweep_self_cleared trusts). An agent run
    costs real money; a finding that already self-cleared is not worth one.
    An unreadable or empty detector claims NOTHING (blind != clean).
  * QA reds are fed in too (2026-09-24): every claim first files the QA
    super-user board's actionable reds as source='qa' rows, claimed ahead of
    heal rows. Since 2026-09-25 a red is filed as soon as the brain's
    investigation of it is CURRENT and survived refutation — this lane is the
    one actor for QA reds, and QA's own auto-propose stands down while it is
    enabled. Reds QA's lane parked or failed on are filed as before; a red
    with a QA PR open or in flight is never filed. Liveness for them is the
    FULL fresh board (not the 4-per-hour slice /heal/findings carries), and
    this lane closes its own qa rows when a fresh board stops reporting them
    — squasher_queue's sweep never touches source='qa'.
  * Finding classes in AGENT_EXCLUDED_ISSUE_PREFIXES are skipped (today:
    operator_profile_gap — missing data, which no code change fixes).
  * Rows with an action_class are skipped: a granted class has its own
    verified actuator (squasher_action_classes) and a model must not race it.
  * One agent attempt per finding row; an infra `failed` may retry once.
  * One agent FIX per finding at a time (2026-09-25): a row is not claimed
    while another row for the same finding (QA: same family) has an agent PR
    open, or had one merged or closed in the last _RECENT_FIX_HOLD_H hours.
    Measured 2026-09-25: row 7 (/dc-hub-media) was fixed by be#5561, the
    finding came back as row 18, and the lane spent two more claims on it and
    opened be#5563 — a duplicate of the merged fix, closed by hand.
    Budget SQUASHER_AGENT_MAX_PER_DAY (default 4), counted from the ledger
    columns on the rows themselves.
  * NOTHING HERE MERGES. The workflow opens the PR (draft unless the repo
    variable SQUASHER_AGENT_AUTOMERGE is "1") and CI + review decide.
  * A fix is COUNTED only when the detector agrees. reconcile() marks a row
    `fixed` when its PR merged AND sweep_self_cleared later closed the row
    because the detector stopped reporting it — merge alone is not a fix.
    A merged PR whose finding is still live after _UNVERIFIED_AFTER_H is
    reported `merged_unverified`, back to a human, never quietly counted.

Surface (admin; under /api/v1/brain/ for the CF bypass rule):
  POST /api/v1/brain/squasher/agent/next        claim one row → brief | 204
  POST /api/v1/brain/squasher/agent/result      settle the claimed row
  POST /api/v1/brain/squasher/agent/reconcile   PR state → fixed/closed/...
  GET  /api/v1/brain/squasher/agent/status      summary + recent rows
Kill: SQUASHER_AGENT_DISABLE=1 (endpoints 404) · SQUASHER_QUEUE_DISABLE=1
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone

from flask import Blueprint, jsonify, request

logger = logging.getLogger(__name__)

squasher_agent_lane_bp = Blueprint("squasher_agent_lane", __name__)

AGENT_OUTCOMES = ("pr_opened", "needs_human", "not_reproducible", "failed")

# The rows this lane may take: what the one-shot lane handed to a human
# (awaiting_decision) or closed with no exit at all (refused). NOT queued —
# the cheap lane goes first — and NOT awaiting_ops: those name an admin
# endpoint, and the agent deliberately holds no admin key.
CLAIMABLE_STATUSES = ("awaiting_decision", "refused")

# Finding classes the agent never claims, keyed by the detector's own issue
# prefix (the live /heal/findings item's `issue`, or the queue row's title).
# Measured 2026-09-24: the queue's most re-observed rows were
# `operator_profile_gap:<operator>` (routes/brain_consistency_radar.py
# check_operator_profile_gap — "N facilities tracked but X% missing power_mw").
# Runs 35948754305 and 35951658519 each spent a claim confirming that the data
# is simply missing: no code change fixes it; it needs sourcing/enrichment. The
# rows stay in the queue for a human or an enrichment lane; the agent skips
# them so its daily budget reaches findings it can fix. Matched on the ISSUE,
# not the URL, so a real defect on an /operators/ page is still claimable.
AGENT_EXCLUDED_ISSUE_PREFIXES = {
    "operator_profile_gap:": "operator data gap — needs enrichment, not code",
}


def excluded_reason(row: dict, live_item: dict | None) -> str | None:
    """Why the agent must not claim this row, or None. Pure."""
    names = (str((live_item or {}).get("issue") or ""),
             str(row.get("title") or ""))
    for prefix, why in AGENT_EXCLUDED_ISSUE_PREFIXES.items():
        if any(n.startswith(prefix) for n in names):
            return why
    return None


# agent_state values that mean "leave this row alone".
_BUSY_STATES = ("running", "pr_open")

_STALE_RUNNING_MIN = 90       # the workflow's own timeout is 60 min
_UNVERIFIED_AFTER_H = 6       # merge → Railway deploy → next detector pass
_MAX_ATTEMPTS = 2             # 1 real attempt + 1 retry after an infra failure
_RECENT_FIX_HOLD_H = 24       # a merged/closed agent PR holds its finding this long
# agent_state values whose row carries a PR that already answered the finding.
_HOLD_STATES = ("pr_open", "fixed", "merged_unverified", "merged_after_clear",
                "cleared_unverified", "pr_closed")
# 2026-09-24: the agent may fix dchub-mcp-server too, so its PR can live there.
_PR_URL_RE = re.compile(
    r"^https://github\.com/azmartone67/(dchub-backend|dchub-mcp-server)/pull/(\d+)$")
_TEXT_CAP = 4000


def _disabled() -> bool:
    return (os.environ.get("SQUASHER_AGENT_DISABLE") == "1"
            or os.environ.get("SQUASHER_QUEUE_DISABLE") == "1")


def max_per_day() -> int:
    try:
        v = int(os.environ.get("SQUASHER_AGENT_MAX_PER_DAY", "4"))
    except ValueError:
        v = 4
    return max(0, min(v, 12))


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ── schema ───────────────────────────────────────────────────────────────

_AGENT_COLUMNS = (
    ("agent_state", "TEXT"),
    ("agent_attempts", "INTEGER NOT NULL DEFAULT 0"),
    ("agent_started_at", "TIMESTAMPTZ"),
    ("agent_finished_at", "TIMESTAMPTZ"),
    ("agent_verified_at", "TIMESTAMPTZ"),
    ("agent_run_url", "TEXT"),
    ("agent_pr_url", "TEXT"),
    ("agent_summary", "TEXT"),
    ("agent_evidence", "TEXT"),
)
_ENSURED = False


def _ensure_columns(cur) -> bool:
    """Once per process, savepoint-isolated, lock_timeout-bounded — the
    squasher_queue._ensure_table lessons (ALTER ... IF NOT EXISTS takes
    ACCESS EXCLUSIVE before it checks, and the nightly pg_dump holds
    AccessShare on every table). Never raises; a lost race retries next call.
    Runs on a DIRECT psycopg2 connection (squasher_queue._conn), so the DDL
    is not swallowed by db_utils' SKIP_DDL wrapper."""
    global _ENSURED
    if _ENSURED:
        return True
    from routes.squasher_queue import _ensure_table
    _ensure_table(cur)
    try:
        cur.execute("SAVEPOINT sq_agent_schema")
        cur.execute("SET LOCAL lock_timeout = '3s'")
        for col, typ in _AGENT_COLUMNS:
            cur.execute("ALTER TABLE squasher_work_queue "
                        f"ADD COLUMN IF NOT EXISTS {col} {typ}")
        cur.execute("RELEASE SAVEPOINT sq_agent_schema")
    except Exception as e:  # noqa: BLE001
        logger.info("[squasher_agent] schema deferred (%s: %s)",
                    type(e).__name__, str(e)[:120])
        try:
            cur.execute("ROLLBACK TO SAVEPOINT sq_agent_schema")
        except Exception:  # noqa: BLE001
            pass
        return False
    _ENSURED = True
    return True


def _conn():
    from routes.squasher_queue import _conn as q_conn
    return q_conn()


# ── the live detector read ───────────────────────────────────────────────

def live_findings() -> dict:
    """{ok, items: {key: item}} from /api/v1/heal/findings, or an honest
    refusal. Same source and same refusal rules as
    squasher_queue.live_finding_keys — but it keeps the ITEMS, because the
    agent's brief needs the detector's own detail/expected/deployed fields,
    not just the fact that the key is present."""
    try:
        from flask import current_app
        from routes.squasher_queue import _self_headers
        with current_app.test_client() as c:
            r = c.get("/api/v1/heal/findings", headers=_self_headers())
            if r.status_code != 200:
                return {"ok": False, "items": {},
                        "reason": f"heal/findings HTTP {r.status_code}"}
            d = r.get_json() or {}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "items": {},
                "reason": f"heal/findings unreadable: {type(e).__name__}"}
    if d.get("_warming_up"):
        return {"ok": False, "items": {}, "reason": "detector _warming_up"}
    items = {}
    for i in (list(d.get("actionable_backend_issues") or [])
              + list(d.get("actionable_frontend_issues") or [])):
        if isinstance(i, dict):
            k = str(i.get("url") or "").strip()
            if k:
                items.setdefault(k, i)
    if not items:
        return {"ok": False, "items": {},
                "reason": "detector returned no findings — blind reads as "
                          "clean from here, so nothing is claimed"}
    return {"ok": True, "items": items}


# ── QA super-user reds ───────────────────────────────────────────────────
#
# Owner 2026-09-24: "feed the agent failing QA reds too". Measured that day:
# QA reds already reach /heal/findings, but only QA_INTAKE_MAX (4) per hour,
# rotated, and nothing ever filed them into squasher_work_queue — so the agent
# could not see them unless someone clicked "Queue fix". QA also has its own
# auto-investigate → auto-propose lane (draft PRs, ONE attempt per finding
# ever); taking only what that lane has finished with means the two actors
# never both open a PR for one red.
#
# ★ 2026-09-25 — ONE ACTOR, NO WAIT. Waiting for QA's lane to give up cost a
# red at least investigate (run N) → propose (run N+1) → refusal → the next 3h
# claim slot: ~8-11h, and the proposer it waited on can only make one
# find-and-replace in one backend file. Now a red is filed the moment the
# brain's investigation is current and survived refutation (the SAME gate QA's
# proposer uses, imported, never copied), and qa_superuser_dashboard's
# auto-propose defers to this lane while it is enabled. A QA PR open or in
# flight still blocks filing, so a human-clicked proposal is never raced.

QA_PREFIX = "dchub://qa-superuser/"
QA_SOURCE = "qa"
_QA_FEED_MAX = 5            # rows filed per claim — a bounded trickle
_QA_CLEAR_MIN_AGE_H = 6     # a qa row must be this old before a clear counts
_SEV_RANK = {"critical": 0, "major": 1}


# ★ 2026-09-24 — ABSENCE IS NOT A PASS. The quota-contradiction check ROTATES the
# tool it probes every 4h block, and the tool is part of the finding key
# (stable_key("mcp", "anon", "quota-contradiction", <tool>)). Measured that day:
# the lane filed one row per tool (492 ai_capacity_index, 493
# get_grid_intelligence, 494 get_fiber_intel) — two agent PRs for ONE root cause
# (mcp#533, mcp#536) — and "cleared" rows whenever rotation moved on, which is
# not evidence of anything. So: rows are deduped by FAMILY (the check, without
# its variant), and a qa row closes only on a fresh board that shows a PASS in
# its family and no RED in it. A family the board says nothing about stays open.
_QA_PASS_MARK = "QA PASS-verified"


# ★ 2026-09-25 — A CHECK THAT CANNOT LOOK IS A PROBE DEFECT NOBODY OWNED.
# BLIND ("could not observe") is correctly never a product failure, so nothing
# routed it anywhere — and a check that is blind most of the time is an
# instrument that has stopped measuring. Measured that day: quota-meter carried
# "UNSTABLE 39x" (passing only when the runner's anon budget happened to allow
# it), paid-vs-anon was blind because the anon control had been spent, glama was
# unreadable. Each was a fix to tools/qa_superuser/, and none reached a fixer.
#
# So: a check FAMILY blind in at least _BLIND_SHARE of the last _BLIND_WINDOW
# canary-fired runs it appeared in (and in at least _BLIND_MIN_RUNS of them) is
# filed as ONE row keyed QA_BLIND_PREFIX + family, briefed as a PROBE fix — the
# product may be fine. It closes when the share drops below the line on a fresh
# history, i.e. the check observes again; that is positive evidence, not absence.
QA_BLIND_PREFIX = QA_PREFIX + "blind/"
_BLIND_WINDOW = 12          # runs — ~48h at the probe's 4h cadence
_BLIND_MIN_RUNS = 6         # the family must have been attempted this often
_BLIND_SHARE = 0.5
_BLIND_FEED_MAX = 1         # a probe fix per claim; reds come first
_QA_OBSERVED_MARK = "QA observes again"


def chronic_blind(runs: list[dict]) -> dict:
    """{QA_BLIND_PREFIX + family: info} for check families blind in most of
    the recent runs. Pure. runs are newest first, each {canary_fired,
    findings}; runs whose must-fail control did not fire are skipped, and only
    families present in the NEWEST trusted run count (a retired check is not
    a probe to fix)."""
    trusted = [r for r in (runs or []) if r.get("canary_fired")][:_BLIND_WINDOW]
    if not trusted:
        return {}
    newest = {}
    for f in trusted[0].get("findings") or []:
        if isinstance(f, dict) and f.get("key"):
            newest.setdefault(qa_family(f["key"]), f)
    seen, blind, last_blind = {}, {}, {}
    for r in trusted:
        fams = {}
        for f in r.get("findings") or []:
            if isinstance(f, dict) and f.get("key"):
                fam = qa_family(f["key"])
                # One vote per family per run; a run where ANY variant of the
                # family observed counts as observed.
                was = fams.get(fam)
                fams[fam] = (f if was is None or was.get("verdict") == "BLIND"
                             else was)
        for fam, f in fams.items():
            seen[fam] = seen.get(fam, 0) + 1
            if f.get("verdict") == "BLIND":
                blind[fam] = blind.get(fam, 0) + 1
                last_blind.setdefault(fam, f)
    out = {}
    for fam, n in seen.items():
        b = blind.get(fam, 0)
        if fam in newest and n >= _BLIND_MIN_RUNS and b / n >= _BLIND_SHARE:
            out[QA_BLIND_PREFIX + fam] = {"family": fam, "blind": b, "runs": n,
                                          "finding": last_blind[fam]}
    return out


def blind_item(key: str, info: dict) -> dict:
    """A chronic-blind family in /heal/findings item shape — the agent's
    detector_item. The brief says, in the detector's own fields, that the
    PROBE is the thing to fix."""
    f = info.get("finding") or {}
    return {"url": key,
            "issue": f"qa_blind chronic: {str(f.get('title') or '')[:150]}",
            "severity": "instrument", "verdict": "BLIND",
            "surface": f.get("surface"), "seat": f.get("seat"),
            "evidence": str(f.get("evidence") or "")[:2000],
            "basis": (f"the QA super-user check family {info.get('family')!r} "
                      f"could not observe its target in {info.get('blind')} of "
                      f"its last {info.get('runs')} runs. A blind reading is "
                      f"NOT a product failure — the probe is not measuring."),
            "remedy": ("Fix the PROBE in tools/qa_superuser/ so this check can "
                       "observe on most runs (e.g. pick a control that is not "
                       "spent, read a stable endpoint), or change what it "
                       "measures. Do not change product behaviour to satisfy "
                       "the check. If only a human can fix it (an external "
                       "registry is down), say needs_human and name the action."),
            "red_when": f.get("red_when"),
            "qa_key": f.get("key"), "chronic_blind": True}


def blind_feed_plan(blind: dict, open_keys) -> list[tuple[str, dict]]:
    """Chronic-blind families to file now: not already open, most-blind
    first, capped. Pure."""
    todo = [(k, i) for k, i in (blind or {}).items() if k not in open_keys]
    todo.sort(key=lambda ki: (-ki[1]["blind"] / max(ki[1]["runs"], 1), ki[0]))
    return todo[:_BLIND_FEED_MAX]


def blind_clear_plan(rows: list[dict], blind: dict,
                     now: datetime | None = None) -> list[int]:
    """Open chronic-blind rows whose family is no longer chronic on a fresh
    history — the check observes again. Pure; only call with a history
    qa_board() read successfully (an unreadable one clears nothing)."""
    now = now or _now()
    out = []
    for r in rows:
        key = str(r.get("finding_key") or "")
        if r.get("source") != QA_SOURCE or not key.startswith(QA_BLIND_PREFIX):
            continue
        if key in (blind or {}):
            continue
        at = r.get("requested_at")
        if at is not None and at.tzinfo is None:
            at = at.replace(tzinfo=timezone.utc)
        if at is None or now - at < timedelta(hours=_QA_CLEAR_MIN_AGE_H):
            continue
        out.append(r["id"])
    return out


def _recent_runs(limit: int = _BLIND_WINDOW * 2) -> list[dict] | None:
    """Newest-first {canary_fired, findings} from qa_superuser_runs, or None
    when unreadable. Twice the window, so skipped (canary-less) runs do not
    shrink it."""
    from routes import qa_superuser_dashboard as qd
    c = qd._conn()
    if c is None:
        return None
    try:
        with c.cursor() as cur:
            cur.execute("SELECT canary_fired, findings FROM qa_superuser_runs"
                        " ORDER BY generated_at DESC LIMIT %s", (limit,))
            return [{"canary_fired": bool(r[0]), "findings": r[1] or []}
                    for r in cur.fetchall()]
    except Exception as e:  # noqa: BLE001
        logger.warning("[squasher-agent] QA history unreadable: %s",
                       type(e).__name__)
        return None
    finally:
        try:
            c.close()
        except Exception:  # noqa: BLE001
            pass


def qa_family(key: str) -> str:
    """The check a QA key belongs to, without its variant: the first three
    `::` parts of the slug (surface::seat::check). Keys come from
    tools/qa_superuser/finding.stable_key(*parts) = "a::b::c[::variant]#hash"."""
    k = str(key or "")
    if k.startswith(QA_PREFIX):
        k = k[len(QA_PREFIX):]
    return "::".join(k.split("#", 1)[0].split("::")[:3])


def qa_board() -> dict:
    """{ok, reds: {dchub-key: finding}} — the latest QA board's ACTIONABLE
    reds with QA's own investigation/proposal/park state attached — or an
    honest refusal. Refuses (never "no reds") when the board is stale, its
    must-fail control did not fire, or the investigation table is unreadable:
    without that last one we cannot tell what QA's lane has handed off."""
    try:
        from routes import qa_superuser_dashboard as qd
        from routes.brain_qa_superuser_intake import run_refusal
        latest = (qd._load(limit=1) or {}).get("latest")
        why = run_refusal(latest)
        if why:
            return {"ok": False, "reds": {}, "reason": why}
        reds = [f for f in (latest.get("findings") or [])
                if isinstance(f, dict) and f.get("key")
                and qd.is_actionable_finding(f)]
        view = {"findings": reds}
        qd._attach_investigations(view)
        if any(f.get("investigation_unreadable") for f in reds):
            return {"ok": False, "reds": {},
                    "reason": "QA investigations unreadable — cannot tell "
                              "what QA's own lane has handed off"}
        allf = [f for f in (latest.get("findings") or [])
                if isinstance(f, dict) and f.get("key")]
        return {"ok": True,
                "reds": {QA_PREFIX + str(f["key"]): f for f in reds},
                # Positive evidence only: a family with a PASS on THIS board,
                # and every family with any RED (actionable or not) on it.
                "passed_families": {qa_family(f["key"]) for f in allf
                                    if f.get("verdict") == "PASS"},
                "red_families": {qa_family(f["key"]) for f in allf
                                 if f.get("verdict") == "RED"},
                # None → only reds QA's lane handed off are filed (the
                # pre-2026-09-25 rule): without the gate a refuted analysis
                # cannot be told from a sound one.
                "gate": _investigation_gate(),
                **_blind_view()}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "reds": {},
                "reason": f"QA board unreadable: {type(e).__name__}"}


def _blind_view() -> dict:
    """{blind, blind_known} for qa_board(). blind_known=False (history
    unreadable) files nothing and clears nothing — blind != clean."""
    runs = _recent_runs()
    if runs is None:
        return {"blind": {}, "blind_known": False}
    return {"blind": chronic_blind(runs), "blind_known": True}


def _investigation_gate():
    """tools.qa_superuser.propose.gate_investigation_detail, or None when the
    tools tree will not import (the auto-propose endpoint refuses on the same
    condition)."""
    try:
        from tools.qa_superuser.propose import gate_investigation_detail
        return gate_investigation_detail
    except Exception as e:  # noqa: BLE001
        logger.warning("[squasher-agent] QA gate unavailable: %s",
                       type(e).__name__)
        return None


def _qa_pr_in_flight(f: dict) -> bool:
    p = f.get("proposal") or {}
    return bool(p.get("pr_url")) or p.get("state") in ("opened", "running")


def qa_ready_basis(f: dict, gate=None) -> str | None:
    """Why this red may be filed for the agent now, or None. Pure.
    'handed_off' — QA's lane finished with it without a PR;
    'investigated' — the brain's investigation is current and survived
    refutation (gate = gate_investigation_detail). Never while a QA PR is
    open or in flight."""
    if _qa_pr_in_flight(f):
        return None
    if qa_handed_off(f):
        return "handed_off"
    if gate is not None and gate(f.get("investigation"))[0]:
        return "investigated"
    return None


def qa_handed_off(f: dict) -> bool:
    """Has QA's own lane FINISHED with this red without a PR? Pure.
    Parked (refuted / no recommendation) or its single PR attempt refused or
    errored — and no QA PR open or in flight."""
    p = f.get("proposal") or {}
    if p.get("pr_url") or p.get("state") in ("opened", "running"):
        return False
    if f.get("parked"):
        return True
    return p.get("state") in ("refused", "error")


def qa_item(key: str, f: dict) -> dict:
    """A QA red in /heal/findings item shape — the agent's detector_item."""
    sev = str(f.get("severity") or "")
    return {"url": key, "issue": f"qa_{sev} {str(f.get('title') or '')[:160]}",
            "severity": sev, "verdict": f.get("verdict"),
            "surface": f.get("surface"), "seat": f.get("seat"),
            "evidence": str(f.get("evidence") or "")[:2000],
            "red_when": f.get("red_when"), "basis": f.get("basis"),
            "remedy": f.get("remedy"), "failing_since": f.get("failing_since"),
            "qa_key": f.get("key")}


def merge_qa_items(items: dict, qa: dict) -> dict:
    """heal items + every current QA red (full board, not the capped slice).
    A refused board adds nothing. Pure."""
    out = dict(items or {})
    if (qa or {}).get("ok"):
        for k, f in (qa.get("reds") or {}).items():
            out[k] = qa_item(k, f)
        for k, info in (qa.get("blind") or {}).items():
            out[k] = blind_item(k, info)
    return out


def qa_feed_plan(reds: dict, open_keys, gate=None) -> list[tuple[str, dict]]:
    """Which reds to file now: ready (qa_ready_basis), not already open,
    critical first, capped. Pure."""
    todo = [(k, f) for k, f in (reds or {}).items()
            if k not in open_keys and qa_ready_basis(f, gate)]
    todo.sort(key=lambda kf: (_SEV_RANK.get(str(kf[1].get("severity")), 9), kf[0]))
    # One row per FAMILY: a check that rotates its variant is one defect.
    seen = {qa_family(k) for k in open_keys if str(k).startswith(QA_PREFIX)}
    out = []
    for k, f in todo:
        fam = qa_family(k)
        if fam in seen:
            continue
        seen.add(fam)
        out.append((k, f))
    return out[:_QA_FEED_MAX]


def qa_clear_plan(rows: list[dict], passed_families, red_families,
                  now: datetime | None = None) -> list[int]:
    """Open source='qa' rows a FRESH board shows PASSING: a PASS in the row's
    family and no RED in it, and the row old enough that one run is not a
    flap. A family the board is silent about is NOT cleared — absence is not
    a pass (a rotating check simply moved on). Pure; the caller must only pass
    families from a board qa_board() accepted."""
    now = now or _now()
    out = []
    for r in rows:
        if r.get("source") != QA_SOURCE:
            continue
        fam = qa_family(r.get("finding_key"))
        if fam not in passed_families or fam in red_families:
            continue
        at = r.get("requested_at")
        if at is not None and at.tzinfo is None:
            at = at.replace(tzinfo=timezone.utc)
        if at is None or now - at < timedelta(hours=_QA_CLEAR_MIN_AGE_H):
            continue
        out.append(r["id"])
    return out


# ── pure decisions (the unit under test) ─────────────────────────────────

def hold_key(key) -> str:
    """What one fix covers: a QA check's family, else the finding key."""
    k = str(key or "")
    return qa_family(k) if k.startswith(QA_PREFIX) else k


def fix_holds(rows: list[dict], now: datetime | None = None) -> dict:
    """{hold_key: holder row} for findings an agent PR already answered. Pure.
    rows carry id, finding_key, agent_state, agent_pr_url and `at` (when the
    answer landed: agent_verified_at, else agent_finished_at). An open PR
    holds at any age; a merged or closed one for _RECENT_FIX_HOLD_H — long
    enough for deploy + detector to settle, short enough that a fix which did
    not hold gets another agent attempt the next day."""
    now = now or _now()
    out = {}
    for r in rows or []:
        state = r.get("agent_state")
        if state not in _HOLD_STATES or not r.get("agent_pr_url"):
            continue
        if state != "pr_open":
            at = r.get("at")
            if at is None:
                continue
            if at.tzinfo is None:
                at = at.replace(tzinfo=timezone.utc)
            if now - at > timedelta(hours=_RECENT_FIX_HOLD_H):
                continue
        out.setdefault(hold_key(r.get("finding_key")), r)
    return out


def held_by(row: dict, holds: dict | None) -> dict | None:
    """The OTHER row whose agent PR holds this row's finding, or None."""
    h = (holds or {}).get(hold_key(row.get("finding_key")))
    return h if h and h.get("id") != row.get("id") else None


def pick_candidate(rows: list[dict], live_keys, now: datetime | None = None,
                   live_items: dict | None = None,
                   holds: dict | None = None) -> dict | None:
    """The first row this lane may claim, or None. Rows arrive ordered by
    priority (most re-observed first). Pure: every clause is a test.
    live_items ({key: detector item}) lets the exclusion read the detector's
    own issue name; without it only the row title is checked. holds is
    fix_holds() over the queue — a finding another row's agent PR already
    answered is not claimed again."""
    now = now or _now()
    live_items = live_items or {}
    for r in rows:
        if r.get("status") not in CLAIMABLE_STATUSES:
            continue
        if (r.get("action_class") or "").strip():
            continue
        if r.get("finding_key") not in live_keys:
            continue
        if held_by(r, holds):
            continue
        if excluded_reason(r, live_items.get(r.get("finding_key"))):
            continue
        state = r.get("agent_state") or ""
        attempts = int(r.get("agent_attempts") or 0)
        if state in _BUSY_STATES:
            continue
        if state == "failed":
            if attempts >= _MAX_ATTEMPTS:
                continue
        elif attempts >= 1:
            # needs_human / not_reproducible / pr_closed / merged_unverified:
            # the agent already gave its answer on this row.
            continue
        return r
    return None


def is_stale_running(started_at, now: datetime | None = None) -> bool:
    if started_at is None:
        return True
    now = now or _now()
    if started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=timezone.utc)
    return now - started_at > timedelta(minutes=_STALE_RUNNING_MIN)


def _clip(v, n: int = _TEXT_CAP) -> str:
    return str(v or "").strip()[:n]


def settle_plan(row: dict, payload: dict) -> tuple[dict | None, str]:
    """Map a workflow result to the row update, or (None, why-refused).

    ★ Checker-only closure: no outcome here closes a row. pr_opened and
      needs_human leave it OPEN (awaiting_decision) for a human or the
      detector; not_reproducible and failed leave status untouched so the
      self-clear sweep — which reads the detector — is what closes it."""
    if (row or {}).get("agent_state") != "running":
        return None, "row is not claimed by the agent lane (agent_state=%r)" % (
            (row or {}).get("agent_state"),)
    outcome = payload.get("outcome")
    if outcome not in AGENT_OUTCOMES:
        return None, f"outcome must be one of {AGENT_OUTCOMES}"
    summary = _clip(payload.get("summary"), 1200)
    evidence = payload.get("evidence") or []
    if not isinstance(evidence, list):
        evidence = [evidence]
    tests = payload.get("tests") if isinstance(payload.get("tests"), list) else []
    upd = {
        "agent_state": outcome if outcome != "pr_opened" else "pr_open",
        "agent_summary": summary,
        # Every field is capped BEFORE serialising, never the JSON after it:
        # slicing a JSON string stores something no reader can parse.
        "agent_evidence": json.dumps({
            "root_cause": _clip(payload.get("root_cause"), 1500),
            "evidence": [_clip(e, 600) for e in evidence[:12]],
            "tests": [_clip(t if isinstance(t, str) else json.dumps(t), 300)
                      for t in tests[:12]],
            "human_action": _clip(payload.get("human_action"), 800),
        }),
        "agent_run_url": _clip(payload.get("run_url"), 300) or None,
    }
    if outcome == "pr_opened":
        pr = _clip(payload.get("pr_url"), 200)
        if not _PR_URL_RE.match(pr):
            return None, "pr_opened needs a dchub-backend or dchub-mcp-server pull URL"
        upd.update(agent_pr_url=pr, pr_url=pr, status="awaiting_decision",
                   note=f"agent opened {pr} — {summary[:300]}")
    elif outcome == "needs_human":
        action = _clip(payload.get("human_action"), 800)
        if not action:
            return None, "needs_human must name the human_action"
        upd.update(status="awaiting_decision", decision=action,
                   note=f"agent investigated with tools; human action "
                        f"required: {action[:400]}")
    elif outcome == "not_reproducible":
        upd.update(note=f"agent could not reproduce it live: {summary[:300]} "
                        f"(the self-clear sweep closes it if the detector "
                        f"agrees)")
    else:
        upd.update(note=f"agent run failed: {summary[:300]}")
    return upd, ""


def reconcile_plan(row: dict, pr: dict | None, live_keys,
                   now: datetime | None = None, live_families=None
                   ) -> dict | None:
    """What a PR's state means for its row, or None for "nothing yet".

    pr is {"state": "open"|"closed", "merged_at": iso|None}, or None when
    GitHub was unreadable — which changes NOTHING (blind != a verdict)."""
    if pr is None or row.get("agent_state") != "pr_open":
        return None
    now = now or _now()
    if pr.get("state") == "open":
        return None
    merged_at = _parse_ts(pr.get("merged_at"))
    url = row.get("agent_pr_url") or ""
    if merged_at is None:
        return {"agent_state": "pr_closed",
                "note": f"agent PR {url} was closed without merging — the "
                        f"finding is back with a human"}
    if row.get("status") == "self_cleared":
        cleared = row.get("finished_at")
        if cleared is not None and cleared.tzinfo is None:
            cleared = cleared.replace(tzinfo=timezone.utc)
        is_qa = str(row.get("finding_key") or "").startswith(QA_PREFIX)
        reason = str(row.get("reason") or "")
        if (is_qa and _QA_PASS_MARK not in reason
                and _QA_OBSERVED_MARK not in reason):
            # Closed by the old absence rule (or by hand): no PASS was ever
            # observed, so the merge is not credited as a fix.
            return {"agent_state": "cleared_unverified",
                    "note": f"{url} merged, but this QA row was closed because "
                            f"the board stopped LISTING it (a rotating check "
                            f"moves on), not because it PASSED — not counted "
                            f"as a fix"}
        if cleared is not None and cleared > merged_at:
            return {"agent_state": "fixed", "status": "resolved",
                    "agent_verified_at": cleared,
                    "note": f"FIXED by {url}: merged {merged_at:%Y-%m-%dT%H:%MZ}"
                            f", then the detector stopped reporting it "
                            f"({cleared:%Y-%m-%dT%H:%MZ})"}
        return {"agent_state": "merged_after_clear",
                "note": f"{url} merged after the finding had already "
                        f"self-cleared — not counted as a fix"}
    key = row.get("finding_key")
    still_red = key in live_keys or (
        str(key or "").startswith(QA_PREFIX)
        and qa_family(key) in (live_families or set()))
    # A chronic-blind family is judged on a _BLIND_WINDOW-run share, which
    # cannot fall below the line until most of the window postdates the fix.
    wait_h = (_BLIND_WINDOW * 4 if str(key or "").startswith(QA_BLIND_PREFIX)
              else _UNVERIFIED_AFTER_H)
    if still_red and now - merged_at > timedelta(hours=wait_h):
        return {"agent_state": "merged_unverified",
                "note": f"{url} merged {merged_at:%Y-%m-%dT%H:%MZ} but the "
                        f"detector still reports the finding "
                        f"{wait_h}h later — the PR did not fix it"}
    return None


def rejection_row(row: dict) -> tuple | None:
    """The brain_review_decisions row a closed-unmerged agent PR stands for.

    ★ This is the lane's only REAL "no". A human looked at a draft the agent
    wrote and closed it — exactly the signal /brain/self-assessment has read
    as `rejection_signal: dead` (0 of 268 reviews, 2026-09-24) because every
    other brain PR is merged within the hour. Written through the canonical
    table so human_reviews_30d, check_rejection_skip() and the lessons
    compiler all see it; keyed on (finding_key, "") the way the merge
    reconciler keys a label-only rejection.

    None when the row carries no finding_key — a rejection nothing can look
    up is noise.
    """
    key = (row.get("finding_key") or "").strip()
    if not key:
        return None
    from routes.brain_learning import issue_hash
    url = row.get("agent_pr_url") or "?"
    return ("code", None, issue_hash(key, ""), key[:200], "reject",
            "github-close",
            (f"squasher agent PR {url} CLOSED WITHOUT MERGING — "
             f"queue row {row.get('id')}")[:500])


def _record_rejection(cur, row: dict) -> bool:
    """Best-effort, inside a SAVEPOINT so a failed insert cannot abort the
    reconcile transaction that just moved the row to pr_closed."""
    vals = rejection_row(row)
    if vals is None:
        return False
    try:
        cur.execute("SAVEPOINT agent_reject")
        # ON CONFLICT is the house idiom and inert here (the table has no
        # unique index); idempotency is _apply(where_state="pr_open").
        cur.execute("""
            INSERT INTO brain_review_decisions
                (proposal_kind, proposal_id, issue_hash, issue_label,
                 decision, reviewer, reviewer_note)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT DO NOTHING""", vals)
        cur.execute("RELEASE SAVEPOINT agent_reject")
        return True
    except Exception as e:  # noqa: BLE001
        try:
            cur.execute("ROLLBACK TO SAVEPOINT agent_reject")
        except Exception:
            pass
        logger.warning("[squasher-agent] rejection write failed: %s", str(e)[:160])
        return False


def _parse_ts(v):
    if not v:
        return None
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None


# ── DB paths ─────────────────────────────────────────────────────────────

_ROW_COLS = ("id", "finding_key", "title", "source", "status", "reason",
             "analysis", "decision", "confidence", "action_class",
             "seen_count", "requested_at", "finished_at", "agent_state",
             "agent_attempts", "agent_started_at", "agent_pr_url")
_SELECT = ("SELECT " + ", ".join(
    f"COALESCE({c}, 1)" if c == "seen_count" else c for c in _ROW_COLS)
    + " FROM squasher_work_queue")


def _rows(cur, where: str, params=(), limit: int = 200) -> list[dict]:
    cur.execute(f"{_SELECT} WHERE {where} "
                # COALESCE: `NULL = 'qa'` is NULL, and Postgres sorts NULLs
                # FIRST under DESC — a NULL-source row would outrank qa rows.
                "ORDER BY (COALESCE(source, '') = 'qa') DESC,"
                " COALESCE(seen_count, 1) DESC,"
                " requested_at ASC, id ASC"
                " LIMIT %s", (*params, limit))
    return [dict(zip(_ROW_COLS, r)) for r in cur.fetchall()]


def _used_24h(cur) -> int:
    cur.execute("SELECT COUNT(*) FROM squasher_work_queue "
                "WHERE agent_started_at > NOW() - INTERVAL '24 hours'")
    return int(cur.fetchone()[0] or 0)


def _apply(cur, row_id: int, upd: dict, *, where_state: str | None = None
           ) -> bool:
    note = upd.pop("note", "")
    sets, vals = [], []
    for k, v in upd.items():
        sets.append(f"{k} = %s")
        vals.append(v)
    if note:
        sets.append("reason = LEFT(%s || ' | ' || COALESCE(reason, ''), 600)")
        vals.append(note)
    sql = f"UPDATE squasher_work_queue SET {', '.join(sets)} WHERE id = %s"
    vals.append(row_id)
    if where_state is not None:
        sql += " AND agent_state = %s"
        vals.append(where_state)
    cur.execute(sql + " RETURNING id", vals)
    return cur.fetchone() is not None


def reclaim_stale(cur, now: datetime | None = None) -> int:
    n = 0
    for r in _rows(cur, "agent_state = 'running'"):
        if is_stale_running(r.get("agent_started_at"), now):
            if _apply(cur, r["id"], {
                    "agent_state": "failed", "agent_finished_at": _now(),
                    "note": "agent claim never settled (workflow died?) — "
                            "reclaimed as failed; one retry allowed"},
                    where_state="running"):
                n += 1
    return n


def _fix_holds(cur) -> dict:
    cur.execute(
        "SELECT id, finding_key, agent_state, agent_pr_url,"
        " COALESCE(agent_verified_at, agent_finished_at)"
        " FROM squasher_work_queue WHERE agent_pr_url IS NOT NULL"
        " AND agent_state IN (" + ", ".join("'%s'" % s for s in _HOLD_STATES)
        + ") AND (agent_state = 'pr_open' OR COALESCE(agent_verified_at,"
        " agent_finished_at) > NOW() - make_interval(hours => %s))"
        " ORDER BY id DESC LIMIT 500", (_RECENT_FIX_HOLD_H,))
    cols = ("id", "finding_key", "agent_state", "agent_pr_url", "at")
    return fix_holds([dict(zip(cols, r)) for r in cur.fetchall()])


def feed_qa(cur, qa: dict) -> dict:
    """File handed-off QA reds as source='qa' rows and close the qa rows a
    fresh board no longer reports. No-op on a refused board. Each insert has
    its own savepoint, so a lost race on the open-row index costs that row
    only."""
    out = {"filed": 0, "cleared": 0}
    if not (qa or {}).get("ok"):
        out["skipped"] = (qa or {}).get("reason") or "QA board not read"
        return out
    from routes.squasher_queue import _OPEN_STATUSES
    open_sql = ", ".join("'%s'" % st for st in _OPEN_STATUSES)
    cur.execute("SELECT finding_key FROM squasher_work_queue WHERE status IN ("
                + open_sql + ") AND LEFT(finding_key, %s) = %s",
                (len(QA_PREFIX), QA_PREFIX))
    open_keys = {r[0] for r in cur.fetchall()}
    # SAVEPOINT exists only inside a transaction block; on an autocommit
    # connection each statement is already isolated (the psycopg2
    # savepoint/autocommit trap squasher_queue._apply_schema_ddl documents).
    guarded = not getattr(getattr(cur, "connection", None), "autocommit", False)

    def insert(key, title, reason, analysis, decision, confidence):
        try:
            if guarded:
                cur.execute("SAVEPOINT sq_agent_qa")
            # Bare DO NOTHING (no target) honours the partial open-row unique
            # index without naming it; a lost race files nothing. One string,
            # status as a parameter: regression_lint reads an INSERT only up
            # to its first quote character.
            cur.execute(
                """INSERT INTO squasher_work_queue (finding_key, title, source,
                       status, reason, analysis, decision, confidence, last_seen)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NOW() ON CONFLICT DO NOTHING)
                   ON CONFLICT DO NOTHING RETURNING id""",
                (key, title[:200], QA_SOURCE, "awaiting_decision",
                 reason[:600], analysis, decision, confidence))
            filed = cur.fetchone() is not None
            if guarded:
                cur.execute("RELEASE SAVEPOINT sq_agent_qa")
            return filed
        except Exception:  # noqa: BLE001
            if guarded:
                cur.execute("ROLLBACK TO SAVEPOINT sq_agent_qa")
            return False

    gate = qa.get("gate")
    for key, f in qa_feed_plan(qa["reds"], open_keys, gate):
        inv = f.get("investigation") or {}
        prop = f.get("proposal") or {}
        if qa_ready_basis(f, gate) == "investigated":
            lead = "QA red, brain investigation current: "
            why = ("the brain's investigation is current and survived "
                   "refutation — filed straight to the agent lane")
        else:
            lead = "QA lane handed off: "
            why = ((f.get("parked") or {}).get("why") or prop.get("detail")
                   or prop.get("state") or "")
        if insert(key, qa_item(key, f)["issue"], lead + str(why),
                  str(inv.get("recommendation") or "")[:4000] or None,
                  str(why)[:1500] or None, inv.get("confidence")):
            out["filed"] += 1
    blind_known = bool(qa.get("blind_known"))
    if blind_known:
        out["blind_filed"] = 0
        for key, info in blind_feed_plan(qa.get("blind") or {}, open_keys):
            it = blind_item(key, info)
            if insert(key, it["issue"],
                      f"QA check chronically blind ({info['blind']} of "
                      f"{info['runs']} recent runs) — fix the probe, not the "
                      f"product", None, it["remedy"][:1500], None):
                out["blind_filed"] += 1
    else:
        out["blind_skipped"] = "QA run history unreadable"
    rows = _rows(cur, "source = %s AND status IN (" + open_sql + ")",
                 (QA_SOURCE,))
    for rid in qa_clear_plan(rows, qa.get("passed_families") or set(),
                             qa.get("red_families") or set()):
        if _apply(cur, rid, {"status": "self_cleared", "finished_at": _now(),
                             "note": f"self-cleared ({_QA_PASS_MARK}): a fresh "
                                     "QA board shows this check PASSING and no "
                                     "RED in its family (must-fail control "
                                     "fired). No fix is claimed here."}):
            out["cleared"] += 1
    if blind_known:
        for rid in blind_clear_plan(rows, qa.get("blind") or {}):
            if _apply(cur, rid, {"status": "self_cleared",
                                 "finished_at": _now(),
                                 "note": f"self-cleared ({_QA_OBSERVED_MARK}): "
                                         f"this check family is no longer "
                                         f"blind in {int(_BLIND_SHARE * 100)}%+"
                                         f" of its last {_BLIND_WINDOW} runs. "
                                         "No fix is claimed here."}):
                out["cleared"] += 1
    return out


def brief_of(row: dict, live_item: dict | None) -> dict:
    """Everything the agent is told. Plain data — the workflow renders it
    into the prompt as a fenced block, never as instructions."""
    return {
        "queue_id": row["id"],
        "finding_key": row["finding_key"],
        "title": row.get("title") or "",
        "status": row.get("status"),
        "seen_count": row.get("seen_count"),
        "first_seen": (row["requested_at"].isoformat()
                       if row.get("requested_at") else None),
        "detector_item": live_item or {},
        "prior_analysis": _clip(row.get("analysis"), 3000),
        "prior_decision_for_human": _clip(row.get("decision"), 1500),
        "prior_confidence": row.get("confidence"),
        "prior_reason": _clip(row.get("reason"), 600),
        # Compiled from every agent's verified outcomes and human reviews
        # (routes/brain_lessons.py). "" when this family has nothing to teach.
        "lessons": _lessons_for(row.get("finding_key")),
    }


def _lessons_for(key) -> str:
    try:
        from routes.brain_lessons import lessons_for
        return lessons_for(key).strip()
    except Exception:  # noqa: BLE001 — lessons must never block a claim
        return ""


def claim_next(live: dict | None = None, qa: dict | None = None) -> dict:
    """{ok, brief} | {ok, idle: reason}. Never raises."""
    live = live if live is not None else live_findings()
    if not live.get("ok"):
        return {"ok": True, "idle": f"detector unreadable: {live.get('reason')}"}
    qa = qa if qa is not None else qa_board()
    items = merge_qa_items(live["items"], qa)
    try:
        with _conn() as conn, conn.cursor() as cur:
            _ensure_columns(cur)
            reclaimed = reclaim_stale(cur)
            fed = feed_qa(cur, qa)
            if _used_24h(cur) >= max_per_day():
                conn.commit()
                return {"ok": True, "reclaimed": reclaimed, "qa": fed,
                        "idle": f"daily budget spent ({max_per_day()}/24h)"}
            where = "status IN (%s)" % ", ".join(
                "'%s'" % s for s in CLAIMABLE_STATUSES)
            rows = _rows(cur, where)
            holds = _fix_holds(cur)
            row = pick_candidate(rows, set(items), live_items=items,
                                 holds=holds)
            if not row:
                conn.commit()
                held = sum(1 for r in rows if held_by(r, holds))
                idle = "no live, unattempted hand-off rows"
                if held:
                    idle += (f" ({held} held: another row's agent PR already "
                             f"answered that finding)")
                return {"ok": True, "reclaimed": reclaimed, "qa": fed,
                        "held": held, "idle": idle}
            # Compare-and-set: two workflow runs cannot claim the same row.
            cur.execute(
                "UPDATE squasher_work_queue SET agent_state = 'running',"
                " agent_attempts = COALESCE(agent_attempts, 0) + 1,"
                " agent_started_at = NOW(), agent_finished_at = NULL"
                " WHERE id = %s AND COALESCE(agent_state, '') NOT IN"
                " ('running', 'pr_open') RETURNING id", (row["id"],))
            if cur.fetchone() is None:
                conn.commit()
                return {"ok": True, "idle": "lost the claim race"}
            conn.commit()
            return {"ok": True, "reclaimed": reclaimed,
                    "qa": fed,
                    "brief": brief_of(row, items.get(row["finding_key"]))}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:200]}"}


def settle(payload: dict) -> tuple[dict, int]:
    try:
        qid = int(payload.get("queue_id"))
    except (TypeError, ValueError):
        return {"ok": False, "error": "queue_id required"}, 400
    try:
        with _conn() as conn, conn.cursor() as cur:
            _ensure_columns(cur)
            rows = _rows(cur, "id = %s", (qid,), limit=1)
            if not rows:
                return {"ok": False, "error": "no such row"}, 404
            upd, why = settle_plan(rows[0], payload)
            if upd is None:
                return {"ok": False, "error": why}, 409
            upd["agent_finished_at"] = _now()
            if not _apply(cur, qid, upd, where_state="running"):
                conn.rollback()
                return {"ok": False, "error": "row changed under us"}, 409
            conn.commit()
            return {"ok": True, "queue_id": qid,
                    "agent_state": upd["agent_state"]}, 200
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:200]}"}, 500


def _fetch_pr(url: str) -> dict | None:
    """GitHub PR state, or None when unreadable. Uses the backend's
    GITHUB_TOKEN (the brain_pr_opener credential); read-only."""
    m = _PR_URL_RE.match(url or "")
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if not m or not token:
        return None
    try:
        import requests
        r = requests.get(
            f"https://api.github.com/repos/azmartone67/{m.group(1)}/pulls/{m.group(2)}",
            headers={"Authorization": f"Bearer {token}",
                     "Accept": "application/vnd.github+json"}, timeout=8)
        if r.status_code != 200:
            return None
        d = r.json()
        return {"state": d.get("state"), "merged_at": d.get("merged_at")}
    except Exception:  # noqa: BLE001
        return None


def reconcile(fetch_pr=None, live: dict | None = None,
              qa: dict | None = None) -> dict:
    fetch_pr = fetch_pr or _fetch_pr
    out = {"ok": True, "checked": 0, "changed": {}, "unreadable": 0}
    live = live if live is not None else live_findings()
    qa = qa if qa is not None else qa_board()
    # An unreadable detector must not produce merged_unverified: pass an
    # empty live set, which can only ever say "nothing yet". QA reds come
    # from the full fresh board (a refused board adds nothing), so a merged
    # PR whose QA red is still on the board is reported, not waited on.
    live_keys = (set(merge_qa_items(live.get("items") or {}, qa))
                 if live.get("ok") else set())
    # A QA family still red on ANY variant means the merged fix did not hold.
    live_families = {qa_family(k) for k in live_keys if k.startswith(QA_PREFIX)}
    try:
        with _conn() as conn, conn.cursor() as cur:
            _ensure_columns(cur)
            for row in _rows(cur, "agent_state = 'pr_open'", limit=50):
                out["checked"] += 1
                pr = fetch_pr(row.get("agent_pr_url") or "")
                if pr is None:
                    out["unreadable"] += 1
                    continue
                plan = reconcile_plan(row, pr, live_keys,
                                      live_families=live_families)
                if plan and _apply(cur, row["id"], dict(plan),
                                   where_state="pr_open"):
                    s = plan["agent_state"]
                    out["changed"][s] = out["changed"].get(s, 0) + 1
                    # Idempotent by construction: _apply(where_state=
                    # "pr_open") succeeds once per PR, so this runs once.
                    if s == "pr_closed" and _record_rejection(cur, row):
                        out["rejections_recorded"] = \
                            out.get("rejections_recorded", 0) + 1
            conn.commit()
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:200]}"}
    return out


def summary() -> dict:
    """For the portal. known=False when unreadable — never a zero."""
    try:
        with _conn() as conn, conn.cursor() as cur:
            _ensure_columns(cur)
            cur.execute(
                "SELECT COALESCE(agent_state, ''), COUNT(*) FROM squasher_work_queue"
                " WHERE agent_state IS NOT NULL GROUP BY 1")
            by_state = {k: int(v) for k, v in cur.fetchall()}
            cur.execute(
                "SELECT COUNT(*) FROM squasher_work_queue WHERE agent_state ="
                " 'fixed' AND agent_verified_at > NOW() - INTERVAL '7 days'")
            fixed_7d = int(cur.fetchone()[0] or 0)
            used = _used_24h(cur)
            recent = _rows(cur, "agent_state IS NOT NULL", limit=10)
        return {"known": True, "by_state": by_state,
                "verified_fixes_7d": fixed_7d, "used_24h": used,
                "max_per_day": max_per_day(),
                "recent": [{"id": r["id"], "finding_key": r["finding_key"],
                            "agent_state": r["agent_state"],
                            "pr_url": r.get("agent_pr_url")}
                           for r in recent]}
    except Exception as e:  # noqa: BLE001
        return {"known": False, "error": f"{type(e).__name__}: {str(e)[:120]}"}


# ── HTTP ─────────────────────────────────────────────────────────────────

def _gate():
    from routes.squasher_queue import _admin_ok, _no_store
    if _disabled():
        return _no_store(jsonify(ok=False, error="disabled")), 404
    if not _admin_ok():
        return _no_store(jsonify(ok=False, error="admin key required")), 401
    return None


def _reply(body: dict, code: int = 200):
    from routes.squasher_queue import _no_store
    return _no_store(jsonify(body)), code


@squasher_agent_lane_bp.post("/api/v1/brain/squasher/agent/next")
def agent_next():
    g = _gate()
    if g:
        return g
    d = claim_next()
    if d.get("ok") and not d.get("brief"):
        return _reply(d, 200)
    return _reply(d, 200 if d.get("ok") else 500)


@squasher_agent_lane_bp.post("/api/v1/brain/squasher/agent/result")
def agent_result():
    g = _gate()
    if g:
        return g
    body, code = settle(request.get_json(silent=True) or {})
    return _reply(body, code)


@squasher_agent_lane_bp.post("/api/v1/brain/squasher/agent/reconcile")
def agent_reconcile():
    g = _gate()
    if g:
        return g
    d = reconcile()
    return _reply(d, 200 if d.get("ok") else 500)


@squasher_agent_lane_bp.get("/api/v1/brain/squasher/agent/status")
def agent_status():
    g = _gate()
    if g:
        return g
    return _reply(summary(), 200)
