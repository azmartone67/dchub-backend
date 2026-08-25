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
from routes._swallowed_writes import note_swallowed_write


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


# ── LP-SENTINEL escalation sink (2026-06-15) ────────────────────────────────
# The in-page LP-SENTINEL (land-power-app.js) detects auto-hide regressions ("a
# layer left the map with no user input") + stale builds, but was console+window
# only — the alarm rang in an empty room (that's how the transformer auto-hide
# went unnoticed). These endpoints capture what REAL users' browsers detect so a
# silent layer regression surfaces between the daily headless QA runs.
_SENTINEL_SCHEMA = """
CREATE TABLE IF NOT EXISTS lp_sentinel_reports (
    id              BIGSERIAL PRIMARY KEY,
    reported_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    layer           TEXT,
    kind            TEXT,
    since_input_ms  INTEGER,
    app_version     TEXT,
    url             TEXT,
    ua              TEXT
);
CREATE INDEX IF NOT EXISTS ix_lp_sentinel_at ON lp_sentinel_reports (reported_at DESC);
"""


@lp_sites_bp.route("/api/v1/lp/sentinel/report", methods=["POST"])
def lp_sentinel_report():
    """Fire-and-forget sink for the in-page LP-SENTINEL (sendBeacon). Records a
    real user's auto-hide / stale-build detection. Best-effort + bounded; never
    errors loudly (it's telemetry)."""
    try:
        d = request.get_json(force=True, silent=True) or {}
    except Exception:
        d = {}
    layer = str(d.get("layer", ""))[:60]
    if not layer:
        return jsonify(ok=False, reason="no_layer"), 200
    kind = str(d.get("kind", "auto_hide"))[:40]
    try:
        since = int(d.get("sinceInputMs") or 0)
    except Exception:
        since = 0
    ver = str(d.get("version", ""))[:20]
    url = str(d.get("url", ""))[:200]
    ua = (request.headers.get("User-Agent") or "")[:200]
    c = _conn()
    if c is None:
        return jsonify(ok=False, reason="no_db"), 200
    try:
        cur = c.cursor()
        cur.execute(_SENTINEL_SCHEMA)
        cur.execute(
            "INSERT INTO lp_sentinel_reports (layer, kind, since_input_ms, app_version, url, ua) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (layer, kind, since, ver, url, ua),
        )
    except Exception:
        note_swallowed_write("lp_sentinel_reports", where="lp_sites.lp_sentinel_report")
        pass
    finally:
        try: c.close()
        except Exception: pass
    return jsonify(ok=True), 200


@lp_sites_bp.route("/api/v1/lp/sentinel/reports", methods=["GET"])
def lp_sentinel_reports():
    """Recent real-user LP-SENTINEL reports (7d) + a per-layer 24h count — for the
    brain/dashboard to surface silent layer regressions."""
    c = _conn()
    if c is None:
        return jsonify(reports=[], count=0), 200
    try:
        cur = c.cursor()
        cur.execute(_SENTINEL_SCHEMA)
        cur.execute(
            "SELECT reported_at, layer, kind, since_input_ms, app_version "
            "FROM lp_sentinel_reports WHERE reported_at > NOW() - INTERVAL '7 days' "
            "ORDER BY reported_at DESC LIMIT 200"
        )
        rows = [{"at": r[0].isoformat() if r[0] else None, "layer": r[1], "kind": r[2],
                 "since_input_ms": r[3], "version": r[4]} for r in cur.fetchall()]
        cur.execute(
            "SELECT layer, COUNT(*) FROM lp_sentinel_reports "
            "WHERE reported_at > NOW() - INTERVAL '24 hours' GROUP BY layer ORDER BY COUNT(*) DESC"
        )
        by_layer = {r[0]: int(r[1]) for r in cur.fetchall()}
        return jsonify(reports=rows, count=len(rows), by_layer_24h=by_layer), 200
    except Exception as e:
        return jsonify(reports=[], error=str(e)[:160]), 200
    finally:
        try: c.close()
        except Exception: pass


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


