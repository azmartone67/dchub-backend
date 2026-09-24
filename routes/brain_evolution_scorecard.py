"""
brain_evolution_scorecard — four numbers, snapshotted weekly, that answer
"is the brain actually getting smarter?" without re-deriving it each time.

WHY (owner, 2026-09-24: "we don't seem to be evolving")
  The brain has many self-reports (/brain/evolution, /self-assessment,
  /effectiveness, cant-fail-signals) and each is a CURRENT reading. None of
  them keeps a weekly series of the few numbers that would move if learning
  were happening, so every "is it improving?" question has been answered by a
  fresh hand measurement. More notes, chunks and graph edges make the brain
  remember more; only these four move when it gets BETTER:

    1. fix_success        of graded fix outcomes (30d), share that HELD
                          brain_fix_outcomes.still_broken — higher is better
    2. recurring_share    of issues seen in the last 7d, share first seen
                          30+ days ago — the same problems coming back.
                          brain_issue_persistence — LOWER is better
    3. spec_to_code       landed specs the implementer drove to a code PR,
                          and squasher-agent fixes the detector confirmed (30d)
                          loop_closure_spec_attempts + squasher_work_queue
    4. negative_signal    real "no"s written in 30d — human rejections
                          (brain_review_decisions). 0 is a DEAD signal, not a
                          clean record: nothing learns from a gate that never
                          disagrees.

  Baseline measured live 2026-09-24 before this shipped: fix_success 32.7%
  (65 of 199), September MTD 24.8% vs August 77.4%; 0 rejections in 268
  reviews; 1 spec driven, 0 acted; chronic_stuck_issues 470.

HONESTY RULES
  · A metric whose source is unreadable is None ("unmeasured"), never 0.
  · A rate under its floor of samples is None — 2 outcomes can swing 50pp.
  · The verdict needs two snapshots at least 7 days apart; before that it
    says so instead of guessing.

ENDPOINTS (admin)
  GET  /api/v1/admin/brain/evolution-scorecard           live + weekly history + verdict
  POST /api/v1/admin/brain/evolution-scorecard/snapshot  upsert this ISO week's row
"""
from __future__ import annotations

import hmac
import json
import logging
import os
from datetime import date, datetime, timedelta, timezone

from flask import Blueprint, jsonify, request

logger = logging.getLogger(__name__)

brain_evolution_scorecard_bp = Blueprint("brain_evolution_scorecard", __name__)

RATE_FLOOR = 20          # graded outcomes before fix_success is a rate
ACTIVE_FLOOR = 20        # active issues before recurring_share is a rate
CHRONIC_AFTER_DAYS = 30
FIX_BAND_PP = 3.0        # a move inside this band is flat, not learning
RECUR_BAND_PP = 2.0
HISTORY_WEEKS = 12


def _conn():
    """A TRANSACTIONAL connection. ★ ai_reach._conn() hands back
    autocommit=True, under which every SAVEPOINT below raises
    NoActiveSqlTransaction — the first live run read all sources as
    "unreadable" (2026-09-24). It is a fresh, unpooled connection, so
    flipping it here affects no one else."""
    try:
        from routes.ai_reach import _conn as _raw
        c = _raw()
        if c is not None:
            c.autocommit = False
        return c
    except Exception:
        return None


def _one(cur, sql, args=None):
    """First row, or None when the source cannot be read. SAVEPOINT-isolated
    so one missing table does not abort the reads after it."""
    try:
        cur.execute("SAVEPOINT scorecard_src")
        cur.execute(sql, args) if args is not None else cur.execute(sql)
        row = cur.fetchone()
        cur.execute("RELEASE SAVEPOINT scorecard_src")
        return row
    except Exception as e:  # noqa: BLE001
        try:
            cur.execute("ROLLBACK TO SAVEPOINT scorecard_src")
        except Exception:
            pass
        logger.info("[evolution-scorecard] source unreadable: %s", str(e)[:160])
        return None


def rate_pct(num, den, floor):
    """Percent, or None when unmeasured or under the floor."""
    if num is None or den is None or den < floor or den <= 0:
        return None
    return round(100.0 * num / den, 1)


# ═══════════════════════════════════════════════════════════════════════
# measure
# ═══════════════════════════════════════════════════════════════════════
_FIX_SQL = (
    "SELECT COUNT(*) FILTER (WHERE still_broken IS NOT NULL),"
    "       COUNT(*) FILTER (WHERE still_broken = FALSE)"
    "  FROM brain_fix_outcomes"
    " WHERE checked_at >= NOW() - %s * INTERVAL '1 day'")
_RECUR_SQL = (
    "SELECT COUNT(*),"
    "       COUNT(*) FILTER (WHERE first_seen_at < NOW() - %s * INTERVAL '1 day')"
    "  FROM brain_issue_persistence"
    " WHERE last_seen_at >= NOW() - INTERVAL '7 days'")
