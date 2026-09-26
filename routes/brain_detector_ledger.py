"""routes/brain_detector_ledger.py — did a detector RUN, and what did it report? (2026-09-13)

WHY. "The finding stopped firing" could not be observed. Measured 2026-09-13:
  · Nothing durable recorded that a detector ran. The consistency radar's set
    of completed detectors lived in process memory (_LAST_SWEEP) and died with
    the replica.
  · Every brain_findings writer call bumps last_seen, resolve and wont_fix
    included, and the radar's 24-hour arm resolves ANY open row not re-written
    for a day, whether or not its detector ran.
  · The live sweep at 03:58Z hit its 25s budget with 54 of 143 detectors still
    running, and check_iso_metric_dropped crashed on a statement timeout. Every
    one of those findings "stopped firing" in that sweep; none was fixed.

So this module keeps the evidence a closer needs, and judges it.

  ledger   One row per detector per recorded radar sweep: its outcome
           (completed | degraded | crashed | timeout | abandoned) and, for a
           completed detector, exactly which issue|url keys it reported.
           degraded = returned normally after _db() or _ro_conn() gave it no
           connection or one of its queries raised; crashed includes a
           detector that reported its own crash.
  verdict  firing | quiet_proven | quiet_unproven | unmeasured, for one finding.

quiet_proven requires ALL of:
  1. the finding is a consistency-radar row with one known detector_fn;
  2. no brain_findings row for it is open;
  3. that detector COMPLETED at least MIN_COMPLETED_RUNS times since it last
     reported the finding, over at least MIN_QUIET_DAYS. Both are measured
     inside the ledger, so nothing is proven quiet before the ledger is old
     enough;
  4. it also completed within RECENT_COMPLETION_HOURS. A detector that has
     started crashing proves nothing, however clean last week was;
  5. no completed run in that window hit the reported-keys cap;
  6. the detector is in ABSENCE_PROVABLE: reviewed to show that "did not report
     the target" means "the target is healthy" (its targets are not derived
     from the failing data, and it does not swallow errors into an empty list).
Anything else is quiet_unproven or unmeasured, and neither may close anything.

WRITE  brain_consistency_radar._persist_findings_to_db (full sweeps only) calls
       record_sweep(). One recorded sweep per LEDGER_MIN_INTERVAL_MIN across
       replicas; rows older than LEDGER_RETENTION_DAYS are pruned.
       Kill switch: BRAIN_DETECTOR_LEDGER_DISABLE=1.
READ   POST /api/v1/brain/spec-debt/finding-evidence (routes/brain_spec_debt.py)
       calls read_evidence(). The radar's resolve-on-absence arms
       (routes/brain_findings_resolve.py) query brain_detector_runs directly.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone

LEDGER_TABLE = "brain_detector_runs"
LEDGER_MIN_INTERVAL_MIN = 30
LEDGER_RETENTION_DAYS = 35
MAX_REPORTED_KEYS = 500

MIN_COMPLETED_RUNS = 6
MIN_QUIET_DAYS = 7
RECENT_COMPLETION_HOURS = 24
FIRING_HOURS = 48

FIRING, QUIET_PROVEN, QUIET_UNPROVEN, UNMEASURED = (
    "firing", "quiet_proven", "quiet_unproven", "unmeasured")

# detector_fn -> the reviewed reason its silence proves the target healthy.
# An entry is a claim about that function's code: name what was checked, when.
#
# ★ EMPTY ON PURPOSE (review of 2026-09-13, the detectors behind every open
#   spec-debt issue). None qualifies yet:
#   · _db() swallows a failed connection and most DB detectors then return [] —
#     indistinguishable from healthy. The ledger now records that as
#     "degraded", and a query that raised on such a connection too, but only
#     for connections made through _db() or _ro_conn().
#   · check_operator_profile_gap, check_repeated_404_patterns,
#     check_mcp_tool_sunset_candidate and check_cross_surface_value_drift pick
#     their targets from recent data (top-N, rolling windows, file:line keys),
#     so a target can drop out while still broken; several turn a query timeout
#     into [].
#   · check_iso_metric_dropped emits _zero_24h and _dropped for one url on
#     exclusive branches, so one key going quiet can mean the other took over;
#     its to_regclass probe also returns [] on error.
#   · check_facility_duplicate_clusters and check_cron_freshness report their
#     own crash as a finding and return normally (sweep_rows now counts that as
#     crashed), and use connections _db() does not see.
#   · detector_runtime_slow reads process-local timings.
#   Until an entry is added, the quiet arm classifies and never closes.
#
# ★ 2026-09-26 — check_iso_metric_dropped re-reviewed and ADDED. Each 09-13
#   objection, against the code on main that day:
#   · exclusive branches: _zero_24h and _dropped for one url are now judged as
#     one finding (SIBLING_ISSUES) — an open row or a report of EITHER key
#     keeps both from being proven quiet.
#   · failed queries: every query runs on a _db() connection, so a raise marks
#     the run degraded; the one path that returned [] without raising (no
#     grid_data table) now marks it degraded too.
#   · targets: every iso with ANY row in grid_data, not a recent window, and
#     nothing in this repo deletes grid_data rows (git grep, 2026-09-26). The
#     intermittent leash is finite: past it a stream is judged like any other.
ABSENCE_PROVABLE: dict[str, str] = {
    "check_iso_metric_dropped": (
        "reviewed 2026-09-26: targets are every iso with grid_data history; a "
        "failed query or missing table records the run degraded; its _zero_24h "
        "and _dropped findings are judged together; silence means the iso wrote "
        "3+ metrics in 24h, or is a registered intermittent stream inside its leash"),
}

# issue -> the other issues its detector reports for the same url INSTEAD of it.
# Going quiet on one of these can mean the other took over, so evidence_for reads
# all of them: any open row or completed report of a sibling blocks the proof.
SIBLING_ISSUES: dict[str, tuple] = {
    "iso_metric_count_zero_24h": ("iso_metric_count_dropped",),
    "iso_metric_count_dropped": ("iso_metric_count_zero_24h",),
}

_OUTCOME_RANK = {"completed": 0, "degraded": 1, "crashed": 2, "timeout": 3, "abandoned": 4}
_SELF_REPORTED_FAILURE = ("consistency_radar_detector_crashed:", "consistency_radar_detector_timeout:")
_OPEN_STATUSES = ("open", "escalated")

LEDGER_DDL = """
CREATE TABLE IF NOT EXISTS brain_detector_runs (
    id                 BIGSERIAL PRIMARY KEY,
    sweep_id           TEXT        NOT NULL,
    swept_at           TIMESTAMPTZ NOT NULL,
    detector_fn        TEXT        NOT NULL,
    outcome            TEXT        NOT NULL,
    reported           JSONB       NOT NULL DEFAULT '[]'::jsonb,
    reported_count     INTEGER     NOT NULL DEFAULT 0,
    reported_truncated BOOLEAN     NOT NULL DEFAULT FALSE,
    recorded_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
)
"""
LEDGER_INDEXES = (
    "CREATE INDEX IF NOT EXISTS brain_detector_runs_fn_swept "
    "ON brain_detector_runs (detector_fn, swept_at DESC)",
    "CREATE INDEX IF NOT EXISTS brain_detector_runs_swept "
    "ON brain_detector_runs (swept_at)",
)


def finding_key(issue, url) -> str:
    """The ledger's key for a finding: the truncation the radar persists with."""
    return f"{str(issue or '')[:200]}|{str(url or '')[:500]}"


