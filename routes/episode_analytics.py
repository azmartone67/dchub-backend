"""Episode analytics — make the brain WORK its chronic-episode leaderboard.

WHY (2026-07-18): brain_findings became an EPISODE ledger on 2026-07-17
(episode_count / episode_seen_count / episode_started_at — semantics live
in routes/brain_findings_writer.py), but NOTHING consumed the analytics:
the operator still triaged the inbox by last_seen while e.g.
brand_surface_dormant:bs_translator sat open 828h. This blueprint is the
read surface that turns the ledger into decisions:

  GET /api/v1/admin/brain/episodes/analytics    (X-Admin-Key gated)

  {
    open_now,                # open findings right now
    resolved_24h, resolved_7d,
    avg_mttr_h,              # 7d resolved, episode_started_at-based
    mttr_by_detector,        # top 10 by 7d resolved volume
    chronic_leaderboard,     # top 15 open by hours_open × episode_seen_count
    flappiest                # top 5 by episode_count (most reopened)
  }

Design rules (repo patterns, deliberately mirrored):
  · Admin gate mirrors brain_backlog_admin: DCHUB_ADMIN_KEY (or legacy
    ADMIN_KEY) via X-Admin-Key header or ?admin_key=, hmac.compare_digest.
    Unauthorized → 403. Gate CLOSED when the env var is unset.
  · DB access mirrors roadmap_master_shell._read_db: pooled,
    replica-preferred (main.get_read_connection), rollback+return in
    finally, db_utils.get_db fallback. Never a raw psycopg2 connection.
  · Fail-soft everywhere: a broken query nulls its section, and a
    short/malformed row skips its entry (schema drift must degrade
    the payload, not 500 it). Episode columns are
    COALESCE'd (episode_started_at→first_seen, counts→1) because
    pre-2026-07-17 rows may predate the episode DDL.

Read-only. No writes, no state.
"""
from __future__ import annotations

import datetime
import hmac
import logging
import os
from contextlib import contextmanager

from flask import Blueprint, jsonify, request

logger = logging.getLogger(__name__)

episode_analytics_bp = Blueprint("episode_analytics", __name__)


# ── admin gate (mirrors brain_backlog_admin) ─────────────────────────

def _admin_key() -> str:
    return (os.environ.get("DCHUB_ADMIN_KEY")
            or os.environ.get("ADMIN_KEY") or "").strip()


def _admin_ok() -> bool:
    expected = _admin_key()
    if not expected:
        return False
    provided = (request.headers.get("X-Admin-Key")
                or request.args.get("admin_key") or "").strip()
    if not provided:
        return False
    return hmac.compare_digest(provided, expected)


# ── db (pooled, replica-preferred, READ-ONLY) ────────────────────────

@contextmanager
def _read_db():
    """Pooled READ-ONLY connection, replica-preferred. Ladder:
      1. main.get_read_connection — the Neon read-replica pool
         (falls back to primary itself).
      2. db_utils.get_db — pooled primary wrapper (close → putconn).
    Yields None when no DB is reachable — callers must fail-soft.
    rollback+return in finally (the repo read-route contract)."""
    conn = None
    cleanup = None
    try:
        from main import get_read_connection, return_read_connection
        _c, _src = get_read_connection()
        if _c is not None:
            conn = _c
            cleanup = lambda: return_read_connection(_c, _src)  # noqa: E731
    except Exception as e:
        logger.debug("[episodes] read pool unavailable: %s", e)
    if conn is None:
        try:
            from db_utils import get_db
            _w = get_db()
            conn = _w
            cleanup = _w.close  # PGConnectionWrapper.close → putconn
        except Exception as e:
            logger.warning("[episodes] db connect failed: %s", e)
    try:
        yield conn
    finally:
        if conn is not None:
            try:
                conn.rollback()
            except Exception:
                pass
            if cleanup is not None:
                try:
                    cleanup()
                except Exception:
                    pass


