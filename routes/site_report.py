"""
DC Hub — Site Intelligence Report (auto-generated from a lat/lon).

GET /api/v1/site-report?lat=&lon=&use_case=&latency_target=&prepared_for=&capacity_mw=&format=

The automated version of Martone Advisors' manual site-survey template
(~/Downloads/dchub-site-report/site-report-template.html). Reproduces the exact
dark DC Hub brand and the same sections — cover scorecards + Power/Transmission,
Natural Gas, Water & Drought, Air Permitting, Fiber, Latency, Summary, About —
auto-populated from DC Hub's OWN data (the same sources the land+power map uses).

PDF rendering (weasyprint) is wired behind &format=pdf, matching the existing
routes/market_brief.py pattern (the Railway-proven engine).

ACCURACY (hard rule): every figure comes from real data. Where a value is
unknown we render "—" / "Not published" rather than inventing it, and all
estimates are labelled point-in-time — same contract as the template footer.

Data sources (imported directly — NO synchronous self-HTTP calls):
  • Power   : site_planner.find_nearest_substations / find_nearest_transmission
  • Gas     : site_planner.find_nearby_gas_pipelines
  • Fiber   : fiber_routes table (via site_planner.execute_query)
  • Air     : main._ap_score_site  (EPA Green Book / AQS / NPS / NEI scorer)
  • Water   : USDM (point + state) → ports js/water-drought-layer.js calculateWaterRisk
  • Latency : great-circle distance × fiber route factor to a chosen interconnect hub

Gated PRO (premium deliverable) via routes.tier_gate.require_tier.
"""

import re
import math
import time
import datetime
import concurrent.futures as _cf

from flask import Blueprint, request, Response, jsonify

site_report_bp = Blueprint("site_report", __name__)

# Per-(lat,lon,target,capacity) data cache — first build hits external sources
# (USDM); subsequent calls within the TTL are instant. Keeps a deliberate,
# user-initiated PRO endpoint from re-hammering USGS/USDM and protects the
# single-replica worker pool.
_CACHE = {}
_CACHE_TTL = 3600  # 1h

# ── Latency interconnect hubs (major carrier-hotel / cloud on-ramps) ──────────
LATENCY_TARGETS = {
    "dallas":        (32.7888, -96.8023, "Dallas (Infomart · 2323 Bryan)"),
    "ashburn":       (39.0438, -77.4874, "Ashburn, VA (Data Center Alley)"),
    "chicago":       (41.8540, -87.6094, "Chicago (350 E Cermak)"),
    "silicon valley":(37.3541, -121.9552, "Silicon Valley (Santa Clara)"),
    "santa clara":   (37.3541, -121.9552, "Santa Clara, CA"),
    "phoenix":       (33.4255, -112.0696, "Phoenix, AZ"),
    "atlanta":       (33.7490, -84.3880, "Atlanta (56 Marietta)"),
    "new york":      (40.7178, -74.0102, "New York (60 Hudson)"),
    "los angeles":   (34.0448, -118.2549, "Los Angeles (One Wilshire)"),
    "seattle":       (47.6045, -122.3347, "Seattle (Westin Building)"),
}
DEFAULT_TARGET = "dallas"
# Fiber route factor: physical fiber rarely follows the great circle. ~1.4 is a
# common screening multiplier (matches the manual Raymondville→Dallas case:
# great-circle ~440 mi × 1.4 ≈ 615 mi vs the fiber-locator's 619 mi).
FIBER_ROUTE_FACTOR = 1.4
# Light in fiber ≈ 2/3 c ≈ 200,000 km/s one-way → ~5 µs/km → RTT ≈ km/100 ms.
KM_PER_MS_RTT = 100.0
# Operator convention (2026-06-25): screening latency to the nearest carrier hotel
# ≈ 1 ms per ~50 measured miles (site → closest carrier-hotel building).
MILES_PER_MS = 50.0

# ── Water composite constants (ported verbatim from water-drought-layer.js) ──
_SEVERITY = {"None": 100, "D0": 80, "D1": 60, "D2": 40, "D3": 20, "D4": 0}
_DROUGHT_NAME = {
    "None": "No Drought", "D0": "Abnormally Dry (D0)", "D1": "Moderate Drought (D1)",
    "D2": "Severe Drought (D2)", "D3": "Extreme Drought (D3)", "D4": "Exceptional Drought (D4)",
}
_WATER_STRESS = {
    "AZ": 25, "NV": 30, "CA": 20, "UT": 22, "NM": 25, "CO": 18,
    "TX": 15, "OK": 12, "KS": 10, "OR": 8, "WA": 5, "ID": 10,
    "VA": 3, "OH": 2, "IL": 2, "GA": 5, "NC": 4,
}
_AQUIFER_STATES = {"TX", "NE", "KS", "OK", "IL", "IN", "OH"}

_FIPS = {
    "AL": "01", "AK": "02", "AZ": "04", "AR": "05", "CA": "06", "CO": "08", "CT": "09", "DE": "10",
    "FL": "12", "GA": "13", "HI": "15", "ID": "16", "IL": "17", "IN": "18", "IA": "19", "KS": "20",
    "KY": "21", "LA": "22", "ME": "23", "MD": "24", "MA": "25", "MI": "26", "MN": "27", "MS": "28",
    "MO": "29", "MT": "30", "NE": "31", "NV": "32", "NH": "33", "NJ": "34", "NM": "35", "NY": "36",
    "NC": "37", "ND": "38", "OH": "39", "OK": "40", "OR": "41", "PA": "42", "RI": "44", "SC": "45",
    "SD": "46", "TN": "47", "TX": "48", "UT": "49", "VT": "50", "VA": "51", "WA": "53", "WV": "54",
    "WI": "55", "WY": "56",
}
_STATE_NAME = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California",
    "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware", "FL": "Florida", "GA": "Georgia",
    "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas",
    "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts",
    "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi", "MO": "Missouri", "MT": "Montana",
    "NE": "Nebraska", "NV": "Nevada", "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico",
    "NY": "New York", "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma",
    "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina",
    "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont",
    "VA": "Virginia", "WA": "Washington", "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming",
}

# Pretty criteria-pollutant labels for the air section tags.
_POLLUTANT_LABEL = {"NO2": "NO₂", "O3": "O₃", "SO2": "SO₂", "PM2.5": "PM2.5", "PM10": "PM10",
                    "CO": "CO", "Pb": "Pb", "GHG": "GHG"}


# ════════════════════════════════════════════════════════════════════════════
#  Small utilities
# ════════════════════════════════════════════════════════════════════════════
def _haversine_mi(lat1, lon1, lat2, lon2):
    R = 3959.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _coords_str(lat, lon):
    # Use a Unicode minus for negatives so it matches the template's "−97.7535".
    def f(v):
        return f"{v:.4f}".replace("-", "−")
    return f"{f(lat)}, {f(lon)}"


def _state_for(lat, lon):
    try:
        from routes.dcgi import _lat_lng_to_state
        s = (_lat_lng_to_state(lat, lon) or "").upper()
        if len(s) == 2 and s in _FIPS:
            return s
    except Exception:
        pass
    # Rough bounding-box fallback for the lower 48 (best-effort only).
    if 25.8 <= lat <= 36.5 and -106.6 <= lon <= -93.5:
        return "TX"
    return None


def _reverse_geocode_state(lat, lon):
    """Accurate coords→US state (2-letter) via ArcGIS reverse-geocode — replaces the
    rectangular-bbox guess that mis-buckets BORDER parcels (Ashburn VA→MD, Carrier
    Mills IL→KY, TX-panhandle→OK). Failure-isolated, short timeout → None so the
    caller falls back to _state_for. Cached with the 1h survey, so ~1 call per build."""
    try:
        import requests
        r = requests.get(
            "https://geocode.arcgis.com/arcgis/rest/services/World/GeocodeServer/reverseGeocode",
            params={"location": f"{lon},{lat}", "f": "json",
                    "outFields": "RegionAbbr,Region,CountryCode"},
            timeout=4)
        addr = (r.json() or {}).get("address") or {}
        cc = (addr.get("CountryCode") or "").upper()
        if cc and cc not in ("USA", "US"):
            return None  # non-US — _state_for / None handles it
        ab = (addr.get("RegionAbbr") or "").strip().upper()
        if len(ab) == 2 and ab in _FIPS:
            return ab
        reg = (addr.get("Region") or "").strip().lower()
        if reg:
            for code, name in _STATE_NAME.items():
                if name.lower() == reg:
                    return code
    except Exception:
        pass
    return None


def _fmt_kv(voltage):
    try:
        v = float(voltage)
        if v <= 0:
            return None
        return f"{int(round(v))} kV"
    except (TypeError, ValueError):
        return None


def _fmt_mi(d):
    try:
        d = float(d)
    except (TypeError, ValueError):
        return None
    return "on-site" if d < 0.3 else (f"{d:.1f} mi" if d < 10 else f"{d:.0f} mi")


def _score_color(s):
    try:
        s = float(s)
    except (TypeError, ValueError):
        return "amb"
    return "grn" if s >= 70 else ("amb" if s >= 40 else "red")


def _dist_color(mi, near=3, mid=15):
    if mi is None:
        return "amb"
    return "grn" if mi < near else ("amb" if mi < mid else "red")


_JUNK_NAME = re.compile(r"^(unknown|unnamed|null|none|n/?a|substation|pipeline)[\W_]*\d*$", re.I)


def _clean_name(name, fallback):
    """Replace junk import placeholders (UNKNOWN306108, NULL, '') with a clean
    fallback so customer-facing reports never show database scaffolding."""
    n = (str(name).strip() if name is not None else "")
    if not n or _JUNK_NAME.match(n):
        return fallback, False
    return n, True


def _call_with_timeout(fn, timeout, *args, **kwargs):
    """Run fn with a hard wall-clock cap. Returns None on timeout/error — keeps
    a flaky external fallback (e.g. the 15s HIFLD transmission probe) from
    blowing the whole report's latency budget / holding a worker too long.

    r-realcap (2026-08-25): this used `with _cf.ThreadPoolExecutor(...) as ex:`.
    ThreadPoolExecutor.__exit__ calls shutdown(wait=True), which JOINS the worker
    — so on timeout the caller went right on blocking for the full duration of
    the call it had just given up on. The cap was documented and never once
    enforced: measured, a 6s cap on a 10s call returned None after 10.01s. That
    is how a report took 18s while every timeout in this file said 6.

    Build the executor by hand and shut it down WITHOUT waiting. Python cannot
    kill a thread, so the orphan still runs to completion in the background —
    but it no longer holds the request. Its DB work is bounded by the query
    itself and its one external call by requests(timeout=15).
    """
    ex = _cf.ThreadPoolExecutor(max_workers=1)
    try:
        return ex.submit(fn, *args, **kwargs).result(timeout=timeout)
    except Exception:
        return None
    finally:
        ex.shutdown(wait=False, cancel_futures=True)


def _pdf_diag():
    """Best-effort diagnostics for why weasyprint can't load its native libs.
    Only invoked on the PDF failure path; never raises. Tells us the runtime
    LD_LIBRARY_PATH and where (if anywhere) libgobject actually lives, so the
    fix is precise instead of guessed."""
    import os as _os, glob as _glob
    d = {}
    try:
        d["LD_LIBRARY_PATH"] = (_os.environ.get("LD_LIBRARY_PATH") or "")[:500]
    except Exception:
        pass
    try:
        import ctypes.util as _cu
        d["find_gobject"] = _cu.find_library("gobject-2.0")
        d["find_pango"] = _cu.find_library("pango-1.0")
    except Exception as _e:
        d["find_err"] = str(_e)[:100]
    found = []
    for pat in ("/usr/lib/x86_64-linux-gnu/libgobject-2.0.so*",
                "/nix/store/*-glib-*/lib/libgobject-2.0.so*",
                "/nix/store/*/lib/libgobject-2.0.so*"):
        try:
            found += _glob.glob(pat)[:3]
            if found:
                break
        except Exception:
            pass
    d["libgobject_paths"] = found[:6]
    # Definitive: is the glib nix package even installed? Lists /nix/store dirs
    # whose name contains 'glib' (the package providing libgobject-2.0).
    try:
        import os as _os2
        if _os2.path.isdir("/nix/store"):
            d["nix_glib_dirs"] = [x for x in _os2.listdir("/nix/store")
                                  if "glib" in x.lower() and "-" in x][:6]
        else:
            d["nix_store"] = "absent"
    except Exception as _e:
        d["nix_glib_err"] = str(_e)[:80]
    # Definitive: do the apt libs reach the RUNTIME Debian multiarch dir, and is
    # libgobject in the ldconfig cache? Answers "do nixpacks aptPkgs persist to
    # runtime?" without another guess.
    try:
        import os as _os3, subprocess as _sp
        _d2 = "/usr/lib/x86_64-linux-gnu"
        d["usrlib_libs"] = ([x for x in _os3.listdir(_d2)
                             if ("pango" in x or "gobject" in x or "cairo" in x)][:8]
                            if _os3.path.isdir(_d2) else "dir-absent")
        try:
            _out = _sp.run(["ldconfig", "-p"], capture_output=True, text=True, timeout=4).stdout
            d["ldconfig_gobject"] = [l.strip() for l in _out.splitlines() if "gobject" in l][:2]
        except Exception:
            d["ldconfig"] = "n/a"
    except Exception as _e:
        d["usrlib_err"] = str(_e)[:80]
    return d


