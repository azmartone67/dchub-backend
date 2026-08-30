"""routes/brain_site_qa_intake.py — the site-QA board ("website master") becomes brain work.

WHY
===
`routes/site_qa.py` is the website master: 28 synthetic tests over every public
page and API, run every 15 minutes by `site-qa.yml` (measured live: 2,520
results/24h). It opens a deduped alert per failing test in `site_qa_alerts`,
resolves it when the test passes again, and counts consecutive failures. It is
the most frequently-sampled instrument the platform has.

It has never reached the brain. Its outputs go to a GitHub issue (P0 only) and
an HTML dashboard, and stop there — the third instance of the same gap
`brain_audit_intake` and `brain_qa_superuser_intake` closed for their boards.

## ★★★ THIS BOARD HAS NO MUST-FAIL CONTROL, AND THAT CHANGES THE DESIGN

`brain_qa_superuser_intake`'s rule 0 is a CANARY gate: that board runs a
must-fail control every cycle, so "is this run capable of reporting a failure
at all?" has a real answer. **site_qa has no such control.** There is no
equivalent question this module can ask, and pretending otherwise would be the
worse error — a gate that reads like proof and is not.

So rule 0 here is two WEAKER, honestly-named substitutes:

  * BLAST RADIUS. If an implausible share of the suite is failing at once, a
    prober/network event is far likelier than every public surface breaking
    simultaneously. `site_qa` fans 28 self-probes through one ThreadPool
    against a single replica, so "everything red" is a shape it can produce on
    its own. We refuse the whole run rather than seed 20 p0s from one bad
    probe. This is NOT a canary: it cannot tell a real site-wide outage from a
    broken prober — it only refuses to guess between them, and says which.
  * STALENESS. Alerts from a harness that stopped running describe a platform
    that has since moved.

Both fail CLOSED when they cannot be computed (unreadable last-run time,
unknown suite size).

## ★★★ AND IT RECORDS ITS OWN CRASHES AS PLATFORM FAILURES

`site_qa._safe_run` catches a runner exception and stores it as
`status="fail"` at the test's declared severity, with the only marker being an
`error_detail` beginning "runner exception:" (which becomes the alert
`message`). So on this board OUR BUG AND THEIR OUTAGE ARE THE SAME ROW.

The QA super-user board makes this distinction natively (`instrument_fault`,
and BLIND is never RED). This one does not, so the intake must make it: a
runner exception is still real work — a broken prober is invisible anywhere
else — but it is labelled as OURS and ranked below confirmed platform failures.
Seeding it as a site defect would send the brain to fix dchub.cloud for a crash
in our own test harness.

The marker is a string convention across two modules, which is fragile, so
`test_brain_site_qa_intake` PINS it against site_qa.py's source: if the
producer's wording changes, that test fails instead of this lane silently
re-classifying our crashes as platform defects.

## The rest of the rules

1. p0/p1 only, and only after SITE_QA_INTAKE_MIN_FAILS (default 2) consecutive
   failures. On a 15-minute cadence a single failure is a 15-minute-old blip;
   seeding it writes a transient into a backlog that dedupes and persists.
   p2/p3 never reach a ~10/cycle model budget.
2. Capped (SITE_QA_INTAKE_MAX, default 3) and ROTATED, so this lane can
   neither starve the other detectors nor starve its own tail.
3. Never on the hot path: `/api/v1/heal/findings` reads a brain_state snapshot.

Kill switch: `SITE_QA_INTAKE_DISABLE=1` → `site_qa_findings()` returns [].
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone

from flask import Blueprint, jsonify, request

from routes.brain_intake_common import (age_hours, cycle_no, rotate_window,
                                        state_get, state_set)

brain_site_qa_intake_bp = Blueprint("brain_site_qa_intake", __name__)

_STATE_KEY = "site_qa_intake_snapshot"
_ISSUE_PREFIX = "siteqa_"

# The exact literal site_qa._safe_run puts in error_detail (-> alert.message)
# when OUR harness raised rather than the platform failing. Pinned by test.
RUNNER_EXCEPTION_MARKER = "runner exception:"

_SEVERITIES = ("p0", "p1")
_RANK = {"p0": 0, "p1": 1}
_RANK_FAULT = 2


def _disabled() -> bool:
    return os.environ.get("SITE_QA_INTAKE_DISABLE", "0") == "1"


def _int_env(name: str, default: int, lo: int = 0) -> int:
    try:
        return max(lo, int(os.environ.get(name, str(default))))
    except Exception:
        return default


def _max_rows() -> int:
    return _int_env("SITE_QA_INTAKE_MAX", 3)


def _ttl_s() -> int:
    return _int_env("SITE_QA_INTAKE_TTL_S", 3600, lo=600)


def _min_fails() -> int:
    return _int_env("SITE_QA_INTAKE_MIN_FAILS", 2, lo=1)


def _max_board_age_h() -> float:
    try:
        return max(0.25, float(os.environ.get("SITE_QA_INTAKE_MAX_AGE_H", "2")))
    except Exception:
        return 2.0


def _max_failing_ratio() -> float:
    """Above this share of the suite failing, refuse the run as an instrument
    event. 0.5 = more than half the site red at once."""
    try:
        v = float(os.environ.get("SITE_QA_INTAKE_MAX_FAILING_RATIO", "0.5"))
        return min(1.0, max(0.05, v))
    except Exception:
        return 0.5


def is_instrument_fault(alert: dict) -> bool:
    """Did OUR harness crash, rather than the platform failing?

    site_qa stores both as status='fail'; this marker is the only signal.
    """
    msg = str((alert or {}).get("message") or "")
    return msg.strip().lower().startswith(RUNNER_EXCEPTION_MARKER)


# ── rule 0: may we trust this board at all? ─────────────────────────────

def run_refusal(board: dict | None, max_age_h: float | None = None,
                max_ratio: float | None = None) -> str | None:
    """None if the board may be seeded from, else the REASON it may not be.

    Pure, so both gates are testable without a database.
    """
    if not board:
        return "no site-QA board could be read"

    age = age_hours(board.get("last_run_at"))
    limit = _max_board_age_h() if max_age_h is None else max_age_h
    if age is None:
        return ("site-QA last-run time is unreadable (last_run_at=%r) — cannot "
                "establish that these alerts are current"
                % (board.get("last_run_at"),))
    if age > limit:
        return ("site-QA last ran %.1fh ago (limit %.1fh) — its open alerts "
                "describe a platform that has since moved" % (age, limit))

    configured = board.get("tests_configured")
    if not isinstance(configured, int) or configured <= 0:
        # Without the suite size there is no blast radius to compute, and the
        # blast-radius gate is the only thing standing in for a canary here.
        return ("site-QA suite size is unknown — cannot compute blast radius, "
                "and this board has no must-fail control to fall back on")

    alerts = board.get("alerts") or []
    ratio = len(alerts) / float(configured)
    cap = _max_failing_ratio() if max_ratio is None else max_ratio
    if ratio > cap:
        return ("%d of %d site-QA tests are failing at once (%.0f%% > %.0f%% "
                "limit) — far likelier a prober or network event than every "
                "public surface breaking together. This board has no must-fail "
                "control, so this cannot be told apart from a real site-wide "
                "outage; it is refused rather than guessed"
                % (len(alerts), configured, ratio * 100, cap * 100))
    return None


# ── rule 1 + 2: pure selection ──────────────────────────────────────────

def _eligible(a: dict) -> bool:
    a = a or {}
    if is_instrument_fault(a):
        # Our own crash is real work regardless of the test's declared
        # severity, but it must still clear the anti-flap floor.
        pass
    elif str(a.get("severity") or "") not in _SEVERITIES:
        return False
    try:
        fails = int(a.get("consecutive_failures") or 0)
    except Exception:
        fails = 0
    return fails >= _min_fails()


def _rank(a: dict) -> int:
    if is_instrument_fault(a):
        return _RANK_FAULT
    return _RANK.get(str(a.get("severity") or ""), _RANK_FAULT)


def select_seedable(alerts: list, limit: int | None = None,
                    cycle: int | None = None) -> tuple[list, int]:
    """(rows to seed, how many eligible exist). Impact-ordered, rotated, capped."""
    limit = _max_rows() if limit is None else limit
    real = [a for a in (alerts or []) if _eligible(a or {})]
    real.sort(key=lambda a: (_rank(a), str(a.get("test_name") or "")))
    total = len(real)
    cyc = cycle_no(_ttl_s()) if cycle is None else cycle
    return rotate_window(real, limit, cyc), total


def to_findings(rows: list, board_as_of=None) -> list:
    """Open alerts → the {url, issue, count, detail} shape the heal endpoint's
    actionable_backend_issues list uses.

    Prefixed `siteqa_` so no FIX_MAP key matches it — the master-heal string
    replacer must never body-substitute one of these (same reasoning as the
    `audit_`, `qa_`, `asset_` and `contract_` prefixes). Identity is built from
    `test_name`, which site_qa already treats as the alert's unique key (unique
    partial index on open alerts), so the Layer-5 learn loop's (issue, url)
    dedupe sees one stable item per failing test rather than one per run.
    """
    out = []
    for a in rows or []:
        name = str((a or {}).get("test_name") or "").strip()
        if not name:
            continue
        fault = is_instrument_fault(a)
        kind = "fault" if fault else str(a.get("severity") or "?")
        meta = a.get("metadata") or {}
        if not isinstance(meta, dict):
            meta = {}
        since = age_hours(a.get("first_failed_at"))
        since_txt = (", failing for %.1fh" % since) if since is not None else ""
        fix = a.get("proposed_fix")
        out.append({
            "url": "dchub://site-qa/%s" % name,
            "issue": "%s%s %s" % (_ISSUE_PREFIX, kind, name[:240]),
            "count": 1,
            "detail": (
                "site-QA synthetic monitor: test `%s` has failed %sx "
                "consecutively%s (severity=%s, probed_url=%s, http=%s). %s"
                "Message: %s%s Board: /api/v1/qa/dashboard (board as of %s)."
                % (name, a.get("consecutive_failures"), since_txt,
                   a.get("severity"), meta.get("url"), meta.get("http_code"),
                   ("★ INSTRUMENT FAULT — this is OUR test harness raising, "
                    "not a platform defect; fix the probe, not the site. "
                    if fault else ""),
                   str(a.get("message") or "")[:400],
                   (" Proposed fix: %s." % str(fix)[:200]) if fix else "",
                   board_as_of or "unknown")),
        })
    return out


# ── snapshot refresh (reads the board, off the hot path) ────────────────

def _load_board() -> dict:
    """Open alerts + last run time + suite size. Fail-soft; never raises.

    Reads the DB directly rather than self-calling /api/v1/qa/*: a loopback
    HTTP call from the backend to its own public edge needs an internal key and
    burns a request slot on a single-replica box, which is the saturation
    site_qa itself was rewritten to stop causing.
    """
    board = {"alerts": [], "last_run_at": None, "tests_configured": None}
    try:
        from routes.site_qa import ALL_TESTS
        board["tests_configured"] = len(ALL_TESTS)
    except Exception:
        board["tests_configured"] = None
    url = (os.environ.get("NEON_DATABASE_URL")
           or os.environ.get("DATABASE_URL"))
    if not url:
        return board
    try:
        import psycopg2
        from psycopg2.extras import RealDictCursor
        with psycopg2.connect(url, connect_timeout=5) as conn, \
                conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT MAX(run_at) AS last_run_at FROM site_qa_results")
            row = cur.fetchone() or {}
            lr = row.get("last_run_at")
            board["last_run_at"] = lr.isoformat() if lr else None
            cur.execute(
                """SELECT test_name, severity, message, first_failed_at,
                          consecutive_failures, proposed_fix, metadata
                     FROM site_qa_alerts
                    WHERE resolved_at IS NULL""")
            alerts = []
            for r in (cur.fetchall() or []):
                d = dict(r)
                ff = d.get("first_failed_at")
                d["first_failed_at"] = ff.isoformat() if ff else None
                alerts.append(d)
            board["alerts"] = alerts
    except Exception:
        return board
    return board


def refresh_snapshot(force: bool = False, load_fn=None) -> dict:
    """Read the board and persist the seedable slice. Fail-soft."""
    if _disabled():
        return {"ok": True, "skipped": "SITE_QA_INTAKE_DISABLE=1"}
    prev = state_get(_STATE_KEY) or {}
    age = time.time() - float(prev.get("ts") or 0)
    if not force and prev and age < _ttl_s():
        return {"ok": True, "skipped": "fresh", "age_s": int(age),
                "rows": len(prev.get("rows") or [])}
    try:
        board = (load_fn or _load_board)() or {}
        base = {"ts": time.time(),
                "as_of": datetime.now(timezone.utc).isoformat(),
                "board_as_of": board.get("last_run_at"),
                "tests_configured": board.get("tests_configured"),
                "open_alerts": len(board.get("alerts") or []),
                "cycle": cycle_no(_ttl_s())}
        refusal = run_refusal(board)
        if refusal:
            # ★ Persist the refusal. Otherwise the previous (trusted) snapshot
            #   keeps serving findings from a board we have since decided not
            #   to trust — the stalest possible failure, and invisible.
            snap = dict(base, refused=refusal, eligible_total=0, rows=[])
            state_set(_STATE_KEY, snap)
            return {"ok": True, "refreshed": True, "refused": refusal,
                    "rows": 0}
        rows, eligible = select_seedable(board.get("alerts") or [])
        snap = dict(base, refused=None, eligible_total=eligible, rows=rows)
        state_set(_STATE_KEY, snap)
        # ★ NO SILENT CAPS: say what was left out.
        return {"ok": True, "refreshed": True, "rows": len(rows),
                "eligible_total": eligible,
                "deferred_to_next_cycle": max(0, eligible - len(rows)),
                "open_alerts": base["open_alerts"],
                "board_as_of": base["board_as_of"]}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)[:160]}


def site_qa_findings() -> list:
    """The heal endpoint's read: cached snapshot only, never a live board read."""
    if _disabled():
        return []
    try:
        snap = state_get(_STATE_KEY) or {}
        return to_findings(snap.get("rows") or [], snap.get("board_as_of"))
    except Exception:
        return []


# ── endpoints (admin) ───────────────────────────────────────────────────

def _admin_ok_local() -> bool:
    try:
        from routes.brain_mechanical_classifier import _admin_ok
        return bool(_admin_ok())
    except Exception:
        key = os.environ.get("DCHUB_ADMIN_KEY", "")
        sent = (request.headers.get("X-Admin-Key")
                or request.args.get("admin_key") or "")
        return bool(key) and sent == key


@brain_site_qa_intake_bp.get("/api/v1/brain/site-qa-intake")
def site_qa_intake_status():
    if not _admin_ok_local():
        return jsonify(ok=False, error="admin key required"), 401
    snap = state_get(_STATE_KEY) or {}
    seeded = to_findings(snap.get("rows") or [], snap.get("board_as_of"))
    eligible = snap.get("eligible_total")
    out = {"ok": True, "enabled": not _disabled(),
           "max_rows": _max_rows(), "ttl_s": _ttl_s(),
           "min_consecutive_failures": _min_fails(),
           "max_board_age_h": _max_board_age_h(),
           "max_failing_ratio": _max_failing_ratio(),
           "has_must_fail_control": False,
           "snapshot_as_of": snap.get("as_of"),
           "board_as_of": snap.get("board_as_of"),
           "tests_configured": snap.get("tests_configured"),
           "open_alerts": snap.get("open_alerts"),
           # When set, the lane is deliberately quiet — and saying so is the
           # point: a silent zero looks identical to a clean site.
           "refused": snap.get("refused"),
           "eligible_total": eligible,
           "cycle": snap.get("cycle"),
           "deferred_to_next_cycle": (max(0, eligible - len(seeded))
                                      if isinstance(eligible, int) else None),
           "seeded": seeded}
    resp = jsonify(out)
    resp.headers["Cache-Control"] = "no-store"
    return resp


@brain_site_qa_intake_bp.post("/api/v1/brain/site-qa-intake/refresh")
def site_qa_intake_refresh():
    if not _admin_ok_local():
        return jsonify(ok=False, error="admin key required"), 401
    result = refresh_snapshot(force=request.args.get("force") == "1")
    resp = jsonify(result)
    resp.headers["Cache-Control"] = "no-store"
    return resp
