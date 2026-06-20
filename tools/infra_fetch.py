#!/usr/bin/env python3
"""Fetch infrastructure layers from external sources and push to DC Hub.

Runs on the GitHub Actions runner (reliable egress), NOT on Railway —
Railway's network can't reach several infra sources (geo.dot.gov for gas
pipelines times out; the daily server-side ingest fails every run). The
runner fetches the source, builds compact rows, and POSTs them to the
backend's ingest endpoint, which writes to Neon. Railway never fetches.

Env:
  DCHUB_ADMIN_KEY   backend admin key (required)
  DCHUB_ORIGIN      backend origin (default: Railway production)

Usage:
  python tools/infra_fetch.py gas-pipelines        # default cap
  python tools/infra_fetch.py gas-pipelines 30000  # explicit cap
"""
import gzip
import json
import os
import sys
import time
import urllib.parse
import urllib.request

ORIGIN = os.environ.get("DCHUB_ORIGIN", "https://dchub-backend-production.up.railway.app").rstrip("/")
ADMIN = "".join((os.environ.get("DCHUB_ADMIN_KEY") or "").split())

# EIA Natural Gas Interstate + Intrastate Pipelines (national, ~32,892
# polylines). The old geo.dot.gov service died 2026-06 (its backend DB
# refuses connections → 500) and the HIFLD Hp6G80Pky0om7QvQ copy was
# deleted (400) — this FEMA-published EIA org service (FiaPA4ga0iQKduv3)
# is the live replacement. Fields: Operator, TYPEPIPE, Status.
GAS_SVC = ("https://services2.arcgis.com/FiaPA4ga0iQKduv3/arcgis/rest/services/"
           "Natural_Gas_Interstate_and_Intrastate_Pipelines_1/FeatureServer/0/query")

# EIA Electric Power Transmission Lines (national, ~94,619 polylines). Same
# reliable EIA org (FiaPA4ga0iQKduv3). Attributes only — the transmission_lines
# table stores no geometry — so returnGeometry=false (94k full geometries would
# be huge & needless). Fields: ID, TYPE, STATUS, OWNER, VOLTAGE, SUB_1, SUB_2.
# This 94k set supersedes the stale HIFLD 52k snapshot (clean single source).
TRANSMISSION_SVC = ("https://services2.arcgis.com/FiaPA4ga0iQKduv3/arcgis/rest/"
                    "services/US_Electric_Power_Transmission_Lines/FeatureServer/0/query")

# EIA Power Plants in the US (national, ~13,446 points). Same EIA org. Needs
# point geometry (returnGeometry=true, outSR=4326) for lat/lng. Fields:
# Plant_Code, Plant_Name, Utility_Na, sector_nam, City, County, State, Zip,
# PrimSource, Total_MW. Refreshes the same 13,446 plants (no duplication).
POWER_PLANTS_SVC = ("https://services2.arcgis.com/FiaPA4ga0iQKduv3/arcgis/rest/"
                    "services/Power_Plants_in_the_US/FeatureServer/0/query")


def _get(url, timeout=90, tries=5):
    last = None
    for a in range(tries):
        try:
            with urllib.request.urlopen(url, timeout=timeout) as r:
                return r.read()
        except Exception as e:
            last = e
            print(f"    fetch attempt {a+1}/{tries}: {str(e)[:90]}", flush=True)
            time.sleep(min(2 * (a + 1), 12))
    raise last


def _first_point(geom):
    try:
        paths = geom.get("paths") or []
        if paths and paths[0]:
            x, y = paths[0][0][0], paths[0][0][1]
            return float(y), float(x)  # lat, lng
    except Exception:
        pass
    return None, None


def fetch_gas_pipelines(cap):
    """Paginate the geo.dot.gov gas-pipeline service → [[lat,lng,operator,type],...]."""
    rows, offset, page = [], 0, 2000   # service maxRecordCount = 2000
    while len(rows) < cap:
        params = urllib.parse.urlencode({
            "where": "1=1",
            "outFields": "Operator,TYPEPIPE,Status",
            "returnGeometry": "true",
            "outSR": "4326",
            "maxAllowableOffset": "0.05",   # simplify; we keep 1 vertex/line
            "geometryPrecision": "4",
            "resultOffset": offset,
            "resultRecordCount": page,
            "f": "json",
        })
        data = json.loads(_get(GAS_SVC + "?" + params).decode())
        feats = data.get("features") or []
        if not feats:
            break
        for f in feats:
            lat, lng = _first_point(f.get("geometry") or {})
            if lat is None:
                continue
            a = f.get("attributes") or {}
            rows.append([lat, lng, (a.get("Operator") or "")[:200], (a.get("TYPEPIPE") or "")[:80]])
        offset += page
        if len(feats) < page:
            break
    return rows[:cap]


def _v_voltage(v):
    """VOLTAGE is kV int; -999999 / negatives are 'not available' sentinels."""
    try:
        f = float(v)
        return f if f > 0 else None
    except (TypeError, ValueError):
        return None