# ════════════════════════════════════════════════════════════════════════════
#  Section data gatherers — each isolated; a failure renders "—", never a 500
# ════════════════════════════════════════════════════════════════════════════
def _gather_power(lat, lon, state):
    out = {"_score": None}
    try:
        from site_planner import (find_nearest_substations, find_nearest_transmission,
                                   identify_iso_region)
    except Exception as e:
        out["assessment"] = f"Power layer unavailable ({type(e).__name__})."
        return out

    iso = {}
    try:
        iso = identify_iso_region(lat, lon, state) or {}
    except Exception:
        iso = {}
    iso_name = iso.get("name") or "—"

    subs = []
    try:
        subs = find_nearest_substations(lat, lon, limit=3, max_distance_miles=50) or []
    except Exception:
        subs = []
    sub = subs[0] if subs else None
    real_name = False

    if sub:
        dist = sub.get("distance_miles")
        try:
            dist = float(dist)
        except (TypeError, ValueError):
            dist = None
        on_parcel = dist is not None and dist < 0.5
        volt_label = _fmt_kv(sub.get("voltage_kv"))
        name, real_name = _clean_name(sub.get("name"),
                                      f"{volt_label} substation" if volt_label else "Unnamed substation")
        out["substation"] = name
        out["substation_note"] = ("On or directly adjacent to the candidate parcel."
                                   if on_parcel else
                                   f"Nearest mapped substation — {_fmt_mi(dist) or '—'} from the site.")
        out["voltage"] = volt_label or "—"
        out["operator"] = sub.get("operator") or "—"
        out["substation_source"] = ("OpenStreetMap (live)" if sub.get("source") == "OpenStreetMap"
                                     else "DC Hub grid layer · HIFLD/Neon")
        out["_dist"] = dist
        out["_volt"] = sub.get("voltage_kv") or 0
    else:
        out["substation"] = "—"
        out["substation_note"] = "No mapped substation within 50 mi in DC Hub's grid layer."
        out["voltage"] = "—"
        out["operator"] = "—"
        out["substation_source"] = "DC Hub grid layer · HIFLD/Neon"
        out["_dist"] = None
        out["_volt"] = 0

    # Transmission line — only probe when we have a REAL substation name. A junk
    # placeholder name guarantees find_nearest_transmission's internal fuzzy
    # match misses and falls back to a ~15s HIFLD live call. Hard-cap at 6s too.
    tx = None
    if sub and real_name:
        tx = _call_with_timeout(find_nearest_transmission, 6, lat, lon, max_distance_miles=25)

    if tx:
        out["line_voltage"] = _fmt_kv(tx.get("voltage_kv")) or out.get("voltage", "—")
        out["line_owner"] = tx.get("owner") or out.get("operator", "—")
        out["line_source"] = "HIFLD electric grid (live)" if tx.get("distance_miles") == "N/A (live query)" else "HIFLD electric grid"
        # Backfill the substation operator from the line owner when the
        # substation record lacks one (common in HIFLD).
        if out.get("operator") in (None, "—") and tx.get("owner"):
            out["operator"] = tx.get("owner")
    else:
        out["line_voltage"] = out.get("voltage", "—")
        out["line_owner"] = out.get("operator", "—")
        out["line_source"] = "HIFLD electric grid"

    # We never invent transfer/headroom — DC Hub does not hold published
    # per-substation available capacity. A load study is the honest answer.
    out["transfer"] = "Load study required"
    out["headroom"] = "Not published"
    out["headroom_color"] = "amb"
    out["iso"] = iso_name

    # Power-readiness scorecard (0–100) from REAL proximity + voltage class.
    score = None
    if out["_dist"] is not None:
        d = out["_dist"]
        v = float(out["_volt"] or 0)
        base = 90 if d < 0.5 else 78 if d < 3 else 60 if d < 10 else 42 if d < 25 else 28
        vbonus = 10 if v >= 345 else 6 if v >= 138 else 0
        score = min(100, base + vbonus)
    out["_score"] = score

    parts = []
    if out["substation"] != "—":
        vtxt = f" ({out['voltage']})" if out["voltage"] != "—" else ""
        parts.append(f"The nearest mapped substation, {out['substation']}{vtxt}, is "
                     f"{_fmt_mi(out['_dist']) or '—'} from the site"
                     + (f" — operated by {out['operator']}" if out['operator'] != '—' else "") + ".")
    if iso_name != "—":
        parts.append(f"The site sits in the {iso_name} territory.")
    parts.append("DC Hub does not publish per-substation transfer headroom, so a load study with "
                 "the interconnecting utility is the critical-path item to confirm campus-scale capacity.")
    out["assessment"] = " ".join(parts)
    return out


def _gather_gas(lat, lon, state):
    out = {"_score": None}
    try:
        from site_planner import find_nearby_gas_pipelines
    except Exception as e:
        out["assessment"] = f"Gas layer unavailable ({type(e).__name__})."
        return out
    g = {}
    try:
        g = find_nearby_gas_pipelines(lat, lon, radius_miles=50, limit=5) or {}
    except Exception:
        g = {}
    np_ = g.get("nearest_pipeline") if g else None
    if np_ and (g.get("count") or 0) > 0:
        dist = np_.get("distance_miles")
        try:
            dist = float(dist)
        except (TypeError, ValueError):
            dist = None
        row0 = (g.get("pipelines") or [{}])[0]
        status = (row0.get("status") or "Operating")
        out["name"] = np_.get("name") or "Mapped pipeline"
        out["type"] = (np_.get("type") or "Natural gas").title() if np_.get("type") else "Natural gas"
        out["status"] = str(status).title()
        out["status_color"] = "grn" if str(status).lower() in ("active", "operating", "in service") else "amb"
        out["operator"] = np_.get("operator") or "—"
        out["region"] = f"{_STATE_NAME.get(state, state or 'United States')} · United States"
        out["source"] = "DC Hub gas layer · HIFLD/Neon"
        out["_dist"] = dist
        access = g.get("gas_access", "")
        out["_score"] = (90 if dist is not None and dist < 1 else
                         78 if dist is not None and dist < 3 else
                         60 if dist is not None and dist < 10 else
                         42 if dist is not None and dist < 20 else 28) if dist is not None else None
        out["assessment"] = (
            f"{('An ' + access.lower()) if access else 'A'} gas posture — the nearest mapped pipeline "
            f"({out['name']}{', ' + out['operator'] if out['operator'] != '—' else ''}) is "
            f"{_fmt_mi(dist) or '—'} from the site. For behind-the-meter generation or backup, a "
            f"tap-feasibility and capacity-release inquiry to the operator would confirm deliverable volumes."
        )
    else:
        out["name"] = "—"
        out["type"] = "—"
        out["status"] = "—"
        out["operator"] = "—"
        out["region"] = f"{_STATE_NAME.get(state, state or 'United States')}"
        out["source"] = "DC Hub gas layer · HIFLD/Neon"
        out["_dist"] = None
        out["_score"] = None
        out["assessment"] = ("No natural-gas pipeline detected within 50 mi in DC Hub's pipeline layer. "
                             "If on-site generation is required, confirm midstream access directly with operators.")
    return out


def _usdm_point_level(lat, lon):
    """Worst USDM drought class intersecting the point. Returns 'None'..'D4',
    or None if the source is unreachable (distinct from 'None' = no drought)."""
    try:
        import requests
        url = ("https://services5.arcgis.com/0OTVzJS4K09zlixn/arcgis/rest/services/"
               "USDM_current/FeatureServer/0/query")
        params = {
            "geometry": f"{lon},{lat}", "geometryType": "esriGeometryPoint",
            "inSR": "4326", "spatialRel": "esriSpatialRelIntersects",
            "outFields": "DM", "returnGeometry": "false", "f": "json",
        }
        r = requests.get(url, params=params, timeout=5)
        d = r.json()
        feats = d.get("features") or []
        if not feats:
            return "None"
        dms = []
        for f in feats:
            try:
                dms.append(int((f.get("attributes") or {}).get("DM")))
            except (TypeError, ValueError):
                continue
        if not dms:
            return "None"
        return {0: "D0", 1: "D1", 2: "D2", 3: "D3", 4: "D4"}.get(max(dms), "None")
    except Exception:
        return None


def _usdm_state_stats(state):
    """Latest USDM categorical drought coverage for a state (d0..d4 %)."""
    fips = _FIPS.get(state)
    if not fips:
        return None
    try:
        import requests
        url = ("https://usdmdataservices.unl.edu/api/StateStatistics/"
               "GetDroughtSeverityStatisticsByAreaPercent")
        today = datetime.date.today()
        start = today - datetime.timedelta(days=42)
        params = {
            "aoi": fips,
            "startdate": f"{start.month}/{start.day}/{start.year}",
            "enddate": f"{today.month}/{today.day}/{today.year}",
            "statisticsType": 2,
        }
        r = requests.get(url, params=params, timeout=7, headers={"Accept": "application/json"})
        rows = r.json()
        if not isinstance(rows, list) or not rows:
            return None
        rows.sort(key=lambda x: x.get("mapDate") or "", reverse=True)
        latest = rows[0]

        def p(k):
            try:
                return round(float(latest.get(k) or 0), 2)
            except (TypeError, ValueError):
                return 0.0
        return {"d0": p("d0"), "d1": p("d1"), "d2": p("d2"), "d3": p("d3"), "d4": p("d4"),
                "none": p("none"), "map_date": (latest.get("mapDate") or "")[:10]}
    except Exception:
        return None


def _gather_water(lat, lon, state):
    out = {"_score": None}
    point = _usdm_point_level(lat, lon)            # 'None'..'D4' or None
    stats = _usdm_state_stats(state)               # dict or None

    if point is None and stats is None:
        out["score"] = "—"
        out["label"] = "Not assessed"
        out["drought"] = "—"
        out["drought_note"] = "USDM source unreachable at request time — re-run to assess."
        out["assessed_at"] = _coords_str(lat, lon)
        out["source"] = "USDM · USGS hydrology"
        out["assessment"] = ("Water/drought data could not be retrieved from the U.S. Drought Monitor "
                             "at the time of this report. This is a source-availability gap, not a "
                             "site condition — re-generate to populate.")
        return out

    # Composite (calculateWaterRisk port). Unknown point → treat factor-1 as no
    # deduction but flag it; state coverage + regional stress + aquifer still apply.
    lvl = point if point is not None else "None"
    score = 100.0
    score -= (100 - _SEVERITY.get(lvl, 100)) * 0.40
    if stats:
        score -= (stats["d2"] + stats["d3"] + stats["d4"]) * 0.30
    score -= _WATER_STRESS.get(state, 5) * 0.20
    if state in _AQUIFER_STATES:
        score += 5
    score = int(round(max(0, min(100, score))))
    out["_score"] = score
    out["score"] = str(score)
    out["label"] = ("Excellent Water Availability" if score >= 85 else
                    "Good Water Availability" if score >= 70 else
                    "Moderate Water Availability" if score >= 50 else
                    "Limited Water Availability" if score >= 30 else "Scarce — water-constrained")

    if point is not None:
        out["drought"] = _DROUGHT_NAME.get(point, "No Drought")
        out["drought_note"] = f"At-point classification — U.S. Drought Monitor"
    elif stats:
        worst = ("D4" if stats["d4"] > 0 else "D3" if stats["d3"] > 0 else "D2" if stats["d2"] > 0
                 else "D1" if stats["d1"] > 0 else "D0" if stats["d0"] > 0 else "None")
        out["drought"] = _DROUGHT_NAME.get(worst, "No Drought")
        out["drought_note"] = "State-level (at-point lookup unavailable) — U.S. Drought Monitor"
    out["assessed_at"] = _coords_str(lat, lon)
    md = (stats or {}).get("map_date")
    out["source"] = f"USDM ({md}) · USGS hydrology" if md else "USDM · USGS hydrology"

    cooling = ("supports evaporative or hybrid cooling" if score >= 70 else
               "may constrain evaporative cooling — favour closed-loop / air-cooled designs" if score >= 40 else
               "is water-constrained — plan for air-cooled / closed-loop cooling")
    out["assessment"] = (
        f"Water Availability scores {score}/100 ({out['drought'].lower()}). This {cooling}. "
        f"Confirm a specific raw/reclaimed source and a municipal or groundwater allocation letter "
        f"to lock the cooling design."
    )
    return out


def _gather_air(lat, lon, capacity_mw):
    out = {"_score": None}
    try:
        import main  # fully loaded at request time; _ap_score_site lives here
        r = main._ap_score_site(lat, lon, float(capacity_mw))
    except Exception as e:
        out["risk"] = "—"
        out["risk_note"] = f"Air-permitting scorer unavailable ({type(e).__name__})."
        out["pathway"] = "—"
        out["assessed_at"] = _coords_str(lat, lon)
        out["pollutants"] = []
        out["offset"] = "—"
        out["context"] = ""
        out["context_label"] = "Context"
        out["sources"] = "EPA Green Book · AQS · NPS FLM · NEI"
        return out

    pollutants = r.get("pollutants") or {}
    reds = sum(1 for p in pollutants.values() if p.get("s") == "red")
    yellows = sum(1 for p in pollutants.values() if p.get("s") == "yellow")
    risk = ("High" if reds >= 2 else "Moderate" if reds == 1 else
            "Elevated" if yellows >= 2 else "Low")
    state = r.get("state")
    out["risk"] = risk
    out["risk_note"] = r.get("verdict_short") or ""
    out["pathway"] = r.get("pathway") or "—"
    out["assessed_at"] = _coords_str(lat, lon)
    out["pollutants"] = [_POLLUTANT_LABEL.get(k, k) for k in pollutants.keys()]
    out["offset"] = r.get("offset_estimate_usd") or "—"
    out["context"] = r.get("state_context") or ""
    out["context_label"] = f"{_STATE_NAME.get(state, state)} context" if state else "Regulatory context"
    out["sources"] = "EPA Green Book · AQS monitor design values · NPS FLM · NEI"
    out["_capacity_mw"] = float(capacity_mw)
    # Air scorecard uses (100 − permitting-risk-score) inverted? No: r['score'] is
    # already a 0–100 where HIGHER = cleaner/easier. Use it directly for the card.
    try:
        out["_score"] = int(r.get("score"))
    except (TypeError, ValueError):
        out["_score"] = None
    return out


