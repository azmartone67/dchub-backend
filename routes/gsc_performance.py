"""Daily Google Search Console performance ingestion.

WHY
---
As of 2026-08-31 the SEO measurement layer held no time series at all. Verified
row counts, all-time:

    gsc_crawl_errors            0
    gsc_index_requests          0
    seo_backlinks               0
    seo_stats                   0
    seo_content_opportunities   0
    seo_indexing_log (42 d)     0
    seo_proven_pages       21,672   <- the only GSC-derived table with rows

So the site knew what it PUBLISHED (21,672 pages, sitemap rebuilt daily) and
nothing about what any of it EARNED. No impressions, no clicks, no positions, no
trend. Every SEO judgement — including the 2026-07-30 decision to add
`Disallow: /api/` for Bingbot, whose own robots.txt comment concedes it "closes
Copilot's only surface" — was made blind and has stayed unmeasured since.

`seo_proven_pages` is not a substitute. It is a rolling 90-day SNAPSHOT keyed by
slug, upserted in place: it answers "does this page have impressions" and can
never answer "is this page rising or falling", because yesterday's value is
overwritten. A snapshot cannot become a trend retroactively — the series has to
start being recorded before it can be read. That is this module.

WHAT IT RECORDS
---------------
One table, three grains, distinguished by `dimension`:

    site   one row per day  — the trend line
    query  top N per day    — what we actually rank for
    page   top N per day    — which pages earn

Storing all three in one table keeps the read side to a single query shape and
lets a caller compare grains on the same axis. `dim_value` is '' for the site
grain (not NULL — it is part of the primary key, and NULL would let duplicate
site rows accumulate silently).

TWO PROPERTIES THAT MATTER
--------------------------
1. **Re-ingest is safe and required.** GSC finalises a day's data over roughly
   72 hours, so a day fetched too early is an undercount that never corrects
   itself. Every run re-fetches a trailing window (default 5 days) and upserts,
   so late-arriving data lands. The primary key (date, dimension, dim_value)
   makes that idempotent — running twice an hour and running once a day produce
   the same table.

2. **A short window is not a backfill.** GSC retains 16 months; this ingests
   whatever `days` asks for. Pass `days=480` once to seed history, then let the
   daily cron carry the trailing window. Until that seed runs, absence of old
   rows means "not yet fetched", NOT "no traffic" — the read route says so in
   `coverage` rather than letting a caller infer a zero.

Auth, token caching and the site constant are reused from
`google_search_console.py` — this module adds a series, not a second integration.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta

import requests
from flask import Blueprint, jsonify, request

from db_utils import get_db
from google_search_console import (GSC_SITE_URL, get_access_token,
                                   refresh_proven_pages)
from internal_auth import require_internal_or_admin

logger = logging.getLogger(__name__)

gsc_perf_bp = Blueprint("gsc_performance", __name__)

# GSC finalises a day over ~72 h. Re-fetch a trailing window every run so an
# early read self-corrects instead of freezing an undercount.
DEFAULT_WINDOW_DAYS = int(os.environ.get("GSC_PERF_WINDOW_DAYS", "5"))

# Per-day cap for the query and page grains. The site grain is always 1 row/day.
# 500 keeps a year of daily ingest well inside a few hundred thousand rows while
# still covering the long tail that matters for content decisions.
DEFAULT_ROW_LIMIT = int(os.environ.get("GSC_PERF_ROW_LIMIT", "500"))

_API = "https://www.googleapis.com/webmasters/v3/sites/{site}/searchAnalytics/query"

_DDL = """
CREATE TABLE IF NOT EXISTS gsc_daily_performance (
    date        DATE        NOT NULL,
    dimension   TEXT        NOT NULL,
    dim_value   TEXT        NOT NULL DEFAULT '',
    clicks      INTEGER     NOT NULL DEFAULT 0,
    impressions INTEGER     NOT NULL DEFAULT 0,
    ctr         REAL        NOT NULL DEFAULT 0,
    position    REAL,
    ingested_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (date, dimension, dim_value)
)
"""

# date DESC is the access pattern for every read here (latest first, windowed).
_DDL_INDEX = """
CREATE INDEX IF NOT EXISTS idx_gsc_perf_dim_date
    ON gsc_daily_performance (dimension, date DESC)
"""

_UPSERT = """
INSERT INTO gsc_daily_performance
    (date, dimension, dim_value, clicks, impressions, ctr, position)
VALUES {values}
ON CONFLICT (date, dimension, dim_value) DO UPDATE SET
    clicks      = EXCLUDED.clicks,
    impressions = EXCLUDED.impressions,
    ctr         = EXCLUDED.ctr,
    position    = EXCLUDED.position,
    ingested_at = NOW()
