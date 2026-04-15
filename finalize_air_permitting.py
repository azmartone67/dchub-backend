#!/usr/bin/env python3
"""
finalize_air_permitting.py — final 5-item polish, one shell command.

Run once in your Replit shell (from the same directory as main.py):
    python3 finalize_air_permitting.py

The 5 items (all in one pass):
  1. AQS live monitors (15 seed -> ~1,500+) via EPA AQS API
     Requires EPA_AQS_API_KEY + EPA_AQS_API_EMAIL in Replit Secrets.
     Skips gracefully if not set; your existing 15 seed monitors stay put.
  2. All-50-states + DC permitting context (16 -> 51).
  3. NEI stationary sources expansion (22 -> ~80) across DC markets.
  4. Smoke tests against live dchub.cloud endpoints (6 checks + score sanity).
  5. Weekly auto-refresh cron template (prints Replit Scheduled Task config).

Writes (or updates):
  - air_permitting_data.py         (NONATTAINMENT + CLASS1 preserved; MONITORS updated if AQS runs)
  - air_permitting_extras.py       (STATE_CONTEXT + NEI_SOURCES; import into main.py)

Prints git commit commands at the end.

Deps: stdlib only. Runtime: ~5s without AQS key, ~3-5 min with key.
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


# ─── Endpoints & constants ─────────────────────────────────────────────
AQS_ANNUAL_URL = "https://aqs.epa.gov/data/api/annualData/byState"
AQS_POLLUTANTS = {"PM10": "81102", "PM2.5": "88101", "O3": "44201",
                  "NO2": "42602", "SO2": "42401"}
AQS_NAAQS = {"PM10": 150.0, "PM2.5": 9.0, "O3": 0.070, "NO2": 100.0, "SO2": 75.0}

DCHUB_BASE = "https://dchub.cloud/api/infrastructure/air-permitting"

DATA_PATH = Path("air_permitting_data.py")
EXTRAS_PATH = Path("air_permitting_extras.py")


def fetch_json(url: str, params: dict | None = None, timeout: int = 45) -> dict:
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={
        "User-Agent": "dchub-air-permitting-finalize/1.0",
        "Accept": "application/json",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", errors="replace"))


# ═══════════════════════════════════════════════════════════════════════
# ITEM 1 — AQS LIVE MONITORS
# ═══════════════════════════════════════════════════════════════════════
def pull_aqs() -> list[dict]:
    email = os.environ.get("EPA_AQS_API_EMAIL", "").strip()
    key   = os.environ.get("EPA_AQS_API_KEY", "").strip()
    if not email or not key:
        print("  · EPA_AQS_API_KEY/EPA_AQS_API_EMAIL not set — skipping live AQS.")
        print("    Free signup: https://aqs.epa.gov/aqsweb/documents/data_api.html")
        return []
    year = time.localtime().tm_year - 1
    states = [f"{i:02d}" for i in range(1, 57) if i not in (3, 7, 14, 43, 52)]
    monitors: list[dict] = []
    for pol, code in AQS_POLLUTANTS.items():
        count_start = len(monitors)
        for st in states:
            try:
                data = fetch_json(AQS_ANNUAL_URL, {
                    "email": email, "key": key, "param": code,
                    "bdate": f"{year}0101", "edate": f"{year}1231", "state": st,
                }, timeout=30)
                for row in data.get("Data", []):
                    try:
                        dv = float(row.get("first_max_value") or
                                   row.get("arithmetic_mean") or 0)
                        if dv <= 0: continue
                        monitors.append({
                            "id":    f"AQS-{row['state_code']}-{row['county_code']}-{row['site_number']}",
                            "pol":   pol,
                            "dv":    dv,
                            "lat":   float(row["latitude"]),
                            "lon":   float(row["longitude"]),
                            "naaqs": AQS_NAAQS[pol],
                            "year":  year,
                        })
                    except (KeyError, ValueError, TypeError):
                        continue
            except Exception:
                continue
            time.sleep(0.12)
        print(f"    {pol:6s} +{len(monitors) - count_start:5d}")
    return monitors


# ═══════════════════════════════════════════════════════════════════════
# ITEM 2 — ALL-50-STATES + DC PERMITTING CONTEXT
# ═══════════════════════════════════════════════════════════════════════
# 16 detailed entries from the original main.py block, plus 34 templated
# entries for states without heavy DC permitting precedent.
STATE_CONTEXT_FULL: dict[str, dict] = {
    # ─── Detailed (data-center-active states) ─────────────────────────
    "AZ": {"score": 65, "agency": "Arizona ADEQ",
           "description": "ADEQ — Class II permits avg 14 months. Cumulative impact analysis required in Phoenix PM10 Serious NA. Offsets $18-35k/ton NOx."},
    "TX": {"score": 75, "agency": "Texas TCEQ",
           "description": "TCEQ — Standard Permit below 10 tpy NOx; above that full NSR. Timelines 6-9 mo minor, 18+ mo NNSR. DFW/HGB need offsets."},
    "VA": {"score": 88, "agency": "Virginia DEQ",
           "description": "DEQ — well-trodden Loudoun data-center pathway. Synthetic minor via runtime caps 4-6 mo. FLM consult required near Shenandoah."},
    "IA": {"score": 92, "agency": "Iowa DNR",
           "description": "DNR — predictable 4-5 mo minor source permitting. Full attainment, no offsets, minimal modeling."},
    "OH": {"score": 82, "agency": "Ohio EPA",
           "description": "Ohio EPA — 5-8 mo permitting. GHG PSD BACT at >150 MW. Watch Title V classification."},
    "CA": {"score": 30, "agency": "CARB + SCAQMD/BAAQMD/SJVAPCD",
           "description": "Most complex permitting in US. Any large site triggers NNSR on multiple pollutants. 18-30 mo timelines."},
    "NV": {"score": 70, "agency": "Nevada NDEP",
           "description": "NDEP — simple outside Clark County PM10 maintenance. Reno-Tahoe 4-6 mo."},
    "IL": {"score": 60, "agency": "Illinois EPA",
           "description": "IEPA — Chicago ozone Moderate NA drives NNSR. Downstate much faster."},
    "PA": {"score": 55, "agency": "PA DEP",
           "description": "PA DEP — Allegheny Co PM2.5 NA is main constraint. Outside that 6-10 mo."},
    "NY": {"score": 50, "agency": "NY DEC",
           "description": "NY DEC — complex in metro. CLCPA adds GHG reduction obligations."},
    "GA": {"score": 78, "agency": "GA EPD",
           "description": "GA EPD — Atlanta Marginal ozone NA. Lithia Springs corridor 5-7 mo."},
    "NC": {"score": 80, "agency": "NC DEQ",
           "description": "NC DEQ — Charlotte and RTP attainment. 4-6 mo for minor sources."},
    "WA": {"score": 72, "agency": "WA Ecology",
           "description": "Central WA (Quincy, Moses Lake) attainment. Puget Sound more complex."},
    "OR": {"score": 75, "agency": "Oregon DEQ",
           "description": "OR DEQ — Hillsboro/Prineville corridor benefits from attainment status."},
    "CO": {"score": 58, "agency": "Colorado CDPHE",
           "description": "CDPHE — Denver Metro ozone Serious NA binding within 50 mi. Offsets scarce."},
    "UT": {"score": 65, "agency": "Utah DEQ",
           "description": "UT DEQ — Wasatch Front PM2.5 maintenance. Winter inversion drives tight constraints."},
    # ─── Templated (less DC permitting activity to date) ──────────────
    "AL": {"score": 78, "agency": "Alabama ADEM",                      "description": "ADEM — standard Title V / minor NSR pathways. Mostly attainment; 4-7 mo typical."},
    "AK": {"score": 72, "agency": "Alaska DEC",                        "description": "DEC — attainment statewide. Fairbanks PM2.5 NA local to Interior, irrelevant to most DC siting."},
    "AR": {"score": 82, "agency": "Arkansas DEQ",                      "description": "ADEQ — full attainment, predictable 4-6 mo minor source permitting."},
    "CT": {"score": 52, "agency": "Connecticut DEEP",                  "description": "DEEP — entire state in ozone Moderate NA. Any combustion source needs NOx offsets or very tight caps."},
    "DE": {"score": 60, "agency": "Delaware DNREC",                    "description": "DNREC — Philly metro ozone NA reaches across state line. Minor-source pathway viable with runtime caps."},
    "FL": {"score": 83, "agency": "Florida DEP",                       "description": "FDEP — full attainment. Orlando/Tampa/Jacksonville 4-6 mo. Watch hurricane-related permit re-review."},
    "HI": {"score": 70, "agency": "Hawaii DOH",                        "description": "HDOH — attainment but volcanic SO2 in Hawaii County complicates modeling. Oahu typical."},
    "ID": {"score": 78, "agency": "Idaho DEQ",                         "description": "DEQ — attainment statewide. West Silver Valley PM10 NA is remote mining legacy, not a DC concern."},
    "IN": {"score": 72, "agency": "Indiana IDEM",                      "description": "IDEM — Chicago ozone NA reaches NW corner. Indy metro attainment. 5-7 mo typical."},
    "KS": {"score": 85, "agency": "Kansas KDHE",                       "description": "KDHE — full attainment, fast minor permitting. Good DC siting candidate."},
    "KY": {"score": 75, "agency": "Kentucky DEP",                      "description": "KYDEP — most of state attainment. Louisville metro ozone Marginal. 5-7 mo."},
    "LA": {"score": 68, "agency": "Louisiana DEQ",                     "description": "LDEQ — Baton Rouge ozone Marginal. Gulf Coast industrial corridor: cumulative impact scrutiny."},
    "ME": {"score": 82, "agency": "Maine DEP",                         "description": "Maine DEP — full attainment, minimal DC activity to date. Straightforward minor source path."},
    "MD": {"score": 55, "agency": "Maryland MDE",                      "description": "MDE — Baltimore-DC ozone Moderate NA covers most populated areas. NNSR threshold 100 tpy NOx."},
    "MA": {"score": 55, "agency": "Mass DEP",                          "description": "MassDEP — ozone Moderate NA statewide. Additional state-level GHG rules on top of federal PSD."},
    "MI": {"score": 75, "agency": "Michigan EGLE",                     "description": "EGLE — Detroit metro ozone Marginal. Elsewhere attainment. GRB/Lansing favorable."},
    "MN": {"score": 82, "agency": "Minnesota MPCA",                    "description": "MPCA — full attainment. Twin Cities 5-7 mo. Active DC growth in Rosemount/Lakeville."},
    "MS": {"score": 83, "agency": "Mississippi MDEQ",                  "description": "MDEQ — full attainment, predictable permitting. Low cost incentives."},
    "MO": {"score": 78, "agency": "Missouri DNR",                      "description": "MoDNR — St Louis ozone Marginal. KC metro attainment. Typical 4-7 mo."},
    "MT": {"score": 85, "agency": "Montana DEQ",                       "description": "Montana DEQ — full attainment, minimal industrial activity. Glacier NP FLM consult in NW."},
    "NE": {"score": 85, "agency": "Nebraska DEE",                      "description": "NDEE — full attainment. Straightforward 4-6 mo minor source."},
    "NH": {"score": 78, "agency": "New Hampshire DES",                 "description": "NHDES — southern NH affected by Boston ozone. Northern NH attainment."},
    "NJ": {"score": 48, "agency": "NJ DEP",                            "description": "NJDEP — ozone Moderate NA statewide. Strict state-level air toxics rules on top of federal."},
    "NM": {"score": 75, "agency": "New Mexico NMED",                   "description": "NMED — most attainment. Permian Basin ozone watch-list but not yet NA."},
    "ND": {"score": 85, "agency": "North Dakota DEQ",                  "description": "NDDEQ — full attainment, predictable. Theodore Roosevelt NP FLM consult in west."},
    "OK": {"score": 80, "agency": "Oklahoma DEQ",                      "description": "ODEQ — OKC/Tulsa attainment but design values approaching NAAQS. 4-6 mo typical."},
    "RI": {"score": 55, "agency": "Rhode Island DEM",                  "description": "RI DEM — ozone Moderate NA statewide. Small state, limited offset market."},
    "SC": {"score": 80, "agency": "SC DHEC",                           "description": "SC DHEC — full attainment. Charleston/Columbia 4-7 mo. Active DC corridor development."},
    "SD": {"score": 86, "agency": "South Dakota DANR",                 "description": "SD DANR — full attainment, fast permitting. Minimal DC activity, simple process."},
    "TN": {"score": 78, "agency": "Tennessee TDEC",                    "description": "TDEC — most attainment. Great Smoky Mountains NP drives FLM consults in east TN."},
    "VT": {"score": 85, "agency": "Vermont DEC",                       "description": "VT DEC — attainment. Small state, low industrial density. Straightforward permitting."},
    "WV": {"score": 65, "agency": "WV DEP",                            "description": "WV DEP — Allegheny County PM2.5 NA hits eastern panhandle. Rest attainment."},
    "WI": {"score": 72, "agency": "Wisconsin DNR",                     "description": "WDNR — Sheboygan ozone Moderate NA, Milwaukee Marginal. Rest of state attainment."},
    "WY": {"score": 85, "agency": "Wyoming DEQ",                       "description": "WY DEQ — attainment. Class I areas (Bridger, Fitzpatrick) drive FLM consults statewide."},
    "DC": {"score": 55, "agency": "DC DOEE",                           "description": "DOEE — ozone Moderate NA. Federal facilities also subject to EPA Region 3 oversight."},
}


# ═══════════════════════════════════════════════════════════════════════
# ITEM 3 — EXPANDED NEI STATIONARY SOURCES
# ═══════════════════════════════════════════════════════════════════════
# Existing 22 + ~60 new, covering major DC markets' cumulative-impact neighbors.
NEI_EXPANDED: list[dict] = [
    # Arizona (existing + Tucson metro)
    {"name": "APS Redhawk Power Plant",                "lat": 33.42, "lon": -112.75, "state": "AZ", "type": "gas"},
    {"name": "Palo Verde Nuclear Generating Station",  "lat": 33.39, "lon": -112.86, "state": "AZ", "type": "nuclear"},
    {"name": "Salt River Agua Fria Generating Station","lat": 33.57, "lon": -112.30, "state": "AZ", "type": "gas"},
    {"name": "APS Ocotillo Power Plant",               "lat": 33.39, "lon": -111.94, "state": "AZ", "type": "gas"},
    {"name": "Asarco Hayden Smelter",                  "lat": 33.00, "lon": -110.78, "state": "AZ", "type": "smelter"},
    {"name": "TEP Irvington Generating Station",       "lat": 32.13, "lon": -110.88, "state": "AZ", "type": "gas"},
    {"name": "TEP Sundt Generating Station",           "lat": 32.19, "lon": -110.97, "state": "AZ", "type": "gas"},
    {"name": "Arlington Valley Energy Facility",       "lat": 33.34, "lon": -112.91, "state": "AZ", "type": "gas"},
    # Texas (existing + DFW + Austin + San Antonio)
    {"name": "Midlothian cement corridor",             "lat": 32.48, "lon":  -97.00, "state": "TX", "type": "cement"},
    {"name": "Luminant Big Brown (retired)",           "lat": 32.05, "lon":  -96.05, "state": "TX", "type": "coal"},
    {"name": "Dallas gas peakers complex",             "lat": 32.70, "lon":  -96.90, "state": "TX", "type": "gas"},
    {"name": "Houston Ship Channel refineries",        "lat": 29.72, "lon":  -95.08, "state": "TX", "type": "refinery"},
    {"name": "Ennis Clay Plant",                       "lat": 32.30, "lon":  -96.62, "state": "TX", "type": "industrial"},
    {"name": "Austin Decker Power Station",            "lat": 30.32, "lon":  -97.62, "state": "TX", "type": "gas"},
    {"name": "Corpus Christi refineries",              "lat": 27.81, "lon":  -97.43, "state": "TX", "type": "refinery"},
    {"name": "CPS Energy J.K. Spruce",                 "lat": 29.30, "lon":  -98.31, "state": "TX", "type": "coal"},
    {"name": "Sandow Steam Electric",                  "lat": 30.56, "lon":  -97.04, "state": "TX", "type": "coal"},
    {"name": "W.A. Parish Generating Station",         "lat": 29.48, "lon":  -95.63, "state": "TX", "type": "gas"},
    # Virginia (existing + Richmond + Dominion fleet)
    {"name": "NOVEC substation complex",               "lat": 39.01, "lon":  -77.50, "state": "VA", "type": "substation"},
    {"name": "Dulles airport operations",              "lat": 38.95, "lon":  -77.45, "state": "VA", "type": "airport"},
    {"name": "Dominion Possum Point",                  "lat": 38.53, "lon":  -77.30, "state": "VA", "type": "gas"},
    {"name": "Covanta Alexandria Energy Resource",     "lat": 38.80, "lon":  -77.06, "state": "VA", "type": "waste-to-energy"},
    {"name": "Dominion Bear Garden",                   "lat": 37.59, "lon":  -78.55, "state": "VA", "type": "gas"},
    {"name": "Dominion Warren County",                 "lat": 38.99, "lon":  -78.16, "state": "VA", "type": "gas"},
    {"name": "Dominion Brunswick County",              "lat": 36.78, "lon":  -77.95, "state": "VA", "type": "gas"},
    # Iowa + Midwest
    {"name": "MidAmerican Des Moines Energy Center",   "lat": 41.52, "lon":  -93.51, "state": "IA", "type": "gas"},
    {"name": "Prairie Creek Generating Station",       "lat": 41.97, "lon":  -91.65, "state": "IA", "type": "gas"},
    {"name": "Ames Power Plant",                       "lat": 42.03, "lon":  -93.61, "state": "IA", "type": "coal"},
    {"name": "Cedar Rapids industrial complex",        "lat": 41.98, "lon":  -91.66, "state": "IA", "type": "industrial"},
    # Ohio
    {"name": "AEP Gavin",                              "lat": 38.94, "lon":  -82.11, "state": "OH", "type": "coal"},
    {"name": "Honda East Liberty",                     "lat": 40.22, "lon":  -83.56, "state": "OH", "type": "industrial"},
    {"name": "Columbus metro industrial complex",      "lat": 39.96, "lon":  -82.99, "state": "OH", "type": "industrial"},
    {"name": "AEP Cardinal Plant",                     "lat": 40.26, "lon":  -80.65, "state": "OH", "type": "coal"},
    {"name": "Duke Energy Zimmer",                     "lat": 38.92, "lon":  -84.21, "state": "OH", "type": "coal"},
    # Illinois
    {"name": "Midwest Generation Joliet",              "lat": 41.56, "lon":  -88.06, "state": "IL", "type": "gas"},
    {"name": "ExxonMobil Joliet Refinery",             "lat": 41.50, "lon":  -88.10, "state": "IL", "type": "refinery"},
    {"name": "Prairie State Energy Campus",            "lat": 38.38, "lon":  -89.68, "state": "IL", "type": "coal"},
    # California
    {"name": "LA Refinery complex",                    "lat": 33.78, "lon": -118.26, "state": "CA", "type": "refinery"},
    {"name": "Port of LA emissions source",            "lat": 33.73, "lon": -118.26, "state": "CA", "type": "port"},
    {"name": "Chevron Richmond Refinery",              "lat": 37.94, "lon": -122.40, "state": "CA", "type": "refinery"},
    {"name": "Moss Landing Power Plant",               "lat": 36.80, "lon": -121.79, "state": "CA", "type": "gas"},
    {"name": "Long Beach Generating Station",          "lat": 33.76, "lon": -118.21, "state": "CA", "type": "gas"},
    # Georgia (Atlanta corridor)
    {"name": "Georgia Power Plant Bowen",              "lat": 34.12, "lon":  -84.92, "state": "GA", "type": "coal"},
    {"name": "Georgia Power Plant Scherer",            "lat": 33.06, "lon":  -83.80, "state": "GA", "type": "coal"},
    {"name": "McDonough-Atkinson Plant",               "lat": 33.82, "lon":  -84.51, "state": "GA", "type": "gas"},
    # North Carolina
    {"name": "Duke Marshall Steam Station",            "lat": 35.60, "lon":  -80.97, "state": "NC", "type": "coal"},
    {"name": "Duke Belews Creek",                      "lat": 36.28, "lon":  -80.07, "state": "NC", "type": "coal"},
    {"name": "Sharon Harris Nuclear Plant",            "lat": 35.63, "lon":  -78.95, "state": "NC", "type": "nuclear"},
    # Pennsylvania
    {"name": "USS Clairton Coke Works",                "lat": 40.30, "lon":  -79.88, "state": "PA", "type": "steel"},
    {"name": "Monongahela Valley Complex",             "lat": 40.32, "lon":  -79.91, "state": "PA", "type": "steel"},
    {"name": "Keystone Generating Station",            "lat": 40.65, "lon":  -79.34, "state": "PA", "type": "coal"},
    # Washington
    {"name": "TransAlta Centralia Plant",              "lat": 46.76, "lon": -122.86, "state": "WA", "type": "gas"},
    {"name": "Intalco Aluminum Smelter",               "lat": 48.83, "lon": -122.72, "state": "WA", "type": "smelter"},
    # Oregon
    {"name": "Boardman Generating Station",            "lat": 45.71, "lon": -119.78, "state": "OR", "type": "coal"},
    {"name": "Hillsboro semiconductor corridor",       "lat": 45.52, "lon": -122.99, "state": "OR", "type": "industrial"},
    # Nevada
    {"name": "Valmy Generating Station",               "lat": 40.88, "lon": -117.13, "state": "NV", "type": "coal"},
    {"name": "Reid Gardner Generating Station",        "lat": 36.62, "lon": -114.45, "state": "NV", "type": "coal"},
    # Colorado
    {"name": "Cherokee Generating Station",            "lat": 39.80, "lon": -104.96, "state": "CO", "type": "gas"},
    {"name": "Pawnee Generating Station",              "lat": 40.22, "lon": -103.74, "state": "CO", "type": "coal"},
    {"name": "Suncor Commerce City Refinery",          "lat": 39.80, "lon": -104.94, "state": "CO", "type": "refinery"},
    # Utah
    {"name": "Kennecott Utah Copper Smelter",          "lat": 40.71, "lon": -112.21, "state": "UT", "type": "smelter"},
    {"name": "Intermountain Power Plant",              "lat": 39.51, "lon": -112.58, "state": "UT", "type": "coal"},
    # Minnesota
    {"name": "Sherburne County Generating Station",    "lat": 45.39, "lon":  -93.89, "state": "MN", "type": "coal"},
    {"name": "Flint Hills Resources Pine Bend",        "lat": 44.77, "lon":  -93.03, "state": "MN", "type": "refinery"},
    # Wisconsin
    {"name": "Oak Creek Power Plant",                  "lat": 42.85, "lon":  -87.84, "state": "WI", "type": "coal"},
    # Missouri
    {"name": "Labadie Energy Center",                  "lat": 38.56, "lon":  -90.85, "state": "MO", "type": "coal"},
    {"name": "Iatan Generating Station",               "lat": 39.37, "lon":  -94.96, "state": "MO", "type": "coal"},
    # Indiana
    {"name": "Gibson Generating Station",              "lat": 38.37, "lon":  -87.76, "state": "IN", "type": "coal"},
    {"name": "Rockport Generating Station",            "lat": 37.92, "lon":  -87.04, "state": "IN", "type": "coal"},
    # Kentucky
    {"name": "Paradise Fossil Plant",                  "lat": 37.26, "lon":  -86.98, "state": "KY", "type": "coal"},
    {"name": "Ghent Generating Station",               "lat": 38.74, "lon":  -85.03, "state": "KY", "type": "coal"},
    # Tennessee
    {"name": "Cumberland Fossil Plant",                "lat": 36.39, "lon":  -87.65, "state": "TN", "type": "coal"},
    # Alabama
    {"name": "James H. Miller Jr. Plant",              "lat": 33.63, "lon":  -87.06, "state": "AL", "type": "coal"},
    # Florida
    {"name": "Big Bend Power Station",                 "lat": 27.79, "lon":  -82.40, "state": "FL", "type": "gas"},
    {"name": "Turkey Point Nuclear",                   "lat": 25.44, "lon":  -80.33, "state": "FL", "type": "nuclear"},
    # Louisiana
    {"name": "Big Cajun II Plant",                     "lat": 30.71, "lon":  -91.36, "state": "LA", "type": "coal"},
    {"name": "Baton Rouge refinery corridor",          "lat": 30.49, "lon":  -91.19, "state": "LA", "type": "refinery"},
]


# ═══════════════════════════════════════════════════════════════════════
# ITEM 4 — SMOKE TESTS
# ═══════════════════════════════════════════════════════════════════════
def smoke_test() -> tuple[int, int]:
    """Run 6 endpoint checks + 1 scoring sanity check. Return (pass, fail)."""
    tests: list[tuple[str, str, callable]] = [
        ("health",
         f"{DCHUB_BASE}/health",
         lambda j: j.get("success") and j.get("data_points", {}).get("nonattainment_areas", 0) >= 16),
        ("nonattainment",
         f"{DCHUB_BASE}/nonattainment",
         lambda j: j.get("count", 0) >= 16 and j.get("data", {}).get("type") == "FeatureCollection"),
        ("monitors",
         f"{DCHUB_BASE}/monitors?lat=33.44&lon=-112.36&radius_km=300",
         lambda j: j.get("success") and isinstance(j.get("data"), list)),
        ("class1",
         f"{DCHUB_BASE}/class1",
         lambda j: j.get("count", 0) >= 20),
        ("sites",
         f"{DCHUB_BASE}/sites",
         lambda j: j.get("count", 0) >= 5),
        ("score-Goodyear-AZ",
         f"{DCHUB_BASE}/score?lat=33.44&lon=-112.36&capacity_mw=120",
         lambda j: j.get("success") and 0 <= j.get("data", {}).get("score", -1) <= 100
                   and "pathway" in j.get("data", {}) and "pollutants" in j.get("data", {})),
    ]
    passed = failed = 0
    for name, url, validator in tests:
        try:
            t0 = time.time()
            data = fetch_json(url, timeout=20)
            if validator(data):
                print(f"    ✓ {name:28s} {time.time()-t0:5.2f}s")
                passed += 1
            else:
                print(f"    ✗ {name:28s} validation failed")
                print(f"        response snippet: {str(data)[:200]}")
                failed += 1
        except urllib.error.HTTPError as e:
            print(f"    ✗ {name:28s} HTTP {e.code}")
            failed += 1
        except Exception as e:
            print(f"    ✗ {name:28s} {type(e).__name__}: {e}")
            failed += 1
    return passed, failed


# ═══════════════════════════════════════════════════════════════════════
# ITEM 5 — WEEKLY REFRESH CRON
# ═══════════════════════════════════════════════════════════════════════
REFRESH_CRON_TEMPLATE = """
# ─── Weekly auto-refresh: Replit Scheduled Task ────────────────────────
#
# In Replit sidebar: Tools -> Scheduled Tasks -> Create task
#   Name:     Air Permitting Weekly Refresh
#   Schedule: Every Sunday at 6:00 AM
#   Command:  python3 upgrade_air_permitting.py && python3 patch_main_air_permitting.py && git -C /home/runner/workspace add air_permitting_data.py && git -C /home/runner/workspace commit -m 'chore(air-permitting): weekly EPA refresh' --allow-empty && git -C /home/runner/workspace push
#
# Or as a plain Unix cron line (if not using Replit's scheduler):
#   0 6 * * 0 cd /home/runner/workspace && python3 upgrade_air_permitting.py >> /tmp/airperm-refresh.log 2>&1
#
# This pulls fresh EPA Green Book polygons + (if key set) AQS monitors,
# commits any diffs, and triggers Railway auto-redeploy.
"""


# ═══════════════════════════════════════════════════════════════════════
# FILE WRITERS
# ═══════════════════════════════════════════════════════════════════════
def update_data_file(new_monitors: list[dict]) -> bool:
    """Update MONITORS in air_permitting_data.py; preserve NA + CLASS1."""
    if not new_monitors:
        return False
    if not DATA_PATH.exists():
        print(f"  ! {DATA_PATH} not found — skipping data update.")
        return False

    text = DATA_PATH.read_text()
    # Replace the MONITORS = [...] block with new data
    import re
    new_block = "MONITORS = " + json.dumps(new_monitors, indent=2, ensure_ascii=False)
    pattern = re.compile(r"MONITORS\s*=\s*\[.*?\n\]", re.DOTALL)
    if pattern.search(text):
        text = pattern.sub(new_block, text)
    else:
        text = text.rstrip() + "\n\n" + new_block + "\n"
    DATA_PATH.write_text(text)
    return True


def write_extras() -> None:
    ts = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())
    content = f'''"""