_SPEC_SQL = (
    "SELECT COUNT(*), COUNT(*) FILTER (WHERE last_acted)"
    "  FROM loop_closure_spec_attempts"
    " WHERE last_attempt_at >= NOW() - INTERVAL '30 days'")
_AGENT_SQL = (
    "SELECT COUNT(*) FILTER (WHERE agent_pr_url IS NOT NULL),"
    "       COUNT(*) FILTER (WHERE agent_state = 'fixed')"
    "  FROM squasher_work_queue"
    " WHERE agent_started_at >= NOW() - INTERVAL '30 days'")
_REJECT_SQL = (
    "SELECT COUNT(*) FILTER (WHERE decision = 'reject'), COUNT(*)"
    "  FROM brain_review_decisions"
    " WHERE decided_at >= NOW() - INTERVAL '30 days'")
_LESSONS_SQL = (
    "SELECT COUNT(*), COUNT(*) FILTER (WHERE guidance <> '')"
    "  FROM brain_lessons")


def measure(cur) -> dict:
    """The four numbers, read live. Every field None when its source is
    unreadable."""
    m: dict = {}
    fix = _one(cur, _FIX_SQL, (30,))
    graded, held = (fix if fix else (None, None))
    m["fix_success"] = {
        "pct_30d": rate_pct(held, graded, RATE_FLOOR),
        "held": held, "graded": graded, "floor": RATE_FLOOR}

    rec = _one(cur, _RECUR_SQL, (CHRONIC_AFTER_DAYS,))
    active, chronic = (rec if rec else (None, None))
    m["recurring_share"] = {
        "pct_7d": rate_pct(chronic, active, ACTIVE_FLOOR),
        "chronic": chronic, "active_7d": active,
        "chronic_after_days": CHRONIC_AFTER_DAYS, "floor": ACTIVE_FLOOR}

    spec = _one(cur, _SPEC_SQL)
    agent = _one(cur, _AGENT_SQL)
    m["spec_to_code"] = {
        "specs_driven_30d": spec[0] if spec else None,
        "spec_prs_opened_30d": spec[1] if spec else None,
        "agent_prs_opened_30d": agent[0] if agent else None,
        "agent_fixes_verified_30d": agent[1] if agent else None}

    rej = _one(cur, _REJECT_SQL)
    lessons = _one(cur, _LESSONS_SQL)
    rejects, reviews = (rej if rej else (None, None))
    m["negative_signal"] = {
        "rejections_30d": rejects, "reviews_30d": reviews,
        "state": (None if rejects is None
                  else "live" if rejects > 0
                  else "dead" if (reviews or 0) >= 20 else "quiet"),
        "lesson_families": lessons[0] if lessons else None,
        "lessons_with_guidance": lessons[1] if lessons else None}
    return m


# ═══════════════════════════════════════════════════════════════════════
# verdict — PURE
# ═══════════════════════════════════════════════════════════════════════
def _get(snap, *path):
    cur = snap
    for p in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(p)
    return cur


def evolution_verdict(history: list) -> dict:
    """Is it getting better? PURE.

    history: [{"week_start": "YYYY-MM-DD", "metrics": {...}}, ...] any order.

    Compares the newest snapshot with the OLDEST one inside the last 4 weeks
    that is at least 7 days older — a month-scale read, because one week of
    fix outcomes is too few to move a rate honestly.

    ★ fix_success is the deciding number: it is the only one that says the
    brain's changes WORKED. recurring_share can veto "evolving" (fixes that
    hold but the same problems keep returning is not learning). The other two
    are reported, not voted — they are inputs to learning, not evidence of it.
    """
    snaps = sorted((h for h in history or [] if h.get("week_start")),
                   key=lambda h: h["week_start"])
    out = {"status": None, "compared": None, "signals": {}}
    if len(snaps) < 2:
        out["reason"] = (f"{len(snaps)} weekly snapshot(s) on record — two at "
                         f"least 7 days apart are needed for a direction")
        return out
    latest = snaps[-1]
    d_latest = date.fromisoformat(latest["week_start"])
    window = [s for s in snaps[:-1]
              if 7 <= (d_latest - date.fromisoformat(s["week_start"])).days <= 28]
    if not window:
        out["reason"] = "no snapshot 7–28 days before the latest"
        return out
    base = window[0]
    out["compared"] = {"from": base["week_start"], "to": latest["week_start"]}

    def delta(*path):
        a, b = _get(base["metrics"], *path), _get(latest["metrics"], *path)
        return None if a is None or b is None else round(b - a, 1)

    fx = delta("fix_success", "pct_30d")
    rc = delta("recurring_share", "pct_7d")
    sp = delta("spec_to_code", "spec_prs_opened_30d")
    rj = _get(latest["metrics"], "negative_signal", "state")
    out["signals"] = {
        "fix_success_delta_pp": fx,
        "fix_success_direction": (None if fx is None else
                                  "improving" if fx > FIX_BAND_PP else
                                  "declining" if fx < -FIX_BAND_PP else "flat"),
        "recurring_share_delta_pp": rc,
        "recurring_direction": (None if rc is None else
                                "improving" if rc < -RECUR_BAND_PP else
                                "worsening" if rc > RECUR_BAND_PP else "flat"),
        "spec_prs_delta": sp,
        "negative_signal": rj,
    }
    s = out["signals"]
    if s["fix_success_direction"] is None:
        out["status"] = None
        out["reason"] = "fix_success unmeasured at one end of the window"
    elif s["fix_success_direction"] == "improving" and \
            s["recurring_direction"] != "worsening":
        out["status"] = "evolving"
    elif s["fix_success_direction"] == "declining":
        out["status"] = "regressing"
    else:
        out["status"] = "not_yet"
        out["reason"] = ("fix_success flat" if s["fix_success_direction"] == "flat"
                         else "fixes improving but the same problems are "
                              "recurring more")
    return out