def _v_clean(s, n):
    if s is None:
        return None
    s = str(s).strip()
    if not s or s.upper() in ("NOT AVAILABLE", "UNKNOWN", "NULL", "NONE"):
        return None
    return s[:n]


# EIA's power-plant service returns the FULL state name ("Mississippi"), but the
# power_plants_eia.state column is varchar(10) and the rest of the schema stores
# 2-letter USPS codes — so a full name >10 chars 500'd the whole batch insert.
# Convert to the postal code (fall back to a 10-char truncation for anything
# unrecognized so the insert can never overflow).
_US_STATE2 = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
    "california": "CA", "colorado": "CO", "connecticut": "CT", "delaware": "DE",
    "district of columbia": "DC", "florida": "FL", "georgia": "GA", "hawaii": "HI",
    "idaho": "ID", "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS",
    "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN", "mississippi": "MS",
    "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV",
    "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM", "new york": "NY",
    "north carolina": "NC", "north dakota": "ND", "ohio": "OH", "oklahoma": "OK",
    "oregon": "OR", "pennsylvania": "PA", "rhode island": "RI",
    "south carolina": "SC", "south dakota": "SD", "tennessee": "TN", "texas": "TX",
    "utah": "UT", "vermont": "VT", "virginia": "VA", "washington": "WA",
    "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY",
    "puerto rico": "PR", "guam": "GU", "virgin islands": "VI",
}


def _us_state_code(v):
    """Full state name -> 2-letter USPS code (varchar(10)-safe)."""
    s = _v_clean(v, 80)
    if s is None:
        return None
    return _US_STATE2.get(s.lower(), s[:10])


def fetch_transmission_lines(cap):
    """Paginate the EIA transmission service → row tuples for the ingest endpoint.

    Attributes only (returnGeometry=false). Row shape (matches
    routes/transmission_ingest._ROW_FIELDS):
      [hifld_id, name, operator, voltage_kv, from_sub, to_sub, status, line_type]
    """
    rows, offset, page = [], 0, 2000   # service maxRecordCount = 2000
    while len(rows) < cap:
        params = urllib.parse.urlencode({
            "where": "1=1",
            "outFields": "ID,TYPE,STATUS,OWNER,VOLTAGE,SUB_1,SUB_2",
            "returnGeometry": "false",
            "resultOffset": offset,
            "resultRecordCount": page,
            "f": "json",
        })
        data = json.loads(_get(TRANSMISSION_SVC + "?" + params).decode())
        feats = data.get("features") or []
        if not feats:
            break
        for f in feats:
            a = f.get("attributes") or {}
            hid = _v_clean(a.get("ID"), 64)
            owner = _v_clean(a.get("OWNER"), 200)
            name = owner or hid
            rows.append([
                hid,
                name[:200] if name else None,
                owner,
                _v_voltage(a.get("VOLTAGE")),
                _v_clean(a.get("SUB_1"), 200),
                _v_clean(a.get("SUB_2"), 200),
                _v_clean(a.get("STATUS"), 80),
                _v_clean(a.get("TYPE"), 80),
            ])
        offset += page
        if len(feats) < page:
            break
    return rows[:cap]


def fetch_power_plants(cap):
    """Paginate the EIA power-plants service → row tuples for the ingest endpoint.

    Needs point geometry (returnGeometry=true, outSR=4326). Skips non-integer
    Plant_Code. Row shape (matches routes/power_plants_ingest._ROW_FIELDS):
      [plant_id, name, utility_name, state, city, county, zipcode,
       lat, lng, primary_fuel, nameplate_capacity_mw, sector]
    """
    def _vint(v):
        try:
            return int(float(v))
        except (TypeError, ValueError):
            return None

    def _vfloat(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    rows, offset, page = [], 0, 2000   # service maxRecordCount = 2000
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
        data = json.loads(_get(POWER_PLANTS_SVC + "?" + params).decode())
        feats = data.get("features") or []
        if not feats:
            break
        for f in feats:
            a = f.get("attributes") or {}
            pid = _vint(a.get("Plant_Code"))
            if pid is None:
                continue
            g = f.get("geometry") or {}
            x, y = g.get("x"), g.get("y")
            lat = _vfloat(y)
            lng = _vfloat(x)
            rows.append([
                pid,
                _v_clean(a.get("Plant_Name"), 200),
                _v_clean(a.get("Utility_Na"), 200),
                _us_state_code(a.get("State")),
                _v_clean(a.get("City"), 100),
                _v_clean(a.get("County"), 100),
                _v_clean(a.get("Zip"), 20),
                lat,
                lng,
                _v_clean(a.get("PrimSource"), 80),
                _vfloat(a.get("Total_MW")),
                _v_clean(a.get("sector_nam"), 120),
            ])
        offset += page
        if len(feats) < page:
            break
    return rows[:cap]


def post_rows(path, rows, cap):
    payload = {"rows": rows}
    body = gzip.compress(json.dumps(payload).encode("utf-8"))
    url = f"{ORIGIN}{path}?cap={cap}"
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "X-Admin-Key": ADMIN, "Content-Type": "application/json",
        "Content-Encoding": "gzip"})
    with urllib.request.urlopen(req, timeout=180) as r:
        return getattr(r, "status", r.getcode()), json.loads(r.read()), len(body)


