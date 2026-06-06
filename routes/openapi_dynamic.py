"""
openapi_dynamic.py — dynamic OpenAPI spec with live facility counts.

Phase ZZZZZ-round36 (2026-05-24). Pre-r36 openapi.json claimed "13,000+
facilities" — real count is 21,401 (drift since the spec was hardcoded).
This module computes counts at request time so the spec stays honest.

Static elements (paths, schemas) are inlined; dynamic counts (facility
total, deals, ISOs, etc.) come from a single DB query per request.
"""
import os
import datetime
from contextlib import contextmanager

from flask import Blueprint, jsonify

try:
    import psycopg2 as _pg
except Exception:
    _pg = None

openapi_dynamic_bp = Blueprint("openapi_dynamic", __name__)


def _dsn():
    return os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL") or ""


@contextmanager
def _conn():
    c = _pg.connect(_dsn())
    try: yield c
    finally: c.close()


_COUNT_CACHE = {"counts": None, "ts": 0}
import time


def _get_counts():
    # 5 min cache
    if _COUNT_CACHE["counts"] and (time.time() - _COUNT_CACHE["ts"]) < 300:
        return _COUNT_CACHE["counts"]
    counts = {"facilities": 21000, "deals": 1900, "isos": 10, "as_of": "stale"}
    if _pg and _dsn():
        # r41-counts-speed (2026-05-25): use pg_class.reltuples (a cached
        # planner statistic) instead of SELECT COUNT(*). COUNT(*) on
        # discovered_facilities was 3-4s cold; reltuples is sub-ms.
        # Statistics are updated by ANALYZE / autovacuum so we get fresh-
        # enough counts (within hours). The 5-min in-process cache above
        # smooths any remaining variance. Fallback to COUNT(*) if
        # reltuples is unavailable (e.g., table just created, no ANALYZE
        # has run yet).
        try:
            with _conn() as c, c.cursor() as cur:
                cur.execute(
                    "SELECT reltuples::bigint FROM pg_class WHERE relname = %s",
                    ("discovered_facilities",))
                row = cur.fetchone()
                est = int(row[0]) if row and row[0] is not None else 0
                if est > 0:
                    counts["facilities"] = est
                else:
                    # Cold-stats fallback
                    cur.execute("SELECT COUNT(*) FROM discovered_facilities")
                    counts["facilities"] = cur.fetchone()[0]
                counts["as_of"] = datetime.datetime.utcnow().isoformat() + "Z"
        except Exception:
            pass
        # Deals — table name varies; try the common names in order.
        for table in ("dc_deals", "deals", "transactions", "ma_deals"):
            try:
                with _conn() as c, c.cursor() as cur:
                    cur.execute(
                        "SELECT reltuples::bigint FROM pg_class WHERE relname = %s",
                        (table,))
                    row = cur.fetchone()
                    est = int(row[0]) if row and row[0] is not None else 0
                    if est > 0:
                        counts["deals"] = est
                        counts["deals_table"] = table
                        break
                    # Cold-stats fallback
                    cur.execute(f"SELECT COUNT(*) FROM {table}")
                    counts["deals"] = cur.fetchone()[0]
                    counts["deals_table"] = table
                    break
            except Exception:
                continue
    _COUNT_CACHE["counts"] = counts
    _COUNT_CACHE["ts"] = time.time()
    return counts


