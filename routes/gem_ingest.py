"""GEM (Global Energy Monitor) ingest — international power + gas infrastructure.

Fills the international PHYSICAL-ASSET gap: unit-level power plants worldwide
(all fuels, incl. the forward pipeline — announced / pre-construction /
construction) and gas infrastructure (LNG terminals), each geolocated with
status + capacity + owner. Source: GEM trackers (Global Integrated Power
Tracker, Global Gas Infrastructure Tracker), CC-BY-4.0.

GEM data is download-gated (a one-time CC-BY form → xlsx bundle), so the FETCH
is not fully hands-off: the owner downloads the bundle, a parser (openpyxl)
turns the sheets into compact rows, and POSTs them here. This endpoint only
does fast DB writes — same admin-gated / idempotent / empty-replace-guarded /
transaction-wrapped pattern as planned_generators_ingest.

Tables: gem_power (integrated power tracker), gem_gas (LNG terminals + gas
points). Each ingest is a full-replace by source tag, so re-loading a fresh GEM
release is safe and idempotent.
"""
import gzip
import json
import logging
import os

import psycopg2
from flask import Blueprint, jsonify, request

log = logging.getLogger("gem_ingest")
gem_ingest_bp = Blueprint("gem_ingest", __name__)

# ── Global Integrated Power ────────────────────────────────────────────────
_POWER_SRC = "gem_integrated_power"
_POWER_FIELDS = ["gem_id", "fuel_type", "plant_name", "unit_name", "capacity_mw",
                 "status", "start_year", "technology", "country", "region",
                 "operator", "owner", "lat", "lng", "wiki_url"]

# ── Gas infrastructure (LNG terminals + gas points) ────────────────────────
_GAS_SRC = "gem_gas_infra"
_GAS_FIELDS = ["gem_id", "kind", "name", "unit_name", "fuel", "capacity",
               "capacity_units", "status", "start_year", "country", "region",
               "owner", "lat", "lng", "wiki_url"]


def _dsn() -> str:
    return os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL") or ""


def _admin_ok() -> bool:
    expected = os.environ.get("DCHUB_ADMIN_KEY") or os.environ.get("DCHUB_INTERNAL_KEY")
    provided = (
        request.headers.get("X-Admin-Key")
        or request.headers.get("X-Internal-Key")
        or request.args.get("admin_key")
        or ""
    )
    return bool(expected) and provided == expected


def _num(v):
    try:
        f = float(v)
        return f if f == f else None  # drop NaN
    except (TypeError, ValueError):
        return None


def _body_rows():
    """Parse the (optionally gzipped) {"rows":[...]} request body → list[dict]."""
    raw = request.get_data() or b""
    if not raw:
        return []
    enc = (request.headers.get("Content-Encoding") or "").lower()
    if "gzip" in enc or request.headers.get("X-Content-Gzip"):
        raw = gzip.decompress(raw)
    j = json.loads(raw)
    if isinstance(j, dict) and isinstance(j.get("rows"), list):
        return j["rows"]
    return []


def _batch_insert(cur, table, fields, rows, src):
    cols = fields + ["source"]
    collist = ",".join(cols)
    ph = "(" + ",".join(["%s"] * len(cols)) + ")"
    inserted, batch = 0, []
    for r in rows:
        batch.append(tuple(r.get(f) for f in fields) + (src,))
        if len(batch) >= 500:
            args = b",".join(cur.mogrify(ph, b) for b in batch)
            cur.execute(f"INSERT INTO {table} ({collist}) VALUES " + args.decode())
            inserted += len(batch)
            batch = []
    if batch:
        args = b",".join(cur.mogrify(ph, b) for b in batch)
        cur.execute(f"INSERT INTO {table} ({collist}) VALUES " + args.decode())
        inserted += len(batch)
    return inserted


