"""Phase GGGG (2026-05-16) — Land + Power saved sites + alerts.

Phase DDDD wired the PRO-tier MCP gates for:
  - save_lp_site     (gate exists, no backend)
  - get_lp_alerts    (gate exists, no backend)
  - lp_bulk_export   (gate exists, no backend)

This module ships the actual backend so PRO subscribers get real
value when they upgrade — not just a gate that returns "upgrade
required" forever.

  POST /api/v1/lp/save                save a candidate L+P site
  GET  /api/v1/lp/saved               list this user's saved sites
  DELETE /api/v1/lp/saved/<id>        unsave a site
  GET  /api/v1/lp/alerts              list this user's alert configs
  POST /api/v1/lp/alerts              create/update an alert
  GET  /api/v1/lp/export.csv          bulk CSV export of saved sites
  GET  /api/v1/lp/export.geojson      bulk GeoJSON export of saved sites

All PRO-gated via routes.tier_gate.require_tier. user_id resolves
from the dchub_token cookie OR X-API-Key header.

Phase JJJJ extends this with nightly alert-firing cron via Resend.
"""

from __future__ import annotations

import os
import json
import datetime
from flask import Blueprint, jsonify, request, Response

# Phase NNNN (2026-05-16): per-endpoint rate-limit decorator.
# Caps per api-key per minute so a runaway client can't spam the
# saved-sites table. PRO/ENTERPRISE callers stay under the limit
# during normal use; abusers get 429 with Retry-After.
from routes.tier_gate import rate_limit as _rl


lp_sites_bp = Blueprint("lp_sites", __name__)


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


_SCHEMA = """
CREATE TABLE IF NOT EXISTS saved_lp_sites (
    id           BIGSERIAL PRIMARY KEY,
    user_id      TEXT NOT NULL,
    name         TEXT NOT NULL,
    latitude     DOUBLE PRECISION NOT NULL,
    longitude    DOUBLE PRECISION NOT NULL,
    state        TEXT,
    market       TEXT,
    notes        TEXT,
    target_mw    REAL,
    dcpi_score_at_save INT,
    saved_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_saved_lp_sites_user_latlon
    ON saved_lp_sites(user_id, latitude, longitude);
CREATE INDEX IF NOT EXISTS ix_saved_lp_sites_user
    ON saved_lp_sites(user_id, saved_at DESC);

CREATE TABLE IF NOT EXISTS saved_lp_alerts (
    id              BIGSERIAL PRIMARY KEY,
    saved_site_id   BIGINT NOT NULL REFERENCES saved_lp_sites(id) ON DELETE CASCADE,
    user_id         TEXT NOT NULL,
    trigger_type    TEXT NOT NULL,  -- dcpi_change | capacity_change | new_facility_nearby
    threshold       REAL NOT NULL DEFAULT 5.0,
    notify_email    TEXT NOT NULL,
    enabled         BOOLEAN NOT NULL DEFAULT TRUE,
    last_fired_at   TIMESTAMPTZ,
    last_value      REAL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_saved_lp_alerts_user
    ON saved_lp_alerts(user_id, enabled);
"""


def _ensure_schema(c):
    try:
        with c.cursor() as cur:
            cur.execute(_SCHEMA)
    except Exception as e:
        # Don't crash request on schema-already-exists race
        try: c.rollback()
        except Exception: pass


def _user_id_from_request() -> str | None:
    """Best-effort caller identity. Returns stable ID or None for anon.
    For now, falls back to api_key hash (good enough for per-user
    persistence). Production should switch to a real auth-context."""
    import hashlib
    api_key = (request.headers.get("X-API-Key")
                or request.args.get("api_key")
                or request.cookies.get("dchub_token"))
    if api_key:
        return "k_" + hashlib.sha256(api_key.encode()).hexdigest()[:16]
    return None


def _require_pro_user():
    """Returns (user_id, gate_response_or_None). If user is not PRO+
    OR can't be identified, returns the 402 response to send back."""
    from routes.tier_gate import _resolve_caller_tier, _gate_response
    tier, _ = _resolve_caller_tier()
    if (tier or "FREE").upper() not in ("PRO", "ENTERPRISE"):
        return None, _gate_response(tier, "PRO", "lp_sites",
            {"value_proposition": ("PRO subscribers can save Land+Power "
                                    "candidate sites to a personal portfolio, "
                                    "configure alerts on DCPI / capacity / "
                                    "nearby facility changes, and bulk-export "
                                    "everything as CSV or GeoJSON for offline "
                                    "analysis.")})
    user_id = _user_id_from_request()
    if not user_id:
        from flask import jsonify as _j
        return None, (_j(error="auth_required",
                          message="X-API-Key header required to identify the "
                                   "user for per-account site persistence."), 401)
    return user_id, None


