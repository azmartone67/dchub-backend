"""routes/brain_findings_resolve.py — which open brain_findings rows a radar sweep may close. (2026-09-13)

WHY. Resolve-on-absence closed findings that were still live. After each full
radar sweep, brain_consistency_radar._persist_findings_to_db resolved:
  · a radar row not re-written for 2 minutes whose detector "completed" the sweep
    — including a detector that got no database, one whose query raised and was
    swallowed into [], and one that reported its own crash as a finding;
  · with that completed set read from _LAST_SWEEP, which the heal-findings refresh
    thread and the force-scan endpoint overwrite without persisting anything;
  · ANY open row, from any detector, not re-written for 24 hours, whether or not
    that detector had run at all.
Each reads "the detector did not look" as "the problem is gone", and the writer
books the inevitable reopen as a new episode.

THE RULE. A row closes only on evidence that its producer looked and did not see
it. Anything unprovable stays open: a fixed finding left open is a visible
nuisance, a live one closed is a silent lie.

  clean_absence  Radar rows. The detector ran CLEAN this sweep, and an earlier run
                 the detector ledger recorded after the row was last seen also ran
                 clean and did not report it. Clean is sweep_rows()' "completed":
                 returned normally, was read before the budget, reported no crash
                 of its own, and hit no connection failure or raised query on a
                 connection from _db() or _ro_conn(). Two clean absences, so a
                 detector that swallows some other failure once closes nothing.
  orphaned       Radar rows whose detector no longer exists: not submitted in this
                 sweep, in no ledger row for ORPHAN_AFTER_DAYS, and not seen for as
                 long. Nothing will ever look for them again.
  unattributed   Radar rows with no detector_fn — the Inspector's items, the
                 runner's scan_partial finding, rows older than provenance. Nothing
                 can show whether their producer ran, so the old 24h rule stands.
  foreign        Rows from other detectors: 24h without a re-write, and only while
                 that detector wrote some other row in the last 24h. A detector
                 that has gone quiet leaves its rows open.

A row whose `detector` is NULL or '' counts as a radar row: the writer sets that
column only on INSERT, so a radar row inserted before the radar declared itself
never gained it. Only the radar writes detector_fn.

Each arm is one statement in its own savepoint. The radar arms need the
detector_fn column; clean_absence and orphaned also need the ledger table.
"""
from __future__ import annotations

from routes._swallowed_writes import note_swallowed_write

ORPHAN_AFTER_DAYS = 7

LEDGER_PROBE_SQL = "SELECT to_regclass('brain_detector_runs') IS NOT NULL"

CLEAN_ABSENCE_SQL = """
    UPDATE brain_findings AS b
       SET status = 'resolved', resolved_at = NOW()
     WHERE b.status = 'open'
       AND COALESCE(NULLIF(b.detector, ''), 'consistency_radar') = 'consistency_radar'
       AND b.detector_fn = ANY(%(clean)s::text[])
       AND b.last_seen < NOW() - INTERVAL '2 minutes'
       AND EXISTS (
             SELECT 1
               FROM brain_detector_runs AS r
              WHERE r.detector_fn = b.detector_fn
                AND r.outcome = 'completed'
                AND NOT r.reported_truncated
                AND r.sweep_id <> %(sweep_id)s
                AND r.swept_at > b.last_seen
                AND NOT (r.reported ? (b.issue || '|' || b.url)))
"""

ORPHANED_SQL = """
    UPDATE brain_findings AS b
       SET status = 'resolved', resolved_at = NOW()
     WHERE b.status = 'open'
       AND COALESCE(NULLIF(b.detector, ''), 'consistency_radar') = 'consistency_radar'
       AND COALESCE(b.detector_fn, '') <> ''
       AND NOT (b.detector_fn = ANY(%(registered)s::text[]))
       AND b.last_seen < NOW() - make_interval(days => %(days)s)
       AND NOT EXISTS (
             SELECT 1
               FROM brain_detector_runs AS r
              WHERE r.detector_fn = b.detector_fn
                AND r.swept_at > NOW() - make_interval(days => %(days)s))
"""

