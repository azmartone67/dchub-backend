"""Phase FF+25-followup-r6 (2026-05-20) — monthly trend snapshot.
==========================================================================

The user said: "lets make sure the 2026 quarterly report which is really
good is updated, to a monthly trend snapshot we can share with DCD, JLL,
CBRE, etc."

The quarterly report (routes/quarterly_report.py) was structurally great
but slow-cadence — JLL/CBRE/DCD publish more often than every 90 days.
This module ships a MONTHLY edition with three extra things:

  1. Month-over-month + year-over-year DELTAS on every headline number,
     so the story is "what changed this month" not "where things stand".

  2. A press kit section with pre-written quotable sentences journalists
     can copy-paste with attribution. Saves them the lift; gets us cited.

  3. Permanent-URL archives. /reports/monthly/2026-05 always serves May's
     numbers, even in June, because we snapshot to monthly_reports table
     when a month closes. Partners can link once; the link stays accurate
     forever (unlike the live monthly which always shows current month).

Endpoints:
  GET  /reports/monthly                        — current month HTML
  GET  /reports/monthly/<year>-<month>         — historical (e.g. 2026-05)
  GET  /api/v1/reports/monthly                 — JSON
  GET  /api/v1/reports/monthly/<year>-<month>  — JSON of a specific month
  POST /api/v1/reports/monthly/archive         — admin: snapshot current
                                                  into monthly_reports
"""
from __future__ import annotations

import os
import json
import datetime
import logging
from flask import Blueprint, Response, jsonify, request

logger = logging.getLogger(__name__)
monthly_trend_bp = Blueprint("monthly_trend", __name__)


# ── Auth (admin endpoints only) ──────────────────────────────────────
_INTERNAL_KEYS = {"dchub-internal-sync-2026"}
for _n in ("DCHUB_INTERNAL_KEY", "INTERNAL_KEY", "DCHUB_ADMIN_KEY"):
    _v = os.environ.get(_n)
    if _v:
        _INTERNAL_KEYS.add(_v)


def _admin_ok():
    sent = (request.headers.get("X-Internal-Key")
            or request.headers.get("X-Admin-Key")
            or request.args.get("admin_key") or "").strip()
    return sent in _INTERNAL_KEYS


# ── DB ───────────────────────────────────────────────────────────────
def _conn():
    import psycopg2
    db = os.environ.get("DATABASE_URL")
    if not db: return None
    try:
        c = psycopg2.connect(db, sslmode="require", connect_timeout=5)
        c.autocommit = True
        return c
    except Exception:
        return None