# ═══════════════════════════════════ POWER ═══════════════════════════════════
@gem_ingest_bp.route("/api/v1/admin/ingest/gem-power", methods=["POST"])
def ingest_gem_power():
    if not _admin_ok():
        return jsonify(ok=False, error="admin key required"), 401
    dsn = _dsn()
    if not dsn:
        return jsonify(ok=False, error="no DATABASE_URL"), 503
    dry = request.args.get("dry_run", "0") == "1"
    # append=1 lets the loader stream the 182k rows across multiple POSTs
    # (first call replaces, subsequent calls append within the same load).
    append = request.args.get("append", "0") == "1"

    try:
        raw = _body_rows()
    except Exception as e:
        return jsonify(ok=False, error=f"bad body: {str(e)[:120]}"), 400

    rows = []
    for r in raw:
        if not isinstance(r, dict) or (r.get("lat") in (None, "")) or (r.get("lng") in (None, "")):
            continue
        rows.append({
            "gem_id":     str(r.get("gem_id") or "")[:40],
            "fuel_type":  str(r.get("fuel_type") or "")[:40],
            "plant_name": str(r.get("plant_name") or "")[:250],
            "unit_name":  str(r.get("unit_name") or "")[:150],
            "capacity_mw": _num(r.get("capacity_mw")),
            "status":     str(r.get("status") or "")[:60],
            "start_year": _num(r.get("start_year")),
            "technology": str(r.get("technology") or "")[:120],
            "country":    str(r.get("country") or "")[:100],
            "region":     str(r.get("region") or "")[:80],
            "operator":   str(r.get("operator") or "")[:200],
            "owner":      str(r.get("owner") or "")[:200],
            "lat":        _num(r.get("lat")),
            "lng":        _num(r.get("lng")),
            "wiki_url":   str(r.get("wiki_url") or "")[:250],
        })

    if dry:
        return jsonify(ok=True, dry_run=True, received=len(rows), sample=rows[:3])
    if not rows:
        return jsonify(ok=False, error="0 valid geocoded rows provided — skipped to avoid wiping table"), 400

    inserted = 0
    try:
        with psycopg2.connect(dsn, sslmode="require", connect_timeout=8) as c:
            with c.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS gem_power (
                        id          SERIAL PRIMARY KEY,
                        gem_id      TEXT,
                        fuel_type   TEXT,
                        plant_name  TEXT,
                        unit_name   TEXT,
                        capacity_mw NUMERIC,
                        status      TEXT,
                        start_year  NUMERIC,
                        technology  TEXT,
                        country     TEXT,
                        region      TEXT,
                        operator    TEXT,
                        owner       TEXT,
                        lat         DOUBLE PRECISION,
                        lng         DOUBLE PRECISION,
                        wiki_url    TEXT,
                        source      TEXT,
                        ingested_at TIMESTAMPTZ DEFAULT NOW()
                    )
                """)
                cur.execute("CREATE INDEX IF NOT EXISTS ix_gempow_status ON gem_power(status)")
                cur.execute("CREATE INDEX IF NOT EXISTS ix_gempow_fuel ON gem_power(fuel_type)")
                cur.execute("CREATE INDEX IF NOT EXISTS ix_gempow_bbox ON gem_power(lng, lat)")
                if not append:
                    cur.execute("DELETE FROM gem_power WHERE source = %s", (_POWER_SRC,))
                inserted = _batch_insert(cur, "gem_power", _POWER_FIELDS, rows, _POWER_SRC)
            c.commit()
    except Exception as e:
        return jsonify(ok=False, error=str(e)[:200], inserted=inserted), 500
    return jsonify(ok=True, inserted=inserted, source=_POWER_SRC, appended=append)


@gem_ingest_bp.route("/api/v1/global-power", methods=["GET"])
def get_global_power():
    """PUBLIC read API — worldwide power units (GEM Global Integrated Power).

    Query params (all optional):
      bbox=minLng,minLat,maxLng,maxLat   viewport (the map sends this)
      fuel=coal,oil/gas,nuclear,...      Type filter (comma-separated, ILIKE-any)
      status=operating|construction|...  status filter (comma-separated, ILIKE-any)
      pipeline=1     shortcut for the FORWARD set (announced/pre-construction/construction)
      country=Germany
      min_mw=100
      limit=3000     (default 3000, max 8000)
      format=geojson (default) | json
    Returns geocoded rows only. Public, CC-BY GEM data.
    """
    dsn = _dsn()
    if not dsn:
        return jsonify(ok=False, error="no DATABASE_URL"), 503
    a = request.args
    where = ["source = %s", "lat IS NOT NULL", "lng IS NOT NULL"]
    params = [_POWER_SRC]
    bbox = a.get("bbox")
    if bbox:
        try:
            w, s, e, n = [float(x) for x in bbox.split(",")[:4]]
            where.append("lng BETWEEN %s AND %s AND lat BETWEEN %s AND %s")
            params += [min(w, e), max(w, e), min(s, n), max(s, n)]
        except (ValueError, IndexError):
            return jsonify(ok=False, error="bad bbox"), 400

    def _any_ilike(col, csv, cap=40):
        toks = [t.strip()[:cap] for t in str(csv).split(",") if t.strip()]
        if not toks:
            return
        where.append("(" + " OR ".join([f"{col} ILIKE %s"] * len(toks)) + ")")
        params.extend(["%" + t + "%" for t in toks])

    if a.get("pipeline") == "1":
        where.append("(status ILIKE %s OR status ILIKE %s OR status ILIKE %s)")
        params += ["%announced%", "%construction%", "%permit%"]
    elif a.get("status"):
        _any_ilike("status", a["status"], 30)
    if a.get("fuel"):
        _any_ilike("fuel_type", a["fuel"], 30)
    if a.get("country"):
        where.append("country ILIKE %s"); params.append("%" + a["country"][:80] + "%")
    if a.get("min_mw"):
        try:
            where.append("capacity_mw >= %s"); params.append(float(a["min_mw"]))
        except ValueError:
            pass
    try:
        limit = min(int(a.get("limit", 3000)), 8000)
    except (TypeError, ValueError):
        limit = 3000

    sql = ("SELECT gem_id, plant_name, unit_name, fuel_type, capacity_mw, status, "
           "start_year, country, operator, owner, wiki_url, lat, lng "
           "FROM gem_power WHERE " + " AND ".join(where) +
           " ORDER BY capacity_mw DESC NULLS LAST LIMIT %s")
    params.append(limit)
    try:
        with psycopg2.connect(dsn, sslmode="require", connect_timeout=8) as c:
            with c.cursor() as cur:
                cur.execute(sql, params)
                cols = [d[0] for d in cur.description]
                recs = [dict(zip(cols, r)) for r in cur.fetchall()]
                cur.execute("SELECT COUNT(*), MAX(ingested_at) FROM gem_power WHERE source=%s", (_POWER_SRC,))
                total, asof = cur.fetchone()
    except Exception as e:
        return jsonify(ok=False, error=str(e)[:160]), 500

    for r in recs:
        for k in ("capacity_mw", "start_year", "lat", "lng"):
            if r.get(k) is not None:
                r[k] = float(r[k])

    # `total` is COUNT(*) over gem_power for this source — it ignores EVERY
    # filter above, including the "lat IS NOT NULL AND lng IS NOT NULL" that
    # constrains the rows actually served. It is the table total, not the total
    # matching your query and not a count of geocoded rows. It is also a UNIT
    # count, not a plant count: gem_power rows carry both plant_name and
    # unit_name, and on the rows this endpoint serves distinct
    # (plant_name, country) runs ~1.27 rows per plant. And it spans ALL
    # statuses — a measured floor of 17,137 rows are cancelled, shelved,
    # retired or mothballed, so "operating + planned" does not describe it.
    # Anything republishing this number must carry those three qualifications.
    _basis = {
        "total_is_unfiltered": True,
        "total_counts": "generating UNITS, not plants",
        "total_includes_ungeocoded": True,
        "total_includes_all_statuses": True,
        "note": ("`total` is COUNT(*) over the whole gem_power source and "
                 "ignores every filter including the geocoding filter that "
                 "restricts the rows returned. Use `count` for the rows in "
                 "this response. Rows are generating units, not plants, and "
                 "span all statuses including cancelled / shelved / retired / "
                 "mothballed — do not publish it as 'plants operating + "
                 "planned'."),
    }
    if a.get("format") == "json":
        return jsonify(ok=True, count=len(recs), total=total, table_total=total,
                       total_basis=_basis,
                       as_of=str(asof) if asof else None,
                       source="GEM Global Integrated Power Tracker", units=recs)

    feats = [{
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [r["lng"], r["lat"]]},
        "properties": {k: v for k, v in r.items() if k not in ("lat", "lng")},
    } for r in recs]
    resp = jsonify({"type": "FeatureCollection", "count": len(feats), "total": total,
                    "table_total": total, "total_basis": _basis,
                    "as_of": str(asof) if asof else None,
                    "source": "GEM Global Integrated Power Tracker (CC-BY, via DC Hub)",
                    "features": feats})
    resp.headers["Cache-Control"] = "public, max-age=3600"
    return resp


# ═══════════════════════════════════ GAS ═════════════════════════════════════
@gem_ingest_bp.route("/api/v1/admin/ingest/gem-gas", methods=["POST"])
def ingest_gem_gas():
    if not _admin_ok():
        return jsonify(ok=False, error="admin key required"), 401
    dsn = _dsn()
    if not dsn:
        return jsonify(ok=False, error="no DATABASE_URL"), 503
    dry = request.args.get("dry_run", "0") == "1"
    try:
        raw = _body_rows()
    except Exception as e:
        return jsonify(ok=False, error=f"bad body: {str(e)[:120]}"), 400

    rows = []
    for r in raw:
        if not isinstance(r, dict) or (r.get("lat") in (None, "")) or (r.get("lng") in (None, "")):
            continue
        rows.append({
            "gem_id":         str(r.get("gem_id") or "")[:40],
            "kind":           str(r.get("kind") or "")[:30],   # lng_terminal | pipeline_point | ...
            "name":           str(r.get("name") or "")[:250],
            "unit_name":      str(r.get("unit_name") or "")[:150],
            "fuel":           str(r.get("fuel") or "")[:40],
            "capacity":       _num(r.get("capacity")),
            "capacity_units": str(r.get("capacity_units") or "")[:30],
            "status":         str(r.get("status") or "")[:60],
            "start_year":     _num(r.get("start_year")),
            "country":        str(r.get("country") or "")[:100],
            "region":         str(r.get("region") or "")[:80],
            "owner":          str(r.get("owner") or "")[:200],
            "lat":            _num(r.get("lat")),
            "lng":            _num(r.get("lng")),
            "wiki_url":       str(r.get("wiki_url") or "")[:250],
        })
    if dry:
        return jsonify(ok=True, dry_run=True, received=len(rows), sample=rows[:3])
    if not rows:
        return jsonify(ok=False, error="0 valid geocoded rows provided — skipped to avoid wiping table"), 400

    inserted = 0
    try:
        with psycopg2.connect(dsn, sslmode="require", connect_timeout=8) as c:
            with c.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS gem_gas (
                        id             SERIAL PRIMARY KEY,
                        gem_id         TEXT,
                        kind           TEXT,
                        name           TEXT,
                        unit_name      TEXT,
                        fuel           TEXT,
                        capacity       NUMERIC,
                        capacity_units TEXT,
                        status         TEXT,
                        start_year     NUMERIC,
                        country        TEXT,
                        region         TEXT,
                        owner          TEXT,
                        lat            DOUBLE PRECISION,
                        lng            DOUBLE PRECISION,
                        wiki_url       TEXT,
                        source         TEXT,
                        ingested_at    TIMESTAMPTZ DEFAULT NOW()
                    )
                """)
                cur.execute("CREATE INDEX IF NOT EXISTS ix_gemgas_kind ON gem_gas(kind)")
                cur.execute("CREATE INDEX IF NOT EXISTS ix_gemgas_bbox ON gem_gas(lng, lat)")
                cur.execute("DELETE FROM gem_gas WHERE source = %s", (_GAS_SRC,))
                inserted = _batch_insert(cur, "gem_gas", _GAS_FIELDS, rows, _GAS_SRC)
            c.commit()
    except Exception as e:
        return jsonify(ok=False, error=str(e)[:200], inserted=inserted), 500
    return jsonify(ok=True, inserted=inserted, source=_GAS_SRC)


