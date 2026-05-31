"""
media_north_star.py — DC Hub Media north-star scoreboard.

ONE number that matters for DC Hub Media: **citation velocity** —
how many DISTINCT AI agents / sources cite us per week. Everything
else (press cadence, LinkedIn, narrative arcs) is in service of
moving this single metric.

  GET /api/v1/media/north-star

Returns the citation-velocity scoreboard, deduped across the two
citation tables that exist on prod:

  - ai_testimonials       — curated table (ChatGPT/Claude/Perplexity
                            quotes etc). Approval-gated.
  - ai_testimonials_auto  — every-6h auto-ingested mentions
                            (HackerNews / Reddit / MCP-derived).

Both schemas have DRIFTED across deploys (the auto table has two
incompatible variants — one with posted_at/captured_at/url, another
with cited_at/source_url and no agent_name), so EVERYTHING here is
runtime-introspected via information_schema BEFORE a column is named.
A missing table, missing column, or empty result yields zeros / empty
arrays with ok:true — this endpoint NEVER returns 500. This mirrors
the proven schema-tolerant pattern in routes/agent_broadcast.py and
the COUNT(DISTINCT COALESCE(NULLIF(...))) distinct-source counting in
routes/dchub_media_hub.py.

"distinct citing source" =
    COUNT(DISTINCT COALESCE(NULLIF(agent_name,''),
                            NULLIF(source,''),
                            platform))

Dedup across the two tables: a citation is unique by url when url is
non-empty, else by the coalesced source string. We UNION both tables
into a normalized (src, url, ts, platform, agent_name, quote) CTE,
dedup that set, then derive every window count from it so a quote that
lands in BOTH tables is counted once.

CORS-open, no auth, Cache-Control public max-age=300. Designed to be
the homepage / pitch-deck scoreboard for "AI citations of DC Hub".
"""
from __future__ import annotations

import datetime
import logging

from flask import Blueprint, jsonify, request, Response

logger = logging.getLogger("dchub.media_north_star")

media_north_star_bp = Blueprint("media_north_star", __name__)

# Truncate quotes in the `recent` rail to this many chars.
_QUOTE_MAX = 160


# ── DB + introspection helpers (mirror agent_broadcast.py) ──────────

def _db_conn():
    """Get a psycopg2 connection or None. Mirrors the resilient helper
    in routes/agent_broadcast.py — never raises on a missing env var or
    an unreachable DB, so the endpoint degrades to zeros instead of 500.
    """
    try:
        import os
        import psycopg2
        url = (os.environ.get("DATABASE_URL")
               or os.environ.get("NEON_DATABASE_URL"))
        if not url:
            return None
        return psycopg2.connect(url, connect_timeout=5)
    except Exception:
        return None


def _table_cols(cur, table: str) -> set[str]:
    """Return the set of column names for a table (empty if absent).

    Used to build schema-tolerant SELECTs — both citation tables have
    drifted across deploys, so we introspect rather than assume.
    """
    try:
        cur.execute("""
            SELECT column_name FROM information_schema.columns
             WHERE table_name = %s
        """, (table,))
        return {r[0] for r in (cur.fetchall() or [])}
    except Exception:
        return set()


def _first_col(cols: set[str], *candidates: str) -> str | None:
    for c in candidates:
        if c in cols:
            return c
    return None


def _src_expr(cols: set[str]) -> str:
    """The canonical 'distinct citing source' expression, built over
    only the columns that exist. Always non-NULL (falls back to '').

        COUNT(DISTINCT COALESCE(NULLIF(agent_name,''),
                                NULLIF(source,''),
                                platform))

    'platform' is the last resort; if even it is missing we end in ''
    so the expression stays text-typed and never NULL-only.
    """
    parts: list[str] = []
    if "agent_name" in cols:
        parts.append("NULLIF(agent_name, '')")
    if "source" in cols:
        parts.append("NULLIF(source, '')")
    if "platform" in cols:
        parts.append("platform")
    parts.append("''")
    return "COALESCE(" + ", ".join(parts) + ")"