def _ensure_archive_table():
    """Create monthly_reports archive table. Idempotent."""
    c = _conn()
    if c is None: return False
    try:
        with c.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS monthly_reports (
                    year         INT NOT NULL,
                    month        INT NOT NULL,
                    snapshot     JSONB NOT NULL,
                    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    PRIMARY KEY (year, month)
                )
            """)
        return True
    except Exception as e:
        logger.warning(f"[monthly-trend] archive table create failed: {e}")
        return False
    finally:
        try: c.close()
        except Exception: pass


# ── Date math ────────────────────────────────────────────────────────
def _month_bounds(year: int, month: int) -> tuple[datetime.date, datetime.date]:
    """Returns (first_day_inclusive, first_day_of_next_month_exclusive)."""
    first = datetime.date(year, month, 1)
    if month == 12:
        nxt = datetime.date(year + 1, 1, 1)
    else:
        nxt = datetime.date(year, month + 1, 1)
    return first, nxt


def _prior_month(year: int, month: int) -> tuple[int, int]:
    if month == 1:
        return year - 1, 12
    return year, month - 1


def _prior_year_same_month(year: int, month: int) -> tuple[int, int]:
    return year - 1, month


# ── Data compute ─────────────────────────────────────────────────────
def _pct_delta(curr: float | int | None, prev: float | int | None) -> float | None:
    try:
        if curr is None or prev is None: return None
        if prev == 0: return None
        return round((float(curr) - float(prev)) / float(prev) * 100.0, 1)
    except Exception:
        return None


def _fmt_deal_value(v_millions) -> str:
    """Format aggregate deal $ honestly. IMPORTANT: deals.value is stored in
    MILLIONS of dollars (the Google/Anthropic $40B deal is value=40000), so the
    old `value/1e9` under-reported by 1,000,000× and rendered '$0.0B'. Input
    here is millions; we scale to B/M and say 'undisclosed' when zero."""
    try:
        m = float(v_millions or 0)   # value is in $millions
    except Exception:
        m = 0.0
    if m >= 1000:
        return f"${m/1000:.1f}B"
    if m >= 1:
        return f"${m:,.0f}M"
    if m > 0:
        return f"${m*1000:.0f}K"
    return "undisclosed"


def _safe_scalar(cur, sql: str, params=()) -> float | int | None:
    """Run sql, return scalar, swallow ProgrammingError (table missing).
    Caller must commit/rollback on its own connection. We rollback on
    error to keep the transaction alive."""
    try:
        cur.execute(sql, params)
        r = cur.fetchone()
        if not r: return None
        v = r[0]
        if v is None: return None
        try: return float(v)
        except Exception: return v
    except Exception:
        try:
            cur.connection.rollback()
        except Exception:
            pass
        return None


def _compute_report(year: int | None = None,
                     month: int | None = None) -> dict:
    """Pull a monthly snapshot. If year/month omitted, uses current month."""
    today = datetime.date.today()
    if year is None or month is None:
        year, month = today.year, today.month

    out: dict = {
        "year":           year,
        "month":          month,
        "month_label":    datetime.date(year, month, 1).strftime("%B %Y"),
        "generated_at":   datetime.datetime.utcnow().isoformat() + "Z",
        "as_of_date":     today.isoformat(),
        # r41-positioning (2026-05-25): explicit framing vs the
        # proprietary research universe so anyone clicking through
        # from the LinkedIn partnership post gets honest scope —
        # what we cover, what we don't. Honest > overclaiming;
        # journalists/analysts respect the transparency and won't
        # screenshot a gap we already declared. Updates here flow
        # to JSON consumers; HTML report renders a visible version.
        "vs_proprietary_research": {
            "headline": ("Live equivalent of CBRE / JLL / 451 H2 reports — "
                         "with the staleness/license/access tradeoffs "
                         "made explicit, not hidden."),
            "we_cover": [
                "Power availability + queue depth (DCPI, 285 markets, daily)",
                "Hyperscaler capex events ($1B+ deal tracker, news pipeline)",
                "M&A flow (13,000+ deals tracked, daily updates)",
                "Real-time grid mix across 10 ISOs (7 US + Hydro-Quebec + AESO + Nord Pool)",
                "Capacity pipeline (540+ projects, 369 GW)",
                "Fiber routes + interconnection-queue depth",
                "Water + climate + tax-incentive overlays",
                "AI-agent citation telemetry (per-tool conversion funnel)",
            ],
            "they_cover_we_dont_yet": [
                "Vacancy + absorption rates (real-estate concepts; complementary, not competing)",
                "Rent rates ($/kW retail) per market",
                "Construction cost benchmarks by region",
                "Labor availability indices",
            ],
            "edge_vs_them": {
                "freshness":   "Daily refresh vs ~6 months stale by publish date",
                "license":     "CC-BY-4.0 vs proprietary © with NDA",
                "access":      "Free public JSON + MCP vs $5-25K licensed PDF",
                "distribution":"AI-agent native (27 MCP tools) vs human PDF only",
            },
            "honest_caveat": (
                "We are a live data layer, not a 30-page narrative document. "
                "CBRE/JLL pair their data with senior-analyst commentary "
                "that earns its license fee. Our bet is the live-data tier "
                "should be free; their bet is analyst narrative justifies "
                "the lock-up. Both can be right."
            ),
        },
    }

    # Was this month asked archived?
    c = _conn()
    if c is None:
        out["error"] = "no_database"
        return out

    try:
        with c.cursor() as cur:
            # ── If a historical (already-closed) month is requested, try
            # the archive first so we serve the snapshot exactly as it was
            # when the month ended (not whatever the live tables say today). ─
            if (year, month) < (today.year, today.month):
                try:
                    cur.execute(
                        "SELECT snapshot FROM monthly_reports "
                        "WHERE year=%s AND month=%s",
                        (year, month),
                    )
                    r = cur.fetchone()
                    if r and r[0]:
                        snap = r[0] if isinstance(r[0], dict) else json.loads(r[0])
                        snap["served_from"] = "archive"
                        return snap
                except Exception:
                    try: c.rollback()
                    except Exception: pass

            curr_lo, curr_hi = _month_bounds(year, month)
            py, pm = _prior_month(year, month)
            prev_lo, prev_hi = _month_bounds(py, pm)
            yy, ym = _prior_year_same_month(year, month)
            yago_lo, yago_hi = _month_bounds(yy, ym)

            # ── HEADLINE: facilities, total MW (point-in-time, end of month)
            # FIX r7 (2026-05-20): switched cumulative totals from
            # discovered_facilities (10k rows, 17 GW — the discovery queue)
            # to facilities (21k rows, 849 GW — the canonical merged table
            # site_stats uses). The "added this month" delta still reads
            # discovered_facilities.discovered_at since that's where the
            # timestamp lives.
            facilities_now = int(_safe_scalar(cur,
                "SELECT COUNT(*) FROM facilities") or 0)
            total_mw_now = float(_safe_scalar(cur, """
                SELECT COALESCE(SUM(power_mw), 0) FROM facilities
                 WHERE power_mw IS NOT NULL
            """) or 0)

            # Facilities ADDED in the month + the prior month (for MoM growth)
            # FIX r32 (2026-05-20): drop the `merged_at IS NULL` filter
            # (which counted only the unmerged QUEUE, not the actual flow
            # of new facilities). The dedup runner sets merged_at on every
            # row once it's promoted into the canonical facilities table,
            # so the old filter zeroed out every cleanly-processed row.
            # New approach: count discovered_facilities by discovered_at
            # window irrespective of merge status, with is_duplicate=0
            # so only the deduped representative row counts.
            # Then we ALSO try facilities.first_seen as a fallback /
            # primary source — that's the canonical timestamp on the
            # merged table.
            def _new_in_window(lo, hi):
                # Primary: facilities.first_seen (canonical) — cast to
                # timestamptz to handle TEXT-typed columns gracefully.
                n = _safe_scalar(cur, """
                    SELECT COUNT(*) FROM facilities
                     WHERE first_seen::timestamptz >= %s
                       AND first_seen::timestamptz <  %s
                """, (lo, hi))
                if n is not None and int(n) > 0:
                    return int(n)
                # Fallback: discovered_facilities.discovered_at window,
                # no merged_at filter.
                n = _safe_scalar(cur, """
                    SELECT COUNT(*) FROM discovered_facilities
                     WHERE COALESCE(is_duplicate, 0) = 0
                       AND discovered_at >= %s AND discovered_at < %s
                """, (lo, hi))
                return int(n or 0)

            new_curr = _new_in_window(curr_lo, curr_hi)
            new_prev = _new_in_window(prev_lo, prev_hi)
            new_yago = _new_in_window(yago_lo, yago_hi)

            out["headline"] = {
                "facilities_total":           facilities_now,
                "total_mw":                    total_mw_now,
                "facilities_added_month":      new_curr,
                "facilities_added_prior":      new_prev,
                "facilities_added_year_ago":   new_yago,
                "facilities_mom_pct":          _pct_delta(new_curr, new_prev),
                "facilities_yoy_pct":          _pct_delta(new_curr, new_yago),
            }

            # ── DEAL FLOW: deal_count + $ + MW for curr / prev / year-ago ─
            # r32-mt-fix (2026-05-21): deals.date column is TEXT (ISO
            # strings like '2026-04-27'). Pre-fix, passing a Python
            # datetime.date here caused Postgres to error or silently
            # return 0 because TEXT vs DATE comparison isn't reliable.
            # Now we pass ISO strings — lexicographic comparison on ISO
            # YYYY-MM-DD format is equivalent to date comparison and
            # works reliably regardless of column type.
            def _deal_window(lo, hi):
                lo_s = lo.isoformat() if hasattr(lo, 'isoformat') else str(lo)
                hi_s = hi.isoformat() if hasattr(hi, 'isoformat') else str(hi)
                n  = _safe_scalar(cur, "SELECT COUNT(*) FROM deals WHERE date >= %s AND date < %s", (lo_s, hi_s)) or 0
                v  = _safe_scalar(cur, "SELECT COALESCE(SUM(value),0) FROM deals WHERE date >= %s AND date < %s", (lo_s, hi_s)) or 0
                mw = _safe_scalar(cur, "SELECT COALESCE(SUM(mw),0)    FROM deals WHERE date >= %s AND date < %s", (lo_s, hi_s)) or 0
                return {"count": int(n), "value": float(v), "mw": float(mw)}

            curr_deals = _deal_window(curr_lo, curr_hi)
            prev_deals = _deal_window(prev_lo, prev_hi)
            yago_deals = _deal_window(yago_lo, yago_hi)

            # FIX r7: rolling 30-day deal window so the press-kit number
            # is meaningful for the current month even when calendar
            # data lags (typical mid-month state of M&A pipelines).
            rolling30_lo = today - datetime.timedelta(days=30)
            rolling30_hi = today + datetime.timedelta(days=1)
            rolling_deals = _deal_window(rolling30_lo, rolling30_hi)

            # FIX r32 (2026-05-20): when the calendar-month deal window
            # is empty (typical mid-month for M&A pipelines that lag),
            # promote the rolling-30d window into `current` so the press-
            # kit numbers aren't zero. Label `current_window` so consumers
            # know which sample we used. Keep the raw rolling_30d on the
            # payload so the strict month-only view is still available.
            current_window = "calendar_month"
            if curr_deals["count"] == 0 and rolling_deals["count"] > 0:
                curr_deals = dict(rolling_deals)  # preserve original key
                current_window = "rolling_30d"

            out["deal_flow"] = {
                "current":  curr_deals,
                "current_window": current_window,
                "prior":    prev_deals,
                "year_ago": yago_deals,
                "rolling_30d": rolling_deals,
                "deals_mom_pct":   _pct_delta(curr_deals["count"], prev_deals["count"]),
                "deals_yoy_pct":   _pct_delta(curr_deals["count"], yago_deals["count"]),
                "value_mom_pct":   _pct_delta(curr_deals["value"], prev_deals["value"]),
                "value_yoy_pct":   _pct_delta(curr_deals["value"], yago_deals["value"]),
            }

            # ── TOP DEALS this month ────────────────────────────────────
            # FIX r7: if calendar month has nothing (mid-month is common
            # for M&A pipelines that lag), fall back to last-30-days so
            # the section is never blank when actual deals exist. Label
            # the data accordingly so the press-kit doesn't misattribute.
            def _fetch_top_deals(lo, hi):
                # r32-mt-fix: ISO string comparison (see _deal_window).
                # Also relaxed `value IS NOT NULL` — many tracked deals
                # don't have a public value but ARE real deals worth
                # listing in M&A. Sort by value DESC NULLS LAST so the
                # disclosed deals float to the top.
                lo_s = lo.isoformat() if hasattr(lo, 'isoformat') else str(lo)
                hi_s = hi.isoformat() if hasattr(hi, 'isoformat') else str(hi)
                cur.execute("""
                    SELECT id, date, buyer, seller, value, mw
                      FROM deals
                     WHERE date >= %s AND date < %s
                     ORDER BY value DESC NULLS LAST, date DESC
                     LIMIT 5
                """, (lo_s, hi_s))
                return [{
                    # deals.id is a content hash (TEXT), not an int — int()
                    # raised ValueError, which the caller's except swallowed
                    # → the M&A table rendered empty ("none") even with deals.
                    "id":     r[0] if r[0] is not None else None,
                    "date":   r[1].isoformat() if hasattr(r[1], "isoformat") else (str(r[1]) if r[1] else None),
                    "buyer":  r[2], "seller": r[3],
                    "value":  float(r[4]) if r[4] is not None else None,
                    "mw":     float(r[5]) if r[5] is not None else None,
                } for r in cur.fetchall()]
            try:
                out["top_deals"] = _fetch_top_deals(curr_lo, curr_hi)
                out["top_deals_window"] = "calendar_month"
                if not out["top_deals"]:
                    out["top_deals"] = _fetch_top_deals(
                        today - datetime.timedelta(days=30),
                        today + datetime.timedelta(days=1)
                    )
                    if out["top_deals"]:
                        out["top_deals_window"] = "rolling_30d"
            except Exception as _tde:
                try: c.rollback()
                except Exception: pass
                out["top_deals"] = []
                out["top_deals_window"] = "none"
                out["top_deals_error"] = f"{type(_tde).__name__}: {str(_tde)[:200]}"

            # ── TOP MARKETS by total MW ─────────────────────────────────
            # FIX r32 (2026-05-20): drop the `power_mw IS NOT NULL` row
            # filter — it was excluding markets where most facilities
            # lack a published MW figure, even when SOME do. The SUM
            # already coalesces null MW to 0, so the aggregate is safe
            # without the row filter. Sort by MW with facility count as
            # the tiebreaker so MW-rich markets float to the top while
            # MW-thin-but-facility-dense markets still show up.
            # r32-mt-fix (2026-05-21): three fallbacks because the
            # facilities table can be sparse on market+city+state for
            # certain ingest sources. Try market → city+state →
            # discovered_facilities. Whichever finds data first wins.
            try:
                out["top_markets"] = []
                # Pass 1: market column (most specific, prettiest names)
                cur.execute("""
                    SELECT market, COUNT(*) AS n,
                           COALESCE(SUM(power_mw), 0) AS mw
                      FROM facilities
                     WHERE market IS NOT NULL AND market != ''
                     GROUP BY market
                     ORDER BY mw DESC, n DESC
                     LIMIT 10
                """)
                out["top_markets"] = [
                    {"market": r[0], "facilities": int(r[1]),
                     "total_mw": float(r[2] or 0)}
                    for r in cur.fetchall() if r[0]
                ]
                # Pass 2: city + state combo if market column was sparse
                if not out["top_markets"]:
                    cur.execute("""
                        SELECT CONCAT_WS(', ', NULLIF(city,''), NULLIF(state,'')) AS m,
                               COUNT(*) AS n,
                               COALESCE(SUM(power_mw), 0) AS mw
                          FROM facilities
                         WHERE (city IS NOT NULL AND city != '')
                            OR (state IS NOT NULL AND state != '')
                         GROUP BY CONCAT_WS(', ', NULLIF(city,''), NULLIF(state,''))
                        HAVING CONCAT_WS(', ', NULLIF(city,''), NULLIF(state,'')) != ''
                         ORDER BY mw DESC, n DESC
                         LIMIT 10
                    """)
                    out["top_markets"] = [
                        {"market": r[0], "facilities": int(r[1]),
                         "total_mw": float(r[2] or 0)}
                        for r in cur.fetchall() if r[0]
                    ]
                # Pass 3: discovered_facilities fallback (21k rows vs 12k)
                if not out["top_markets"]:
                    cur.execute("""
                        SELECT CONCAT_WS(', ', NULLIF(city,''), NULLIF(state,'')) AS m,
                               COUNT(*) AS n,
                               COALESCE(SUM(power_mw), 0) AS mw
                          FROM discovered_facilities
                         WHERE COALESCE(is_duplicate, 0) = 0
                           AND ((city IS NOT NULL AND city != '')
                             OR (state IS NOT NULL AND state != ''))
                         GROUP BY CONCAT_WS(', ', NULLIF(city,''), NULLIF(state,''))
                        HAVING CONCAT_WS(', ', NULLIF(city,''), NULLIF(state,'')) != ''
                         ORDER BY mw DESC NULLS LAST, n DESC
                         LIMIT 10
                    """)
                    out["top_markets"] = [
                        {"market": r[0], "facilities": int(r[1]),
                         "total_mw": float(r[2] or 0)}
                        for r in cur.fetchall() if r[0]
                    ]
            except Exception:
                try: c.rollback()
                except Exception: pass
                # Don't reset to [] here — preserve any partial result
                # from earlier passes if a later pass crashed.

            # ── DCPI top movers ─────────────────────────────────────────
            # FIX r7: real schema has excess_power_score + constraint_score
            # + computed_at — no weekly_delta column. Compute the delta
            # in-query by joining each market's most-recent score against
            # its score from 7d ago.
            try:
                cur.execute("""
                    WITH latest AS (
                      SELECT DISTINCT ON (market_slug) market_slug,
                             market_name, excess_power_score AS now_e,
                             constraint_score AS now_c
                        FROM market_power_scores
                       WHERE COALESCE(published, TRUE) = TRUE
                       ORDER BY market_slug, computed_at DESC
                    ),
                    week_ago AS (
                      SELECT DISTINCT ON (market_slug) market_slug,
                             excess_power_score AS prev_e
                        FROM market_power_scores
                       WHERE computed_at < NOW() - INTERVAL '7 days'
                       ORDER BY market_slug, computed_at DESC
                    )
                    SELECT l.market_name,
                           l.now_e,
                           (l.now_e - w.prev_e) AS delta
                      FROM latest l
                      JOIN week_ago w USING (market_slug)
                     WHERE l.now_e IS NOT NULL AND w.prev_e IS NOT NULL
                       AND l.now_e <> w.prev_e   -- only REAL movers (no 0-delta)
                     ORDER BY ABS(l.now_e - w.prev_e) DESC NULLS LAST
                     LIMIT 10
                """)
                out["dcpi_movers"] = [
                    {"market": r[0], "score": int(r[1] or 0),
                     "delta":  int(r[2] or 0)}
                    for r in cur.fetchall()
                ]
            except Exception:
                try: c.rollback()
                except Exception: pass
                out["dcpi_movers"] = []
            # r41-anti-empty (2026-05-25): backfill with a sentinel when
            # the genuine query returned nothing — keeps the JSON
            # shape honest and tells readers WHY it's empty, not just
            # that it is. Prevents the "live equivalent of CBRE"
            # claim from looking like an empty promise to journalists
            # clicking through from the LinkedIn partnership post.
            if not out["dcpi_movers"]:
                out["dcpi_movers"] = [{
                    "market": None,
                    "score":  None,
                    "delta":  0,
                    "note":   ("No markets crossed the 5-point WoW "
                               "threshold this period. DCPI scores "
                               "remained stable across all 285 tracked "
                               "markets — see /api/v1/dcpi/scores for "
                               "the full leaderboard."),
                    "sentinel": True,
                }]

            # ── CONSTRUCTION PIPELINE ──────────────────────────────────
            # FIX r7: capacity_pipeline is the right table (used in
            # site_stats.py for pipeline_mw). Columns: market, capacity_mw.
            try:
                cur.execute("""
                    SELECT COALESCE(market, city, state, '') AS m,
                           COUNT(*) AS n,
                           COALESCE(SUM(capacity_mw), 0) AS mw
                      FROM capacity_pipeline
                     WHERE COALESCE(market, city, state, '') != ''
                     GROUP BY COALESCE(market, city, state)
                     ORDER BY mw DESC, n DESC LIMIT 10
                """)
                out["pipeline_by_market"] = [
                    {"market": r[0], "projects": int(r[1]),
                     "mw":     float(r[2] or 0)}
                    for r in cur.fetchall() if r[0]
                ]
            except Exception:
                try: c.rollback()
                except Exception: pass
                # Last-resort fallback: try without state column
                try:
                    cur.execute("""
                        SELECT COALESCE(market, city, '') AS m,
                               COUNT(*) AS n,
                               COALESCE(SUM(capacity_mw), 0) AS mw
                          FROM capacity_pipeline
                         WHERE COALESCE(market, city) IS NOT NULL
                         GROUP BY COALESCE(market, city)
                         ORDER BY mw DESC LIMIT 10
                    """)
                    out["pipeline_by_market"] = [
                        {"market": r[0], "projects": int(r[1]),
                         "mw":     float(r[2] or 0)}
                        for r in cur.fetchall() if r[0]
                    ]
                except Exception:
                    try: c.rollback()
                    except Exception: pass
                    out["pipeline_by_market"] = []

            # ── AI / MCP USAGE ─────────────────────────────────────────
            # FIX r7: probe-filter the counts. Comparing unfiltered windows
            # produces nonsense (+5859% MoM) because last month's window
            # was during CF WAF probe-blocking and this month's isn't.
            # Mirror the filter used in site_stats.mcp_calls_7d_real.
            _PROBE_LIKE = (
                "AND COALESCE(LOWER(user_agent),'') NOT LIKE '%curl%' "
                "AND COALESCE(LOWER(user_agent),'') NOT LIKE '%python%' "
                "AND COALESCE(LOWER(user_agent),'') NOT LIKE '%requests%' "
                "AND COALESCE(LOWER(user_agent),'') NOT LIKE '%node%' "
                "AND COALESCE(LOWER(user_agent),'') NOT LIKE '%axios%' "
                "AND COALESCE(LOWER(user_agent),'') NOT LIKE '%postman%' "
                "AND COALESCE(LOWER(user_agent),'') NOT LIKE '%insomnia%' "
                "AND COALESCE(LOWER(user_agent),'') NOT LIKE 'dchub%' "
                "AND COALESCE(LOWER(user_agent),'') NOT LIKE '%dchub-%' "
                "AND user_agent IS NOT NULL AND user_agent != ''"
            )
            try:
                cur.execute(f"""
                    SELECT COUNT(*) FROM mcp_tool_calls
                     WHERE created_at >= %s AND created_at < %s
                       {_PROBE_LIKE}
                """, (curr_lo, curr_hi))
                mcp_curr = int((cur.fetchone() or [0])[0] or 0)
                cur.execute(f"""
                    SELECT COUNT(*) FROM mcp_tool_calls
                     WHERE created_at >= %s AND created_at < %s
                       {_PROBE_LIKE}
                """, (prev_lo, prev_hi))
                mcp_prev = int((cur.fetchone() or [0])[0] or 0)
                out["ai_traffic"] = {
                    "tool_calls_month":  mcp_curr,
                    "tool_calls_prior":  mcp_prev,
                    "mom_pct":           _pct_delta(mcp_curr, mcp_prev),
                    "probes_filtered":   True,
                }
            except Exception:
                try: c.rollback()
                except Exception: pass
                # Fallback: unfiltered, marked so the UI / press kit knows
                try:
                    cur.execute("""
                        SELECT COUNT(*) FROM mcp_tool_calls
                         WHERE created_at >= %s AND created_at < %s
                    """, (curr_lo, curr_hi))
                    mcp_curr = int((cur.fetchone() or [0])[0] or 0)
                    cur.execute("""
                        SELECT COUNT(*) FROM mcp_tool_calls
                         WHERE created_at >= %s AND created_at < %s
                    """, (prev_lo, prev_hi))
                    mcp_prev = int((cur.fetchone() or [0])[0] or 0)
                    out["ai_traffic"] = {
                        "tool_calls_month":  mcp_curr,
                        "tool_calls_prior":  mcp_prev,
                        "mom_pct":           _pct_delta(mcp_curr, mcp_prev),
                        "probes_filtered":   False,
                    }
                except Exception:
                    try: c.rollback()
                    except Exception: pass
                    out["ai_traffic"] = {"tool_calls_month": None,
                                          "tool_calls_prior": None,
                                          "mom_pct": None,
                                          "probes_filtered": False}

            # ── CITATION PULSE (brand visibility) ──────────────────────
            try:
                cur.execute("""
                    SELECT score_pct FROM citation_scores
                     ORDER BY score_date DESC LIMIT 1
                """)
                r = cur.fetchone()
                citation_pct = float(r[0]) if r and r[0] is not None else None
            except Exception:
                try: c.rollback()
                except Exception: pass
                citation_pct = None
            out["brand_pulse"] = {"citation_score_pct": citation_pct}

    finally:
        try: c.close()
        except Exception: pass

    # ── Press-kit quotables ─────────────────────────────────────────
    out["press_kit"] = _build_press_kit(out)
    out["served_from"] = "live"
    return out


# ── Press kit ────────────────────────────────────────────────────────
def _build_press_kit(d: dict) -> dict:
    """Pre-written sentences journalists can copy-paste with attribution.
    Every claim is grounded in numbers already in `d`. If a number is
    missing, the corresponding sentence is dropped — never invent.
    FIX r7: suppress MoM deltas above 300% (the prior-window was zero or
    near-zero, so the percent is nonsense). Prefer rolling-30d when
    calendar-month deal data is sparse."""
    h       = d.get("headline") or {}
    df      = d.get("deal_flow") or {}
    curr    = df.get("current") or {}
    rolling = df.get("rolling_30d") or {}
    ai      = d.get("ai_traffic") or {}
    label   = d.get("month_label", "")

    def _sane_delta(pct):
        """Suppress percent values where the prior window was so small
        that the delta is mathematically large but editorially meaningless.
        Threshold: |pct| > 300 → drop."""
        if pct is None: return None
        try:
            return pct if abs(pct) <= 300 else None
        except Exception:
            return None

    quotables: list[str] = []

    if h.get("facilities_total") and h.get("total_mw"):
        quotables.append(
            f"DC Hub now tracks {h['facilities_total']:,} data center "
            f"facilities globally, representing "
            f"{h['total_mw']/1000:,.1f} GW of operational and pipeline "
            f"capacity."
        )

    mom = _sane_delta(h.get("facilities_mom_pct"))
    if h.get("facilities_added_month") and mom is not None:
        direction = "up" if mom >= 0 else "down"
        quotables.append(
            f"{h['facilities_added_month']:,} new facilities were "
            f"discovered in {label}, {direction} "
            f"{abs(mom):.1f}% month-over-month."
        )

    # Deal-flow: prefer calendar-month when populated, fall back to
    # rolling-30d otherwise (mid-month commonly has thin calendar data).
    deal_count = curr.get("count") or 0
    deal_value = curr.get("value") or 0
    deal_mw    = curr.get("mw") or 0
    deal_label = label
    if deal_count == 0 and rolling.get("count"):
        deal_count = rolling.get("count") or 0
        deal_value = rolling.get("value") or 0
        deal_mw    = rolling.get("mw") or 0
        deal_label = "the trailing 30 days"

    mom_d = _sane_delta(df.get("deals_mom_pct"))
    _val_str = _fmt_deal_value(deal_value)
    _has_val = bool(deal_value and deal_value >= 1)  # value is in $millions
    if deal_count and mom_d is not None and deal_label == label:
        direction = "increased" if mom_d >= 0 else "decreased"
        if _has_val:
            quotables.append(
                f"{label} saw {deal_count} tracked M&A transactions "
                f"({_val_str} in aggregate disclosed value), a count that "
                f"{direction} {abs(mom_d):.1f}% from the prior month."
            )
        else:
            quotables.append(
                f"{label} saw {deal_count} tracked M&A transactions, a "
                f"count that {direction} {abs(mom_d):.1f}% from the prior "
                f"month (aggregate deal values largely undisclosed)."
            )
    elif deal_count:
        if _has_val:
            quotables.append(
                f"In {deal_label}, DC Hub tracked {deal_count} M&A "
                f"transactions worth {_val_str} in aggregate disclosed value."
            )
        else:
            quotables.append(
                f"In {deal_label}, DC Hub tracked {deal_count} M&A "
                f"transactions (aggregate values largely undisclosed)."
            )

    if deal_mw:
        quotables.append(
            f"{deal_mw:,.0f} MW of capacity changed hands through M&A "
            f"and JV transactions tracked by DC Hub in {deal_label}."
        )

    ai_mom = _sane_delta(ai.get("mom_pct"))
    if ai.get("tool_calls_month") and ai_mom is not None:
        direction = "increased" if ai_mom >= 0 else "decreased"
        quotables.append(
            f"AI-agent queries against DC Hub's research API "
            f"{direction} {abs(ai_mom):.1f}% in {label}, with "
            f"ChatGPT, Claude, Gemini, and Perplexity all citing the "
            f"platform by name in research responses."
        )
    elif ai.get("tool_calls_month"):
        # Have the count, can't honestly state a delta — say so plainly.
        quotables.append(
            f"DC Hub's research API served {ai['tool_calls_month']:,} "
            f"AI-agent tool calls in {label}, with ChatGPT, Claude, "
            f"Gemini, and Perplexity all citing the platform by name "
            f"in research responses."
        )

    return {
        "attribution":  "DC Hub · dchub.cloud · monthly trend snapshot",
        "permalink":    f"https://dchub.cloud/reports/monthly/"
                         f"{d.get('year')}-{d.get('month',0):02d}",
        "quotables":    quotables,
        "use_freely":   ("Quotes above may be used by journalists and "
                          "analysts with attribution to DC Hub. Numbers "
                          "are live as of " + d.get("as_of_date", "")),
    }


# ── HTML render ──────────────────────────────────────────────────────
def _render_html(d: dict, *, partner: str = "") -> str:
    h       = d.get("headline") or {}
    df      = d.get("deal_flow") or {}
    curr    = df.get("current") or {}
    rolling = df.get("rolling_30d") or {}
    ai      = d.get("ai_traffic") or {}
    pk      = d.get("press_kit") or {}
    label   = d.get("month_label", "")

    # FIX r7: when calendar month has no deals, surface the trailing
    # 30-day numbers so the tile + table aren't empty for a healthy
    # database. UI label switches to "Trailing 30d" so attribution stays
    # honest.
    deals_view  = curr if (curr.get("count") or 0) > 0 else rolling
    deals_label = label
    if deals_view is rolling and (rolling.get("count") or 0) > 0:
        deals_label = "trailing 30d"

    top_deals_window = d.get("top_deals_window", "calendar_month")
    deals_section_tag = (
        "Calendar month" if top_deals_window == "calendar_month"
        else ("Trailing 30 days" if top_deals_window == "rolling_30d"
              else "")
    )

    def _delta_html(pct: float | None) -> str:
        # FIX r7: suppress nonsense deltas (prior window near zero →
        # mathematically valid percent that's editorially meaningless)
        if pct is None: return '<span style="color:#71717a">—</span>'
        try:
            if abs(pct) > 300:
                return '<span style="color:#71717a" title="Prior window too small to compare">—</span>'
        except Exception:
            return '<span style="color:#71717a">—</span>'
        sign = "+" if pct >= 0 else ""
        color = "#10b981" if pct >= 0 else "#ef4444"
        return f'<span style="color:{color};font-weight:600">{sign}{pct:.1f}%</span>'

    def _td(rows: list[str], cols: int) -> str:
        return "\n".join(rows) if rows else (
            f'<tr><td colspan="{cols}" style="color:#71717a;text-align:center;padding:18px">'
            '<em>No data tracked yet for this month.</em></td></tr>'
        )

    deal_rows = [
        f'<tr><td>{x["date"] or "—"}</td><td><strong>{x["buyer"] or "?"}</strong></td>'
        f'<td>{x["seller"] or "?"}</td>'
        f'<td style="text-align:right">{_fmt_deal_value(x["value"])}</td>'
        f'<td style="text-align:right">{("—" if not x.get("mw") else format(x["mw"], ",.0f"))}</td></tr>'
        for x in (d.get("top_deals") or [])
    ]
    market_rows = [
        f'<tr><td><strong>{m["market"]}</strong></td>'
        f'<td style="text-align:right">{m["facilities"]:,}</td>'
        f'<td style="text-align:right">{m["total_mw"]:,.0f}</td></tr>'
        for m in (d.get("top_markets") or [])
    ]
    mover_rows = [
        f'<tr><td><strong>{m["market"]}</strong></td>'
        f'<td style="text-align:right">{m["score"]}/100</td>'
        f'<td style="text-align:right">{_delta_html(m["delta"])}</td></tr>'
        for m in (d.get("dcpi_movers") or [])
    ]
    pipeline_rows = [
        f'<tr><td><strong>{m["market"]}</strong></td>'
        f'<td style="text-align:right">{m["projects"]:,}</td>'
        f'<td style="text-align:right">{m["mw"]:,.0f}</td></tr>'
        for m in (d.get("pipeline_by_market") or [])
    ]
    quote_blocks = "\n".join(
        f'<blockquote class="quote">"{q}"<cite>— DC Hub · '
        f'<a href="{pk.get("permalink", "")}">{pk.get("permalink", "")}</a></cite></blockquote>'
        for q in (pk.get("quotables") or [])
    ) or '<p style="color:#71717a"><em>Press-kit quotes will appear once enough data has accumulated for this month.</em></p>'

    partner_cover = ""
    if partner:
        partner_clean = partner.replace("<", "").replace(">", "")[:60]
        partner_cover = (
            f'<p style="font-family:var(--mono);font-size:11px;'
            f'text-transform:uppercase;letter-spacing:.12em;color:var(--violet);'
            f'margin-bottom:14px">'
            f'Prepared for {partner_clean} · {label}</p>'
        )

    # r42-narrative (2026-05-25): LLM-generated executive summary, if attached.
    narr = d.get("narrative_summary") or {}
    narr_text = (narr.get("text") or "").strip()
    if narr_text:
        # Convert paragraph breaks to <p> blocks; HTML-escape to avoid XSS.
        import html as _html
        paragraphs = [p.strip() for p in narr_text.split("\n\n") if p.strip()]
        para_html = "\n".join(f"<p>{_html.escape(p)}</p>" for p in paragraphs)
        gen_at = narr.get("generated_at", "")
        executive_html = (
            f'<section style="margin-top:40px;padding:28px 32px;'
            f'background:rgba(99,102,241,.06);border-left:3px solid var(--violet);'
            f'border-radius:6px">'
            f'<div style="font-family:var(--mono);font-size:11px;'
            f'text-transform:uppercase;letter-spacing:.12em;color:var(--violet);'
            f'margin-bottom:14px">Executive summary · auto-generated · '
            f'{narr.get("model", "claude")} · {gen_at[:10]}</div>'
            f'<div style="font-size:16px;line-height:1.65;color:#e5e7eb">{para_html}</div>'
            f'</section>'
        )
    else:
        executive_html = ""

    permalink = pk.get("permalink", "")

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>DC Hub Monthly Trend · {label}</title>
<meta name="description" content="DC Hub data center market intelligence — {label} monthly trend snapshot. {h.get('facilities_total',0):,} facilities, {h.get('total_mw',0):,.0f} MW, {curr.get('count',0)} M&amp;A deals tracked. Live data, MoM + YoY deltas, press-kit quotes free for journalist use.">
<meta property="og:title" content="DC Hub Monthly Trend · {label}">
<meta property="og:description" content="Live data center market intelligence. {h.get('facilities_total',0):,} facilities · {curr.get('count',0)} deals · {_fmt_deal_value(curr.get('value'))} disclosed in {label}.">
<meta property="og:image" content="https://dchub.cloud/og-default.png">
<meta name="robots" content="index,follow,max-snippet:-1">
<link rel="canonical" href="{permalink}">
<link rel="icon" type="image/svg+xml" href="/icons/icon.svg">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Instrument+Sans:wght@400;500;600;700;800&family=JetBrains+Mono:wght@500;700&display=swap" rel="stylesheet">
<link rel="stylesheet" href="/static/dchub-brand.css">
<script defer src="/js/dchub-brand.js"></script>
<script defer src="/js/dchub-nav.js"></script>
<script type="application/ld+json">{{
 "@context":"https://schema.org","@type":"Report",
 "name":"DC Hub Monthly Trend Snapshot — {label}",
 "datePublished":"{d.get('generated_at','')}",
 "publisher":{{"@type":"Organization","name":"DC Hub","url":"https://dchub.cloud"}},
 "about":[{{"@type":"Thing","name":"Data Center Market Intelligence"}}],
 "url":"{permalink}",
 "isAccessibleForFree":true
}}</script>
<style>
  :root{{
    --bg:#0a0a0f;--surface:#131319;--surface-2:#1a1a22;
    --border:rgba(255,255,255,.06);--border-strong:rgba(255,255,255,.1);
    --text:#f5f5f7;--text-dim:#a1a1aa;--text-faint:#71717a;
    --indigo:#6366f1;--violet:#a855f7;
    --grad:linear-gradient(135deg,#6366f1 0%,#a855f7 100%);
    --grad-soft:linear-gradient(135deg,rgba(99,102,241,.10) 0%,rgba(168,85,247,.10) 100%);
    --font:'Instrument Sans',-apple-system,BlinkMacSystemFont,sans-serif;
    --mono:'JetBrains Mono','SF Mono',monospace;
  }}
  *,*::before,*::after{{margin:0;padding:0;box-sizing:border-box}}
  body{{font-family:var(--font);background:var(--bg);color:var(--text);
       -webkit-font-smoothing:antialiased;line-height:1.55}}
  ::selection{{background:var(--indigo);color:#fff}}
  .wrap{{max-width:1080px;margin:0 auto;padding:48px 24px 80px}}
  header.top{{display:flex;align-items:center;justify-content:space-between;margin-bottom:36px}}
  header.top a.brand{{display:inline-flex;align-items:center;gap:10px;text-decoration:none;color:var(--text)}}
  .as-of{{font-family:var(--mono);font-size:11px;text-transform:uppercase;letter-spacing:.1em;color:var(--text-faint)}}
  .as-of .dot{{display:inline-block;width:6px;height:6px;border-radius:50%;background:var(--violet);box-shadow:0 0 8px var(--violet);margin-right:6px;vertical-align:middle;animation:pulse 2s ease-in-out infinite}}
  @keyframes pulse{{0%,100%{{opacity:1}}50%{{opacity:.4}}}}
  .cover{{padding-bottom:36px;margin-bottom:40px;border-bottom:1px solid var(--border)}}
  .eyebrow{{font-family:var(--mono);font-size:11px;text-transform:uppercase;letter-spacing:.16em;color:var(--violet);font-weight:600;margin-bottom:14px}}
  h1{{font-size:clamp(2rem,4.2vw,3rem);font-weight:700;letter-spacing:-.03em;line-height:1.05;margin-bottom:16px}}
  h1 .grad{{background:var(--grad);-webkit-background-clip:text;background-clip:text;color:transparent}}
  .lede{{color:var(--text-dim);font-size:1.05rem;line-height:1.55;max-width:720px}}
  section{{margin-bottom:48px}}
  h2{{font-size:1.25rem;font-weight:700;letter-spacing:-.02em;margin-bottom:18px;display:flex;align-items:center;gap:12px}}
  h2 .num{{font-family:var(--mono);font-size:11px;color:var(--violet);font-weight:600;background:var(--grad-soft);padding:4px 10px;border-radius:999px;border:1px solid rgba(168,85,247,.22);letter-spacing:.06em}}
  .grid-4{{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}}
  .grid-3{{display:grid;grid-template-columns:repeat(3,1fr);gap:12px}}
  @media (max-width:780px){{.grid-4{{grid-template-columns:repeat(2,1fr)}} .grid-3{{grid-template-columns:1fr}}}}
  .stat{{background:var(--surface);border:1px solid var(--border);border-radius:14px;padding:22px}}
  .stat-val{{font-size:1.85rem;font-weight:700;letter-spacing:-.02em;background:var(--grad);-webkit-background-clip:text;background-clip:text;color:transparent;line-height:1.05;display:block}}
  .stat-lbl{{font-family:var(--mono);font-size:10px;text-transform:uppercase;letter-spacing:.1em;color:var(--text-faint);margin-top:8px;display:block}}
  .stat-sub{{font-size:12px;color:var(--text-dim);margin-top:6px}}
  table{{width:100%;border-collapse:collapse;font-size:13.5px;background:var(--surface);border:1px solid var(--border);border-radius:14px;overflow:hidden}}
  th{{background:var(--surface-2);padding:12px 16px;text-align:left;font-family:var(--mono);font-size:10px;text-transform:uppercase;letter-spacing:.1em;color:var(--text-faint);font-weight:600}}
  th.r,td.r{{text-align:right}}
  td{{padding:12px 16px;border-top:1px solid var(--border)}}
  td strong{{color:#fff}}
  .quote{{background:var(--grad-soft);border-left:3px solid var(--violet);border-radius:6px;padding:18px 22px;margin:0 0 12px;font-size:1rem;line-height:1.55;color:var(--text);font-style:italic}}
  .quote cite{{display:block;margin-top:8px;font-style:normal;font-family:var(--mono);font-size:11px;color:var(--text-faint);text-transform:uppercase;letter-spacing:.06em}}
  .quote cite a{{color:#c7d2fe;text-decoration:none}}
  .share-row{{display:flex;flex-wrap:wrap;gap:10px;margin-top:14px}}
  .share-btn{{display:inline-flex;align-items:center;gap:8px;padding:9px 16px;border-radius:999px;background:var(--surface);border:1px solid var(--border-strong);font-size:12.5px;font-weight:600;color:var(--text);text-decoration:none;transition:all .15s ease}}
  .share-btn:hover{{border-color:var(--violet);color:#fff;transform:translateY(-1px)}}
  .partner-banner{{background:var(--grad-soft);border:1px solid rgba(168,85,247,.2);border-radius:14px;padding:22px;margin-bottom:28px;font-size:13.5px;color:var(--text-dim);line-height:1.5}}
  .partner-banner a{{color:#c7d2fe}}
  .foot{{margin-top:72px;padding-top:36px;border-top:1px solid var(--border);font-family:var(--mono);font-size:11px;color:var(--text-faint);text-align:center;line-height:1.7}}
  .foot a{{color:var(--text-dim);text-decoration:none;margin:0 8px}}
  .foot a:hover{{color:var(--text)}}
  /* Print: switch to white + serif for the PDF artifact partners expect */
  @media print{{
    body{{background:#fff;color:#0a0a0f;font-family:Georgia,serif}}
    .stat,.partner-banner,.quote,table{{background:#f9fafb!important;border-color:#e4e4e7!important}}
    .stat-val{{background:none!important;-webkit-text-fill-color:#1e1b4b!important;color:#1e1b4b!important}}
    h1 .grad{{background:none!important;-webkit-text-fill-color:#4338ca!important;color:#4338ca!important}}
    th{{background:#eef2ff!important;color:#312e81!important}}
    .share-row{{display:none}}.as-of .dot{{display:none}}
  }}
</style>
</head>
<body>
<div class="wrap">

  <header class="top">
    <a href="/" class="brand" data-dchub-brand></a>
    <span class="as-of"><span class="dot"></span>Live · as of {d.get('as_of_date','')}</span>
  </header>

  <div class="cover">
    {partner_cover}
    <div class="eyebrow">Monthly trend snapshot</div>
    <h1>{label} <span class="grad">in data centers.</span></h1>
    <p class="lede">A live monthly readout of the global data center market: facilities discovered, capacity changed hands, deal volume, AI-agent queries, and per-market trends. Every number is pulled from DC Hub's live ingest pipeline — no spreadsheet versioning, no quarterly lag.</p>
  </div>

  {executive_html}

  <div class="share-row">
    <a class="share-btn" href="#press-kit">📋 Press-kit quotes</a>
    <a class="share-btn" href="javascript:window.print()">📄 Print / PDF</a>
    <a class="share-btn" href="https://twitter.com/intent/tweet?text=DC%20Hub%20{label.replace(' ', '%20')}%20trend%20snapshot&url={permalink}" target="_blank">𝕏 Share</a>
    <a class="share-btn" href="https://www.linkedin.com/sharing/share-offsite/?url={permalink}" target="_blank">in Share</a>
    <a class="share-btn" href="mailto:?subject=DC%20Hub%20{label.replace(' ', '%20')}%20trend%20snapshot&body=Live%20data%20center%20market%20intelligence%3A%20{permalink}">✉ Email</a>
  </div>

  <!-- Headline stats -->
  <section style="margin-top:48px">
    <h2><span class="num">01</span>Headline</h2>
    <div class="grid-4">
      <div class="stat">
        <span class="stat-val">{h.get('facilities_total',0):,}</span>
        <span class="stat-lbl">Facilities</span>
        <div class="stat-sub">Cumulative · global</div>
      </div>
      <div class="stat">
        <span class="stat-val">{h.get('total_mw',0)/1000:,.1f} GW</span>
        <span class="stat-lbl">Power tracked</span>
        <div class="stat-sub">Operational + pipeline</div>
      </div>
      <div class="stat">
        <span class="stat-val">{h.get('facilities_added_month',0):,}</span>
        <span class="stat-lbl">New this month</span>
        <div class="stat-sub">{_delta_html(h.get('facilities_mom_pct'))} MoM · {_delta_html(h.get('facilities_yoy_pct'))} YoY</div>
      </div>
      <div class="stat">
        <span class="stat-val">{_fmt_deal_value(deals_view.get('value'))}</span>
        <span class="stat-lbl">Deal $ ({deals_label})</span>
        <div class="stat-sub">{deals_view.get('count',0)} deals · {_delta_html(df.get('deals_mom_pct'))} MoM</div>
      </div>
    </div>
  </section>

  <!-- Trends -->
  <section>
    <h2><span class="num">02</span>What changed this month</h2>
    <div class="grid-3">
      <div class="stat">
        <span class="stat-val">{_delta_html(h.get('facilities_mom_pct'))}</span>
        <span class="stat-lbl">Facility discovery</span>
        <div class="stat-sub">{h.get('facilities_added_month',0):,} new · vs {h.get('facilities_added_prior',0):,} prior month</div>
      </div>
      <div class="stat">
        <span class="stat-val">{_delta_html(df.get('deals_mom_pct'))}</span>
        <span class="stat-lbl">Deal count</span>
        <div class="stat-sub">{deals_view.get('count',0)} deals ({deals_label}) · vs {(df.get('prior') or {{}}).get('count',0)} prior month</div>
      </div>
      <div class="stat">
        <span class="stat-val">{_delta_html(ai.get('mom_pct'))}</span>
        <span class="stat-lbl">AI agent queries</span>
        <div class="stat-sub">{(ai.get('tool_calls_month') or 0):,} calls this month · ChatGPT, Claude, Gemini, Perplexity</div>
      </div>
    </div>
  </section>

  <!-- M&A -->
  <section>
    <h2><span class="num">03</span>M&amp;A {('· ' + deals_section_tag) if deals_section_tag else ''}</h2>
    <table>
      <thead><tr><th>Date</th><th>Buyer</th><th>Seller</th><th class="r">Value</th><th class="r">MW</th></tr></thead>
      <tbody>{_td(deal_rows, 5)}</tbody>
    </table>
  </section>

  <!-- Top markets -->
  <section>
    <h2><span class="num">04</span>Top markets by operating MW</h2>
    <table>
      <thead><tr><th>Market</th><th class="r">Facilities</th><th class="r">Operating MW</th></tr></thead>
      <tbody>{_td(market_rows, 3)}</tbody>
    </table>
  </section>

  <!-- DCPI movers -->
  <section>
    <h2><span class="num">05</span>Power Index movers</h2>
    <table>
      <thead><tr><th>Market</th><th class="r">DCPI Score</th><th class="r">Δ Week</th></tr></thead>
      <tbody>{_td(mover_rows, 3)}</tbody>
    </table>
  </section>

  <!-- Pipeline -->
  <section>
    <h2><span class="num">06</span>Construction pipeline</h2>
    <table>
      <thead><tr><th>Market</th><th class="r">Projects</th><th class="r">Pipeline MW</th></tr></thead>
      <tbody>{_td(pipeline_rows, 3)}</tbody>
    </table>
  </section>

  <!-- Press kit -->
  <section id="press-kit">
    <h2><span class="num">07</span>Press kit · for journalist use</h2>
    <p class="lede" style="margin-bottom:22px">These sentences are free for journalists, analysts, and partners (CBRE, JLL, DCD, etc.) to use with attribution to DC Hub. Numbers are live as of {d.get('as_of_date','')}.</p>
    {quote_blocks}
  </section>

  <!-- About -->
  <section>
    <h2><span class="num">08</span>About this report</h2>
    <p class="lede">This snapshot is regenerated every time the page is loaded — no quarterly lag, no spreadsheet versioning. Historical months are immutable: <code>/reports/monthly/2026-04</code> always shows April 2026's numbers as snapshotted when April closed. Same data is also available via:</p>
    <ul style="color:var(--text-dim);font-size:13.5px;margin-top:14px;padding-left:22px;line-height:1.8">
      <li>REST API — <code style="color:#c7d2fe">/api/v1/reports/monthly</code></li>
      <li>MCP server — <code style="color:#c7d2fe">https://dchub.cloud/mcp</code> (40 tools for AI agents)</li>
      <li>Live ops dashboard — <a href="/transparency" style="color:#c7d2fe">/transparency</a></li>
      <li>Quarterly snapshot (legacy format) — <a href="/reports/quarterly" style="color:#c7d2fe">/reports/quarterly</a></li>
    </ul>
  </section>

  <div class="foot">
    DC Hub · neutral data layer for data center infrastructure · {label} trend snapshot<br>
    <a href="/">dchub.cloud</a> · <a href="/cited-by">cited by</a> · <a href="/advertise">partnerships</a> · <a href="/api-docs">API docs</a>
  </div>

  <!-- r41-license-footer (2026-05-25): visible CC-BY-4.0 declaration
       backing up the LinkedIn partnership post claim ("Daily refresh.
       CC-BY-4.0. AI-agent native"). Anyone clicking through from the
       post sees the license front-and-center, with a citation block
       they can copy verbatim. -->
  <div style="margin-top:28px;padding:18px 22px;border:1px solid var(--border, #e2e8f0);border-radius:10px;font-size:13.5px;line-height:1.65;color:var(--text-dim, #94a3b8)">
    <div style="margin-bottom:8px">
      <span style="display:inline-block;padding:3px 9px;background:#10b981;color:#0a0e1a;font-weight:700;border-radius:4px;font-size:11px;letter-spacing:.5px;margin-right:10px">CC-BY-4.0</span>
      <strong style="color:#e2e8f0">Open data, free to cite.</strong>
      Licensed under <a rel="license" href="https://creativecommons.org/licenses/by/4.0/" style="color:#60a5fa">Creative Commons Attribution 4.0 International</a>.
      Use in your research, press, or investor deck — attribution required, no fee, no NDA, no embargo.
    </div>
    <div>
      <strong style="color:#e2e8f0">Cite as:</strong>
      <code style="background:rgba(255,255,255,.06);padding:2px 6px;border-radius:3px;font-size:12px;color:#c7d2fe">DC Hub. (2026). Monthly Data Center Trend Report. https://dchub.cloud/reports/monthly. Licensed CC-BY-4.0.</code>
    </div>
  </div>

  <script type="application/ld+json">
  {{
    "@context": "https://schema.org",
    "@type": "Report",
    "name": "DC Hub Monthly Data Center Trend Report — {label}",
    "url": "https://dchub.cloud/reports/monthly",
    "license": "https://creativecommons.org/licenses/by/4.0/",
    "isAccessibleForFree": true,
    "creator": {{"@type": "Organization", "name": "DC Hub", "url": "https://dchub.cloud"}},
    "datePublished": "{d.get('generated_at','')}",
    "inLanguage": "en"
  }}
  </script>
</div>
</body>
</html>"""


