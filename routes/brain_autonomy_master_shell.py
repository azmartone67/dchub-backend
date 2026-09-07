"""routes/brain_autonomy_master_shell.py — thinking → ACTING (2026-08-17).

The audit finding this shell exists to close: the brain files 324 findings and
51 proposals a week, and lands nothing without a human. The squasher portal
(routes/squasher_portal.py) MEASURES that gap; this shell is the first surface
allowed to CLOSE part of it — for exactly one class of work: DATA ACTUATORS.

WHY DATA ACTUATORS AND NOT CODE (the 2026-07-30 detector-hunt conclusion,
re-proven 08-17): the mechanical code-transform classes are exhausted (last
autofix PR 07-19), while the recurring finding classes are DATA/ops states —
unresolved news entities, re-grown deal duplicates, keeper-less dedup groups.
A bounded SQL repair with a dry-run, a rollback record and an invariant check
needs no PR, no merge gate, and cannot ship bad code. Every actuator here is
one a human already ran by hand at least once, verified end to end.

THREE LANES (the operator's 1-2-3 of 2026-08-17):
  1 · ACTUATORS — a vetted registry the tick may FIRE, each with a trigger
      metric (fires only when the defect is present), a hard daily budget,
      and a rollback record written BEFORE the mutation. Live fires happen
      only on POST; GET is always measure-only.
  2 · PROPOSAL LIFECYCLE — brain_enhancement_proposals sat at 137x
      status='proposed' / 0 anything else (79 distinct fingerprints — the
      book re-files itself like spec-debt does). The tick dedupes exact
      fingerprints (earliest wins), ranks survivors, and keeps EXACTLY the
      top 3 at status='queued' for the L22 handoff. Statuses only — this
      lane may never DELETE a row.
  3 · ACTIVATION LOOP — the one revenue motion that is armed end-to-end
      (ACTIVATION_NUDGE_ARM=1, CUSTOMER_WHITE_GLOVE_ACT=1, tick daily,
      capped 25/run, once-per-customer). This lane MEASURES the chain and
      goes red naming the broken link; it never sends anything itself —
      customer sends stay inside the existing gated sender.

SAFETY
  * GET = measure only. POST = measure + act, and only outside kill switches:
    BRAIN_AUTONOMY_SHELL_DISABLE=1 (whole shell) ·
    BRAIN_ACTUATORS_DISABLE=1 (lane 1 firing) ·
    PROPOSAL_TRIAGE_DISABLE=1 (lane 2 moves).
  * Budgets: each actuator max 1 live fire / 24h; max 3 live fires / 24h
    across all actuators (_budget_ok, checked per fire, from
    brain_actuator_runs — the log IS the budget ledger).
  * Every live fire writes a brain_actuator_runs row with the rollback
    payload BEFORE the mutation commits.
  * Heartbeat: stamps cron_last_run job 'brain_autonomy_tick' so
    brain_consistency_radar.check_cron_freshness watches this loop dying.

Surface: GET  /admin/brain-autonomy                       (HTML)
         GET  /api/v1/admin/brain-autonomy/master-tick    (JSON, measure)
         POST /api/v1/admin/brain-autonomy/master-tick    (JSON, measure+act)
Driver:  .github/workflows/brain-autonomy-daily.yml (POST, Railway origin —
         admin GETs through the edge are cached and 15s-timeout-limited).
"""
from __future__ import annotations

import json
import logging
import os
import time
from html import escape as _esc

from flask import Blueprint, Response, jsonify, request

from util.deals import DEALS_OK, deals_ok

logger = logging.getLogger(__name__)
brain_autonomy_master_shell_bp = Blueprint("brain_autonomy_master_shell", __name__)

# Budgets — the log table is the ledger, these are the ceilings.
ACTUATOR_DAILY_CAP_EACH = 1
ACTUATOR_DAILY_CAP_GLOBAL = 3

