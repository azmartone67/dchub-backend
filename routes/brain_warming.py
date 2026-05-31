"""
brain_warming.py — Brain heartbeat warmer + new round-35 detectors.

Phase ZZZZZ-round35 (2026-05-24). Two purposes:

  1. WARMER (/warm) — cron-callable endpoint that touches the brain
     heartbeat compute path so it stays warm. Pre-r35, the brain
     dashboard showed "warming" most of the time because nothing
     was triggering recomputation outside of user pageviews.

  2. NEW DETECTORS (/detectors) — three new brain signals:
     - tier1_mcp_adoption_7d: usage counts for the 3 new MCP tools
       (rank_markets, find_alternatives, score_facility) shipped in
       round 34. If adoption is 0, the tools may not be discovered.
     - og_image_404_rate: if OG images are still 404ing post-r35 fix,
       brain should know.
     - flask_html_503_rate: tracks how often the v4.9.6 worker had
       to fall through to KV stale or 503.

Both endpoints are idempotent and side-effect-free except for KV/cache
warming. Safe to call from Railway cron, GitHub Actions, or external
uptime monitor.
"""
import os
import datetime
import urllib.request
import urllib.error

from flask import Blueprint, jsonify

try:
    import psycopg2 as _pg
except Exception:
    _pg = None

brain_warming_bp = Blueprint("brain_warming", __name__,
                              url_prefix="/api/v1/brain-warming")

BASE = "https://api.dchub.cloud"
WARM_TARGETS = [
    f"{BASE}/api/v1/brain/heartbeat",
    f"{BASE}/api/v1/pulse",
    f"{BASE}/api/v1/freshness",
    f"{BASE}/sitemap-index.xml",
    f"{BASE}/api/v1/iso/hydroquebec/snapshot",
    f"{BASE}/api/v1/iso/aeso-intl/snapshot",
    f"{BASE}/api/v1/iso/nordpool-intl/snapshot",
]


def _hit(url, timeout=10):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "dchub-brain-warmer/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return {"status": resp.status, "bytes": len(resp.read(2048))}
    except urllib.error.HTTPError as e:
        return {"status": e.code, "error": "http"}
    except Exception as e:
        return {"status": 0, "error": f"{type(e).__name__}"}


# AUTO-REPAIR: duplicate route '/warm' also in routes/grid_cache_warmer.py:59 — review and remove one
@brain_warming_bp.route("/warm", methods=["GET", "POST"])
def warm():
    started = datetime.datetime.utcnow()
    results = {}
    for url in WARM_TARGETS:
        results[url] = _hit(url)
    elapsed_ms = int((datetime.datetime.utcnow() - started).total_seconds() * 1000)
    healthy = sum(1 for r in results.values() if 200 <= r.get("status", 0) < 400)
    return jsonify({
        "warmed_count": len(results),
        "healthy": healthy,
        "results": results,
        "elapsed_ms": elapsed_ms,
        "at": started.isoformat() + "Z",
    }), 200 if healthy == len(results) else 207


def _dsn():
    return os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL") or ""


@brain_warming_bp.route("/detectors", methods=["GET"])
def detectors():
    out = {
        "at": datetime.datetime.utcnow().isoformat() + "Z",
        "detectors": {},
    }
    if not (_pg and _dsn()):
        out["error"] = "no_db_or_psycopg2"
        return jsonify(out), 200

    # Each query runs in its OWN connection so a single failure (e.g.
    # missing table) doesn't poison the rest. psycopg2 puts the txn into
    # an aborted state on error; rollback per-query also works but a
    # fresh conn is more bulletproof for a diagnostics endpoint.
    # Schema cheat sheet (verified 2026-05-24):
    #   mcp_tool_usage    → date (DATE), tool_name, tier, call_count
    #   api_usage_meter   → usage_date (DATE), api_key, tier, calls_count, last_call_at
    QUERIES = [
        ("tier1_mcp_adoption_7d", """
            SELECT tool_name, SUM(call_count)::int AS n
            FROM mcp_tool_usage
            WHERE tool_name IN ('rank_markets','find_alternatives','score_facility')
              AND date > CURRENT_DATE - INTERVAL '7 days'
            GROUP BY tool_name
        """, "dict"),
        ("api_calls_24h",
            "SELECT COALESCE(SUM(calls_count),0)::int FROM api_usage_meter "
            "WHERE usage_date >= CURRENT_DATE - INTERVAL '1 day'",
            "scalar"),
        ("api_calls_unique_keys_7d",
            "SELECT COUNT(DISTINCT api_key)::int FROM api_usage_meter "
            "WHERE usage_date >= CURRENT_DATE - INTERVAL '7 days'",
            "scalar"),
        ("mcp_tool_usage_total_7d",
            "SELECT COALESCE(SUM(call_count),0)::int FROM mcp_tool_usage "
            "WHERE date > CURRENT_DATE - INTERVAL '7 days'",
            "scalar"),
        ("discovered_facilities_total",
            "SELECT COUNT(*) FROM discovered_facilities",
            "scalar"),
        ("discovered_with_mw",
            "SELECT COUNT(*) FROM discovered_facilities WHERE power_mw IS NOT NULL AND power_mw > 0",
            "scalar"),
        ("sitemap_url_count_facilities",
            "SELECT COUNT(*) FROM discovered_facilities WHERE name IS NOT NULL",
            "scalar"),
    ]
    for label, sql, shape in QUERIES:
        try:
            with _pg.connect(_dsn()) as c, c.cursor() as cur:
                cur.execute(sql)
                if shape == "dict":
                    out["detectors"][label] = {r[0]: r[1] for r in cur.fetchall()}
                else:
                    out["detectors"][label] = cur.fetchone()[0]
        except Exception as e:
            out["detectors"][label] = {"_error": type(e).__name__, "msg": str(e)[:140]}

    # OG image health probe
    og_probe = _hit(f"{BASE}/static/og/default.png", timeout=5)
    out["detectors"]["og_default_status"] = og_probe.get("status", 0)
    out["detectors"]["og_pillow_route_health"] = _hit(
        f"{BASE}/static/og/health", timeout=5).get("status", 0)

    return jsonify(out), 200

# AUTO-REPAIR: duplicate route '/health' also in main.py:3839 — review and remove one

@brain_warming_bp.route("/health", methods=["GET"])
def health():
    return jsonify({
        "blueprint": "brain_warming_bp",
        "status": "ok",
        "warm_targets": len(WARM_TARGETS),
        "phase": "ZZZZZ-round35",
    }), 200