def _require_keyed_user():
    """r-free-shortlist (2026-06-24): FREE-allowed variant of _require_pro_user.
    Any IDENTIFIED key (free+) may persist + read its OWN shortlist
    (save_site / list_saved_sites) — the retention 'reason to come back'. Still
    requires a key (for the per-account user_id); anon has nowhere to persist.
    No tier gate and NO email destination here, so there is no abuse vector —
    unlike alerts, which stay PRO until their destination is bound-email-locked."""
    user_id = _user_id_from_request()
    if not user_id:
        from flask import jsonify as _j
        return None, (_j(error="auth_required",
                          message="A DC Hub key identifies your shortlist. "
                                  "Call claim_free_key (free, no email) then retry."), 401)
    return user_id, None


def _bound_email_from_request():
    """The verified email bound to the caller, or None. FREE-tier alerts are LOCKED
    to this address so a free caller can never relay alert mail to a third party (no
    spam relay).

    keystone / item-7 (2026-06-30): also resolve the email from the calling MCP
    session — the audit hit 'bind_email_first' 403s even when an email WAS bound,
    because the transport's per-session key carries no email while the human's bound
    email lives on a DIFFERENT claimed key. Resolve across (a) this key, (b) this
    trial key, (c) the key this Mcp-Session-Id is bound to (metadata.session_id — the
    keystone bind), (d) mcp_session_upgrades.user_email. Fail-soft per lookup."""
    from flask import request as _rq
    ak = (_rq.headers.get("X-API-Key") or _rq.args.get("api_key") or "").strip()
    try:
        sid = (_rq.headers.get("X-MCP-Session") or "").strip()[:200]
    except Exception:
        sid = ""
    if not ak and not sid:
        return None
    lookups = []
    if ak:
        lookups.append(("SELECT email FROM mcp_dev_keys WHERE api_key = %s", (ak,)))
        lookups.append(("SELECT operator_email FROM auto_trial_keys WHERE api_key = %s", (ak,)))
    if sid:
        lookups.append(("""SELECT email FROM mcp_dev_keys
                             WHERE metadata->>'session_id' = %s AND email IS NOT NULL
                             ORDER BY created_at DESC LIMIT 1""", (sid,)))
        lookups.append(("SELECT user_email FROM mcp_session_upgrades WHERE mcp_session_id = %s", (sid,)))
    c = _conn()
    if c is None:
        return None
    try:
        with c.cursor() as cur:
            for _sql, _params in lookups:
                try:
                    cur.execute(_sql, _params)
                    r = cur.fetchone()
                    if r and r[0] and str(r[0]).strip():
                        return str(r[0]).strip().lower()
                except Exception:
                    try: c.rollback()
                    except Exception: pass
                    continue
        return None
    except Exception:
        return None
    finally:
        try: c.close()
        except Exception: pass


# ── Portfolio deltas (2026-07-11) ────────────────────────────────
# r-portfolio: get_changes was 100% GLOBAL (California movers for an agent
# whose only saved site is Phoenix) and list_saved_sites returned static
# rows — the one question a returning agent actually has ("did MY sites
# move?") was unanswerable. These helpers compute per-saved-site deltas
# (DCPI verdict flip, excess-power move, alert fired, new facility nearby)
# so both endpoints can answer it. Every query is individually fail-soft:
# a missing table/column degrades that signal to empty, never the response.

def _haversine_km(lat1, lon1, lat2, lon2):
    import math
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2))
         * math.sin(dlon / 2) ** 2)
    return 6371.0 * 2 * math.asin(min(1.0, math.sqrt(a)))


def _market_match_keys(market):
    """Normalize a saved site's market string for matching against both
    market_slug ('northern-virginia') and market_name ('Northern Virginia')."""
    m = (market or "").strip().lower()
    return (m.replace(" ", "-"), m)