# Real transport/access carriers vs. DC operators, hyperscalers, content & IX
# aggregates that pollute fiber_routes.provider (r-fiber-carriers 2026-06-25:
# Ashburn was showing "Equinix · Meta · CyrusOne · Digital Realty" as carriers).
_NON_CARRIER_TOKENS = {
    "equinix", "cyrusone", "coresite", "databank", "vantage", "edgeconnex", "sabey",
    "flexential", "aligned", "switchds", "compass", "meta", "facebook", "amazon",
    "aws", "google", "microsoft", "azure", "oracle", "apple", "alibaba", "tencent",
    "cloudflare", "akamai", "fastly", "netflix", "peeringdb", "ixp", "ix",
    "edison", "entergy", "ameren", "evergy", "exelon", "firstenergy", "pseg", "xcel",
}
_NON_CARRIER_PHRASES = ("digital realty", "digitalrealty", "iron mountain",
                        "stack infrastructure", "qts ", "& power", "electric power",
                        "power company", "power co", "electric coop", "electric co-op",
                        "rural electric", "municipal")


def _is_real_carrier(name):
    """True for actual transport/access carriers; False for DC operators,
    hyperscalers, content networks and IX aggregates."""
    if not name:
        return False
    n = str(name).lower().strip()
    if not n:
        return False
    import re as _re
    if _re.match(r"^\d+\+?\s*carrier", n):      # aggregate label e.g. "20+ Carriers"
        return False
    if "/ix" in n or "peeringdb" in n:
        return False
    if any(p in n for p in _NON_CARRIER_PHRASES):
        return False
    toks = set(_re.findall(r"[a-z0-9.&]+", n))
    return not (toks & _NON_CARRIER_TOKENS)


def _carrier_key(name):
    """Collapse name variants so 'Summit IG'/'SummitIG' and 'GTT Communications
    (AS3257)'/'GTT Communications, Inc.' dedupe to one carrier."""
    import re as _re
    n = _re.sub(r"\(as\d+\)", "", str(name).lower())
    n = _re.sub(r",?\s*\b(inc|llc|ltd|corp|communications|networks?|global|wholesale|"
                r"technologies|telecom|fibre|fiber|group|company)\b.*$", "", n)
    n = _re.sub(r"[^a-z0-9]", "", n)
    return n[:12] or str(name).lower()


def _nearest_carrier_hotel(lat, lon, min_carriers=25, max_mi=400):
    """Nearest REAL carrier hotel = nearest carrier_facility_presence facility with
    a high distinct-carrier count (a true interconnection building, e.g. NTT
    Ashburn VA8 / Equinix Ashburn — NOT a single-tenant cloud on-ramp). Used as the
    latency target and named in the fiber section. Failure-isolated → None."""
    try:
        from site_planner import execute_query
    except Exception:
        return None
    # Bounding-box prefilter (carrier_facility_presence is ~600K global rows with no
    # spatial index — a full GROUP BY over all of it times out). Prune to a regional
    # box first so the distance/COUNT only runs on nearby facilities.
    deg_lat = max_mi / 69.0
    deg_lng = max_mi / (69.0 * max(0.1, abs(math.cos(math.radians(lat)))))
    d = ("3959*acos(LEAST(1.0, GREATEST(-1.0, "
         "cos(radians(%s))*cos(radians(facility_lat))*cos(radians(facility_lng)-radians(%s)) "
         "+ sin(radians(%s))*sin(radians(facility_lat)))))")
    q = (f"WITH f AS (SELECT facility_name, facility_city, facility_state, facility_lat, "
         f"facility_lng, COUNT(DISTINCT carrier_name) ncar, MIN({d}) dist "
         f"FROM carrier_facility_presence WHERE facility_lat IS NOT NULL "
         f"AND facility_lat BETWEEN %s AND %s AND facility_lng BETWEEN %s AND %s "
         f"GROUP BY 1,2,3,4,5) "
         f"SELECT facility_name, facility_city, facility_state, facility_lat, facility_lng, "
         f"ncar, dist FROM f WHERE ncar >= %s AND dist <= %s ORDER BY dist ASC LIMIT 1")
    try:
        rows = execute_query(q, (lat, lon, lat,
                                 lat - deg_lat, lat + deg_lat,
                                 lon - deg_lng, lon + deg_lng,
                                 min_carriers, max_mi)) or []
    except Exception:
        return None
    if not rows:
        return None
    r = rows[0]
    try:
        return {
            "name": (r.get("facility_name") or "carrier hotel").strip(),
            "city": (r.get("facility_city") or "").strip(),
            "state": (r.get("facility_state") or "").strip(),
            "lat": float(r.get("facility_lat")),
            "lng": float(r.get("facility_lng")),
            "carriers": int(r.get("ncar") or 0),
            "distance_mi": float(r.get("dist")),
        }
    except (TypeError, ValueError):
        return None


def _fiber_nearby_providers(lat, lon, radius_miles=25, limit=8):
    try:
        from site_planner import execute_query
    except Exception:
        return []
    deg_lat = radius_miles / 69.0
    deg_lng = radius_miles / (69.0 * max(0.1, abs(math.cos(math.radians(lat)))))
    q = """
        SELECT provider,
               MIN(3959*acos(LEAST(1.0, GREATEST(-1.0,
                   cos(radians(%s))*cos(radians(start_lat))*cos(radians(start_lng)-radians(%s)) +
                   sin(radians(%s))*sin(radians(start_lat)))))) AS dist_mi,
               COUNT(*) AS n
        FROM fiber_routes
        WHERE start_lat IS NOT NULL AND start_lng IS NOT NULL
          AND start_lat BETWEEN %s AND %s
          AND start_lng BETWEEN %s AND %s
          AND provider IS NOT NULL AND provider <> ''
        GROUP BY provider
        ORDER BY dist_mi ASC
        LIMIT %s;
    """
    try:
        rows = execute_query(q, (lat, lon, lat,
                                 lat - deg_lat, lat + deg_lat,
                                 lon - deg_lng, lon + deg_lng, limit)) or []
        return rows
    except Exception:
        return []


def _gather_fiber(lat, lon):
    out = {"_score": None}
    rows = _fiber_nearby_providers(lat, lon, radius_miles=50, limit=40)
    # Keep only real transport/access carriers (drop DC operators / hyperscalers /
    # IX aggregates), dedupe name variants, nearest first.
    seen = {}  # carrier_key -> [display_name, nearest_dist_mi]
    for r in rows:
        name = r.get("provider")
        if not _is_real_carrier(name):
            continue
        try:
            d = float(r.get("dist_mi"))
        except (TypeError, ValueError):
            d = 99.0
        k = _carrier_key(name)
        if k not in seen:
            seen[k] = [name, d]
        else:
            if len(name) < len(seen[k][0]):
                seen[k][0] = name        # cleaner (shorter) display variant
            if d < seen[k][1]:
                seen[k][1] = d           # keep nearest distance
    carriers = sorted(seen.values(), key=lambda x: x[1])
    adj = [c[0] for c in carriers if c[1] < 5]
    near = [c[0] for c in carriers if c[1] >= 5]
    out["adjacent"] = " · ".join(adj[:5]) if adj else "—"
    out["nearby"] = " · ".join(near[:5]) if near else "—"
    out["nearby_dist"] = "Within ~50 mi"
    out["source"] = "DC Hub fiber routes · PeeringDB carrier presence"
    n = len(carriers)
    out["_count"] = n
    out["_score"] = (90 if n >= 4 else 72 if n >= 2 else 45 if n >= 1 else 20)
    if n:
        lead = (f" — including {adj[0]} adjacent" if adj else (f" — nearest {near[0]}" if near else ""))
        out["assessment"] = (
            f"DC Hub maps {n} fiber carrier{'s' if n != 1 else ''} within ~50 mi of the site"
            + lead
            + ". For exact entrance-facility distances and dark-fiber / wave availability, pair this "
            "with a fiber-locator route export (attachable to this report)."
        )
    else:
        out["assessment"] = (
            "No transport carriers are mapped within ~50 mi in DC Hub's fiber layer for this point. "
            "Carrier presence may still exist — confirm with a fiber-locator export, which can be "
            "appended to this report."
        )
    return out


def _gather_latency(lat, lon, target_arg):
    out = {}
    t = (target_arg or "").strip().lower()

    # Default (no explicit target): measure to the NEAREST real carrier hotel
    # (interconnection building), not a metro centroid, at ~MILES_PER_MS mi/ms.
    if not t:
        hotel = _nearest_carrier_hotel(lat, lon)
        if hotel:
            gc_mi = _haversine_mi(lat, lon, hotel["lat"], hotel["lng"])
            rtt_ms = gc_mi / MILES_PER_MS
            loc = ", ".join(x for x in (hotel["city"], hotel["state"]) if x)
            out["latency_target"] = hotel["name"] + (f" — {loc}" if loc else "")
            out["latency_target_name"] = hotel["name"]
            out["latency_target_lat"] = hotel["lat"]
            out["latency_target_lng"] = hotel["lng"]
            out["latency_carriers"] = hotel["carriers"]
            out["latency_ms"] = (f"{rtt_ms:.2f}" if rtt_ms < 1 else f"{rtt_ms:.1f}")
            out["latency_distance"] = f"{gc_mi:.1f} mi to carrier hotel · {hotel['carriers']} carriers"
            out["latency_transport"] = "Fiber · to nearest carrier hotel"
            out["latency_note"] = (
                f"Latency to the nearest major carrier hotel — {hotel['name']}"
                + (f" ({loc})" if loc else "")
                + f" hosting {hotel['carriers']} networks — at ~{MILES_PER_MS:.0f} miles per ms "
                f"({gc_mi:.1f} mi to the building). A measured fiber-locator route supersedes this estimate."
            )
            out["_ms"] = rtt_ms
            out["_score"] = (95 if rtt_ms < 1 else 88 if rtt_ms < 3 else 75 if rtt_ms < 6
                             else 60 if rtt_ms < 12 else 40 if rtt_ms < 25 else 25)
            return out

    # Explicit target, or no carrier hotel within range → metro-hub fallback.
    note_default = ""
    if not t:
        tlat, tlon, label = min(LATENCY_TARGETS.values(),
                                key=lambda v: _haversine_mi(lat, lon, v[0], v[1]))
    elif t in LATENCY_TARGETS:
        tlat, tlon, label = LATENCY_TARGETS[t]
    elif "," in t:
        try:
            a, b = t.split(",", 1)
            tlat, tlon = float(a), float(b)
            label = f"{tlat:.3f}, {tlon:.3f}".replace("-", "−")
        except Exception:
            tlat, tlon, label = LATENCY_TARGETS[DEFAULT_TARGET]
            note_default = " (target not recognised — defaulted to Dallas)"
    else:
        tlat, tlon, label = LATENCY_TARGETS[DEFAULT_TARGET]
        note_default = f" (target “{target_arg}” not recognised — defaulted to Dallas)"

    gc_mi = _haversine_mi(lat, lon, tlat, tlon)
    route_mi = gc_mi * FIBER_ROUTE_FACTOR
    rtt_ms = route_mi / MILES_PER_MS
    out["latency_target"] = label
    out["latency_target_lat"] = tlat
    out["latency_target_lng"] = tlon
    out["latency_ms"] = f"{rtt_ms:.1f}"
    out["latency_distance"] = f"~{route_mi:.0f} mi route (gc {gc_mi:.0f} mi × {FIBER_ROUTE_FACTOR})"
    out["latency_transport"] = "Fiber · est."
    out["latency_note"] = (
        f"Estimated latency to {label} at ~{MILES_PER_MS:.0f} miles per ms over a great-circle × "
        f"{FIBER_ROUTE_FACTOR} route.{note_default} A measured fiber-locator route supersedes this estimate."
    )
    out["_ms"] = rtt_ms
    out["_score"] = (90 if rtt_ms < 10 else 75 if rtt_ms < 20 else 55 if rtt_ms < 40 else 35 if rtt_ms < 70 else 20)
    return out


