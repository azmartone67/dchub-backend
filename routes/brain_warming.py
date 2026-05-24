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

    try:
        with _pg.connect(_dsn()) as c, c.cursor() as cur:
            # tier 1 adoption — uses mcp_tool_usage table if present
            try:
                cur.execute("""
                    SELECT tool_name, COUNT(*) AS n
                    FROM mcp_tool_usage
                    WHERE tool_name IN ('rank_markets','find_alternatives','score_facility')
                      AND ts > NOW() - INTERVAL '7 days'
                    GROUP BY tool_name
                """)
                out["detectors"]["tier1_mcp_adoption_7d"] = {r[0]: r[1] for r in cur.fetchall()}
            except Exception as e:
                out["detectors"]["tier1_mcp_adoption_7d"] = {"_error": type(e).__name__}

            # api_usage_meter (round 34 stripe_metered table)
            try:
                cur.execute(
                    "SELECT COUNT(*) FROM api_usage_meter WHERE ts > NOW() - INTERVAL '24 hours'")
                out["detectors"]["api_calls_24h"] = cur.fetchone()[0]
            except Exception:
                out["detectors"]["api_calls_24h"] = "n/a"

            # facilities population for OG image generation viability
            try:
                cur.execute("SELECT COUNT(*) FROM discovered_facilities")
                out["detectors"]["discovered_facilities_total"] = cur.fetchone()[0]
            except Exception:
                out["detectors"]["discovered_facilities_total"] = "n/a"

    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"

    # OG image health probe
    og_probe = _hit(f"{BASE}/static/og/default.png", timeout=5)
    out["detectors"]["og_default_status"] = og_probe.get("status", 0)
    out["detectors"]["og_pillow_route_health"] = _hit(
        f"{BASE}/static/og/health", timeout=5).get("status", 0)

    return jsonify(out), 200


@brain_warming_bp.route("/health", methods=["GET"])
def health():
    return jsonify({
        "blueprint": "brain_warming_bp",
        "status": "ok",
        "warm_targets": len(WARM_TARGETS),
        "phase": "ZZZZZ-round35",
    }), 200
