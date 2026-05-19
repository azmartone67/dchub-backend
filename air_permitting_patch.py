# =================================================================
# AIR PERMITTING LAYER — EPA Green Book / AQS / FLM / NEI
# Added: 2026-04-14
# Feeds the Environmental & Risk "Air Permitting" layer on the Land &
# Power Map. All endpoints under /api/infrastructure/air-permitting/*
# return the standard {success, count, data, elapsed_ms} envelope.
# =================================================================
import math as _ap_math
import time as _ap_time


# ------------------------------------------------------------------
# Seed data (illustrative; replace with live EPA feeds when available)
# ------------------------------------------------------------------
_AP_NONATTAINMENT = {
    "ozone": [
        {"name": "Houston-Galveston-Brazoria",         "class": "Serious",  "bounds": [[28.9, -96.3], [30.2, -94.3]]},
        {"name": "Dallas-Fort Worth",                  "class": "Moderate", "bounds": [[32.2, -97.8], [33.7, -96.1]]},
        {"name": "Phoenix-Mesa",                       "class": "Moderate", "bounds": [[32.9, -112.8],[34.0, -111.4]]},
        {"name": "Northern Virginia (DC metro)",       "class": "Marginal", "bounds": [[38.3, -77.8], [39.3, -76.8]]},
        {"name": "Chicago-Naperville",                 "class": "Moderate", "bounds": [[41.2, -88.4], [42.5, -87.2]]},
        {"name": "LA South Coast",                     "class": "Extreme",  "bounds": [[33.3, -118.8],[34.8, -116.5]]},
        {"name": "New York-Northern NJ-CT",            "class": "Moderate", "bounds": [[40.3, -74.6], [41.4, -73.0]]},
        {"name": "Denver Metro-North Front Range",     "class": "Serious",  "bounds": [[39.4, -105.4],[40.5, -104.4]]},
    ],
    "pm25": [
        {"name": "San Joaquin Valley",                 "class": "Serious",  "bounds": [[35.0, -121.0],[38.0, -118.5]]},
        {"name": "Allegheny County PA",                "class": "Moderate", "bounds": [[40.2, -80.3], [40.7, -79.6]]},
        {"name": "LA South Coast",                     "class": "Serious",  "bounds": [[33.3, -118.8],[34.8, -116.5]]},
        {"name": "Imperial County CA",                 "class": "Moderate", "bounds": [[32.6, -116.1],[33.5, -114.5]]},
    ],
    "pm10": [
        {"name": "Phoenix West (Salt River)",          "class": "Serious",     "bounds": [[33.2, -113.0],[33.8, -112.3]]},
        {"name": "Imperial County CA",                 "class": "Serious",     "bounds": [[32.6, -116.1],[33.5, -114.5]]},
        {"name": "Clark County NV",                    "class": "Maintenance", "bounds": [[35.9, -115.4],[36.4, -114.9]]},
        {"name": "Yuma, AZ",                           "class": "Moderate",    "bounds": [[32.5, -114.9],[32.9, -114.4]]},
    ],
}

