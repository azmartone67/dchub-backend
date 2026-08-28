"""
iso_eu_entsoe.py — Europe grid ingestion via ENTSO-E Transparency (LIVE).

#60 global-expansion (2026-06-02). Third LIVE international integration (after
GB Elexon + AU AEMO) and by far the broadest: ONE token unlocks ~all of the
European bidding zones. The zone list is ONE explicit registry
(_ZONE_REGISTRY below) — adding a bidding zone is one row, nothing else.

DATA SOURCE — ENTSO-E Transparency Platform REST API:
  https://web-api.tp.entsoe.eu/api
  • A75 / processType A16 — Actual Generation per Production Type (per zone,
    per PSR fuel B-code, MW). This is the fuel mix.
  Auth: securityToken query param. The token is read from the environment
  (ENTSOE_API_Token, with case/format fallbacks). NO token embedded in code.
  Rate limit: this repo cites it TWO ways — 400 req/min here vs 100 req/min in
  routes/international_ingestion.py:16. UNVERIFIED which is correct, so the
  call rate is sized against the LOWER of the two. We issue at most one call
  per zone per _ZONE_TTL, not one per request (see the rate math there).

  NOTE: the API speaks XML (GL_MarketDocument), not JSON. We parse it
  namespace-agnostically (local-name matching) and take the LATEST point per
  PSR TimeSeries (the most recent settled period; ENTSO-E lags ~1-2h).

HONESTY: LIVE-ONLY. If the token is missing OR a zone's API call/parse fails,
that zone writes NOTHING — no modeled/fabricated fallback (unlike the Nord
Pool / IESO baseline modules). renewable_pct here = wind+solar+hydro / total
to MATCH the US/UK scoreboard definition (biomass reported separately) so the
grids rank apples-to-apples. ISO_CODE = "ENTSOE" for the aggregate; per-zone
rows persist under EU_<code> so the ENTSOE-tagged DCPI markets can join.
"""
import os
import time
import datetime
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager

import psycopg2 as _pg
from flask import Blueprint, jsonify, request
from routes._swallowed_writes import note_swallowed_write

try:
    import requests as _rq
except Exception:
    _rq = None

iso_eu_entsoe_bp = Blueprint("iso_eu_entsoe", __name__, url_prefix="/api/v1/iso/eu")
SOURCE_ID = "iso-eu-entsoe-live"
ISO_CODE = "ENTSOE"

_ENTSOE_BASE = "https://web-api.tp.entsoe.eu/api"

# ENTSO-E PSR (Production Source) B-codes → our fuel category.
_PSR = {
    "B01": "biomass",
    "B02": "coal",      # Fossil Brown coal / Lignite
    "B03": "gas",       # Fossil Coal-derived gas
    "B04": "gas",       # Fossil Gas
    "B05": "coal",      # Fossil Hard coal
    "B06": "oil",
    "B07": "oil",       # Fossil Oil shale
    "B08": "other",     # Fossil Peat
    "B09": "geothermal",
    "B10": "hydro",     # Pumped Storage (generation leg)
    "B11": "hydro",     # Run-of-river
    "B12": "hydro",     # Water Reservoir
    "B13": "other",     # Marine
    "B14": "nuclear",
    "B15": "other",     # Other renewable
    "B16": "solar",
    "B17": "biomass",   # Waste (count with biomass)
    "B18": "wind",      # Wind Offshore
    "B19": "wind",      # Wind Onshore
    "B20": "other",
}
# Scoreboard-comparable renewable = wind+solar+hydro (matches the US EIA +
# UK Elexon definition the get_grid_scoreboard tool ranks on; biomass shown
# separately so it never silently inflates the ranking).
_RENEWABLE_CATS = {"wind", "solar", "hydro"}

# ── ENTSO-E bidding-zone REGISTRY ──────────────────────────────────
# THE single source of truth for which European zones this module fetches.
#
# TO ADD A ZONE: append ONE row. Nothing else is required — the blueprint is
# already registered (main.py), there is no DB migration, no cron change and no
# zone list on the MCP side. _SNAP_TTL is 300s, so a new row is live on every
# surface within ~5 min of deploy.
#
# Row = (code, EIC in_Domain, display name, hub city, EIC provenance)
#   code       — surfaces on the scoreboard as EU_<code>; keep the SE_3 / NO_1
#                underscore form so ids stay consistent with the existing rows.
#   provenance — WHERE that EIC was verified. Never add a row without one: a
#                wrong EIC fails SILENTLY (non-200 or an Acknowledgement doc
#                → _zone_snapshot returns None → the zone simply never appears,
#                with no log, no error and no alert).
#   hub city   — display label only; nothing joins on it.
#
# AFTER ADDING, VERIFY EACH NEW CODE POST-DEPLOY:
#   GET /api/v1/iso/eu/debug?zone=<CODE> → parsed != null AND zone_snapshot != null
# Do NOT check your work against /api/v1/iso/eu/snapshot: it is tier-gated and
# strips "zones" for anonymous callers, so a working zone looks missing there.
_EXCLUDED_EICS = {
    # Deliberately NOT in the registry — each grid is already its own scoreboard
    # row, so adding it here would double-count it. Enforced by _build_zones.
    "10YGB----------A": "GB — served live by routes/iso_uk_elexon.py (NGESO)",
    "10YIE-1001A00010": "IE — already covered by the all-island IE_SEM zone",
}

