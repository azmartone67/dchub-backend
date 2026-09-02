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
from datetime import datetime, timedelta, timezone

import requests
from flask import Blueprint, jsonify, request

from db_utils import get_db
from google_search_console import GSC_SITE_URL, get_access_token
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
                             row_limit: int = DEFAULT_ROW_LIMIT) -> dict:
    """Fetch and upsert `days` of daily performance at all three grains.

    Idempotent: re-running over the same window updates in place, so a cron that
    double-fires and a manual re-run both converge on the same rows."""
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

    return {
        # A partial failure is a failure. Reporting success:true with one grain
        # missing is exactly the "green board, dead lane" pattern the audit found.
        "success": not errors,
        "window": {"start": s, "end": e, "days": int(days)},
        "rows_written": written,
        "gsc_rows_scanned": scanned,
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


def _coverage_of(raw, dim: str):
    """(oldest, newest, rows_stored) for one grain — the table-wide extent,
    NOT the window, so a caller can tell 'not ingested' from 'no traffic'."""
    raw.execute(
        "SELECT MIN(date), MAX(date), COUNT(*) "
        "FROM gsc_daily_performance WHERE dimension = %s", (dim,))
    return raw.fetchone() or (None, None, 0)


def _site_rows(raw, start, end) -> list[dict]:
    """The site grain, newest first, over the INCLUSIVE window [start, end].

    ★ 2026-09-02 merge note. #3566 introduced this helper taking `days` and
    running `date >= CURRENT_DATE - days`; #3569 gave the read route explicit
    start/end windows so it can compare a window against the one before it.
    The helper takes the window, and BOTH callers resolve their window through
    _resolve_windows — so the board and the API still run one query, which is
    the whole reason #3566 extracted it. With the defaults (end = today UTC =
    the DB's CURRENT_DATE, start = end - days) the predicate is the same set of
    dates as before: the added upper bound can only exclude future-dated rows,
    and the GSC series lags 2-3 days behind today.
    """
    raw.execute(
        "SELECT date, clicks, impressions, ctr, position "
        "FROM gsc_daily_performance "
        " WHERE dimension = 'site' AND date >= %s AND date <= %s "
        " ORDER BY date DESC", (start, end))
    return [{"date": str(r[0]), "clicks": r[1], "impressions": r[2],
             "ctr": round(float(r[3] or 0), 4),
             "position": (round(float(r[4]), 2) if r[4] is not None else None)}
            for r in raw.fetchall()]


_ORDERS = {
    # column expression over the CURRENT window → sort direction
    "impressions": ("cur_impr", "DESC"),
    "clicks": ("cur_clicks", "DESC"),
    "position": ("cur_pos", "ASC"),          # rank 1 is best → ascending
    # compare=1 only: what LOST the most between the two windows
    "lost_clicks": ("(prior_clicks - cur_clicks)", "DESC"),
    "lost_impressions": ("(prior_impr - cur_impr)", "DESC"),
}
MAX_READ_LIMIT = 5000


def _parse_iso_date(raw):
    """YYYY-MM-DD → date, or None when absent. Raises ValueError on junk."""
    raw = (raw or "").strip()
    if not raw:
        return None
    return datetime.strptime(raw, "%Y-%m-%d").date()


def _resolve_windows(days, start, end):
    """The current window [start, end] and the EQUAL-LENGTH window that
    precedes it, both inclusive.

    Defaults reproduce the pre-2026-09-02 predicate `date >= CURRENT_DATE -
    days` exactly: end = today (UTC, the DB's CURRENT_DATE), start = end -
    days — i.e. days+1 calendar dates. The prior window is the same number of
    dates ending the day before `start`, so a 7-vs-7 or 28-vs-28 comparison
    never compares 8 dates against 7 (the off-by-one that turns a flat week
    into a fake -12%).

    `end` alone anchors a window of `days` ending there (useful because the
    GSC series lags 2–3 days: end=newest gives a full trailing window)."""
    today = datetime.now(timezone.utc).date()
    if end is None:
        end = today
    if start is None:
        start = end - timedelta(days=days)
    if start > end:
        raise ValueError("start must not be after end")
    length = (end - start).days + 1
    prior_end = start - timedelta(days=1)
    prior_start = prior_end - timedelta(days=length - 1)
    return start, end, prior_start, prior_end


def site_series(days: int = 14) -> dict:
    """The site grain, IN-PROCESS — for boards and the brain, never via HTTP.

    ★ 2026-09-02. routes/surface_integrity_master_shell's SEO lane was a
    hardcoded UNMEASURED/FAIL string ("rank/impression truth lives in Google
    Search Console ... behind interactive auth unavailable to this process")
    while this module had been ingesting the service-account series daily:
    247 site-day rows, newest 2026-08-29, at the read route on 2026-09-02
    00:24Z. A lane that reads its own service over HTTP through the edge
    grades a cache and a timeout budget; reading the table grades the series.

    Same two queries as the read route's site branch — _coverage_of, _site_rows
    and _resolve_windows are shared, so the board and the API cannot drift.
    Raises on a DB failure: the caller decides what an unreadable series means
    (the shell renders it '?', never PASS).
    """
    days = max(1, min(int(days), 480))
    start, end, _prior_start, _prior_end = _resolve_windows(days, None, None)
    conn = get_db()
    try:
        c = conn.cursor()
        raw = getattr(c, "_cur", c)
        oldest, newest, total = _coverage_of(raw, "site")
        return {
            "rows": _site_rows(raw, start, end),
            "window_days": days,
            "coverage": {"oldest": str(oldest) if oldest else None,
                         "newest": str(newest) if newest else None,
                         "rows_stored": int(total or 0)},
        }
    finally:
        try:
            conn.close()
        except Exception:
            pass


@gsc_perf_bp.route("/api/v1/seo/performance", methods=["GET"])
def read_performance():
    """The series, for dashboards, the brain, and anyone asking whether SEO is
    working — and, since 2026-09-02, WHAT IS LOSING.

    `?dimension=site|query|page` (default site), `?days=28`, `?limit=50`
    (cap 5000), `?start=YYYY-MM-DD`, `?end=YYYY-MM-DD`,
    `?order=impressions|clicks|position` (default impressions),
    `?compare=1` → every row also carries `prior_clicks`, `prior_impressions`,
    `prior_position` for the equal-length window that precedes the current
    one; the site grain gets a `compare` block of totals instead. With
    compare, `order=lost_clicks|lost_impressions` sorts by the drop.

    Backward compatible: a call with none of the new args returns the same
    row shapes and the same window as before (QA sweep F8: the old route
    could only say "top 1000 by impressions since CURRENT_DATE - days",
    which covered 38% of clicks and could not name a losing page).

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
        limit = max(1, min(int(request.args.get("limit") or 50), MAX_READ_LIMIT))
    except (TypeError, ValueError):
        limit = 50
    compare = (request.args.get("compare") or "").strip().lower() in ("1", "true", "yes")
    order = (request.args.get("order") or "impressions").strip().lower()
    if order not in _ORDERS or (order.startswith("lost_") and not compare):
        return jsonify({"success": False,
                        "error": "order must be impressions, clicks or position"
                                 " (lost_clicks / lost_impressions with compare=1)"}), 400
    try:
        start, end, prior_start, prior_end = _resolve_windows(
            days, _parse_iso_date(request.args.get("start")),
            _parse_iso_date(request.args.get("end")))
    except ValueError as e:
        return jsonify({"success": False,
                        "error": f"start/end must be YYYY-MM-DD ({e})"}), 400
    days = (end - start).days   # echoes the effective window, as before

    conn = None
    try:
        conn = get_db()
        c = conn.cursor()
        raw = getattr(c, "_cur", c)

        oldest, newest, total = _coverage_of(raw, dim)

        compare_block = None
        if dim == "site":
            rows = _site_rows(raw, start, end)
            if compare:
                # Totals for both windows in one pass; position is
                # impression-weighted for the same reason as the grain query.
                raw.execute(
                    "SELECT "
                    "  SUM(CASE WHEN date >= %s THEN clicks ELSE 0 END), "
                    "  SUM(CASE WHEN date >= %s THEN impressions ELSE 0 END), "
                    "  CASE WHEN SUM(CASE WHEN date >= %s THEN impressions ELSE 0 END) > 0 "
                    "       THEN SUM(CASE WHEN date >= %s THEN position * impressions ELSE 0 END) "
                    "          / SUM(CASE WHEN date >= %s THEN impressions ELSE 0 END) END, "
                    "  SUM(CASE WHEN date < %s THEN clicks ELSE 0 END), "
                    "  SUM(CASE WHEN date < %s THEN impressions ELSE 0 END), "
                    "  CASE WHEN SUM(CASE WHEN date < %s THEN impressions ELSE 0 END) > 0 "
                    "       THEN SUM(CASE WHEN date < %s THEN position * impressions ELSE 0 END) "
                    "          / SUM(CASE WHEN date < %s THEN impressions ELSE 0 END) END "
                    "  FROM gsc_daily_performance "
                    " WHERE dimension = 'site' AND date >= %s AND date <= %s",
                    (start,) * 5 + (start,) * 5 + (prior_start, end))
                t = raw.fetchone() or (0, 0, None, 0, 0, None)
                compare_block = {
                    "clicks": int(t[0] or 0), "impressions": int(t[1] or 0),
                    "position": (round(float(t[2]), 2) if t[2] is not None else None),
                    "prior_clicks": int(t[3] or 0), "prior_impressions": int(t[4] or 0),
                    "prior_position": (round(float(t[5]), 2) if t[5] is not None else None),
                }
        else:
            # Aggregate the window so a caller gets "top queries this month",
            # not one row per query per day they then have to sum themselves.
            # Position is impression-weighted — a flat AVG over days would let a
            # single 1-impression day at rank 3 outvote 10,000 impressions at 40.
            col, direction = _ORDERS[order]
            if compare:
                # ONE grouped pass over both windows (a row absent from the
                # current window but present in the prior one is exactly the
                # "lost" row this exists to surface, so the join is on the
                # UNION of both windows, not the current one).
                raw.execute(
                    "SELECT dim_value, "
                    "  SUM(CASE WHEN date >= %s THEN clicks ELSE 0 END) AS cur_clicks, "
                    "  SUM(CASE WHEN date >= %s THEN impressions ELSE 0 END) AS cur_impr, "
                    "  CASE WHEN SUM(CASE WHEN date >= %s THEN impressions ELSE 0 END) > 0 "
                    "       THEN SUM(CASE WHEN date >= %s THEN position * impressions ELSE 0 END) "
                    "          / SUM(CASE WHEN date >= %s THEN impressions ELSE 0 END) END AS cur_pos, "
                    "  SUM(CASE WHEN date < %s THEN clicks ELSE 0 END) AS prior_clicks, "
                    "  SUM(CASE WHEN date < %s THEN impressions ELSE 0 END) AS prior_impr, "
                    "  CASE WHEN SUM(CASE WHEN date < %s THEN impressions ELSE 0 END) > 0 "
                    "       THEN SUM(CASE WHEN date < %s THEN position * impressions ELSE 0 END) "
                    "          / SUM(CASE WHEN date < %s THEN impressions ELSE 0 END) END AS prior_pos "
                    "  FROM gsc_daily_performance "
                    " WHERE dimension = %s AND date >= %s AND date <= %s "
                    " GROUP BY dim_value "
                    f" ORDER BY {col} {direction} NULLS LAST "
                    " LIMIT %s",
                    (start,) * 5 + (start,) * 5 + (dim, prior_start, end, limit))
                rows = [{"value": r[0], "clicks": int(r[1] or 0),
                         "impressions": int(r[2] or 0),
                         "position": (round(float(r[3]), 2) if r[3] is not None else None),
                         "prior_clicks": int(r[4] or 0),
                         "prior_impressions": int(r[5] or 0),
                         "prior_position": (round(float(r[6]), 2) if r[6] is not None else None)}
                        for r in raw.fetchall()]
            else:
                raw.execute(
                    "SELECT dim_value, SUM(clicks) AS cur_clicks, "
                    "       SUM(impressions) AS cur_impr, "
                    "       CASE WHEN SUM(impressions) > 0 "
                    "            THEN SUM(position * impressions) / SUM(impressions) "
                    "            ELSE NULL END AS cur_pos "
                    "  FROM gsc_daily_performance "
                    " WHERE dimension = %s AND date >= %s AND date <= %s "
                    " GROUP BY dim_value "
                    f" ORDER BY {col} {direction} NULLS LAST "
                    " LIMIT %s", (dim, start, end, limit))
                rows = [{"value": r[0], "clicks": int(r[1] or 0),
                         "impressions": int(r[2] or 0),
                         "position": (round(float(r[3]), 2) if r[3] is not None else None)}
                        for r in raw.fetchall()]

        out = {
            "success": True,
            "dimension": dim,
            "window_days": days,
            "window": {"start": str(start), "end": str(end)},
            "order": order,
            "rows": rows,
            "coverage": {
                "oldest": str(oldest) if oldest else None,
                "newest": str(newest) if newest else None,
                "rows_stored": int(total or 0),
                "note": ("empty means NOT INGESTED, not zero traffic — "
                         "POST /api/v1/admin/gsc/performance/ingest?days=480 "
                         "seeds history"),
            },
        }
        if compare:
            out["prior_window"] = {"start": str(prior_start), "end": str(prior_end)}
            if compare_block is not None:
                out["compare"] = compare_block
        return jsonify(out)
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
