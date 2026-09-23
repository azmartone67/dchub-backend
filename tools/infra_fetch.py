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
# reliable EIA org (FiaPA4ga0iQKduv3). Fields: ID, TYPE, STATUS, OWNER, VOLTAGE,
# SUB_1, SUB_2. This 94k set supersedes the stale HIFLD 52k snapshot.
# ★ Geometry IS fetched (EPSG:4326, ~1.5 MB/page, ~1 s/page measured
# 2026-09-23) but NOT stored — the table has no geometry column. It is measured
# on the runner into length_miles + state (util/polyline_geometry.py). Until
# 2026-09-23 this was returnGeometry=false and every row landed with state NULL
# and length_miles 0, so the per-state market profile read 0 lines / 0 miles.
# Shape__Length is Web Mercator metres (x sec(latitude)) — never use it raw.
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

    Row shape (matches routes/transmission_ingest._ROW_FIELDS):
      [hifld_id, name, operator, voltage_kv, from_sub, to_sub, status, line_type,
       length_miles, state]
    length_miles + state are measured from the EPSG:4326 geometry here, on the
    runner; the geometry itself is not posted.
    """
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from util.polyline_geometry import measure_line

    rows, offset, page = [], 0, 2000   # service maxRecordCount = 2000
    while len(rows) < cap:
        params = urllib.parse.urlencode({
            "where": "1=1",
            "outFields": "ID,TYPE,STATUS,OWNER,VOLTAGE,SUB_1,SUB_2",
            "returnGeometry": "true",
            "outSR": "4326",
            "geometryPrecision": "6",
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
            miles, state = measure_line((f.get("geometry") or {}).get("paths"))
            rows.append([
                hid,
                name[:200] if name else None,
                owner,
                _v_voltage(a.get("VOLTAGE")),
                _v_clean(a.get("SUB_1"), 200),
                _v_clean(a.get("SUB_2"), 200),
                _v_clean(a.get("STATUS"), 80),
                _v_clean(a.get("TYPE"), 80),
                miles,
                state or None,
            ])
        offset += page
        if len(feats) < page:
            break
    rows = rows[:cap]
    # Coverage is ENFORCED by the ingest (routes/transmission_ingest.py
    # MEASURED_FLOOR refuses the replace); this line is so the run log shows it.
    print(f"  measured: length {sum(1 for r in rows if r[8] is not None):,}/{len(rows):,}, "
          f"state {sum(1 for r in rows if r[9]):,}/{len(rows):,}, "
          f"{sum(r[8] or 0 for r in rows):,.0f} miles", flush=True)
    return rows


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


# EIA-860M "Planned" sheet — the forward pipeline of new generators NATIONWIDE
# (incl. non-ISO regions the per-ISO queue misses). The v2 JSON API has no
# planned route, so we parse the monthly Excel here on the runner (13MB +
# openpyxl is too heavy for the 1-replica backend). eia.gov needs a browser UA
# + Referer or the xlsx download returns 0 bytes.
EIA860M_PAGE = "https://www.eia.gov/electricity/data/eia860m/"
_EIA_BUA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"


def _eia_get(url, referer=None, timeout=90, tries=4):
    headers = {"User-Agent": _EIA_BUA}
    if referer:
        headers["Referer"] = referer
    last = None
    for a in range(tries):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except Exception as e:
            last = e
            print(f"    eia fetch {a+1}/{tries}: {str(e)[:90]}", flush=True)
            time.sleep(min(2 * (a + 1), 10))
    raise last


def fetch_planned_generators(cap):
    """Download the latest EIA-860M Excel + parse the 'Planned' sheet → dict rows."""
    import io
    import re
    import calendar
    try:
        import openpyxl
    except ImportError:
        print("::error::openpyxl not installed (pip install openpyxl)", flush=True)
        return []
    # Pick the newest non-archive monthly file (e.g. /xls/april_generator2026.xlsx).
    html = _eia_get(EIA860M_PAGE, timeout=40).decode("utf-8", "ignore")
    found = re.findall(r"/electricity/data/eia860m/xls/([a-z]+)_generator(\d{4})\.xlsx", html, re.I)
    months = {m.lower(): i for i, m in enumerate(calendar.month_name) if m}
    cand = sorted(set(found), key=lambda x: (int(x[1]), months.get(x[0].lower(), 0)), reverse=True)
    xlsx = None
    for mon, yr in cand:
        url = f"{EIA860M_PAGE}xls/{mon}_generator{yr}.xlsx"
        try:
            raw = _eia_get(url, referer=EIA860M_PAGE, timeout=120)
        except Exception:
            continue
        if raw and len(raw) > 1_000_000:
            print(f"    EIA-860M file: {mon}_generator{yr}.xlsx ({len(raw)//1024//1024}MB)", flush=True)
            xlsx = raw
            break
    if not xlsx:
        print("::error::no usable EIA-860M xlsx found", flush=True)
        return []
    wb = openpyxl.load_workbook(io.BytesIO(xlsx), read_only=True, data_only=True)
    if "Planned" not in wb.sheetnames:
        print("::error::no Planned sheet", flush=True)
        return []
    xr = list(wb["Planned"].iter_rows(values_only=True))
    hdr = [str(c) if c is not None else "" for c in xr[2]]   # header on row 3

    def ci(sub):
        for i, h in enumerate(hdr):
            if sub.lower() in h.lower():
                return i
        return None
    c = {k: ci(k) for k in ("Entity Name", "Plant ID", "Plant Name", "Plant State",
                            "County", "Balancing Authority", "Generator ID",
                            "Nameplate Capacity", "Technology", "Energy Source Code",
                            "Status", "Operation Month", "Operation Year",
                            "Latitude", "Longitude")}

    def g(r, key):
        i = c[key]
        return r[i] if i is not None and i < len(r) else None
    out = []
    for r in xr[3:]:
        if not r or not g(r, "Plant ID"):
            continue
        out.append({
            "entity_name":   g(r, "Entity Name"),
            "plant_id":      g(r, "Plant ID"),
            "plant_name":    g(r, "Plant Name"),
            "state":         g(r, "Plant State"),
            "county":        g(r, "County"),
            "ba_code":       g(r, "Balancing Authority"),
            "generator_id":  g(r, "Generator ID"),
            "technology":    g(r, "Technology"),
            "energy_source": g(r, "Energy Source Code"),
            "capacity_mw":   g(r, "Nameplate Capacity"),
            "status":        g(r, "Status"),
            "planned_month": g(r, "Operation Month"),
            "planned_year":  g(r, "Operation Year"),
            "lat":           g(r, "Latitude"),
            "lng":           g(r, "Longitude"),
        })
        if len(out) >= cap:
            break
    return out


# ─────────────────────────────────────────────────────────────────────
# PROJECT lanes (2026-09-22). The as-built federal layers above are frozen
# upstream (EIA transmission service last edited 2025-08-26, EIA gas pipelines
# 2025-07-01), so NEW gas/transmission information only appears in project
# lists. Both workbooks are parsed here and POSTed as named-field dicts; the
# ingest (routes/infra_projects_ingest.py) owns the keys, coercion and the
# first_seen_at bookkeeping. Header cells are matched by NAME, never position,
# so a column EIA/ERCOT inserts shifts nothing.
# ─────────────────────────────────────────────────────────────────────
GAS_PROJECTS_XLSX = "https://www.eia.gov/naturalgas/pipelines/EIA-NaturalGasPipelineProjects.xlsx"
GAS_PROJECTS_PAGE = "https://www.eia.gov/naturalgas/data.php"
GAS_PROJECTS_SHEET = "Natural Gas Pipeline Projects"
ERCOT_PLANNING_PAGE = "https://www.ercot.com/gridinfo/planning"
TPIT_LINK_TITLE = "Transmission Project and Information Tracking"


def _iso(v):
    """Excel dates → ISO strings (JSON-safe); everything else unchanged."""
    import datetime as _dt
    if isinstance(v, (_dt.datetime, _dt.date)):
        return v.isoformat()
    return v


def _header_index(hdr, wanted):
    """{field: column} by case-insensitive header PREFIX. A field whose header
    is missing maps to None and reads as null — it never borrows a neighbour."""
    norm = [" ".join(str(h or "").split()).lower() for h in hdr]
    out = {}
    for field, prefix in wanted.items():
        p = prefix.lower()
        out[field] = next((i for i, h in enumerate(norm) if h.startswith(p)), None)
    return out


def _cell(row, idx):
    return row[idx] if idx is not None and idx < len(row) else None


_GAS_PROJECT_COLS = {
    "source_row_updated": "last updated date", "project_name": "project name",
    "operator": "pipeline operator name", "project_type": "project type",
    "status": "status", "completed_date": "completed date",
    "in_service_year": "year in service date", "states": "state(s)",
    "beg_state": "beg_state", "end_state": "end_state", "regions": "region(s)",
    "cost_musd": "cost (millions)", "miles": "miles",
    "capacity_mmcfd": "additional capacity", "diameter_in": "pipeline diameter",
    "pipeline_type": "pipeline type", "authority": "authority",
    "docket": "docket", "crosses_state_border": "crosses state border",
    "notes": "notes", "demand_served": "demand served", "project_url": "website",
}


def parse_gas_projects_workbook(wb):
    """EIA pipeline-projects workbook → dict rows (active sheet only).

    The Historical sheet is deliberately NOT read: it is the annual archive of
    completed/cancelled projects (1996-2024). A project that leaves the active
    sheet is marked in_latest_release=FALSE by the ingest, not re-read from the
    archive under a possibly different name."""
    import datetime as _dt
    release = None
    if "Contents" in wb.sheetnames:
        # The label sits in column B, not A (measured 2026-09-22), so the
        # label is found by content and the value is the next non-empty cell.
        for r in wb["Contents"].iter_rows(values_only=True):
            at = next((i for i, x in enumerate(r or ())
                       if str(x or "").strip().lower().startswith("release date")), None)
            if at is None:
                continue
            v = next((x for x in r[at + 1:] if x not in (None, "")), None)
            release = (v.date().isoformat() if isinstance(v, _dt.datetime)
                       else (str(v) if v else None))
            break
    if GAS_PROJECTS_SHEET not in wb.sheetnames:
        raise RuntimeError(f"sheet {GAS_PROJECTS_SHEET!r} missing; have {wb.sheetnames}")
    rows = list(wb[GAS_PROJECTS_SHEET].iter_rows())
    hi = next((i for i, r in enumerate(rows[:10])
               if any(str(c.value or "").strip() == "Project Name" for c in r)), None)
    if hi is None:
        raise RuntimeError("no 'Project Name' header in the first 10 rows")
    col = _header_index([c.value for c in rows[hi]], _GAS_PROJECT_COLS)
    out = []
    for r in rows[hi + 1:]:
        name = _cell(r, col["project_name"])
        if name is None or not str(name.value or "").strip():
            continue
        rec = {f: (_iso(_cell(r, i).value) if _cell(r, i) is not None else None)
               for f, i in col.items()}
        # The Website column is display text ("Project website") over a
        # hyperlink; the link is the value worth keeping.
        wc = _cell(r, col["project_url"])
        rec["project_url"] = wc.hyperlink.target if wc is not None and wc.hyperlink else None
        rec["source_release"] = release
        out.append(rec)
    return out


def fetch_gas_pipeline_projects(cap):
    """EIA natural gas pipeline projects (quarterly workbook) → dict rows."""
    import io
    try:
        import openpyxl
    except ImportError:
        print("::error::openpyxl not installed (pip install openpyxl)", flush=True)
        return []
    raw = _eia_get(GAS_PROJECTS_XLSX, referer=GAS_PROJECTS_PAGE, timeout=120)
    print(f"    EIA pipeline-projects workbook: {len(raw)//1024}KB", flush=True)
    # NOT read_only: hyperlinks (the project website) are only exposed on a
    # fully loaded workbook. The file is ~1MB, so this costs nothing.
    wb = openpyxl.load_workbook(io.BytesIO(raw), data_only=True)
    rows = parse_gas_projects_workbook(wb)
    print(f"    release {rows[0]['source_release'] if rows else '?'}: {len(rows)} projects",
          flush=True)
    return rows[:cap]


_TPIT_COLS = {
    "project_number": "ercot project number", "title": "project title",
    "description": "project description", "comments": "comments/reasons",
    "from_location": 'terminal "from" location', "to_location": 'terminal "to" location',
    "source_status": "transmission status", "owner": "transmission owner (text",
    "projected_isd": "projected in-service date", "actual_isd": "actual in-service date",
    "kv": "service level kv", "miles_new": "trans circuit miles new",
    "miles_rebuilt": "trans circuit miles rebuilt", "mva": "autotransformer capacity",
    "county_from": "county location for substation", "county_to": "county location for ending",
    "tier": "planning charter tier", "rpg_number": "rpg number",
}
# Sheet-name prefix → the list a project sits on. The TSP contact column is
# never read: it holds personal names and e-mail addresses.
_TPIT_LISTS = ("future", "planned", "completed", "cancelled")


def find_tpit_url(html):
    """The current public TPIT workbook, found by its link TITLE on ERCOT's
    planning page. The URL itself changes with every release (it embeds the
    as-of date), so it can never be hardcoded. The 'Archived …' zip shares the
    words, so the match is on the exact title and an .xlsx href."""
    import html as _html
    import re
    for m in re.finditer(r"<a\s[^>]*>", html, re.I):
        tag = m.group(0)
        href = re.search(r'href="([^"]+)"', tag)
        title = re.search(r'title="([^"]*)"', tag)
        if not href or not title:
            continue
        if (_html.unescape(title.group(1)).strip() == TPIT_LINK_TITLE
                and href.group(1).lower().endswith(".xlsx")):
            u = _html.unescape(href.group(1))
            return u if u.startswith("http") else "https://www.ercot.com" + u
    return None


def parse_tpit_workbook(wb, source_url):
    """ERCOT TPIT workbook → dict rows, one per project per list sheet."""
    import re
    out = []
    for ws in wb.worksheets:
        title = ws.title.lower()
        lst = next((k for k in _TPIT_LISTS if title.startswith(k)), None)
        if lst is None or "tpit" not in title:
            continue
        rows = list(ws.iter_rows(values_only=True))
        hi = next((i for i, r in enumerate(rows[:10])
                   if r and str(r[0] or "").strip().lower().startswith("ercot project number")),
                  None)
        if hi is None:
            raise RuntimeError(f"{ws.title}: no 'ERCOT Project Number' header")
        banner = " ".join(str(x) for x in (rows[0] if rows else ()) if x)
        m = re.search(r"AS OF (\d{1,2})/(\d{1,2})/(\d{4})", banner, re.I)
        release = (f"{int(m.group(3)):04d}-{int(m.group(1)):02d}-{int(m.group(2)):02d}"
                   if m else None)
        col = _header_index(rows[hi], _TPIT_COLS)
        for r in rows[hi + 1:]:
            if not r or _cell(r, col["project_number"]) in (None, ""):
                continue
            rec = {f: _iso(_cell(r, i)) for f, i in col.items()}
            rec.update(source_list=lst, source_url=source_url, source_release=release)
            out.append(rec)
    return out


def fetch_transmission_projects(cap):
    """ERCOT TPIT (public No-Cost workbook) → dict rows."""
    import io
    try:
        import openpyxl
    except ImportError:
        print("::error::openpyxl not installed (pip install openpyxl)", flush=True)
        return []
    page = _eia_get(ERCOT_PLANNING_PAGE, timeout=40).decode("utf-8", "ignore")
    url = find_tpit_url(page)
    if not url:
        print(f"::error::no '{TPIT_LINK_TITLE}' .xlsx link on {ERCOT_PLANNING_PAGE}", flush=True)
        return []
    raw = _eia_get(url, referer=ERCOT_PLANNING_PAGE, timeout=120)
    print(f"    TPIT workbook: {url.rsplit('/', 1)[-1]} ({len(raw)//1024}KB)", flush=True)
    wb = openpyxl.load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
    rows = parse_tpit_workbook(wb, url)
    print(f"    {len(rows)} TPIT rows", flush=True)
    return rows[:cap]


LAYERS = {
    "planned-generators": {
        "fetch": fetch_planned_generators,
        "ingest": "/api/v1/admin/ingest/planned-generators",
        "default_cap": 10000,    # EIA-860M Planned sheet is ~2,300 generators
    },
    "generator-inventory": {
        "fetch": fetch_generator_inventory,
        "ingest": "/api/v1/admin/ingest/generator-inventory",
        "default_cap": 40000,    # ~25-30k operable generators nationally
    },
    "gas-pipelines": {
        "fetch": fetch_gas_pipelines,
        "ingest": "/api/v1/admin/ingest/gas-pipelines",
        # ★ 2026-08-07: was 30000 — BELOW the source. fetch_gas_pipelines ends
        # `return rows[:cap]`, so a cap under the upstream count silently
        # truncates, and because this loader deletes and rewrites its own
        # source tag every run, the table sat at EXACTLY 30,000 rows with a
        # fresh created_at and ZERO net growth for 54 days. It read healthy on
        # every freshness check while 2,892 EIA segments were dropped on the
        # floor — the treadmill that /admin/data-liveness was built to catch.
        # Measured live 2026-08-07 (returnCountOnly on the EIA service): 32,892
        # features. Every sibling here carries headroom over its source; this
        # one alone did not.
        "default_cap": 40000,   # EIA service has ~32,892 pipeline features
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
    # Upsert lanes: "inserted" is NEW projects only, so a quiet week is 0 by
    # design and beats no_new_data (which resets the dead-man zero counter)
    # instead of success-with-0 (which climbs it toward a false red).
    "gas-pipeline-projects": {
        "fetch": fetch_gas_pipeline_projects,
        "ingest": "/api/v1/admin/ingest/gas-pipeline-projects",
        "default_cap": 2000,     # EIA active sheet had 137 projects 2026-09-22
        "zero_is_quiet": True,
    },
    "transmission-projects": {
        "fetch": fetch_transmission_projects,
        "ingest": "/api/v1/admin/ingest/transmission-projects",
        "default_cap": 20000,    # ERCOT TPIT had 2,122 projects 2026-09-22
        "zero_is_quiet": True,
    },
}


# ─────────────────────────────────────────────────────────────────────
# DEAD-MAN BOARD BEAT — carry the REAL inserted count onto /api/v1/ops/deadman.
# The off-worker watcher (tools/deadman/watch.py) beats these same feeds with
# LIVENESS ONLY (last-success ts, no rows_inserted), so every board feed reads
# rows_inserted=NULL and the "0 rows landed" alarm can NEVER trip — #1691's
# COALESCE preserves a real count IF a job posts one, but no board-feed job did.
# This posts the count the ingest endpoint just returned, keyed to the SAME feed
# name the board/watcher use (the GitHub workflow filename, .yml stripped), so a
# "green" feed finally means "rows actually landed", not just "workflow exited 0".
# Fail-OPEN: a beat error is swallowed and NEVER changes this job's exit code. We
# deliberately DON'T send cadence_hours — tools/deadman/watch.py is the
# authoritative cadence registry, and the beat endpoint COALESCEs a missing
# cadence, so its value is left untouched.
# ─────────────────────────────────────────────────────────────────────

# layer key (this script) -> board feed name. The board feed name is the GitHub
# workflow filename that runs `infra_fetch.py <layer>`, .yml stripped — i.e. the
# exact key tools/deadman/watch.py registers. Only layers whose board workflow
# actually invokes THIS script are mapped; any other layer skips the beat (no
# double-reporting, fail-open).
_BOARD_FEED = {
    "gas-pipelines":      "gas-pipeline-ingest",       # gas-pipeline-ingest.yml
    "transmission-lines": "transmission-ingest",       # transmission-ingest.yml
    "power-plants":       "power-plants-ingest",        # power-plants-ingest.yml
    "planned-generators": "planned-generators-ingest",  # planned-generators-ingest.yml
    "gas-pipeline-projects": "gas-pipeline-projects-ingest",  # gas-pipeline-projects-ingest.yml
    "transmission-projects": "transmission-projects-ingest",  # transmission-projects-ingest.yml
}


def beat_board(layer, rows_inserted, status="success"):
    """Best-effort dead-man BEAT carrying the REAL inserted count. NEVER raises."""
    feed = _BOARD_FEED.get(layer)
    if not feed or not ADMIN:
        return
    try:
        body = json.dumps({
            "feed": feed,
            "status": status,
            "rows_inserted": int(rows_inserted or 0),
            "note": "real inserted count from infra_fetch.py (%s)" % layer,
        }).encode()
        # Runs on the GitHub Actions runner, NOT the worker — beat the backend
        # ORIGIN directly (same origin + admin key the ingest POST already uses).
        req = urllib.request.Request(
            f"{ORIGIN}/api/v1/admin/ingest-runs/beat", data=body, method="POST",
            headers={"Content-Type": "application/json",
                     "User-Agent": "dchub-infra-fetch/1.0",
                     "X-Admin-Key": ADMIN})
        urllib.request.urlopen(req, timeout=20).read()
        print(f"  board beat: {feed} rows_inserted={int(rows_inserted or 0)}", flush=True)
    except Exception as e:  # fail-open: a beat error must never fail the ingest
        print(f"  board beat failed (non-fatal): {str(e)[:120]}", flush=True)


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
    # Additive, fail-open: publish the REAL inserted count to the dead-man board.
    quiet = cfg.get("zero_is_quiet") and not j.get("inserted")
    beat_board(layer, j.get("inserted"), status="no_new_data" if quiet else "success")
    return 0


if __name__ == "__main__":
    sys.exit(main())