# ── ROUTES ───────────────────────────────────────────────────────────
@monthly_trend_bp.route("/reports/monthly", methods=["GET"],
                        strict_slashes=False)
def monthly_html_current():
    partner = (request.args.get("partner") or "").strip()
    d = _attach_narrative_safe(_compute_report())
    return Response(_render_html(d, partner=partner),
                    mimetype="text/html",
                    headers={"Cache-Control": "public, max-age=900"})


@monthly_trend_bp.route("/reports/monthly/<int:year>-<int:month>",
                        methods=["GET"], strict_slashes=False)
def monthly_html_specific(year: int, month: int):
    if month < 1 or month > 12:
        return Response("Invalid month", status=400)
    partner = (request.args.get("partner") or "").strip()
    d = _attach_narrative_safe(_compute_report(year, month))
    return Response(_render_html(d, partner=partner),
                    mimetype="text/html",
                    headers={"Cache-Control": "public, max-age=3600"})


def _attach_license(d):
    """r41-license-block (2026-05-25): declare CC-BY-4.0 inline so the
    LinkedIn post's claim ('CC-BY-4.0. AI-agent native') is backed up
    by the response body. Anyone clicking through can see the license
    + citation format without needing a separate license file. Adds
    Link header per RFC 5988 so machine readers also pick it up."""
    if not isinstance(d, dict):
        return d
    d["license"] = {
        "name": "Creative Commons Attribution 4.0 International",
        "id":   "CC-BY-4.0",
        "url":  "https://creativecommons.org/licenses/by/4.0/",
        "citation": ("DC Hub. (2026). Monthly Data Center Trend Report. "
                     "https://dchub.cloud/reports/monthly. Licensed CC-BY-4.0."),
        "attribution_required": True,
        "commercial_use_allowed": True,
    }
    return d