# EIA-860M operating-generator-capacity (monthly JSON v2 API, not ArcGIS) —
# OPERABLE generator inventory by balancing authority = grid-capacity signal
# ("which grids hold how much of each generation type, incl. standby/reserve").
# EIA's v2 API has NO planned/under-construction route, so this is the operable
# snapshot (the forward queue lives in interconnect_queue). Needs EIA_API_KEY.
# Returns dict rows; the ingest endpoint accepts named fields. Latest period only.
EIA_KEY = os.environ.get("EIA_API_KEY") or os.environ.get("EIA_KEY") or ""
EIA_GEN = "https://api.eia.gov/v2/electricity/operating-generator-capacity/data/"
# OP=operating, SB=standby/reserve, OA=out-of-service but expected back <1yr.
_INVENTORY_STATUS = ["OP", "SB", "OA"]


def fetch_generator_inventory(cap):
    """Latest-period operable generators (operating + standby + returning) from EIA-860M."""
    if not EIA_KEY:
        print("::warning::EIA_API_KEY not set — generator-inventory skipped", flush=True)
        return []
    rows, offset, page, latest = [], 0, 5000, None
    while len(rows) < cap and offset < 80000:
        params = [("api_key", EIA_KEY), ("frequency", "monthly"),
                  ("data[]", "nameplate-capacity-mw"),
                  ("sort[0][column]", "period"), ("sort[0][direction]", "desc"),
                  ("offset", str(offset)), ("length", str(page))]
        for s in _INVENTORY_STATUS:
            params.append(("facets[status][]", s))
        raw = _get(EIA_GEN + "?" + urllib.parse.urlencode(params), timeout=60)
        data = (json.loads(raw).get("response") or {}).get("data") or []
        if not data:
            break
        if latest is None:
            latest = data[0].get("period")   # newest period in the desc-sorted result
        for r in data:
            if r.get("period") != latest:     # sorted desc → past the current snapshot
                return rows[:cap]
            try:
                mw = float(r.get("nameplate-capacity-mw") or 0)
            except (TypeError, ValueError):
                mw = 0.0
            rows.append({
                "period":        (r.get("period") or "")[:7],
                "plant_id":      str(r.get("plantid") or "")[:20],
                "generator_id":  str(r.get("generatorid") or "")[:20],
                "plant_name":    (r.get("plantName") or "")[:200],
                "state":         (r.get("stateid") or "")[:2],
                "ba_code":       (r.get("balancing_authority_code") or "")[:20],
                "entity_name":   (r.get("entityName") or "")[:200],
                "technology":    (r.get("technology") or "")[:100],
                "energy_source": (r.get("energy_source_code") or "")[:20],
                "capacity_mw":   mw,
                "status":        (r.get("status") or "")[:8],
                "status_desc":   (r.get("statusDescription") or "")[:200],
            })
            if len(rows) >= cap:
                break
        offset += page
        if len(data) < page:
            break
    return rows[:cap]


LAYERS = {
    "generator-inventory": {
        "fetch": fetch_generator_inventory,
        "ingest": "/api/v1/admin/ingest/generator-inventory",
        "default_cap": 40000,    # ~25-30k operable generators nationally
    },
    "gas-pipelines": {
        "fetch": fetch_gas_pipelines,
        "ingest": "/api/v1/admin/ingest/gas-pipelines",
        "default_cap": 30000,
    },
    "transmission-lines": {
        "fetch": fetch_transmission_lines,
        "ingest": "/api/v1/admin/ingest/transmission-lines",
        "default_cap": 100000,   # EIA service has ~94,619 lines
    },
    "power-plants": {
        "fetch": fetch_power_plants,
        "ingest": "/api/v1/admin/ingest/power-plants",
        "default_cap": 20000,    # EIA service has ~13,446 plants
    },
}


def main():
    if not ADMIN:
        print("::error::missing DCHUB_ADMIN_KEY"); return 1
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    layer = args[0] if args else "gas-pipelines"
    if layer not in LAYERS:
        print(f"::error::unknown layer {layer}; known: {list(LAYERS)}"); return 1
    cfg = LAYERS[layer]
    cap = int(args[1]) if len(args) > 1 else cfg["default_cap"]

    t0 = time.time()
    print(f"== {layer}: fetching (cap {cap}) ==", flush=True)
    rows = cfg["fetch"](cap)
    print(f"  fetched {len(rows):,} points in {time.time()-t0:.0f}s", flush=True)
    if not rows:
        print("::error::source returned 0 rows"); return 1
    st, j, gz = post_rows(cfg["ingest"], rows, cap)
    print(f"  posted {gz//1024}KB gz (HTTP {st}) → {json.dumps(j)[:200]}", flush=True)
    if st != 200 or not j.get("ok"):
        print(f"::error::ingest failed (HTTP {st})"); return 1
    print(f"== {layer} done: {j.get('inserted')} rows inserted ==")
    return 0


if __name__ == "__main__":
    sys.exit(main())