# ★ An UNDO is ledgered, but it is not an actuation. A rollback writes a
# brain_actuator_runs row so the reversal is auditable — and if that row
# counted against the daily budget, three undos would budget-lock the whole
# fleet for 24h, including the actuator you are trying to re-run correctly.
# ONE definition, written by rollback_actuator_id() and read back out by
# _budget_ok(), so the writer and the budget arm cannot drift apart.
ROLLBACK_SUFFIX = ":rollback"
DEALS_VICTIM_CAP = 200          # rows one deals fire may quarantine
TRIAGE_MOVES_CAP = 60           # status flips one triage pass may make
QUEUE_SIZE = 3

_JOB_NAME = "brain_autonomy_tick"
_JOB_INTERVAL_S = 86400


def _disabled() -> bool:
    return (os.environ.get("BRAIN_AUTONOMY_SHELL_DISABLE") or "").strip() == "1"


def _admin_ok() -> bool:
    sent = (request.headers.get("X-Admin-Key")
            or request.args.get("admin_key") or "").strip()
    expected = ((os.environ.get("DCHUB_ADMIN_KEY")
                 or os.environ.get("DCHUB_INTERNAL_KEY") or "").strip())
    return bool(sent) and sent == expected


def _conn():
    import psycopg2
    db = os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL")
    if not db:
        return None
    try:
        return psycopg2.connect(db, sslmode="require", connect_timeout=8)
    except Exception:
        return None


def _row(cur, sql, args=None):
    try:
        cur.execute(sql, args or ())
        return cur.fetchone()
    except Exception as e:
        logger.debug("[brain-autonomy] query failed: %s", str(e)[:140])
        try:
            cur.connection.rollback()
        except Exception:
            pass
        return None


def _check(cid, name, passed, detail, critical=False):
    return {"id": cid, "name": name, "pass": passed,
            "detail": detail, "critical": critical}


def _lane_pass(checks):
    crit = [c for c in checks if c.get("critical")]
    if any(c.get("pass") is False for c in crit):
        return False
    if crit and all(c.get("pass") is True for c in crit):
        return True
    return None


def _ensure_tables(c):
    try:
        with c.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS brain_actuator_runs (
                    id BIGSERIAL PRIMARY KEY,
                    actuator TEXT NOT NULL,
                    fired_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    live BOOLEAN NOT NULL DEFAULT FALSE,
                    trigger_n INT,
                    rows_affected INT,
                    ok BOOLEAN,
                    result JSONB,
                    rollback JSONB
                )""")
        c.commit()
    except Exception:
        try:
            c.rollback()
        except Exception:
            pass


def rollback_actuator_id(actuator: str) -> str:
    """The ledger id an UNDO of `actuator` is written under. _budget_ok
    excludes exactly these rows — see ROLLBACK_SUFFIX."""
    return f"{actuator}{ROLLBACK_SUFFIX}"


def _budget_ok(cur, actuator: str):
    """(each_ok, global_ok) against the run ledger — live fires only, and
    UNDOs are not fires: rows whose actuator ends in ROLLBACK_SUFFIX are
    excluded from BOTH arms so a reversal never spends the budget.
    (right(actuator, n) rather than LIKE: a literal % in a parameterised
    statement is a repo-wide landmine.)"""
    r = _row(cur, "SELECT COUNT(*) FILTER (WHERE actuator = %s), COUNT(*)"
                  " FROM brain_actuator_runs"
                  " WHERE live AND fired_at > NOW() - interval '24 hours'"
                  "   AND right(actuator, %s) <> %s",
             (actuator, len(ROLLBACK_SUFFIX), ROLLBACK_SUFFIX))
    if r is None:
        return (False, False)   # unreadable ledger = no budget, never fire
    return (int(r[0] or 0) < ACTUATOR_DAILY_CAP_EACH,
            int(r[1] or 0) < ACTUATOR_DAILY_CAP_GLOBAL)


# ── lane 1 · the vetted actuator registry ─────────────────────────────
#
# Each actuator: trigger(cur) -> int (the defect count; 0 = nothing to do),
# fire(conn, cur, trigger_n) -> dict(rows_affected=…, rollback=…, …).
# The fire is only reached when trigger > 0, budgets pass, kill switches are
# off, and the request is a POST. Rollback data is collected BEFORE the
# mutation and stored on the run row.

def _trigger_entity_blindspot(cur):
    """Shell #36 lane 5a's number. The query itself is RESIDENT in
    news_entity_extraction beside the stoplist — restating it here is what
    let it rot unnoticed in two places at once. None = UNMEASURED, and
    _lane_actuators will not fire blind on it."""
    try:
        from routes.news_entity_extraction import entity_blindspot_count
    except Exception as e:
        logger.debug("[brain-autonomy] blindspot import failed: %s",
                     str(e)[:140])
        return None
    return entity_blindspot_count(cur)


def _fire_entity_reresolve(conn, cur, trigger_n):
    """FALSE→TRUE only, via the scan's OWN matcher — no rollback payload
    needed: in_facilities=TRUE is derived from the facilities tables (ground
    truth), and the scan would re-derive it on the next mention anyway."""
    from routes.news_entity_extraction import _reresolve_unmatched
    res = _reresolve_unmatched(conn, cap=300)
    return {"rows_affected": int(res.get("resolved") or 0),
            "rollback": None, "result": res,
            "ok": "error" not in res}


_DEAL_COLS = None


def _deal_cols():
    global _DEAL_COLS
    if _DEAL_COLS is None:
        from routes.graph_spine_master_shell import _DEAL_BUSINESS_COLS
        _DEAL_COLS = _DEAL_BUSINESS_COLS
    return _DEAL_COLS


def _trigger_deal_dupes(cur):
    """Shell #36 lane 3a's excess: served rows byte-identical beyond 1/group."""
    cols = _deal_cols()
    r = _row(cur, f"WITH b AS (SELECT {cols} FROM deals WHERE {DEALS_OK}),"
                  f" g AS (SELECT count(*) AS n FROM b GROUP BY {cols})"
                  f" SELECT coalesce(sum(n) - count(*), 0) FROM g WHERE n > 1")
    return None if r is None else int(r[0] or 0)