_AP_MONITORS = [
    {"id":"AQS-04-013-4003","pol":"PM10","dv":165,"lat":33.42,"lon":-112.09,"naaqs":150,"year":2024},
    {"id":"AQS-04-013-0019","pol":"PM10","dv":142,"lat":33.50,"lon":-112.16,"naaqs":150,"year":2024},
    {"id":"AQS-04-013-9997","pol":"PM2.5","dv":8.1,"lat":33.50,"lon":-112.05,"naaqs":9,"year":2024},
    {"id":"AQS-51-107-1005","pol":"PM2.5","dv":8.6,"lat":38.95,"lon":-77.45,"naaqs":9,"year":2024},
    {"id":"AQS-19-153-0030","pol":"PM2.5","dv":6.4,"lat":41.60,"lon":-93.61,"naaqs":9,"year":2024},
    {"id":"AQS-39-049-0037","pol":"PM2.5","dv":7.8,"lat":40.00,"lon":-82.95,"naaqs":9,"year":2024},
    {"id":"AQS-06-037-1103","pol":"PM2.5","dv":12.1,"lat":34.07,"lon":-118.22,"naaqs":9,"year":2024},
    {"id":"AQS-48-113-0069","pol":"O3","dv":0.076,"lat":32.82,"lon":-96.83,"naaqs":0.070,"year":2024},
    {"id":"AQS-48-201-1039","pol":"O3","dv":0.081,"lat":29.76,"lon":-95.36,"naaqs":0.070,"year":2024},
    {"id":"AQS-51-059-0030","pol":"O3","dv":0.069,"lat":38.90,"lon":-77.27,"naaqs":0.070,"year":2024},
    {"id":"AQS-17-031-0076","pol":"O3","dv":0.072,"lat":41.87,"lon":-87.63,"naaqs":0.070,"year":2024},
    {"id":"AQS-40-109-1037","pol":"O3","dv":0.068,"lat":35.47,"lon":-97.52,"naaqs":0.070,"year":2024},
    {"id":"AQS-39-049-0081","pol":"O3","dv":0.069,"lat":40.08,"lon":-82.81,"naaqs":0.070,"year":2024},
    {"id":"AQS-19-153-0033","pol":"O3","dv":0.063,"lat":41.64,"lon":-93.45,"naaqs":0.070,"year":2024},
    {"id":"AQS-04-013-1003","pol":"O3","dv":0.076,"lat":33.45,"lon":-112.07,"naaqs":0.070,"year":2024},
]

_AP_CLASS1 = [
    {"name":"Grand Canyon NP","lat":36.10,"lon":-112.10,"state":"AZ"},
    {"name":"Saguaro NP","lat":32.25,"lon":-110.50,"state":"AZ"},
    {"name":"Chiricahua Wilderness","lat":31.85,"lon":-109.30,"state":"AZ"},
    {"name":"Shenandoah NP","lat":38.50,"lon":-78.40,"state":"VA"},
    {"name":"Dolly Sods Wilderness","lat":39.00,"lon":-79.30,"state":"WV"},
    {"name":"James River Face Wilderness","lat":37.60,"lon":-79.40,"state":"VA"},
    {"name":"Big Bend NP","lat":29.30,"lon":-103.30,"state":"TX"},
    {"name":"Guadalupe Mountains NP","lat":31.90,"lon":-104.90,"state":"TX"},
    {"name":"Caney Creek Wilderness","lat":34.50,"lon":-94.00,"state":"AR"},
    {"name":"Wichita Mountains Wilderness","lat":34.70,"lon":-98.70,"state":"OK"},
    {"name":"Great Smoky Mountains NP","lat":35.60,"lon":-83.50,"state":"TN"},
    {"name":"Yosemite NP","lat":37.80,"lon":-119.50,"state":"CA"},
    {"name":"Sequoia NP","lat":36.50,"lon":-118.70,"state":"CA"},
    {"name":"Mingo Wilderness","lat":37.00,"lon":-90.10,"state":"MO"},
    {"name":"Hercules-Glades Wilderness","lat":36.70,"lon":-93.00,"state":"MO"},
    {"name":"Otter Creek Wilderness","lat":38.80,"lon":-79.70,"state":"WV"},
    {"name":"Boundary Waters Canoe Area","lat":47.90,"lon":-91.50,"state":"MN"},
    {"name":"Rocky Mountain NP","lat":40.34,"lon":-105.68,"state":"CO"},
    {"name":"Zion NP","lat":37.30,"lon":-113.05,"state":"UT"},
    {"name":"Mount Rainier NP","lat":46.85,"lon":-121.76,"state":"WA"},
]