# r-lonfix (2026-07-16): callers name coordinates every way that exists in the
# wild (lat/latitude, lon/lng/longitude) — and the old `or 0` default meant a
# key-name miss saved a (0, 0) row while reporting "Saved!" (19 of the last 23
# saved_lp_sites rows had longitude=0), poisoning every downstream delta/alert
# read. Missing is now an ERROR; an explicit 0 stays a valid coordinate.
def _extract_coords(d):
    """Pull (lat, lon, err) from a save payload. err is None on success, else
    one of 'missing_lat'/'missing_lon'/'missing_lat_lon'/'not_numeric'/
    'out_of_range' (lat and lon are None whenever err is set)."""
    lat_raw = next((d[k] for k in ("latitude", "lat") if d.get(k) is not None), None)
    lon_raw = next((d[k] for k in ("longitude", "lon", "lng") if d.get(k) is not None), None)
    if lat_raw is None or lon_raw is None:
        missing = "_".join(n for n, v in (("lat", lat_raw), ("lon", lon_raw))
                           if v is None)
        return None, None, f"missing_{missing}"
    try:
        lat, lon = float(lat_raw), float(lon_raw)
    except (ValueError, TypeError):
        return None, None, "not_numeric"
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None, None, "out_of_range"
    return lat, lon, None


def _match_new_facilities(sites, facilities, radius_km=50.0, cap_per_site=3):
    """Pure matcher: {site_id: [up to cap_per_site nearest new facilities]}.
    Sites/facilities are dicts with latitude/longitude; bad rows are skipped."""
    out = {}
    for s in sites:
        try:
            slat, slon = float(s["latitude"]), float(s["longitude"])
        except (KeyError, TypeError, ValueError):
            continue
        near = []
        for f in facilities:
            try:
                flat, flon = f.get("latitude"), f.get("longitude")
                if flat is None or flon is None:
                    continue
                km = _haversine_km(slat, slon, float(flat), float(flon))
            except (TypeError, ValueError):
                continue
            if km <= radius_km:
                near.append({"name": f.get("name"), "state": f.get("state"),
                             "capacity_mw": f.get("capacity_mw"),
                             "km": round(km, 1)})
        if near:
            near.sort(key=lambda x: x["km"])
            out[s["id"]] = near[:cap_per_site]
    return out


def _site_movement(site):
    """Pure: did this enriched site 'move' since the window start? Any of:
    verdict flipped, excess-power moved >= 1pt, an alert fired, or a new
    facility appeared nearby."""
    try:
        if site.get("verdict_changed"):
            return True
        d = site.get("excess_power_delta_window")
        if d is not None and abs(float(d)) >= 1.0:
            return True
        if site.get("alerts_fired_window"):
            return True
        if site.get("new_facilities_nearby"):
            return True
    except (TypeError, ValueError):
        pass
    return False


def _clamp_since(since_dt):
    """Clamp a portfolio window start to [now-30d, now-1h]; default 7d."""
    now = datetime.datetime.now(datetime.timezone.utc)
    if since_dt is None:
        return now - datetime.timedelta(days=7)
    try:
        if since_dt.tzinfo is None:
            since_dt = since_dt.replace(tzinfo=datetime.timezone.utc)
        floor = now - datetime.timedelta(days=30)
        ceil = now - datetime.timedelta(hours=1)
        return max(floor, min(since_dt, ceil))
    except Exception:
        return now - datetime.timedelta(days=7)


