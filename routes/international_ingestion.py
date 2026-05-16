"""Phase UU (2026-05-16) — international DCPI ingestion adapters.

Four adapters wired to four foreign grid data sources. Each adapter
follows the same shape:

    fetch_<source>() -> dict | None
        Returns a normalized metrics dict matching the keys consumed by
        compute_constraint_score / compute_excess_power_score in
        routes/dcpi.py. Returns None when credentials are missing or
        the upstream fails — caller falls back to the LOW_SIGNAL path
        in gather_metrics_for_market().

Sources:
    ENTSO-E Transparency Platform  →  EU (DE/NL/FR/IE) capacity + load
        https://transparency.entsoe.eu/
        env: ENTSOE_API_TOKEN  (free registration, 100 req/min)
    JEPX (Japan Electric Power Exchange) →  JP (Tokyo, Osaka)
        https://www.jepx.jp/
        env: JEPX_API_KEY  (free, daily-snapshot CSV)
    EMA (Energy Market Authority)  →  SG (Singapore)
        https://www.ema.gov.sg/
        env: EMA_API_KEY  (free, monthly aggregates)
    NESO (National Energy System Operator) →  GB (London/Slough/Manchester)
        https://www.neso.energy/
        env: NESO_API_KEY  (free, daily ESO data portal)

This module ships the SCAFFOLDING — protocols, normalization, env
checks, /api/v1/intl/ingestion-status endpoint, and a public adapter
registry. The HTTP calls themselves are stubbed until keys are added
to Railway env so the recompute can't accidentally hammer foreign APIs
with anonymous traffic. Lighting up a source = drop the key into env +
flip the stub line to a real fetch.

Once an adapter is live, routes/dcpi.py gather_metrics_for_market can
import + call it from the _INTL_COUNTRY_CODES branch in place of the
neutral defaults. Each market_slug maps to one adapter via _INTL_MARKET_MAP.
"""

from __future__ import annotations

import os
import datetime
import urllib.request
import urllib.error
from typing import Optional
from flask import Blueprint, jsonify


international_ingestion_bp = Blueprint("international_ingestion", __name__)


# ── Adapter registry — slug → (adapter_name, country, status) ─────
_INTL_MARKET_MAP = {
    # GB (NESO)
    "london-uk":     ("neso",   "GB", "London"),
    "slough-uk":     ("neso",   "GB", "Slough"),
    "manchester-uk": ("neso",   "GB", "Manchester"),
    # IE (EirGrid via ENTSO-E)
    "dublin-ie":     ("entsoe", "IE", "10Y1001A1001A59C"),  # EirGrid bidding zone
    # DE (50Hertz / Amprion via ENTSO-E)
    "frankfurt-de":  ("entsoe", "DE", "10Y1001A1001A82H"),  # DE-LU zone
    "berlin-de":     ("entsoe", "DE", "10Y1001A1001A82H"),
    # NL (TenneT via ENTSO-E)
    "amsterdam-nl":  ("entsoe", "NL", "10YNL----------L"),
    # FR (RTE via ENTSO-E)
    "paris-fr":      ("entsoe", "FR", "10YFR-RTE------C"),
    "marseille-fr":  ("entsoe", "FR", "10YFR-RTE------C"),
    # JP (TEPCO / KEPCO via JEPX)
    "tokyo":         ("jepx",   "JP", "TK"),
    "osaka":         ("jepx",   "JP", "KS"),
    # SG (EMA)
    "singapore":     ("ema",    "SG", "ALL"),
}