def _gather_market(lat, lon, state):
    """DCPI market context for the site — verdict, time-to-power, queue — plus
    delivered-power cost and data-center tax incentives. One DB connection,
    fully failure-isolated (any miss degrades to an empty dict, never a 500).
    Reuses the same reads as the get_market_brief MCP tool (site_brief)."""
    out = {}
    try:
        from routes.site_valuation_engine import _nearest_market
        from routes.site_brief import (_conn, _resolve_market,
                                        _energy_for_state, _tax_for_state)
    except Exception:
        return out

    slug = m_state = None
    dist = None
    try:
        slug, m_state, dist = _nearest_market(lat, lon)
    except Exception:
        pass
    st = state or m_state

    conn = None
    try:
        conn = _conn()
        cur = conn.cursor()
        row = _resolve_market(cur, slug, st)
        eff_state = st  # state for the cost/tax lookups
        if row:
            (m_slug, m_name, m_st, m_iso, verdict, excess, constraint,
             ttp, queue_wait, queue_cap, reserve, gen_add, curtail,
             stranded, emergency, risks_json, opps_json, computed_at) = row
            vu = (verdict or "").upper()
            out["market"] = {
                "slug": m_slug, "name": m_name, "state": m_st, "iso": m_iso,
                "verdict": vu or None,
                "verdict_color": {"BUILD": "grn", "CAUTION": "amb",
                                  "AVOID": "red"}.get(vu, "ind"),
                "excess_power_score": excess, "constraint_score": constraint,
                "time_to_power_months": ttp, "queue_wait_months": queue_wait,
                "queue_capacity_mw": queue_cap, "reserve_margin_pct": reserve,
                "distance_mi": (round(float(dist)) if dist is not None else None),
            }
            # The resolved DCPI market row's state is authoritative — prefer it
            # over the coarse _state_for(), which can mis-bucket near-border
            # coords (e.g. Ashburn VA → MD), poisoning the cost/tax lookups.
            eff_state = m_st or st
        out["energy"] = _energy_for_state(cur, eff_state)
        out["tax"] = _tax_for_state(cur, eff_state)
    except Exception:
        pass
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
    return out


def _compute_site_verdict(power, water, air, market):
    """Composite BUILD / CAUTION / AVOID screening verdict.

    Hard physical site constraints (air, water, transmission distance) drive
    AVOID; softer factors plus the DCPI market signal drive CAUTION. The market
    verdict is surfaced as context — never as a sole AVOID gate, because a DCPI
    AVOID is two-sided (a saturated marquee cluster vs a no-demand town)."""
    avoid, caution = [], []
    risk = (air or {}).get("risk")
    ws = (water or {}).get("_score")
    pd = (power or {}).get("_dist")

    if risk == "High":
        avoid.append("High air-permitting risk (major-source / nonattainment pathway)")
    elif risk in ("Moderate", "Elevated"):
        caution.append(f"{risk} air-permitting pathway")

    if ws is not None and ws < 30:
        avoid.append("severe water constraint")
    elif ws is not None and ws < 50:
        caution.append("limited water availability — favour closed-loop cooling")

    if pd is None:
        avoid.append("no mapped transmission within 50 mi")
    elif pd > 25:
        avoid.append(f"nearest transmission {_fmt_mi(pd)} out (>25 mi)")
    elif pd > 10:
        caution.append(f"transmission {_fmt_mi(pd)} from the site")

    mk = market or {}
    mv = (mk.get("verdict") or "").upper()
    ttp = mk.get("time_to_power_months")
    nm = mk.get("name") or "the local market"
    if mv == "CAUTION":
        caution.append(f"DCPI market verdict CAUTION for {nm}")
    elif mv == "AVOID":
        caution.append(f"DCPI market verdict AVOID for {nm} "
                       "(confirm whether saturation or weak demand)")
    try:
        if ttp is not None and float(ttp) >= 48:
            caution.append(f"long interconnection queue (~{int(float(ttp))} mo time-to-power in-market)")
    except (TypeError, ValueError):
        pass

    if avoid:
        label, color = "AVOID", "red"
        reasons = avoid + caution
        head = "Material constraints at the screening level — resolve before committing capital."
    elif caution:
        label, color = "CAUTION", "amb"
        reasons = caution
        head = "Buildable with diligence — the items below are the critical path."
    else:
        label, color = "BUILD", "grn"
        reasons = ["No hard power, water, air, or market constraints surfaced at the screening level."]
        head = "No screening-level blockers — proceed to detailed diligence."
    return {"label": label, "color": color, "headline": head, "reasons": reasons[:5]}


# ════════════════════════════════════════════════════════════════════════════
#  Survey assembly
# ════════════════════════════════════════════════════════════════════════════
def _build_survey_data(lat, lon, latency_target, capacity_mw):
    state = _reverse_geocode_state(lat, lon) or _state_for(lat, lon)
    # Run the section gatherers concurrently — water (USDM) and power (HIFLD
    # fallback) are the long poles; overlapping them keeps the uncached build to
    # roughly the single slowest section instead of their sum. Each gatherer is
    # already failure-isolated; a future timeout just yields an empty section.
    # NOT `with ...`: ThreadPoolExecutor.__exit__ joins every worker, so the
    # per-section _grab(timeout=18) below could not bound the request either —
    # the same defect as _call_with_timeout above (r-realcap 2026-08-25). Built
    # by hand and shut down without waiting, one wedged section now costs the
    # report that section rather than its wall clock.
    ex = _cf.ThreadPoolExecutor(max_workers=7)
    try:
        _f = {
            "power": ex.submit(_gather_power, lat, lon, state),
            "gas": ex.submit(_gather_gas, lat, lon, state),
            "water": ex.submit(_gather_water, lat, lon, state),
            "air": ex.submit(_gather_air, lat, lon, capacity_mw),
            "fiber": ex.submit(_gather_fiber, lat, lon),
            "latency": ex.submit(_gather_latency, lat, lon, latency_target),
            "market": ex.submit(_gather_market, lat, lon, state),
        }

        def _grab(k):
            try:
                return _f[k].result(timeout=18) or {}
            except Exception:
                return {}
        power = _grab("power")
        gas = _grab("gas")
        water = _grab("water")
        air = _grab("air")
        fiber = _grab("fiber")
        latency = _grab("latency")
        mkt = _grab("market")
        market = mkt.get("market") or {}
        energy = mkt.get("energy") or {}
        tax = mkt.get("tax") or {}
    finally:
        ex.shutdown(wait=False, cancel_futures=True)

    # (The survey `state` above is now reverse-geocoded — accurate at borders. The
    # old DCPI-market-state override was a bbox-bug patch and is removed: with
    # accurate geocoding it would mis-fire on cross-border metros, e.g. a Jersey
    # City NJ site whose nearest market "New York" is in NY.)

    # Align the air section's state + regulatory context with the survey's resolved
    # state. The air scorer's _ap_resolve_state (main._ap_score_site) has its OWN
    # near-border bbox bug (smallest-box-wins → Carrier Mills IL→Kentucky, Ashburn
    # VA→Maryland), so without this the report shows e.g. "Illinois" location but
    # "Kentucky context".
    if state in _FIPS and (air or {}).get("risk") not in (None, "—") and air.get("state") != state:
        try:
            from air_permitting_extras import STATE_CONTEXT as _AP_CTX
            _ac = (_AP_CTX or {}).get(state) or {}
        except Exception:
            _ac = {}
        air["state"] = state
        air["context_label"] = f"{_STATE_NAME.get(state, state)} context"
        if _ac.get("description"):
            air["context"] = _ac["description"]

    # Auto satellite site map for the Power page — every report gets a visual of
    # the parcel (a POST upload from the submission portal overrides this).
    power["site_map"] = _static_site_map(lat, lon)
    power["site_map_caption"] = ("DC Hub satellite view · " + _coords_str(lat, lon)
                                 + " — pin marks the candidate site (imagery: Esri).")
    power["_site_map_pin"] = True

    iso = power.get("iso", "—")
    loc_bits = [b for b in [_STATE_NAME.get(state, state), (iso if iso and iso != "—" else None)] if b]
    location = " · ".join(loc_bits) if loc_bits else "United States"

    # ── Cover scorecards (6) — summaries of the real section data ──
    def card(label, value, unit, sub, color):
        return {"label": label, "value": value, "unit": unit, "sub": sub, "color": color}

    sc = []
    # Water
    sc.append(card("Water Availability",
                   water.get("score", "—"), "/100" if water.get("score") != "—" else "",
                   water.get("label", "—").replace(" Water Availability", ""),
                   _score_color(water.get("_score"))))
    # Power / Transmission
    pwr_val = power.get("voltage", "—")
    pwr_sub = (f"{power.get('operator')}" if power.get("operator", "—") != "—" else "grid")
    if power.get("_dist") is not None:
        pwr_sub += f" · {_fmt_mi(power['_dist'])}"
    sc.append(card("Power / Transmission", pwr_val if pwr_val != "—" else "—", "",
                   pwr_sub, _score_color(power.get("_score"))))
    # Gas
    if gas.get("_dist") is not None:
        gas_val = _fmt_mi(gas["_dist"]) or "—"
        gas_sub = gas.get("name", "—")
    else:
        gas_val, gas_sub = "—", "no pipeline <25 mi"
    sc.append(card("Gas Pipeline", gas_val, "", gas_sub, _score_color(gas.get("_score"))))
    # Air
    air_color = {"High": "red", "Moderate": "amb", "Elevated": "amb", "Low": "grn"}.get(air.get("risk"), "amb")
    sc.append(card("Air Permitting", air.get("risk", "—"), " risk" if air.get("risk") != "—" else "",
                   air.get("pathway", "—"), air_color))
    # Fiber
    fib_n = fiber.get("_count", 0)
    sc.append(card("Fiber", (f"{fib_n} carrier" + ("s" if fib_n != 1 else "")) if fib_n else "—", "",
                   (fiber.get("adjacent") if fiber.get("adjacent", "—") != "—" else "see fiber-locator"),
                   _score_color(fiber.get("_score"))))
    # Latency
    _lms = latency.get("_ms")
    lat_color = ("grn" if _lms < 20 else "amb" if _lms < 50 else "red") if _lms is not None else "amb"
    short_target = (latency.get("latency_target") or "core").split(" (")[0].split(",")[0]
    sc.append(card(f"Latency → {short_target}", latency.get("latency_ms", "—"), " ms",
                   "fiber (est.)", lat_color))

    # ── Summary grid ──
    def gcell(label, value, note, color):
        return {"label": label, "value": value, "note": note, "color": color}

    summary_grid = [
        gcell("Water", f"{water.get('score','—')}/100" if water.get("score") != "—" else "—",
              water.get("label", "—").replace(" Water Availability", "") or "—",
              _score_color(water.get("_score"))),
        gcell("Gas", (_fmt_mi(gas["_dist"]) or "—") if gas.get("_dist") is not None else "—",
              gas.get("name", "—") if gas.get("_dist") is not None else "no pipeline <25 mi",
              _score_color(gas.get("_score"))),
        gcell("Fiber", (f"{fib_n} carriers" if fib_n else "—"),
              fiber.get("adjacent", "—") if fib_n else "see fiber-locator", "cy"),
        gcell("Latency", f"{latency.get('latency_ms','—')} ms", f"→ {short_target}", "vio"),
        gcell("Power", power.get("voltage", "—"),
              "on-site · headroom TBD" if power.get("_dist") is not None and power["_dist"] < 0.5
              else (f"{_fmt_mi(power['_dist'])} · headroom TBD" if power.get("_dist") is not None else "—"),
              _score_color(power.get("_score"))),
        gcell("Air", f"{air.get('risk','—')} risk" if air.get("risk") != "—" else "—",
              air.get("pathway", "—"), air_color),
    ]

    # ── DCPI market + cost + tax cells (keeps the summary grid a clean 3×3) ──
    ind = energy.get("industrial_cents_kwh") if energy else None
    summary_grid.append(gcell(
        "Power cost", (f"{ind}¢" if ind is not None else "—"),
        ("industrial /kWh" if ind is not None else "no EIA rate"), "amb"))
    if tax:
        tval = ("DC-specific" if tax.get("data_center_specific")
                else ("Sales-tax exempt" if tax.get("sales_tax_exempt") else "Limited"))
        summary_grid.append(gcell("Tax incentives", tval,
                                  tax.get("state_name") or "state program",
                                  "grn" if tax.get("data_center_specific") else "amb"))
    else:
        summary_grid.append(gcell("Tax incentives", "—", "no state record", "amb"))
    if market.get("verdict"):
        ttp_m = market.get("time_to_power_months")
        mnote = market.get("name") or "—"
        if ttp_m is not None:
            try:
                mnote = f"{mnote} · ~{int(float(ttp_m))} mo to power"
            except (TypeError, ValueError):
                pass
        summary_grid.append(gcell("DCPI market", market["verdict"], mnote,
                                  market.get("verdict_color", "ind")))
    else:
        summary_grid.append(gcell("DCPI market", "—", "no market within range", "ind"))

    # ── Composite screening verdict (the headline a site selector reads first) ──
    verdict = _compute_site_verdict(power, water, air, market)

    # Bottom line — data-driven, honest about gating items.
    gates = []
    gates.append("grid headroom (load study with the interconnecting utility — DC Hub does not hold "
                 "published transfer capacity)")
    if air.get("risk") in ("High", "Moderate"):
        gates.append(f"air permitting ({air.get('pathway','')}".rstrip() + ")")
    if water.get("_score") is not None and water["_score"] < 50:
        gates.append("water sourcing (limited availability — favour closed-loop cooling)")
    bottom = (
        f"This screening consolidates DC Hub's live grid, pipeline, hydrology, air-quality and fiber "
        f"layers for {location}. The primary diligence "
        f"item{'s are' if len(gates) > 1 else ' is'} " + "; and ".join(gates) + ". "
        "Every figure above is sourced and refreshable; estimates are point-in-time and should be "
        "confirmed with the utility, regulator, and water provider before commitment."
    )

    return {
        "site_name": f"{_STATE_NAME.get(state, 'Candidate')} Site" if state else "Candidate Site",
        "coords": _coords_str(lat, lon),
        "location": location,
        "date": datetime.date.today().isoformat(),
        "scorecards": sc,
        "power": power, "gas": gas, "water": water, "air": air,
        "fiber": {**fiber, **latency},
        "verdict": verdict, "market": market, "energy": energy, "tax": tax,
        "summary": {"grid": summary_grid, "bottom_line": bottom},
        "about": _ABOUT,
        "_meta": {"capacity_mw": capacity_mw, "state": state, "iso": iso},
    }


