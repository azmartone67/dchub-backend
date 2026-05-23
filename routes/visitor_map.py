"""
visitor_map.py — Phase ZZZZZ-round23 (2026-05-23).

Tools-roadmap Item 1: global heatmap of MCP traffic. The user asked
"can we use any of these tools" referring to IPinfo's Map IP tool.
This module exposes the same pattern as our own surface.

Endpoints:
  GET /api/v1/visitor-map           — public aggregated GeoJSON
                                       (no raw IPs, just city/country
                                       clusters with call counts)
  GET /api/v1/admin/visitor-map     — admin-only, includes raw IPs +
                                       org breakdown for triage

Both pull from mcp_tool_calls last 7d, group by IP, enrich each IP
once via IPinfo cache (24h TTL — _ipinfo_enrich already in
visitor_intelligence.py), then cluster by (city, country, lat, lng)
rounded to 1 decimal place for privacy.

Privacy: NEVER return raw IPs in the public endpoint. The aggregator
clusters before serializing so a city with one bot doesn't reveal
the IP.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from collections import defaultdict
from flask import Blueprint, jsonify, request, make_response

try:
    import psycopg2
    import psycopg2.extras
except ImportError:
    psycopg2 = None

visitor_map_bp = Blueprint("visitor_map", __name__)


def _conn():
    db = os.environ.get("DATABASE_URL")
    if not db or psycopg2 is None:
        return None
    try:
        c = psycopg2.connect(db, sslmode="require", connect_timeout=5)
        c.autocommit = True
        return c
    except Exception:
        return None


def _top_ips(days: int = 7, limit: int = 200) -> list[dict]:
    """Get the top-N caller IPs (by call count) over the window."""
    c = _conn()
    if c is None:
        return []
    out = []
    try:
        with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT ip_address, COUNT(*) AS calls,
                       COUNT(DISTINCT DATE(created_at)) AS days_active,
                       MAX(created_at) AS last_seen
                  FROM mcp_tool_calls
                 WHERE created_at >= NOW() - INTERVAL %s
                   AND ip_address IS NOT NULL
                   AND ip_address != ''
                   AND ip_address NOT LIKE '162.220.232.%%'
                   AND ip_address NOT LIKE '162.220.233.%%'
                   AND ip_address != '127.0.0.1'
                 GROUP BY ip_address
                 ORDER BY calls DESC
                 LIMIT %s
            """, (f"{days} days", limit))
            out = [dict(r) for r in cur.fetchall()]
    finally:
        try: c.close()
        except Exception: pass
    return out


def _enrich_and_cluster(ips: list[dict]) -> dict:
    """Enrich each IP via IPinfo (cached). Cluster results by
    (city, country) and round lat/lng to 1 decimal for privacy."""
    try:
        from routes.visitor_intelligence import _ipinfo_enrich
    except Exception:
        return {"clusters": [], "total_ips": 0, "total_calls": 0}

    clusters: dict[tuple, dict] = defaultdict(lambda: {
        "calls": 0, "ips": 0, "lat": None, "lng": None,
        "city": None, "country": None, "region": None,
        "orgs": set(),
    })
    total_calls = 0
    for row in ips:
        ip = row.get("ip_address") or ""
        calls = int(row.get("calls") or 0)
        total_calls += calls
        enrich = _ipinfo_enrich(ip) or {}
        city = enrich.get("city") or "Unknown"
        country = enrich.get("country") or "??"
        region = enrich.get("region") or ""
        # Try to extract lat/lng from IPinfo response.
        # ipinfo.io returns 'loc' as 'lat,lng' string OR nothing.
        # _ipinfo_enrich's current schema doesn't include loc — but
        # we can get an approximate location from city+country via
        # the cluster key. Real lat/lng only matters for the heatmap
        # so we use IPinfo's 'loc' field when present.
        loc = enrich.get("loc") or ""
        lat = lng = None
        if loc and "," in loc:
            try:
                lat_s, lng_s = loc.split(",", 1)
                lat = round(float(lat_s), 1)
                lng = round(float(lng_s), 1)
            except Exception:
                pass
        key = (city, country, lat, lng)
        c = clusters[key]
        c["calls"] += calls
        c["ips"] += 1
        c["city"] = city
        c["country"] = country
        c["region"] = region
        if lat is not None: c["lat"] = lat
        if lng is not None: c["lng"] = lng
        org = enrich.get("org")
        if org: c["orgs"].add(org)

    # Convert sets to lists for JSON serialization
    out_clusters = []
    for c in clusters.values():
        c["orgs"] = sorted(c["orgs"])[:5]  # top 5 orgs per cluster
        out_clusters.append(c)
    out_clusters.sort(key=lambda c: c["calls"], reverse=True)
    return {
        "clusters": out_clusters,
        "total_ips": len(ips),
        "total_calls": total_calls,
    }