# ── write ────────────────────────────────────────────────────────────────

def sweep_rows(sweep: dict, findings: list) -> list[dict]:
    """One ledger row per detector for one sweep. Pure.

    `sweep` carries completed_fns / crashed_fns / timeout_fns / abandoned_fns.
    A detector named in more than one list takes the WORST outcome, so evidence
    of absence only ever comes from a detector whose every signal says it
    finished. Only a completed detector's findings carry `_detector_fn`, so a
    reported key is never attributed to a detector that did not finish."""
    outcome_of: dict[str, str] = {}

    def worse(fn, outcome):
        fn = str(fn or "")
        if fn and (fn not in outcome_of
                   or _OUTCOME_RANK[outcome] > _OUTCOME_RANK[outcome_of[fn]]):
            outcome_of[fn] = outcome

    for outcome, field in (("completed", "completed_fns"), ("degraded", "degraded_fns"),
                           ("crashed", "crashed_fns"), ("timeout", "timeout_fns"),
                           ("abandoned", "abandoned_fns")):
        for fn in sweep.get(field) or []:
            worse(fn, outcome)
    reported: dict[str, set] = {}
    for f in findings or []:
        if not (isinstance(f, dict) and f.get("_detector_fn") and f.get("issue")):
            continue
        fn = str(f["_detector_fn"])
        # Some detectors catch their own failure, return it as a finding and
        # return normally, so the runner counts them completed. That run
        # proves nothing about their targets.
        if str(f["issue"]).startswith(_SELF_REPORTED_FAILURE):
            worse(fn, "crashed")
        reported.setdefault(fn, set()).add(finding_key(f.get("issue"), f.get("url")))
    rows = []
    for fn in sorted(outcome_of):
        keys = sorted(reported.get(fn, ())) if outcome_of[fn] == "completed" else []
        rows.append({"detector_fn": fn, "outcome": outcome_of[fn],
                     "reported": keys[:MAX_REPORTED_KEYS], "reported_count": len(keys),
                     "reported_truncated": len(keys) > MAX_REPORTED_KEYS})
    return rows