def _url_expr(cols: set[str]) -> str:
    """Best-available url column (url, else source_url), else ''.
    The two auto-table variants disagree: one has `url`, the seeder
    variant has `source_url`."""
    col = _first_col(cols, "url", "source_url")
    return col if col else "''"


def _agent_expr(cols: set[str]) -> str:
    """Display name for the `recent` rail."""
    parts: list[str] = []
    if "agent_name" in cols:
        parts.append("NULLIF(agent_name, '')")
    if "platform" in cols:
        parts.append("NULLIF(platform, '')")
    if "source" in cols:
        parts.append("NULLIF(source, '')")
    parts.append("'AI agent'")
    return "COALESCE(" + ", ".join(parts) + ")"


def _platform_expr(cols: set[str]) -> str:
    parts: list[str] = []
    if "platform" in cols:
        parts.append("NULLIF(platform, '')")
    if "source" in cols:
        parts.append("NULLIF(source, '')")
    parts.append("'unknown'")
    return "COALESCE(" + ", ".join(parts) + ")"


def _synthetic_exclusion(cols: set[str]) -> list[str]:
    """SQL predicates that drop internal/synthetic rows — cron heartbeat
    log lines, mcp-auto markers, system noise — so they NEVER inflate
    citation velocity. Mirrors the Phase-299 'mcp-auto' exclusion in
    dchub_media.py. Without this the auto-ingest's own cron-heartbeat
    rows (platform='internal', agent_name='cron') get counted as
    "citations", which is exactly the false-velocity trap the
    distinct-source counting is meant to avoid. Only names columns that
    exist on this deploy's variant.
    """
    preds: list[str] = []
    if "platform" in cols:
        preds.append("(platform IS NULL OR LOWER(platform) "
                     "NOT IN ('internal', 'system', 'cron'))")
    if "agent_name" in cols:
        preds.append("(agent_name IS NULL OR LOWER(agent_name) "
                     "NOT IN ('cron', 'system', 'mcp-auto', 'dchub', "
                     "'heartbeat'))")
    if "source" in cols:
        preds.append("(source IS NULL OR (LOWER(source) NOT LIKE '%cron%' "
                     "AND LOWER(source) NOT LIKE '%heartbeat%' "
                     "AND LOWER(source) NOT LIKE '%mcp-auto%'))")
    if "quote" in cols:
        # Belt-and-suspenders: the cron rows self-identify in the quote.
        preds.append("(quote IS NULL OR LOWER(quote) "
                     "NOT LIKE 'cron heartbeat%')")
    return preds


def _build_table_select(cols: set[str], table: str,
                        ts_candidates: tuple[str, ...],
                        approval_filter: bool) -> str | None:
    """Build a normalized SELECT for one citation table, or None if the
    table/quote column is absent.

    Emits columns: src, url, ts, platform, agent_name, quote.
    Each underlying column is introspected, so a column missing on this
    deploy's variant never raises 'column does not exist'.
    """
    if "quote" not in cols:
        return None
    ts_col = _first_col(cols, *ts_candidates)
    ts_expr = ts_col if ts_col else "NULL::timestamptz"
    clauses: list[str] = []
    if approval_filter and "approved" in cols:
        # ai_testimonials is approval-gated. Per spec: when the column
        # exists, count approved-only. The auto table has no real
        # approval workflow on some variants, so it is included whole.
        clauses.append("approved = true")
    # Always drop internal/synthetic noise (cron heartbeats etc.) from
    # BOTH tables so citation velocity reflects real external citations.
    clauses.extend(_synthetic_exclusion(cols))
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    return f"""
        SELECT {_src_expr(cols)}      AS src,
               {_url_expr(cols)}      AS url,
               {ts_expr}              AS ts,
               {_platform_expr(cols)} AS platform,
               {_agent_expr(cols)}    AS agent_name,
               quote                  AS quote
          FROM {table}
          {where}
    """


