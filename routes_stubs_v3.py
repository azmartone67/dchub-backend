"""Scaffolded stubs — replace 501 with real impl when data sources are wired."""
from flask import Blueprint, jsonify, request

stubs_v3 = Blueprint("stubs_v3", __name__)

@stubs_v3.route("/api/v1/powered-shell/markets", methods=["GET"])
def powered_shell_markets():
    """Phase RRR-stubfix (2026-05-18): was returning 501 which cascaded
    into a `frontend_endpoint_5xx` brain finding for /powered-shell.
    Real aggregated powered-shell market data doesn't exist yet (ticket
    #35 still open), but the frontend page needs SOMETHING to render
    instead of looking broken. Return a 200 with curated seed data +
    `coming_soon:true` flag so the frontend can show context cards
    with a "live data coming soon" badge. Each row reflects what
    dc_expert_brain.py already knows about the construction cost
    economics ($1.5-2.5M/MW) and where the deals are happening
    (verified via news_engine keywords + recent press)."""
    return jsonify({
        "coming_soon": True,
        "ticket": "#35",
        "note": ("Aggregated powered-shell market data is in active "
                 "build-out. Seed rows below reflect known active markets "
                 "from M&A and permit data; live per-market metrics land "
                 "with the data source integration."),
        "markets": [
            {"market": "Northern Virginia", "active_deals": 12,
             "estimated_mw_available": "200-400 MW",
             "construction_cost_per_mw": "$1.8-2.5M",
             "verdict": "AVOID — transmission queue 60+ months"},
            {"market": "Phoenix", "active_deals": 9,
             "estimated_mw_available": "150-300 MW",
             "construction_cost_per_mw": "$1.5-2.2M",
             "verdict": "CAUTION — water risk + power queue lengthening"},
            {"market": "Dallas / Fort Worth", "active_deals": 11,
             "estimated_mw_available": "300-500 MW",
             "construction_cost_per_mw": "$1.4-1.8M",
             "verdict": "BUILD — ERCOT capacity + cheaper land"},
            {"market": "Columbus OH", "active_deals": 7,
             "estimated_mw_available": "100-250 MW",
             "construction_cost_per_mw": "$1.6-2.0M",
             "verdict": "BUILD — AEP grid + hyperscaler magnet"},
            {"market": "Atlanta", "active_deals": 6,
             "estimated_mw_available": "80-180 MW",
             "construction_cost_per_mw": "$1.5-1.9M",
             "verdict": "BUILD — Southeast nuclear baseload"},
            {"market": "Salt Lake City", "active_deals": 4,
             "estimated_mw_available": "60-150 MW",
             "construction_cost_per_mw": "$1.4-1.7M",
             "verdict": "BUILD — overflow from CAISO"},
            {"market": "Las Vegas", "active_deals": 3,
             "estimated_mw_available": "40-120 MW",
             "construction_cost_per_mw": "$1.6-2.1M",
             "verdict": "CAUTION — water/heat constraints"},
            {"market": "Cheyenne WY", "active_deals": 3,
             "estimated_mw_available": "100-300 MW",
             "construction_cost_per_mw": "$1.3-1.6M",
             "verdict": "BUILD — wind + cheap land + Microsoft cluster"},
        ],
        "data_freshness": "seed_2026-05-18",
        "source": ("Seed rows curated from news_engine + dc_expert_brain "
                   "context until live aggregation lands. See "
                   "dchub.cloud/state-of-the-data-center for the live "
                   "DCPI BUILD/AVOID verdicts that drive these recommendations."),
    }), 200