def _fire_deal_dupe_quarantine(conn, cur, trigger_n):
    """The repair_deals_exact_dupes.py logic, resident: keeper = min(id),
    victims get data_flag='quarantine_duplicate' (NEVER DELETE — served
    queries and the RAG registry already exclude it, _sweep_orphans GCs the
    embeddings). Rollback = id→prior-flag list, collected BEFORE the UPDATE
    and stored on the run row. The write is guarded on the served predicate,
    so replays and races are no-ops."""
    cols = _deal_cols()
    cur.execute(f"SELECT min(id), array_agg(id ORDER BY id)"
                f" FROM deals WHERE {DEALS_OK}"
                f" GROUP BY {cols} HAVING count(*) > 1")
    victims = []
    for keep, ids in cur.fetchall():
        victims += [i for i in ids if i != keep]
        if len(victims) >= DEALS_VICTIM_CAP:
            victims = victims[:DEALS_VICTIM_CAP]
            break
    if not victims:
        return {"rows_affected": 0, "rollback": None,
                "result": {"note": "trigger raced to 0"}, "ok": True}
    cur.execute("SELECT id, data_flag FROM deals WHERE id = ANY(%s)",
                (victims,))
    rollback = [{"id": r[0], "prior": r[1]} for r in cur.fetchall()]
    cur.execute(
        f"UPDATE deals AS d SET data_flag = 'quarantine_duplicate'"
        f" WHERE d.id = ANY(%s) AND {deals_ok('d')}", (victims,))
    flipped = cur.rowcount
    after = _trigger_deal_dupes(cur)
    return {"rows_affected": int(flipped or 0), "rollback": rollback,
            "result": {"victims": len(victims), "excess_after": after},
            "ok": after == 0 or (after is not None and flipped > 0)}


ACTUATORS = [
    {"id": "news_entity_reresolve",
     "heals": "unresolved news entities that already prefix-match a facility"
              " (graph-spine lane 5a; 'Unknown entities' findings)",
     "trigger": _trigger_entity_blindspot,
     "fire": _fire_entity_reresolve},
    {"id": "deals_exact_dupe_quarantine",
     "heals": "byte-identical served deal rows re-grown by the ext_ writer"
              " (graph-spine lane 3a)",
     "trigger": _trigger_deal_dupes,
     "fire": _fire_deal_dupe_quarantine},
]


