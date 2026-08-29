"""brain_orphan_decisions.py — does anything READ what the brain decides?
===========================================================================

THE INCIDENT THIS EXISTS FOR (2026-08-29)
-----------------------------------------
`customer_lifecycle_events` worked perfectly. It flagged 16 accounts
`stranded` and 5 `churned`, and wrote *"Activation nudge — paid, zero calls
past grace"* BY NAME for tj@karklins.com and rob@hedmarkholdings.com.

`mcp_outreach_log` had **zero rows**. Nothing was ever sent.

Nine of seventeen external payers have never made a single call. The system
knew exactly who they were and wrote the nudge. The decision landed in a
table nothing reads.

That is not an intelligence failure and no amount of better reasoning fixes
it. It is a WIRING failure, and it is this codebase's single most repeated
defect class:

  · the ZERO-WRITER TWIN — a detector writing to a table nobody serves,
    while its twin serves a table nobody writes (wrong_table_class_0730)
  · `is_dup` set as a no-op — suppression that never reached the count
    (dedup_suppression_truth)
  · three dead imports making a full-reload job silently nil
    (fullreload_blind_0807)
  · registry rows LISTED but never DELIVERED (registry_drift_0808)
  · 45 of 66 strategic recommendations sitting `new`, re-read every 6h
  · `customer_touchpoints` — no row since 2026-03-13

Every one of them returned HTTP 200. Every one of them looked healthy from
the writing side. The only thing they have in common is that NOBODY ASKED
WHETHER THE OTHER END WAS LISTENING.

WHAT THIS DOES
--------------
For each registered decision sink: is the writer alive, is the reader alive,
and — the case that costs the most — did an upstream DECIDE something that
the sink never received?

  MISSING        the table does not exist. `to_regclass` IS NULL.
  SHAPE_DRIFT    it exists, but a column this registry names does not.
                 The check itself is broken; say so rather than pass.
  SILENT_WRITER  ★ the mcp_outreach_log case. Upstream produced N decisions
                 in the window and the sink took ZERO rows. The loudest
                 verdict here, and the only one that needs no backlog to
                 fire — one un-acted decision is already the defect.
  ORPHANED       rows exist, none consumed in the window, backlog over floor.
  STALLED        writes continue but the consumed ratio fell under the floor.
  OK             both ends alive.

SAFETY
------
Read-only. Not one statement outside SELECT — the only write is the finding
this files through the normal writer, and `?file=0` suppresses even that.
Every sink is independently wrapped: a sink that raises is reported as an
`error` row and the sweep continues, so one shape change can never cost the
other twelve checks. A missing table is DATA, never a 500.

Identifiers are interpolated (a registry in code, reviewed like ACTION_CLASSES
is), so every one is regex-validated at module import — a typo is an
ImportError at boot, not a SQL injection surface.

ROUTES
------
  GET /api/v1/brain/orphan-decisions          sweep + file findings
  GET /api/v1/brain/orphan-decisions?file=0   sweep only, file nothing

Plain function for the shells (JSON-safe dict, never raises):
  sweep(file: bool = False) -> dict
"""
from __future__ import annotations

import logging
import re

from flask import Blueprint, jsonify, request

logger = logging.getLogger(__name__)

brain_orphan_decisions_bp = Blueprint("brain_orphan_decisions", __name__)

# Window every rate is measured over. Long enough that a weekly cadence is
# not read as silence, short enough that a loop dead for a month is loud.
_WINDOW_DAYS = 30

_IDENT_RE = re.compile(r"^[a-z_][a-z0-9_]*$")


