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


@lp_sites_bp.route("/api/v1/lp/saved/<int:site_id>", methods=["GET"])
@_rl(per_minute=30)
def lp_get_site(site_id):
    """Phase QQQQ (2026-05-16): per-site detail. Returns the saved
    site + current DCPI for its market + nearby substations/fiber
    count within 50km. Powers the per-site detail UI (queued) and
    gives MCP clients a single-call rich snapshot for analyze workflows."""
    user_id, gate = _require_pro_user()
    if gate is not None: return gate
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


@lp_sites_bp.route("/api/v1/lp/saved/<int:site_id>", methods=["DELETE"])
@_rl(per_minute=30)
def lp_unsave(site_id):
    user_id, gate = _require_pro_user()
    if gate is not None: return gate
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
