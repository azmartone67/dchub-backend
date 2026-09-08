"""news_ingest_health.py — does news ingestion still ADD anything? (2026-09-08)

Between 2026-09-03 and 2026-09-07 `news_articles` intake fell from ~180 rows/day
to ~15, and **every existing check stayed green for five days**. Nothing was
broken in a way anything here could see:

  · `data-sync.yml` judges the news refresh by the `cron_last_run` WATERMARK —
    "did the job complete", never "did it add anything". Its own log line says
    so: `news-refresh: new_articles UNKNOWN (a relayed 202 carries none)`. A run
    that inserts zero articles and a healthy one are the same green tick.
  · The QA super-user board renders freshness as a PASSING gauge — "newest
    published story is 1.0 days old" — which stays true while volume collapses.
    **Fresh is not growth.** One article a day keeps every freshness check happy.
  · `/api/v1/data-freshness` does not track news at all (8 sources, none of them
    content) and returns `success: true` while silently listing `facilities`
    under `skipped` on a column error.

So this module measures the one thing none of them do: **rows actually added,
against the platform's own trailing baseline.**

★★★ THE COLUMN TRAP THIS GUARD EXISTS BECAUSE OF.
`news_articles.created_at` is NULL on all 12,271 rows — a dead column. A daily
count grouped on it returns ZERO ROWS, which reads exactly like "ingestion
stopped" and sent the 09-08 investigation down a false path. The ingest clock is
`fetched_at`. `_self_test`'s `clock_column_live` leg asserts the column this
guard counts on is actually populated, so a NULL column can never masquerade as
a collapse — and, just as important, so a collapse can never be dismissed as a
NULL column.

★★★ NO INVENTED TARGETS. The floor is not a number someone liked: it is derived
from this platform's OWN trailing history (`_BASELINE_LOOKBACK_DAYS`, excluding
the recent window being judged). If the baseline cannot be derived the verdict is
`unmeasurable` — never `ok`. cf the QA super-user's rule 3 and
feedback_scan_that_can_find_nothing_needs_a_floor: a check that can find nothing
must say so out loud rather than report a clean bill of health.

★ BROAD SWEEP vs DRIBBLE. Healthy intake arrives as one batch touching many
sources at once (2026-09-08 18:39 wrote 34 rows across 33 sources). A degraded
day writes 3-4 rows from 3-4 sources. Counting rows alone hides that, because a
single chatty source can hold the row count up while 30 feeds are dark — so
`broad_sweeps_24h` is reported alongside, keyed on DISTINCT SOURCES per batch.

Auth: X-Admin-Key (DCHUB_ADMIN_KEY / DCHUB_INTERNAL_KEY).
Read-only by default. `?file_finding=1` (POST) writes ONE brain finding when the
verdict is bad, so the stall reaches the brain instead of only a dashboard.
"""
from __future__ import annotations

import os

from flask import Blueprint, jsonify, request

news_ingest_health_bp = Blueprint("news_ingest_health", __name__)

# The ingest clock. NOT created_at — see the column trap in the docstring.
_CLOCK = "fetched_at"

# A write batch counts as a "broad sweep" at this many distinct sources. Healthy
# sweeps hit 15-33; degraded days top out at 4. 10 sits in the empty middle.
_BROAD_SWEEP_SOURCES = 10

# The driver (data-sync.yml, `30 */3 * * *`) fires every 3h. 24h with no broad
# sweep is eight consecutive missed windows — not variance.
_STALL_HOURS = 24

# Trailing baseline window, and the gap that keeps the window being JUDGED out
# of the baseline that judges it.
_BASELINE_LOOKBACK_DAYS = 30
_BASELINE_EXCLUDE_RECENT_DAYS = 7

# Below this share of the trailing median day, intake is degraded.
_DEGRADED_RATIO = 0.25

# The baseline needs this many observed days to mean anything.
_MIN_BASELINE_DAYS = 5


def _admin_ok() -> bool:
    expected = os.environ.get("DCHUB_ADMIN_KEY") or os.environ.get("DCHUB_INTERNAL_KEY")
    provided = (
        request.headers.get("X-Admin-Key")
        or request.headers.get("X-Internal-Key")
        or request.args.get("admin_key")
        or ""
    )
    return bool(expected) and provided == expected