"""


def _ensure_table() -> None:
    """Direct DDL. safe_db SKIPs DDL — the trap already documented in
    auto_trial._ensure_bind_receipt_log, free_tier_limiter, linkedin_posts_schema
    and intelligence_engine. A CREATE issued through the wrapper is silently
    surrendered and the first INSERT then fails on a table that was never made."""
    conn = get_db()
    try:
        c = conn.cursor()
        raw = getattr(c, "_cur", c)
        raw.execute(_DDL)
        raw.execute(_DDL_INDEX)
        conn.commit()
    finally:
        try:
            conn.close()
        except Exception:
            pass


# Google's hard ceiling for a single searchAnalytics response. Asking for more
# is a 400, not a truncation — the 2026-08-31 480-day seed failed both the query
# and page grains with:
#   "'240000' is not a valid row limit value"
# because the caller multiplied a per-day limit by the window length. Anything
# above this must be PAGINATED with startRow, never requested in one call.
_GSC_MAX_ROW_LIMIT = 25000

# Upper bound on rows fetched per grain for one ingest, across all pages.
# Keeps a 16-month seed bounded in time and storage; reported to the caller
# whenever it actually bites.
_SEED_ROW_CEILING = int(os.environ.get("GSC_PERF_SEED_CEILING", "100000"))


def _query_gsc(token: str, start: str, end: str, dimensions: list[str],
               row_limit: int, max_pages: int = 20) -> tuple[list[dict] | None, str | None]:
    """searchAnalytics, paginated. Returns (rows, error).

    Pages with startRow until a short page arrives, `row_limit` rows are
    collected, or max_pages is hit. Same shape as
    google_search_console.refresh_proven_pages, which already learned that a
    single call silently truncates to the top N by clicks — which would drop
    exactly the high-impression/low-click rows this series exists to surface.

    ★ A partial page is NOT an error. Stopping early on a short page is how we
    know we reached the end; running out of max_pages with full pages IS worth
    knowing, so it is reported rather than silently accepted."""
    site = GSC_SITE_URL.replace(":", "%3A").replace("/", "%2F")
    want = max(1, int(row_limit))
    per_call = min(want, _GSC_MAX_ROW_LIMIT)

    rows: list[dict] = []
    start_row = 0
    for _ in range(max(1, int(max_pages))):
        body = {
            "startDate": start,
            "endDate": end,
            "dimensions": dimensions,
            "rowLimit": per_call,
            "startRow": start_row,
        }
        try:
            resp = requests.post(
                _API.format(site=site),
                headers={"Authorization": f"Bearer {token}",
                         "Content-Type": "application/json"},
                json=body, timeout=90,
            )
        except Exception as e:  # noqa: BLE001
            return None, f"{type(e).__name__}: {str(e)[:200]}"
        if resp.status_code != 200:
            return None, f"HTTP {resp.status_code}: {resp.text[:300]}"

        batch = resp.json().get("rows", []) or []
        rows.extend(batch)
        if len(batch) < per_call:
            break                      # short page — end of data
        if len(rows) >= want:
            break                      # caller has what it asked for
        start_row += len(batch)

    return rows, None


def ingest_daily_performance(token: str, days: int = DEFAULT_WINDOW_DAYS,
                             row_limit: int = DEFAULT_ROW_LIMIT,
                             refresh_proven: bool = True) -> dict:
    """Fetch and upsert `days` of daily performance at all three grains.

    Idempotent: re-running over the same window updates in place, so a cron that
    double-fires and a manual re-run both converge on the same rows.

    F4 (2026-09-02): also refreshes `seo_proven_pages` — the table sitemap
    admission (#2946) reads to readmit GSC-proven facility URLs past the
    capacity gate. That table had NO caller: POST /api/gsc/proven/refresh was
    only ever hand-fired, and /api/gsc/proven read last_refreshed
    2026-08-24 06:45:03 on 2026-09-02 while the sitemap rebuilt every 4h on
    a frozen admission list. The one daily cron that already holds a GSC token
    now carries both writes; a proven refresh failure is an ingest failure by
    the partial-failure rule below, never a quiet skip."""
    if not token:
        return {"success": False, "error": "no GSC access token "
                                           "(GOOGLE_SERVICE_ACCOUNT_JSON unset "
                                           "or service account not verified)"}

    # GSC has no data for the last ~2 days; asking anyway is harmless (it
    # returns nothing) but the window must extend far enough back to be useful.
    end = datetime.utcnow().date()
    start = end - timedelta(days=max(1, int(days)))
    s, e = start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")

    _ensure_table()

    # row_limit is PER DAY, so the total wanted scales with the window — but it
    # must be requested through pagination, never as one oversized rowLimit.
    # The 2026-08-31 seed asked for 500 x 480 = 240,000 in a single call and
    # Google rejected it outright ("not a valid row limit value"), losing both
    # the query and page grains while the site grain succeeded.
    #
    # Capped at _SEED_ROW_CEILING so a 16-month seed cannot page forever: at 500
    # per day that ceiling is reached around a 200-day window, and beyond it the
    # long tail is thinner than the storage and time it costs. The cap is
    # REPORTED (see rows_capped below) rather than applied silently — a
    # truncation nobody is told about reads as complete coverage.
    _wanted = row_limit * max(1, int(days))
    _per_grain = min(_wanted, _SEED_ROW_CEILING)
    grains = (
        # (dimension label, GSC dimensions, row limit)
        # 'date' is always first so keys[0] is the day at every grain.
        ("site",  ["date"],          _GSC_MAX_ROW_LIMIT),
        ("query", ["date", "query"], _per_grain),
        ("page",  ["date", "page"],  _per_grain),
    )

    written, errors, scanned = {}, {}, {}
    for label, dims, limit in grains:
        rows, err = _query_gsc(token, s, e, dims, limit)
        if err:
            errors[label] = err
            continue
        scanned[label] = len(rows)

        payload = []
        for r in rows:
            keys = r.get("keys") or []
            if not keys:
                continue
            day = keys[0]
            value = keys[1] if len(keys) > 1 else ""
            payload.append((
                day, label, (value or "")[:1024],
                int(r.get("clicks", 0) or 0),
                int(r.get("impressions", 0) or 0),
                float(r.get("ctr", 0) or 0),
                round(float(r.get("position", 0) or 0), 2) or None,
            ))
        if not payload:
            written[label] = 0
            continue

        conn = get_db()
        try:
            c = conn.cursor()
            # RAW cursor: the wrapper probes SELECT lastval() after any INSERT
            # without RETURNING. This table has a composite TEXT/DATE key and no
            # sequence, so lastval() is undefined, PG errors, and the open
            # transaction aborts — taking the next chunk with it. Same trap
            # refresh_proven_pages documents.
            raw = getattr(c, "_cur", c)
            n = 0
            for i in range(0, len(payload), 500):
                chunk = payload[i:i + 500]
                args = ",".join(["(%s,%s,%s,%s,%s,%s,%s)"] * len(chunk))
                flat = [f for row in chunk for f in row]
                raw.execute(_UPSERT.format(values=args), flat)
                n += len(chunk)
            conn.commit()
            written[label] = n
        except Exception as ex:  # noqa: BLE001
            errors[label] = f"{type(ex).__name__}: {str(ex)[:200]}"
            try:
                conn.rollback()
            except Exception:
                pass
        finally:
            try:
                conn.close()
            except Exception:
                pass

    proven = None
    if refresh_proven:
        try:
            proven = refresh_proven_pages(token)
        except Exception as ex:  # noqa: BLE001
            proven = {"success": False,
                      "error": f"{type(ex).__name__}: {str(ex)[:200]}"}
        if not (isinstance(proven, dict) and proven.get("success")):
            errors["proven_pages"] = str(
                (proven or {}).get("error") if isinstance(proven, dict) else proven
                or "refresh_proven_pages reported success:false")[:300]

    return {
        # A partial failure is a failure. Reporting success:true with one grain
        # missing is exactly the "green board, dead lane" pattern the audit found.
        "success": not errors,
        "window": {"start": s, "end": e, "days": int(days)},
        "rows_written": written,
        "gsc_rows_scanned": scanned,
        # F4: the sitemap-admission table this cron now also refreshes.
        "proven_pages": proven,
        # Say so when the ceiling bit. A silent truncation reads as full
        # coverage, which is the failure mode this whole module exists to refuse.
        "rows_capped": ({"per_grain_ceiling": _SEED_ROW_CEILING,
                         "wanted": _wanted,
                         "note": "query/page grains were capped; the long tail "
                                 "beyond this is not stored"}
                        if _wanted > _SEED_ROW_CEILING else None),
        "errors": errors or None,
    }


@gsc_perf_bp.route("/api/v1/admin/gsc/performance/ingest", methods=["POST"])
def admin_ingest():
    """Run an ingest. `?days=480` once to seed 16 months of history; the daily
    cron then carries the trailing window.

    ★ require_internal_or_admin is a PREDICATE — `require_internal_or_admin(req)
    -> bool` — not a decorator. Used as `@require_internal_or_admin` it was
    applied to this function, returned False (a function has no .headers), and
    the route then tried to register the bool `False` as a view. Flask needs
    `__name__` on a view, so registration raised

        'bool' object has no attribute '__name__'

    which main.py's try/except logged and swallowed. The whole blueprint failed
    to register, so BOTH routes 404'd — including the public read route that
    needs no auth at all. Call it as a guard, never as a decorator."""
    if not require_internal_or_admin(request):
        return jsonify({"success": False, "error": "unauthorized"}), 401
    try:
        days = int(request.args.get("days") or DEFAULT_WINDOW_DAYS)
    except (TypeError, ValueError):
        days = DEFAULT_WINDOW_DAYS
    days = max(1, min(days, 480))
    result = ingest_daily_performance(get_access_token(), days=days)
    return jsonify(result), (200 if result.get("success") else 502)


@gsc_perf_bp.route("/api/v1/seo/performance", methods=["GET"])
def read_performance():
    """The series, for dashboards, the brain, and anyone asking whether SEO is
    working.

    `?dimension=site|query|page` (default site), `?days=28`, `?limit=50`.

    ★ Always returns a `coverage` block. An empty result here means "not
    ingested", which is NOT the same as "no traffic" — until the 480-day seed
    runs, history is genuinely absent rather than zero. Read `coverage.oldest`
    before drawing a trend from this."""
    dim = (request.args.get("dimension") or "site").strip().lower()
    if dim not in ("site", "query", "page"):
        return jsonify({"success": False,
                        "error": "dimension must be site, query or page"}), 400
    try:
        days = max(1, min(int(request.args.get("days") or 28), 480))
    except (TypeError, ValueError):
        days = 28
    try:
        limit = max(1, min(int(request.args.get("limit") or 50), 1000))
    except (TypeError, ValueError):
        limit = 50

    conn = None
    try:
        conn = get_db()
        c = conn.cursor()
        raw = getattr(c, "_cur", c)

        raw.execute(
            "SELECT MIN(date), MAX(date), COUNT(*) "
            "FROM gsc_daily_performance WHERE dimension = %s", (dim,))
        oldest, newest, total = raw.fetchone() or (None, None, 0)

        if dim == "site":
            raw.execute(
                "SELECT date, clicks, impressions, ctr, position "
                "FROM gsc_daily_performance "
                " WHERE dimension = 'site' AND date >= CURRENT_DATE - %s "
                " ORDER BY date DESC", (days,))
            rows = [{"date": str(r[0]), "clicks": r[1], "impressions": r[2],
                     "ctr": round(float(r[3] or 0), 4),
                     "position": (round(float(r[4]), 2) if r[4] is not None else None)}
                    for r in raw.fetchall()]
        else:
            # Aggregate the window so a caller gets "top queries this month",
            # not one row per query per day they then have to sum themselves.
            # Position is impression-weighted — a flat AVG over days would let a
            # single 1-impression day at rank 3 outvote 10,000 impressions at 40.
            raw.execute(
                "SELECT dim_value, SUM(clicks), SUM(impressions), "
                "       CASE WHEN SUM(impressions) > 0 "
                "            THEN SUM(position * impressions) / SUM(impressions) "
                "            ELSE NULL END "
                "  FROM gsc_daily_performance "
                " WHERE dimension = %s AND date >= CURRENT_DATE - %s "
                " GROUP BY dim_value "
                " ORDER BY SUM(impressions) DESC "
                " LIMIT %s", (dim, days, limit))
            rows = [{"value": r[0], "clicks": int(r[1] or 0),
                     "impressions": int(r[2] or 0),
                     "position": (round(float(r[3]), 2) if r[3] is not None else None)}
                    for r in raw.fetchall()]

        return jsonify({
            "success": True,
            "dimension": dim,
            "window_days": days,
            "rows": rows,
            "coverage": {
                "oldest": str(oldest) if oldest else None,
                "newest": str(newest) if newest else None,
                "rows_stored": int(total or 0),
                "note": ("empty means NOT INGESTED, not zero traffic — "
                         "POST /api/v1/admin/gsc/performance/ingest?days=480 "
                         "seeds history"),
            },
        })
    except Exception as e:  # noqa: BLE001
        logger.warning("gsc performance read failed: %s", e)
        return jsonify({"success": False,
                        "error": f"{type(e).__name__}: {str(e)[:200]}"}), 500
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def register_gsc_performance_routes(app):
    app.register_blueprint(gsc_perf_bp)
    return gsc_perf_bp