_ZONE_REGISTRY = [
    # code       EIC                   display name           hub city          EIC provenance
    ("DE_LU",   "10Y1001A1001A82H", "Germany–Luxembourg",  "Frankfurt",       "#60 2026-06-02; live"),
    ("FR",      "10YFR-RTE------C", "France",              "Paris",           "#60 2026-06-02; live"),
    ("NL",      "10YNL----------L", "Netherlands",         "Amsterdam",       "#60 2026-06-02; live"),
    ("IE_SEM",  "10Y1001A1001A59C", "Ireland (SEM)",       "Dublin",          "#60 2026-06-02; live"),
    ("ES",      "10YES-REE------0", "Spain",               "Madrid",          "#60 2026-06-02; live"),
    ("BE",      "10YBE----------2", "Belgium",             "Brussels",        "#60 2026-06-02; live"),
    ("PL",      "10YPL-AREA-----S", "Poland",              "Warsaw",          "#60 2026-06-02; live"),
    ("AT",      "10YAT-APG------L", "Austria",             "Vienna",          "#60 2026-06-02; live"),
    ("SE_3",    "10Y1001A1001A46L", "Sweden (SE3)",        "Stockholm",       "#60 2026-06-02; live"),
    ("NO_1",    "10YNO-1--------2", "Norway (NO1)",        "Oslo",            "#60 2026-06-02; live"),
    ("FI",      "10YFI-1--------U", "Finland",             "Helsinki",        "#60 2026-06-02; live"),
    ("DK_1",    "10YDK-1--------W", "Denmark (DK1)",       "Copenhagen",      "#60 2026-06-02; live"),
    # r-eu-expand (2026-06-25): +13 zones, EICs verified against the entsoe-py
    # canonical mapping. DC-growth zones first.
    ("IT_NORD", "10Y1001A1001A73I", "Italy North",         "Milan",           "r-eu-expand 2026-06-25; live"),
    ("CH",      "10YCH-SWISSGRIDZ", "Switzerland",         "Zurich",          "r-eu-expand 2026-06-25; live"),
    ("PT",      "10YPT-REN------W", "Portugal",            "Lisbon",          "r-eu-expand 2026-06-25; live"),
    ("CZ",      "10YCZ-CEPS-----N", "Czech Republic",      "Prague",          "r-eu-expand 2026-06-25; live"),
    ("SE_4",    "10Y1001A1001A47J", "Sweden (SE4)",        "Malmo",           "r-eu-expand 2026-06-25; live"),
    ("NO_2",    "10YNO-2--------T", "Norway (NO2)",        "Stavanger",       "r-eu-expand 2026-06-25; live"),
    ("DK_2",    "10YDK-2--------M", "Denmark (DK2)",       "Copenhagen E",    "r-eu-expand 2026-06-25; live"),
    ("GR",      "10YGR-HTSO-----Y", "Greece",              "Athens",          "r-eu-expand 2026-06-25; live"),
    ("RO",      "10YRO-TEL------P", "Romania",             "Bucharest",       "r-eu-expand 2026-06-25; live"),
    ("HU",      "10YHU-MAVIR----U", "Hungary",             "Budapest",        "r-eu-expand 2026-06-25; live"),
    ("SK",      "10YSK-SEPS-----K", "Slovakia",            "Bratislava",      "r-eu-expand 2026-06-25; live"),
    # BG answers INTERMITTENTLY: ENTSO-E returns an Acknowledgement (no data)
    # for long stretches, so this zone drops in and out of the live count. That
    # is why "configured" and "returned" are reported separately — never quote
    # the configured number as the number of zones the scoreboard shows.
    ("BG",      "10YCA-BULGARIA-R", "Bulgaria",            "Sofia",           "r-eu-expand 2026-06-25; INTERMITTENT"),
    ("SI",      "10YSI-ELES-----O", "Slovenia",            "Ljubljana",       "r-eu-expand 2026-06-25; live"),
    # ws2-entsoe (2026-07-29): +8. EICs copied from global_power_apis.py AREAS,
    # which carries its own provenance note (verified against the canonical
    # entsoe-py mapping) — no un-sourced EIC was invented for this expansion.
    # Completes Italy (all 7 bidding zones) and Sweden (all 4).
    ("IT_CNOR", "10Y1001A1001A70O", "Italy Centre-North",  "Florence",        "global_power_apis.AREAS (entsoe-py canonical)"),
    ("IT_CSUD", "10Y1001A1001A71M", "Italy Centre-South",  "Rome",            "global_power_apis.AREAS (entsoe-py canonical)"),
    ("IT_SUD",  "10Y1001A1001A788", "Italy South",         "Naples",          "global_power_apis.AREAS (entsoe-py canonical)"),
    ("IT_SICI", "10Y1001A1001A75E", "Italy Sicily",        "Palermo",         "global_power_apis.AREAS (entsoe-py canonical)"),
    ("IT_SARD", "10Y1001A1001A74G", "Italy Sardinia",      "Cagliari",        "global_power_apis.AREAS (entsoe-py canonical)"),
    ("IT_CALA", "10Y1001C--00096J", "Italy Calabria",      "Reggio Calabria", "global_power_apis.AREAS (entsoe-py canonical)"),
    ("SE_1",    "10Y1001A1001A44P", "Sweden (SE1)",        "Lulea",           "global_power_apis.AREAS (entsoe-py canonical)"),
    ("SE_2",    "10Y1001A1001A45N", "Sweden (SE2)",        "Sundsvall",       "global_power_apis.AREAS (entsoe-py canonical)"),
]