@openapi_dynamic_bp.route("/openapi-live.json", methods=["GET"])
def openapi_live():
    counts = _get_counts()
    spec = {
        "openapi": "3.1.0",
        "info": {
            "title": "DC Hub REST API",
            "version": "2.1.2",
            "description": (
                f"Live data center intelligence: {counts['facilities']:,} facilities, "
                f"{counts['deals']:,} M&A deals, grid data across live grids on 4 continents — 7 US ISOs (plus TVA, BPA, Ontario's IESO), UK, ~12 EU zones, Taiwan, Australia and 43 US utility balancing authorities, "
                "real-time grid mix, fiber routes, water risk, tax incentives. "
                f"Counts as of {counts['as_of']}."
            ),
            "contact": {"email": "api@dchub.cloud", "url": "https://dchub.cloud"},
            "license": {"name": "Commercial — tier-based"},
        },
        "servers": [
            {"url": "https://api.dchub.cloud", "description": "Primary (Cloudflare + Railway)"},
            {"url": "https://dchub-backend-render.onrender.com", "description": "Failover (Render, read-only)"},
        ],
        "x-dc-hub": {
            "facility_count": counts["facilities"],
            "deal_count": counts["deals"],
            "iso_coverage": ["CAISO","PJM","ERCOT","MISO","NYISO","SPP","ISONE",
                              "HYDROQUEBEC","AESO","NORDPOOL"],
            "mcp_endpoint": "https://dchub.cloud/mcp",
            "discovery": {
                "llms.txt": "https://dchub.cloud/llms.txt",
                "llms-full.txt": "https://dchub.cloud/llms-full.txt",
                "robots.txt": "https://dchub.cloud/robots.txt",
                "AGENTS.md": "https://dchub.cloud/AGENTS.md",
                "agent.json": "https://api.dchub.cloud/.well-known/agent.json",
                "mcp.json": "https://api.dchub.cloud/.well-known/mcp.json",
                "sitemap": "https://api.dchub.cloud/sitemap-index.xml",
            },
            "freshness_proof": "https://dchub.cloud/freshness",
        },
        "paths": {
            "/api/v1/search/facilities": {"get": {"summary": "Search facilities",
                "description": f"Search {counts['facilities']:,} facilities by city/state/operator/MW.",
                "responses": {"200": {"description": "OK"}}}},
            "/api/v1/facilities/{id}": {"get": {"summary": "Get facility by ID",
                "responses": {"200": {"description": "OK"}, "404": {"description": "not found"}}}},
            "/api/v1/deals": {"get": {"summary": f"List {counts['deals']:,} M&A transactions",
                "responses": {"200": {"description": "OK"}}}},
            "/api/v1/grid/intelligence/{iso}": {"get": {"summary": "Per-ISO grid intelligence",
                "responses": {"200": {"description": "OK"}}}},
            "/api/v1/iso/hydroquebec/snapshot": {"get": {"summary": "Hydro-Quebec real-time grid snapshot",
                "responses": {"200": {"description": "OK"}}}},
            "/api/v1/ai-capacity-index": {"get": {"summary": "AI Compute Capacity Index — where 100MW can land in 30/60/90 days",
                "responses": {"200": {"description": "OK"}}}},
            "/api/v1/hyperscaler-deals": {"get": {"summary": "Hyperscaler deal tracker (Stargate, Oracle, CoreWeave, AMD-Taiwan, ...)",
                "responses": {"200": {"description": "OK"}}}},
            "/api/v1/mcp/tools/rank_markets": {"post": {"summary": "Top-N markets by criteria",
                "responses": {"200": {"description": "OK"}}}},
            "/api/v1/mcp/tools/find_alternatives": {"post": {"summary": "Find similar nearby facilities",
                "responses": {"200": {"description": "OK"}}}},
            "/api/v1/mcp/tools/score_facility": {"post": {"summary": "Independent 7-dim facility scoring",
                "responses": {"200": {"description": "OK"}}}},
            "/mcp": {"post": {"summary": "MCP streamable-http endpoint (24 tools)",
                "responses": {"200": {"description": "OK"}}}},
            "/api/v1/market-brief/{slug}": {"get": {
                "summary": "Single Market Brief — full 9-section JSON",
                "description": ("Live Market Brief for one slug "
                                "(northern-virginia, dallas, phoenix, …). "
                                "Anon/free get hero + KPIs + outlook teaser; "
                                "PRO+ unlocks Power & Grid, Pipeline, Operators, "
                                "M&A, Comps, Risk."),
                "parameters": [
                    {"name": "slug", "in": "path", "required": True,
                     "schema": {"type": "string"},
                     "example": "northern-virginia"},
                ],
                "responses": {"200": {"description": "Brief JSON"},
                              "404": {"description": "market_not_found"}}}},
            "/api/v1/market-brief/all": {"get": {
                "summary": "Bulk Market Briefs — every brief in one call (BI integration)",
                "description": ("Returns all briefs the caller's tier is "
                                "entitled to (anon/free=5 markets, PRO+=15, "
                                "ENTERPRISE=all ~232). Streamed when >50 briefs. "
                                "Paginated via ?limit & ?offset (default 50, "
                                "max 500 PRO+). 6h edge cache. Designed for "
                                "Tableau, Power BI, Hex, Snowflake."),
                "parameters": [
                    {"name": "limit", "in": "query",
                     "schema": {"type": "integer", "default": 50, "maximum": 500},
                     "description": "Page size. Default 50."},
                    {"name": "offset", "in": "query",
                     "schema": {"type": "integer", "default": 0},
                     "description": "Page offset."},
                ],
                "responses": {
                    "200": {"description": "Bulk JSON of briefs"},
                    "429": {"description": "Daily cap exceeded (anon=10/d, free=50/d)"},
                }}},
            "/api/v1/market-brief/diff": {"get": {
                "summary": "Incremental Market Briefs — only briefs changed since `since`",
                "description": ("Returns only briefs whose computed_at is "
                                "after the supplied timestamp. Use case: BI "
                                "tools that refresh every 6h and want to "
                                "skip unchanged briefs."),
                "parameters": [
                    {"name": "since", "in": "query", "required": False,
                     "schema": {"type": "string", "format": "date-time"},
                     "example": "2026-06-06T00:00:00Z",
                     "description": ("ISO 8601 timestamp. If missing/invalid, "
                                     "returns everything (same as /all).")},
                    {"name": "limit", "in": "query",
                     "schema": {"type": "integer", "default": 50, "maximum": 500}},
                    {"name": "offset", "in": "query",
                     "schema": {"type": "integer", "default": 0}},
                ],
                "responses": {"200": {"description": "Bulk JSON of changed briefs"},
                              "429": {"description": "Daily cap exceeded"}}}},
            "/api/v1/market-brief/all.csv": {"get": {
                "summary": "Bulk Market Briefs as CSV — Excel/Tableau download",
                "description": ("Same data + tier gating as /all but emitted "
                                "as CSV with a canonical column order "
                                "(market_slug, market_name, verdict, "
                                "composite_score, excess_power, …). "
                                "Streamed when >50 markets. "
                                "Content-Disposition: attachment."),
                "parameters": [
                    {"name": "limit", "in": "query",
                     "schema": {"type": "integer", "default": 50, "maximum": 500}},
                    {"name": "offset", "in": "query",
                     "schema": {"type": "integer", "default": 0}},
                ],
                "responses": {"200": {"description": "text/csv",
                                       "content": {"text/csv": {}}},
                              "429": {"description": "Daily cap exceeded"}}}},
        }
    }
    return jsonify(spec), 200, {"Cache-Control": "public, max-age=300, s-maxage=600"}


@openapi_dynamic_bp.route("/openapi-counts", methods=["GET"])
def openapi_counts():
    # r41-counts-speed (2026-05-25): explicit edge cache. /openapi-counts
    # was hit on every page render of the badges UI + by external
    # monitors, but lacked a Cache-Control header — so every request
    # went all the way through Flask middleware (~700ms) + DB lookup
    # even though the data only changes a few times per day. Now CF
    # caches for 5 min at the edge; cold path is the pg_class estimate
    # so even cache misses are sub-second.
    return jsonify(_get_counts()), 200, {
        "Cache-Control": "public, max-age=300, s-maxage=600, stale-while-revalidate=3600",
    }