air_permitting_extras.py — STATE_CONTEXT (51 states+DC) + NEI_SOURCES (expanded).
Generated by finalize_air_permitting.py on {ts}.

To activate in main.py:

    from air_permitting_extras import STATE_CONTEXT, NEI_SOURCES
    _AP_STATE_CONTEXT = STATE_CONTEXT
    _AP_NEI = NEI_SOURCES
"""
STATE_CONTEXT = {json.dumps(STATE_CONTEXT_FULL, indent=2, ensure_ascii=False)}

NEI_SOURCES = {json.dumps(NEI_EXPANDED, indent=2, ensure_ascii=False)}
'''
    EXTRAS_PATH.write_text(content)


# ═══════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════
def main():
    print("═" * 64)
    print("DC Hub Air Permitting — Finalize (5-item polish)")
    print("═" * 64)

    # Item 1 — AQS live monitors
    print("\n[1/5] AQS live monitors")
    monitors = pull_aqs()
    monitors_updated = update_data_file(monitors)
    if monitors:
        print(f"  ✓ Pulled {len(monitors)} monitors; {'updated' if monitors_updated else 'NOT updated'} {DATA_PATH}")
    # Item 2 — State context
    print("\n[2/5] All-50-states + DC permitting context")
    print(f"  ✓ Expanded from 16 -> {len(STATE_CONTEXT_FULL)} jurisdictions")

    # Item 3 — NEI expansion
    print("\n[3/5] NEI stationary sources")
    print(f"  ✓ Expanded from 22 -> {len(NEI_EXPANDED)} sources across DC markets")

    # Write extras (items 2+3 together)
    write_extras()
    print(f"  ✓ Wrote {EXTRAS_PATH} ({EXTRAS_PATH.stat().st_size:,} bytes)")

    # Item 4 — smoke tests
    print("\n[4/5] Live endpoint smoke tests")
    passed, failed = smoke_test()
    print(f"  {'✓' if failed == 0 else '✗'} Result: {passed}/{passed+failed} passed")

    # Item 5 — cron template
    print("\n[5/5] Weekly auto-refresh cron template")
    print(REFRESH_CRON_TEMPLATE)

    # Summary
    print("═" * 64)
    print("Summary")
    print("═" * 64)
    print(f"  AQS monitors:     {len(monitors):5d} (was 15 seed)")
    print(f"  States + DC:      {len(STATE_CONTEXT_FULL):5d} (was 16)")
    print(f"  NEI sources:      {len(NEI_EXPANDED):5d} (was 22)")
    print(f"  Smoke tests:      {passed}/{passed+failed} passed")
    print()
    print("Next:")
    files_to_add = ["air_permitting_extras.py"]
    if monitors_updated:
        files_to_add.insert(0, "air_permitting_data.py")
    print(f"  git add {' '.join(files_to_add)}")
    print(f"  git commit -m 'feat(air-permitting): final polish — 51-state context + expanded NEI + live AQS'")
    print(f"  git push")
    print()
    print("Then one small edit in main.py (optional for state/NEI expansion):")
    print("  Replace inline _AP_STATE_CONTEXT and _AP_NEI with:")
    print("    from air_permitting_extras import STATE_CONTEXT as _AP_STATE_CONTEXT")
    print("    from air_permitting_extras import NEI_SOURCES as _AP_NEI")
    print("═" * 64)


if __name__ == "__main__":
    main()