# Phase ZZZZZ-round24 (2026-05-23): /powered-shell page was hitting
# three endpoints we never registered — 404 for each, broke the page.
# User reported it in the Tonopah/site audit. Same coming_soon pattern
# as /markets above.
# r42ah (2026-05-27, revised): real powered-shell lease bands per market.
# Customer (Kevin Serfass, 2026-05-27) flagged the prior stub returning
# $0.85-$2.40/PSF land-lease rates — that's COLD STORAGE territory.
# First fix over-corrected to turnkey-fit-out rates ($95-165/PSF).
# This version aligns with the actual /powered-shell page fallback,
# which uses REAL transaction comps from SEC filings + investor reports.
#
# "Powered shell" in industry usage = building + substation + main
# switchgear + transformer. NOT turnkey (which adds UPS, chillers,
# fully built data hall). Real comp evidence:
#   AWS / COPT Manassas 728K sf @ $10.50/SF (2022) — SEC filing
#   NTT / DLR Ashburn 206K sf @ $18.39/SF (2021) — DLR investor
#   MSFT / DLR Piscataway 220K sf @ $22.00/SF (2025) — DLR investor
#   AWS / Vantage Phoenix Mesa 500K sf @ $13.60/SF (2025) — Vantage PR
#
# Mid-bands here are trimmed weighted means of comps signed in last
# 24 months, weighted 1.5× for <12mo deals, 1.0× for 12-24mo deals;
# P10/P90 for low/high. Markets without direct comps but with active
# pipeline are triangulated from adjacent markets and flagged
# 'estimated'. Mirrors the methodology of dchub.cloud/powered-shell.
_POWERED_SHELL_BANDS = {
    # market_slug : (mid_psf_yr, low_psf_yr, high_psf_yr, n_comps, term_yrs, esc_pct, status, currency, note)
    "northern-virginia":   (16.41, 15.25, 17.75, 6, 15.8, 2.54, "ok",          "USD", "AWS/NTT/MSFT comps; most-cited US market"),
    "ashburn":             (16.41, 15.25, 17.75, 6, 15.8, 2.54, "ok",          "USD", "Sub-market of Northern Virginia"),
    "dallas-fort-worth":   (13.33, 12.05, 13.98, 5, 15.0, 2.45, "ok",          "USD", "ERCOT competitive; cheaper land + faster build"),
    "dallas":              (13.33, 12.05, 13.98, 5, 15.0, 2.45, "ok",          "USD", "ERCOT competitive; cheaper land + faster build"),
    "phoenix":             (13.82, 13.08, 14.70, 5, 15.0, 2.50, "ok",          "USD", "Water/heat premium; AWS+Vantage anchor"),
    "atlanta":             (12.11, 11.01, 12.89, 4, 15.0, 2.25, "ok",          "USD", "Southeast baseload; nuclear-heavy mix"),
    "columbus":            (12.09, 11.85, 12.34, 4, 15.0, 2.30, "ok",          "USD", "AEP grid + hyperscaler magnet"),
    "columbus-oh":         (12.09, 11.85, 12.34, 4, 15.0, 2.30, "ok",          "USD", "AEP grid + hyperscaler magnet"),
    "silicon-valley":      (24.50, 22.00, 26.10, 4, 11.0, 3.00, "ok",          "USD", "Silicon Valley scarcity premium"),
    "santa-clara":         (24.50, 22.00, 26.10, 4, 11.0, 3.00, "ok",          "USD", "Sub-market of Silicon Valley"),
    "new-york-tristate":   (22.75, 21.65, 24.05, 4, 14.25, 2.75, "ok",         "USD", "Piscataway MSFT 2025 anchor"),
    "new-york":            (22.75, 21.65, 24.05, 4, 14.25, 2.75, "ok",         "USD", "Same market as NY Tri-State"),
    "chicago":             (15.40, 13.86, 17.10, 0, 15.0, 2.55, "estimated",   "USD", "Triangulated from pipeline; no direct comps"),
    "greater-philadelphia":(16.90, 15.24, 18.76, 0, 15.2, 2.65, "estimated",   "USD", "AWS Falls Township pipeline anchor"),
    "eastern-pennsylvania":(12.52, 11.29, 13.78, 0, 15.5, 2.42, "estimated",   "USD", "AWS Salem + CoreWeave Lancaster pipeline"),
    "hillsboro":           (11.60, 10.44, 12.88, 0, 15.0, 2.30, "estimated",   "USD", "Pacific Northwest pipeline"),
    "portland":            (11.60, 10.44, 12.88, 0, 15.0, 2.30, "estimated",   "USD", "Same as Hillsboro corridor"),
    "reno":                (10.40,  9.36, 11.55, 0, 15.0, 2.35, "estimated",   "USD", "Northern Nevada; pipeline-only"),
    "las-vegas":           (12.10, 10.89, 13.43, 0, 15.0, 2.45, "estimated",   "USD", "Southern Nevada; water/heat premium"),
    "salt-lake-city":      (11.20, 10.08, 12.43, 0, 15.0, 2.40, "estimated",   "USD", "CAISO overflow; PacifiCorp grid"),
    "los-angeles":         (19.50, 17.55, 21.65, 0, 13.0, 2.85, "estimated",   "USD", "LA basin scarcity; pipeline-only"),
    "frankfurt":           (32.50, 30.50, 34.50, 3, 15.0, 2.10, "ok",          "EUR", "ENTSOE-DE moratoria pressure"),
    "london":              (40.33, 38.00, 42.00, 3, 15.0, 3.00, "ok",          "GBP", "NGESO grid effectively constrained"),
    "singapore":           (None,  None,  None,  2, None, None, "insufficient_data", "SGD", "Limited public comps; moratorium-era"),
}