def _build_zones(registry):
    """registry rows → {code: (eic, name, hub)} — the exact 3-tuple contract the
    rest of this module reads, so adding a row touches nothing else.

    NEVER RAISES. main.py registers this blueprint inside a try/except that only
    prints, so an exception at import time would 404 every /api/v1/iso/eu/*
    route with no other signal. A malformed / duplicate / excluded row is
    DROPPED and reported in the returned warnings list, which /zones and /health
    echo — the drop is visible instead of silent."""
    zones, by_eic, warnings = {}, {}, []
    for row in registry:
        try:
            code = str(row[0]).strip().upper()
            eic, name, hub = str(row[1]).strip(), str(row[2]).strip(), str(row[3]).strip()
        except Exception:
            warnings.append(f"malformed_row:{row!r}")
            continue
        if not code or not eic or not name:
            warnings.append(f"incomplete_row:{code or row!r}")
            continue
        if code in zones:
            warnings.append(f"duplicate_code:{code}")
            continue
        if eic in by_eic:
            warnings.append(f"duplicate_eic:{code}_vs_{by_eic[eic]}:{eic}")
            continue
        if eic in _EXCLUDED_EICS:
            warnings.append(f"excluded_eic:{code}:{_EXCLUDED_EICS[eic]}")
            continue
        if len(row) < 5 or not str(row[4]).strip():
            warnings.append(f"no_eic_provenance:{code}")   # kept, but flagged
        zones[code] = (eic, name, hub)
        by_eic[eic] = code
    return zones, warnings


# (code → (EIC in_Domain, name, hub city)) — built, never hand-edited.
_ZONES, _ZONE_REGISTRY_WARNINGS = _build_zones(_ZONE_REGISTRY)


def _token():
    """ENTSO-E security token from env. Several name variants so it works
    however the operator saved it on Railway. None if unset (→ LIVE-only no-op)."""
    return (os.environ.get("ENTSOE_API_Token")
            or os.environ.get("ENTSOE_API_KEY")   # r-entso-fix (2026-06-25): the name the operator set on Railway + Render
            or os.environ.get("ENTSOE_TOKEN")
            or os.environ.get("ENTSOE_API_TOKEN")
            or os.environ.get("ENTSOE_SECURITY_TOKEN")
            or os.environ.get("ENTSO_E_TOKEN")
            or "").strip()


def _dsn():
    return os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL") or ""


@contextmanager
def _conn():
    c = _pg.connect(_dsn())
    try:
        yield c
    finally:
        c.close()


def _ln(tag):
    """Local-name of a possibly-namespaced XML tag."""
    return tag.rsplit("}", 1)[-1]


def _parse_ts_utc(s):
    """Tolerant ENTSO-E timestamp parse ('2026-08-07T22:00Z') → aware UTC
    datetime, or None. Never raises."""
    if not s:
        return None
    raw = str(s).strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        d = datetime.datetime.fromisoformat(raw)
    except Exception:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=datetime.timezone.utc)
    return d.astimezone(datetime.timezone.utc)


# ENTSO-E resolutions seen on A75. PT60M and PT1H are the same thing spelled
# two ways; both appear in the wild.
_RESOLUTION_S = {
    "PT1M": 60, "PT5M": 300, "PT10M": 600, "PT15M": 900, "PT30M": 1800,
    "PT60M": 3600, "PT1H": 3600, "P1D": 86400, "P7D": 604800, "P1M": 2592000,
}


def _resolution_seconds(text):
    return _RESOLUTION_S.get((text or "").strip().upper())