@gem_ingest_bp.route("/api/v1/global-gas", methods=["GET"])
def get_global_gas():
    """PUBLIC read API — worldwide gas infrastructure (GEM GGIT: LNG terminals + points)."""
    dsn = _dsn()
    if not dsn:
        return jsonify(ok=False, error="no DATABASE_URL"), 503
    a = request.args
    where = ["source = %s", "lat IS NOT NULL", "lng IS NOT NULL"]
    params = [_GAS_SRC]
    bbox = a.get("bbox")
    if bbox:
        try:
            w, s, e, n = [float(x) for x in bbox.split(",")[:4]]
            where.append("lng BETWEEN %s AND %s AND lat BETWEEN %s AND %s")
            params += [min(w, e), max(w, e), min(s, n), max(s, n)]
        except (ValueError, IndexError):
            return jsonify(ok=False, error="bad bbox"), 400
    if a.get("kind"):
        where.append("kind ILIKE %s"); params.append("%" + a["kind"][:30] + "%")
    if a.get("country"):
        where.append("country ILIKE %s"); params.append("%" + a["country"][:80] + "%")
    try:
        limit = min(int(a.get("limit", 3000)), 8000)
    except (TypeError, ValueError):
        limit = 3000
    sql = ("SELECT gem_id, name, unit_name, kind, fuel, capacity, capacity_units, "
           "status, start_year, country, owner, wiki_url, lat, lng "
           "FROM gem_gas WHERE " + " AND ".join(where) +
           " ORDER BY capacity DESC NULLS LAST LIMIT %s")
    params.append(limit)
    try:
        with psycopg2.connect(dsn, sslmode="require", connect_timeout=8) as c:
            with c.cursor() as cur:
                cur.execute(sql, params)
                cols = [d[0] for d in cur.description]
                recs = [dict(zip(cols, r)) for r in cur.fetchall()]
                cur.execute("SELECT COUNT(*), MAX(ingested_at) FROM gem_gas WHERE source=%s", (_GAS_SRC,))
                total, asof = cur.fetchone()
    except Exception as e:
        return jsonify(ok=False, error=str(e)[:160]), 500
    for r in recs:
        for k in ("capacity", "start_year", "lat", "lng"):
            if r.get(k) is not None:
                r[k] = float(r[k])
    if a.get("format") == "json":
        return jsonify(ok=True, count=len(recs), total=total,
                       as_of=str(asof) if asof else None,
                       source="GEM Global Gas Infrastructure Tracker", assets=recs)
    feats = [{
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [r["lng"], r["lat"]]},
        "properties": {k: v for k, v in r.items() if k not in ("lat", "lng")},
    } for r in recs]
    resp = jsonify({"type": "FeatureCollection", "count": len(feats), "total": total,
                    "as_of": str(asof) if asof else None,
                    "source": "GEM Global Gas Infrastructure Tracker (CC-BY, via DC Hub)",
                    "features": feats})
    resp.headers["Cache-Control"] = "public, max-age=3600"
    return resp