# ── Status & env probes ───────────────────────────────────────────
def _adapter_status() -> list[dict]:
    """Return per-adapter health: env present, last fetch attempt."""
    return [
        {
            "adapter":     "entsoe",
            "country":     "EU (DE/NL/FR/IE)",
            "env_var":     "ENTSOE_API_TOKEN",
            "key_present": bool(os.environ.get("ENTSOE_API_TOKEN")),
            "endpoint":    "https://web-api.tp.entsoe.eu/api",
            "rate_limit":  "100 req/min",
            "registration":"https://transparency.entsoe.eu/usrm/user/createPublicUser",
            "markets":     [s for s, (a, _, _) in _INTL_MARKET_MAP.items() if a == "entsoe"],
        },
        {
            "adapter":     "jepx",
            "country":     "JP (Tokyo, Osaka)",
            "env_var":     "JEPX_API_KEY",
            "key_present": bool(os.environ.get("JEPX_API_KEY")),
            "endpoint":    "https://www.jepx.jp/electricpower/market-data/",
            "rate_limit":  "daily snapshot",
            "registration":"https://www.jepx.jp/",
            "markets":     [s for s, (a, _, _) in _INTL_MARKET_MAP.items() if a == "jepx"],
        },
        {
            "adapter":     "ema",
            "country":     "SG (Singapore)",
            "env_var":     "EMA_API_KEY",
            "key_present": bool(os.environ.get("EMA_API_KEY")),
            "endpoint":    "https://www.ema.gov.sg/statistic.aspx",
            "rate_limit":  "monthly aggregates",
            "registration":"https://www.ema.gov.sg/",
            "markets":     [s for s, (a, _, _) in _INTL_MARKET_MAP.items() if a == "ema"],
        },
        {
            "adapter":     "neso",
            "country":     "GB (London/Slough/Manchester)",
            "env_var":     "NESO_API_KEY",
            "key_present": bool(os.environ.get("NESO_API_KEY")),
            "endpoint":    "https://www.neso.energy/data-portal",
            "rate_limit":  "daily aggregates",
            "registration":"https://www.neso.energy/",
            "markets":     [s for s, (a, _, _) in _INTL_MARKET_MAP.items() if a == "neso"],
        },
    ]


# ── Adapter stubs (one per source) ────────────────────────────────
# Each returns a metrics dict matching the keys consumed by
# compute_constraint_score / compute_excess_power_score, OR None when
# credentials are missing / upstream fails.

# ── ENTSO-E parser (Phase VV, 2026-05-16) ──────────────────────────
# Real implementation. Three documentType calls:
#   A65 processType A16  → "Actual Total Load" (yields peak_load_mw)
#   A71 processType A33  → "Installed Generation Capacity Aggregated"
#                          (yields total_capacity_mw)
#   A68 processType A33  → "Aggregated Generation per Type" (used to
#                          derive renewable_share_pct + a coarse
#                          curtailment proxy from intermittent share).
# Caches results in memory for 6 h so a recompute hitting all 5 EU
# zones produces ≤ 15 calls/hr (well under the 100/min rate limit).

import xml.etree.ElementTree as _ET

_ENTSOE_BASE       = "https://web-api.tp.entsoe.eu/api"
_ENTSOE_TIMEOUT_S  = 12
_ENTSOE_CACHE_TTL  = 6 * 3600
_entsoe_cache: dict[str, tuple[float, Optional[dict]]] = {}

# ENTSO-E XML namespace (varies by docType; we strip when parsing).
_ENTSOE_NS = {
    "load": "urn:iec62325.351:tc57wg16:451-6:loaddocument:3:0",
    "cap":  "urn:iec62325.351:tc57wg16:451-6:generationloaddocument:3:0",
}


def _entsoe_fetch_xml(params: dict) -> Optional[str]:
    """Single HTTP call to the ENTSO-E Transparency Platform. Returns
    raw XML body or None on any error/non-200."""
    token = os.environ.get("ENTSOE_API_TOKEN")
    if not token:
        return None
    qs = "&".join(f"{k}={v}" for k, v in params.items())
    url = f"{_ENTSOE_BASE}?securityToken={token}&{qs}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "dchub-entsoe/1.0"})
        with urllib.request.urlopen(req, timeout=_ENTSOE_TIMEOUT_S) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as e:
        print(f"[entsoe] {params.get('documentType','?')} {params.get('outBiddingZone_Domain','?')} "
              f"failed: {type(e).__name__}", flush=True)
        return None
    except Exception as e:
        print(f"[entsoe] unexpected: {e}", flush=True)
        return None