def _period_latest_point(period_el):
    """★ r-entsoe-period (2026-08-08). The latest Point of ONE <Period>, with
    the instant it covers.

    THE BUG THIS REPLACES: the caller used to walk `ts.iter()` — every Point in
    every Period of the TimeSeries at once — and keep the largest `position`.
    ENTSO-E numbers positions RELATIVE TO EACH PERIOD, restarting at 1 in every
    one. So for a document split into multiple Periods (which is exactly what
    happens across a resolution change or a DST boundary, and what a 5-hour
    query window invites), the winner was whichever Period simply had the most
    points — frequently an EARLIER one. The reading was then published as the
    "latest settled period" with no timestamp to contradict it.

    Returns (point_end_utc | None, quantity | None). point_end is the END of
    the interval the point covers: period start + position * resolution, which
    is the instant the measurement closed.
    """
    start = end = None
    res_s = None
    for el in period_el:
        ln = _ln(el.tag)
        if ln == "timeInterval":
            for t in el:
                tln = _ln(t.tag)
                if tln == "start":
                    start = _parse_ts_utc(t.text)
                elif tln == "end":
                    end = _parse_ts_utc(t.text)
        elif ln == "resolution":
            res_s = _resolution_seconds(el.text)
    best_pos, best_qty = -1, None
    for pt in period_el:
        if _ln(pt.tag) != "Point":
            continue
        pos = qty = None
        for c in list(pt):
            cln = _ln(c.tag)
            if cln == "position":
                try: pos = int((c.text or "").strip())
                except Exception: pos = None
            elif cln == "quantity":
                try: qty = float((c.text or "").strip())
                except Exception: qty = None
        # Positions are period-local, so this comparison is now correct: it
        # only ever runs WITHIN one Period.
        if pos is not None and qty is not None and pos > best_pos:
            best_pos, best_qty = pos, qty
    if best_qty is None:
        return None, None
    pt_end = None
    if start is not None and res_s:
        pt_end = start + datetime.timedelta(seconds=res_s * best_pos)
        if end is not None and pt_end > end:
            pt_end = end          # never claim past the period's own end
    elif end is not None:
        pt_end = end              # no resolution: the period end is the best we know
    return pt_end, best_qty


def _parse_generation_xml(xml_text):
    """ENTSO-E A75 GL_MarketDocument → {"fuels": {category: mw},
    "period_end": iso|None, "period_end_newest": iso|None} for the LATEST
    settled period. Returns None on parse failure or an Acknowledgement
    (no-data / error) document. Sums multiple TimeSeries of the same fuel
    (e.g. wind on/offshore) and skips consumption legs (outBiddingZone).

    The timestamps are returned BESIDE the fuel map, never inside it — a
    consumer that iterates the fuels must not trip over a string.

    r-entsoe-period (2026-08-08): the latest Point is now chosen PER PERIOD and
    the latest Period wins — see _period_latest_point for why the old
    max-position-across-Periods scan picked the wrong point. The chosen
    instants are returned so the reading can finally carry a data timestamp
    instead of only the age of our own fetch.
    """
    try:
        root = ET.fromstring(xml_text)
    except Exception:
        return None
    if _ln(root.tag).startswith("Acknowledgement"):
        return None  # ENTSO-E returns an Acknowledgement doc on no-data / errors
    cats = {}
    found_any = False
    chosen_ends = []
    for ts in root.iter():
        if _ln(ts.tag) != "TimeSeries":
            continue
        psr = None
        is_consumption = False
        for el in ts.iter():
            ln = _ln(el.tag)
            if ln == "psrType" and not psr:
                psr = (el.text or "").strip()
            elif ln == "outBiddingZone_Domain.mRID":
                is_consumption = True  # storage consumption leg — skip
        if not psr or is_consumption:
            continue
        # Walk this TimeSeries' Periods and keep the LATEST one that has a
        # usable point. Ranking is by the point's own end instant; a Period
        # whose timestamps do not parse falls back to document order (the last
        # such Period wins), which is still strictly better than comparing
        # period-local positions across Periods.
        best_end, best_qty = None, None
        for per in ts.iter():
            if _ln(per.tag) != "Period":
                continue
            p_end, p_qty = _period_latest_point(per)
            if p_qty is None:
                continue
            if best_qty is None:
                best_end, best_qty = p_end, p_qty
            elif p_end is not None and best_end is not None:
                if p_end > best_end:
                    best_end, best_qty = p_end, p_qty
            elif p_end is not None and best_end is None:
                best_end, best_qty = p_end, p_qty
            elif p_end is None and best_end is None:
                best_end, best_qty = p_end, p_qty   # document order
        if best_qty is None:
            continue
        cat = _PSR.get(psr, "other")
        cats[cat] = cats.get(cat, 0.0) + best_qty
        found_any = True
        if best_end is not None:
            chosen_ends.append(best_end)
    if not found_any:
        return None
    # The mix is only as current as its STALEST component, so the age a
    # consumer should judge it on is the OLDEST chosen instant. The newest is
    # published too, so a spread between fuels is visible rather than hidden
    # behind one number.
    return {
        "fuels": cats,
        "period_end": min(chosen_ends).isoformat() if chosen_ends else None,
        "period_end_newest": max(chosen_ends).isoformat() if chosen_ends else None,
    }