@gem_ingest_bp.route("/api/v1/global-gas-pipelines", methods=["GET"])
def get_global_gas_pipelines():
    """PUBLIC — GEM gas-transmission pipeline geometry (LineString/MultiLineString/
    GeometryCollection). Viewport-filtered by each feature's stored bounding box
    (table gem_gas_pipelines is loaded directly from the GGIT GeoJSON with a
    precomputed min/max lng/lat per feature). Returns a GeoJSON FeatureCollection."""
    dsn = _dsn()
    if not dsn:
        return jsonify(ok=False, error="no DATABASE_URL"), 503
    a = request.args
    where = ["geom_json IS NOT NULL"]
    params = []
    bbox = a.get("bbox")
    if bbox:
        try:
            w, s, e, n = [float(x) for x in bbox.split(",")[:4]]
            where.append("min_lng <= %s AND max_lng >= %s AND min_lat <= %s AND max_lat >= %s")
            params += [max(w, e), min(w, e), max(s, n), min(s, n)]
        except (ValueError, IndexError):
            return jsonify(ok=False, error="bad bbox"), 400
    if a.get("status"):
        where.append("status ILIKE %s"); params.append("%" + a["status"][:30] + "%")
    if a.get("fuel"):
        # comma-separated ILIKE-any on the fuel column (gas vs oil/ngl split)
        _ftoks = [t.strip()[:20] for t in a["fuel"].split(",") if t.strip()]
        if _ftoks:
            where.append("(" + " OR ".join(["fuel ILIKE %s"] * len(_ftoks)) + ")")
            params.extend(["%" + t + "%" for t in _ftoks])
    try:
        limit = min(int(a.get("limit", 1500)), 4000)
    except (TypeError, ValueError):
        limit = 1500
    sql = ("SELECT project_id, name, segment, status, fuel, countries, owner, start_year, geom_json "
           "FROM gem_gas_pipelines WHERE " + " AND ".join(where) +
           " ORDER BY ((max_lng-min_lng)+(max_lat-min_lat)) DESC LIMIT %s")
    params.append(limit)
    try:
        with psycopg2.connect(dsn, sslmode="require", connect_timeout=8) as c:
            with c.cursor() as cur:
                cur.execute(sql, params)
                rows = cur.fetchall()
                cur.execute("SELECT COUNT(*), MAX(ingested_at) FROM gem_gas_pipelines")
                total, asof = cur.fetchone()
    except Exception as e:
        return jsonify(ok=False, error=str(e)[:160]), 500
    feats = []
    for pid, name, seg, status, fuel, countries, owner, yr, geom in rows:
        try:
            g = json.loads(geom) if geom else None
        except Exception:
            g = None
        if not g:
            continue
        feats.append({"type": "Feature", "geometry": g, "properties": {
            "project_id": pid, "name": name, "segment": seg, "status": status,
            "fuel": fuel, "countries": countries, "owner": owner,
            "start_year": float(yr) if yr is not None else None}})
    resp = jsonify({"type": "FeatureCollection", "count": len(feats), "total": total,
                    "as_of": str(asof) if asof else None,
                    "source": "GEM Global Gas Infrastructure Tracker — pipelines (CC-BY, via DC Hub)",
                    "features": feats})
    resp.headers["Cache-Control"] = "public, max-age=3600"
    return resp