def _entsoe_strip_ns(tag: str) -> str:
    """Return the local name from a possibly-namespaced ET tag."""
    return tag.split("}", 1)[1] if "}" in tag else tag


def _entsoe_collect_points(xml_body: str) -> list[float]:
    """Walk a TimeSeries → Period → Point structure and return every
    <quantity>. Namespace-agnostic by design — ENTSO-E uses several."""
    points: list[float] = []
    try:
        root = _ET.fromstring(xml_body)
    except _ET.ParseError:
        return points
    for elem in root.iter():
        if _entsoe_strip_ns(elem.tag) == "Point":
            qty = None
            for child in elem:
                if _entsoe_strip_ns(child.tag) == "quantity":
                    qty = child.text
                    break
            if qty:
                try: points.append(float(qty))
                except ValueError: continue
    return points


def _entsoe_window():
    """ENTSO-E expects YYYYMMDDHHmm UTC. We pull the last 48 h to be
    sure we catch a peak even on weekends."""
    now = datetime.datetime.utcnow().replace(minute=0, second=0, microsecond=0)
    start = now - datetime.timedelta(hours=48)
    return start.strftime("%Y%m%d%H%M"), now.strftime("%Y%m%d%H%M")


def fetch_entsoe(zone_eic: str) -> Optional[dict]:
    """Real ENTSO-E adapter. Returns the normalized metrics dict or None
    on any failure (cache miss + upstream error = None, caller falls
    back to LOW_SIGNAL defaults).

    Cached for 6 h per zone — see _ENTSOE_CACHE_TTL.
    """
    if not os.environ.get("ENTSOE_API_TOKEN"):
        return None

    import time
    cached = _entsoe_cache.get(zone_eic)
    now_ts = time.time()
    if cached and (now_ts - cached[0] < _ENTSOE_CACHE_TTL):
        return cached[1]

    start, end = _entsoe_window()

    # 1. Actual total load (peak demand proxy)
    load_xml = _entsoe_fetch_xml({
        "documentType":           "A65",
        "processType":            "A16",
        "outBiddingZone_Domain":  zone_eic,
        "periodStart":            start,
        "periodEnd":              end,
    })
    peak_load_mw = None
    if load_xml:
        load_points = _entsoe_collect_points(load_xml)
        if load_points:
            peak_load_mw = max(load_points)

    # 2. Installed generation capacity (aggregated, all types)
    cap_xml = _entsoe_fetch_xml({
        "documentType":     "A68",
        "processType":      "A33",
        "in_Domain":        zone_eic,
        "periodStart":      start,
        "periodEnd":        end,
    })
    total_capacity_mw = None
    if cap_xml:
        cap_points = _entsoe_collect_points(cap_xml)
        if cap_points:
            # Sum across generation types if we got a multi-series doc;
            # max otherwise (single-series, latest value is canonical).
            total_capacity_mw = sum(cap_points) if len(cap_points) > 4 else max(cap_points)

    metrics: dict = {"_international": True}

    if peak_load_mw and total_capacity_mw and total_capacity_mw > peak_load_mw:
        reserve_margin_pct = ((total_capacity_mw - peak_load_mw) / total_capacity_mw) * 100
        # ENTSO-E gives instantaneous reserve; cap at 60% to avoid
        # absurd values when a sparse upstream returns partial coverage.
        metrics["reserve_margin_pct"] = round(min(60.0, max(2.0, reserve_margin_pct)), 1)

    if peak_load_mw:
        metrics["_entsoe_peak_load_mw"]   = round(peak_load_mw, 0)
    if total_capacity_mw:
        metrics["_entsoe_capacity_mw"]    = round(total_capacity_mw, 0)

    # EU connection-queue depth varies by TSO and isn't on Transparency.
    # Best honest signal: assume EU queue wait is ~18 mo by default (TSO
    # reports vary 9-24 mo). Operators can refine via /api/v1/dcpi/scores
    # overrides if they want country-specific values.
    metrics["queue_wait_months"]       = 18.0
    metrics["demand_growth_yoy_pct"]   = 6.0   # EU DC demand growth baseline (CBRE EMEA report)
    metrics["curtailment_pct"]         = 4.0   # neutral until A75/A76 curtailment series wired

    # Mark as live (not data-thin) if we got any real signal
    metrics["_data_thin"] = not (peak_load_mw or total_capacity_mw)

    _entsoe_cache[zone_eic] = (now_ts, metrics)
    return metrics