# Per-zone result cache. _SNAP_CACHE below collapses concurrent CALLERS into
# one fan-out; this one stops a fan-out from re-fetching a zone that answered
# moments ago, so registry growth adds zones WITHOUT adding a proportional
# upstream call rate.
#
# RATE MATH (why 900s): the worst case is one full fan-out per _SNAP_TTL, i.e.
# len(_ZONES) calls / 300s / gunicorn worker / replica (_SNAP_CACHE is a plain
# module dict — per PROCESS, never shared). At 33 zones that is 33 calls per
# 5 min per process; with _ZONE_TTL=900 only every third fan-out actually goes
# upstream, so steady state is ~33 calls / 15 min / process ≈ 2.2 req/min —
# inside the LOWER of the two rate limits this repo cites (100 req/min).
#
# 900s is also honest as DATA: ENTSO-E publishes A75 with a ~1-2h lag, so a
# ≤15-min-old reading is well inside the source's own resolution, and every
# zone carries observed_age_s so a consumer can see exactly how old it is.
# It is deliberately NOT the 6h used by routes/international_ingestion.py:159
# — that module is unreferenced dead code, and 6h-old fuel mix must never be
# served as "live".
_ZONE_CACHE = {}
_ZONE_TTL = 900

OBSERVED_AGE_BASIS = (
    "observed_age_s is how long ago DC HUB FETCHED this reading (0 = fetched "
    "on this call). It is NOT the age of the data. Judge freshness on "
    "data_age_s, which is measured from data_period_end — the instant the "
    "ENTSO-E A75 reading itself covers. ENTSO-E publishes A75 with a ~1-2h "
    "lag, so data_age_s is normally hours even when observed_age_s is 0.")


def _with_ages(snap, observed_age_s, now=None):
    """PURE. Attach both age readings to a zone snapshot.

    r-entsoe-age (2026-08-08): every zone shipped observed_age_s alone, and on
    a fresh fetch that is 0 — which a consumer reads as "this instant" for a
    feed the source itself lags 1-2 hours. data_age_s is derived from the
    reading's OWN period end, and is None (never 0) when the document carried
    no parseable timestamp, so "unknown age" can never be mistaken for "now".
    """
    out = dict(snap)
    out["observed_age_s"] = observed_age_s
    out["observed_age_basis"] = OBSERVED_AGE_BASIS
    end = _parse_ts_utc(out.get("data_period_end"))
    if end is None:
        out["data_age_s"] = None
        out["data_age_unknown_reason"] = (
            "the A75 document carried no parseable Period timeInterval, so the "
            "reading's own instant is unknown. Do NOT read this as fresh.")
    else:
        ref = now if now is not None else datetime.datetime.now(datetime.timezone.utc)
        out["data_age_s"] = max(0, int((ref - end).total_seconds()))
    return out


def _zone_snapshot(code, max_age=None):
    """Live fuel-mix snapshot for one EIC zone, or None. No fabrication.

    Reuses a cached reading younger than `max_age` (default _ZONE_TTL) instead
    of re-calling ENTSO-E.

    ★ r-entsoe-age (2026-08-08). Every zone used to ship observed_age_s and
    NOTHING else — and observed_age_s measures how long ago WE FETCHED, not how
    old the DATA is. On a fresh fetch it is 0, so all 33 bidding zones (33 of
    the 47 ranked on the grid scoreboard) published `observed_age_s: 0` against
    an A75 feed ENTSO-E itself lags by 1-2 hours. Zero read as "this instant".
    The row now carries data_period_end (the instant the reading covers) and
    data_age_s derived from it, and observed_age_s keeps its own meaning with
    observed_age_basis saying so in words."""
    hit = _ZONE_CACHE.get(code)
    if hit:
        age = time.time() - hit["ts"]
        if 0 <= age < (_ZONE_TTL if max_age is None else max_age):
            return _with_ages(hit["snap"], observed_age_s=int(age))
    token = _token()
    if not token or _rq is None:
        return None
    eic = _ZONES[code][0]
    now = datetime.datetime.utcnow()
    frm = (now - datetime.timedelta(hours=5)).strftime("%Y%m%d%H00")
    to = now.strftime("%Y%m%d%H00")
    try:
        r = _rq.get(_ENTSOE_BASE, params={
            "securityToken": token,
            "documentType": "A75",
            "processType": "A16",
            "in_Domain": eic,
            "periodStart": frm,
            "periodEnd": to,
        }, timeout=15)
        if not r.ok:
            return None
        parsed = _parse_generation_xml(r.text)
    except Exception:
        return None
    if not parsed:
        return None
    cats = parsed.get("fuels") or {}
    total = sum(v for v in cats.values() if v and v > 0)
    if total <= 0:
        return None
    renew = sum(cats.get(c, 0.0) for c in _RENEWABLE_CATS)
    gas = cats.get("gas", 0.0)
    name, city = _ZONES[code][1], _ZONES[code][2]
    snap = {
        "code": code, "name": name, "hub": city,
        # The instant the reading covers, straight from the A75 Period we
        # selected — NOT when we fetched it. None only when the document
        # carried no parseable timeInterval, and then data_age_s is None too
        # (never 0, which would read as "now").
        "data_period_end": parsed.get("period_end"),
        "data_period_end_newest": parsed.get("period_end_newest"),
        "generation_total_mw": round(total, 0),
        "fuel_gas_mw": round(gas, 0),
        "fuel_nuclear_mw": round(cats.get("nuclear", 0.0), 0),
        "fuel_coal_mw": round(cats.get("coal", 0.0), 0),
        "fuel_wind_mw": round(cats.get("wind", 0.0), 0),
        "fuel_solar_mw": round(cats.get("solar", 0.0), 0),
        "fuel_hydro_mw": round(cats.get("hydro", 0.0), 0),
        "fuel_biomass_mw": round(cats.get("biomass", 0.0), 0),
        "renewable_pct": round(100.0 * renew / total, 1),   # wind+solar+hydro (scoreboard-comparable)
        "gas_pct": round(100.0 * gas / total, 1),
    }
    _ZONE_CACHE[code] = {"snap": snap, "ts": time.time()}
    return _with_ages(snap, observed_age_s=0)