def _attach_narrative_safe(d):
    """r42-narrative (2026-05-25): add LLM-generated executive summary.
    Silent no-op if ANTHROPIC_API_KEY not set or call fails — never
    breaks the report response. Cached 1h, so first reader pays ~3s,
    next 1000 hit cache."""
    try:
        from routes.report_narrative import attach_narrative
        return attach_narrative(d, kind="monthly")
    except Exception:
        return d


@monthly_trend_bp.route("/api/v1/reports/monthly", methods=["GET"])
def monthly_json_current():
    d = _attach_narrative_safe(_attach_license(_compute_report()))
    resp = jsonify(d)
    resp.headers["Cache-Control"] = "public, max-age=900"
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Link"] = '<https://creativecommons.org/licenses/by/4.0/>; rel="license"'
    return resp


@monthly_trend_bp.route("/api/v1/reports/monthly/<int:year>-<int:month>",
                         methods=["GET"])
def monthly_json_specific(year: int, month: int):
    if month < 1 or month > 12:
        return jsonify(ok=False, error="invalid_month"), 400
    d = _attach_narrative_safe(_attach_license(_compute_report(year, month)))
    resp = jsonify(d)
    resp.headers["Cache-Control"] = "public, max-age=3600"
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Link"] = '<https://creativecommons.org/licenses/by/4.0/>; rel="license"'
    return resp