# ── the registry ─────────────────────────────────────────────────────────
#
# One entry per place the brain records a DECISION. Adding a sink is a code
# review, same contract as ACTION_CLASSES.
#
#   sink        table the decision lands in
#   ts          its timestamp column — "was the writer alive?"
#   consumed    SQL boolean over the row: TRUE once something ACTED on it.
#               None = the row's existence IS the action (a send is
#               terminal; there is no second step to wait for).
#   upstream    (table, ts, predicate) that SHOULD have produced sink rows.
#               This is what turns a quiet table into a provable defect:
#               without it, zero rows is ambiguous (nothing to do?) — with
#               it, zero rows against N upstream decisions is a broken wire.
#   min_open    backlog below which ORPHANED is noise, not a finding
#   min_ratio   consumed/written floor before STALLED fires
#
_SINKS = (
    {
        "sink": "mcp_outreach_log",
        "ts": "sent_at",
        "consumed": None,          # a send is terminal
        "upstream": (
            "customer_lifecycle_events", "at",
            "to_stage IN ('stranded', 'churned')",
        ),
        "min_open": 1,
        "min_ratio": 0.0,
        "owner": "customer_white_glove",
        "why": (
            "The 2026-08-29 activation gap: 16 stranded + 5 churned accounts "
            "named, nudge text written per account, zero rows sent. 9 of 17 "
            "external payers have never made a call."),
    },
    {
        "sink": "brain_strategic_recommendations",
        "ts": "created_at",
        "consumed": "status <> 'new'",
        "upstream": None,
        "min_open": 10,
        "min_ratio": 0.10,
        "owner": "brain_strategic_planner",
        "why": ("45 of 66 recommendations sat `new` and were re-read every 6h. "
                "The advise->decide->ship loop is open by design; this "
                "measures HOW open."),
    },
    {
        "sink": "press_releases_queue",
        "ts": "created_at",
        "consumed": "status <> 'draft'",
        "upstream": None,
        "min_open": 5,
        "min_ratio": 0.10,
        "owner": "dchub_media / data_growth_radar",
        "why": ("Draft-only by design — the human publishes. That makes an "
                "un-drained queue invisible unless something counts it."),
    },
    {
        "sink": "squasher_work_queue",
        "ts": "requested_at",
        "consumed": "status <> 'queued'",
        "upstream": None,
        "min_open": 10,
        "min_ratio": 0.10,
        "owner": "brain_bug_squash",
        "why": "closed_with_pr was 0 while the queue kept accepting rows.",
    },
    {
        "sink": "brain_lane_decisions",
        "ts": "decided_at",
        "consumed": "outcome IS NOT NULL",
        "upstream": None,
        "min_open": 8,
        "min_ratio": 0.30,
        "owner": "brain_lane_driver",
        "why": ("A decision with no stamped outcome never became a lesson — "
                "the RAG recall that grades tomorrow's decision reads this "
                "column."),
    },
    {
        # ★2026-08-29 lane 2 (sink-watch). This detector was built because
        # customer_white_glove decided 16 accounts were stranded and
        # mcp_outreach_log took zero rows. brain_escalations is the CATCHER
        # that was then built for that hand-off — and a catcher nobody
        # empties is the identical bug one layer up. The queue must be
        # subject to the same check it exists to satisfy.
        #
        # `activated` is MEASURED (the account started calling), so it is
        # not a drain a human can fake; the drains are 'contacted' and an
        # explicit resolve. Deliberately human-terminal, exactly like
        # press_releases_queue — which is what makes an un-drained queue
        # invisible unless something counts it. Same thresholds.
        #
        # upstream is None on purpose: sync() refreshes existing rows
        # without moving first_seen_at, so a steady-state queue would read
        # as a SILENT_WRITER the moment the roster re-escalated the same
        # accounts. Consumption is the honest measure here.
        "sink": "brain_escalations",
        "ts": "first_seen_at",
        "consumed": "status <> 'open'",
        "upstream": None,
        "min_open": 5,
        "min_ratio": 0.10,
        "owner": "brain_escalation_queue",
        "why": ("The nudge fired for all nine stranded payers and all nine "
                "stayed at zero calls; the loop correctly concluded 'human "
                "touch, not another email' and that conclusion had nowhere "
                "to land. This row is the catcher being watched in turn."),
    },
)


def _validate_registry() -> None:
    """A typo in a registry identifier must fail at import, not compose SQL."""
    for s in _SINKS:
        names = [s["sink"], s["ts"]]
        if s["upstream"]:
            names += [s["upstream"][0], s["upstream"][1]]
        for n in names:
            if not _IDENT_RE.match(n or ""):
                raise ValueError(
                    f"brain_orphan_decisions: bad identifier {n!r} in {s['sink']!r}")


_validate_registry()


def _table_exists(cur, table: str) -> bool:
    cur.execute("SELECT to_regclass(%s) IS NOT NULL", (table,))
    return bool((cur.fetchone() or (False,))[0])


def _columns(cur, table: str) -> set:
    cur.execute("SELECT column_name FROM information_schema.columns "
                "WHERE table_name = %s", (table,))
    return {r[0] for r in (cur.fetchall() or ())}


def _scalar(cur, sql: str) -> int:
    cur.execute(sql)
    return int((cur.fetchone() or (0,))[0] or 0)