_AP_NEI = [
    {"name":"APS Redhawk Power Plant","lat":33.42,"lon":-112.75,"state":"AZ","type":"gas"},
    {"name":"Palo Verde Nuclear Generating Station","lat":33.39,"lon":-112.86,"state":"AZ","type":"nuclear"},
    {"name":"Salt River Agua Fria Generating Station","lat":33.57,"lon":-112.30,"state":"AZ","type":"gas"},
    {"name":"APS Ocotillo Power Plant","lat":33.39,"lon":-111.94,"state":"AZ","type":"gas"},
    {"name":"Asarco Hayden Smelter","lat":33.00,"lon":-110.78,"state":"AZ","type":"smelter"},
    {"name":"Midlothian cement corridor","lat":32.48,"lon":-97.00,"state":"TX","type":"cement"},
    {"name":"Luminant Big Brown","lat":32.05,"lon":-96.05,"state":"TX","type":"coal"},
    {"name":"Dallas gas peakers complex","lat":32.70,"lon":-96.90,"state":"TX","type":"gas"},
    {"name":"Houston Ship Channel refineries","lat":29.72,"lon":-95.08,"state":"TX","type":"refinery"},
    {"name":"NOVEC substation complex","lat":39.01,"lon":-77.50,"state":"VA","type":"substation"},
    {"name":"Dulles airport operations","lat":38.95,"lon":-77.45,"state":"VA","type":"airport"},
    {"name":"Dominion Possum Point","lat":38.53,"lon":-77.30,"state":"VA","type":"gas"},
    {"name":"Covanta Alexandria Energy Resource","lat":38.80,"lon":-77.06,"state":"VA","type":"waste-to-energy"},
    {"name":"MidAmerican Des Moines Energy Center","lat":41.52,"lon":-93.51,"state":"IA","type":"gas"},
    {"name":"Prairie Creek Generating Station","lat":41.97,"lon":-91.65,"state":"IA","type":"gas"},
    {"name":"Ames Power Plant","lat":42.03,"lon":-93.61,"state":"IA","type":"coal"},
    {"name":"AEP Gavin","lat":38.94,"lon":-82.11,"state":"OH","type":"coal"},
    {"name":"Honda East Liberty","lat":40.22,"lon":-83.56,"state":"OH","type":"industrial"},
    {"name":"Columbus metro industrial complex","lat":39.96,"lon":-82.99,"state":"OH","type":"industrial"},
    {"name":"Midwest Generation Joliet","lat":41.56,"lon":-88.06,"state":"IL","type":"gas"},
    {"name":"LA Refinery complex","lat":33.78,"lon":-118.26,"state":"CA","type":"refinery"},
    {"name":"Port of LA emissions source","lat":33.73,"lon":-118.26,"state":"CA","type":"port"},
]

_AP_STATE_CONTEXT = {
    "AZ": {"score":65, "agency":"Arizona ADEQ", "description":"ADEQ — Class II permits avg 14 months. Cumulative impact analysis required in Phoenix PM10 Serious NA. Offsets $18–35k/ton NOx."},
    "TX": {"score":75, "agency":"Texas TCEQ",   "description":"TCEQ — Standard Permit below 10 tpy NOx; above that full NSR. Timelines 6–9 mo minor, 18+ mo NNSR. DFW/HGB need offsets."},
    "VA": {"score":88, "agency":"Virginia DEQ", "description":"DEQ — well-trodden Loudoun data-center pathway. Synthetic minor via runtime caps 4–6 mo. FLM consult required near Shenandoah."},
    "IA": {"score":92, "agency":"Iowa DNR",     "description":"DNR — predictable 4–5 mo minor source permitting. Full attainment, no offsets, minimal modeling."},
    "OH": {"score":82, "agency":"Ohio EPA",     "description":"Ohio EPA — 5–8 mo permitting. GHG PSD BACT at >150 MW. Watch Title V classification."},
    "CA": {"score":30, "agency":"CARB+SCAQMD",  "description":"California — most complex permitting in US. Nearly any large site triggers NNSR on multiple pollutants. 18–30 mo timelines."},
    "NV": {"score":70, "agency":"Nevada NDEP",  "description":"NDEP — simple outside Clark County PM10 maintenance. Reno-Tahoe 4–6 mo."},
    "IL": {"score":60, "agency":"Illinois EPA", "description":"IEPA — Chicago ozone Moderate NA drives NNSR. Downstate much faster."},
    "PA": {"score":55, "agency":"PA DEP",       "description":"PA DEP — Allegheny Co PM2.5 NA is main constraint. Outside that 6–10 mo."},
    "NY": {"score":50, "agency":"NY DEC",       "description":"NY DEC — complex in metro. CLCPA adds GHG reduction obligations."},
    "GA": {"score":78, "agency":"GA EPD",       "description":"GA EPD — Atlanta Marginal ozone NA. Lithia Springs corridor 5–7 mo."},
    "NC": {"score":80, "agency":"NC DEQ",       "description":"NC DEQ — Charlotte and RTP attainment. 4–6 mo for minor sources."},
    "WA": {"score":72, "agency":"WA Ecology",   "description":"Central WA (Quincy, Moses Lake) attainment. Puget Sound more complex."},
    "OR": {"score":75, "agency":"Oregon DEQ",   "description":"OR DEQ — Hillsboro/Prineville corridor benefits from attainment status."},
    "CO": {"score":58, "agency":"Colorado CDPHE","description":"CDPHE — Denver Metro ozone Serious NA binding within 50 mi. Offsets scarce."},
    "UT": {"score":65, "agency":"Utah DEQ",     "description":"UT DEQ — Wasatch Front PM2.5 maintenance. Winter inversion drives tight constraints."},
}