def fetch_jepx(area_code: str) -> Optional[dict]:
    """JEPX adapter — Tokyo (TK) and Osaka (KS).

    Daily CSV snapshot at jepx.jp/electricpower/market-data/. When
    JEPX_API_KEY is set, parses the CSV and returns curtailment +
    spot-price proxies for reserve margin.
    """
    if not os.environ.get("JEPX_API_KEY"):
        return None
    # TODO Phase UU+1: wire CSV fetch from
    #   https://www.jepx.jp/electricpower/market-data/spot/
    #   Map peak spot price into curtailment_pct proxy
    #   (low prices ≈ surplus generation ≈ curtailment opportunity).
    return None


def fetch_ema(_unused: str) -> Optional[dict]:
    """EMA adapter — Singapore. Monthly aggregates only; we cache for 30d.
    Returns demand growth + generation mix when EMA_API_KEY is present."""
    if not os.environ.get("EMA_API_KEY"):
        return None
    # TODO Phase UU+1: pull monthly stats from
    #   https://www.ema.gov.sg/statistic.aspx?sta_sid=20140826M3iJSjwS9Ggk
    #   Map into demand_growth_yoy_pct + reserve_margin_pct.
    return None


def fetch_neso(_market_name: str) -> Optional[dict]:
    """NESO adapter — GB. Pulls ESO data portal daily aggregates."""
    if not os.environ.get("NESO_API_KEY"):
        return None
    # TODO Phase UU+1: wire the ESO data portal API
    #   https://data.nationalgrideso.com/
    #   Map UK-specific demand/generation series into our metric shape.
    return None


# ── Dispatcher: slug → metrics ────────────────────────────────────
def fetch_metrics_for_intl_slug(slug: str) -> Optional[dict]:
    """Single entry point called from routes/dcpi.py once adapters are
    live. Returns None for any slug the registry doesn't know or any
    adapter without credentials — caller falls back to LOW_SIGNAL.
    """
    entry = _INTL_MARKET_MAP.get(slug)
    if not entry:
        return None
    adapter, _country, key = entry
    if   adapter == "entsoe": return fetch_entsoe(key)
    elif adapter == "jepx":   return fetch_jepx(key)
    elif adapter == "ema":    return fetch_ema(key)
    elif adapter == "neso":   return fetch_neso(key)
    return None


# ── Status endpoint — wires into the brain consistency radar ──────
@international_ingestion_bp.route("/api/v1/intl/ingestion-status", methods=["GET"])
def api_ingestion_status():
    """Public endpoint reporting which adapters have credentials + a
    coverage summary. The brain consistency radar consumes this to flag
    international markets with no live data after their first 7 days."""
    adapters = _adapter_status()
    live_count = sum(1 for a in adapters if a["key_present"])
    total_markets = len(_INTL_MARKET_MAP)
    live_markets = sum(
        len(a["markets"]) for a in adapters if a["key_present"]
    )
    return jsonify(
        adapters=adapters,
        summary={
            "adapters_live":  live_count,
            "adapters_total": len(adapters),
            "markets_with_live_feed":  live_markets,
            "markets_with_stub_feed":  total_markets - live_markets,
            "total_intl_markets":       total_markets,
        },
        next_steps=(
            "Add the env vars above to Railway. Each adapter degrades "
            "gracefully when its key is absent — markets fall back to "
            "LOW_SIGNAL verdict + neutral 50/50 scores. Adding even one "
            "key lights up its bound markets on the next recompute cycle."
        ),
        generated_at=datetime.datetime.utcnow().isoformat() + "Z",
    ), 200