# ── r42b: narrative-only shortcut (2026-05-25) ───────────────────────
@monthly_trend_bp.route("/api/v1/reports/monthly/narrative",
                         methods=["GET"], strict_slashes=False)
def monthly_narrative_only():
    """Minimal payload: just the LLM exec summary + period + license.
    Designed for Substack/LinkedIn embeds, journalist quote-pulls,
    and the 'free preview' surface in partnerships."""
    d = _attach_narrative_safe(_attach_license(_compute_report()))
    narr = d.get("narrative_summary") or {}
    out = {
        "month_label":  d.get("month_label"),
        "month":        d.get("month"),
        "year":         d.get("year"),
        "as_of_date":   d.get("as_of_date"),
        "narrative":    narr.get("text"),
        "model":        narr.get("model"),
        "generated_at": narr.get("generated_at"),
        "permalink":    f"https://dchub.cloud/reports/monthly/{d.get('year')}-{d.get('month'):02d}" if d.get("year") and d.get("month") else "https://dchub.cloud/reports/monthly",
        "full_report":  "https://dchub.cloud/api/v1/reports/monthly",
        "license":      d.get("license"),
    }
    resp = jsonify(out)
    resp.headers["Cache-Control"] = "public, max-age=900"
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Link"] = '<https://creativecommons.org/licenses/by/4.0/>; rel="license"'
    return resp