def week_start(d: date | None = None) -> date:
    d = d or datetime.now(timezone.utc).date()
    return d - timedelta(days=d.weekday())


# ═══════════════════════════════════════════════════════════════════════
# store
# ═══════════════════════════════════════════════════════════════════════
_DDL = """
CREATE TABLE IF NOT EXISTS brain_evolution_scorecard (
    week_start  DATE PRIMARY KEY,
    metrics     JSONB NOT NULL,
    computed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
)"""


def snapshot() -> dict:
    """Measure and upsert this ISO week's row. Re-running inside the week
    refreshes it — the row is the week's LATEST reading."""
    c = _conn()
    if c is None:
        return {"ok": False, "error": "no db connection"}
    try:
        with c.cursor() as cur:
            cur.execute(_DDL)
            m = measure(cur)
            wk = week_start()
            cur.execute("""
                INSERT INTO brain_evolution_scorecard (week_start, metrics,
                    computed_at) VALUES (%s, %s::jsonb, NOW() ON CONFLICT DO NOTHING)
                ON CONFLICT (week_start) DO UPDATE SET
                    metrics = EXCLUDED.metrics, computed_at = NOW()""",
                (wk, json.dumps(m)))
        c.commit()
        return {"ok": True, "week_start": wk.isoformat(), "metrics": m}
    except Exception as e:  # noqa: BLE001
        try:
            c.rollback()
        except Exception:
            pass
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:200]}"}
    finally:
        try:
            c.close()
        except Exception:
            pass


def read_scorecard() -> dict:
    c = _conn()
    if c is None:
        return {"ok": False, "error": "no db connection"}
    try:
        with c.cursor() as cur:
            live = measure(cur)
            rows = []
            try:
                cur.execute("SAVEPOINT scorecard_hist")
                cur.execute("SELECT week_start, metrics, computed_at"
                            "  FROM brain_evolution_scorecard"
                            " ORDER BY week_start DESC LIMIT %s",
                            (HISTORY_WEEKS,))
                rows = cur.fetchall()
                cur.execute("RELEASE SAVEPOINT scorecard_hist")
            except Exception:
                cur.execute("ROLLBACK TO SAVEPOINT scorecard_hist")
        c.rollback()
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:200]}"}
    finally:
        try:
            c.close()
        except Exception:
            pass
    history = [{"week_start": r[0].isoformat(), "metrics": r[1],
                "computed_at": r[2].isoformat() if r[2] else None}
               for r in rows]
    return {"ok": True, "live": live, "history": history,
            "verdict": evolution_verdict(history),
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "purpose": ("Is the brain getting better, week over week? "
                        "fix_success decides; recurring_share can veto; "
                        "spec_to_code and negative_signal are the inputs "
                        "that should move first.")}


# ═══════════════════════════════════════════════════════════════════════
# endpoints
# ═══════════════════════════════════════════════════════════════════════
def _admin_ok() -> bool:
    expected = (os.environ.get("DCHUB_ADMIN_KEY")
                or os.environ.get("DCHUB_INTERNAL_KEY") or "").strip()
    got = (request.headers.get("X-Admin-Key") or "").strip()
    return bool(expected) and bool(got) and hmac.compare_digest(got, expected)


@brain_evolution_scorecard_bp.route(
    "/api/v1/admin/brain/evolution-scorecard", methods=["GET"])
def brain_evolution_scorecard_read():
    if not _admin_ok():
        return jsonify({"ok": False, "error": "admin key required"}), 401
    return jsonify(read_scorecard()), 200


@brain_evolution_scorecard_bp.route(
    "/api/v1/admin/brain/evolution-scorecard/snapshot", methods=["POST"])
def brain_evolution_scorecard_snapshot():
    if not _admin_ok():
        return jsonify({"ok": False, "error": "admin key required"}), 401
    return jsonify(snapshot()), 200
