"""Dead-man ledger + overdue view — the linchpin from the 2026-07-19 loop audit.

Every scheduled feed BEATS {feed,status,rows_inserted,max_content_date} here after
each run; an INDEPENDENT off-worker watcher (.github/workflows/deadman-watch.yml on
GitHub Actions — NOT the single APScheduler thread that drives all ~75 in-worker jobs)
reads /api/v1/ops/deadman and alarms (GH issue) when a loop hasn't SUCCEEDED within 2x
its cadence, inserted 0 rows across recent runs, or emitted a content date in the future.

"Cron green proves nothing": health here = "the loop ran AND inserted sane data",
never "a row exists" or "a timestamp looks recent".
"""
import os
import datetime
import logging

import psycopg2
from flask import Blueprint, jsonify, request

log = logging.getLogger("ingest_runs")
ingest_runs_bp = Blueprint("ingest_runs", __name__)

# Fallback cadence (hours) when a beat did not carry one. The authoritative registry
# lives in the watcher (tools/deadman/watch.py); this is only a safety net so a beat
# that forgets cadence_hours still gets a sane overdue threshold.
_DEFAULT_CADENCE_H = 48.0
# Statuses that are NOT themselves an alarm (the loop ran and was fine / intentionally idle).
_OK_STATUS = {"success", "ok", "idle", "no-op", "noop", "skipped", ""}


def _dsn():
    return (
        os.environ.get("DATABASE_URL")
        or os.environ.get("NEON_DATABASE_URL")
        or os.environ.get("POSTGRES_URL")
        or ""
    )


def _admin_ok():
    exp = os.environ.get("DCHUB_ADMIN_KEY") or os.environ.get("DCHUB_INTERNAL_KEY") or ""
    got = (
        request.headers.get("X-Admin-Key")
        or request.headers.get("X-Internal-Key")
        or request.args.get("admin_key")
        or ""
    )
    return bool(exp) and got == exp


def _ensure(cur):
    cur.execute(
        """CREATE TABLE IF NOT EXISTS ingest_runs (
            feed              TEXT PRIMARY KEY,
            last_run          TIMESTAMPTZ,
            last_status       TEXT,
            rows_inserted     BIGINT,
            max_content_date  TIMESTAMPTZ,
            cadence_hours     NUMERIC,
            consecutive_zero  INT DEFAULT 0,
            note              TEXT,
            updated_at        TIMESTAMPTZ DEFAULT NOW()
        )"""
    )


@ingest_runs_bp.route("/api/v1/admin/ingest-runs/beat", methods=["POST"])
def beat():
    """A scheduler (or the off-worker watcher) records its last SUCCESSFUL run.

    Body: {feed, status?, rows_inserted?, max_content_date?, cadence_hours?, last_run?, note?}
    Upsert — one row per feed, always the latest. consecutive_zero climbs while
    rows_inserted==0 and resets on any positive insert (the "rows=0 for N runs" alarm).
    """
    if not _admin_ok():
        return jsonify(ok=False, error="admin key required"), 401
    dsn = _dsn()
    if not dsn:
        return jsonify(ok=False, error="no DATABASE_URL"), 503
    j = request.get_json(silent=True) or {}
    feed = str(j.get("feed") or "").strip()[:80]
    if not feed:
        return jsonify(ok=False, error="feed required"), 400
    status = str(j.get("status") or "success").strip()[:40]
    rows = j.get("rows_inserted")
    try:
        rows = int(rows) if rows is not None else None
    except (TypeError, ValueError):
        rows = None
    mcd = (str(j.get("max_content_date")).strip() or None) if j.get("max_content_date") else None
    cad = j.get("cadence_hours")
    try:
        cad = float(cad) if cad is not None else None
    except (TypeError, ValueError):
        cad = None
    lr = (str(j.get("last_run")).strip() or None) if j.get("last_run") else None
    note = (str(j.get("note")).strip()[:280] or None) if j.get("note") else None
    # Sentinel so ON CONFLICT can tell "rows==0" (bump) from "rows unknown" (leave counter).
    rows_sig = rows if rows is not None else -1
    try:
        with psycopg2.connect(dsn, sslmode="require", connect_timeout=8) as c, c.cursor() as cur:
            _ensure(cur)
            cur.execute(
                """INSERT INTO ingest_runs
                       (feed, last_run, last_status, rows_inserted, max_content_date,
                        cadence_hours, consecutive_zero, note, updated_at)
                   VALUES (%s, COALESCE(%s::timestamptz, NOW()), %s, %s, %s::timestamptz, %s,
                           CASE WHEN %s = 0 THEN 1 ELSE 0 END, %s, NOW())
                   ON CONFLICT (feed) DO UPDATE SET
                       last_run         = COALESCE(EXCLUDED.last_run, ingest_runs.last_run),
                       last_status      = EXCLUDED.last_status,
                       rows_inserted    = EXCLUDED.rows_inserted,
                       max_content_date = COALESCE(EXCLUDED.max_content_date, ingest_runs.max_content_date),
                       cadence_hours    = COALESCE(EXCLUDED.cadence_hours, ingest_runs.cadence_hours),
                       consecutive_zero = CASE WHEN %s = 0
                                               THEN ingest_runs.consecutive_zero + 1 ELSE 0 END,
                       note             = COALESCE(EXCLUDED.note, ingest_runs.note),
                       updated_at       = NOW()""",
                (feed, lr, status, rows, mcd, cad, rows_sig, note, rows_sig),
            )
            c.commit()
    except Exception as e:  # noqa: BLE001 — fail soft, ledger must never 500 a caller into a retry storm
        log.warning("ingest_runs beat failed for %s: %s", feed, e)
        return jsonify(ok=False, error=str(e)[:200]), 500
    return jsonify(ok=True, feed=feed)