_AP_SAMPLE_SITES = [
    {"id":"ph1","name":"Goodyear, AZ","lat":33.44,"lon":-112.36,"capacity":120},
    {"id":"dfw1","name":"Red Oak, TX","lat":32.52,"lon":-96.80,"capacity":150},
    {"id":"va1","name":"Ashburn, VA","lat":39.04,"lon":-77.49,"capacity":90},
    {"id":"ia1","name":"Altoona, IA","lat":41.64,"lon":-93.45,"capacity":100},
    {"id":"oh1","name":"New Albany, OH","lat":40.08,"lon":-82.81,"capacity":200},
]

_AP_STATE_BOXES = {
    "AZ":[[31.3,-115.0],[37.0,-109.0]], "TX":[[25.8,-106.7],[36.5,-93.5]],
    "VA":[[36.5,-83.7],[39.5,-75.2]],   "IA":[[40.3,-96.7],[43.5,-90.1]],
    "OH":[[38.4,-84.8],[41.9,-80.5]],   "CA":[[32.5,-124.5],[42.0,-114.0]],
    "NV":[[35.0,-120.0],[42.0,-114.0]], "IL":[[36.9,-91.5],[42.5,-87.0]],
    "PA":[[39.7,-80.5],[42.3,-74.7]],   "NY":[[40.4,-79.8],[45.0,-71.8]],
    "GA":[[30.3,-85.6],[35.0,-80.7]],   "NC":[[33.8,-84.4],[36.6,-75.4]],
    "WA":[[45.5,-124.8],[49.0,-116.9]], "OR":[[42.0,-124.6],[46.3,-116.5]],
    "CO":[[36.9,-109.1],[41.1,-102.0]], "UT":[[36.9,-114.1],[42.0,-109.0]],
}

_AP_OZONE_CLASS_PENALTY = {"Marginal":70,"Moderate":40,"Serious":20,"Severe":10,"Extreme":0,"Maintenance":65}
_AP_PM_CLASS_PENALTY    = {"Moderate":40,"Serious":15,"Maintenance":60}


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------
def _ap_haversine_km(lat1, lon1, lat2, lon2):
    R = 6371.0
    rlat1, rlat2 = _ap_math.radians(lat1), _ap_math.radians(lat2)
    dlat = _ap_math.radians(lat2 - lat1); dlon = _ap_math.radians(lon2 - lon1)
    a = _ap_math.sin(dlat/2)**2 + _ap_math.cos(rlat1)*_ap_math.cos(rlat2)*_ap_math.sin(dlon/2)**2
    return 2*R*_ap_math.asin(_ap_math.sqrt(a))


def _ap_in_bounds(lat, lon, bounds):
    (mnLat, mnLon), (mxLat, mxLon) = bounds
    return mnLat <= lat <= mxLat and mnLon <= lon <= mxLon


def _ap_na_factor(pollutant, lat, lon):
    for na in _AP_NONATTAINMENT.get(pollutant, []):
        if _ap_in_bounds(lat, lon, na["bounds"]):
            table = _AP_OZONE_CLASS_PENALTY if pollutant == "ozone" else _AP_PM_CLASS_PENALTY
            return table.get(na["class"], 25), na
        (mnLat, mnLon), (mxLat, mxLon) = na["bounds"]
        cx, cy = (mnLat+mxLat)/2, (mnLon+mxLon)/2
        dist = _ap_haversine_km(lat, lon, cx, cy)
        span = _ap_haversine_km(mnLat, mnLon, mxLat, mxLon) / 2
        if dist - span < 15:
            return 75, None
    return 100, None