_ABOUT = {
    "headline": "The intelligence layer for data-center & energy infrastructure.",
    "intro": ("DC Hub turns fragmented public and proprietary infrastructure data into a single, "
              "decision-ready intelligence layer — purpose-built for site selection, diligence, and "
              "capital allocation in the AI-infrastructure era. Every figure in this report is "
              "sourced, attributed, and refreshable."),
    "stats": [
        {"v": "19,000+", "l": "facilities tracked"},
        {"v": "233", "l": "markets"},
        {"v": "170+", "l": "countries"},
        {"v": "4,000+", "l": "M&A deals"},
    ],
    "capabilities": [
        ["DCPI™ — Data Center Power Index",
         "BUILD / CAUTION / AVOID verdicts on grid headroom and time-to-power across every U.S. market."],
        ["DCGI™ — Data Center Gas Index",
         "Pipeline capacity and delivered-gas economics by state and basin."],
        ["Live grid intelligence",
         "ISO interconnection queues, energy prices, and substation + transmission overlays."],
        ["Multi-domain site scoring",
         "Power, gas, water & drought, air permitting, fiber, and latency assessed in a single pass."],
        ["Agent-native",
         "An MCP server exposes the entire dataset to AI agents directly, with citations."],
    ],
    "partner": ("This report is generated by DC Hub's automated Site Intelligence engine — the same "
                "live data behind dchub.cloud. Pair it with hands-on diligence to de-risk and "
                "accelerate development, from screening to interconnection strategy."),
    "url": "dchub.cloud",
}


def _get_or_build_survey(lat, lon, use_case, latency_target, prepared_for, prepared_by, capacity_mw):
    key = f"{round(lat, 4)},{round(lon, 4)}|{(latency_target or '').lower()}|{capacity_mw}"
    now = time.time()
    hit = _CACHE.get(key)
    if hit and (now - hit[0]) < _CACHE_TTL:
        data = hit[1]
    else:
        data = _build_survey_data(lat, lon, latency_target, capacity_mw)
        _CACHE[key] = (now, data)
        if len(_CACHE) > 200:
            for k in list(_CACHE.keys()):
                if now - _CACHE[k][0] > _CACHE_TTL:
                    _CACHE.pop(k, None)
    # Overlay per-request presentation fields (don't affect cached data).
    data = dict(data)
    data["use_case"] = (use_case or
                        "Data-center / AI-infrastructure site evaluation (power, gas, water, air, fiber, latency).")
    data["prepared_by"] = prepared_by or "DC Hub"
    data["prepared_for"] = prepared_for or ""
    return data


# ════════════════════════════════════════════════════════════════════════════
#  Server-side renderer (no JS — ports the template's render IIFE to Python)
# ════════════════════════════════════════════════════════════════════════════
def _esc(s):
    if s is None:
        return ""
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def _kv(k, v_html):
    if v_html in (None, ""):
        return ""
    return (f'<div class="kv"><span class="k">{_esc(k)}</span>'
            f'<span class="vv">{v_html}</span></div>')


def _verdict_card(v):
    """Render the composite BUILD / CAUTION / AVOID screening verdict hero."""
    if not v or not v.get("label"):
        return ""
    reasons = "".join(f'<li>{_esc(r)}</li>' for r in v.get("reasons", []))
    return (
        f'<div class="verdict {v.get("color","amb")}">'
        f'<div class="vlabel">{_esc(v["label"])}</div>'
        '<div class="vbody"><p class="vk">Screening verdict · DC Hub composite</p>'
        f'<div class="vhead">{_esc(v.get("headline",""))}</div>'
        f'<ul class="vreasons">{reasons}</ul></div></div>'
    )


def _mapfig(src, cap, h="3.4in", pin=False):
    """Render a map figure (data: URI or URL). Empty src → ''. When pin=True,
    overlays a centered marker (used for the auto satellite site map, which is
    centered on the coordinates)."""
    if not src:
        return ""
    pin_html = ('<div style="position:absolute;top:50%;left:50%;'
                'transform:translate(-50%,-100%);font-size:30px;line-height:1;'
                'pointer-events:none;filter:drop-shadow(0 2px 3px rgba(0,0,0,.55))">📍</div>'
                if pin else "")
    return (f'<div class="mapfig" style="position:relative"><img src="{_esc(src)}" style="height:{h}"/>'
            + pin_html
            + (f'<div class="mapcap">{_esc(cap)}</div>' if cap else "")
            + "</div>")


def _static_site_map(lat, lon, dlat=0.0065, dlon=0.0095):
    """Free ArcGIS World Imagery static export centered on the site (no API key,
    CORS-ok). ~1.5 km across — shows the parcel + immediate surroundings."""
    return ("https://services.arcgisonline.com/arcgis/rest/services/World_Imagery/"
            "MapServer/export?bbox="
            f"{lon - dlon:.5f},{lat - dlat:.5f},{lon + dlon:.5f},{lat + dlat:.5f}"
            "&bboxSR=4326&imageSR=4326&size=920,560&format=png&f=image")


_CSS = """
  :root{--bg:#0a0a0f;--surf:#14141b;--surf2:#1b1b24;--b:rgba(255,255,255,.10);--b2:rgba(255,255,255,.16);
    --tx:#fafafa;--mut:#a1a1aa;--dim:#71717a;--ind:#818cf8;--vio:#a855f7;--cy:#22d3ee;--grn:#34d399;--amb:#fbbf24;--red:#f87171;}
  *{box-sizing:border-box;-webkit-print-color-adjust:exact;print-color-adjust:exact;}
  html,body{margin:0;padding:0;background:var(--bg);color:#d4d4d8;font-family:'Instrument Sans',-apple-system,system-ui,sans-serif;line-height:1.55;-webkit-font-smoothing:antialiased;}
  .page{width:8.5in;min-height:11in;margin:0 auto;padding:.7in .75in;position:relative;page-break-after:always;overflow:hidden;}
  .page:last-child{page-break-after:auto;}
  @page{size:Letter;margin:0;}
  .kick{font-family:'JetBrains Mono',monospace;font-size:10px;letter-spacing:.22em;text-transform:uppercase;color:var(--cy);margin:0 0 10px;}
  .secnum{color:var(--ind);font-weight:600;}
  h1{font-size:40px;font-weight:800;letter-spacing:-.025em;color:#fff;margin:0 0 6px;line-height:1.02;}
  h2{font-size:19px;font-weight:700;color:#fff;letter-spacing:-.01em;margin:0;display:flex;align-items:center;gap:10px;}
  .sec-rule{height:1px;background:linear-gradient(90deg,var(--b2),transparent);margin:10px 0 16px;}
  .cover{display:flex;flex-direction:column;min-height:9.6in;}
  .brandrow{display:flex;align-items:center;justify-content:space-between;}
  .wordmark{font-weight:800;font-size:20px;color:#fff;letter-spacing:-.02em;}.wordmark span{color:var(--ind);}
  .badge{font-family:'JetBrains Mono',monospace;font-size:10px;color:var(--mut);border:1px solid var(--b);border-radius:999px;padding:5px 11px;}
  .cover-glow{position:absolute;top:-2in;left:-1.5in;width:9in;height:6in;background:radial-gradient(closest-side,rgba(129,140,248,.22),transparent 70%);pointer-events:none;}
  .cover-mid{margin-top:1.5in;}
  .coords{font-family:'JetBrains Mono',monospace;color:var(--cy);font-size:14px;margin:0 0 18px;}
  .lead{color:var(--mut);font-size:15px;max-width:5.6in;margin:14px 0 0;}
  .usecase{margin-top:22px;background:linear-gradient(135deg,rgba(129,140,248,.12),rgba(168,85,247,.10));border:1px solid var(--b);border-left:3px solid var(--vio);border-radius:12px;padding:14px 18px;}
  .usecase .l{font-family:'JetBrains Mono',monospace;font-size:10px;letter-spacing:.14em;text-transform:uppercase;color:var(--vio);margin:0 0 4px;}
  .usecase .v{color:#e9e9ee;font-size:14px;font-weight:500;}
  .scorecards{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin-top:auto;padding-top:28px;}
  .sc{background:linear-gradient(160deg,var(--surf),var(--surf2));border:1px solid var(--b);border-radius:14px;padding:15px 16px;}
  .sc .t{font-family:'JetBrains Mono',monospace;font-size:9.5px;letter-spacing:.1em;text-transform:uppercase;color:var(--dim);margin:0 0 8px;}
  .sc .v{font-size:23px;font-weight:800;color:#fff;line-height:1;letter-spacing:-.02em;}.sc .v small{font-size:13px;color:var(--mut);font-weight:600;}
  .sc .s{font-size:11.5px;margin-top:6px;font-weight:600;}
  .verdict{display:flex;align-items:stretch;gap:0;margin-top:24px;border:1px solid var(--b);border-radius:14px;overflow:hidden;background:linear-gradient(160deg,var(--surf),var(--surf2));}
  .verdict.grn{border-left:4px solid var(--grn);}.verdict.amb{border-left:4px solid var(--amb);}.verdict.red{border-left:4px solid var(--red);}
  .verdict .vlabel{font-family:'JetBrains Mono',monospace;font-weight:800;font-size:25px;letter-spacing:.04em;padding:16px 22px;display:flex;align-items:center;min-width:1.9in;border-right:1px solid var(--b);}
  .verdict.grn .vlabel{color:var(--grn);background:rgba(52,211,153,.07);}
  .verdict.amb .vlabel{color:var(--amb);background:rgba(251,191,36,.07);}
  .verdict.red .vlabel{color:var(--red);background:rgba(248,113,113,.07);}
  .verdict .vbody{padding:13px 18px;flex:1;}
  .verdict .vk{font-family:'JetBrains Mono',monospace;font-size:9px;letter-spacing:.14em;text-transform:uppercase;color:var(--dim);margin:0 0 3px;}
  .verdict .vhead{color:#fff;font-weight:700;font-size:13.5px;margin-bottom:6px;line-height:1.35;}
  .verdict .vreasons{margin:0;padding-left:16px;color:var(--mut);font-size:11.5px;line-height:1.5;}
  .cover-foot{display:flex;justify-content:space-between;align-items:flex-end;margin-top:26px;padding-top:14px;border-top:1px solid var(--b);font-family:'JetBrains Mono',monospace;font-size:10px;color:var(--dim);}
  .grid2{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin:0 0 14px;}
  .grid3{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin:0 0 14px;}
  .card{background:var(--surf);border:1px solid var(--b);border-radius:13px;padding:16px 18px;}
  .card.grn{border-left:3px solid var(--grn);}.card.amb{border-left:3px solid var(--amb);}.card.red{border-left:3px solid var(--red);}
  .card.ind{border-left:3px solid var(--ind);}.card.cy{border-left:3px solid var(--cy);}.card.vio{border-left:3px solid var(--vio);}
  .lab{font-family:'JetBrains Mono',monospace;font-size:9.5px;letter-spacing:.1em;text-transform:uppercase;color:var(--dim);margin:0 0 6px;}
  .big{font-size:30px;font-weight:800;color:#fff;line-height:1;letter-spacing:-.02em;}.big small{font-size:14px;color:var(--mut);font-weight:600;}
  .kv{display:flex;justify-content:space-between;gap:14px;padding:7px 0;border-bottom:1px solid var(--b);font-size:13px;}.kv:last-child{border-bottom:0;}
  .kv .k{color:var(--mut);}.kv .vv{color:#fff;font-weight:600;font-family:'JetBrains Mono',monospace;text-align:right;}
  .pill{display:inline-block;font-family:'JetBrains Mono',monospace;font-size:10px;font-weight:700;letter-spacing:.04em;padding:3px 9px;border-radius:999px;border:1px solid;}
  .pill.grn{color:var(--grn);border-color:var(--grn);background:rgba(52,211,153,.08);}
  .pill.amb{color:var(--amb);border-color:var(--amb);background:rgba(251,191,36,.08);}
  .pill.red{color:var(--red);border-color:var(--red);background:rgba(248,113,113,.08);}
  .note{font-size:12.5px;color:var(--mut);margin:10px 0 0;line-height:1.5;}.note b{color:#fff;}
  .src{font-family:'JetBrains Mono',monospace;font-size:9.5px;color:var(--dim);margin-top:12px;}.src b{color:var(--mut);font-weight:600;}
  .tags{display:flex;flex-wrap:wrap;gap:6px;margin-top:8px;}
  .tag{font-family:'JetBrains Mono',monospace;font-size:10px;color:#cbd5e1;background:var(--surf2);border:1px solid var(--b);border-radius:6px;padding:3px 8px;}
  .foot{position:absolute;bottom:.45in;left:.75in;right:.75in;display:flex;justify-content:space-between;font-family:'JetBrains Mono',monospace;font-size:9px;color:var(--dim);border-top:1px solid var(--b);padding-top:8px;}
  .mapfig{margin:14px 0 6px;border:1px solid var(--b);border-radius:12px;overflow:hidden;background:#0a0a0f;}
  .mapfig img{width:100%;display:block;object-fit:contain;background:#0a0a0f;}
  .mapcap{font-family:'JetBrains Mono',monospace;font-size:9.5px;color:var(--mut);padding:8px 12px;border-top:1px solid var(--b);background:var(--surf);}
  .statstrip{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin:22px 0;}
  .stat{background:linear-gradient(160deg,var(--surf),var(--surf2));border:1px solid var(--b);border-radius:12px;padding:14px 16px;text-align:center;}
  .stat .sv{font-size:23px;font-weight:800;color:#fff;letter-spacing:-.02em;line-height:1;}
  .stat .sl{font-family:'JetBrains Mono',monospace;font-size:8.5px;letter-spacing:.07em;text-transform:uppercase;color:var(--dim);margin-top:6px;}
  .caps{display:flex;flex-direction:column;gap:9px;margin:4px 0 0;}
  .capitem{background:var(--surf);border:1px solid var(--b);border-left:3px solid var(--ind);border-radius:11px;padding:11px 16px;}
  .capt{color:#fff;font-weight:700;font-size:13px;}
  .capd{color:var(--mut);font-size:12px;margin-top:3px;line-height:1.45;}
"""


