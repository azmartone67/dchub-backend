"""Planned-generators ingest — EIA-860M "Planned" sheet → planned_generators.

The forward pipeline of NEW power capacity coming online: planned, permitting,
and under-construction generators NATIONWIDE — including the non-ISO regions
(TVA, Southern Co, Arizona PS, PacifiCorp, LADWP, …) that the per-ISO
interconnection-queue feed doesn't cover. Each row has coordinates, state,
county, balancing authority, technology, nameplate MW, status, and planned
operation month/year — i.e. a plottable "where + when new power lands" layer.

Source: EIA-860M (Electric Generator Inventory, monthly) Excel "Planned" sheet
(api has no planned route; the Excel does). The Excel is 13MB so the FETCH runs
on the GitHub runner (tools/infra_fetch.py downloads + parses with openpyxl and
POSTs compact rows) — this endpoint only does fast DB writes.

Safety: admin-gated, idempotent (source='eia860m_planned', deletes own rows),
empty-replace guard, transaction-wrapped, ?dry_run=1 echoes without writing.
"""
import gzip
import json
import logging
import os

import psycopg2
from flask import Blueprint, jsonify, request

log = logging.getLogger("planned_generators_ingest")
planned_gen_ingest_bp = Blueprint("planned_generators_ingest", __name__)

_SRC = "eia860m_planned"
_FIELDS = ["entity_name", "plant_id", "plant_name", "state", "county", "ba_code",
           "generator_id", "technology", "energy_source", "capacity_mw",
           "status", "planned_month", "planned_year", "lat", "lng"]


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
        return float(v)
    except (TypeError, ValueError):
        return None