def portfolio_snapshot(user_id, since_dt=None, max_sites=100):
    """Per-saved-site deltas for a returning agent, one compact dict:

      {saved_sites, alerts_armed, since, moved_count,
       moved: [{id, name, market, verdict_was, verdict_now, ...} cap 10],
       sites: [every saved site + enrichment fields]}

    Returns {"saved_sites": 0} when the user has no saved sites, or None on
    total failure (caller keeps its unpersonalized response). ~5 grouped
    queries regardless of site count; each signal fails soft to empty."""
    if not user_id:
        return None
    since_dt = _clamp_since(since_dt)
    c = _conn()
    if c is None:
        return None
    try:
        import psycopg2.extras
        with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            try:
                cur.execute("""
                    SELECT id, name, latitude, longitude, state, market,
                           target_mw, dcpi_score_at_save, saved_at
                      FROM saved_lp_sites
                     WHERE user_id = %s
                     ORDER BY saved_at DESC LIMIT %s
                """, (user_id, max_sites))
                rows = cur.fetchall()
            except Exception:
                return None
            if not rows:
                return {"saved_sites": 0}
            sites = []
            for r in rows:
                sites.append({
                    "id": int(r["id"]), "name": r["name"],
                    "latitude": float(r["latitude"]),
                    "longitude": float(r["longitude"]),
                    "state": r["state"], "market": r["market"],
                    "target_mw": float(r["target_mw"]) if r["target_mw"] is not None else None,
                    "dcpi_at_save": int(r["dcpi_score_at_save"]) if r["dcpi_score_at_save"] is not None else None,
                    "saved_at": r["saved_at"].isoformat() if r["saved_at"] else None,
                })

            slugs, names = set(), set()
            for s in sites:
                if s.get("market"):
                    sl, nm = _market_match_keys(s["market"])
                    slugs.add(sl); names.add(nm)

            # (a) window verdict + excess-power delta from the history-preserving
            # daily snapshots (same source as get_changes dcpi_movers).
            snap = {}
            if slugs:
                try:
                    cur.execute("""
                        WITH latest AS (
                          SELECT DISTINCT ON (market_slug) market_slug,
                                 LOWER(market_name) AS lname,
                                 excess_power_score AS now_e, verdict AS now_v
                            FROM dcpi_daily_snapshots
                           ORDER BY market_slug, snapshot_date DESC
                        ), prev AS (
                          SELECT DISTINCT ON (market_slug) market_slug,
                                 excess_power_score AS prev_e, verdict AS prev_v
                            FROM dcpi_daily_snapshots
                           WHERE snapshot_date <= %s::date
                           ORDER BY market_slug, snapshot_date DESC
                        )
                        SELECT l.market_slug, l.lname, l.now_e, l.now_v,
                               p.prev_e, p.prev_v
                          FROM latest l LEFT JOIN prev p USING (market_slug)
                         WHERE l.market_slug = ANY(%s) OR l.lname = ANY(%s)
                    """, (since_dt, list(slugs), list(names)))
                    for r in cur.fetchall():
                        d = {"now_e": r["now_e"], "now_v": r["now_v"],
                             "prev_e": r["prev_e"], "prev_v": r["prev_v"]}
                        snap[r["market_slug"]] = d
                        if r["lname"]:
                            snap[r["lname"]] = d
                except Exception:
                    try: c.rollback()
                    except Exception: pass

            # (b) current composite DCPI (0-100) so dcpi_at_save has a live
            # counterpart — same derivation as the /lp/saved/<id> detail view.
            comp = {}
            if slugs:
                try:
                    cur.execute("""
                        SELECT DISTINCT ON (LOWER(market_slug))
                               LOWER(market_slug) AS mslug,
                               LOWER(market_name) AS mname,
                               excess_power_score, constraint_score,
                               time_to_power_months, verdict
                          FROM market_power_scores
                         WHERE LOWER(market_slug) = ANY(%s)
                            OR LOWER(market_name) = ANY(%s)
                         ORDER BY LOWER(market_slug), computed_at DESC
                    """, (list(slugs), list(names)))
                    from routes.dcpi import derive_composite_score
                    for r in cur.fetchall():
                        try:
                            v = round(float(derive_composite_score(
                                r["excess_power_score"], r["constraint_score"],
                                r["time_to_power_months"], r["verdict"])), 1)
                        except Exception:
                            continue
                        comp[r["mslug"]] = v
                        if r["mname"]:
                            comp[r["mname"]] = v
                except Exception:
                    try: c.rollback()
                    except Exception: pass

            # (c) armed alerts + fired-in-window per site
            alerts = {}
            try:
                cur.execute("""
                    SELECT saved_site_id,
                           COUNT(*) FILTER (WHERE enabled) AS armed,
                           COUNT(*) FILTER (WHERE last_fired_at > %s) AS fired,
                           MAX(last_fired_at) AS last_fired
                      FROM saved_lp_alerts
                     WHERE user_id = %s
                     GROUP BY saved_site_id
                """, (since_dt, user_id))
                for r in cur.fetchall():
                    alerts[int(r["saved_site_id"])] = {
                        "armed": int(r["armed"] or 0),
                        "fired": int(r["fired"] or 0),
                        "last_fired": r["last_fired"].isoformat() if r["last_fired"] else None,
                    }
            except Exception:
                try: c.rollback()
                except Exception: pass

            # (d) new facilities in the window (fleet filter per canonical
            # formula), matched to sites in Python — one query, not N.
            nearby = {}
            try:
                cur.execute("""
                    SELECT name, state, latitude, longitude, capacity_mw
                      FROM discovered_facilities
                     WHERE created_at > %s AND COALESCE(is_duplicate, 0) = 0
                       AND latitude IS NOT NULL AND longitude IS NOT NULL
                     ORDER BY created_at DESC LIMIT 300
                """, (since_dt,))
                new_fac = [dict(r) for r in cur.fetchall()]
                if new_fac:
                    nearby = _match_new_facilities(sites, new_fac)
            except Exception:
                try: c.rollback()
                except Exception: pass
    except Exception:
        return None
    finally:
        try: c.close()
        except Exception: pass

    total_armed = 0
    for s in sites:
        key_slug, key_name = _market_match_keys(s.get("market"))
        sn = snap.get(key_slug) or snap.get(key_name)
        if sn:
            if sn.get("now_v") is not None:
                s["verdict_now"] = sn["now_v"]
            if sn.get("prev_v") is not None:
                s["verdict_was"] = sn["prev_v"]
            if sn.get("now_v") and sn.get("prev_v"):
                s["verdict_changed"] = sn["now_v"] != sn["prev_v"]
            if sn.get("now_e") is not None and sn.get("prev_e") is not None:
                s["excess_power_delta_window"] = round(float(sn["now_e"]) - float(sn["prev_e"]), 1)
        cv = comp.get(key_slug) or comp.get(key_name)
        if cv is not None:
            s["dcpi_now"] = cv
            if s.get("dcpi_at_save") is not None:
                s["dcpi_delta_since_save"] = round(cv - float(s["dcpi_at_save"]), 1)
        al = alerts.get(s["id"])
        if al:
            s["alerts_armed"] = al["armed"]
            total_armed += al["armed"]
            if al["fired"]:
                s["alerts_fired_window"] = al["fired"]
            if al["last_fired"]:
                s["last_alert_fired_at"] = al["last_fired"]
        if nearby.get(s["id"]):
            s["new_facilities_nearby"] = nearby[s["id"]]
        s["moved"] = _site_movement(s)

    moved = [s for s in sites if s["moved"]]
    _MOVED_FIELDS = ("id", "name", "market", "state", "verdict_was",
                     "verdict_now", "verdict_changed",
                     "excess_power_delta_window", "dcpi_now",
                     "dcpi_delta_since_save", "alerts_fired_window",
                     "new_facilities_nearby")
    return {
        "saved_sites": len(sites),
        "alerts_armed": total_armed,
        "since": since_dt.isoformat(),
        "moved_count": len(moved),
        "moved": [{k: s[k] for k in _MOVED_FIELDS if s.get(k) is not None}
                  for s in moved[:10]],
        "sites": sites,
    }