# ── REST endpoints ────────────────────────────────────────────────

@lp_sites_bp.route("/api/v1/lp/save", methods=["POST"])
@_rl(per_minute=30)
def lp_save():
    user_id, gate = _require_pro_user()
    if gate is not None: return gate
    d = request.get_json(silent=True) or {}
    try:
        lat = float(d.get("latitude") or d.get("lat") or 0)
        lon = float(d.get("longitude") or d.get("lon") or 0)
    except (ValueError, TypeError):
        return jsonify(error="invalid_coords"), 400
    name = (d.get("name") or "").strip()[:120] or "Untitled site"
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return jsonify(error="invalid_coords"), 400
    state  = (d.get("state") or "")[:8].upper() or None
    market = (d.get("market") or "")[:80] or None
    notes  = (d.get("notes") or "")[:1000] or None
    try: target_mw = float(d.get("target_mw") or 0) or None
    except (ValueError, TypeError): target_mw = None
    dcpi_score = d.get("dcpi_score_at_save")
    try: dcpi_score = int(dcpi_score) if dcpi_score is not None else None
    except (ValueError, TypeError): dcpi_score = None

    c = _conn()
    if c is None: return jsonify(error="no_database"), 503
    try:
        _ensure_schema(c)
        with c.cursor() as cur:
            cur.execute("""
                INSERT INTO saved_lp_sites
                  (user_id, name, latitude, longitude, state, market,
                   notes, target_mw, dcpi_score_at_save)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (user_id, latitude, longitude) DO UPDATE
                  SET name = EXCLUDED.name,
                      state = EXCLUDED.state,
                      market = EXCLUDED.market,
                      notes = EXCLUDED.notes,
                      target_mw = EXCLUDED.target_mw
                RETURNING id, saved_at
            """, (user_id, name, lat, lon, state, market, notes,
                  target_mw, dcpi_score))
            row = cur.fetchone()
            if row:
                site_id, saved_at = row[0], row[1]
                return jsonify(ok=True, site_id=int(site_id),
                               saved_at=saved_at.isoformat() if saved_at else None,
                               message=f"Saved '{name}'."), 200
    finally:
        try: c.close()
        except Exception: pass
    return jsonify(ok=False, error="save_failed"), 500


