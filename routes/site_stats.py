"""Phase PP (2026-05-15) — Live site stats for homepage + /intelligence hub.

A single JSON endpoint the frontend can hit on page load to replace ALL
hardcoded numbers ("15,000+ facilities", "300+ markets", "9,000+ substations"
etc.). Every count is queried live; nothing is hardcoded.

Why a dedicated endpoint instead of /api/v1/stats?
  - /api/v1/stats is facility-focused, returns 50+ source breakdowns.
  - Homepage needs ~10 specific numbers and a small grid-pulse block.
  - Caching strategy is different: this endpoint should be edge-cacheable
    (60s public cache) because every visitor sees the same numbers; the
    facility-stats endpoint is admin-y.

Public, no auth. 60s CDN cache.
"""

from __future__ import annotations

import os
import sys
import time
from datetime import datetime, timezone

import psycopg2
from flask import Blueprint, jsonify

from util.capacity_pipeline import CP_OK
from util.db_honesty import try_fetchall
from util.deals import DEALS_OK

site_stats_bp = Blueprint("site_stats", __name__)


# In-process cache so hammering this endpoint doesn't blow up the DB
# even if CF cache is bypassed. 60s TTL matches the public Cache-Control.
_CACHE: dict = {"payload": None, "ts": 0.0}
_CACHE_TTL = 60.0


def _conn():
    url = os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL")
    if not url:
        return None
    try:
        return psycopg2.connect(url, connect_timeout=6)
    except Exception as e:
        print(f"[site_stats] DB connect failed: {e}", file=sys.stderr)
        return None


class _Stats:
    """Collect homepage stats, keeping "we read 0" apart from "we could not read".

    ★ The predecessor helper here was `_scalar(cur, sql, default=0)`, which
    returned the DEFAULT on any error — "table missing, column missing,
    permission, etc." — so a broken read rendered as a confident `0` on a
    public, edge-cached surface. Three of them were broken, and the module
    docstring above says "Every count is queried live; nothing is hardcoded":

        air_permits        `air_permits` has never existed in this database
        transactions       `dc_transactions` has never existed; M&A is `deals`
        new_facilities_7d  `discovered_facilities.discovered_at` is TEXT, so
                           `discovered_at > NOW() - INTERVAL '7 days'` raises
                           UndefinedFunction (text > timestamptz). Measured
                           live 2026-08-01: the honest figure over the real
                           `first_seen` column is 1,258 — published as 0,
                           under a heading that reads "New this week — for
                           the 'DC Hub is growing' narrative".

    A count that failed is `None` plus a named error, never 0. A consumer can
    branch on null; it cannot detect a lie told as 0. See util/db_honesty.
    """

    def __init__(self, cur):
        self.cur = cur
        self.out: dict = {}
        self.errors: dict = {}

    def num(self, key, sql, cast=int, empty=0):
        """Read one cell. `empty` is what a SUCCESSFUL read of NULL means —
        0 for a COUNT, None for a MAX(timestamp) over no rows."""
        rows, err = try_fetchall(self.cur, sql)
        if err:
            self.out[key] = None
            self.errors[key] = err
            return None
        val = rows[0][0] if rows and rows[0] else None
        self.out[key] = cast(val) if val is not None else empty
        return self.out[key]

    def raw(self, key, sql, empty=None):
        """Same contract for a value that needs no cast (timestamps)."""
        return self.num(key, sql, cast=lambda v: v, empty=empty)