def _lane_actuators(conn, act: bool):
    out = []
    firing_enabled = (os.environ.get("BRAIN_ACTUATORS_DISABLE") or "") != "1"
    fired_any = False
    with conn.cursor() as cur:
        for a in ACTUATORS:
            trig = None
            try:
                trig = a["trigger"](cur)
            except Exception as e:
                logger.debug("[brain-autonomy] trigger %s failed: %s",
                             a["id"], str(e)[:140])
            if trig is None:
                out.append(_check(a["id"], f"{a['id']} — trigger readable",
                                  None, "trigger query failed — UNMEASURED,"
                                  " will not fire blind", critical=True))
                continue
            each_ok, global_ok = _budget_ok(cur, a["id"])
            state = (f"trigger={trig} · heals: {a['heals']} · budget "
                     f"{'ok' if (each_ok and global_ok) else 'SPENT'} "
                     f"({ACTUATOR_DAILY_CAP_EACH}/actuator/day,"
                     f" {ACTUATOR_DAILY_CAP_GLOBAL} global)")
            if trig == 0:
                out.append(_check(a["id"], f"{a['id']} — defect absent",
                                  True, state, critical=True))
                continue
            if not (act and firing_enabled and each_ok and global_ok):
                why = ("measure-only GET" if not act else
                       "BRAIN_ACTUATORS_DISABLE=1" if not firing_enabled
                       else "budget spent")
                out.append(_check(a["id"], f"{a['id']} — defect present,"
                                  " not fired", False,
                                  state + f" · NOT FIRED ({why})",
                                  critical=True))
                continue
            # FIRE: run row first (rollback rides on it), then the mutation,
            # one transaction — a crash rolls back both together.
            try:
                res = a["fire"](conn, cur, trig)
                cur.execute(
                    "INSERT INTO brain_actuator_runs"
                    " (actuator, live, trigger_n, rows_affected, ok,"
                    "  result, rollback)"
                    " VALUES (%s, TRUE, %s, %s, %s, %s::jsonb, %s::jsonb)",
                    (a["id"], trig, res.get("rows_affected"),
                     bool(res.get("ok")),
                     json.dumps(res.get("result") or {}, default=str),
                     json.dumps(res.get("rollback"), default=str)
                     if res.get("rollback") is not None else None))
                conn.commit()
                fired_any = True
                out.append(_check(
                    a["id"], f"{a['id']} — FIRED",
                    bool(res.get("ok")),
                    f"trigger={trig} → rows_affected="
                    f"{res.get('rows_affected')} · "
                    f"{json.dumps(res.get('result') or {}, default=str)[:160]}"
                    " · rollback stored on brain_actuator_runs",
                    critical=True))
            except Exception as e:
                try:
                    conn.rollback()
                except Exception:
                    pass
                out.append(_check(a["id"], f"{a['id']} — fire FAILED", False,
                                  f"trigger={trig} · {str(e)[:160]} ·"
                                  " transaction rolled back", critical=True))
    r = None
    with conn.cursor() as cur:
        r = _row(cur, "SELECT COUNT(*) FILTER (WHERE live AND fired_at >"
                      " NOW() - interval '7 days'),"
                      " COALESCE(SUM(rows_affected) FILTER (WHERE live AND"
                      " fired_at > NOW() - interval '7 days'), 0)"
                      " FROM brain_actuator_runs")
    if r:
        out.append(_check("actuation_pulse",
                          "the shell has ACTED in the last 7 days", None,
                          f"{int(r[0] or 0)} live fire(s), "
                          f"{int(r[1] or 0)} row(s) healed in 7d — the"
                          " squasher-portal question, answered with rows"))
    return out, fired_any


# ── lane 2 · proposal lifecycle ───────────────────────────────────────