def _render_html(S):
    H = []

    def foot():
        return (f'<div class="foot"><span>DC Hub · Site Study</span>'
                f'<span>{_esc(S["location"])} · {_esc(S["coords"])}</span></div>')

    # ── COVER ──
    sc = "".join(
        f'<div class="sc"><div class="t">{_esc(c["label"])}</div>'
        f'<div class="v">{_esc(c["value"])}'
        + (f'<small>{_esc(c["unit"])}</small>' if c.get("unit") else "") + "</div>"
        + (f'<div class="s" style="color:var(--{c.get("color","mut")})">● {_esc(c["sub"])}</div>'
           if c.get("sub") else "") + "</div>"
        for c in S.get("scorecards", [])
    )
    prep = f'Prepared by {_esc(S["prepared_by"])}'
    if S.get("prepared_for"):
        prep += f' · for {_esc(S["prepared_for"])}'
    H.append(
        '<section class="page cover"><div class="cover-glow"></div>'
        '<div class="brandrow"><div class="wordmark">DC<span>·</span>Hub</div>'
        f'<div class="badge">SITE STUDY · {_esc(S["date"])}</div></div>'
        '<div class="cover-mid"><p class="kick">Power · Gas · Water · Air · Fiber · Latency</p>'
        f'<h1>{_esc(S["site_name"])}<br>Site Study</h1>'
        f'<p class="coords">◎ {_esc(S["coords"])} · {_esc(S["location"])}</p>'
        '<p class="lead">A consolidated power, gas, network, water, and permitting assessment for the '
        'candidate site — auto-generated from DC Hub\'s live grid, pipeline, hydrology, and air-quality '
        'datasets.</p>'
        + (f'<div class="usecase"><div class="l">Intended Use Case</div>'
           f'<div class="v">{_esc(S["use_case"])}</div></div>' if S.get("use_case") else "")
        + '</div>'
        + _verdict_card(S.get("verdict"))
        + f'<div class="scorecards">{sc}</div>'
        f'<div class="cover-foot"><span>{prep} · Powered by DC&nbsp;Hub</span>'
        '<span>dchub.cloud</span></div></section>'
    )

    # ── POWER ──
    p = S.get("power") or {}
    headroom_pill = (f'<span class="pill {p.get("headroom_color","amb")}">{_esc(p.get("headroom"))}</span>'
                     if p.get("headroom") else "")
    # DCPI market context (verdict + time-to-power + queue + power cost) folded
    # into the substation card — degrades silently to nothing when unavailable.
    mk = S.get("market") or {}
    en = S.get("energy") or {}
    _vpill = (f'<span class="pill {mk.get("verdict_color","amb")}">{_esc(mk.get("verdict"))}</span>'
              if mk.get("verdict") else None)

    def _mo(x):
        try:
            return f'~{int(float(x))} mo' if x is not None else None
        except (TypeError, ValueError):
            return None
    _mname = (f'{_esc(mk.get("name"))}'
              + (f' · {mk.get("distance_mi")} mi' if mk.get("distance_mi") is not None else '')
              ) if mk.get("name") else None
    market_kvs = (
        _kv("Nearest DCPI market", _mname)
        + _kv("In-market verdict", _vpill)
        + _kv("Time to power", _mo(mk.get("time_to_power_months")))
        + _kv("Queue wait", _mo(mk.get("queue_wait_months")))
        + _kv("Power cost (industrial)",
              (f'{en.get("industrial_cents_kwh")}¢/kWh'
               if en.get("industrial_cents_kwh") is not None else None))
    )
    H.append(
        '<section class="page"><p class="kick"><span class="secnum">01</span> &nbsp;Power &amp; Grid</p>'
        '<h2>⚡ Transmission &amp; Substation</h2><div class="sec-rule"></div>'
        + _mapfig(p.get("site_map"), p.get("site_map_caption"), "3.0in", pin=p.get("_site_map_pin"))
        + '<div class="grid2">'
        '<div class="card ind"><p class="lab">Nearest Substation</p>'
        f'<div class="big">{_esc(p.get("substation","—"))}</div>'
        + (f'<p class="note">{_esc(p.get("substation_note"))}</p>' if p.get("substation_note") else "")
        + _kv("Voltage", _esc(p.get("voltage", "—")))
        + _kv("Operator", _esc(p.get("operator", "—")))
        + _kv("ISO / RTO", _esc(p.get("iso", "—")))
        + market_kvs
        + (f'<div class="src"><b>Source:</b> {_esc(p.get("substation_source"))}'
           + (' · DCPI market layer' if mk.get("verdict") else '') + '</div>'
           if p.get("substation_source") else "")
        + '</div>'
        '<div class="card amb"><p class="lab">Transmission Line</p>'
        f'<div class="big">{_esc(p.get("line_voltage","—"))}</div>'
        + _kv("Est. transfer capacity", _esc(p.get("transfer", "—")))
        + _kv("Owner", _esc(p.get("line_owner", "—")))
        + _kv("Headroom status", headroom_pill)
        + (f'<div class="src"><b>Source:</b> {_esc(p.get("line_source"))}</div>' if p.get("line_source") else "")
        + '</div></div>'
        + (f'<p class="note"><b>Assessment.</b> {_esc(p.get("assessment"))}</p>' if p.get("assessment") else "")
        + foot() + '</section>'
    )

    # ── GAS ──
    g = S.get("gas") or {}
    gas_status_pill = (f'<span class="pill {g.get("status_color","grn")}">{_esc(g.get("status"))}</span>'
                       if g.get("status") and g.get("status") != "—" else "")
    H.append(
        '<section class="page"><p class="kick"><span class="secnum">02</span> &nbsp;Natural Gas</p>'
        '<h2>🔥 Pipeline Access</h2><div class="sec-rule"></div>'
        '<div class="card grn"><p class="lab">Nearest Pipeline</p>'
        f'<div class="big">{_esc(g.get("name","—"))}</div>'
        + _kv("Type", _esc(g.get("type", "—")))
        + _kv("Status", gas_status_pill)
        + _kv("Operator", _esc(g.get("operator", "—")))
        + _kv("Region", _esc(g.get("region", "—")))
        + (f'<div class="src"><b>Source:</b> {_esc(g.get("source"))}</div>' if g.get("source") else "")
        + '</div>'
        + (f'<p class="note"><b>Assessment.</b> {_esc(g.get("assessment"))}</p>' if g.get("assessment") else "")
        + foot() + '</section>'
    )

    # ── WATER ──
    w = S.get("water") or {}
    H.append(
        '<section class="page"><p class="kick"><span class="secnum">03</span> &nbsp;Water &amp; Hydrology</p>'
        '<h2>💧 Water &amp; Drought</h2><div class="sec-rule"></div><div class="grid2">'
        '<div class="card grn" style="text-align:center"><p class="lab">Water Availability Score</p>'
        f'<div class="big" style="font-size:54px;color:var(--grn)">{_esc(w.get("score","—"))}'
        + ('<small style="font-size:18px">/100</small>' if w.get("score") not in (None, "—") else "")
        + '</div>'
        + (f'<p class="note" style="color:var(--grn);font-weight:600">{_esc(w.get("label"))}</p>'
           if w.get("label") else "")
        + '</div>'
        '<div class="card grn"><p class="lab">Drought Status</p>'
        f'<div class="big">{_esc(w.get("drought","—"))}</div>'
        + (f'<p class="note">{_esc(w.get("drought_note"))}</p>' if w.get("drought_note") else "")
        + _kv("Assessed at", _esc(w.get("assessed_at", "—")))
        + (f'<div class="src"><b>Source:</b> {_esc(w.get("source"))}</div>' if w.get("source") else "")
        + '</div></div>'
        + (f'<p class="note"><b>Assessment.</b> {_esc(w.get("assessment"))}</p>' if w.get("assessment") else "")
        + foot() + '</section>'
    )

    # ── AIR ──
    a = S.get("air") or {}
    rc = "red" if (a.get("risk") and "high" in a["risk"].lower()) else (
        "amb" if (a.get("risk") and a["risk"].lower() in ("moderate", "elevated")) else "grn")
    tags = "".join(f'<span class="tag">{_esc(x)}</span>' for x in (a.get("pollutants") or []))
    H.append(
        '<section class="page"><p class="kick"><span class="secnum">04</span> &nbsp;Air Permitting</p>'
        '<h2>🌫️ Air Quality &amp; Permitting</h2><div class="sec-rule"></div><div class="grid2">'
        f'<div class="card {rc}"><p class="lab">Permitting Risk</p>'
        f'<div class="big" style="color:var(--{rc})">{_esc(a.get("risk","—"))}</div>'
        + (f'<p class="note">{_esc(a.get("risk_note"))}</p>' if a.get("risk_note") else "")
        + _kv("Pathway", _esc(a.get("pathway", "—")))
        + _kv("Assessed at", _esc(a.get("assessed_at", "—")))
        + '</div>'
        '<div class="card amb"><p class="lab">Criteria Pollutants Tracked</p>'
        f'<div class="tags">{tags}</div>'
        + (f'<p class="lab" style="margin-top:14px">Offset Estimate</p><p class="note">{_esc(a.get("offset"))}</p>'
           if a.get("offset") and a.get("offset") != "—" else "")
        + '</div></div>'
        + (f'<p class="note"><b>{_esc(a.get("context_label","Context"))}.</b> {_esc(a.get("context"))}</p>'
           if a.get("context") else "")
        + (f'<p class="src"><b>Sources:</b> {_esc(a.get("sources"))}</p>' if a.get("sources") else "")
        + foot() + '</section>'
    )

    # ── FIBER & LATENCY ──
    f = S.get("fiber") or {}
    H.append(
        '<section class="page"><p class="kick"><span class="secnum">05</span> &nbsp;Connectivity</p>'
        '<h2>🛜 Fiber &amp; Latency</h2><div class="sec-rule"></div><div class="grid2">'
        '<div class="card cy"><p class="lab">Fiber Carriers</p>'
        + _kv("Adjacent to site", _esc(f.get("adjacent", "—")))
        + _kv(_esc(f.get("nearby_dist", "Nearby")), _esc(f.get("nearby", "—")))
        + (f'<div class="src"><b>Source:</b> {_esc(f.get("source"))}</div>' if f.get("source") else "")
        + '</div>'
        f'<div class="card vio"><p class="lab">Latency → {_esc(f.get("latency_target","core"))}</p>'
        f'<div class="big" style="color:var(--vio)">{_esc(f.get("latency_ms","—"))} <small>ms</small></div>'
        + _kv("Distance", _esc(f.get("latency_distance", "—")))
        + _kv("Transport", _esc(f.get("latency_transport", "—")))
        + (f'<p class="note">{_esc(f.get("latency_note"))}</p>' if f.get("latency_note") else "")
        + '</div></div>'
        + (f'<p class="note"><b>Assessment.</b> {_esc(f.get("assessment"))}</p>' if f.get("assessment") else "")
        + foot() + '</section>'
    )

    # ── CONNECTIVITY MAPS (uploaded fiber-locator exports — submission portal) ──
    if f.get("fiber_map") or f.get("latency_map"):
        H.append(
            '<section class="page"><p class="kick"><span class="secnum">05</span> &nbsp;Connectivity · Maps</p>'
            '<h2>🗺️ Fiber &amp; Latency Maps</h2><div class="sec-rule"></div>'
            + _mapfig(f.get("fiber_map"), f.get("fiber_map_caption"), "3.55in")
            + _mapfig(f.get("latency_map"), f.get("latency_map_caption"), "3.55in")
            + foot() + '</section>'
        )

    # ── SUMMARY ──
    sm = S.get("summary") or {}
    grid = "".join(
        f'<div class="card {c.get("color","ind")}"><p class="lab">{_esc(c["label"])}</p>'
        f'<div class="big" style="font-size:22px">{_esc(c["value"])}</div>'
        f'<p class="note">{_esc(c.get("note",""))}</p></div>'
        for c in (sm.get("grid") or [])
    )
    H.append(
        '<section class="page"><p class="kick"><span class="secnum">06</span> &nbsp;Summary</p>'
        '<h2>◎ Site Readiness Summary</h2><div class="sec-rule"></div>'
        + _verdict_card(S.get("verdict"))
        + f'<div class="grid3">{grid}</div>'
        + (f'<div class="card" style="margin-top:6px"><p class="lab">Bottom line</p>'
           f'<p class="note" style="font-size:13.5px;color:#d4d4d8">{_esc(sm.get("bottom_line"))}</p></div>'
           if sm.get("bottom_line") else "")
        + f'<p class="note" style="font-size:11px;color:var(--dim)">This report consolidates live data as '
          f'of {_esc(S["date"])}. Figures (grid headroom, water, air, latency) are point-in-time estimates '
          f'from public datasets and should be confirmed with the utility, regulator, and water provider '
          f'before commitment.</p>'
        f'<div class="cover-foot" style="margin-top:24px"><span>{_esc(prep)} · Powered by DC&nbsp;Hub · '
        f'dchub.cloud</span><span>{_esc(S["date"])}</span></div>' + foot() + '</section>'
    )

    # ── ABOUT ──
    ab = S.get("about") or {}
    stats = "".join(f'<div class="stat"><div class="sv">{_esc(s["v"])}</div>'
                    f'<div class="sl">{_esc(s["l"])}</div></div>' for s in ab.get("stats", []))
    caps = "".join(f'<div class="capitem"><div class="capt">{_esc(c[0])}</div>'
                   f'<div class="capd">{_esc(c[1])}</div></div>' for c in ab.get("capabilities", []))
    H.append(
        '<section class="page"><p class="kick">About</p>'
        '<div class="wordmark" style="font-size:26px;margin:2px 0 16px">DC<span>·</span>Hub</div>'
        f'<h1 style="font-size:29px;line-height:1.15;max-width:7in">{_esc(ab.get("headline"))}</h1>'
        f'<p class="lead" style="max-width:6.4in">{_esc(ab.get("intro"))}</p>'
        f'<div class="statstrip">{stats}</div>'
        f'<div class="caps">{caps}</div>'
        + (f'<div class="usecase" style="border-left-color:var(--cy);margin-top:16px">'
           f'<div class="l" style="color:var(--cy)">About this report</div>'
           f'<div class="v">{_esc(ab.get("partner"))}</div></div>' if ab.get("partner") else "")
        + f'<div class="cover-foot" style="margin-top:30px"><span>{_esc(prep)} · Powered by DC&nbsp;Hub</span>'
          f'<span>{_esc(ab.get("url","dchub.cloud"))}</span></div>' + foot() + '</section>'
    )

    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f'<title>DC Hub · {_esc(S["site_name"])} · Site Study</title>'
        '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Instrument+Sans:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;500;600&display=swap">'
        f'<style>{_CSS}</style></head><body>'
        + "".join(H)
        + '</body></html>'
    )