# Short in-process cache so the scoreboard + map + direct callers share ONE
# fan-out instead of each re-running the whole registry. 5-min TTL — well
# fresh for grid data (ENTSO-E itself lags ~1-2h); the per-zone _ZONE_CACHE
# above is what actually bounds the upstream call rate as the registry grows.
_SNAP_CACHE = {"data": None, "ts": 0.0}
_SNAP_TTL = 300

# Per-zone outcome of the LAST fan-out: {zone_code: reason}. Populated by
# _live_snapshot and surfaced on /snapshot so a configured-but-not-ranking
# zone is visible instead of silently absent. Reset each fan-out, so it
# describes the current cache generation, not history.
_ZONE_ERRORS = {}


def _live_snapshot():
    """Aggregate EU snapshot across all reachable zones + per-zone detail.
    Fans the registry out in PARALLEL (sequential was 12-24s — long enough to
    blow the scoreboard's edge/tool timeout) and caches for 5 min. None only if
    the token is unset or EVERY zone failed (LIVE-only). A zone that fails is
    dropped, so len(zones) <= len(_ZONES); callers must report both counts."""
    if not _token():
        return None
    now = time.time()
    if _SNAP_CACHE["data"] is not None and (now - _SNAP_CACHE["ts"]) < _SNAP_TTL:
        return _SNAP_CACHE["data"]
    zones = {}
    zone_errors = {}
    # ws2-merged (2026-07-29): width scales with the registry so adding zones
    # adds PARALLELISM, not more sequential waves. Capped at 24 so one process
    # never opens an unbounded socket burst; with _ZONE_CACHE most zones cost
    # no socket at all.
    with ThreadPoolExecutor(max_workers=min(len(_ZONES), 24)) as pool:
        futs = {pool.submit(_zone_snapshot, code): code for code in _ZONES}
        try:
            for fut in as_completed(futs, timeout=25):
                code = futs[fut]
                try:
                    snap = fut.result(timeout=16)
                    if snap:
                        zones[code] = snap
                    else:
                        # A bad EIC does NOT raise — _zone_snapshot returns None
                        # and this branch used to be an unconditional `pass`.
                        # That is exactly how BG (10YCA-BULGARIA-R) went missing:
                        # configured but never ranked, no error anywhere.
                        zone_errors[code] = ("no_data — bad EIC, no A75 publication "
                                             "for the window, or an Acknowledgement doc")
                except Exception as e:
                    zone_errors[code] = f"{type(e).__name__}: {str(e)[:120]}"
        except Exception as e:
            # as_completed raises TimeoutError FROM THE GENERATOR when the 25s
            # budget expires; the inner except only covers fut.result(), so it
            # escaped http_snapshot() as a 500 on the exact endpoint the grid
            # scoreboard reads. Degrade to the zones collected so far.
            for _c in futs.values():
                zone_errors.setdefault(_c, f"fanout_budget_exceeded:{type(e).__name__}")
    for _c in _ZONES:
        if _c not in zones:
            zone_errors.setdefault(_c, "no_data — zone did not report this cycle")
    _ZONE_ERRORS.clear()
    _ZONE_ERRORS.update({k: v for k, v in zone_errors.items() if k not in zones})
    if not zones:
        return None
    agg_total = sum(z["generation_total_mw"] for z in zones.values())
    agg_renew = sum((z["fuel_wind_mw"] + z["fuel_solar_mw"] + z["fuel_hydro_mw"]) for z in zones.values())
    agg_gas = sum(z["fuel_gas_mw"] for z in zones.values())
    metrics = {
        "generation_total_mw": {"value": round(agg_total, 0), "unit": "MW"},
        "fuel_gas_mw": {"value": round(agg_gas, 0), "unit": "MW"},
        "fuel_wind_mw": {"value": round(sum(z["fuel_wind_mw"] for z in zones.values()), 0), "unit": "MW"},
        "fuel_solar_mw": {"value": round(sum(z["fuel_solar_mw"] for z in zones.values()), 0), "unit": "MW"},
        "fuel_hydro_mw": {"value": round(sum(z["fuel_hydro_mw"] for z in zones.values()), 0), "unit": "MW"},
        "fuel_nuclear_mw": {"value": round(sum(z["fuel_nuclear_mw"] for z in zones.values()), 0), "unit": "MW"},
        "renewable_pct": {"value": round(100.0 * agg_renew / agg_total, 1) if agg_total else 0, "unit": "pct"},
        "gas_pct": {"value": round(100.0 * agg_gas / agg_total, 1) if agg_total else 0, "unit": "pct"},
        "active_zones": {"value": len(zones), "unit": "count"},
    }
    result = {"metrics": metrics, "zones": zones}
    _SNAP_CACHE["data"] = result
    _SNAP_CACHE["ts"] = now
    return result