def _build_stats() -> dict:
    """Pull every homepage stat in one connection, one cursor.

    Failure isolation: util.db_honesty.try_fetchall rolls back on error, so
    a missing table does not cascade into the reads after it. The endpoint
    should always return something — but a stat it could not read comes back
    as null in `stats` plus a named reason in `stat_errors`, never as 0.
    """
    conn = _conn()
    if conn is None:
        return {"ok": False, "error": "no_database",
                "stats": {}, "as_of": _now()}

    st = _Stats(None)
    try:
        with conn.cursor() as cur:
            st.cur = cur
            s = st.out
            # ── Coverage ───────────────────────────────────────────
            # Phase AAA-2 (2026-05-17) — match the truth flip from Phase HH:
            # the homepage was painting 12,553 (legacy `facilities` table
            # count) while /api/v1/stats reports 21,374 (real `discovered_
            # facilities` count). User flagged this mismatch directly.
            # Now site/stats returns the same truth — discovered count if
            # available, fallback to legacy table. countries also pulls
            # from the larger pool.
            if st.num("facilities",
                      "SELECT COUNT(*) FROM discovered_facilities") is None:
                st.errors.pop("facilities", None)
                st.num("facilities", "SELECT COUNT(*) FROM facilities")
            # Expose both for backwards compatibility
            st.num("facilities_legacy_published",
                   "SELECT COUNT(*) FROM facilities")
            # ★2026-07-30: added `country <> ''` — byte-for-byte the
            # countries_covered query on /api/v1/stats/canonical. Without
            # the guard the empty-string bucket counts as a country and
            # the homepage reads one higher than canonical (the exact
            # ★2026-07-27 divergence documented in facilities_by_dims.py).
            if st.num("countries",
                      "SELECT COUNT(DISTINCT country) FROM discovered_facilities WHERE country IS NOT NULL AND country <> ''") is None:
                st.errors.pop("countries", None)
                st.num("countries",
                       "SELECT COUNT(DISTINCT country) FROM facilities WHERE country IS NOT NULL")
            st.num("markets_tracked",
                   "SELECT COUNT(DISTINCT market_slug) FROM market_power_scores WHERE published = true")
            st.num("build_markets",
                   "SELECT COUNT(DISTINCT market_slug) FROM market_power_scores WHERE published = true AND verdict = 'BUILD'")
            st.num("avoid_markets",
                   "SELECT COUNT(DISTINCT market_slug) FROM market_power_scores WHERE published = true AND verdict = 'AVOID'")
            st.num("substations", "SELECT COUNT(*) FROM substations")
            # ★ `air_permits` has NEVER existed here. It stays dead ON PURPOSE:
            # the live permit tables are construction_permits (752),
            # facility_permits (1,766) and permitting_intel (288), and none of
            # them is an air-permit population — choosing one is a data
            # -modelling decision, not a rename. What is NOT acceptable is the
            # silent `0` this published before. Null + a named error says "we
            # do not have this", which is the truth; `0` said "we looked and
            # there are none", which was not.
            st.num("air_permits", "SELECT COUNT(*) FROM air_permits")
            # `dc_transactions` has never existed either. The live M&A table is
            # `deals`, quarantine-guarded: 4,711 raw / 1,843 publishable, and
            # the raw figure is the stale "4,000+" the site stopped claiming
            # because it counts one transaction up to 945 times.
            st.num("transactions",
                   f"SELECT COUNT(*) FROM deals WHERE {DEALS_OK}")

            # ── Power capacity (real, queryable numbers) ───────────
            st.num("total_mw_tracked",
                   "SELECT COALESCE(SUM(power_mw), 0) FROM facilities WHERE power_mw IS NOT NULL",
                   cast=float, empty=0.0)
            st.num("operational_mw",
                   "SELECT COALESCE(SUM(power_mw), 0) FROM facilities WHERE power_mw IS NOT NULL AND LOWER(COALESCE(status,'')) IN ('operational','live','active')",
                   cast=float, empty=0.0)
            # 2026-07-31: was the unfiltered SUM — 2,680.6 GW, 4.6x the
            # publishable 586.6 GW, because 725 of 1,973 rows are quarantined
            # (utility interconnection QUEUES summed as single buildings). See
            # util/capacity_pipeline.
            st.num("pipeline_mw",
                   f"SELECT COALESCE(SUM(capacity_mw), 0) FROM capacity_pipeline WHERE {CP_OK}",
                   cast=float, empty=0.0)

            # ── Energy / grid ──────────────────────────────────────
            st.num("states_with_rates",
                   "SELECT COUNT(DISTINCT state) FROM eia_retail_rates WHERE rate_cents_kwh > 0")
            st.num("isos_covered",
                   "SELECT COUNT(DISTINCT iso) FROM market_power_scores WHERE iso IS NOT NULL AND iso != ''")

            # ── MCP / AI traffic (the "agents are using us" signal) ─
            # mcp_calls_7d is the GROSS count (includes our own QA probes).
            # mcp_calls_7d_real (Phase FF+25-followup-r3, 2026-05-20)
            # filters out the self-traffic platforms so the homepage tile
            # reflects external AI-agent demand only. CF WAF over-blocking
            # of our probes May 17-19 dragged the gross count from 38k→27k
            # while real external traffic was unchanged; we now ship both
            # so the public-facing number is robust against probe noise.
            st.num("mcp_calls_7d",
                   "SELECT COUNT(*) FROM mcp_tool_calls WHERE created_at > NOW() - INTERVAL '7 days'")
            # r86f: HONEST external traffic. The old "exclude node/curl/python UA"
            # denylist read 0 because the MCP proxy (server.mjs) never forwards the
            # caller's UA — 88% of rows carry user_agent='node', so the denylist
            # stripped ~all real traffic. And the gross count is dominated by
            # INTERNAL self-traffic (platform='dchub-selfheal' alone ~33k/wk).
            # Define "real external" by EXCLUDING internal PLATFORMS (selfheal /
            # tests / probes / sweeps); this also correctly drops the ~29k
            # 'unknown'-client rows, which are self-heal under the hood. Until
            # server.mjs forwards the real client (then per-caller attribution
            # works fully), this is the honest reach number.
            _ext = (
                " AND COALESCE(LOWER(platform),'') NOT LIKE 'dchub-%'"
                " AND COALESCE(LOWER(platform),'') NOT LIKE '%-probe'"
                " AND COALESCE(LOWER(platform),'') NOT LIKE '%-test'"
                " AND COALESCE(LOWER(platform),'') NOT LIKE 'sweep%'"
                " AND COALESCE(LOWER(platform),'') NOT LIKE 'loop%'"
                " AND COALESCE(LOWER(platform),'') NOT IN ('dchub-selfheal','mcp-probe','diag','')"
            )
            st.num("mcp_calls_7d_real",
                   "SELECT COUNT(*) FROM mcp_tool_calls"
                   " WHERE created_at > NOW() - INTERVAL '7 days'" + _ext)
            # distinct EXTERNAL callers by client identity (NOT ip_address —
            # that's the proxy's egress IP, so it collapsed to ~4 proxy IPs).
            st.num("mcp_unique_callers_7d",
                   "SELECT COUNT(DISTINCT COALESCE(NULLIF(LOWER(client_name),'unknown'), platform))"
                   " FROM mcp_tool_calls WHERE created_at > NOW() - INTERVAL '7 days'" + _ext)
            # mcp_developers = LIFETIME registered dev keys (every key ever
            # minted, incl. revoked/expired) — the public social-proof number,
            # intentionally cumulative, NOT "active keys". r-canonical-funnel
            # (2026-06-27): keep it unchanged (don't lower a public trust number),
            # but ALSO surface the canonical ACTIVE count so this surface stops
            # being mis-read as a disagreeing "active keys" figure vs
            # /admin/funnel-health (the cross_surface_metric_divergence finding).
            st.num("mcp_developers", "SELECT COUNT(*) FROM mcp_dev_keys")
            try:
                from canonical_funnel import get_canonical_funnel as _cfunnel
                s["mcp_active_dev_keys"] = int(_cfunnel().get("active_dev_keys", 0) or 0)
            except Exception:
                pass

            # ── Trust signals ──────────────────────────────────────
            st.num("testimonials",
                   "SELECT COUNT(*) FROM ai_testimonials WHERE approved = true")
            st.num("press_releases",
                   "SELECT COUNT(*) FROM press_releases WHERE published = true")

            # ── Freshness (when did our biggest tables last update) ─
            # `discovered_at` is TEXT, so MAX() is a lexicographic max — fine
            # for an ISO-8601 column, and it is what has always been served.
            st.raw("facilities_last_updated",
                   "SELECT MAX(discovered_at) FROM discovered_facilities")
            st.raw("dcpi_last_updated",
                   "SELECT MAX(computed_at) FROM market_power_scores")
            s["facilities_last_updated"] = _to_iso(s["facilities_last_updated"])
            s["dcpi_last_updated"] = _to_iso(s["dcpi_last_updated"])

            # ── New this week — for the "DC Hub is growing" narrative ─
            # ★ Was `discovered_at > NOW() - INTERVAL '7 days'`. `discovered_at`
            # is TEXT (`first_seen` is the timestamptz), so Postgres raised
            # `operator does not exist: text > timestamp with time zone`, the
            # old _scalar default swallowed it, and this tile published 0 —
            # the honest figure on 2026-08-01 was 1,258. Same TEXT-timestamp
            # trap ai_cumulative is documented for.
            st.num("new_facilities_7d",
                   "SELECT COUNT(*) FROM discovered_facilities WHERE first_seen > NOW() - INTERVAL '7 days'")
            st.num("new_mcp_devs_7d",
                   "SELECT COUNT(*) FROM mcp_dev_keys WHERE created_at > NOW() - INTERVAL '7 days'")

            # ── Grid pulse — per-ISO snapshot for the hero widget ──
            # Returns up to 7 ISOs with their market footprint + avg
            # DCPI excess/constraint. The hero shows the top 3-4.
            pulse, pulse_err = _grid_pulse(cur)
            s["grid_pulse"] = pulse
            if pulse_err:
                st.errors["grid_pulse"] = pulse_err
    finally:
        try: conn.close()
        except Exception: pass

    payload = {"ok": True, "stats": s, "as_of": _now()}
    # A consumer must be able to SEE that the response is partial without
    # diffing it against a healthy one.
    payload["stats_complete"] = not st.errors
    if st.errors:
        payload["stat_errors"] = st.errors
    return payload


