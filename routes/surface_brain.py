"""Phase EEE (2026-05-16) — Surface Brain.

The vision: every page/feature on dchub.cloud is its own organism with
its own intelligence loop. /markets learns which markets are explored.
/land-power learns which sites are analyzed, which layers get toggled.
/map learns what regions users zoom into. Each surface tracks usage,
infers demand-gaps, computes growth, and reports to the central brain.

Generic framework — adding a new surface is one register_surface()
call + a frontend beacon. Each surface inherits:

  - Per-surface telemetry table: surface_telemetry (event_type, params,
    outcome, surface_id, anon_id, ts)
  - GET /api/v1/surface/<id>/pulse          — health + recent activity
  - GET /api/v1/surface/<id>/demand-gaps    — what users wanted but failed
  - GET /api/v1/surface/<id>/growth         — DAU/MAU/wow trend
  - GET /api/v1/surfaces                     — registry of all surfaces
  - POST /api/v1/surface/track               — beacon ingest (public,
                                                throttled per-IP)

Central brain heartbeat aggregates per-surface pulse so the operator
sees one number ("verdict: alive — 6 surfaces healthy, 1 declining").

Together with mcp_growth + media_pulse, every surface gets its own
self-improvement loop without each one re-inventing the infrastructure.
"""

from __future__ import annotations

import os
import json
import datetime
from typing import Any, Optional
from flask import Blueprint, jsonify, request
import psycopg2
import psycopg2.extras


surface_brain_bp = Blueprint("surface_brain", __name__)


def _conn():
    db = os.environ.get("DATABASE_URL")
    if not db: return None
    try:
        c = psycopg2.connect(db, sslmode="require", connect_timeout=5)
        c.autocommit = True
        return c
    except Exception:
        return None