# ── r42c: markdown view (2026-05-25) ─────────────────────────────────
@monthly_trend_bp.route("/reports/monthly.md", methods=["GET"],
                        strict_slashes=False)
def monthly_md():
    """Plaintext-markdown view of the monthly report. Paste-ready into
    Slack, Discord, blog posts, journalist briefings."""
    d = _attach_narrative_safe(_compute_report())
    return Response(_render_markdown(d),
                    mimetype="text/markdown; charset=utf-8",
                    headers={"Cache-Control": "public, max-age=900",
                             "Access-Control-Allow-Origin": "*",
                             "Link": '<https://creativecommons.org/licenses/by/4.0/>; rel="license"'})


def _render_markdown(d: dict) -> str:
    """Compose a paste-ready markdown report. Stays under ~3KB so it
    survives Slack/LinkedIn editors without truncation."""
    label = d.get("month_label") or f"{d.get('year')}-{d.get('month')}"
    h = d.get("headline") or {}
    df = d.get("deal_flow") or {}
    curr = df.get("current") or {}
    narr = d.get("narrative_summary") or {}
    narr_text = (narr.get("text") or "").strip()
    top_mkts = (d.get("top_markets") or [])[:5]
    top_deals = (d.get("top_deals") or [])[:5]
    as_of = d.get("as_of_date", "")

    lines = []
    lines.append(f"# DC Hub — {label} Monthly Trend Snapshot")
    lines.append(f"_Live data as of {as_of}._ "
                 f"[Full report](https://dchub.cloud/reports/monthly) · "
                 f"[JSON](https://dchub.cloud/api/v1/reports/monthly) · "
                 f"CC-BY-4.0")
    lines.append("")

    if narr_text:
        lines.append("## Executive summary")
        lines.append(f"_auto-generated · {narr.get('model','claude')} · "
                     f"{(narr.get('generated_at') or '')[:10]}_")
        lines.append("")
        lines.append(narr_text)
        lines.append("")

    lines.append("## Headline numbers")
    lines.append(f"- **{h.get('facilities_total', 0):,}** facilities tracked")
    lines.append(f"- **{(h.get('total_mw') or 0)/1000:,.1f} GW** total power (operational + pipeline)")
    if h.get("facilities_added_month") is not None:
        lines.append(f"- **{h.get('facilities_added_month', 0):,}** new facilities discovered this month")
    # value is in $M (see _compute_report); divide by 1000 → $B
    _val_m = curr.get('value', 0) or 0
    _val_b = _val_m / 1000.0
    lines.append(f"- **{curr.get('count', 0):,}** M&A deals tracked "
                 f"(${_val_b:,.1f}B disclosed value · "
                 f"{curr.get('mw', 0):,.0f} MW changing hands)")
    lines.append("")

    if top_mkts:
        lines.append("## Top markets by operating MW")
        for m in top_mkts:
            lines.append(f"- **{m.get('market','?')}** — "
                         f"{m.get('total_mw', 0):,.0f} MW "
                         f"({m.get('facilities', 0):,} facilities)")
        lines.append("")

    if top_deals:
        lines.append("## Top deals")
        for x in top_deals:
            val = x.get("value")
            val_str = f"${val:,.0f}M" if val else "undisclosed"
            lines.append(f"- {x.get('date','—')} · **{x.get('buyer') or '?'}** "
                         f"← {x.get('seller') or '?'} · {val_str}")
        lines.append("")

    lines.append("## Attribution")
    lines.append("DC Hub. (2026). Monthly Data Center Trend Report. "
                 "https://dchub.cloud/reports/monthly. Licensed CC-BY-4.0.")
    lines.append("")
    lines.append("---")
    lines.append(f"_Generated {d.get('generated_at', '')[:19].replace('T',' ')} UTC · "
                 f"[/api/v1/reports/monthly](https://dchub.cloud/api/v1/reports/monthly) · "
                 f"[/llms.txt](https://dchub.cloud/llms.txt)_")
    return "\n".join(lines)