def _grid_pulse(cur) -> tuple:
    """Per-ISO grid snapshot. Powers the homepage Grid Pulse widget.

    Returns (rows, error). `[]` used to mean both "no ISOs scored" and "the
    query blew up"; the caller published the first reading either way.
    """
    try:
        cur.execute("""
            SELECT iso,
                   COUNT(DISTINCT market_slug) AS markets,
                   ROUND(AVG(excess_power_score)::numeric, 1) AS avg_excess,
                   ROUND(AVG(constraint_score)::numeric, 1)   AS avg_constraint,
                   COUNT(DISTINCT CASE WHEN verdict = 'BUILD' THEN market_slug END) AS build_count
              FROM (
                SELECT DISTINCT ON (market_slug)
                       iso, market_slug, excess_power_score,
                       constraint_score, verdict
                  FROM market_power_scores
                 WHERE published = true AND iso IS NOT NULL AND iso != ''
                 ORDER BY market_slug, computed_at DESC
              ) latest
             GROUP BY iso
             ORDER BY markets DESC
             LIMIT 7
        """)
        return ([
            {"iso": r[0],
             "markets": int(r[1] or 0),
             "avg_excess": float(r[2] or 0),
             "avg_constraint": float(r[3] or 0),
             "build_count": int(r[4] or 0)}
            for r in cur.fetchall()
        ], None)
    except Exception as e:
        try: cur.connection.rollback()
        except Exception: pass
        return (None, f"{type(e).__name__}: {str(e).splitlines()[0][:160]}")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _to_iso(val) -> str | None:
    if val is None: return None
    try: return val.isoformat()
    except Exception: return str(val)


@site_stats_bp.get("/api/v1/site/stats")
def site_stats():
    """Live site stats for the homepage hero + /intelligence hub.

    Cached at the edge (60s public) AND in-process (60s) so that even
    a thundering herd at deploy time can't take the DB down. Frontend
    can poll every 30s without amplifying load."""
    now = time.time()
    if _CACHE["payload"] and (now - _CACHE["ts"]) < _CACHE_TTL:
        body = _CACHE["payload"]
        cached = True
    else:
        body = _build_stats()
        _CACHE["payload"] = body
        _CACHE["ts"] = now
        cached = False

    from flask import make_response
    resp = make_response(jsonify(body))
    # 60s public cache + 30s stale-while-revalidate so first paint never
    # waits on the DB even when the cache misses.
    resp.headers["Cache-Control"] = "public, max-age=60, stale-while-revalidate=30"
    resp.headers["X-Stats-Cache"] = "hit" if cached else "miss"
    return resp