def _ap_monitor_factor(lat, lon):
    enriched = []
    for m in _AP_MONITORS:
        dist = _ap_haversine_km(lat, lon, m["lat"], m["lon"])
        enriched.append({**m, "distance_km": round(dist, 1),
                         "pct_of_naaqs": round(m["dv"]/m["naaqs"]*100, 1)})
    enriched.sort(key=lambda x: x["distance_km"])
    nearest = enriched[:3]
    if not nearest:
        return 85, []
    weights = [1/max(5.0, n["distance_km"]) for n in nearest]
    total_w = sum(weights) or 1.0
    def _pct(pct):
        if pct <= 50: return 100
        if pct >= 105: return 0
        return max(0.0, 100.0*(105-pct)/55)
    weighted = sum(_pct(n["pct_of_naaqs"])*w for n,w in zip(nearest, weights)) / total_w
    if nearest[0]["distance_km"] > 150:
        weighted = min(weighted, 80)
    return int(round(weighted)), nearest


def _ap_class1_factor(lat, lon):
    enriched = [{**c, "distance_km": round(_ap_haversine_km(lat, lon, c["lat"], c["lon"]))}
                for c in _AP_CLASS1]
    enriched.sort(key=lambda x: x["distance_km"])
    nearest = enriched[:3]
    nearest_km = enriched[0]["distance_km"] if enriched else 9999
    score = 20 if nearest_km < 100 else (55 if nearest_km < 300 else 100)
    for n in nearest:
        n["flm_consultation_required"] = n["distance_km"] <= 300
    return score, nearest


def _ap_nei_factor(lat, lon, radius_km=16):
    enriched = [{**n, "distance_km": round(_ap_haversine_km(lat, lon, n["lat"], n["lon"]), 1)}
                for n in _AP_NEI if _ap_haversine_km(lat, lon, n["lat"], n["lon"]) <= radius_km]
    enriched.sort(key=lambda x: x["distance_km"])
    score = max(40, 100 - len(enriched)*12)
    return score, enriched[:10]


def _ap_resolve_state(lat, lon):
    for state, box in _AP_STATE_BOXES.items():
        if _ap_in_bounds(lat, lon, box):
            return state
    return None


def _ap_pathway(ozone_na, pm25_na, pm10_na, capacity_mw, genset_mw):
    est_nox_tpy = genset_mw * 0.35
    est_ghg_tpy = capacity_mw * 900
    ozone_class = ozone_na["class"] if ozone_na else None
    nnsr_threshold = {"Marginal":100,"Moderate":100,"Serious":50,"Severe":25,"Extreme":10,"Maintenance":100}.get(ozone_class, 999)
    in_serious_pm = (pm25_na and pm25_na["class"] in ("Serious","Severe")) or (pm10_na and pm10_na["class"] == "Serious")
    if ozone_na and est_nox_tpy >= nnsr_threshold: return "NNSR (offsets required)"
    if in_serious_pm: return "NNSR (offsets required)"
    if ozone_na or pm25_na or pm10_na: return "Synthetic Minor (runtime cap)"
    if est_ghg_tpy >= 75000: return "PSD (GHG BACT)"
    return "Minor Source Permit"


def _ap_offset_usd(pathway, genset_mw, ozone_area):
    if "NNSR" not in pathway:
        if "PSD" in pathway: return "No offsets; BACT analysis cost $0.5M–$1.5M"
        if "Synthetic" in pathway: return "No offsets; synthetic-minor cap drafting ~$50–150k"
        return "None — attainment area"
    est_nox_tpy = genset_mw * 0.35
    ratio = {"Marginal":1.1,"Moderate":1.15,"Serious":1.3,"Severe":1.3,"Extreme":1.5,"Maintenance":1.0}.get(
        ozone_area["class"] if ozone_area else "Moderate", 1.2)
    low_cost = est_nox_tpy * ratio * 12000
    high_cost = est_nox_tpy * ratio * 35000
    return f"${low_cost/1e6:.1f}M – ${high_cost/1e6:.1f}M (NOx offsets, ratio {ratio}:1)"