def _lane_proposals(conn, act: bool):
    out = []
    moves_enabled = (os.environ.get("PROPOSAL_TRIAGE_DISABLE") or "") != "1"
    moved_dup = moved_queue = 0
    with conn.cursor() as cur:
        if act and moves_enabled:
            try:
                # exact re-files: same non-empty fingerprint, keep earliest.
                cur.execute("""
                    UPDATE brain_enhancement_proposals SET status='duplicate'
                     WHERE id IN (
                        SELECT p.id FROM brain_enhancement_proposals p
                        JOIN (SELECT fingerprint, MIN(id) AS keep
                                FROM brain_enhancement_proposals
                               WHERE status IN ('proposed','queued')
                                 AND COALESCE(fingerprint,'') <> ''
                               GROUP BY fingerprint HAVING COUNT(*) > 1) k
                          ON k.fingerprint = p.fingerprint
                       WHERE p.status IN ('proposed','queued')
                         AND p.id <> k.keep
                       ORDER BY p.id LIMIT %s)""", (TRIAGE_MOVES_CAP,))
                moved_dup = cur.rowcount
                # keep EXACTLY the top-N at 'queued' (idempotent both ways).
                cur.execute("""
                    WITH top AS (SELECT id FROM brain_enhancement_proposals
                                  WHERE status IN ('proposed','queued')
                                  ORDER BY leverage_rank DESC NULLS LAST,
                                           confidence DESC NULLS LAST, id
                                  LIMIT %s),
                    up AS (UPDATE brain_enhancement_proposals
                              SET status='queued'
                            WHERE id IN (SELECT id FROM top)
                              AND status <> 'queued' RETURNING 1),
                    down AS (UPDATE brain_enhancement_proposals
                                SET status='proposed'
                              WHERE status='queued'
                                AND id NOT IN (SELECT id FROM top)
                              RETURNING 1)
                    SELECT (SELECT COUNT(*) FROM up),
                           (SELECT COUNT(*) FROM down)""", (QUEUE_SIZE,))
                r = cur.fetchone()
                moved_queue = int((r[0] or 0) + (r[1] or 0)) if r else 0
                conn.commit()
            except Exception as e:
                try:
                    conn.rollback()
                except Exception:
                    pass
                out.append(_check("triage_moves", "triage pass applied",
                                  False, f"moves failed, rolled back:"
                                  f" {str(e)[:140]}", critical=True))
        r = _row(cur, """SELECT COALESCE(status,'(null)'), COUNT(*)
                           FROM brain_enhancement_proposals
                          GROUP BY 1 ORDER BY 2 DESC""")
        counts = {}
        try:
            cur.execute("""SELECT COALESCE(status,'(null)'), COUNT(*)
                             FROM brain_enhancement_proposals GROUP BY 1""")
            counts = {k: int(v) for k, v in cur.fetchall()}
        except Exception:
            pass
        dup_pending = _row(cur, """
            SELECT COALESCE(SUM(n - 1), 0) FROM (
              SELECT COUNT(*) AS n FROM brain_enhancement_proposals
               WHERE status IN ('proposed','queued')
                 AND COALESCE(fingerprint,'') <> ''
               GROUP BY fingerprint HAVING COUNT(*) > 1) d""")
        dup_pending = int(dup_pending[0] or 0) if dup_pending else None
        queued = []
        try:
            cur.execute("""SELECT id, LEFT(COALESCE(title,'?'), 80),
                                  ROUND(COALESCE(leverage_rank,0)::numeric, 2)
                             FROM brain_enhancement_proposals
                            WHERE status='queued'
                            ORDER BY leverage_rank DESC NULLS LAST LIMIT 5""")
            queued = [f"#{r[0]} {r[1]} (lev {r[2]})" for r in cur.fetchall()]
        except Exception:
            pass
    out.append(_check(
        "proposal_pipeline", "the book is a pipeline, not a pile",
        (dup_pending == 0 and counts.get("queued", 0) == QUEUE_SIZE)
        if dup_pending is not None else None,
        f"statuses {counts} · exact-dup rows pending {dup_pending} ·"
        f" moved this pass: {moved_dup} → duplicate, {moved_queue} queue"
        f" swap(s) (cap {TRIAGE_MOVES_CAP}/pass; statuses only, this lane"
        f" NEVER deletes)", critical=True))
    out.append(_check(
        "queued_top3", f"top {QUEUE_SIZE} await the L22 handoff", None,
        " · ".join(queued) if queued else "queue empty"))
    return out


# ── lane 3 · the armed activation loop, measured ─────────────────────