def record_sweep(cur, sweep: dict, findings: list) -> dict:
    """Write one sweep to the ledger. The caller owns the transaction.

    Throttled across replicas: an advisory lock serialises writers, and a sweep
    is recorded only if none was recorded in the last LEDGER_MIN_INTERVAL_MIN."""
    if os.environ.get("BRAIN_DETECTOR_LEDGER_DISABLE", "0") == "1":
        return {"recorded": 0, "skipped": "disabled"}
    sweep_id = str(sweep.get("sweep_id") or "")
    if not sweep_id or not sweep.get("at"):
        return {"recorded": 0, "skipped": "no sweep identity"}
    rows = sweep_rows(sweep, findings)
    if not rows:
        return {"recorded": 0, "skipped": "no detector outcomes"}
    cur.execute(LEDGER_DDL)
    for ddl in LEDGER_INDEXES:
        cur.execute(ddl)
    cur.execute("SELECT pg_try_advisory_xact_lock(hashtext('brain_detector_runs'))")
    if not (cur.fetchone() or (False,))[0]:
        return {"recorded": 0, "skipped": "another writer holds the ledger lock"}
    cur.execute("SELECT COALESCE(MAX(swept_at) > NOW() - make_interval(mins => %s), FALSE) "
                "FROM brain_detector_runs", (LEDGER_MIN_INTERVAL_MIN,))
    if (cur.fetchone() or (False,))[0]:
        return {"recorded": 0, "skipped": "throttled"}
    # One statement for the whole sweep. (sweep_id, detector_fn) is the natural
    # key, so re-recording a sweep — a retried persist — adds nothing.
    cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS brain_detector_runs_sweep_fn "
                "ON brain_detector_runs (sweep_id, detector_fn)")
    swept_at = datetime.fromtimestamp(float(sweep["at"]), tz=timezone.utc).isoformat()
    cur.execute("""
        INSERT INTO brain_detector_runs
               (sweep_id, swept_at, detector_fn, outcome, reported, reported_count,
                reported_truncated)
        SELECT sweep_id, swept_at, detector_fn, outcome, reported, reported_count,
               reported_truncated
          FROM jsonb_to_recordset(%s::jsonb) AS r(
               sweep_id text, swept_at timestamptz, detector_fn text, outcome text,
               reported jsonb, reported_count integer, reported_truncated boolean)
        ON CONFLICT (sweep_id, detector_fn) DO NOTHING
    """, (json.dumps([{"sweep_id": sweep_id, "swept_at": swept_at, **r} for r in rows]),))
    cur.execute("DELETE FROM brain_detector_runs "
                "WHERE swept_at < NOW() - make_interval(days => %s)", (LEDGER_RETENTION_DAYS,))
    return {"recorded": len(rows), "skipped": None}


# ── judge ────────────────────────────────────────────────────────────────

def _hours_since(ts, now: datetime):
    if ts is None:
        return None
    if isinstance(ts, str):
        ts = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return (now - ts).total_seconds() / 3600.0