# ── REST endpoints ────────────────────────────────────────────────

@lp_sites_bp.route("/api/v1/lp/save", methods=["POST"])
@_rl(per_minute=30)
def lp_save():
    user_id, gate = _require_keyed_user()   # r-free-shortlist: save is free with a key
    if gate is not None: return gate
    d = request.get_json(silent=True) or {}
    lat, lon, coord_err = _extract_coords(d)
    if coord_err:
        return jsonify(
            error="invalid_coords", detail=coord_err,
            hint="pass BOTH lat (or latitude) AND lon (or lng/longitude) as "
                 "decimal degrees, e.g. {\"lat\": 39.04, \"lon\": -77.48}"), 400
    name = (d.get("name") or "").strip()[:120] or "Untitled site"
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
        # r-portfolio (2026-07-11): self-derive the DCPI baseline when the
        # caller didn't pass one. The MCP save_site tool never sends
        # dcpi_score_at_save, so every agent-saved site had a NULL baseline
        # and "your site's score moved since you saved it" could never fire.
        # Same derivation as the /lp/saved/<id> detail view; fail-soft.
        if dcpi_score is None and market:
            try:
                _slug, _name = _market_match_keys(market)
                with c.cursor() as cur:
                    cur.execute("""
                        SELECT excess_power_score, constraint_score,
                               time_to_power_months, verdict
                          FROM market_power_scores
                         WHERE LOWER(market_name) = %s OR LOWER(market_slug) = %s
                         ORDER BY computed_at DESC LIMIT 1
                    """, (_name, _slug))
                    r = cur.fetchone()
                    if r:
                        from routes.dcpi import derive_composite_score
                        dcpi_score = int(round(float(derive_composite_score(
                            r[0], r[1], r[2], r[3]))))
            except Exception:
                try: c.rollback()
                except Exception: pass
        with c.cursor() as cur:
            # ON CONFLICT keeps the ORIGINAL baseline (deltas measure from
            # first save) but backfills it when the existing row has NULL.
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
                      target_mw = EXCLUDED.target_mw,
                      dcpi_score_at_save = COALESCE(saved_lp_sites.dcpi_score_at_save,
                                                    EXCLUDED.dcpi_score_at_save)
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


