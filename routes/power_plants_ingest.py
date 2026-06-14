"""Power plant ingest — EIA Power Plants in the US → power_plants_eia.

The Land & Power "power plants" layer FROZE: power_plants_eia held a stale EIA-860
snapshot (13,446 rows, source='eia') with no refresh path. Railway's egress to
ArcGIS is unreliable, so — exactly like the proven gas-pipeline ingest — a GitHub
Actions runner (tools/infra_fetch.py) fetches the live EIA service and POSTs
compact rows here, which writes Neon.

Source: EIA Power_Plants_in_the_US (FiaPA4ga0iQKduv3 org, ~13,446 features).
returnGeometry=true, outSR=4326 (point lat/lng). Fields: Plant_Code, Plant_Name,
Utility_Na, sector_nam, City, County, State, Zip, PrimSource, Total_MW, Install_MW.

Safety:
  - Admin-gated (X-Admin-Key / X-Internal-Key).
  - Idempotent FULL-REPLACE inside ONE transaction: deletes the prior EIA snapshot
    + runner rows, then batched INSERT — same 13,446 plants, refreshed, so the
    table is NOT duplicated. A schema surprise rolls back cleanly (old rows survive,
    no empty-layer state).
  - Dynamic column detection (only insert columns that exist live).
  - plant_id is INTEGER: cast Plant_Code, skip non-int.
  - Capped + batched (500/exec via cur.mogrify).
  - ?dry_run=1 fetches + parses without writing.
  - Accepts POST body {"rows":[...]} (preferred, runner-provided) OR a server-side
    _fetch fallback.
"""
import json
import logging
import os
import urllib.parse
import urllib.request

import psycopg2
from flask import Blueprint, jsonify, request

log = logging.getLogger("power_plants_ingest")
power_plants_ingest_bp = Blueprint("power_plants_ingest", __name__)

_SRC = "eia-arcgis-runner"
# Sources superseded by this full-replace (the prior EIA snapshot + runner rows).
_DELETE_SOURCES = ("eia-arcgis-runner", "eia", "eia_860", "eia860", "eia-860")
# EIA Power Plants in the US (national, ~13,446 features). Same reliable
# FiaPA4ga0iQKduv3 org the working gas ingest uses. Needs point geometry.
_SVC = ("https://services2.arcgis.com/FiaPA4ga0iQKduv3/arcgis/rest/services/"
        "Power_Plants_in_the_US/FeatureServer/0/query")

# Row tuple shape (matches tools/infra_fetch.fetch_power_plants):
#   (plant_id, name, utility_name, state, city, county, zipcode,
#    lat, lng, primary_fuel, nameplate_capacity_mw, sector)
_ROW_FIELDS = ("plant_id", "name", "utility_name", "state", "city", "county",
               "zipcode", "lat", "lng", "primary_fuel",
               "nameplate_capacity_mw", "sector")
# Columns that are duplicated from another field per the mapping spec.
#   plant_name      = name
#   primary_fuel    = energy_source
#   nameplate_capacity_mw = capacity_mw
_DUP_FROM = {
    "plant_name": "name",
    "energy_source": "primary_fuel",
    "capacity_mw": "nameplate_capacity_mw",
}


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


def _int(v):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def _float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _clean(s, n):
    if s is None:
        return None
    s = str(s).strip()
    if not s:
        return None
    return s[:n]


def _point(geom: dict):
    try:
        x, y = geom.get("x"), geom.get("y")
        if x is None or y is None:
            return None, None
        return float(y), float(x)  # lat, lng
    except (TypeError, ValueError):
        return None, None


def _fetch(cap: int):
    """Paginate the EIA power-plants service → list of row tuples (see _ROW_FIELDS).

    Needs point geometry (returnGeometry=true, outSR=4326). Pages of 2000
    (service maxRecordCount). Skips features with a non-integer Plant_Code."""
    rows = []
    offset = 0
    page = 2000
    while len(rows) < cap:
        params = urllib.parse.urlencode({
            "where": "1=1",
            "outFields": ("Plant_Code,Plant_Name,Utility_Na,sector_nam,City,"
                          "County,State,Zip,PrimSource,Total_MW"),
            "returnGeometry": "true",
            "outSR": "4326",
            "resultOffset": offset,
            "resultRecordCount": page,
            "f": "json",
        })
        with urllib.request.urlopen(_SVC + "?" + params, timeout=55) as r:
            data = json.loads(r.read().decode())
        feats = data.get("features") or []
        if not feats:
            break
        for f in feats:
            a = f.get("attributes") or {}
            pid = _int(a.get("Plant_Code"))
            if pid is None:
                continue  # plant_id is INTEGER NOT NULL-ish identity; skip junk
            lat, lng = _point(f.get("geometry") or {})
            rows.append((
                pid,
                _clean(a.get("Plant_Name"), 200),
                _clean(a.get("Utility_Na"), 200),
                _clean(a.get("State"), 80),
                _clean(a.get("City"), 120),
                _clean(a.get("County"), 120),
                _clean(a.get("Zip"), 20),
                lat,
                lng,
                _clean(a.get("PrimSource"), 80),
                _float(a.get("Total_MW")),
                _clean(a.get("sector_nam"), 120),
            ))
        offset += page
        if len(feats) < page:
            break
    return rows[:cap]