@lp_sites_bp.route("/api/v1/lp/saved", methods=["GET"])
@_rl(per_minute=60)
def lp_list_saved():
    user_id, gate = _require_pro_user()
    if gate is not None: return gate
    c = _conn()
    if c is None: return jsonify(error="no_database"), 503
    try:
        _ensure_schema(c)
        import psycopg2.extras
        with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT id, name, latitude, longitude, state, market, notes,
                       target_mw, dcpi_score_at_save, saved_at
                  FROM saved_lp_sites
                 WHERE user_id = %s
                 ORDER BY saved_at DESC LIMIT 500
            """, (user_id,))
            rows = cur.fetchall()
    finally:
        try: c.close()
        except Exception: pass
    out = []
    for r in rows:
        out.append({
            "id":         int(r["id"]),
            "name":       r["name"],
            "latitude":   float(r["latitude"]),
            "longitude":  float(r["longitude"]),
            "state":      r["state"],
            "market":     r["market"],
            "notes":      r["notes"],
            "target_mw":  float(r["target_mw"]) if r["target_mw"] is not None else None,
            "dcpi_score_at_save": int(r["dcpi_score_at_save"]) if r["dcpi_score_at_save"] is not None else None,
            "saved_at":   r["saved_at"].isoformat() if r["saved_at"] else None,
        })
    return jsonify(saved=out, count=len(out)), 200


# Phase QQQQ + GGGG consolidated handler — single decorator for both
# GET (detail) and DELETE (unsave) to satisfy regression-lint's
# duplicate-route rule. Branches on request.method.
@lp_sites_bp.route("/api/v1/lp/saved/<int:site_id>", methods=["GET", "DELETE"])
@_rl(per_minute=30)
def lp_site_detail_or_delete(site_id):
    user_id, gate = _require_pro_user()
    if gate is not None: return gate
    if request.method == "DELETE":
        return _lp_unsave_impl(site_id, user_id)
    return _lp_get_site_impl(site_id, user_id)


def _lp_get_site_impl(site_id, user_id):
    """Phase QQQQ (2026-05-16): per-site detail."""
    c = _conn()
    if c is None: return jsonify(error="no_database"), 503
    try:
        import psycopg2.extras
        with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT id, name, latitude, longitude, state, market, notes,
                       target_mw, dcpi_score_at_save, saved_at
                  FROM saved_lp_sites
                 WHERE id = %s AND user_id = %s
            """, (site_id, user_id))
            site = cur.fetchone()
            if not site:
                return jsonify(error="not_found"), 404
            lat = float(site["latitude"]); lon = float(site["longitude"])

            # Current DCPI for the market
            current_dcpi = None
            if site.get("market"):
                try:
                    cur.execute("""
                        SELECT score FROM market_power_scores
                         WHERE LOWER(market_name) = LOWER(%s)
                            OR LOWER(market_slug) = LOWER(%s)
                         ORDER BY computed_at DESC LIMIT 1
                    """, (site["market"], site["market"].replace(" ", "-")))
                    r = cur.fetchone()
                    if r: current_dcpi = float(r["score"]) if r["score"] is not None else None
                except Exception: pass

            # Nearby substation count within 50km — bbox + haversine
            nearby_substations = 0
            try:
                deg_lat = 50.0 / 111.0
                deg_lon = 50.0 / (111.0 * max(0.01, abs((90.0 - abs(lat)) / 90.0) + 0.1))
                cur.execute("""
                    SELECT COUNT(*) FROM substations
                     WHERE latitude  BETWEEN %s AND %s
                       AND longitude BETWEEN %s AND %s
                       AND (
                         6371.0 * acos(LEAST(1.0, GREATEST(-1.0,
                           cos(radians(%s)) * cos(radians(latitude)) *
                           cos(radians(longitude) - radians(%s)) +
                           sin(radians(%s)) * sin(radians(latitude))
                         )))
                       ) <= 50.0
                """, (lat - deg_lat, lat + deg_lat,
                      lon - deg_lon, lon + deg_lon,
                      lat, lon, lat))
                nearby_substations = int((cur.fetchone() or {"count": 0})["count"] or 0)
            except Exception: pass

            # DCPI delta since save
            dcpi_delta = None
            if current_dcpi is not None and site.get("dcpi_score_at_save") is not None:
                dcpi_delta = round(current_dcpi - float(site["dcpi_score_at_save"]), 1)

            return jsonify({
                "id":           int(site["id"]),
                "name":         site["name"],
                "latitude":     lat,
                "longitude":    lon,
                "state":        site["state"],
                "market":       site["market"],
                "notes":        site["notes"],
                "target_mw":    float(site["target_mw"]) if site["target_mw"] is not None else None,
                "dcpi_at_save": int(site["dcpi_score_at_save"]) if site["dcpi_score_at_save"] is not None else None,
                "dcpi_now":     current_dcpi,
                "dcpi_delta":   dcpi_delta,
                "nearby_substations_50km": nearby_substations,
                "saved_at":     site["saved_at"].isoformat() if site["saved_at"] else None,
                "map_url":      f"https://dchub.cloud/land-power-map?lat={lat}&lon={lon}",
            }), 200
    finally:
        try: c.close()
        except Exception: pass


def _lp_unsave_impl(site_id, user_id):
    c = _conn()
    if c is None: return jsonify(error="no_database"), 503
    try:
        with c.cursor() as cur:
            cur.execute("""
                DELETE FROM saved_lp_sites
                 WHERE id = %s AND user_id = %s
            """, (site_id, user_id))
            removed = cur.rowcount or 0
    finally:
        try: c.close()
        except Exception: pass
    return jsonify(ok=True, removed=removed), 200