def _parse_since_param(raw):
    """Parse an optional ?since= ('24h'/'7d' shorthand or ISO-8601) into a
    tz-aware datetime, or None (caller defaults). Mirrors changes_feed."""
    if not raw:
        return None
    now = datetime.datetime.now(datetime.timezone.utc)
    try:
        s = str(raw).strip().lower()
        if s.endswith("h") and s[:-1].isdigit():
            return now - datetime.timedelta(hours=int(s[:-1]))
        if s.endswith("d") and s[:-1].isdigit():
            return now - datetime.timedelta(days=int(s[:-1]))
        dt = datetime.datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return dt
    except Exception:
        return None


@lp_sites_bp.route("/api/v1/lp/saved", methods=["GET"])
@_rl(per_minute=60)
def lp_list_saved():
    user_id, gate = _require_keyed_user()   # r-free-shortlist: read-back is free with a key
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

    # r-portfolio (2026-07-11): the static read-back answered "what did I
    # save?" but not the returning agent's real question — "what MOVED?".
    # Merge per-site deltas (verdict flip, excess-power move, alerts fired,
    # new facilities nearby) + a compact summary. ?since=24h/7d/ISO sets the
    # window (default 7d). Fully fail-soft: enrichment failure returns the
    # plain list exactly as before.
    payload = {"saved": out, "count": len(out)}
    if out:
        try:
            pf = portfolio_snapshot(
                user_id, since_dt=_parse_since_param(request.args.get("since")),
                max_sites=500)
            if pf and pf.get("sites"):
                _ENRICH = ("verdict_now", "verdict_was", "verdict_changed",
                           "excess_power_delta_window", "dcpi_now",
                           "dcpi_delta_since_save", "alerts_armed",
                           "alerts_fired_window", "last_alert_fired_at",
                           "new_facilities_nearby", "moved")
                by_id = {s["id"]: s for s in pf["sites"]}
                for row in out:
                    e = by_id.get(row["id"])
                    if e:
                        for k in _ENRICH:
                            if e.get(k) is not None:
                                row[k] = e[k]
                summary = {
                    "saved_sites": pf.get("saved_sites"),
                    "alerts_armed": pf.get("alerts_armed"),
                    "moved_count": pf.get("moved_count"),
                    "since": pf.get("since"),
                }
                if pf.get("moved_count"):
                    _mv = [s["name"] for s in pf.get("moved", [])[:3] if s.get("name")]
                    summary["agent_hint"] = (
                        f"{pf['moved_count']} of your {pf['saved_sites']} saved "
                        f"site(s) moved in this window"
                        + (f" ({', '.join(_mv)})" if _mv else "")
                        + " — rows carry verdict_was/verdict_now + deltas.")
                unalerted = [r["id"] for r in out if not r.get("alerts_armed")]
                if unalerted:
                    summary["unwatched_site_ids"] = unalerted[:20]
                    summary["arm_hint"] = (
                        "Sites without an alert armed — set_site_alert on these "
                        "ids emails your human the moment one moves.")
                payload["portfolio"] = summary
        except Exception:
            pass
    return jsonify(**payload), 200