@planned_gen_ingest_bp.route("/api/v1/admin/ingest/planned-generators", methods=["POST"])
def ingest_planned_generators():
    if not _admin_ok():
        return jsonify(ok=False, error="admin key required"), 401
    dsn = _dsn()
    if not dsn:
        return jsonify(ok=False, error="no DATABASE_URL"), 503

    dry = request.args.get("dry_run", "0") == "1"

    # Rows are provided by the GitHub runner (Excel parse): gzipped {"rows":[{...}]}.
    rows = []
    raw = request.get_data() or b""
    if raw:
        try:
            enc = (request.headers.get("Content-Encoding") or "").lower()
            if "gzip" in enc or request.headers.get("X-Content-Gzip"):
                raw = gzip.decompress(raw)
            j = json.loads(raw)
            if isinstance(j, dict) and isinstance(j.get("rows"), list):
                for r in j["rows"]:
                    if isinstance(r, dict) and r.get("plant_id"):
                        rows.append({
                            "entity_name":  str(r.get("entity_name") or "")[:200],
                            "plant_id":     str(r.get("plant_id") or "")[:20],
                            "plant_name":   str(r.get("plant_name") or "")[:200],
                            "state":        str(r.get("state") or "")[:2],
                            "county":       str(r.get("county") or "")[:100],
                            "ba_code":      str(r.get("ba_code") or "")[:20],
                            "generator_id": str(r.get("generator_id") or "")[:20],
                            "technology":   str(r.get("technology") or "")[:100],
                            "energy_source": str(r.get("energy_source") or "")[:20],
                            "capacity_mw":  _num(r.get("capacity_mw")),
                            "status":       str(r.get("status") or "")[:120],
                            "planned_month": _num(r.get("planned_month")),
                            "planned_year": _num(r.get("planned_year")),
                            "lat":          _num(r.get("lat")),
                            "lng":          _num(r.get("lng")),
                        })
        except Exception as e:
            return jsonify(ok=False, error=f"bad body: {str(e)[:120]}"), 400

    if dry:
        return jsonify(ok=True, dry_run=True, received=len(rows), sample=rows[:3])
    if not rows:
        return jsonify(ok=False, error="0 rows provided (runner parses the EIA-860M Planned sheet and POSTs them) — skipped to avoid wiping table"), 400

    inserted = 0
    try:
        with psycopg2.connect(dsn, sslmode="require", connect_timeout=8) as c:
            with c.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS planned_generators (
                        id            SERIAL PRIMARY KEY,
                        entity_name   TEXT,
                        plant_id      TEXT,
                        plant_name    TEXT,
                        state         TEXT,
                        county        TEXT,
                        ba_code       TEXT,
                        generator_id  TEXT,
                        technology    TEXT,
                        energy_source TEXT,
                        capacity_mw   NUMERIC,
                        status        TEXT,
                        planned_month NUMERIC,
                        planned_year  NUMERIC,
                        lat           DOUBLE PRECISION,
                        lng           DOUBLE PRECISION,
                        source        TEXT,
                        ingested_at   TIMESTAMPTZ DEFAULT NOW()
                    )
                """)
                cur.execute("CREATE INDEX IF NOT EXISTS ix_plangen_state ON planned_generators(state)")
                cur.execute("CREATE INDEX IF NOT EXISTS ix_plangen_ba ON planned_generators(ba_code)")
                cur.execute("DELETE FROM planned_generators WHERE source = %s", (_SRC,))

                cols = _FIELDS + ["source"]
                collist = ",".join(cols)
                ph = "(" + ",".join(["%s"] * len(cols)) + ")"

                def _vals(r):
                    return tuple(r.get(f) for f in _FIELDS) + (_SRC,)

                batch = []
                for r in rows:
                    batch.append(_vals(r))
                    if len(batch) >= 500:
                        args = b",".join(cur.mogrify(ph, b) for b in batch)
                        cur.execute(f"INSERT INTO planned_generators ({collist}) VALUES " + args.decode())
                        inserted += len(batch)
                        batch = []
                if batch:
                    args = b",".join(cur.mogrify(ph, b) for b in batch)
                    cur.execute(f"INSERT INTO planned_generators ({collist}) VALUES " + args.decode())
                    inserted += len(batch)
            c.commit()
    except Exception as e:
        return jsonify(ok=False, error=str(e)[:200], inserted=inserted), 500

    return jsonify(ok=True, inserted=inserted, source=_SRC)


@planned_gen_ingest_bp.route("/api/v1/planned-generators", methods=["GET"])
def get_planned_generators():
    """PUBLIC read API for the planned-generator pipeline (map + MCP consume this).

    Query params (all optional):
      bbox=minLng,minLat,maxLng,maxLat   spatial filter (the map sends the viewport)
      state=TX        2-letter state
      ba=ERCO         balancing-authority code
      status=U        EIA status code prefix (U/V=under construction, P/L/T=planned, TS)
      min_mw=50       minimum nameplate MW
      limit=2000      cap (default 2000, max 5000)
      format=geojson  (default) GeoJSON FeatureCollection; format=json → flat array

    Returns only geocoded rows. Public, read-only, federal data (EIA-860M).
    """
    dsn = _dsn()
    if not dsn:
        return jsonify(ok=False, error="no DATABASE_URL"), 503
    args = request.args
    where = ["source = %s", "lat IS NOT NULL", "lng IS NOT NULL"]
    params = [_SRC]
    bbox = args.get("bbox")
    if bbox:
        try:
            w, s, e, n = [float(x) for x in bbox.split(",")[:4]]
            where.append("lng BETWEEN %s AND %s AND lat BETWEEN %s AND %s")
            params += [min(w, e), max(w, e), min(s, n), max(s, n)]
        except (ValueError, IndexError):
            return jsonify(ok=False, error="bad bbox (want minLng,minLat,maxLng,maxLat)"), 400
    if args.get("state"):
        where.append("UPPER(state) = %s"); params.append(args["state"].upper()[:2])
    if args.get("ba"):
        where.append("UPPER(ba_code) = %s"); params.append(args["ba"].upper()[:20])
    if args.get("status"):
        where.append("status ILIKE %s"); params.append("(" + args["status"][:3] + "%")
    if args.get("min_mw"):
        try:
            where.append("capacity_mw >= %s"); params.append(float(args["min_mw"]))
        except ValueError:
            pass
    try:
        limit = min(int(args.get("limit", 2000)), 5000)
    except (TypeError, ValueError):
        limit = 2000

    sql = (
        "SELECT plant_name, entity_name, state, county, ba_code, technology, "
        "capacity_mw, status, planned_month, planned_year, lat, lng "
        "FROM planned_generators WHERE " + " AND ".join(where) +
        " ORDER BY capacity_mw DESC NULLS LAST LIMIT %s"
    )
    params.append(limit)
    try:
        with psycopg2.connect(dsn, sslmode="require", connect_timeout=8) as c:
            with c.cursor() as cur:
                cur.execute(sql, params)
                cols = [d[0] for d in cur.description]
                recs = [dict(zip(cols, r)) for r in cur.fetchall()]
                cur.execute("SELECT MAX(ingested_at) FROM planned_generators WHERE source=%s", (_SRC,))
                asof = cur.fetchone()[0]
    except Exception as e:
        return jsonify(ok=False, error=str(e)[:160]), 500

    for r in recs:
        for k in ("capacity_mw", "planned_month", "planned_year", "lat", "lng"):
            if r.get(k) is not None:
                r[k] = float(r[k])

    if args.get("format") == "json":
        return jsonify(ok=True, count=len(recs), as_of=str(asof) if asof else None,
                       source="EIA-860M planned generators", generators=recs)

    # Default: GeoJSON FeatureCollection (Leaflet-native)
    feats = []
    for r in recs:
        feats.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [r["lng"], r["lat"]]},
            "properties": {k: v for k, v in r.items() if k not in ("lat", "lng")},
        })
    resp = jsonify({"type": "FeatureCollection", "count": len(feats),
                    "as_of": str(asof) if asof else None,
                    "source": "EIA-860M planned generators (DC Hub)",
                    "features": feats})
    resp.headers["Cache-Control"] = "public, max-age=3600"
    return resp


def register_planned_generators_ingest(app):
    """Idempotent registration helper."""
    try:
        app.register_blueprint(planned_gen_ingest_bp)
    except Exception as e:
        log.warning(f"planned_generators_ingest registration: {e}")
