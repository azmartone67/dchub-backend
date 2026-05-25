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
        # Facilities — query independently so deals-table-name issue
        # doesn't poison the facility count.
        try:
            with _conn() as c, c.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM discovered_facilities")
                counts["facilities"] = cur.fetchone()[0]
                counts["as_of"] = datetime.datetime.utcnow().isoformat() + "Z"
        except Exception:
            pass
        # Deals — table name varies; try the common names in order.
        for table in ("dc_deals", "deals", "transactions", "ma_deals"):
            try:
                with _conn() as c, c.cursor() as cur:
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
                f"{counts['deals']:,} M&A deals, 10 ISOs (7 US + Hydro-Quebec + AESO + Nord Pool 15 zones), "
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
        }
    }
    return jsonify(spec), 200, {"Cache-Control": "public, max-age=300, s-maxage=600"}


@openapi_dynamic_bp.route("/openapi-counts", methods=["GET"])
def openapi_counts():
    return jsonify(_get_counts()), 200