UNATTRIBUTED_SQL = """
    UPDATE brain_findings
       SET status = 'resolved', resolved_at = NOW()
     WHERE status = 'open'
       AND COALESCE(NULLIF(detector, ''), 'consistency_radar') = 'consistency_radar'
       AND COALESCE(detector_fn, '') = ''
       AND last_seen < NOW() - INTERVAL '24 hours'
"""

FOREIGN_SQL = """
    UPDATE brain_findings
       SET status = 'resolved', resolved_at = NOW()
     WHERE status = 'open'
       AND COALESCE(NULLIF(detector, ''), 'consistency_radar') <> 'consistency_radar'
       AND last_seen < NOW() - INTERVAL '24 hours'
       AND detector IN (SELECT live.detector
                          FROM brain_findings AS live
                         WHERE live.last_seen >= NOW() - INTERVAL '24 hours')
"""


def plan(sweep: dict | None, findings: list, *, fn_col_live: bool,
         ledger_live: bool) -> list[tuple]:
    """The statements one full-sweep persist may run, as (arm, sql, params). Pure.

    `sweep` is the persisting sweep's own outcome record (the radar's
    _SWEEP_OUTCOMES[sweep_id]), or None when its findings do not all belong to
    one known sweep; `findings` are that sweep's findings."""
    arms: list[tuple] = []
    if fn_col_live:
        if sweep and sweep.get("sweep_id") and ledger_live:
            from routes.brain_detector_ledger import sweep_rows
            clean = sorted(row["detector_fn"] for row in sweep_rows(sweep, findings)
                           if row["outcome"] == "completed")
            if clean:
                arms.append(("clean_absence", CLEAN_ABSENCE_SQL,
                             {"clean": clean, "sweep_id": str(sweep["sweep_id"])}))
            registered = sorted({str(fn) for fn in sweep.get("registered_fns") or () if fn})
            if registered:
                arms.append(("orphaned", ORPHANED_SQL,
                             {"registered": registered, "days": ORPHAN_AFTER_DAYS}))
        arms.append(("unattributed", UNATTRIBUTED_SQL, None))
    arms.append(("foreign", FOREIGN_SQL, None))
    return arms


def resolve_absent(cur, sweep: dict | None, findings: list, *, fn_col_live: bool) -> dict:
    """Run plan()'s statements, each in its own savepoint: {arm: rows resolved}.

    The caller owns the transaction. Never raises — an arm that fails is rolled
    back alone and its rows stay open; if the plan itself cannot be built,
    nothing is resolved."""
    ledger_live = False
    if fn_col_live and sweep and _savepoint(cur, "bf_resolve_ledger_probe"):
        try:
            cur.execute(LEDGER_PROBE_SQL)
            ledger_live = bool((cur.fetchone() or (False,))[0])
            _release(cur, "bf_resolve_ledger_probe")
        except Exception:
            _rollback(cur, "bf_resolve_ledger_probe")
    try:
        arms = plan(sweep, findings, fn_col_live=fn_col_live, ledger_live=ledger_live)
    except Exception:
        note_swallowed_write("brain_findings", where="brain_findings_resolve.plan")
        return {}
    resolved: dict = {}
    for arm, sql, params in arms:
        name = f"bf_resolve_{arm}"
        if not _savepoint(cur, name):
            continue
        try:
            cur.execute(sql, params)
            resolved[arm] = max(cur.rowcount or 0, 0)
            _release(cur, name)
        except Exception:
            note_swallowed_write("brain_findings", where=f"brain_findings_resolve.{arm}")
            _rollback(cur, name)
    return resolved


def _savepoint(cur, name: str) -> bool:
    try:
        cur.execute(f"SAVEPOINT {name}")
        return True
    except Exception:
        return False


def _release(cur, name: str) -> None:
    try:
        cur.execute(f"RELEASE SAVEPOINT {name}")
    except Exception:
        pass


def _rollback(cur, name: str) -> None:
    try:
        cur.execute(f"ROLLBACK TO SAVEPOINT {name}")
    except Exception:
        pass