def _ap_pollutant_statuses(lat, lon, ozone_na, pm25_na, pm10_na, capacity_mw):
    statuses = {}
    def nearest(pol):
        cands = [m for m in _AP_MONITORS if m["pol"].lower() == pol.lower()]
        if not cands: return None, None
        best = min(cands, key=lambda m: _ap_haversine_km(lat, lon, m["lat"], m["lon"]))
        return best, _ap_haversine_km(lat, lon, best["lat"], best["lon"])

    if pm10_na:
        statuses["PM10"] = {"s":"red","d":f"{pm10_na['class']} NA: {pm10_na['name']}"}
    else:
        m, dist = nearest("PM10")
        if m and dist < 500:
            pct = m["dv"]/m["naaqs"]*100
            s = "red" if pct>100 else "yellow" if pct>80 else "green"
            statuses["PM10"] = {"s":s,"d":f"{pct:.0f}% of NAAQS (monitor {dist:.0f} km)"}
        else:
            statuses["PM10"] = {"s":"green","d":"Attainment"}

    if pm25_na:
        statuses["PM2.5"] = {"s":"red","d":f"{pm25_na['class']} NA: {pm25_na['name']}"}
    else:
        m, dist = nearest("PM2.5")
        if m and dist < 500:
            pct = m["dv"]/m["naaqs"]*100
            s = "red" if pct>100 else "yellow" if pct>80 else "green"
            statuses["PM2.5"] = {"s":s,"d":f"{pct:.0f}% of NAAQS"}
        else:
            statuses["PM2.5"] = {"s":"green","d":"Attainment"}

    if ozone_na:
        s = "red" if ozone_na["class"] in ("Moderate","Serious","Severe","Extreme") else "yellow"
        statuses["O3"] = {"s":s,"d":f"{ozone_na['class']} NA: {ozone_na['name']}"}
    else:
        m, dist = nearest("O3")
        if m and dist < 500:
            pct = m["dv"]/m["naaqs"]*100
            s = "red" if pct>100 else "yellow" if pct>85 else "green"
            statuses["O3"] = {"s":s,"d":f"DV {m['dv']} ppm ({pct:.0f}% of NAAQS)"}
        else:
            statuses["O3"] = {"s":"green","d":"Attainment"}

    statuses["NO2"] = {"s":"yellow" if ozone_na else "green",
                      "d":"Elevated — urban corridor" if ozone_na else "Below NAAQS"}
    statuses["SO2"] = {"s":"green","d":"Below NAAQS"}
    statuses["CO"]  = {"s":"green","d":"Attainment"}
    statuses["Pb"]  = {"s":"green","d":"Attainment"}

    ghg_tpy = capacity_mw * 900
    if ghg_tpy > 150000:
        statuses["GHG"] = {"s":"red","d":f"{ghg_tpy:,.0f} tpy — full PSD BACT"}
    elif ghg_tpy > 75000:
        statuses["GHG"] = {"s":"yellow","d":f"{ghg_tpy:,.0f} tpy — above 75k PSD threshold"}
    else:
        statuses["GHG"] = {"s":"green","d":f"{ghg_tpy:,.0f} tpy — below PSD"}
    return statuses