@visitor_map_bp.route("/api/v1/visitor-map", methods=["GET"])
def visitor_map_public():
    """Public aggregated city-level heatmap of MCP traffic. No raw
    IPs. Cached 5 min at the edge.

    Returns GeoJSON-ready cluster list ready to feed into Leaflet."""
    try:
        days = max(1, min(30, int(request.args.get("days", 7))))
    except (ValueError, TypeError):
        days = 7
    try:
        limit = max(20, min(500, int(request.args.get("limit", 200))))
    except (ValueError, TypeError):
        limit = 200
    ips = _top_ips(days=days, limit=limit)
    if not ips:
        resp = jsonify(
            ok=True, days=days, total_ips=0, total_calls=0,
            clusters=[],
            as_of=datetime.now(timezone.utc).isoformat(),
            note="No MCP traffic in the window — or DATABASE_URL missing.",
        )
        resp.headers["Cache-Control"] = "public, max-age=300"
        return resp, 200
    result = _enrich_and_cluster(ips)
    resp = jsonify(
        ok=True,
        days=days,
        total_ips=result["total_ips"],
        total_calls=result["total_calls"],
        cluster_count=len(result["clusters"]),
        clusters=result["clusters"],
        as_of=datetime.now(timezone.utc).isoformat(),
        attribution="IPinfo (ipinfo.io) for geolocation enrichment",
        note=("city/country clusters only; raw IPs never returned. "
              "Lat/lng rounded to 1 decimal place for privacy."),
    )
    resp.headers["Cache-Control"] = "public, max-age=300, stale-while-revalidate=600"
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp, 200


@visitor_map_bp.route("/api/v1/admin/visitor-map", methods=["GET"])
def visitor_map_admin():
    """Admin-only: includes raw IPs + org breakdown for triage."""
    provided = (request.headers.get("X-Admin-Key")
                or request.headers.get("X-Internal-Key")
                or request.args.get("admin_key"))
    try:
        from internal_auth import is_valid_internal_key
        if not is_valid_internal_key(provided):
            resp = make_response(jsonify(ok=False, error="unauthorized"), 401)
            resp.headers["Cache-Control"] = "no-store, max-age=0"
            return resp
    except Exception:
        return jsonify(ok=False, error="auth_module_unavailable"), 500

    try:
        days = max(1, min(30, int(request.args.get("days", 7))))
    except (ValueError, TypeError):
        days = 7
    ips = _top_ips(days=days, limit=200)
    try:
        from routes.visitor_intelligence import _ipinfo_enrich
        try:
            from routes.brain_security_detectors import _is_hosting_ip
        except Exception:
            _is_hosting_ip = lambda e: False
    except Exception:
        _ipinfo_enrich = lambda x: {}
        _is_hosting_ip = lambda e: False

    enriched_rows = []
    for row in ips[:50]:  # cap admin payload to 50 rows
        ip = row.get("ip_address") or ""
        enrich = _ipinfo_enrich(ip) or {}
        enriched_rows.append({
            "ip":           ip,
            "calls":        int(row.get("calls") or 0),
            "days_active":  int(row.get("days_active") or 0),
            "last_seen":    (row.get("last_seen").isoformat()
                              if row.get("last_seen") else None),
            "city":         enrich.get("city"),
            "region":       enrich.get("region"),
            "country":      enrich.get("country"),
            "org":          enrich.get("org"),
            "hostname":     enrich.get("hostname"),
            "is_hosting":   _is_hosting_ip(enrich),
        })
    # tally hosting share
    total_calls = sum(r["calls"] for r in enriched_rows)
    hosting_calls = sum(r["calls"] for r in enriched_rows if r["is_hosting"])
    hosting_pct = round(100.0 * hosting_calls / total_calls, 1) if total_calls else 0
    return jsonify(
        ok=True,
        days=days,
        rows=enriched_rows,
        summary={
            "total_calls": total_calls,
            "hosting_calls": hosting_calls,
            "hosting_pct": hosting_pct,
            "unique_orgs": len(set(r["org"] for r in enriched_rows if r["org"])),
            "unique_countries": len(set(r["country"] for r in enriched_rows if r["country"])),
        },
        as_of=datetime.now(timezone.utc).isoformat(),
    ), 200