def _scalar(cur, sql: str, params: tuple = ()) -> int:
    """Run a COUNT-style query, return int(0) on any failure (and roll
    back so the connection stays usable for the next query)."""
    try:
        cur.execute(sql, params)
        row = cur.fetchone()
        return int(row[0]) if row and row[0] is not None else 0
    except Exception:
        try:
            cur.connection.rollback()
        except Exception:
            pass
        return 0


def _compute() -> dict:
    """Assemble the north-star payload. Every query is independently
    guarded — one failing table or column never kills the response.
    """
    now = datetime.datetime.utcnow()
    payload: dict = {
        "ok":                   True,
        "as_of":                now.isoformat() + "Z",
        "north_star":           "citation_velocity",
        "citation_velocity_7d": 0,
        "citation_velocity_30d": 0,
        "prior_7d":             0,
        "trend_pct":            None,
        "total_citations":      0,
        "by_platform":          [],
        "recent":               [],
    }

    c = _db_conn()
    if not c:
        # No DB reachable (e.g. local dev without DATABASE_URL). Return
        # the honest zero scoreboard rather than a 500.
        return payload

    try:
        with c.cursor() as cur:
            main_cols = _table_cols(cur, "ai_testimonials")
            auto_cols = _table_cols(cur, "ai_testimonials_auto")

            # Per-table normalized SELECTs. ai_testimonials prefers
            # approved_at then created_at; the auto table prefers
            # posted_at, then captured_at, then cited_at, then
            # created_at (covering both known auto-table variants).
            selects: list[str] = []
            main_sel = _build_table_select(
                main_cols, "ai_testimonials",
                ("approved_at", "created_at"),
                approval_filter=True,
            )
            if main_sel:
                selects.append(main_sel)
            auto_sel = _build_table_select(
                auto_cols, "ai_testimonials_auto",
                ("posted_at", "captured_at", "cited_at", "created_at"),
                approval_filter=False,
            )
            if auto_sel:
                selects.append(auto_sel)

            if not selects:
                # Neither table exists / has a usable schema.
                return payload

            union_sql = "\nUNION ALL\n".join(selects)

            # Deduped citation set: unique by url when url is non-empty,
            # else by the coalesced source string. We keep the EARLIEST
            # ts per dedup key so a re-ingested quote doesn't inflate a
            # later window. DISTINCT ON requires the ORDER BY to lead
            # with the dedup key.
            base_cte = f"""
                WITH raw AS (
                    {union_sql}
                ),
                keyed AS (
                    SELECT
                        CASE WHEN COALESCE(url, '') <> ''
                             THEN 'u:' || url
                             ELSE 's:' || COALESCE(NULLIF(src, ''), 'unknown')
                        END                              AS dedup_key,
                        src, url, ts, platform, agent_name, quote
                      FROM raw
                ),
                deduped AS (
                    SELECT DISTINCT ON (dedup_key)
                           dedup_key, src, url, ts, platform,
                           agent_name, quote
                      FROM keyed
                     ORDER BY dedup_key, ts ASC NULLS LAST
                )
            """

            def distinct_sources(window_clause: str,
                                 params: tuple = ()) -> int:
                sql = (base_cte + f"""
                    SELECT COUNT(DISTINCT COALESCE(NULLIF(src, ''), 'unknown'))
                      FROM deduped
                     {window_clause}
                """)
                return _scalar(cur, sql, params)

            # citation_velocity_7d / 30d / prior_7d (days 8-14 ago) /
            # all-time. Each is independently guarded by _scalar.
            payload["citation_velocity_7d"] = distinct_sources(
                "WHERE ts >= NOW() - INTERVAL '7 days'")
            payload["citation_velocity_30d"] = distinct_sources(
                "WHERE ts >= NOW() - INTERVAL '30 days'")
            payload["prior_7d"] = distinct_sources(
                "WHERE ts >= NOW() - INTERVAL '14 days' "
                "AND ts < NOW() - INTERVAL '7 days'")
            payload["total_citations"] = distinct_sources("")

            # trend_pct: (7d - prior_7d) / prior_7d * 100, 1dp; null if
            # prior_7d == 0.
            prior = payload["prior_7d"]
            if prior and prior > 0:
                cur_7d = payload["citation_velocity_7d"]
                payload["trend_pct"] = round(
                    (cur_7d - prior) / prior * 100.0, 1)
            else:
                payload["trend_pct"] = None

            # by_platform: last 30d, top 10 platforms by distinct-source
            # count, desc. Counting distinct sources (not raw rows) keeps
            # one looping power-source from dominating the breakdown.
            try:
                cur.execute(base_cte + """
                    SELECT platform,
                           COUNT(DISTINCT COALESCE(NULLIF(src, ''), 'unknown'))
                                                                  AS cnt
                      FROM deduped
                     WHERE ts >= NOW() - INTERVAL '30 days'
                     GROUP BY platform
                     ORDER BY cnt DESC, platform ASC
                     LIMIT 10
                """)
                for r in cur.fetchall() or []:
                    plat, cnt = r
                    payload["by_platform"].append({
                        "platform": plat or "unknown",
                        "count":    int(cnt or 0),
                    })
            except Exception:
                try:
                    c.rollback()
                except Exception:
                    pass

            # recent: last 8 citations, newest first, quote truncated.
            try:
                cur.execute(base_cte + """
                    SELECT agent_name, platform, quote, url, ts
                      FROM deduped
                     ORDER BY ts DESC NULLS LAST
                     LIMIT 8
                """)
                for r in cur.fetchall() or []:
                    agent_name, platform, quote, url, ts = r
                    payload["recent"].append({
                        "agent_name": agent_name or "AI agent",
                        "platform":   platform or "unknown",
                        "quote":      (quote or "")[:_QUOTE_MAX],
                        "url":        url or "https://dchub.cloud",
                        "at":         (ts.isoformat()
                                       if hasattr(ts, "isoformat") else None),
                    })
            except Exception:
                try:
                    c.rollback()
                except Exception:
                    pass

    except Exception as e:
        # Total-failure guard: log and return whatever we have (zeros).
        logger.warning("north-star compute failed: %s", e)
        try:
            c.rollback()
        except Exception:
            pass
    finally:
        try:
            c.close()
        except Exception:
            pass

    return payload


# ── Endpoint ────────────────────────────────────────────────────────

def _cors_headers() -> dict:
    return {
        "Access-Control-Allow-Origin":  "*",
        "Access-Control-Allow-Methods": "GET, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type, X-Agent-Name",
        "Cache-Control":                "public, max-age=300",
    }


@media_north_star_bp.route(
    "/api/v1/media/north-star", methods=["GET", "OPTIONS"]
)
def media_north_star():
    if request.method == "OPTIONS":
        return ("", 204, _cors_headers())
    try:
        payload = _compute()
    except Exception as e:
        # Belt-and-suspenders: never 500. _compute already guards, but
        # if anything slips through, return the honest zero scoreboard.
        logger.warning("north-star endpoint error: %s", e)
        payload = {
            "ok":                    True,
            "as_of":                 datetime.datetime.utcnow().isoformat() + "Z",
            "north_star":            "citation_velocity",
            "citation_velocity_7d":  0,
            "citation_velocity_30d": 0,
            "prior_7d":              0,
            "trend_pct":             None,
            "total_citations":       0,
            "by_platform":           [],
            "recent":                [],
        }
    resp: Response = jsonify(payload)
    for k, v in _cors_headers().items():
        resp.headers[k] = v
    return resp, 200