def _rows(conn, sql: str, args: tuple | None = None):
    """Fail-soft fetchall. None on error (a section must distinguish
    'query broke' from 'no rows'). Rolls back its own failure so the
    next section's query still runs on the same connection."""
    if conn is None:
        return None
    cur = None
    try:
        cur = conn.cursor()
        if args is None:
            cur.execute(sql)
        else:
            cur.execute(sql, args)
        return cur.fetchall()
    except Exception as e:
        logger.debug("[episodes] query failed: %s -- %s", sql[:90], e)
        try:
            conn.rollback()
        except Exception:
            pass
        return None
    finally:
        try:
            if cur is not None:
                cur.close()
        except Exception:
            pass


# ── analytics SQL ────────────────────────────────────────────────────
# episode_started_at is COALESCE'd to first_seen (legacy rows predate the
# episode DDL); counts COALESCE to 1 — one observation is one sighting.

_SQL_VITALS = """
    SELECT COUNT(*) FILTER (WHERE status = 'open')                        AS open_now,
           COUNT(*) FILTER (WHERE resolved_at >= NOW() - INTERVAL '24 hours') AS resolved_24h,
           COUNT(*) FILTER (WHERE resolved_at >= NOW() - INTERVAL '7 days')   AS resolved_7d
    FROM brain_findings
"""

_SQL_AVG_MTTR_7D = """
    SELECT AVG(EXTRACT(EPOCH FROM
               (resolved_at - COALESCE(episode_started_at, first_seen))) / 3600.0)
    FROM brain_findings
    WHERE resolved_at >= NOW() - INTERVAL '7 days'
      AND COALESCE(episode_started_at, first_seen) IS NOT NULL
      AND resolved_at >= COALESCE(episode_started_at, first_seen)
"""

_SQL_MTTR_BY_DETECTOR = """
    SELECT COALESCE(detector, 'unknown') AS detector,
           COUNT(*) AS resolved_7d,
           AVG(EXTRACT(EPOCH FROM
               (resolved_at - COALESCE(episode_started_at, first_seen))) / 3600.0)
               AS avg_mttr_h
    FROM brain_findings
    WHERE resolved_at >= NOW() - INTERVAL '7 days'
      AND COALESCE(episode_started_at, first_seen) IS NOT NULL
      AND resolved_at >= COALESCE(episode_started_at, first_seen)
    GROUP BY 1
    ORDER BY 2 DESC, 3 DESC
    LIMIT 10
"""

# chronic_score = hours_open × re-observations PER DAY (r-episode-rate
# 2026-07-18). The original hours_open × episode_seen_count degenerated
# live: the 07-17 backfill stamped episode_started_at=first_seen (34d ago)
# but every counter began at 1 that day, so all always-refiring findings
# carried the identical count (86 = scans since the DDL) and the product
# collapsed to age-ranking with a 4-way tie at the top. Counting reobs
# over the window the counter ACTUALLY ran (episode start, floored at the
# DDL date) yields a rate that separates a finding screaming every scan
# from one seen twice a week — and stops double-counting elapsed time
# (count ∝ rate × time made the old score ∝ hours²).
_EPISODE_COUNTER_EPOCH = "2026-07-17"  # episode DDL ship date (commit 81360b0b)

_SQL_CHRONIC_LEADERBOARD = f"""
    WITH open_rows AS (
        SELECT issue, url, detector,
               EXTRACT(EPOCH FROM
                   (NOW() - COALESCE(episode_started_at, first_seen))) / 3600.0
                   AS hours_open,
               COALESCE(episode_seen_count, 1) AS re_observations,
               COALESCE(episode_count, 1)      AS episode_count,
               GREATEST(EXTRACT(EPOCH FROM (NOW() -
                   GREATEST(COALESCE(episode_started_at, first_seen),
                            TIMESTAMPTZ '{_EPISODE_COUNTER_EPOCH}'))) / 3600.0,
                        1.0) AS hours_counting
        FROM brain_findings
        WHERE status = 'open'
          AND COALESCE(episode_started_at, first_seen) IS NOT NULL
    )
    SELECT issue, url, detector, hours_open, re_observations, episode_count,
           re_observations / hours_counting * 24.0 AS reobs_per_day,
           hours_open * (re_observations / hours_counting * 24.0)
               AS chronic_score
    FROM open_rows
    ORDER BY 8 DESC NULLS LAST, 4 DESC
    LIMIT 15
"""