def _conn():
    url = os.environ.get("NEON_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not url:
        return None
    import psycopg2
    return psycopg2.connect(url, connect_timeout=8)


def _measure(cur) -> dict:
    """Every number this guard publishes, and nothing it does not measure."""
    out: dict = {}

    cur.execute(
        f"SELECT count(*) FROM news_articles WHERE {_CLOCK} > NOW() - INTERVAL '24 hours'")
    out["rows_24h"] = int(cur.fetchone()[0] or 0)

    cur.execute(
        f"SELECT count(DISTINCT source) FROM news_articles "
        f"WHERE {_CLOCK} > NOW() - INTERVAL '24 hours'")
    out["distinct_sources_24h"] = int(cur.fetchone()[0] or 0)

    cur.execute(
        f"SELECT count(*) FROM news_articles WHERE {_CLOCK} > NOW() - INTERVAL '7 days'")
    out["rows_7d"] = int(cur.fetchone()[0] or 0)

    # Broad sweeps in the last 24h: write batches touching many sources at once.
    cur.execute(
        f"""SELECT count(*) FROM (
                SELECT date_trunc('minute', {_CLOCK}) AS b
                  FROM news_articles
                 WHERE {_CLOCK} > NOW() - INTERVAL '24 hours'
                 GROUP BY 1
                HAVING count(DISTINCT source) >= %s) s""",
        (_BROAD_SWEEP_SOURCES,))
    out["broad_sweeps_24h"] = int(cur.fetchone()[0] or 0)

    # When did a broad sweep last happen at all?
    cur.execute(
        f"""SELECT max(b), EXTRACT(EPOCH FROM (NOW() - max(b))) / 3600.0 FROM (
                SELECT date_trunc('minute', {_CLOCK}) AS b
                  FROM news_articles
                 WHERE {_CLOCK} > NOW() - INTERVAL '%s days'
                 GROUP BY 1
                HAVING count(DISTINCT source) >= %s) s"""
        % (_BASELINE_LOOKBACK_DAYS, _BROAD_SWEEP_SOURCES))
    row = cur.fetchone()
    out["last_broad_sweep_at"] = row[0].isoformat() if row and row[0] else None
    out["hours_since_broad_sweep"] = (
        round(float(row[1]), 1) if row and row[1] is not None else None)

    # Trailing baseline — the platform's own median day, excluding the window
    # under judgement so a collapse cannot lower the bar it is measured against.
    cur.execute(
        f"""SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY n), count(*) FROM (
                SELECT {_CLOCK}::date AS d, count(*) AS n
                  FROM news_articles
                 WHERE {_CLOCK} <  NOW() - INTERVAL '%s days'
                   AND {_CLOCK} >= NOW() - %s * INTERVAL '1 day'
                 GROUP BY 1) x"""
        % (_BASELINE_EXCLUDE_RECENT_DAYS, _BASELINE_LOOKBACK_DAYS))
    row = cur.fetchone()
    out["baseline_median_rows_per_day"] = (
        round(float(row[0]), 1) if row and row[0] is not None else None)
    out["baseline_days_observed"] = int(row[1] or 0) if row else 0
    return out


def _self_test(cur) -> dict:
    """Three legs. Any failure withholds the verdict — an unverified guard's
    output is worse than none, because it is trusted."""
    legs: dict = {}

    # 1. The table this guard counts must exist and hold rows. "No table" and
    #    "no ingestion" must never render the same.
    try:
        cur.execute("SELECT to_regclass('public.news_articles') IS NOT NULL")
        present = bool(cur.fetchone()[0])
    except Exception:
        present = False
    legs["table_present"] = {
        "passed": present,
        "why": "an absent table would otherwise count as zero ingestion",
    }

    # 2. ★ THE COLUMN TRAP. The clock must be populated. created_at is NULL on
    #    every row of this table; counting on it returns zero and reads exactly
    #    like a collapse. A guard keyed on a dead column is not a guard.
    live = False
    non_null = 0
    if present:
        try:
            cur.execute(f"SELECT count({_CLOCK}) FROM news_articles")
            non_null = int(cur.fetchone()[0] or 0)
            live = non_null > 0
        except Exception:
            live = False
    legs["clock_column_live"] = {
        "passed": live,
        "column": _CLOCK,
        "non_null_rows": non_null,
        "why": ("a NULL clock column returns 0 for every window and is "
                "indistinguishable from stalled ingestion"),
    }

    # 3. Negative canary: a window that cannot contain rows must report zero.
    #    Catches a comparison that always passes — the shape that makes a
    #    counter incapable of ever reporting a problem.
    zero_ok = False
    if present:
        try:
            cur.execute(
                f"SELECT count(*) FROM news_articles "
                f"WHERE {_CLOCK} > NOW() + INTERVAL '365 days'")
            zero_ok = int(cur.fetchone()[0] or 0) == 0
        except Exception:
            zero_ok = False
    legs["negative_canary"] = {
        "passed": zero_ok,
        "why": "a window in the far future must be empty; if it is not, the filter is inert",
    }

    return {"passed": all(l["passed"] for l in legs.values()), "legs": legs}


def _verdict(m: dict) -> dict:
    """Turn measurements into a judgement, and state the failure condition."""
    base = m.get("baseline_median_rows_per_day")
    days = m.get("baseline_days_observed") or 0
    hours = m.get("hours_since_broad_sweep")

    red_when = (
        f"no write batch touching >= {_BROAD_SWEEP_SOURCES} distinct sources in "
        f"{_STALL_HOURS}h (the driver fires every 3h), OR rows in 24h below "
        f"{int(_DEGRADED_RATIO * 100)}% of the trailing median day")

    if base is None or days < _MIN_BASELINE_DAYS:
        return {
            "verdict": "unmeasurable",
            "ok": False,
            "why": (f"trailing baseline needs >= {_MIN_BASELINE_DAYS} observed days, "
                    f"got {days} — cannot judge, and will not report ok"),
            "red_when": red_when,
        }

    floor = base * _DEGRADED_RATIO
    if hours is None or hours > _STALL_HOURS:
        return {
            "verdict": "stalled",
            "ok": False,
            "why": (f"last broad sweep {hours if hours is not None else 'never'} h ago "
                    f"(> {_STALL_HOURS}h); the news driver fires every 3h"),
            "floor_rows_per_day": round(floor, 1),
            "red_when": red_when,
        }
    if m["rows_24h"] < floor:
        return {
            "verdict": "degraded",
            "ok": False,
            "why": (f"{m['rows_24h']} rows in 24h is under {round(floor, 1)} "
                    f"({int(_DEGRADED_RATIO * 100)}% of the {base}/day trailing median)"),
            "floor_rows_per_day": round(floor, 1),
            "red_when": red_when,
        }
    return {
        "verdict": "ok",
        "ok": True,
        "why": (f"{m['rows_24h']} rows in 24h from {m['distinct_sources_24h']} sources, "
                f"{m['broad_sweeps_24h']} broad sweep(s)"),
        "floor_rows_per_day": round(floor, 1),
        "red_when": red_when,
    }


def _file_finding(cur, v: dict, m: dict) -> str:
    """One finding, so a stall reaches the brain instead of only a dashboard."""
    from routes.brain_findings_writer import upsert_brain_finding
    return upsert_brain_finding(
        cur,
        issue=f"news_ingest_{v['verdict']}",
        url="/api/v1/admin/news/ingest-health",
        count=1,
        detail=(f"{v['why']}. rows_24h={m['rows_24h']} "
                f"sources_24h={m['distinct_sources_24h']} "
                f"broad_sweeps_24h={m['broad_sweeps_24h']} "
                f"baseline={m['baseline_median_rows_per_day']}/day"),
        detector="news_ingest_health",
        status="open")


@news_ingest_health_bp.route("/api/v1/admin/news/ingest-health",
                             methods=["GET", "POST"])
def news_ingest_health():
    if not _admin_ok():
        return jsonify(ok=False, error="admin key required"), 401

    c = None
    try:
        c = _conn()
        if c is None:
            return jsonify(ok=False, verdict="unmeasurable",
                           error="no DATABASE_URL"), 200
        with c.cursor() as cur:
            st = _self_test(cur)
            if not st["passed"]:
                # Withhold the verdict rather than publish one from a guard
                # that has not proved it can observe.
                return jsonify(ok=False, verdict="unmeasurable",
                               self_test=st,
                               why="self-test failed; measurements withheld"), 200
            m = _measure(cur)
            v = _verdict(m)

            filed = None
            if (request.method == "POST"
                    and request.args.get("file_finding") in ("1", "true")
                    and not v["ok"]):
                try:
                    filed = _file_finding(cur, v, m)
                    c.commit()
                except Exception as e:
                    filed = f"error: {type(e).__name__}"
    except Exception as e:
        return jsonify(ok=False, verdict="unmeasurable",
                       error=f"{type(e).__name__}: {str(e)[:160]}"), 200
    finally:
        if c is not None:
            try:
                c.close()
            except Exception:
                pass

    body = dict(v)
    body["measurements"] = m
    body["self_test"] = st
    body["thresholds"] = {
        "broad_sweep_min_sources": _BROAD_SWEEP_SOURCES,
        "stall_hours": _STALL_HOURS,
        "degraded_ratio": _DEGRADED_RATIO,
        "baseline_lookback_days": _BASELINE_LOOKBACK_DAYS,
        "baseline_excludes_recent_days": _BASELINE_EXCLUDE_RECENT_DAYS,
        "clock_column": _CLOCK,
    }
    if filed is not None:
        body["finding_filed"] = filed
    return jsonify(body), 200