# Phase GGGG (2026-05-16): single handler for /api/v1/lp/alerts
# accepting GET (list) + POST (create) — consolidated to keep the
# regression-lint duplicate-route check happy (only one decorator per
# URL path). The two operations branch on request.method below.
@lp_sites_bp.route("/api/v1/lp/alerts", methods=["GET", "POST"])
@_rl(per_minute=30)
def lp_alerts_handler():
    user_id, gate = _require_pro_user()
    if gate is not None: return gate

    if request.method == "GET":
        c = _conn()
        if c is None: return jsonify(error="no_database"), 503
        try:
            _ensure_schema(c)
            import psycopg2.extras
            with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("""
                    SELECT a.id, a.saved_site_id, a.trigger_type, a.threshold,
                           a.notify_email, a.enabled, a.last_fired_at,
                           a.last_value, a.created_at,
                           s.name AS site_name, s.latitude, s.longitude
                      FROM saved_lp_alerts a
                      JOIN saved_lp_sites s ON s.id = a.saved_site_id
                     WHERE a.user_id = %s
                     ORDER BY a.created_at DESC LIMIT 200
                """, (user_id,))
                rows = cur.fetchall()
        finally:
            try: c.close()
            except Exception: pass
        out = [{
            "id":           int(r["id"]),
            "saved_site_id": int(r["saved_site_id"]),
            "site_name":    r["site_name"],
            "lat":          float(r["latitude"]),
            "lon":          float(r["longitude"]),
            "trigger_type": r["trigger_type"],
            "threshold":    float(r["threshold"]) if r["threshold"] is not None else None,
            "notify_email": r["notify_email"],
            "enabled":      bool(r["enabled"]),
            "last_fired_at":r["last_fired_at"].isoformat() if r["last_fired_at"] else None,
            "last_value":   float(r["last_value"]) if r["last_value"] is not None else None,
            "created_at":   r["created_at"].isoformat() if r["created_at"] else None,
        } for r in rows]
        return jsonify(alerts=out, count=len(out)), 200

    # POST — create alert
    d = request.get_json(silent=True) or {}
    try: site_id = int(d.get("saved_site_id") or 0)
    except (ValueError, TypeError): return jsonify(error="invalid_site_id"), 400
    trigger = (d.get("trigger_type") or "").strip().lower()
    if trigger not in ("dcpi_change", "capacity_change", "new_facility_nearby"):
        return jsonify(error="invalid_trigger_type",
                       allowed=["dcpi_change", "capacity_change", "new_facility_nearby"]), 400
    try: threshold = float(d.get("threshold") or 5.0)
    except (ValueError, TypeError): threshold = 5.0
    email = (d.get("notify_email") or "").strip().lower()
    if "@" not in email or len(email) > 200:
        return jsonify(error="invalid_email"), 400

    c = _conn()
    if c is None: return jsonify(error="no_database"), 503
    try:
        _ensure_schema(c)
        with c.cursor() as cur:
            # Verify site belongs to this user
            cur.execute("SELECT 1 FROM saved_lp_sites WHERE id = %s AND user_id = %s",
                        (site_id, user_id))
            if not cur.fetchone():
                return jsonify(error="site_not_found_or_not_yours"), 404
            cur.execute("""
                INSERT INTO saved_lp_alerts
                  (saved_site_id, user_id, trigger_type, threshold, notify_email)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT DO NOTHING
                RETURNING id
            """, (site_id, user_id, trigger, threshold, email))
            row = cur.fetchone()
            alert_id = int(row[0]) if row else None
    finally:
        try: c.close()
        except Exception: pass
    return jsonify(ok=True, alert_id=alert_id,
                   message=f"Alert created. Will email {email} when "
                            f"{trigger} exceeds {threshold}."), 200


@lp_sites_bp.route("/api/v1/lp/alerts/<int:alert_id>", methods=["DELETE"])
@_rl(per_minute=30)
def lp_alert_delete(alert_id):
    """Delete one site-alert config, scoped to the caller's user_id.

    Alerts Center (2026-06-06): the management page needs to REMOVE an
    alert, not just create one. Previously /lp/alerts had GET+POST only —
    the gap meant a user could add alerts forever but never clear them.
    Mirrors _lp_unsave_impl: idempotent, returns {ok, removed}."""
    user_id, gate = _require_pro_user()
    if gate is not None: return gate
    c = _conn()
    if c is None: return jsonify(error="no_database"), 503
    try:
        with c.cursor() as cur:
            cur.execute("DELETE FROM saved_lp_alerts WHERE id = %s AND user_id = %s",
                        (alert_id, user_id))
            removed = cur.rowcount or 0
    finally:
        try: c.close()
        except Exception: pass
    return jsonify(ok=True, removed=removed), 200