def _ap_score_site(lat, lon, capacity_mw, genset_mw=None):
    if genset_mw is None:
        genset_mw = capacity_mw * 0.6
    ozone_score, ozone_na = _ap_na_factor("ozone", lat, lon)
    pm25_score,  pm25_na  = _ap_na_factor("pm25",  lat, lon)
    pm10_score,  pm10_na  = _ap_na_factor("pm10",  lat, lon)
    monitor_score, near_monitors = _ap_monitor_factor(lat, lon)
    class1_score,  near_class1   = _ap_class1_factor(lat, lon)
    nei_score,     near_nei      = _ap_nei_factor(lat, lon)
    state = _ap_resolve_state(lat, lon)
    ctx = _AP_STATE_CONTEXT.get(state, {}) if state else {}
    state_score = ctx.get("score", 75)

    w = {"ozone":0.25,"pm25":0.25,"pm10":0.10,"monitors":0.15,"class1":0.10,"nei":0.10,"state":0.05}
    composite = (w["ozone"]*ozone_score + w["pm25"]*pm25_score + w["pm10"]*pm10_score +
                 w["monitors"]*monitor_score + w["class1"]*class1_score +
                 w["nei"]*nei_score + w["state"]*state_score)
    score = int(round(max(0, min(100, composite))))

    pathway = _ap_pathway(ozone_na, pm25_na, pm10_na, capacity_mw, genset_mw)
    offset = _ap_offset_usd(pathway, genset_mw, ozone_na)
    pollutants = _ap_pollutant_statuses(lat, lon, ozone_na, pm25_na, pm10_na, capacity_mw)

    reds = sum(1 for p in pollutants.values() if p["s"] == "red")
    yellows = sum(1 for p in pollutants.values() if p["s"] == "yellow")
    if reds >= 2:
        verdict = f"High permitting risk — {reds} pollutants in nonattainment or exceedance."
    elif reds == 1:
        verdict = "Moderate permitting risk — one pollutant constraint likely to drive NNSR or PSD."
    elif yellows >= 2:
        verdict = "Tight margins — attainment but limited cumulative headroom."
    else:
        verdict = "Clean air-permitting profile — minor source pathway likely."

    return {
        "score": score,
        "verdict_short": verdict,
        "pathway": pathway,
        "offset_estimate_usd": offset,
        "pollutants": pollutants,
        "class1": [{"n":c["name"],"km":c["distance_km"],
                    "flm_consultation_required":c["flm_consultation_required"]} for c in near_class1],
        "nei": [{"n":n["name"],"d":round(n["distance_km"]*0.6214,1)} for n in near_nei],
        "nearest_monitors": near_monitors,
        "state": state,
        "state_context": ctx.get("description", ""),
        "factors": {
            "ozone": {"score":ozone_score,"in_na":ozone_na["name"] if ozone_na else None},
            "pm25":  {"score":pm25_score, "in_na":pm25_na["name"]  if pm25_na  else None},
            "pm10":  {"score":pm10_score, "in_na":pm10_na["name"]  if pm10_na  else None},
            "monitors":monitor_score,"class1":class1_score,"nei":nei_score,"state":state_score,
        },
        "inputs": {"lat":lat,"lon":lon,"capacity_mw":capacity_mw,"genset_mw":genset_mw},
    }


# ------------------------------------------------------------------
# Flask routes
# ------------------------------------------------------------------
# AUTO-REPAIR: duplicate route '/api/infrastructure/air-permitting/nonattainment' also in main.py:20469 — review and remove one
@app.route('/api/infrastructure/air-permitting/nonattainment', methods=['GET'])
def ap_nonattainment():
    """EPA Green Book nonattainment GeoJSON for the map overlay."""
    _t0 = _ap_time.time()
    pollutant = request.args.get('pollutant')
    features = []
    pols = [pollutant] if pollutant else list(_AP_NONATTAINMENT.keys())
    for p in pols:
        if p not in _AP_NONATTAINMENT: continue
        for na in _AP_NONATTAINMENT[p]:
            (mnLat, mnLon), (mxLat, mxLon) = na["bounds"]
            features.append({
                "type": "Feature",
                "geometry": {"type":"Polygon","coordinates":[[
                    [mnLon,mnLat],[mxLon,mnLat],[mxLon,mxLat],[mnLon,mxLat],[mnLon,mnLat]
                ]]},
                "properties": {"pollutant":p,"name":na["name"],"classification":na["class"],
                               "source":"EPA Green Book"}
            })
    return jsonify({"success":True,"count":len(features),
                    "data":{"type":"FeatureCollection","features":features},
                    "elapsed_ms":round((_ap_time.time()-_t0)*1000, 2)})

# AUTO-REPAIR: duplicate route '/api/infrastructure/air-permitting/monitors' also in main.py:20493 — review and remove one

