"""
openapi_dynamic.py — dynamic OpenAPI spec with live facility counts.

Phase ZZZZZ-round36 (2026-05-24). Pre-r36 openapi.json claimed "13,000+
facilities" against a live count that had long since moved. This module
resolves the counts at request time so the spec stays honest.

Static elements (paths, schemas) are inlined; the dynamic counts come from
``canonical_stats`` — see _get_counts for why NOT from a row count.
"""
import datetime

from flask import Blueprint, jsonify

openapi_dynamic_bp = Blueprint("openapi_dynamic", __name__)


_COUNT_CACHE = {"counts": None, "ts": 0}
import time


def _get_counts():
    """Live, CITEABLE counts for the public spec.

    ★2026-08-23 — this used to read ``pg_class.reltuples`` for both numbers and
    publish the results as "N facilities" and "N M&A deals". Both are ROW piles,
    not the things they were labelled:

      • ``discovered_facilities`` rows are ~1.4x the building count (the March
        2026 backfill wrote several rows per site) — 26,388 rows against 18,656
        distinct buildings, published live as "26,388 facilities". That is the
        rows_ne_buildings class routes/claim_breaker.py refuses in media copy
        (#3111); the OpenAPI spec is the same claim to a machine reader.
      • ``deals`` rows over-state ~2.9x — the AUTO deal id embeds the ingest
        date, so one deal accrues a row per day. 4,979 rows against 1,932
        distinct, published live as "4,979 M&A deals".

    Both now come from ``canonical_stats``: ``facilities_verified`` =
    COUNT(DISTINCT canonical_slug) WHERE COALESCE(is_duplicate,0)=0 (the same
    query ``media_fact_check_guard.check_facility_count_claims`` measures
    published copy against) and ``deals`` = the deduped distinct-deal count.
    ``get_canonical_stats()`` is itself cached and never raises.

    Unknown is ``None``, never a literal: an unreadable canon yields a
    count-free description and a JSON ``null``, which is visibly broken. A
    literal default here would be the only value anyone ever saw — the exact
    ``or 33`` shape that kept /by-the-numbers wrong for months.
    """
    # 5 min cache
    if _COUNT_CACHE["counts"] and (time.time() - _COUNT_CACHE["ts"]) < 300:
        return _COUNT_CACHE["counts"]
    counts = {"facilities": None, "deals": None, "isos": 7, "as_of": "unavailable"}
    try:
        import canonical_stats as _cs
        s = _cs.get_canonical_stats() or {}
        fac = s.get("facilities_verified")
        deals = s.get("deals")
        if isinstance(fac, (int, float)) and fac > 0:
            counts["facilities"] = int(fac)
        if isinstance(deals, (int, float)) and deals > 0:
            counts["deals"] = int(deals)
        if counts["facilities"] or counts["deals"]:
            counts["as_of"] = datetime.datetime.utcnow().isoformat() + "Z"
    except Exception:
        pass
    _COUNT_CACHE["counts"] = counts
    _COUNT_CACHE["ts"] = time.time()
    return counts


@openapi_dynamic_bp.route("/openapi-live.json", methods=["GET"])
def openapi_live():
    counts = _get_counts()
    # Pre-rendered so an unknown count drops the clause instead of raising.
    _fac_s = f"{counts['facilities']:,}" if counts.get("facilities") else ""
    _deal_s = f"{counts['deals']:,}" if counts.get("deals") else ""
    spec = {
        "openapi": "3.1.0",
        "info": {
            "title": "DC Hub REST API",
            "version": "2.1.2",
            "description": (
                "Live data center intelligence: "
                + (f"{_fac_s} facilities, " if _fac_s else "")
                + (f"{_deal_s} M&A deals, " if _deal_s else "")
                + "grid data across live grids on 5 continents — 7 US ISOs "
                  "(plus TVA, BPA, Ontario's IESO), UK, ~24 EU zones, Taiwan, "
                  "Japan, South Korea, Brazil, Australia and 43 US utility "
                  "balancing authorities, real-time grid mix, fiber routes, "
                  "water risk, tax incentives. "
                + f"Counts as of {counts['as_of']}."
            ),
            "contact": {"email": "api@dchub.cloud", "url": "https://dchub.cloud"},
            # ★2026-08-10 licence coherence — see DATA-LICENSE.md.
            "license": {"name": "CC-BY-4.0 for DCPI scores + methodology; other layers per DATA-LICENSE.md", "url": "https://dchub.cloud/data-sources"},
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
                "description": (f"Search {_fac_s} facilities by city/state/operator/MW."
                                if _fac_s else "Search facilities by city/state/operator/MW."),
                "responses": {"200": {"description": "OK"}}}},
            "/api/v1/facilities/{id}": {"get": {"summary": "Get facility by ID",
                "responses": {"200": {"description": "OK"}, "404": {"description": "not found"}}}},
            "/api/v1/deals": {"get": {"summary": (f"List {_deal_s} M&A transactions" if _deal_s
                                                   else "List M&A transactions"),
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