# Phase QQQQ + GGGG consolidated handler — single decorator for both
# GET (detail) and DELETE (unsave) to satisfy regression-lint's
# duplicate-route rule. Branches on request.method.
@lp_sites_bp.route("/api/v1/lp/saved/<int:site_id>", methods=["GET", "DELETE"])
@_rl(per_minute=30)
def lp_site_detail_or_delete(site_id):
    user_id, gate = _require_keyed_user()   # r-free-shortlist: manage your own saved site, free with a key
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

            # Current DCPI for the market. market_power_scores has no stored
            # `score` column — derive the composite at read time from the
            # writer-guaranteed components (canonical formula, same pattern
            # as site_valuation_engine). dcpi_score_at_save is the composite
            # the UI showed at save time, so this keeps dcpi_delta
            # apples-to-apples.
            current_dcpi = None
            if site.get("market"):
                try:
                    cur.execute("""
                        SELECT excess_power_score, constraint_score,
                               time_to_power_months, verdict
                          FROM market_power_scores
                         WHERE LOWER(market_name) = LOWER(%s)
                            OR LOWER(market_slug) = LOWER(%s)
                         ORDER BY computed_at DESC LIMIT 1
                    """, (site["market"], site["market"].replace(" ", "-")))
                    r = cur.fetchone()
                    if r:
                        from routes.dcpi import derive_composite_score
                        current_dcpi = float(derive_composite_score(
                            r["excess_power_score"], r["constraint_score"],
                            r["time_to_power_months"], r["verdict"]))
                except Exception: pass

            # Nearby substation count within 50km — bbox + haversine
            nearby_substations = 0
            try:
                deg_lat = 50.0 / 111.0
                deg_lon = 50.0 / (111.0 * max(0.01, abs((90.0 - abs(lat)) / 90.0) + 0.1))
                cur.execute("""
                    SELECT COUNT(*) FROM substations
                     WHERE lat BETWEEN %s AND %s
                       AND lng BETWEEN %s AND %s
                       AND (
                         6371.0 * acos(LEAST(1.0, GREATEST(-1.0,
                           cos(radians(%s)) * cos(radians(lat)) *
                           cos(radians(lng) - radians(%s)) +
                           sin(radians(%s)) * sin(radians(lat))
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
    user_id, gate = _require_keyed_user()   # r-free-alerts: free with a key; destination is locked to the bound email below
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
    # r-free-alerts (2026-06-24): a FREE caller may only alert their OWN bound
    # email — no third-party destinations (= no spam relay). PRO/ENT keep an
    # arbitrary notify_email (low-abuse, paying, e.g. a team distro).
    from routes.tier_gate import _resolve_caller_tier as _rct
    _tier, _ = _rct()
    if (_tier or "free").lower() not in ("pro", "enterprise"):
        _bound = _bound_email_from_request()
        if not _bound:
            return jsonify(error="bind_email_first",
                           message="Free alerts are delivered to your bound email. "
                                   "Call bind_email with your email first, then set the alert."), 403
        email = _bound
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
<p class=lead>Your DC Hub portfolio — candidate Land+Power sites, the alerts watching them, and one-click CSV / GeoJSON export, all in one place. Agents get the same persistence through the <code>save_site</code> and <code>set_market_alert</code> MCP tools.</p>
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