def _lane_activation(conn):
    out = []
    arm = (os.environ.get("ACTIVATION_NUDGE_ARM") or "") == "1"
    act = (os.environ.get("CUSTOMER_WHITE_GLOVE_ACT") or "") == "1"
    with conn.cursor() as cur:
        fresh = _row(cur, """SELECT NOW() - last_started_at < interval '48 hours',
                                    last_started_at
                               FROM cron_last_run
                              WHERE job_name = 'customer_white_glove_tick'""")
        sent = _row(cur, """SELECT COUNT(*) FILTER (WHERE sent_at > NOW() -
                                     interval '7 days'),
                                   COUNT(*), MAX(sent_at)
                              FROM email_drip_log
                             WHERE email_key = 'activation_nudge'""")
    chain_ok = arm and act and bool(fresh and fresh[0])
    out.append(_check(
        "chain_armed", "flags set and the daily tick is alive",
        chain_ok,
        f"ACTIVATION_NUDGE_ARM={'1' if arm else 'UNSET'} ·"
        f" CUSTOMER_WHITE_GLOVE_ACT={'1' if act else 'UNSET'} ·"
        f" tick last {fresh[1] if fresh else 'NEVER'} — every link must hold"
        " or stranded customers wait invisibly", critical=True))
    # The nudge's own candidate definition, IMPORTED — a restated copy is how
    # a lane and its sender drift apart. armed=False + force_dry=True: this
    # lane may never send.
    backlog = None
    try:
        from activation_nudge import run_activation_nudge
        preview = run_activation_nudge(armed=False, force_dry=True)
        # report shape: {'candidates': <int>, 'sent': 0, 'preview': [...]}
        if isinstance(preview, dict) and preview.get("ok") is not False:
            backlog = int(preview.get("candidates") or 0)
    except Exception as e:
        out.append(_check("backlog", "paid-but-never-called backlog readable",
                          None, f"UNMEASURED — preview failed:"
                          f" {str(e)[:140]}", critical=True))
    if backlog is not None:
        out.append(_check(
            "backlog", "nobody paid ≥48h ago and sits unnudged with 0 calls",
            (backlog == 0) if chain_ok else None,
            f"{backlog} candidate(s) right now (sender's own definition:"
            " paid, 0 calls, 48h-45d, never nudged, owner/seed excluded,"
            " cap 25/run). Sent: "
            + (f"{int(sent[0] or 0)} in 7d, {int(sent[1] or 0)} lifetime,"
               f" last {sent[2]}" if sent else "unreadable")
            + " · this lane never sends — the gated sender owns that",
            critical=True))
    return out


# ── tick ──────────────────────────────────────────────────────────────

def _stamp_heartbeat(c, ok: bool, ms: int):
    try:
        with c.cursor() as cur:
            cur.execute("""
                INSERT INTO cron_last_run
                    (job_name, last_started_at, last_completed_at,
                     last_status, last_duration_ms, expected_interval_s,
                     run_count)
                VALUES (%s, NOW() ON CONFLICT DO NOTHING, NOW(), %s, %s, %s, 1)
                ON CONFLICT (job_name) DO UPDATE SET
                    last_started_at = NOW(), last_completed_at = NOW(),
                    last_status = EXCLUDED.last_status,
                    last_duration_ms = EXCLUDED.last_duration_ms,
                    expected_interval_s = EXCLUDED.expected_interval_s,
                    run_count = cron_last_run.run_count + 1""",
                (_JOB_NAME, "ok" if ok else "error", ms, _JOB_INTERVAL_S))
        c.commit()
    except Exception:
        try:
            c.rollback()
        except Exception:
            pass