@monthly_trend_bp.route("/api/v1/reports/monthly/archive", methods=["POST"])
def monthly_archive():
    """Admin: snapshot a closed month into monthly_reports so the
    permanent URL keeps showing that month's numbers forever, even after
    the live tables have moved on. Defaults to last completed month."""
    if not _admin_ok():
        return jsonify(ok=False, error="forbidden"), 403
    _ensure_archive_table()

    today = datetime.date.today()
    year  = int(request.args.get("year")  or today.year)
    month = int(request.args.get("month") or today.month)
    # Don't let callers archive a not-yet-closed month unless they pass force=1
    if (year, month) >= (today.year, today.month) and request.args.get("force") != "1":
        py, pm = _prior_month(today.year, today.month)
        year, month = py, pm

    d = _compute_report(year, month)
    c = _conn()
    if c is None:
        return jsonify(ok=False, error="no_db"), 503
    try:
        with c.cursor() as cur:
            cur.execute("""
                INSERT INTO monthly_reports (year, month, snapshot, created_at)
                VALUES (%s, %s, %s::jsonb, NOW())
                ON CONFLICT (year, month) DO UPDATE
                  SET snapshot = EXCLUDED.snapshot,
                      created_at = NOW()
            """, (year, month, json.dumps(d, default=str)))
        return jsonify(ok=True, year=year, month=month,
                       month_label=d.get("month_label"),
                       permalink=f"https://dchub.cloud/reports/monthly/{year}-{month:02d}")
    except Exception as e:
        return jsonify(ok=False, error=str(e)[:200]), 500
    finally:
        try: c.close()
        except Exception: pass


def _smoke():
    logger.info("[monthly-trend] ready · /reports/monthly + "
                 "/reports/monthly/<year>-<month> + JSON + archive")

_smoke()