def judge(rows: list[dict], stats: dict, ledger: dict, now: datetime) -> dict:
    """The verdict for one finding. Pure.

    rows    brain_findings rows for the issue+url: status, resolved_at,
            last_seen, detector, detector_fn
    stats   ledger stats for its detector_fn: last_reported, first_run,
            last_completed, completed_since, truncated_since
    ledger  {first_sweep, last_sweep, sweeps} for the whole ledger
    """
    def out(verdict, reason, detector_fn=None):
        return {"verdict": verdict, "reason": reason, "detector_fn": detector_fn}

    if not rows:
        return out(UNMEASURED, "no brain_findings row for this issue and url")
    open_rows = [r for r in rows
                 if str(r.get("status") or "open") in _OPEN_STATUSES and not r.get("resolved_at")]
    if any((_hours_since(r.get("last_seen"), now) or 1e9) <= FIRING_HOURS for r in open_rows):
        return out(FIRING, f"an open row was written in the last {FIRING_HOURS}h")
    detectors = {str(r.get("detector") or "") for r in rows}
    if detectors != {"consistency_radar"}:
        return out(UNMEASURED, "produced by " + ", ".join(sorted(d or "an unnamed detector"
                                                                for d in detectors))
                   + ", which the ledger does not record")
    fns = {str(r.get("detector_fn") or "") for r in rows} - {""}
    if len(fns) != 1:
        return out(UNMEASURED, "no detector_fn on its rows" if not fns
                   else "rows name more than one detector_fn: " + ", ".join(sorted(fns)))
    fn = next(iter(fns))
    last_reported = stats.get("last_reported")
    if last_reported is not None and _hours_since(last_reported, now) <= FIRING_HOURS:
        return out(FIRING, f"{fn} reported it in a completed run in the last {FIRING_HOURS}h", fn)
    if not ledger or ledger.get("first_sweep") is None:
        return out(UNMEASURED, "the detector ledger has no sweeps yet", fn)
    if stats.get("first_run") is None:
        return out(UNMEASURED, f"{fn} has no runs in the detector ledger", fn)
    closed_as = {str(r.get("status") or "") for r in rows} - set(_OPEN_STATUSES)
    if closed_as & {"wont_fix", "dismissed"}:
        return out(UNMEASURED, f"marked {', '.join(sorted(closed_as & {'wont_fix', 'dismissed'}))}"
                   " by policy or by hand — not evidence of a fix", fn)
    if open_rows:
        return out(QUIET_UNPROVEN, f"a row is still open, not re-written for {FIRING_HOURS}h+", fn)
    since = _hours_since(stats.get("last_completed"), now)
    if since is None or since > RECENT_COMPLETION_HOURS:
        return out(QUIET_UNPROVEN, f"{fn} has not completed a run in the last "
                   f"{RECENT_COMPLETION_HOURS}h", fn)
    if stats.get("truncated_since"):
        return out(QUIET_UNPROVEN, "a completed run in the window hit the reported-keys cap", fn)
    starts = [t for t in (last_reported, ledger.get("first_sweep")) if t is not None]
    quiet_days = min(_hours_since(t, now) for t in starts) / 24.0
    completed = int(stats.get("completed_since") or 0)
    if completed < MIN_COMPLETED_RUNS or quiet_days < MIN_QUIET_DAYS:
        return out(QUIET_UNPROVEN, f"{fn} completed {completed} run(s) over {quiet_days:.1f} day(s) "
                   f"without reporting it; proof needs {MIN_COMPLETED_RUNS} over "
                   f"{MIN_QUIET_DAYS}", fn)
    if fn not in ABSENCE_PROVABLE:
        return out(QUIET_UNPROVEN, f"{fn} completed {completed} runs over {quiet_days:.1f} days "
                   f"without reporting it, but it is not reviewed as absence-provable", fn)
    return out(QUIET_PROVEN, f"{fn} completed {completed} runs over {quiet_days:.1f} days "
               f"without reporting it ({ABSENCE_PROVABLE[fn]})", fn)


# ── read ─────────────────────────────────────────────────────────────────

_STATS_SQL = """
WITH d AS (
    SELECT swept_at, outcome, reported, reported_truncated
      FROM brain_detector_runs WHERE detector_fn = %(fn)s
), lr AS (
    SELECT MAX(swept_at) AS t FROM d WHERE outcome = 'completed' AND reported ?| %(keys)s
)
SELECT (SELECT t FROM lr) AS last_reported,
       MIN(swept_at) AS first_run,
       MAX(swept_at) FILTER (WHERE outcome = 'completed') AS last_completed,
       COUNT(*) FILTER (WHERE outcome = 'completed'
                          AND swept_at > COALESCE((SELECT t FROM lr), '-infinity'::timestamptz))
           AS completed_since,
       COALESCE(BOOL_OR(reported_truncated) FILTER (WHERE outcome = 'completed'
                          AND swept_at > COALESCE((SELECT t FROM lr), '-infinity'::timestamptz)),
                FALSE) AS truncated_since
  FROM d
"""