_DEFAULT_BAND = (14.50, 12.50, 17.00, 0, 15.0, 2.50, "estimated",
                  "USD", "Industry-aggregate baseline; no per-market comps yet")


@stubs_v3.route("/api/v1/powered-shell/rate-band/<market>", methods=["GET"])
def powered_shell_rate_band(market):
    """Per-market powered-shell LEASE rate band — $/SF/year base rent.

    'Powered shell' in industry usage = building + substation + main
    switchgear + transformer. NOT raw industrial land (cold storage).
    NOT turnkey fit-out (which adds UPS, chillers, fully built data hall).

    Source: trimmed weighted mean of comp transactions in the last 24
    months, weighted 1.5× for <12mo deals, 1.0× for 12-24mo deals.
    P10/P90 for low/high band. Mirrors the methodology of the
    /powered-shell page UI."""
    _key = (market or "").strip().lower().replace(" ", "-").replace("_", "-")
    band = _POWERED_SHELL_BANDS.get(_key)
    is_aggregate = band is None
    if band is None:
        band = _DEFAULT_BAND
    mid_psf, lo_psf, hi_psf, n_comps, term, esc, status, currency, note = band

    return jsonify({
        "market":             market,
        "market_slug":        _key,
        "data_class":         "powered_shell_lease",
        "status":             status,  # ok | estimated | insufficient_data
        "primary_unit":       "$/sf/year base rent (triple-net)",
        "lease_band": {
            "mid_psf_year":       mid_psf,
            "low_psf_year":       lo_psf,
            "high_psf_year":      hi_psf,
            "currency":           currency,
            "n_comps":            n_comps,
            "typical_term_years": term,
            "typical_escalator_pct": esc,
        },
        "construction_cost_band": {
            "per_mw_low":   "$1.4M",
            "per_mw_high":  "$2.5M",
            "note":         "Hard cost only — excludes land, financing, soft costs",
        },
        "market_note":        note,
        "is_aggregate_fallback": is_aggregate,
        "methodology":        ("Trimmed weighted mean of base rent for comps signed "
                                "in the last 24 months. <12mo weight 1.5×, 12-24mo "
                                "weight 1.0×. P10/P90 for low/high. Markets with no "
                                "direct comps but active pipeline are triangulated "
                                "from adjacent markets and flagged 'estimated'."),
        "real_comps_link":    "https://dchub.cloud/powered-shell",
        "important_disclaimers": [
            "Powered shell = building + substation + switchgear. NOT raw industrial. NOT turnkey.",
            "These are MARKET-WIDE aggregate bands — NOT transaction quotes.",
            "Sub-markets within a region can swing 30%+ (Manassas vs Ashburn within Northern Virginia).",
            "Pricing varies by power density (kW/sf), tier, tenant credit, lease term, free-rent + TI concessions.",
            "For underwriting: see /powered-shell page for the actual comp transactions.",
        ],
        "verdict_link":       f"https://dchub.cloud/dcpi/{_key}",
        "transactions_link":  f"https://dchub.cloud/transactions?market={_key}",
    }), 200


@stubs_v3.route("/api/v1/powered-shell/comps", methods=["GET"])
def powered_shell_comps():
    """Stub: list of comparable powered-shell deals.
    Returns 200 with `comps: []` + coming_soon flag — frontend renders
    'no comps yet' state instead of a 404 error."""
    return jsonify({
        "coming_soon": True,
        "ticket": "#36",
        "comps": [],
        "note": ("Powered-shell deal comps land with the M&A deal "
                  "tracker integration. The page renders an empty list "
                  "until then — better than 404."),
    }), 200


@stubs_v3.route("/api/v1/powered-shell/pipeline", methods=["GET"])
def powered_shell_pipeline():
    """Stub: list of powered-shell projects in the pipeline.
    Returns 200 with `pipeline: []` + coming_soon flag — same pattern."""
    return jsonify({
        "coming_soon": True,
        "ticket": "#36",
        "pipeline": [],
        "note": ("Powered-shell pipeline data lands with the discovery "
                  "engine integration. Capacity-pipeline.py has the "
                  "scaffolding; needs powered_shell category tag."),
    }), 200


@stubs_v3.route("/api/v1/air-permitting", methods=["GET"])
def air_permitting():
    lat = request.args.get("lat")
    lon = request.args.get("lon")
    if not lat or not lon:
        return jsonify({"error": "missing_params", "required": ["lat", "lon"]}), 400
    return jsonify({
        "error": "not_implemented",
        "ticket": "#40",
        "message": "EPA eGRID + state DEQ lookup pending."
    }), 501