@ingest_runs_bp.route("/api/v1/ops/deadman", methods=["GET"])
def deadman():
    """PUBLIC read — the overdue view. `any_overdue=true` ⇒ a loop stopped succeeding.

    A feed is overdue when ANY holds:
      • last success older than 2x its cadence (the classic dead-man),
      • last_status is not a known-OK status (it ran and reported failure/anomaly),
      • >=3 consecutive zero-row runs (loop fires but inserts nothing),
      • max_content_date is in the future (>6h ahead — the news-future-date rot class).
    """
    dsn = _dsn()
    if not dsn:
        return jsonify(ok=False, error="no DATABASE_URL"), 503
    now = datetime.datetime.now(datetime.timezone.utc)
    try:
        with psycopg2.connect(dsn, sslmode="require", connect_timeout=8) as c, c.cursor() as cur:
            _ensure(cur)
            cur.execute(
                """SELECT feed, last_run, last_status, rows_inserted, max_content_date,
                          cadence_hours, consecutive_zero, note
                     FROM ingest_runs"""
            )
            rows = cur.fetchall()
    except Exception as e:  # noqa: BLE001
        log.warning("ingest_runs deadman read failed: %s", e)
        return jsonify(ok=False, error=str(e)[:200]), 500

    feeds, overdue = [], []
    for feed, lr, st, ri, mcd, cad, cz, note in rows:
        cad_h = float(cad) if cad is not None else _DEFAULT_CADENCE_H
        reasons = []
        if lr is None:
            reasons.append("never ran")
        else:
            age_h = (now - lr).total_seconds() / 3600.0
            if age_h > 2 * cad_h:
                reasons.append(f"last success {age_h:.0f}h ago (>2x cadence {cad_h:.0f}h)")
        if st and st.lower() not in _OK_STATUS:
            reasons.append(f"status={st}")
        if cz and cz >= 3:
            reasons.append(f"{cz} consecutive zero-row runs")
        if mcd and mcd > now + datetime.timedelta(hours=6):
            reasons.append(f"content date in the FUTURE ({mcd.date().isoformat()})")
        rec = {
            "feed": feed,
            "last_run": lr.isoformat() if lr else None,
            "status": st,
            "rows_inserted": ri,
            "cadence_hours": cad_h,
            "age_hours": round((now - lr).total_seconds() / 3600.0, 1) if lr else None,
            "overdue": bool(reasons),
            "reasons": reasons,
        }
        if note:
            rec["note"] = note
        feeds.append(rec)
        if reasons:
            overdue.append(rec)

    feeds.sort(key=lambda r: (not r["overdue"], r["feed"]))
    resp = jsonify(
        ok=True,
        generated_at=now.isoformat(),
        tracked=len(feeds),
        any_overdue=bool(overdue),
        overdue_count=len(overdue),
        overdue=overdue,
        feeds=feeds,
    )
    resp.headers["Cache-Control"] = "public, max-age=60"
    return resp


def register_ingest_runs(app):
    try:
        app.register_blueprint(ingest_runs_bp)
        log.info("ingest_runs (dead-man ledger) registered")
    except Exception as e:  # noqa: BLE001
        log.warning("ingest_runs register failed: %s", e)