def _persist_metrics(snap):
    """Persist the EU aggregate (iso=ENTSOE) + each zone (iso=EU_<code>)."""
    if not snap:
        return 0
    rows = 0
    with _conn() as c, c.cursor() as cur:
        def _ins(iso, name, value, unit):
            nonlocal rows
            try:
                cur.execute(
                    """INSERT INTO grid_data (iso, metric_name, metric_value, unit)
                       VALUES (%s, %s, %s, %s)
                       ON CONFLICT (iso, timestamp, metric_name) DO NOTHING""",
                    (iso, name, value, unit))
                if cur.rowcount > 0:
                    rows += 1
            except Exception:
                note_swallowed_write("grid_data", where="iso_eu_entsoe._ins")
                pass
        for name, data in snap["metrics"].items():
            _ins(ISO_CODE, name, data["value"], data.get("unit", ""))
        for code, z in snap["zones"].items():
            zi = f"EU_{code}"
            _ins(zi, "generation_total_mw", z["generation_total_mw"], "MW")
            _ins(zi, "renewable_pct", z["renewable_pct"], "pct")
            _ins(zi, "gas_pct", z["gas_pct"], "pct")
        c.commit()
    return rows


def run_extraction():
    started = time.time()
    summary = {
        "iso": ISO_CODE, "method": "live_entsoe_a75",
        "metrics_extracted": 0, "rows_inserted": 0, "errors": [],
        "source": "ENTSO-E Transparency (A75 actual generation per type)",
    }
    try:
        if not _token():
            summary["errors"].append("entsoe_token_missing — set ENTSOE_API_Token on Railway (wrote nothing, no modeled fallback)")
            summary["elapsed_ms"] = int((time.time() - started) * 1000)
            return summary
        snap = _live_snapshot()
        if snap is None:
            summary["errors"].append("entsoe_live_fetch_failed — all zones unreachable/empty (wrote nothing)")
        else:
            summary["metrics_extracted"] = len(snap["metrics"])
            summary["rows_inserted"] = _persist_metrics(snap)
            # Two readings of "how many zones", named. They differ whenever a
            # zone's call fails (it is dropped silently) — never collapse them.
            summary["zones_configured"] = len(_ZONES)
            summary["active_zones"] = len(snap["zones"])
            summary["zones_live"] = sorted(snap["zones"].keys())
            summary["zones_missing"] = sorted(set(_ZONES) - set(snap["zones"]))
            summary["snapshot"] = {k: v["value"] for k, v in snap["metrics"].items()}
    except Exception as e:
        summary["errors"].append(f"{type(e).__name__}: {e}")
    summary["elapsed_ms"] = int((time.time() - started) * 1000)
    return summary


def compute_dcpi_score():
    return {
        "iso": ISO_CODE, "code": "eu", "name": "Europe (ENTSO-E)",
        "region": "eu", "composite_score": 73.0, "verdict": "CAUTION",
        "rank_factors": {
            "cheap_power": 40,      # among the most expensive wholesale power globally
            "renewable_mix": 80,    # very high wind+solar+hydro across the zones
            "headroom": 50,         # tight in DE/NL/IE; surplus in Nordics/Iberia
            "policy_support": 78,   # strong decarbonization + DC investment, planning friction
            "fiber_density": 88,    # FLAP-D (Frankfurt/London/Amsterdam/Paris/Dublin) = top hubs
            "climate_risk": 84,
            "water_avail": 76,
        },
        "advantages": [
            "FLAP-D markets (Frankfurt, Amsterdam, Paris, Dublin) are the European DC core",
            "High renewable share across most bidding zones",
            "Live grid data via ENTSO-E Transparency (one token, every zone in _ZONE_REGISTRY)",
        ],
        "constraints": [
            "Among the most expensive wholesale electricity globally",
            "Grid-connection moratoria/constraints in Amsterdam + Dublin",
        ],
        "source": "ENTSO-E Transparency Platform (live actual generation per type)",
    }


# ── HTTP endpoints ──────────────────────────────────────────────────────────
# AUTO-REPAIR: duplicate route '/run' also in enhanced_promotion.py:831 — review and remove one
@iso_eu_entsoe_bp.route("/run", methods=["POST", "GET"])
def http_run():
    return jsonify(run_extraction()), 200

# AUTO-REPAIR: duplicate route '/snapshot' also in routes/iso_lmp_ingest.py:707 — review and remove one