# ════════════════════════════════════════════════════════════════════════════
#  Submission portal — upload the manual fiber-locator maps + generate
# ════════════════════════════════════════════════════════════════════════════
_IMG_MAGIC = {b"\x89PNG\r\n\x1a\n": "image/png", b"\xff\xd8\xff": "image/jpeg",
              b"GIF87a": "image/gif", b"GIF89a": "image/gif"}
_MAX_IMG = 8 * 1024 * 1024  # 8 MB per map


def _img_data_uri(file_storage):
    """Validate an uploaded image (PNG/JPEG/GIF, <=8 MB by magic bytes) and
    return a data: URI for inline embedding, else None. Never raises."""
    if not file_storage or not getattr(file_storage, "filename", ""):
        return None
    try:
        blob = file_storage.read(_MAX_IMG + 1)
        if not blob or len(blob) > _MAX_IMG:
            return None
        mime = next((m for magic, m in _IMG_MAGIC.items() if blob.startswith(magic)), None)
        if not mime:
            return None
        import base64
        return f"data:{mime};base64," + base64.b64encode(blob).decode("ascii")
    except Exception:
        return None


def _render_unlock_page():
    """Tiny client-side bounce: read the dchub_token JWT from localStorage (set
    by the logged-in app on dchub.cloud, same origin) and re-fetch the report
    with an Authorization: Bearer header — which the gate accepts even when the
    CF proxy didn't forward the cookie to /api/*. If not logged in, prompt login."""
    return """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>DC Hub · Site Study</title>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Instrument+Sans:wght@400;600;700&display=swap">
<style>
 html,body{margin:0;height:100%;background:#0a0a0f;color:#a1a1aa;font-family:'Instrument Sans',system-ui,sans-serif}
 .wrap{height:100%;display:flex;align-items:center;justify-content:center;text-align:center;padding:24px}
 .wm{font-weight:800;font-size:20px;color:#fff;margin-bottom:18px}.wm span{color:#818cf8}
 .sp{width:30px;height:30px;border:3px solid #1b1b24;border-top-color:#818cf8;border-radius:50%;animation:sp 1s linear infinite;margin:0 auto 16px}
 @keyframes sp{to{transform:rotate(360deg)}} a{color:#22d3ee;font-weight:600}
</style></head>
<body><div class="wrap"><div><div class="wm">DC<span>·</span>Hub</div>
<div class="sp" id="sp"></div><div id="m">Loading your DC Hub Site Study…</div></div></div>
<script>
(function(){
  var t=null; try{t=localStorage.getItem('dchub_token');}catch(e){}
  var m=document.getElementById('m'), sp=document.getElementById('sp');
  function fail(h){ if(sp) sp.style.display='none'; m.innerHTML=h; }
  if(!t){ fail('Please <a href="/login?next='+encodeURIComponent(location.pathname+location.search)+'">log in</a> to view this PRO report.'); return; }
  fetch(location.pathname+location.search,{headers:{'Authorization':'Bearer '+t},credentials:'include'})
   .then(function(r){ if(r.ok) return r.text(); throw r.status; })
   .then(function(html){ document.open(); document.write(html); document.close(); })
   .catch(function(code){ fail(code===402
      ? 'This report needs a PRO plan. <a href="/pricing">Upgrade \\u2192</a>'
      : 'Could not load (error '+code+'). <a href="/pricing">See plans</a> or re-log in.'); });
})();
</script></body></html>"""