def _check_one(cur, spec: dict) -> dict:
    """One sink. Pure SELECT. Caller isolates failures."""
    sink, ts = spec["sink"], spec["ts"]
    out = {"sink": sink, "owner": spec["owner"], "why": spec["why"],
           "window_days": _WINDOW_DAYS}

    if not _table_exists(cur, sink):
        out.update(verdict="MISSING",
                   detail=f"table {sink} does not exist (to_regclass IS NULL)")
        return out

    cols = _columns(cur, sink)
    missing = [c for c in (ts,) if c not in cols]
    if missing:
        out.update(verdict="SHAPE_DRIFT",
                   detail=f"{sink} lacks column(s) {', '.join(missing)} — "
                          f"this check cannot measure it")
        return out

    win = f"{ts} > NOW() - INTERVAL '{_WINDOW_DAYS} days'"
    out["rows_total"] = _scalar(cur, f"SELECT COUNT(*) FROM {sink}")
    out["rows_written_window"] = _scalar(
        cur, f"SELECT COUNT(*) FROM {sink} WHERE {win}")

    # ── SILENT_WRITER: did an upstream decide something this never received?
    if spec["upstream"]:
        up_t, up_ts, up_pred = spec["upstream"]
        if _table_exists(cur, up_t) and up_ts in _columns(cur, up_t):
            up_n = _scalar(
                cur,
                f"SELECT COUNT(*) FROM {up_t} "
                f"WHERE {up_ts} > NOW() - INTERVAL '{_WINDOW_DAYS} days' "
                f"AND ({up_pred})")
            out["upstream_table"] = up_t
            out["upstream_decisions_window"] = up_n
            if up_n > 0 and out["rows_written_window"] == 0:
                out.update(
                    verdict="SILENT_WRITER",
                    detail=(f"{up_t} produced {up_n} decision(s) in "
                            f"{_WINDOW_DAYS}d matching [{up_pred}] and {sink} "
                            f"took ZERO rows. The decision was made and "
                            f"nothing acted on it."))
                return out

    # ── consumption
    if spec["consumed"] is None:
        out["consumed_window"] = out["rows_written_window"]
        out["verdict"] = "OK"
        out["detail"] = (f"{out['rows_written_window']} row(s) in "
                         f"{_WINDOW_DAYS}d; existence is the action")
        return out

    consumed_expr = spec["consumed"]
    out["consumed_window"] = _scalar(
        cur, f"SELECT COUNT(*) FROM {sink} WHERE {win} AND ({consumed_expr})")
    out["open_total"] = _scalar(
        cur, f"SELECT COUNT(*) FROM {sink} WHERE NOT ({consumed_expr})")

    written = out["rows_written_window"]
    consumed = out["consumed_window"]
    ratio = (consumed / written) if written else None
    out["consumed_ratio"] = round(ratio, 3) if ratio is not None else None

    if consumed == 0 and out["open_total"] >= spec["min_open"]:
        out.update(verdict="ORPHANED",
                   detail=(f"{out['open_total']} unconsumed row(s) and ZERO "
                           f"consumed in {_WINDOW_DAYS}d "
                           f"(consumed = [{consumed_expr}])"))
    elif ratio is not None and ratio < spec["min_ratio"] and written >= 5:
        out.update(verdict="STALLED",
                   detail=(f"consumed {consumed}/{written} = {ratio:.1%} in "
                           f"{_WINDOW_DAYS}d, under the "
                           f"{spec['min_ratio']:.0%} floor"))
    else:
        out.update(verdict="OK",
                   detail=(f"consumed {consumed}/{written} in "
                           f"{_WINDOW_DAYS}d, {out['open_total']} open"))
    return out


_BAD = ("MISSING", "SHAPE_DRIFT", "SILENT_WRITER", "ORPHANED", "STALLED")


def sweep(file: bool = False) -> dict:
    """Read every registered sink. JSON-safe, never raises.

    file=True files one brain_finding per non-OK sink. The finding identity
    is the sink, so a re-run refreshes the row instead of duplicating it.
    """
    from db_utils import safe_db

    results, filed = [], 0
    try:
        with safe_db() as conn:
            cur = conn.cursor()
            for spec in _SINKS:
                try:
                    results.append(_check_one(cur, spec))
                except Exception as e:
                    # One sink's shape change must never cost the others.
                    try:
                        conn.rollback()
                    except Exception:
                        pass
                    results.append({"sink": spec["sink"], "verdict": "error",
                                    "detail": str(e)[:200]})

            if file:
                try:
                    from routes.brain_findings_writer import upsert_brain_finding
                    for r in results:
                        if r.get("verdict") not in _BAD:
                            continue
                        upsert_brain_finding(
                            cur,
                            issue=f"orphan_decision:{r['sink']}",
                            url="/api/v1/brain/orphan-decisions",
                            count=1,
                            detail=(f"[{r['verdict']}] {r.get('detail', '')} "
                                    f"— owner: {r.get('owner', '?')}. "
                                    f"{r.get('why', '')}"),
                            detector="orphan_decisions")
                        filed += 1
                    conn.commit()
                except Exception as e:
                    logger.warning("[orphan-decisions] file failed: %s",
                                   str(e)[:160])
                    filed = 0
    except Exception as e:
        return {"ok": False, "error": str(e)[:200], "sinks": []}

    counts = {}
    for r in results:
        counts[r.get("verdict", "?")] = counts.get(r.get("verdict", "?"), 0) + 1
    worst = next((v for v in _BAD if counts.get(v)), "OK")

    return {
        "ok": True,
        "window_days": _WINDOW_DAYS,
        "verdict": worst,
        "counts": counts,
        "findings_filed": filed,
        "sinks": results,
        "basis": ("each sink read directly; SILENT_WRITER compares an "
                  "upstream decision count against sink rows in the same "
                  "window"),
    }


@brain_orphan_decisions_bp.route("/api/v1/brain/orphan-decisions",
                                 methods=["GET"])
def orphan_decisions_route():
    want_file = str(request.args.get("file", "1")).lower() not in ("0", "false", "no")
    return jsonify(sweep(file=want_file))


def register_brain_orphan_decisions(app) -> bool:
    app.register_blueprint(brain_orphan_decisions_bp)
    return True