@lp_sites_bp.route("/api/v1/lp/export.csv", methods=["GET"])
@_rl(per_minute=10)  # exports are heavier — tighter cap
def lp_export_csv():
    user_id, gate = _require_pro_user()
    if gate is not None: return gate
    c = _conn()
    if c is None: return jsonify(error="no_database"), 503
    try:
        _ensure_schema(c)
        import psycopg2.extras
        with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT id, name, latitude, longitude, state, market,
                       notes, target_mw, dcpi_score_at_save, saved_at
                  FROM saved_lp_sites WHERE user_id = %s
                 ORDER BY saved_at DESC
            """, (user_id,))
            rows = cur.fetchall()
    finally:
        try: c.close()
        except Exception: pass
    import csv, io
    buf = io.StringIO()
    cols = ["id","name","latitude","longitude","state","market","notes",
            "target_mw","dcpi_score_at_save","saved_at"]
    w = csv.writer(buf)
    w.writerow(cols)
    for r in rows:
        w.writerow([r.get(c, "") if c != "saved_at" else
                     (r["saved_at"].isoformat() if r.get("saved_at") else "")
                     for c in cols])
    resp = Response(buf.getvalue(), mimetype="text/csv")
    resp.headers["Content-Disposition"] = "attachment; filename=dchub-lp-sites.csv"
    resp.headers["X-Total-Rows"] = str(len(rows))
    return resp, 200


@lp_sites_bp.route("/api/v1/lp/export.geojson", methods=["GET"])
@_rl(per_minute=10)
def lp_export_geojson():
    user_id, gate = _require_pro_user()
    if gate is not None: return gate
    c = _conn()
    if c is None: return jsonify(error="no_database"), 503
    try:
        _ensure_schema(c)
        import psycopg2.extras
        with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT id, name, latitude, longitude, state, market,
                       notes, target_mw, dcpi_score_at_save, saved_at
                  FROM saved_lp_sites WHERE user_id = %s
                 ORDER BY saved_at DESC
            """, (user_id,))
            rows = cur.fetchall()
    finally:
        try: c.close()
        except Exception: pass
    features = []
    for r in rows:
        features.append({
            "type": "Feature",
            "geometry": {"type": "Point",
                          "coordinates": [float(r["longitude"]), float(r["latitude"])]},
            "properties": {
                "id":         int(r["id"]),
                "name":       r["name"],
                "state":      r["state"],
                "market":     r["market"],
                "notes":      r["notes"],
                "target_mw":  float(r["target_mw"]) if r["target_mw"] is not None else None,
                "dcpi_score_at_save": int(r["dcpi_score_at_save"]) if r["dcpi_score_at_save"] is not None else None,
                "saved_at":   r["saved_at"].isoformat() if r.get("saved_at") else None,
            },
        })
    body = json.dumps({"type": "FeatureCollection", "features": features},
                      ensure_ascii=False)
    resp = Response(body, mimetype="application/geo+json")
    resp.headers["Content-Disposition"] = "attachment; filename=dchub-lp-sites.geojson"
    resp.headers["X-Total-Features"] = str(len(features))
    return resp, 200