@gem_ingest_bp.route("/api/v1/global-coal-mines", methods=["GET"])
def get_global_coal_mines():
    """PUBLIC — GEM Coal Mine Boundaries & Methane Sources (mixed Polygon boundaries +
    Point methane features: degasification / ventilation / gas wells / vents / flares).
    Viewport-filtered by each feature's stored bounding box (table gem_coal_mines).
    Returns a GeoJSON FeatureCollection."""
    dsn = _dsn()
    if not dsn:
        return jsonify(ok=False, error="no DATABASE_URL"), 503
    a = request.args
    where = ["geom_json IS NOT NULL"]
    params = []
    bbox = a.get("bbox")
    if bbox:
        try:
            w, s, e, n = [float(x) for x in bbox.split(",")[:4]]
            where.append("min_lng <= %s AND max_lng >= %s AND min_lat <= %s AND max_lat >= %s")
            params += [max(w, e), min(w, e), max(s, n), min(s, n)]
        except (ValueError, IndexError):
            return jsonify(ok=False, error="bad bbox"), 400
    if a.get("category"):
        where.append("category ILIKE %s"); params.append("%" + a["category"][:40] + "%")
    if a.get("country"):
        where.append("country ILIKE %s"); params.append("%" + a["country"][:80] + "%")
    try:
        limit = min(int(a.get("limit", 2500)), 6000)
    except (TypeError, ValueError):
        limit = 2500
    # boundaries (polygons) first so they draw under the point features
    sql = ("SELECT gem_mine_id, mine_name, category, subcategory, coal_grade, owners, "
           "parent, country, wiki, geom_json "
           "FROM gem_coal_mines WHERE " + " AND ".join(where) +
           " ORDER BY (category='mine boundary') DESC LIMIT %s")
    params.append(limit)
    try:
        with psycopg2.connect(dsn, sslmode="require", connect_timeout=8) as c:
            with c.cursor() as cur:
                cur.execute(sql, params)
                rows = cur.fetchall()
                cur.execute("SELECT COUNT(*), COUNT(DISTINCT gem_mine_id), MAX(ingested_at) FROM gem_coal_mines")
                total, mines, asof = cur.fetchone()
    except Exception as e:
        return jsonify(ok=False, error=str(e)[:160]), 500
    feats = []
    for mid, name, cat, sub, grade, owners, parent, country, wiki, geom in rows:
        try:
            g = json.loads(geom) if geom else None
        except Exception:
            g = None
        if not g:
            continue
        feats.append({"type": "Feature", "geometry": g, "properties": {
            "mine_id": mid, "mine_name": name, "category": cat, "subcategory": sub,
            "coal_grade": grade, "owners": owners, "parent": parent,
            "country": country, "wiki": wiki}})
    resp = jsonify({"type": "FeatureCollection", "count": len(feats), "total": total,
                    "distinct_mines": mines, "as_of": str(asof) if asof else None,
                    "source": "GEM Coal Mine Boundaries & Methane Sources (CC-BY, via DC Hub)",
                    "features": feats})
    resp.headers["Cache-Control"] = "public, max-age=3600"
    return resp


def register_gem_ingest(app):
    """Idempotent registration helper (mirrors the other ingest blueprints)."""
    try:
        app.register_blueprint(gem_ingest_bp)
    except Exception as e:
        log.warning(f"gem_ingest registration: {e}")