def _coerce_body_row(r):
    """Normalize a runner-provided row → _ROW_FIELDS tuple; drop non-int plant_id."""
    if not isinstance(r, (list, tuple)) or len(r) < 1:
        return None
    g = lambda i: r[i] if len(r) > i else None
    pid = _int(g(0))
    if pid is None:
        return None
    return (
        pid,
        _clean(g(1), 200),
        _clean(g(2), 200),
        _clean(g(3), 80),
        _clean(g(4), 120),
        _clean(g(5), 120),
        _clean(g(6), 20),
        _float(g(7)),
        _float(g(8)),
        _clean(g(9), 80),
        _float(g(10)),
        _clean(g(11), 120),
    )


@power_plants_ingest_bp.route("/api/v1/admin/ingest/power-plants", methods=["POST"])
def ingest_power_plants():
    if not _admin_ok():
        return jsonify(ok=False, error="admin key required"), 401
    dsn = _dsn()
    if not dsn:
        return jsonify(ok=False, error="no DATABASE_URL"), 503

    dry = request.args.get("dry_run", "0") == "1"
    try:
        cap = min(int(request.args.get("cap", 20000)), 50000)
    except (TypeError, ValueError):
        cap = 20000

    # Runner-provided rows (preferred). Body: {"rows":[[plant_id,name,utility_name,
    # state,city,county,zipcode,lat,lng,primary_fuel,nameplate_capacity_mw,sector],
    # ...]}, optionally gzipped. Falls back to server-side _fetch.
    body_rows = None
    raw = request.get_data() or b""
    if raw:
        try:
            enc = (request.headers.get("Content-Encoding") or "").lower()
            if "gzip" in enc or request.headers.get("X-Content-Gzip"):
                import gzip as _gz
                raw = _gz.decompress(raw)
            j = json.loads(raw)
            if isinstance(j, dict) and isinstance(j.get("rows"), list):
                body_rows = []
                for r in j["rows"]:
                    cr = _coerce_body_row(r)
                    if cr is not None:
                        body_rows.append(cr)
        except Exception:
            body_rows = None

    if body_rows is not None:
        rows = body_rows[:cap] if request.args.get("cap") else body_rows
    else:
        try:
            rows = _fetch(cap)
        except Exception as e:
            return jsonify(ok=False, error=f"source fetch failed: {str(e)[:160]}"), 502

    if dry:
        return jsonify(ok=True, dry_run=True, fetched=len(rows), sample=rows[:3])

    if not rows:
        return jsonify(ok=False, error="no rows to ingest (refused empty full-replace)"), 400

    inserted = 0
    try:
        with psycopg2.connect(dsn, sslmode="require", connect_timeout=8) as c:
            with c.cursor() as cur:
                cur.execute("SELECT column_name, character_maximum_length "
                            "FROM information_schema.columns "
                            "WHERE table_name='power_plants_eia'")
                _colrows = cur.fetchall()
                cols = {r[0] for r in _colrows}
                # Per-column varchar widths — defensive truncation so a single
                # over-long source value (e.g. EIA's full state name vs the
                # varchar(10) state column) can never 500 the whole batch.
                colmax = {r[0]: r[1] for r in _colrows if r[1]}

                # Idempotent full-replace: clear prior EIA snapshot + runner rows.
                if "source" in cols:
                    cur.execute(
                        "DELETE FROM power_plants_eia WHERE source IN %s",
                        (tuple(_DELETE_SOURCES),))

                use = [col for col in _ROW_FIELDS if col in cols]
                # Duplicated columns (plant_name/energy_source/capacity_mw) when present.
                dup = [col for col in _DUP_FROM if col in cols and _DUP_FROM[col] in _ROW_FIELDS]
                if "source" in cols:
                    use_tail_source = True
                else:
                    use_tail_source = False
                all_cols = use + dup + (["source"] if use_tail_source else [])
                collist = ",".join(all_cols)
                ph = "(" + ",".join(["%s"] * len(all_cols)) + ")"

                def _fit(col, v):
                    w = colmax.get(col)
                    if w and isinstance(v, str) and len(v) > w:
                        return v[:w]
                    return v

                def _row_vals(t):
                    m = dict(zip(_ROW_FIELDS, t))
                    vals = [_fit(col, m[col]) for col in use]
                    vals += [_fit(col, m[_DUP_FROM[col]]) for col in dup]
                    if use_tail_source:
                        vals.append(_fit("source", _SRC))
                    return tuple(vals)

                batch = []
                for t in rows:
                    batch.append(_row_vals(t))
                    if len(batch) >= 500:
                        args = b",".join(cur.mogrify(ph, b) for b in batch)
                        cur.execute(f"INSERT INTO power_plants_eia ({collist}) "
                                    f"VALUES " + args.decode())
                        inserted += len(batch)
                        batch = []
                if batch:
                    args = b",".join(cur.mogrify(ph, b) for b in batch)
                    cur.execute(f"INSERT INTO power_plants_eia ({collist}) "
                                f"VALUES " + args.decode())
                    inserted += len(batch)
            c.commit()
    except Exception as e:
        return jsonify(ok=False, error=str(e)[:200], inserted=inserted), 500

    return jsonify(ok=True, inserted=inserted, source=_SRC)


def register_power_plants_ingest(app):
    """Idempotent registration helper."""
    try:
        app.register_blueprint(power_plants_ingest_bp)
    except Exception as e:
        log.warning(f"power_plants_ingest registration: {e}")