# ─────────────────────────────────────────────────────────────────────────
# Alerts Center (2026-06-06) — the human-facing companion to the MCP agent
# moat. ONE branded page where a PRO user sees + manages everything the
# persistence layer holds for them: saved Land+Power sites, the alerts on
# those sites (with working delete + bulk export), plus the public $1B+
# hyperscaler deal feed we track for everyone. Fully client-rendered: the
# shell is public/always-200, and each section loads from the REAL API with
# the same-origin dchub_token cookie (the same identity the nav bridge sets
# and the save_site MCP tool writes under). Degrades honestly to a PRO /
# sign-in gate when the caller isn't an identified PRO user.
# ─────────────────────────────────────────────────────────────────────────
_ALERTS_CENTER_HTML = """<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<meta name=theme-color content="#0a0a0f">
<title>Alerts & Saved Sites · DC Hub</title>
<meta name=description content="Your DC Hub portfolio — saved Land+Power sites, market-movement alerts, bulk export, and the live $1B+ hyperscaler deal feed. One place for you and your agents.">
<meta name=robots content="noindex">
<link rel=stylesheet href="https://fonts.googleapis.com/css2?family=Instrument+Sans:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;500&display=swap">
<link rel=stylesheet href="/static/dchub-brand.css">
<script src="/js/dchub-nav.js" defer></script>
<style>
:root{--bg:#0a0a0f;--surf:#131319;--surf2:#1a1a22;--b:rgba(255,255,255,.09);--tx:#fafafa;--mut:#a1a1aa;--dim:#71717a;--ind:#818cf8;--vio:#a855f7;--cy:#22d3ee;--grn:#34d399;--red:#f87171;--amb:#fbbf24}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:#d4d4d8;font-family:'Instrument Sans',-apple-system,BlinkMacSystemFont,system-ui,sans-serif;line-height:1.55}
.wrap{max-width:980px;margin:0 auto;padding:2.5rem 1.25rem 4rem}
.kick{font-family:'JetBrains Mono',monospace;font-size:.72rem;letter-spacing:.16em;text-transform:uppercase;color:var(--cy);margin:0 0 .5rem}
h1{font-size:2rem;font-weight:800;letter-spacing:-.02em;color:#fff;margin:0 0 .4rem}
.lead{color:var(--mut);max-width:660px;margin:0 0 1.25rem}
.status{font-family:'JetBrains Mono',monospace;font-size:.78rem;color:var(--dim);margin:0 0 1.5rem;min-height:1em}
.sec{margin:2.2rem 0 0}
.sec-h{display:flex;align-items:baseline;gap:.6rem;margin:0 0 .9rem}
.sec-h h2{font-size:1.15rem;font-weight:700;color:#fff;margin:0;letter-spacing:-.01em}
.chip{font-family:'JetBrains Mono',monospace;font-size:.66rem;font-weight:600;color:var(--mut);border:1px solid var(--b);border-radius:999px;padding:.1rem .5rem}
.spacer{flex:1}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:.85rem}
.card{background:var(--surf);border:1px solid var(--b);border-radius:12px;padding:1rem 1.1rem}
.card .nm{font-weight:700;color:#fff;font-size:1.04rem;margin:0 0 .15rem}
.card .sub{font-family:'JetBrains Mono',monospace;font-size:.72rem;color:var(--dim);margin:0 0 .55rem}
.card .bits{display:flex;flex-wrap:wrap;gap:.35rem;margin:0 0 .7rem}
.bit{font-size:.74rem;color:var(--mut);background:var(--surf2);border:1px solid var(--b);border-radius:7px;padding:.18rem .5rem}
.card .acts{display:flex;gap:.5rem;align-items:center}
.alink{font-size:.78rem;color:var(--ind);text-decoration:none}
.alink:hover{text-decoration:underline}
.del{margin-left:auto;background:transparent;border:1px solid var(--b);color:var(--dim);border-radius:7px;padding:.22rem .55rem;font-size:.74rem;cursor:pointer;font-family:inherit}
.del:hover{border-color:var(--red);color:var(--red)}
.arow{display:flex;align-items:center;gap:.75rem;background:var(--surf);border:1px solid var(--b);border-radius:10px;padding:.7rem .9rem;margin:0 0 .55rem}
.arow .an{font-weight:600;color:#fff;font-size:.92rem}
.arow .as{font-family:'JetBrains Mono',monospace;font-size:.72rem;color:var(--dim)}
.badge{font-family:'JetBrains Mono',monospace;font-size:.6rem;font-weight:700;letter-spacing:.04em;border:1px solid;border-radius:999px;padding:.12rem .5rem;text-transform:uppercase}
.deal{display:flex;align-items:center;gap:.75rem;padding:.6rem 0;border-bottom:1px solid var(--b)}
.deal:last-child{border-bottom:0}
.deal a{color:#e4e4e7;text-decoration:none;font-size:.9rem;font-weight:500}
.deal a:hover{color:#fff;text-decoration:underline}
.deal .val{font-family:'JetBrains Mono',monospace;font-size:.74rem;font-weight:700;color:var(--grn);white-space:nowrap}
.deal .ds{font-family:'JetBrains Mono',monospace;font-size:.68rem;color:var(--dim);white-space:nowrap}
.empty{background:var(--surf);border:1px dashed var(--b);border-radius:12px;padding:1.4rem 1.2rem;color:var(--mut)}
.empty p{margin:0 0 .8rem}
.btn{display:inline-block;background:linear-gradient(135deg,var(--ind),var(--vio));color:#fff;text-decoration:none;font-weight:600;font-size:.86rem;padding:.55rem 1rem;border-radius:9px;border:0}
.btn.ghost{background:transparent;border:1px solid var(--b);color:var(--tx)}
.btn.ghost:hover{border-color:var(--ind)}
.bar{display:flex;flex-wrap:wrap;gap:.5rem;margin:.9rem 0 0}
.lock{background:var(--surf);border:1px solid var(--b);border-radius:14px;padding:1.8rem 1.6rem;max-width:560px}
.lock h2{margin:0 0 .5rem;color:#fff;font-size:1.3rem}
.lock p{color:var(--mut);margin:0 0 .9rem}
.muted{color:var(--dim);font-size:.85rem}
.foot{margin-top:2.8rem;padding-top:1.25rem;border-top:1px solid var(--b);color:var(--dim);font-size:.8rem;font-family:'JetBrains Mono',monospace}
.foot a{color:var(--ind);text-decoration:none}
</style></head><body><div class=wrap>
<p class=kick>Premium · Your Portfolio</p>
<h1>Alerts &amp; Saved Sites</h1>
<p class=lead>Everything DC Hub is holding for you — candidate Land+Power sites, the alerts watching them, and bulk export. The same portfolio your agents read and write through the MCP tools <code>save_site</code> and <code>set_market_alert</code>.</p>
<p class=status id=status>Loading your portfolio…</p>
<div id=gate></div>
<div id=sites class=sec style=display:none></div>
<div id=alerts class=sec style=display:none></div>
<div id=deals class=sec style=display:none></div>
<div class=foot>DC Hub · agent-native data-center &amp; power intelligence · <a href="/dashboard.html#api-keys">API keys</a> · <a href="/brief">Market briefs</a> · <a href="/pricing">Pricing</a></div>
</div>
<script>
(function(){
  var $=function(id){return document.getElementById(id);};
  function esc(s){return (s==null?'':String(s)).replace(/[&<>"]/g,function(c){return{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c];});}
  function fmtDate(s){if(!s)return '';try{return new Date(s).toLocaleDateString(undefined,{month:'short',day:'numeric',year:'numeric'});}catch(e){return '';}}
  function jget(url){return fetch(url,{credentials:'same-origin',headers:{'Accept':'application/json'}}).then(function(r){return r.json().then(function(b){return {s:r.status,b:b||{}};},function(){return {s:r.status,b:{}};});},function(){return {s:0,b:{}};});}

  function gate(kind){
    var msg = kind==='auth'
      ? 'Sign in to see the sites and alerts saved to your account.'
      : 'Saving sites, movement alerts, and bulk export are PRO features.';
    var cta = kind==='auth'
      ? '<a class=btn href="/login.html">Sign in</a> <a class="btn ghost" href="/pricing">See plans</a>'
      : '<a class=btn href="/pricing">Upgrade to PRO</a> <a class="btn ghost" href="/login.html">Sign in</a>';
    $('gate').innerHTML='<div class=lock><h2>Your portfolio is PRO</h2><p>'+msg+'</p>'
      +'<p class=muted>Save candidate sites from the <a href="/land-power-map" style="color:var(--ind)">land + power map</a>, '
      +'set alerts on DCPI / capacity / nearby-facility shifts, and pull everything as CSV or GeoJSON — for you and your agents.</p>'
      +cta+'</div>';
  }

  function renderSites(list){
    var sec=$('sites');
    var head='<div class=sec-h><h2>Saved sites</h2><span class=chip>'+list.length+'</span><span class=spacer></span>'
      +(list.length?'<a class="alink" href="/api/v1/lp/export.csv">Export CSV</a> &nbsp;<a class="alink" href="/api/v1/lp/export.geojson">GeoJSON</a>':'')+'</div>';
    if(!list.length){
      sec.innerHTML=head+'<div class=empty><p>No saved sites yet. Drop a pin on the map (or have your agent call <code>save_site</code>) to start a portfolio.</p><a class=btn href="/land-power-map">Open the land + power map</a></div>';
      sec.style.display=''; return;
    }
    var cards=list.map(function(s){
      var sub=[s.market,s.state].filter(Boolean).map(esc).join(' · ');
      var bits=[];
      if(s.target_mw!=null) bits.push(esc(s.target_mw)+' MW target');
      if(s.dcpi_score_at_save!=null) bits.push('DCPI '+esc(s.dcpi_score_at_save)+' @ save');
      if(s.latitude!=null&&s.longitude!=null) bits.push(Number(s.latitude).toFixed(3)+', '+Number(s.longitude).toFixed(3));
      var mapHref='/land-power-map?lat='+encodeURIComponent(s.latitude)+'&lon='+encodeURIComponent(s.longitude);
      return '<div class=card data-site="'+esc(s.id)+'">'
        +'<div class=nm>'+esc(s.name||'Untitled site')+'</div>'
        +(sub?'<div class=sub>'+sub+'</div>':'<div class=sub>saved '+esc(fmtDate(s.saved_at))+'</div>')
        +'<div class=bits>'+bits.map(function(b){return '<span class=bit>'+b+'</span>';}).join('')+'</div>'
        +'<div class=acts><a class=alink href="'+mapHref+'">View on map</a>'
        +'<button class=del data-act=del-site data-id="'+esc(s.id)+'">Remove</button></div></div>';
    }).join('');
    sec.innerHTML=head+'<div class=grid>'+cards+'</div>';
    sec.style.display='';
  }

  function renderAlerts(list){
    var sec=$('alerts');
    var head='<div class=sec-h><h2>Site alerts</h2><span class=chip>'+list.length+'</span></div>';
    if(!list.length){
      sec.innerHTML=head+'<div class=empty><p>No alerts set. Get an email when a saved site\\'s DCPI score, grid capacity, or nearby facilities shift — set one from a site, or via the <code>set_market_alert</code> agent tool.</p></div>';
      sec.style.display=''; return;
    }
    var trigCol={dcpi_change:'var(--ind)',capacity_change:'var(--cy)',new_facility_nearby:'var(--vio)'};
    var rows=list.map(function(a){
      var col=trigCol[a.trigger_type]||'var(--mut)';
      var sub=[]; if(a.threshold!=null) sub.push('±'+esc(a.threshold));
      if(a.notify_email) sub.push(esc(a.notify_email));
      if(a.last_fired_at) sub.push('last fired '+esc(fmtDate(a.last_fired_at))); else sub.push('not yet fired');
      return '<div class=arow><span class=badge style="color:'+col+';border-color:'+col+'">'+esc((a.trigger_type||'').replace(/_/g,' '))+'</span>'
        +'<div><div class=an>'+esc(a.site_name||('Site #'+a.saved_site_id))+'</div>'
        +'<div class=as>'+sub.join(' · ')+'</div></div>'
        +'<span class=spacer></span><button class=del data-act=del-alert data-id="'+esc(a.id)+'">Remove</button></div>';
    }).join('');
    sec.innerHTML=head+rows;
    sec.style.display='';
  }

  function renderDeals(){
    jget('/api/v1/hyperscaler-alerts').then(function(r){
      var list=(r.b&&r.b.alerts)||[]; if(!list.length) return;
      var sec=$('deals');
      var rows=list.slice(0,8).map(function(d){
        var v=d.value_display||(d.value_usd?('$'+(Number(d.value_usd)/1e9).toFixed(1)+'B'):'');
        var src=[d.actor,d.source].filter(Boolean).map(esc).join(' · ');
        return '<div class=deal><a href="'+esc(d.url||'#')+'" target=_blank rel=noopener>'+esc(d.headline||'')+'</a>'
          +'<span class=spacer></span>'+(src?'<span class=ds>'+src+'</span>':'')
          +(v?'<span class=val>'+esc(v)+'</span>':'')+'</div>';
      }).join('');
      sec.innerHTML='<div class=sec-h><h2>$1B+ deals we\\'re tracking</h2><span class=chip>public</span><span class=spacer></span>'
        +'<a class=alink href="/brief">Set a market alert →</a></div>'
        +'<div class=card>'+rows+'</div>';
      sec.style.display='';
    });
  }

  $('sites').addEventListener('click',function(e){ handleDel(e); });
  $('alerts').addEventListener('click',function(e){ handleDel(e); });
  function handleDel(e){
    var btn=e.target.closest&&e.target.closest('[data-act]'); if(!btn) return;
    var act=btn.getAttribute('data-act'), id=btn.getAttribute('data-id');
    var url=act==='del-site'?('/api/v1/lp/saved/'+id):('/api/v1/lp/alerts/'+id);
    if(!confirm('Remove this '+(act==='del-site'?'saved site':'alert')+'?')) return;
    btn.disabled=true; btn.textContent='Removing…';
    fetch(url,{method:'DELETE',credentials:'same-origin'}).then(function(r){
      if(r.ok){ load(); } else { btn.disabled=false; btn.textContent='Remove'; alert('Could not remove (status '+r.status+').'); }
    }).catch(function(){ btn.disabled=false; btn.textContent='Remove'; });
  }

  function load(){
    jget('/api/v1/lp/saved').then(function(r){
      if(r.s===401){ gate('auth'); $('status').textContent=''; $('sites').style.display='none'; $('alerts').style.display='none'; return; }
      if(r.s===402||r.s===403){ gate('pro'); $('status').textContent=''; $('sites').style.display='none'; $('alerts').style.display='none'; return; }
      if(r.s!==200){ $('status').textContent='Could not load your portfolio (status '+r.s+'). Try refreshing.'; return; }
      $('gate').innerHTML=''; $('status').textContent='';
      renderSites((r.b&&r.b.saved)||[]);
      jget('/api/v1/lp/alerts').then(function(a){ renderAlerts(a.s===200?((a.b&&a.b.alerts)||[]):[]); });
    });
  }

  renderDeals();
  load();
})();
</script>
</body></html>"""


@lp_sites_bp.route("/alerts", methods=["GET"])
@lp_sites_bp.route("/account/alerts", methods=["GET"])
def html_alerts_center():
    """Public/always-200 shell for the Alerts Center. All per-user data is
    fetched client-side from the real /api/v1/lp/* endpoints using the
    same-origin dchub_token cookie; the page degrades to a PRO/sign-in gate
    when the caller is not an identified PRO user. Not tier-gated here so
    anon visitors see the value proposition + the public deal feed."""
    return Response(_ALERTS_CENTER_HTML, mimetype="text/html",
                    headers={"Cache-Control": "private, max-age=0, must-revalidate"})