_SQL_FLAPPIEST = """
    SELECT issue, url, detector,
           COALESCE(episode_count, 1)      AS episode_count,
           status,
           COALESCE(episode_seen_count, 1) AS re_observations
    FROM brain_findings
    ORDER BY COALESCE(episode_count, 1) DESC, last_seen DESC NULLS LAST
    LIMIT 5
"""


def _r1(v):
    """round-to-1dp that tolerates None/Decimal."""
    try:
        return round(float(v), 1)
    except Exception:
        return None


@episode_analytics_bp.route("/api/v1/admin/brain/episodes/analytics",
                            methods=["GET"])
def episodes_analytics():
    """Chronic-episode leaderboard + MTTR vitals. Admin-only, read-only."""
    if not _admin_ok():
        return jsonify(ok=False, error="forbidden"), 403

    out = {
        "ok": True,
        "as_of": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "open_now": None,
        "resolved_24h": None,
        "resolved_7d": None,
        "avg_mttr_h": None,
        "mttr_by_detector": [],
        "chronic_leaderboard": [],
        "flappiest": [],
        "note": ("Episode ledger analytics (brain_findings, semantics "
                 "2026-07-17). chronic_score = hours_open of the CURRENT "
                 "episode × reobs_per_day (re-observation rate over the "
                 "window the episode counter actually ran) — work the top "
                 "of chronic_leaderboard, not the inbox."),
    }

    with _read_db() as conn:
        if conn is None:
            return jsonify(ok=False, error="no db"), 503

        vit = _rows(conn, _SQL_VITALS)
        if vit:
            try:
                out["open_now"] = int(vit[0][0] or 0)
                out["resolved_24h"] = int(vit[0][1] or 0)
                out["resolved_7d"] = int(vit[0][2] or 0)
            except Exception as e:
                logger.debug("[episodes] malformed vitals row: %s", e)

        mttr = _rows(conn, _SQL_AVG_MTTR_7D)
        if mttr and mttr[0]:
            out["avg_mttr_h"] = _r1(mttr[0][0])

        for r in (_rows(conn, _SQL_MTTR_BY_DETECTOR) or []):
            try:
                out["mttr_by_detector"].append({
                    "detector": r[0],
                    "resolved_7d": int(r[1] or 0),
                    "avg_mttr_h": _r1(r[2]),
                })
            except Exception as e:
                logger.debug("[episodes] malformed detector row skipped: %s", e)

        for r in (_rows(conn, _SQL_CHRONIC_LEADERBOARD) or []):
            try:
                out["chronic_leaderboard"].append({
                    "issue": r[0],
                    "url": r[1],
                    "detector": r[2],
                    "hours_open": _r1(r[3]) or 0.0,
                    "re_observations": int(r[4] or 1),
                    "episode_count": int(r[5] or 1),
                    "reobs_per_day": _r1(r[6]),
                    "chronic_score": _r1(r[7]),
                })
            except Exception as e:
                logger.debug("[episodes] malformed leaderboard row skipped: %s", e)

        for r in (_rows(conn, _SQL_FLAPPIEST) or []):
            try:
                out["flappiest"].append({
                    "issue": r[0],
                    "url": r[1],
                    "detector": r[2],
                    "episode_count": int(r[3] or 1),
                    "status": r[4],
                    "re_observations": int(r[5] or 1),
                })
            except Exception as e:
                logger.debug("[episodes] malformed flappiest row skipped: %s", e)

    return jsonify(out), 200, {"Cache-Control": "no-store, max-age=0",
                               "X-DC-Hub-Surface": "brain-episode-analytics"}