@app.route('/api/infrastructure/air-permitting/monitors', methods=['GET'])
def ap_monitors():
    """AQS monitor points with design values."""
    _t0 = _ap_time.time()
    lat_s = request.args.get('lat')
    lon_s = request.args.get('lon')
    radius_km = float(request.args.get('radius_km', 500))
    limit = int(request.args.get('limit', 50))
    results = []
    for m in _AP_MONITORS:
        item = dict(m)
        if lat_s and lon_s:
            dist = _ap_haversine_km(float(lat_s), float(lon_s), m["lat"], m["lon"])
            if dist > radius_km: continue
            item["distance_km"] = round(dist, 1)
        item["pct_of_naaqs"] = round(m["dv"]/m["naaqs"]*100, 1)
        item["exceeds_naaqs"] = m["dv"] > m["naaqs"]
        results.append(item)
    results.sort(key=lambda x: x.get("distance_km", 0))
    return jsonify({"success":True,"count":len(results[:limit]),
                    "data":results[:limit],
                    "elapsed_ms":round((_ap_time.time()-_t0)*1000, 2)})
# AUTO-REPAIR: duplicate route '/api/infrastructure/air-permitting/class1' also in main.py:20517 — review and remove one


@app.route('/api/infrastructure/air-permitting/class1', methods=['GET'])
def ap_class1():
    """Federal Class I areas (NPS + Wilderness)."""
    _t0 = _ap_time.time()
    return jsonify({"success":True,"count":len(_AP_CLASS1),"data":_AP_CLASS1,
# AUTO-REPAIR: duplicate route '/api/infrastructure/air-permitting/sites' also in main.py:20525 — review and remove one
                    "elapsed_ms":round((_ap_time.time()-_t0)*1000, 2)})


@app.route('/api/infrastructure/air-permitting/sites', methods=['GET'])
def ap_sites():
    """Sample candidate sites (demo)."""
    _t0 = _ap_time.time()
# AUTO-REPAIR: duplicate route '/api/infrastructure/air-permitting/score' also in main.py:20533 — review and remove one
    return jsonify({"success":True,"count":len(_AP_SAMPLE_SITES),"data":_AP_SAMPLE_SITES,
                    "elapsed_ms":round((_ap_time.time()-_t0)*1000, 2)})


@app.route('/api/infrastructure/air-permitting/score', methods=['GET', 'POST'])
def ap_score():
    """
    Parcel air-permitting score.
    GET  ?lat=&lon=&capacity_mw=&genset_mw=
    POST JSON {lat, lon, capacity_mw, genset_mw?}
    """
    _t0 = _ap_time.time()
    try:
        if request.method == 'POST':
            body = request.get_json(silent=True) or {}
            lat = float(body.get('lat'))
            lon = float(body.get('lon'))
            capacity_mw = float(body.get('capacity_mw', 100))
            genset_mw = body.get('genset_mw')
            genset_mw = float(genset_mw) if genset_mw is not None else None
        else:
            lat = float(request.args.get('lat'))
            lon = float(request.args.get('lon'))
            capacity_mw = float(request.args.get('capacity_mw', 100))
            g = request.args.get('genset_mw')
            genset_mw = float(g) if g else None
    except (TypeError, ValueError) as _e:
        return jsonify({"success":False,"error":f"Invalid input: {_e}"}), 400

    try:
        result = _ap_score_site(lat, lon, capacity_mw, genset_mw)
    except Exception as _e:
        return jsonify({"success":False,"error":f"Scoring failed: {_e}"}), 500
# AUTO-REPAIR: duplicate route '/api/infrastructure/air-permitting/health' also in main.py:20567 — review and remove one

    return jsonify({"success":True,"count":1,"data":result,
                    "elapsed_ms":round((_ap_time.time()-_t0)*1000, 2)})


@app.route('/api/infrastructure/air-permitting/health', methods=['GET'])
def ap_health():
    """Health / data-catalog endpoint."""
    return jsonify({
        "success": True,
        "service": "air-permitting",
        "version": "1.0.0",
        "sources": ["EPA Green Book","EPA AQS","NPS FLM","EPA NEI"],
        "data_points": {
            "nonattainment_areas": sum(len(v) for v in _AP_NONATTAINMENT.values()),
            "monitors": len(_AP_MONITORS),
            "class1": len(_AP_CLASS1),
            "sample_sites": len(_AP_SAMPLE_SITES),
        },
    })

# =================================================================
# END AIR PERMITTING LAYER
# =================================================================