# ── Schema ────────────────────────────────────────────────────────────
_SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS surface_telemetry (
    id              BIGSERIAL PRIMARY KEY,
    surface_id      TEXT NOT NULL,
    event_type      TEXT NOT NULL,
    event_target    TEXT,            -- e.g. market_slug, lat-lon, layer name
    outcome         TEXT,            -- 'ok' | 'fail' | 'not_found' | ...
    params          JSONB,
    anon_id         TEXT,            -- short hash of ip+ua (NOT stored as PII)
    referrer        TEXT,
    user_agent      TEXT,
    ip_hash         TEXT,            -- sha256 prefix
    ts              TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_surface_telemetry_surface_ts
    ON surface_telemetry(surface_id, ts DESC);
CREATE INDEX IF NOT EXISTS ix_surface_telemetry_event_outcome
    ON surface_telemetry(surface_id, event_type, outcome);
"""

def _ensure_schema():
    c = _conn()
    if c is None: return False
    try:
        with c.cursor() as cur:
            cur.execute(_SCHEMA_DDL)
        return True
    except Exception as e:
        print(f"[surface_brain] schema: {e}")
        return False
    finally:
        try: c.close()
        except Exception: pass

try: _ensure_schema()
except Exception: pass


# ── Surface registry ──────────────────────────────────────────────────
class Surface:
    """A surface = a page/feature that has its own brain.

    Registered once at module-import time via register_surface().
    Each instance can compute pulse / demand-gaps / growth from the
    shared surface_telemetry table. No per-surface DB infrastructure.
    """
    def __init__(self, surface_id: str, name: str, description: str,
                  routes: list[str] | None = None,
                  paid_tools: list[str] | None = None,
                  expected_event_types: list[str] | None = None):
        self.surface_id = surface_id
        self.name = name
        self.description = description
        self.routes = routes or []
        self.paid_tools = paid_tools or []           # MCP tools that bind to this surface
        self.expected_event_types = expected_event_types or []

    def to_dict(self) -> dict:
        return {
            "surface_id":  self.surface_id,
            "name":        self.name,
            "description": self.description,
            "routes":      self.routes,
            "paid_tools":  self.paid_tools,
            "expected_event_types": self.expected_event_types,
        }

    def pulse(self) -> dict:
        """Computed vital signs for this surface — events 24h/7d, unique
        anonymous users, top-explored entities, success rate."""
        c = _conn()
        if c is None: return {"surface_id": self.surface_id, "error": "no_database"}
        try:
            with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                out: dict = {
                    "surface_id":     self.surface_id,
                    "name":            self.name,
                    "events_24h":      0,
                    "events_7d":       0,
                    "unique_anon_24h": 0,
                    "unique_anon_7d":  0,
                    "success_rate_pct": None,
                    "top_targets_7d":  [],
                    "event_mix_7d":    [],
                }
                # Volume
                cur.execute("""
                    SELECT
                      COUNT(*) FILTER (WHERE ts >= NOW() - INTERVAL '24 hours')  AS e24,
                      COUNT(*) FILTER (WHERE ts >= NOW() - INTERVAL '7 days')    AS e7,
                      COUNT(DISTINCT anon_id) FILTER (WHERE ts >= NOW() - INTERVAL '24 hours' AND anon_id IS NOT NULL) AS u24,
                      COUNT(DISTINCT anon_id) FILTER (WHERE ts >= NOW() - INTERVAL '7 days' AND anon_id IS NOT NULL)   AS u7
                      FROM surface_telemetry
                     WHERE surface_id = %s
                """, (self.surface_id,))
                r = cur.fetchone() or {}
                out["events_24h"]      = int(r.get("e24") or 0)
                out["events_7d"]       = int(r.get("e7") or 0)
                out["unique_anon_24h"] = int(r.get("u24") or 0)
                out["unique_anon_7d"]  = int(r.get("u7") or 0)

                # Success rate
                cur.execute("""
                    SELECT
                      COUNT(*) AS total,
                      COUNT(*) FILTER (WHERE outcome IN ('ok','success','200'))  AS ok_n
                      FROM surface_telemetry
                     WHERE surface_id = %s
                       AND ts >= NOW() - INTERVAL '7 days'
                       AND outcome IS NOT NULL
                """, (self.surface_id,))
                r = cur.fetchone() or {}
                t = int(r.get("total") or 0)
                if t > 0:
                    out["success_rate_pct"] = round(100.0 * int(r.get("ok_n") or 0) / t, 1)

                # Top targets (entity-specific aggregation)
                cur.execute("""
                    SELECT event_target, COUNT(*) AS n
                      FROM surface_telemetry
                     WHERE surface_id = %s
                       AND ts >= NOW() - INTERVAL '7 days'
                       AND event_target IS NOT NULL
                     GROUP BY event_target
                     ORDER BY n DESC LIMIT 10
                """, (self.surface_id,))
                out["top_targets_7d"] = [
                    {"target": r["event_target"][:80], "events": int(r["n"])}
                    for r in cur.fetchall()
                ]

                # Event mix
                cur.execute("""
                    SELECT event_type, COUNT(*) AS n
                      FROM surface_telemetry
                     WHERE surface_id = %s
                       AND ts >= NOW() - INTERVAL '7 days'
                     GROUP BY event_type
                     ORDER BY n DESC LIMIT 10
                """, (self.surface_id,))
                out["event_mix_7d"] = [
                    {"type": r["event_type"], "events": int(r["n"])}
                    for r in cur.fetchall()
                ]
            return out
        finally:
            try: c.close()
            except Exception: pass

    def demand_gaps(self) -> dict:
        """What users tried that failed. Events with outcome=fail|not_found
        ranked by volume — the surface's 'what should we build next'."""
        c = _conn()
        if c is None: return {"surface_id": self.surface_id, "gaps": []}
        try:
            with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("""
                    SELECT event_type, event_target, outcome, COUNT(*) AS n
                      FROM surface_telemetry
                     WHERE surface_id = %s
                       AND ts >= NOW() - INTERVAL '14 days'
                       AND outcome IN ('fail','not_found','error','404','timeout')
                     GROUP BY event_type, event_target, outcome
                     ORDER BY n DESC LIMIT 15
                """, (self.surface_id,))
                gaps = [
                    {"event": r["event_type"], "target": (r["event_target"] or "")[:80],
                     "outcome": r["outcome"], "count": int(r["n"])}
                    for r in cur.fetchall()
                ]
            return {"surface_id": self.surface_id, "gaps": gaps, "window_days": 14}
        finally:
            try: c.close()
            except Exception: pass

    def growth(self) -> dict:
        """WoW + day-by-day growth trend for this surface."""
        c = _conn()
        if c is None: return {"surface_id": self.surface_id}
        try:
            with c.cursor() as cur:
                # WoW
                cur.execute("""
                    SELECT
                      COUNT(*) FILTER (WHERE ts >= NOW() - INTERVAL '7 days')  AS now_7d,
                      COUNT(*) FILTER (WHERE ts >= NOW() - INTERVAL '14 days' AND ts < NOW() - INTERVAL '7 days') AS prev_7d
                      FROM surface_telemetry
                     WHERE surface_id = %s
                """, (self.surface_id,))
                r = cur.fetchone()
                now_7d = int(r[0] or 0) if r else 0
                prev_7d = int(r[1] or 0) if r else 0
                pct = None
                if prev_7d > 0:
                    pct = round(100.0 * (now_7d - prev_7d) / prev_7d, 1)
                # Day-by-day for last 14 days
                cur.execute("""
                    SELECT date_trunc('day', ts) AS day, COUNT(*) AS n
                      FROM surface_telemetry
                     WHERE surface_id = %s
                       AND ts >= NOW() - INTERVAL '14 days'
                     GROUP BY day ORDER BY day
                """, (self.surface_id,))
                daily = [{"day": r[0].date().isoformat(), "events": int(r[1])}
                         for r in cur.fetchall()]
            return {
                "surface_id":  self.surface_id,
                "events_7d":   now_7d,
                "events_prev_7d": prev_7d,
                "wow_pct":     pct,
                "daily_14d":   daily,
            }
        finally:
            try: c.close()
            except Exception: pass

    def health_score(self) -> int:
        """0-100 single-number health. Combines volume, success rate, and
        WoW growth. Used by central brain heartbeat for at-a-glance status."""
        score = 50
        p = self.pulse()
        g = self.growth()
        # Volume contributes 25 pts
        e7 = p.get("events_7d") or 0
        if   e7 >= 1000: score += 25
        elif e7 >= 100:  score += 15
        elif e7 >= 10:   score += 5
        elif e7 == 0:    score -= 15
        # Success rate contributes 25 pts
        sr = p.get("success_rate_pct")
        if sr is not None:
            if   sr >= 95: score += 25
            elif sr >= 85: score += 15
            elif sr >= 70: score += 5
            elif sr <  50: score -= 15
        # Growth direction contributes 25 pts (capped — sign matters more than magnitude)
        wow = g.get("wow_pct")
        if wow is not None:
            if   wow >= 20: score += 25
            elif wow >= 5:  score += 15
            elif wow >= -5: score += 5
            elif wow <  -25: score -= 15
        return max(0, min(100, score))


# Global registry
SURFACES: dict[str, Surface] = {}

def register_surface(surface: Surface) -> None:
    SURFACES[surface.surface_id] = surface


# ── Telemetry ingest ──────────────────────────────────────────────────
import hashlib as _hashlib

def _anon_id(ip: str, ua: str) -> str:
    """Short stable hash so repeat callers count as one anon user, but
    PII (raw IP) is never stored."""
    return _hashlib.sha256(((ip or "") + "|" + (ua or "")).encode()).hexdigest()[:16]


def _ip_hash(ip: str) -> str:
    return _hashlib.sha256((ip or "").encode()).hexdigest()[:32]


# Very lightweight in-memory per-IP rate limit so the beacon endpoint
# can't be weaponized. 100 events/min/IP — generous for real users,
# kills basic spam.
_RL: dict[str, list[float]] = {}
_RL_WINDOW = 60.0
_RL_MAX = 100

def _rate_limited(ip_h: str) -> bool:
    import time
    now = time.time()
    bucket = _RL.setdefault(ip_h, [])
    bucket[:] = [t for t in bucket if (now - t) < _RL_WINDOW]
    if len(bucket) >= _RL_MAX:
        return True
    bucket.append(now)
    return False


@surface_brain_bp.route("/api/v1/surface/track", methods=["POST", "OPTIONS"])
def track_event():
    """Public beacon endpoint. Pages POST events here on load + key
    interactions. Body: {surface, event, target?, outcome?, params?}.
    Rate-limited per-IP. Fails silently to avoid disrupting page UX
    if the beacon mis-fires."""
    if request.method == "OPTIONS":
        resp = jsonify(ok=True)
        resp.headers["Access-Control-Allow-Origin"] = "*"
        resp.headers["Access-Control-Allow-Methods"] = "POST,OPTIONS"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
        return resp, 200

    body = request.get_json(silent=True) or {}
    surface_id = (body.get("surface") or "").strip()[:50]
    event_type = (body.get("event") or "").strip()[:80]
    if not surface_id or not event_type:
        # Silent fail — don't leak validation info to beacons
        return jsonify(ok=False), 200

    # Tolerate unknown surface_ids (allow new pages to start beaconing
    # before backend registration; later pulse calls will return empty).
    target = (body.get("target") or "")[:200] or None
    outcome = (body.get("outcome") or "")[:30] or None
    params = body.get("params")
    params_str = json.dumps(params, default=str)[:4000] if params else None

    raw_ip = (request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
              or request.remote_addr or "")
    ua = (request.headers.get("User-Agent") or "")[:300]
    ip_h = _ip_hash(raw_ip)
    anon = _anon_id(raw_ip, ua)
    referrer = (request.headers.get("Referer") or "")[:300] or None

    if _rate_limited(ip_h):
        return jsonify(ok=False, reason="rate_limited"), 200

    c = _conn()
    if c is None: return jsonify(ok=True), 200  # silent
    try:
        with c.cursor() as cur:
            cur.execute("""
                INSERT INTO surface_telemetry
                    (surface_id, event_type, event_target, outcome,
                     params, anon_id, referrer, user_agent, ip_hash)
                VALUES (%s,%s,%s,%s, %s::jsonb, %s,%s,%s,%s)
            """, (surface_id, event_type, target, outcome,
                  params_str, anon, referrer, ua, ip_h))
        return jsonify(ok=True), 200
    except Exception as e:
        # Don't crash the beacon — log + swallow
        print(f"[surface_brain] track insert failed: {e}")
        return jsonify(ok=True), 200
    finally:
        try: c.close()
        except Exception: pass


# Phase FFF (2026-05-16): in-process surfaces cache. list_surfaces
# computed health_score() per surface = 3 SQL queries × 5 surfaces = 15
# queries per request. Live timing 3.2s. With caching: <50ms.
import time as _time_surf
_SURFACES_CACHE = {"payload": None, "ts": 0.0}
_SURFACES_TTL_S = 120.0


# ── Surface APIs ──────────────────────────────────────────────────────
@surface_brain_bp.route("/api/v1/surfaces", methods=["GET"])
def list_surfaces():
    """Public — registry of all surfaces with their current health scores."""
    now = _time_surf.time()
    cached = _SURFACES_CACHE["payload"]
    if cached is not None and (now - _SURFACES_CACHE["ts"]) < _SURFACES_TTL_S:
        resp_data = dict(cached)
        resp_data["_cache_age_seconds"] = round(now - _SURFACES_CACHE["ts"], 1)
        resp_data["_cached"] = True
        resp = jsonify(resp_data)
        resp.headers["Cache-Control"] = "public, max-age=60"
        resp.headers["Access-Control-Allow-Origin"] = "*"
        return resp, 200

    out = []
    for sid, surface in sorted(SURFACES.items()):
        try:
            score = surface.health_score()
        except Exception:
            score = None
        out.append({
            **surface.to_dict(),
            "health_score": score,
        })
    payload = {
        "surfaces":  out,
        "count":     len(out),
        "average_health": round(sum((s["health_score"] or 0) for s in out) / max(1, len(out)), 1),
        "generated_at":  datetime.datetime.utcnow().isoformat() + "Z",
    }
    _SURFACES_CACHE["payload"] = payload
    _SURFACES_CACHE["ts"]      = now
    resp = jsonify(payload)
    resp.headers["Cache-Control"] = "public, max-age=60"
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp, 200


@surface_brain_bp.route("/api/v1/surface/<surface_id>/pulse", methods=["GET"])
def surface_pulse(surface_id):
    s = SURFACES.get(surface_id)
    if s is None:
        return jsonify(error="surface_not_registered",
                       hint=f"see /api/v1/surfaces for registered ids"), 404
    out = s.pulse()
    resp = jsonify(out)
    resp.headers["Cache-Control"] = "public, max-age=120"
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp, 200


@surface_brain_bp.route("/api/v1/surface/<surface_id>/demand-gaps", methods=["GET"])
def surface_demand_gaps(surface_id):
    s = SURFACES.get(surface_id)
    if s is None:
        return jsonify(error="surface_not_registered"), 404
    out = s.demand_gaps()
    resp = jsonify(out)
    resp.headers["Cache-Control"] = "public, max-age=300"
    return resp, 200


@surface_brain_bp.route("/api/v1/surface/<surface_id>/growth", methods=["GET"])
def surface_growth(surface_id):
    s = SURFACES.get(surface_id)
    if s is None:
        return jsonify(error="surface_not_registered"), 404
    out = s.growth()
    resp = jsonify(out)
    resp.headers["Cache-Control"] = "public, max-age=300"
    return resp, 200


# ── STARTER REGISTRATIONS ─────────────────────────────────────────────
# Phase EEE: 3 surfaces wired at framework launch. Adding more is one
# register_surface() call from anywhere in the app.

register_surface(Surface(
    surface_id="markets",
    name="Markets",
    description="Top-level /markets index + per-market /markets/<slug> pages",
    routes=["/markets", "/markets/<slug>"],
    paid_tools=["get_market_intel", "recommend_market", "compare_markets"],
    expected_event_types=["view", "search", "filter", "compare", "not_found"],
))

register_surface(Surface(
    surface_id="land_power",
    name="Land & Power",
    description="The /land-power interactive map — 40+ data layers for site selection",
    routes=["/land-power"],
    paid_tools=["find_power_site", "analyze_site", "get_grid_intelligence", "get_fiber_intel"],
    expected_event_types=["view", "analyze", "layer_toggle", "site_select", "export"],
))

register_surface(Surface(
    surface_id="map",
    name="Facility Map",
    description="The /map facility browser — 20K+ data centers worldwide",
    routes=["/map"],
    paid_tools=["search_facilities", "search_facilities_semantic", "get_facility"],
    expected_event_types=["view", "zoom", "search", "facility_click", "filter"],
))

register_surface(Surface(
    surface_id="dcpi",
    name="DCPI",
    description="The /dcpi Data Center Power Index — daily-refreshing scorecard",
    routes=["/dcpi", "/dcpi/<slug>"],
    paid_tools=["get_market_intel", "recommend_market", "explain_market_move"],
    expected_event_types=["view", "search", "filter", "export", "ask"],
))

register_surface(Surface(
    surface_id="ai_hub",
    name="AI Hub",
    description="The /ai agent-discovery landing page + /llms.txt + /.well-known/*",
    routes=["/ai", "/llms.txt", "/.well-known/mcp.json", "/mcp/tools"],
    paid_tools=[],
    expected_event_types=["view", "claim_key", "docs_click", "tool_catalog_view"],
))


# ── Auto-instrumentation helper ───────────────────────────────────────
# A small helper for backend routes to log their OWN surface views.
# Frontend pages use the beacon; backend-rendered pages can use this.
def auto_log(surface_id: str, event_type: str = "view",
             target: Optional[str] = None, outcome: str = "ok"):
    """Fire-and-forget log from a Flask handler. Safe to call from any
    route — DB errors are swallowed."""
    try:
        raw_ip = (request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
                  or request.remote_addr or "")
        ua = (request.headers.get("User-Agent") or "")[:300]
        ip_h = _ip_hash(raw_ip)
        if _rate_limited(ip_h):
            return
        c = _conn()
        if c is None: return
        try:
            with c.cursor() as cur:
                cur.execute("""
                    INSERT INTO surface_telemetry
                        (surface_id, event_type, event_target, outcome,
                         anon_id, user_agent, ip_hash)
                    VALUES (%s,%s,%s,%s,%s,%s,%s)
                """, (surface_id, event_type, target, outcome,
                      _anon_id(raw_ip, ua), ua, ip_h))
        finally:
            try: c.close()
            except Exception: pass
    except Exception:
        pass   # never crash a request from instrumentation