@iso_eu_entsoe_bp.route("/snapshot", methods=["GET"])
def http_snapshot():
    if not _token():
        return jsonify({"iso": ISO_CODE, "error": "entsoe_token_missing",
                        "hint": "Set ENTSOE_API_Token in Railway env on the backend service."}), 503
    try:
        snap = _live_snapshot()
    except Exception as e:
        # This endpoint feeds the grid scoreboard — it must degrade, never 500.
        return jsonify({"iso": ISO_CODE, "error": "entsoe_live_unavailable",
                        "reason": f"fanout_failed:{type(e).__name__}"}), 503
    if snap is None:
        return jsonify({"iso": ISO_CODE, "error": "entsoe_live_unavailable",
                        "reason": "no_zone_answered"}), 503
    from routes.tier_gate import jsonify_gated_snapshot
    _missing = {k: v for k, v in _ZONE_ERRORS.items() if k not in snap["zones"]}
    # HONEST NUMBERS: BOTH readings, named. zones_configured / zones_live are
    # TOP-LEVEL scalars because dchub-mcp-server/server.mjs reads
    # `eu.zones_configured` directly; zone_coverage is the structured form and
    # zones_missing carries the per-zone reason. None of these four keys is in
    # routes/tier_gate.py:272 _INTL_SNAPSHOT_REDACT, so anonymous callers get
    # the counts but still no zone detail.
    return jsonify_gated_snapshot({"iso": ISO_CODE, "live": True,
                    "metrics": {k: v["value"] for k, v in snap["metrics"].items()},
                    "zones": snap["zones"],
                    "zones_configured": len(_ZONES),
                    "zones_live": len(snap["zones"]),
                    "zones_missing": _missing,
                    "zone_coverage": {"configured": len(_ZONES),
                                      "returned": len(snap["zones"]),
                                      "missing": sorted(set(_ZONES) - set(snap["zones"]))},
                    "source": "ENTSO-E Transparency (A75)"}, 200)


@iso_eu_entsoe_bp.route("/zones", methods=["GET"])
def http_zones():
    return jsonify({"iso": ISO_CODE,
                    "zones": {k: {"eic": v[0], "name": v[1], "hub": v[2]} for k, v in _ZONES.items()},
                    "count": len(_ZONES),
                    "count_basis": "rows accepted from _ZONE_REGISTRY — CONFIGURED, not live; a zone appears on the scoreboard only if its call answered",
                    "registry_rows": len(_ZONE_REGISTRY),
                    "registry_warnings": _ZONE_REGISTRY_WARNINGS}), 200
# AUTO-REPAIR: duplicate route '/dcpi-score' also in routes/iso_uk_elexon.py:254 — review and remove one


@iso_eu_entsoe_bp.route("/dcpi-score", methods=["GET"])
def http_dcpi_score():
# AUTO-REPAIR: duplicate route '/health' also in main.py:7752 — review and remove one
    return jsonify(compute_dcpi_score()), 200


@iso_eu_entsoe_bp.route("/health", methods=["GET"])
def http_health():
    tok = bool(_token())
    probe = _zone_snapshot("DE_LU") if tok else None
    return jsonify({"iso": ISO_CODE, "token_configured": tok,
                    "live_feed_ok": probe is not None,
                    # Basis for live_feed_ok: 0 = fetched on this request,
                    # >0 = age (s) of the reused DE_LU reading, null = no probe.
                    # This probe used to be UNCACHED on every single hit.
                    "probe_age_s": (probe or {}).get("observed_age_s"),
                    # r-entsoe-age: the DATA's own age, which is the one that
                    # matters — probe_age_s above is only our fetch age.
                    "probe_data_age_s": (probe or {}).get("data_age_s"),
                    "probe_data_period_end": (probe or {}).get("data_period_end"),
                    "zones_configured": len(_ZONES),
                    "registry_warnings": _ZONE_REGISTRY_WARNINGS,
                    "source": "entsoe_transparency"}), 200


@iso_eu_entsoe_bp.route("/debug", methods=["GET"])
def http_debug():
    """Diagnostic for verifying XML parsing post-deploy WITHOUT leaking the
    token. ?zone=DE_LU — returns HTTP status, a truncated raw-XML head, and the
    parsed fuel cats so a bad EIC / parse mismatch is visible. Token never echoed."""
    zone = (request.args.get("zone") or "DE_LU").upper()
    if zone not in _ZONES:
        return jsonify({"error": "unknown_zone", "valid": sorted(_ZONES.keys())}), 400
    if not _token():
        return jsonify({"error": "entsoe_token_missing"}), 503
    eic = _ZONES[zone][0]
    now = datetime.datetime.utcnow()
    out = {"zone": zone, "eic": eic}
    try:
        r = _rq.get(_ENTSOE_BASE, params={
            "securityToken": _token(), "documentType": "A75", "processType": "A16",
            "in_Domain": eic,
            "periodStart": (now - datetime.timedelta(hours=5)).strftime("%Y%m%d%H00"),
            "periodEnd": now.strftime("%Y%m%d%H00"),
        }, timeout=15)
        out["http_status"] = r.status_code
        out["xml_head"] = (r.text or "")[:600]
        out["parsed"] = _parse_generation_xml(r.text)
        out["zone_snapshot"] = _zone_snapshot(zone)
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
    return jsonify(out), 200