def _render_portal(prefill):
    """Branded submission portal — paste coords, DC Hub auto-fills the report,
    optionally attach the (manual) fiber-locator maps, one click → report."""
    pf = prefill or {}
    lat = _esc(pf.get("lat", ""))
    lon = _esc(pf.get("lon", ""))
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>DC Hub · Site Intelligence Report — Generate</title>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Instrument+Sans:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;500;600&display=swap">
<style>
 :root{{--bg:#0a0a0f;--surf:#14141b;--surf2:#1b1b24;--b:rgba(255,255,255,.12);--tx:#fafafa;--mut:#a1a1aa;--dim:#71717a;--ind:#818cf8;--vio:#a855f7;--cy:#22d3ee;--grn:#34d399;}}
 *{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:#e4e4e7;font-family:'Instrument Sans',system-ui,sans-serif;line-height:1.55}}
 .wrap{{max-width:680px;margin:0 auto;padding:40px 22px 80px}}
 .brand{{font-weight:800;font-size:22px;color:#fff;letter-spacing:-.02em}}.brand span{{color:var(--ind)}}
 .kick{{font-family:'JetBrains Mono',monospace;font-size:10px;letter-spacing:.2em;text-transform:uppercase;color:var(--cy);margin:18px 0 6px}}
 h1{{font-size:30px;font-weight:800;color:#fff;letter-spacing:-.02em;margin:0 0 8px;line-height:1.1}}
 .lead{{color:var(--mut);font-size:14.5px;max-width:34em;margin:0 0 22px}}
 .card{{background:var(--surf);border:1px solid var(--b);border-radius:14px;padding:20px 22px;margin-bottom:16px}}
 label{{display:block;font-family:'JetBrains Mono',monospace;font-size:10px;letter-spacing:.08em;text-transform:uppercase;color:var(--dim);margin:0 0 6px}}
 .row{{display:grid;grid-template-columns:1fr 1fr;gap:14px}}
 input,select,textarea{{width:100%;padding:10px 12px;background:var(--bg);border:1px solid var(--b);border-radius:8px;color:#fff;font:inherit;font-size:14px;margin-bottom:14px}}
 input:focus,select:focus,textarea:focus{{outline:none;border-color:var(--ind);box-shadow:0 0 0 3px rgba(129,140,248,.18)}}
 input[type=file]{{padding:8px;font-size:12px;color:var(--mut)}}
 .hint{{font-size:11.5px;color:var(--dim);margin:-8px 0 14px}}
 .sec{{font-weight:700;color:#fff;font-size:14px;margin:6px 0 12px;display:flex;align-items:center;gap:8px}}
 .badge{{font-family:'JetBrains Mono',monospace;font-size:9px;color:var(--grn);border:1px solid var(--grn);border-radius:999px;padding:2px 8px;background:rgba(52,211,153,.08)}}
 button{{width:100%;padding:14px;background:linear-gradient(135deg,var(--ind),var(--vio));border:none;border-radius:10px;color:#fff;font-weight:700;font-size:15px;cursor:pointer;font-family:inherit}}
 button:disabled{{opacity:.6;cursor:wait}}
 #msg{{margin-top:14px;font-size:13px;min-height:20px}}
 a{{color:var(--cy)}}
 .auto{{font-size:12px;color:var(--mut);background:var(--surf2);border:1px solid var(--b);border-radius:10px;padding:12px 14px;margin-bottom:18px}}
 .auto b{{color:#fff}}
</style></head>
<body><div class="wrap">
 <div class="brand">DC<span>·</span>Hub</div>
 <p class="kick">Site Intelligence Report</p>
 <h1>Generate a branded site report</h1>
 <p class="lead">Paste coordinates and DC&nbsp;Hub auto-fills power, gas, water, air, fiber &amp; latency
   from its own live data. The only manual step is your fiber-locator map exports — attach them below
   (optional) and they're embedded into the report. <b>PRO feature.</b></p>

 <div class="auto">⚙️ <b>Auto-filled from DC Hub:</b> nearest substation &amp; transmission (HIFLD), gas
   pipeline, USDM water/drought, EPA air-permitting, fiber routes, and great-circle latency. You only
   provide the coordinates (and optionally the two fiber-locator maps).</div>

 <form id="f">
   <div class="card">
     <div class="sec">📍 Location <span class="badge">required</span></div>
     <div class="row">
       <div><label>Latitude</label><input name="lat" id="lat" value="{lat}" placeholder="26.4767" required></div>
       <div><label>Longitude</label><input name="lon" id="lon" value="{lon}" placeholder="-97.7535" required></div>
     </div>
     <label>Intended use case</label>
     <input name="use_case" placeholder="AI training campus — dedicated dark-fiber / OWS">
     <div class="row">
       <div><label>Latency target</label>
         <select name="latency_target">
           <option value="dallas">Dallas (Infomart)</option>
           <option value="ashburn">Ashburn, VA</option>
           <option value="chicago">Chicago (350 E Cermak)</option>
           <option value="silicon valley">Silicon Valley</option>
           <option value="phoenix">Phoenix</option>
           <option value="atlanta">Atlanta</option>
           <option value="new york">New York (60 Hudson)</option>
         </select></div>
       <div><label>Prepared for (client)</label><input name="prepared_for" placeholder="Acme Capital"></div>
     </div>
     <label>Assumed IT load (MW) — drives air-permitting</label>
     <input name="capacity_mw" value="100" inputmode="decimal">
   </div>

   <div class="card">
     <div class="sec">🗺️ Fiber-locator maps <span class="badge" style="color:var(--vio);border-color:var(--vio);background:rgba(168,85,247,.08)">optional</span></div>
     <p class="hint">Manual exports from your fiber-locator tool. PNG/JPG, ≤8&nbsp;MB each. Embedded on the
       report's Connectivity Maps page; the site map appears on the Power page.</p>
     <label>Site map (your DC Hub map screenshot)</label>
     <input type="file" name="site_map" accept="image/*">
     <label>Fiber carriers map</label>
     <input type="file" name="fiber_map" accept="image/*">
     <label>Latency / route map</label>
     <input type="file" name="latency_map" accept="image/*">
   </div>

   <button type="submit" id="go">Generate Site Report →</button>
   <div id="msg"></div>
 </form>

<script>
(function(){{
  var f=document.getElementById('f'), go=document.getElementById('go'), msg=document.getElementById('msg');
  f.addEventListener('submit', function(e){{
    e.preventDefault();
    var lat=parseFloat(document.getElementById('lat').value), lon=parseFloat(document.getElementById('lon').value);
    if(!isFinite(lat)||!isFinite(lon)){{ msg.style.color='#f87171'; msg.textContent='Enter valid coordinates.'; return; }}
    go.disabled=true; msg.style.color='#a1a1aa'; msg.textContent='Generating report from DC Hub data…';
    var fd=new FormData(f);
    var _t=null; try{{_t=localStorage.getItem('dchub_token');}}catch(e){{}}
    var _hdrs={{}}; if(_t) _hdrs['Authorization']='Bearer '+_t;
    fetch('/api/v1/site-report', {{ method:'POST', body:fd, credentials:'include', headers:_hdrs }})
      .then(function(r){{
        if(r.status===402){{ msg.innerHTML='<span style="color:#fbbf24">This is a PRO feature. <a href="/pricing">Upgrade to generate →</a></span>'; go.disabled=false; return null; }}
        if(!r.ok){{ return r.text().then(function(t){{ throw new Error('HTTP '+r.status+' '+t.slice(0,160)); }}); }}
        return r.text();
      }})
      .then(function(html){{
        if(html===null) return;
        var w=window.open('','_blank');
        if(w){{ w.document.open(); w.document.write(html); w.document.close(); msg.style.color='#34d399'; msg.textContent='✅ Report opened in a new tab.'; }}
        else {{ msg.style.color='#fbbf24'; msg.textContent='Pop-up blocked — allow pop-ups and retry.'; }}
        go.disabled=false;
      }})
      .catch(function(err){{ msg.style.color='#f87171'; msg.textContent='Error: '+err.message; go.disabled=false; }});
  }});
}})();
</script>
</div></body></html>"""


# ════════════════════════════════════════════════════════════════════════════
#  Route
# ════════════════════════════════════════════════════════════════════════════
@site_report_bp.route("/api/v1/site-report/portal", methods=["GET"])
@site_report_bp.route("/api/v1/site-study/portal", methods=["GET"])
def site_report_portal():
    """Human-facing submission portal (public form; generation is PRO-gated by
    the POST below). Pre-fills lat/lon from the query so the map's
    'Generate Report' button lands here ready to go."""
    prefill = {"lat": (request.args.get("lat") or "").strip(),
               "lon": (request.args.get("lon") or "").strip()}
    return Response(_render_portal(prefill), mimetype="text/html; charset=utf-8",
                    headers={"Cache-Control": "public, max-age=300"})


def _render_for(form, survey, lat, lon):
    """Pick the renderer. form=premium -> the branded 5-page Martone Advisors Site
    Analysis (routes.site_report_premium); anything else -> the standard report.
    Fail-safe: a premium-render error degrades to the standard report, never a 500."""
    if (form or "").strip().lower() == "premium":
        try:
            from routes.site_report_premium import render_premium_html
            return render_premium_html(survey, lat, lon)
        except Exception:
            pass  # fall through to the always-available standard renderer
    return _render_html(survey)


# ── Signed PDF links (r-sigurl 2026-06-25) ───────────────────────────────────
# The generate_site_analysis MCP tool mints a short-lived HMAC-signed link so the
# agent's HUMAN can download the premium PDF without a Pro session. The signature
# is scoped to one exact lat/lon premium-PDF report + expiry — it grants access to
# nothing else, and only PRO callers ever receive one (the tool is PRO-gated).
def _report_sig_secret():
    import os as _o
    return (_o.environ.get("JWT_SECRET") or _o.environ.get("DCHUB_ADMIN_KEY") or "").encode()


def _report_sig(lat, lon, exp):
    import hmac as _hm, hashlib as _hl, base64 as _b64
    secret = _report_sig_secret()
    if not secret:
        return None
    msg = f"{round(float(lat), 4)}|{round(float(lon), 4)}|premium|pdf|{int(exp)}".encode()
    mac = _hm.new(secret, msg, _hl.sha256).digest()
    return _b64.urlsafe_b64encode(mac).decode().rstrip("=")


def _verify_report_sig(lat, lon, exp, sig):
    import time as _t, hmac as _hm
    try:
        if not sig or int(exp) < int(_t.time()):
            return False
        expected = _report_sig(lat, lon, exp)
        return bool(expected) and _hm.compare_digest(expected, str(sig))
    except Exception:
        return False


def _signed_pdf_url(lat, lon, extra=None, ttl_days=7, base="https://dchub.cloud"):
    """Full signed premium-PDF URL. Sig covers lat/lon/exp (access control); extra
    params (capacity_mw, prepared_by, …) shape the report and ride along unsigned."""
    import time as _t
    exp = int(_t.time()) + int(ttl_days) * 86400
    sig = _report_sig(lat, lon, exp)
    if not sig:
        return None
    url = (f"{base}/api/v1/site-report?lat={float(lat):.4f}&lon={float(lon):.4f}"
           f"&form=premium&format=pdf&exp={exp}&sig={sig}")
    for k, v in (extra or {}).items():
        if v not in (None, ""):
            from urllib.parse import quote as _q
            url += f"&{k}={_q(str(v))}"
    return url


@site_report_bp.route("/api/v1/site-report", methods=["GET", "POST"])
@site_report_bp.route("/api/v1/site-study", methods=["GET", "POST"])
def site_report():
    # r-sarender (2026-06-24): admin bypass — lets the owner/agent verify the
    # (now Gotenberg-rendered) PDF path without a PRO session. The admin key is a
    # secret; admins always reach their own product. Used to confirm the weasyprint→
    # Gotenberg fix end-to-end against the Railway origin (gate + CF edge sidestepped).
    import os as _os_sr
    _sr_ak = (request.headers.get("X-Admin-Key") or "").strip()  # header-only: a secret in a URL would leak to logs/referers
    _sr_admin = bool(_sr_ak) and _sr_ak == (_os_sr.environ.get("DCHUB_ADMIN_KEY", "") or "").strip()
    # Signed-URL bypass: a valid HMAC sig (minted for a PRO generate_site_analysis
    # call) lets the human download THIS lat/lon premium PDF with no Pro session.
    _sr_signed = bool(request.args.get("sig") and request.args.get("exp")) and _verify_report_sig(
        request.args.get("lat"), request.args.get("lon"),
        request.args.get("exp"), request.args.get("sig"))
    # PRO gate (premium deliverable). Anon/FREE → 402 + upgrade JSON.
    try:
        from routes.tier_gate import _resolve_caller_tier, _gate_response
        tier, _ = _resolve_caller_tier()
        if not _sr_admin and not _sr_signed and (tier or "FREE").upper() not in ("PRO", "ENTERPRISE", "FOUNDING", "RESEARCH_SEED", "ADMIN"):
            # A logged-in user's dchub_token JWT lives in localStorage, but a
            # top-level browser navigation to /api/* only sends it as a COOKIE,
            # which the CF proxy may not forward → the gate sees FREE. For an
            # unauthenticated *browser* GET, serve a tiny client-side "unlock"
            # page that re-fetches with Authorization: Bearer <localStorage
            # .dchub_token> (the gate accepts Bearer). Loop-safe: only when no
            # Authorization / api_key was supplied (the re-fetch carries one).
            _has_auth = bool(request.headers.get("Authorization")
                             or request.headers.get("X-API-Key")
                             or request.args.get("api_key"))
            _wants_html = "text/html" in (request.headers.get("Accept") or "")
            if request.method == "GET" and _wants_html and not _has_auth:
                return Response(_render_unlock_page(), mimetype="text/html; charset=utf-8",
                                headers={"Cache-Control": "private, no-store",
                                         "X-Content-Type-Options": "nosniff"})
            return _gate_response(tier, "PRO", "site_report", {
                "what": "Auto-generated, branded DC Hub Site Study from a lat/lon",
                "verdict": "Composite BUILD / CAUTION / AVOID screening verdict",
                "sections": ["Power/Transmission + DCPI market verdict & time-to-power",
                             "Gas", "Water & Drought", "Air Permitting", "Fiber",
                             "Latency", "Power cost & tax incentives"],
                "upgrade_url": "/pricing",
            })
    except Exception:
        # If the gate itself errors, fail safe to PRO-denied rather than leaking.
        return jsonify({"error": "tier_check_failed", "upgrade_url": "/pricing"}), 402

    vals = request.values  # merges query args (GET) + form fields (POST multipart)
    try:
        lat = float(vals.get("lat"))
        lon = float(vals.get("lon"))
    except (TypeError, ValueError):
        return jsonify({
            "error": "lat and lon are required floats",
            "example": "/api/v1/site-report?lat=26.4767&lon=-97.7535&latency_target=dallas",
            "portal": "/api/v1/site-report/portal",
        }), 400
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return jsonify({"error": "lat/lon out of range"}), 400

    use_case = (vals.get("use_case") or "").strip()
    latency_target = (vals.get("latency_target") or "").strip()
    prepared_for = (vals.get("prepared_for") or "").strip()
    prepared_by = (vals.get("prepared_by") or "").strip() or "DC Hub"
    try:
        capacity_mw = float(vals.get("capacity_mw") or 100)
        if capacity_mw <= 0 or capacity_mw > 10000:
            capacity_mw = 100.0
    except (TypeError, ValueError):
        capacity_mw = 100.0
    fmt = (vals.get("format") or "html").lower()
    form = (vals.get("form") or "").strip().lower()  # "premium" -> Martone Advisors Site Analysis

    try:
        survey = _get_or_build_survey(lat, lon, use_case, latency_target,
                                      prepared_for, prepared_by, capacity_mw)
    except Exception as e:
        return jsonify({"error": "report_build_failed",
                        "detail": f"{type(e).__name__}: {str(e)[:200]}"}), 500

    # POST (submission portal): embed uploaded fiber-locator maps. Deep-copy so
    # we never mutate the shared CACHED survey (that would leak one caller's
    # uploaded maps into another caller's cached report).
    if request.method == "POST" and request.files:
        import copy as _copy
        survey = _copy.deepcopy(survey)
        _site = _img_data_uri(request.files.get("site_map"))
        _fiber = _img_data_uri(request.files.get("fiber_map"))
        _latency = _img_data_uri(request.files.get("latency_map"))
        if _site:
            survey.setdefault("power", {})["site_map"] = _site
            survey["power"]["_site_map_pin"] = False  # user's own map — no center pin
            survey["power"]["site_map_caption"] = (
                vals.get("site_map_caption") or "DC Hub land + power view — uploaded site map.")
        if _fiber:
            survey.setdefault("fiber", {})["fiber_map"] = _fiber
            survey["fiber"]["fiber_map_caption"] = (
                vals.get("fiber_map_caption") or "Fiber-locator export — carrier routes near the site.")
        if _latency:
            survey.setdefault("fiber", {})["latency_map"] = _latency
            survey["fiber"]["latency_map_caption"] = (
                vals.get("latency_map_caption") or "Fiber-locator route — measured latency path.")

    if fmt == "json":
        # Strip private "_*" helper keys for a clean machine-readable payload.
        def clean(o):
            if isinstance(o, dict):
                return {k: clean(v) for k, v in o.items() if not k.startswith("_")}
            if isinstance(o, list):
                return [clean(x) for x in o]
            return o
        _resp = {"ok": True, "lat": lat, "lon": lon, "survey": clean(survey)}
        # VALUE-MOMENT (2026-07-08): every json report yields a BRANDED, shareable
        # 5-page Site Analysis PDF the agent can hand to a named human — the
        # anon-agent → named-human bridge. The signed link opens with NO Pro
        # session (scoped to this lat/lon, ~7d). This branch is reached ONLY after
        # the PRO gate / admin / signed bypass above, so surfacing the link never
        # leaks the paywalled deliverable to a free caller. (Previously gated on
        # form=="premium"; the signed URL always mints the premium template, so a
        # standard json call now gets the shareable artifact too.)
        _pdf_url = _signed_pdf_url(lat, lon, extra={
            "capacity_mw": (int(capacity_mw) if capacity_mw else None),
            "prepared_for": prepared_for, "prepared_by": prepared_by,
            "latency_target": latency_target,
        })
        _resp["pdf_report_url"] = _pdf_url
        _resp["deliverable"] = "Branded 5-page DC Hub Site Analysis PDF"
        _resp["share_text"] = (
            f"DC Hub Site Analysis · {lat:.4f}, {lon:.4f}"
            + (f" · prepared for {prepared_for}" if prepared_for else "")
            + f" · download (no login, ~7-day link): {_pdf_url}")
        _resp["share_hint"] = ("Hand `pdf_report_url` to your human — it opens the branded "
                               "DC Hub PDF with no login. `share_text` is paste-ready.")
        return jsonify(_resp)

    if fmt == "pdf":
        try:
            # r-pdfrender (2026-06-24): render via the private Gotenberg (chromium)
            # service — weasyprint is dead on Railway (libgobject can't load). The
            # _render_html(survey) output is the same HTML the &format=html path serves.
            from routes.pdf_render import html_to_pdf
            # premium has more pages + map fetches -> a longer Gotenberg timeout.
            pdf_bytes = html_to_pdf(_render_for(form, survey, lat, lon),
                                    timeout=90 if form == "premium" else 60)
        except Exception as e:
            # Render service unreachable → degrade honestly to a 503 that points at the
            # print-to-PDF-ready HTML report. Never a 500.
            return jsonify({
                "error": "pdf_engine_unavailable",
                "message": ("Server-side PDF export is temporarily unavailable. The full HTML report "
                            "is available now and is print-to-PDF ready (Cmd/Ctrl-P → Save as PDF)."),
                "html_url": f"/api/v1/site-report?lat={lat}&lon={lon}",
                "detail": f"{type(e).__name__}: {str(e)[:160]}",
            }), 503
        fn = (f"Martone_Advisors_Site_Analysis_{lat:.4f}_{lon:.4f}.pdf" if form == "premium"
              else f"DC_Hub_Site_Report_{lat:.4f}_{lon:.4f}.pdf")
        return Response(pdf_bytes, mimetype="application/pdf", headers={
            "Content-Disposition": f'attachment; filename="{fn}"',
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        })

    return Response(_render_for(form, survey, lat, lon), mimetype="text/html; charset=utf-8", headers={
        "Cache-Control": "private, no-store",
        "X-Content-Type-Options": "nosniff",
    })


def register_site_report_routes(app):
    """Register the site-report blueprint (idempotent guard upstream)."""
    app.register_blueprint(site_report_bp)
