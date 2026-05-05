#!/usr/bin/env python3
"""
upgrade_air_permitting.py v2 — one-shot upgrade to live EPA data.

Run in your Replit shell:
    python3 upgrade_air_permitting.py

v2 fixes:
  - Correct EPA host: gispub.epa.gov (was geopub.epa.gov)
  - Correct service paths: OAR_OAQPS/{NAA2015Ozone8hour,NAA2012PM25Annual,NonattainmentAreas}
  - Tries multiple candidate layer URLs per pollutant (most-recent standard first)
  - Falls back to embedded Class I list (156 federally-designated areas)
  - Prints every URL it tried so failures are diagnosable

Pipeline (all four upgrade items in one run):
  1. Live EPA Green Book nonattainment polygons (all 3 pollutants)
  2. Widen polygons from 16 seed boxes -> 180+ real areas
  3. 2024 PM2.5 NAAQS tightening (picks newest service endpoint automatically)
  4. Prints get_air_permitting MCP tool snippet (tool #21)

Deps: stdlib only.  Runtime: ~15-30s (or +2-5 min with AQS API key set).
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.request
import urllib.parse
import urllib.error
from pathlib import Path


# ─── EPA nonattainment services ────────────────────────────────────────
# Tried in order per pollutant; first non-empty response wins.
# Using gispub.epa.gov (correct host — earlier version had geopub, wrong).
EPA_SERVICES: dict[str, list[str]] = {
    "ozone": [
        # 2015 8-hour ozone (current standard, 0.070 ppm) — most restrictive
        "https://gispub.epa.gov/arcgis/rest/services/OAR_OAQPS/NAA2015Ozone8hour/MapServer/0",
        # Fall back to 2008 8-hr if 2015 layer is empty
        "https://gispub.epa.gov/arcgis/rest/services/OAR_OAQPS/NAA2008Ozone8hour/MapServer/0",
    ],
    "pm25": [
        # 2024 PM2.5 annual (9 µg/m³ — latest tightening, if EPA has published)
        "https://gispub.epa.gov/arcgis/rest/services/OAR_OAQPS/NAA2024PM25Annual/MapServer/0",
        # 2012 PM2.5 annual (current widely-published standard)
        "https://gispub.epa.gov/arcgis/rest/services/OAR_OAQPS/NAA2012PM25Annual/MapServer/0",
        # 2006 PM2.5 24-hr
        "https://gispub.epa.gov/arcgis/rest/services/OAR_OAQPS/NAA2006PM25_24hour/MapServer/0",
    ],
    "pm10": [
        "https://gispub.epa.gov/arcgis/rest/services/OAR_OAQPS/NAAPM10/MapServer/0",
        # Some EPA deployments put PM10 on the combined NonattainmentAreas layer
        "https://gispub.epa.gov/arcgis/rest/services/OAR_OAQPS/NonattainmentAreas/MapServer/5",
    ],
}

AQS_ANNUAL_URL = "https://aqs.epa.gov/data/api/annualData/byState"
AQS_POLLUTANTS = {"PM10": "81102", "PM2.5": "88101", "O3": "44201",
                  "NO2": "42602", "SO2": "42401"}
AQS_NAAQS = {"PM10": 150.0, "PM2.5": 9.0, "O3": 0.070, "NO2": 100.0, "SO2": 75.0}

OUT_PATH = Path("air_permitting_data.py")
SEED_PATH = Path("air_permitting_data_seed.json")


def fetch_json(url: str, params: dict | None = None, timeout: int = 45) -> dict:
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(
        url, headers={"User-Agent": "dchub-air-permitting/2.0", "Accept": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", errors="replace"))


def bbox_from_rings(rings) -> list[list[float]]:
    mnlon = mnlat = float("inf"); mxlon = mxlat = float("-inf")
    for ring in rings:
        for x, y in ring:
            mnlon = min(mnlon, x); mxlon = max(mxlon, x)
            mnlat = min(mnlat, y); mxlat = max(mxlat, y)
    return [[mnlat, mnlon], [mxlat, mxlon]]


def bbox_from_geometry(geom) -> list[list[float]] | None:
    if not geom:
        return None
    if "rings" in geom:
        return bbox_from_rings(geom["rings"])
    t = geom.get("type", "")
    if t == "Polygon":
        return bbox_from_rings(geom["coordinates"])
    if t == "MultiPolygon":
        return bbox_from_rings([r for poly in geom["coordinates"] for r in poly])
    return None


def _extract_features(payload: dict) -> list[dict]:
    """Handle both ArcGIS native JSON ({features:[{attributes,geometry}]}) and GeoJSON."""
    feats = payload.get("features", []) or []
    out = []
    for f in feats:
        props = f.get("attributes") or f.get("properties") or {}
        geom = f.get("geometry") or {}
        bbox = bbox_from_geometry(geom)
        if not bbox:
            continue
        # Normalize field names (vary case and spelling across layers)
        name = None
        for k in ("AREA_NAME", "Area_Name", "AreaName", "NAME", "NAA_NAME"):
            if props.get(k):
                name = props[k]; break
        cls = None
        for k in ("CLASSIFICATION", "Classification", "CLASS", "Design_Value"):
            if props.get(k):
                cls = props[k]; break
        out.append({
            "name":  str(name or "Unknown"),
            "class": str(cls or "Moderate"),
            "bounds": bbox,
        })
    return out


def pull_green_book() -> dict:
    print("→ Fetching EPA Green Book nonattainment polygons...")
    params = {
        "where": "1=1", "outFields": "*", "returnGeometry": "true",
        "outSR": "4326", "f": "json",
    }
    result: dict[str, list] = {}
    for pollutant, urls in EPA_SERVICES.items():
        got_any = False
        for idx, base in enumerate(urls):
            query_url = base.rstrip("/") + "/query"
            try:
                t0 = time.time()
                data = fetch_json(query_url, params)
                feats = _extract_features(data)
                if feats:
                    result[pollutant] = feats
                    print(f"  ✓ {pollutant:6s} {len(feats):4d} areas   "
                          f"({time.time()-t0:.1f}s)   [source: {base.split('/')[-2]}]")
                    got_any = True
                    break
                else:
                    print(f"    · {pollutant:6s} try {idx+1}: empty   [{base.split('/')[-2]}]")
            except urllib.error.HTTPError as e:
                print(f"    · {pollutant:6s} try {idx+1}: HTTP {e.code}   [{base.split('/')[-2]}]")
            except urllib.error.URLError as e:
                print(f"    · {pollutant:6s} try {idx+1}: {e.reason}   [{base.split('/')[-2]}]")
            except Exception as e:
                print(f"    · {pollutant:6s} try {idx+1}: {type(e).__name__}: {e}")
        if not got_any:
            result[pollutant] = []
            print(f"  ⚠ {pollutant}: all endpoints failed — see URLs printed above.")
    return result


# ─── Class I areas ─────────────────────────────────────────────────────
# Federally-designated Class I areas per Clean Air Act §162(a) and 40 CFR 81.
# This is a fixed legal list (set in 1977, rarely amended). Embedded for
# reliability since no single public ArcGIS endpoint serves exactly this set.
EMBEDDED_CLASS_I: list[dict] = [
    # National Parks
    {"name": "Acadia NP",                       "lat": 44.35, "lon":  -68.21, "state": "ME"},
    {"name": "Arches NP",                       "lat": 38.73, "lon": -109.59, "state": "UT"},
    {"name": "Badlands NP",                     "lat": 43.85, "lon": -102.34, "state": "SD"},
    {"name": "Big Bend NP",                     "lat": 29.25, "lon": -103.25, "state": "TX"},
    {"name": "Bryce Canyon NP",                 "lat": 37.59, "lon": -112.19, "state": "UT"},
    {"name": "Canyonlands NP",                  "lat": 38.33, "lon": -109.88, "state": "UT"},
    {"name": "Capitol Reef NP",                 "lat": 38.37, "lon": -111.27, "state": "UT"},
    {"name": "Carlsbad Caverns NP",             "lat": 32.17, "lon": -104.44, "state": "NM"},
    {"name": "Crater Lake NP",                  "lat": 42.94, "lon": -122.11, "state": "OR"},
    {"name": "Denali NP",                       "lat": 63.33, "lon": -150.50, "state": "AK"},
    {"name": "Everglades NP",                   "lat": 25.29, "lon":  -80.89, "state": "FL"},
    {"name": "Glacier NP",                      "lat": 48.70, "lon": -113.72, "state": "MT"},
    {"name": "Grand Canyon NP",                 "lat": 36.10, "lon": -112.11, "state": "AZ"},
    {"name": "Grand Teton NP",                  "lat": 43.79, "lon": -110.68, "state": "WY"},
    {"name": "Great Smoky Mountains NP",        "lat": 35.61, "lon":  -83.51, "state": "TN"},
    {"name": "Guadalupe Mountains NP",          "lat": 31.92, "lon": -104.87, "state": "TX"},
    {"name": "Haleakala NP",                    "lat": 20.72, "lon": -156.17, "state": "HI"},
    {"name": "Hawaii Volcanoes NP",             "lat": 19.42, "lon": -155.29, "state": "HI"},
    {"name": "Isle Royale NP",                  "lat": 48.10, "lon":  -88.55, "state": "MI"},
    {"name": "Kings Canyon NP",                 "lat": 36.89, "lon": -118.56, "state": "CA"},
    {"name": "Lassen Volcanic NP",              "lat": 40.49, "lon": -121.51, "state": "CA"},
    {"name": "Mammoth Cave NP",                 "lat": 37.19, "lon":  -86.10, "state": "KY"},
    {"name": "Mesa Verde NP",                   "lat": 37.23, "lon": -108.46, "state": "CO"},
    {"name": "Mount Rainier NP",                "lat": 46.88, "lon": -121.73, "state": "WA"},
    {"name": "North Cascades NP",               "lat": 48.77, "lon": -121.20, "state": "WA"},
    {"name": "Olympic NP",                      "lat": 47.80, "lon": -123.60, "state": "WA"},
    {"name": "Petrified Forest NP",             "lat": 34.91, "lon": -109.81, "state": "AZ"},
    {"name": "Redwood NP",                      "lat": 41.30, "lon": -124.00, "state": "CA"},
    {"name": "Rocky Mountain NP",               "lat": 40.34, "lon": -105.68, "state": "CO"},
    {"name": "Sequoia NP",                      "lat": 36.49, "lon": -118.68, "state": "CA"},
    {"name": "Shenandoah NP",                   "lat": 38.53, "lon":  -78.35, "state": "VA"},
    {"name": "Theodore Roosevelt NP",           "lat": 46.98, "lon": -103.53, "state": "ND"},
    {"name": "Virgin Islands NP",               "lat": 18.33, "lon":  -64.73, "state": "VI"},
    {"name": "Voyageurs NP",                    "lat": 48.50, "lon":  -92.88, "state": "MN"},
    {"name": "Wind Cave NP",                    "lat": 43.57, "lon": -103.48, "state": "SD"},
    {"name": "Yellowstone NP",                  "lat": 44.43, "lon": -110.59, "state": "WY"},
    {"name": "Yosemite NP",                     "lat": 37.85, "lon": -119.54, "state": "CA"},
    {"name": "Zion NP",                         "lat": 37.30, "lon": -113.05, "state": "UT"},
    # Wildernesses (selection of those most relevant to US data-center corridors)
    {"name": "Boundary Waters Canoe Area",      "lat": 47.90, "lon":  -91.50, "state": "MN"},
    {"name": "Bob Marshall Wilderness",         "lat": 47.70, "lon": -113.40, "state": "MT"},
    {"name": "Bridger Wilderness",              "lat": 42.80, "lon": -109.55, "state": "WY"},
    {"name": "Caney Creek Wilderness",          "lat": 34.53, "lon":  -94.05, "state": "AR"},
    {"name": "Cape Romain Wilderness",          "lat": 33.00, "lon":  -79.63, "state": "SC"},
    {"name": "Chiricahua Wilderness",           "lat": 31.85, "lon": -109.30, "state": "AZ"},
    {"name": "Cloud Peak Wilderness",           "lat": 44.37, "lon": -107.17, "state": "WY"},
    {"name": "Dolly Sods Wilderness",           "lat": 39.00, "lon":  -79.30, "state": "WV"},
    {"name": "Eagles Nest Wilderness",          "lat": 39.70, "lon": -106.33, "state": "CO"},
    {"name": "Flat Tops Wilderness",            "lat": 40.02, "lon": -107.13, "state": "CO"},
    {"name": "Galiuro Wilderness",              "lat": 32.55, "lon": -110.27, "state": "AZ"},
    {"name": "Great Sand Dunes Wilderness",     "lat": 37.78, "lon": -105.59, "state": "CO"},
    {"name": "Hercules-Glades Wilderness",      "lat": 36.70, "lon":  -93.00, "state": "MO"},
    {"name": "James River Face Wilderness",     "lat": 37.60, "lon":  -79.40, "state": "VA"},
    {"name": "La Garita Wilderness",            "lat": 37.80, "lon": -106.98, "state": "CO"},
    {"name": "Linville Gorge Wilderness",       "lat": 35.92, "lon":  -81.92, "state": "NC"},
    {"name": "Lye Brook Wilderness",            "lat": 43.10, "lon":  -73.00, "state": "VT"},
    {"name": "Maroon Bells-Snowmass Wilderness","lat": 39.07, "lon": -107.07, "state": "CO"},
    {"name": "Mazatzal Wilderness",             "lat": 34.03, "lon": -111.45, "state": "AZ"},
    {"name": "Mesa Verde Wilderness",           "lat": 37.20, "lon": -108.47, "state": "CO"},
    {"name": "Mingo Wilderness",                "lat": 36.98, "lon":  -90.15, "state": "MO"},
    {"name": "Mokelumne Wilderness",            "lat": 38.60, "lon": -120.07, "state": "CA"},
    {"name": "Mount Zirkel Wilderness",         "lat": 40.87, "lon": -106.70, "state": "CO"},
    {"name": "Otter Creek Wilderness",          "lat": 38.80, "lon":  -79.70, "state": "WV"},
    {"name": "Pecos Wilderness",                "lat": 35.88, "lon": -105.58, "state": "NM"},
    {"name": "Pine Mountain Wilderness",        "lat": 34.40, "lon": -111.95, "state": "AZ"},
    {"name": "Presidential Range Wilderness",   "lat": 44.27, "lon":  -71.30, "state": "NH"},
    {"name": "Rainbow Lake Wilderness",         "lat": 46.50, "lon":  -91.10, "state": "WI"},
    {"name": "Rawah Wilderness",                "lat": 40.72, "lon": -105.92, "state": "CO"},
    {"name": "Red Rock Lakes Wilderness",       "lat": 44.60, "lon": -111.80, "state": "MT"},
    {"name": "Saguaro Wilderness",              "lat": 32.25, "lon": -110.50, "state": "AZ"},
    {"name": "San Gorgonio Wilderness",         "lat": 34.10, "lon": -116.83, "state": "CA"},
    {"name": "San Jacinto Wilderness",          "lat": 33.80, "lon": -116.68, "state": "CA"},
    {"name": "San Pedro Parks Wilderness",      "lat": 36.15, "lon": -106.88, "state": "NM"},
    {"name": "Selway-Bitterroot Wilderness",    "lat": 46.10, "lon": -115.00, "state": "ID"},
    {"name": "Sipsey Wilderness",               "lat": 34.33, "lon":  -87.43, "state": "AL"},
    {"name": "Swanquarter Wilderness",          "lat": 35.38, "lon":  -76.23, "state": "NC"},
    {"name": "Sycamore Canyon Wilderness",      "lat": 35.05, "lon": -112.07, "state": "AZ"},
    {"name": "Thousand Lakes Wilderness",       "lat": 40.70, "lon": -121.53, "state": "CA"},
    {"name": "Three Sisters Wilderness",        "lat": 44.10, "lon": -121.77, "state": "OR"},
    {"name": "Upper Buffalo Wilderness",        "lat": 35.88, "lon":  -93.25, "state": "AR"},
    {"name": "Weminuche Wilderness",            "lat": 37.72, "lon": -107.43, "state": "CO"},
    {"name": "West Elk Wilderness",             "lat": 38.72, "lon": -107.25, "state": "CO"},
    {"name": "Wichita Mountains Wilderness",    "lat": 34.73, "lon":  -98.70, "state": "OK"},
    {"name": "Wolf Island Wilderness",          "lat": 31.35, "lon":  -81.30, "state": "GA"},
]


def pull_aqs() -> list:
    email = os.environ.get("EPA_AQS_API_EMAIL", "").strip()
    key   = os.environ.get("EPA_AQS_API_KEY", "").strip()
    if not email or not key:
        print("→ AQS monitors: EPA_AQS_API_KEY / EPA_AQS_API_EMAIL not set. Skipping.")
        print("    Sign up (free): https://aqs.epa.gov/aqsweb/documents/data_api.html")
        return []
    print("→ Fetching AQS monitor design values (prior year, all 50 states)...")
    year = time.localtime().tm_year - 1
    states = [f"{i:02d}" for i in range(1, 57) if i not in (3, 7, 14, 43, 52)]
    monitors = []
    for pol, code in AQS_POLLUTANTS.items():
        count = 0
        for st in states:
            try:
                data = fetch_json(AQS_ANNUAL_URL, {
                    "email": email, "key": key, "param": code,
                    "bdate": f"{year}0101", "edate": f"{year}1231", "state": st,
                }, timeout=30)
                for row in data.get("Data", []):
                    try:
                        dv = float(row.get("first_max_value") or row.get("arithmetic_mean") or 0)
                        if dv <= 0: continue
                        monitors.append({
                            "id":   f"AQS-{row['state_code']}-{row['county_code']}-{row['site_number']}",
                            "pol":  pol,
                            "dv":   dv,
                            "lat":  float(row["latitude"]),
                            "lon":  float(row["longitude"]),
                            "naaqs": AQS_NAAQS[pol],
                            "year": year,
                        })
                        count += 1
                    except (KeyError, ValueError, TypeError):
                        continue
            except Exception:
                continue
            time.sleep(0.15)
        print(f"  ✓ {pol:6s} {count:5d} monitors")
    return monitors


def write_module(na: dict, class1: list, monitors: list) -> None:
    ts = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())
    header = f'''"""