def _tick(act: bool):
    t0 = time.time()
    c = _conn()
    if c is None:
        return {"ok": False, "error": "no_db"}
    _ensure_tables(c)
    lanes = []
    try:
        def _lane(title, fn):
            """Run one lane and record what it COST.

            ★2026-08-23 — #3086 fixed the blind-spot query that had this page
            answering CF's 502 (13/13 probes, a flat ~12.2s) while the tick
            behind it ran 4.2s–26.7s. What made that hard to SEE is still
            here: the tick reported no duration at all, so attributing it
            meant probing the JSON route by hand and correlating wall-clock
            against which check happened to fail. #3086's own finding was
            that "both callers swallow the timeout and report the check
            UNMEASURED, so nothing ever went red" — a shell can be minutes
            slow and look identical to a healthy one. A lane that states its
            cost is the difference between reading the board and running the
            correlation again."""
            _t = time.time()
            checks = fn()
            return {"lane": title, "checks": checks,
                    "pass": _lane_pass(checks),
                    "ms": int((time.time() - _t) * 1000)}

        # _lane_actuators returns (checks, fired_any); the flag is the fire
        # path's own signal and this measure surface has never read it.
        lanes.append(_lane("1 · actuators — vetted repairs the brain may fire",
                           lambda: _lane_actuators(c, act)[0]))
        lanes.append(_lane("2 · proposal lifecycle — dedup, rank, queue top 3",
                           lambda: _lane_proposals(c, act)))
        lanes.append(_lane("3 · activation loop — armed, capped, measured",
                           lambda: _lane_activation(c)))
        ok = all(l["pass"] is not False for l in lanes)
        ms = int((time.time() - t0) * 1000)
        out = {"ok": True, "acted": bool(act), "lanes": lanes,
               "lanes_pass": sum(1 for l in lanes if l["pass"] is True),
               "lanes_total": len(lanes),
               "ms": ms,
               "note": ("POST acts (budgeted, kill-switchable); GET only"
                        " measures. Rollbacks live on brain_actuator_runs.")}
        _stamp_heartbeat(c, ok, ms)
        return out
    finally:
        try:
            c.close()
        except Exception:
            pass


@brain_autonomy_master_shell_bp.route(
    "/api/v1/admin/brain-autonomy/master-tick", methods=["GET", "POST"])
def brain_autonomy_tick():
    # ★404, NEVER 5xx (tests/test_shell_killswitch_never_5xx.py): the CF
    # worker's proxyWithRetry reads ANY 5xx from Railway as a dead origin and
    # fails the whole site over to the stale Render backend. A kill switch
    # must disable ONE shell, not the site. I shipped 503 here and the guard
    # caught it pre-merge.
    if _disabled():
        return jsonify(ok=False, disabled=True), 404
    if not _admin_ok():
        return jsonify(ok=False, error="forbidden"), 403
    return jsonify(_tick(act=(request.method == "POST")))


@brain_autonomy_master_shell_bp.route("/admin/brain-autonomy")
def brain_autonomy_page():
    if _disabled():
        return Response("disabled", status=404)   # never 5xx — see above
    if not _admin_ok():
        return Response("forbidden (X-Admin-Key / ?admin_key=)", status=403)
    d = _tick(act=False)
    rows = []
    for l in d.get("lanes", []):
        rows.append(f"<tr><th colspan=3>{_esc(str(l['lane']))} — pass:"
                    f" {_esc(str(l['pass']))} · {int(l.get('ms') or 0)}ms"
                    f"</th></tr>")
        for ch in l.get("checks", []):
            mark = {"True": "✅", "False": "❌"}.get(str(ch.get("pass")), "◻️")
            rows.append(f"<tr><td>{mark}</td><td>{_esc(str(ch['name']))}</td>"
                        f"<td>{_esc(str(ch.get('detail') or ''))}</td></tr>")
    html = ("<html><head><title>brain autonomy</title><style>body{font-family:"
            "-apple-system,sans-serif;margin:24px;background:#0a0a0f;color:#eee}"
            "table{border-collapse:collapse;max-width:1100px}td,th{border:1px "
            "solid #333;padding:6px 10px;text-align:left;font-size:13px}"
            "th{background:#16213e}</style></head><body><h2>Brain autonomy — "
            "thinking → acting</h2><p>GET measures; the daily driver POSTs. "
            "Kill: BRAIN_AUTONOMY_SHELL_DISABLE / BRAIN_ACTUATORS_DISABLE / "
            "PROPOSAL_TRIAGE_DISABLE.</p>"
            f"<p style='color:#8ab'>tick {int(d.get('ms') or 0)}ms — this page "
            "renders the whole tick synchronously, so this number IS its edge "
            "budget (CF DEFAULT 15s, measured cut ~12.2s)</p><table>"
            + "".join(rows) + "</table></body></html>")
    return Response(html, mimetype="text/html")