def _like_prefix(s: str) -> str:
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def evidence_for(cur, targets: list[dict], now: datetime | None = None) -> dict:
    """Read brain_findings + the ledger for each {issue, url, url_prefix} and
    judge it. `url_prefix` allows a unique prefix match for a url a title cut."""
    now = now or datetime.now(timezone.utc)
    cur.execute("SELECT to_regclass('public.brain_detector_runs') IS NOT NULL")
    ledger_exists = bool((cur.fetchone() or (False,))[0])
    ledger = {"exists": ledger_exists, "first_sweep": None, "last_sweep": None, "sweeps": 0}
    if ledger_exists:
        cur.execute("SELECT MIN(swept_at), MAX(swept_at), COUNT(DISTINCT sweep_id) "
                    "FROM brain_detector_runs")
        first, last, n = cur.fetchone() or (None, None, 0)
        ledger.update(first_sweep=first, last_sweep=last, sweeps=int(n or 0))
    cur.execute("SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'brain_findings'")
    cols = {r[0] for r in cur.fetchall()}
    fn_col = "detector_fn" if "detector_fn" in cols else "NULL"
    results = []
    for t in targets:
        issue, url = str(t.get("issue") or "")[:200], str(t.get("url") or "")[:500]
        select = (f"SELECT status, resolved_at, last_seen, detector, {fn_col}, url "
                  "FROM brain_findings WHERE issue = %s AND url ")
        cur.execute(select + "= %s", (issue, url))
        rows, match = cur.fetchall(), "exact"
        if not rows and t.get("url_prefix") and len(url) >= 12:
            cur.execute(select + "LIKE %s ESCAPE '\\'", (issue, _like_prefix(url)))
            rows, match = cur.fetchall(), "prefix"
            if len({r[5] for r in rows}) > 1:
                results.append({"issue": issue, "url": url, "match": "ambiguous",
                                "verdict": UNMEASURED, "detector_fn": None,
                                "reason": f"{len({r[5] for r in rows})} urls share that prefix"})
                continue
        if not rows:
            match = "none"
        siblings = SIBLING_ISSUES.get(issue, ())
        if rows and siblings and len({r[5] for r in rows}) == 1:
            # The detector reports ONE of these for a url; the others' rows and
            # reports count against this finding too (see SIBLING_ISSUES).
            cur.execute(select.replace("issue = %s", "issue = ANY(%s)") + "= %s",
                        (list(siblings), rows[0][5]))
            rows = rows + cur.fetchall()
        dict_rows = [{"status": r[0], "resolved_at": r[1], "last_seen": r[2],
                      "detector": r[3], "detector_fn": r[4]} for r in rows]
        stats = {}
        fns = {str(r["detector_fn"] or "") for r in dict_rows} - {""}
        if ledger["first_sweep"] is not None and len(fns) == 1:
            u = rows[0][5] if rows else url
            keys = [finding_key(i, u) for i in (issue, *siblings)]
            cur.execute(_STATS_SQL, {"fn": next(iter(fns)), "keys": keys})
            s = cur.fetchone()
            if s:
                stats = dict(zip(("last_reported", "first_run", "last_completed",
                                  "completed_since", "truncated_since"), s))
        v = judge(dict_rows, stats, ledger, now)
        results.append({"issue": issue, "url": url, "match": match, **v,
                        "evidence": {k: _iso(val) for k, val in stats.items()},
                        "rows": len(dict_rows)})
    return {"state": "MEASURED", "as_of": now.isoformat(),
            "ledger": {k: _iso(v) for k, v in ledger.items()},
            "policy": {"min_completed_runs": MIN_COMPLETED_RUNS, "min_quiet_days": MIN_QUIET_DAYS,
                       "recent_completion_hours": RECENT_COMPLETION_HOURS,
                       "firing_hours": FIRING_HOURS,
                       "absence_provable": sorted(ABSENCE_PROVABLE)},
            "findings": results}


def _iso(v):
    return v.isoformat() if isinstance(v, datetime) else v


def read_evidence(targets: list[dict]) -> dict:
    """evidence_for() on its own short-lived, read-only connection.

    Bounded: 5s to connect, and SET LOCAL statement_timeout inside a transaction
    (the pooler discards a session-level SET). Any failure is UNMEASURED — never
    an empty result that reads as "nothing is firing"."""
    db = os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL")
    if not db:
        return {"state": "UNMEASURED", "reason": "no DATABASE_URL"}
    import psycopg2
    conn = None
    try:
        conn = psycopg2.connect(db, sslmode="require", connect_timeout=5)
        conn.autocommit = False
        with conn.cursor() as cur:
            cur.execute("SET LOCAL statement_timeout = 4000")
            return evidence_for(cur, targets)
    except Exception as e:  # noqa: BLE001
        return {"state": "UNMEASURED", "reason": f"{type(e).__name__}: {str(e)[:200]}"}
    finally:
        if conn is not None:
            try:
                conn.rollback()
                conn.close()
            except Exception:  # noqa: BLE001
                pass