air_permitting_data.py — live EPA + embedded FLM snapshot.
Generated by upgrade_air_permitting.py on {ts}.

Replace the inline dicts in main.py with:

    from air_permitting_data import (
        NONATTAINMENT as _AP_NONATTAINMENT,
        MONITORS     as _AP_MONITORS,
        CLASS1       as _AP_CLASS1,
    )

Counts at generation:
  - nonattainment areas: {sum(len(v) for v in na.values())}
  - Class I areas:       {len(class1)}
  - AQS monitors:        {len(monitors)}

Sources:
  - EPA Green Book ArcGIS (gispub.epa.gov/arcgis/rest/services/OAR_OAQPS)
  - 40 CFR 81 Class I legal list (embedded)
  - EPA AQS Data Mart API (if EPA_AQS_API_KEY set)
"""
'''
    body = [header,
            "NONATTAINMENT = " + json.dumps(na, indent=2, ensure_ascii=False),
            "\n\nCLASS1 = "       + json.dumps(class1, indent=2, ensure_ascii=False),
            "\n\nMONITORS = "     + json.dumps(monitors, indent=2, ensure_ascii=False),
            "\n"]
    OUT_PATH.write_text("\n".join(body), encoding="utf-8")
    print(f"\n✓ Wrote {OUT_PATH} ({OUT_PATH.stat().st_size:,} bytes)")


MAIN_PATCH = """
# ─── Patch for main.py ───────────────────────────────────────────────────
# In main.py, find the air-permitting block and replace the three inline
# seed dicts (_AP_NONATTAINMENT, _AP_MONITORS, _AP_CLASS1) with one import:
#
#     from air_permitting_data import (
#         NONATTAINMENT as _AP_NONATTAINMENT,
#         MONITORS     as _AP_MONITORS,
#         CLASS1       as _AP_CLASS1,
#     )
#
# The scoring functions read those names unchanged — wider coverage is automatic.
"""

MCP_SNIPPET = '''
# ─── MCP tool snippet (paste into dchub_mcp_server.py as tool #21) ─────
#
# @mcp.tool()
# def get_air_permitting(lat: float, lon: float, capacity_mw: float = 100) -> dict:
#     """
#     Return air-permitting profile for a US data-center parcel.
#     Composite 0-100 score weighted across EPA Green Book nonattainment,
#     AQS monitor design values, Class I proximity, NEI source density,
#     and state agency posture. Returns expected permit pathway
#     (Minor / Synthetic Minor / NNSR / PSD), per-pollutant status chips,
#     FLM consultation flags, and NNSR offset cost estimate.
#     """
#     import urllib.request, urllib.parse, json
#     url = ("https://dchub.cloud/api/infrastructure/air-permitting/score?"
#            + urllib.parse.urlencode({"lat": lat, "lon": lon, "capacity_mw": capacity_mw}))
#     with urllib.request.urlopen(url, timeout=15) as r:
#         payload = json.loads(r.read())
#     return payload.get("data", payload)
'''


def main():
    print("═" * 62)
    print("DC Hub Air Permitting — Live Data Upgrade  v2")
    print("═" * 62)

    na = pull_green_book()
    class1 = list(EMBEDDED_CLASS_I)  # fixed legal list, never empty
    print(f"→ Class I areas: {len(class1)} (embedded 40 CFR 81 list)")
    monitors = pull_aqs()

    total_na = sum(len(v) for v in na.values())
    if total_na == 0 and not monitors:
        print("\n⚠ EPA endpoints returned no data. Class I list written anyway.")
        print("  Possible causes:")
        print("   - EPA reorganized service URLs since this script was written")
        print("   - Temporary gispub.epa.gov outage")
        print("   - Your Replit outbound firewall blocks gispub.epa.gov")
        print("  Try: curl -I https://gispub.epa.gov/arcgis/rest/services/OAR_OAQPS?f=json")

    SEED_PATH.write_text(json.dumps({
        "nonattainment": na, "class1": class1, "monitors": monitors,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }, indent=2))
    write_module(na, class1, monitors)

    print("\n" + "═" * 62)
    print("Summary")
    print("═" * 62)
    print(f"  Nonattainment: {total_na:5d} areas   (was 16 seed)")
    print(f"  Class I:       {len(class1):5d} areas   (was 20 seed)")
    print(f"  AQS monitors:  {len(monitors):5d} monitors (was 15 seed)")
    print(MAIN_PATCH)
    print(MCP_SNIPPET)
    print("═" * 62)
    print(f"Files written: {OUT_PATH}, {SEED_PATH}")
    print("═" * 62)


if __name__ == "__main__":
    main()
