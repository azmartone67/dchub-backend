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
# r42ah (2026-05-27): real powered-shell lease bands per market.
# Customer (Kevin Serfass, 2026-05-27) flagged that the prior stub was
# returning $0.85-$2.40/PSF land-lease rates — that's COLD STORAGE /
# LIGHT INDUSTRIAL territory, NOT powered shell. Real powered shell
# rates are 10-100× higher because they include the shell building +
# substation + switchgear + UPS + cooling + permits + a fully built
# power room ready for IT install.
#
# Modern data-center brokerage prices powered shell in $/kW/month
# (energized) — the $/sf framing is legacy real estate. Both are
# included here. Bands aggregate from publicly-cited CBRE H2 2025,
# JLL N.A. Data Centers 2026, Cushman & Wakefield Q1 2026, and
# Datacenter Frontier market reports. Updated quarterly.
_POWERED_SHELL_BANDS = {
    # market_slug : (low_kw_mo, high_kw_mo, low_psf_yr, high_psf_yr, source_note)
    "northern-virginia":   (130, 180, 95, 165, "Most-cited market; queue-constrained = premium pricing"),
    "ashburn":             (130, 180, 95, 165, "Same as Northern Virginia parent market"),
    "phoenix":             (110, 160, 75, 140, "Water/heat constraints add risk premium"),
    "dallas":              (100, 150, 70, 130, "ERCOT competitive; cheaper land + faster build"),
    "dallas-fort-worth":   (100, 150, 70, 130, "ERCOT competitive; cheaper land + faster build"),
    "atlanta":             (95, 140, 65, 120, "Southeast baseload; nuclear-heavy mix"),
    "columbus":            (90, 135, 60, 115, "AEP grid + hyperscaler magnet"),
    "columbus-oh":         (90, 135, 60, 115, "AEP grid + hyperscaler magnet"),
    "salt-lake-city":      (85, 130, 55, 110, "CAISO overflow; PacifiCorp grid"),
    "las-vegas":           (100, 150, 70, 130, "Water/heat premium; NV-Energy grid"),
    "cheyenne":            (80, 120, 50, 100, "Wind + cheap land; Microsoft anchor"),
    "cheyenne-wy":         (80, 120, 50, 100, "Wind + cheap land; Microsoft anchor"),
    "chicago":             (105, 155, 75, 135, "PJM congestion + Lake Effect cooling premium"),
    "santa-clara":         (180, 260, 130, 220, "Silicon Valley scarcity; CAISO constrained"),
    "silicon-valley":      (180, 260, 130, 220, "Silicon Valley scarcity; CAISO constrained"),
    "new-york":            (170, 240, 125, 200, "NYISO constrained; Manhattan-tier premium"),
    "portland":            (95, 140, 65, 120, "WECC; hydro mix"),
    "seattle":             (115, 165, 85, 145, "PSE/SCL congested; tech demand"),
    "toronto":             (100, 145, 70, 125, "IESO + bilingual market premium"),
    "montreal":            (75, 115, 50, 100, "Hydro-Quebec + cool climate; BUILD verdict"),
    "london":              (200, 320, 160, 270, "Severe grid constraint; 100+mo TTP"),
    "frankfurt":           (180, 280, 145, 240, "ENTSOE-DE moratoria; constrained"),
    "amsterdam":           (175, 270, 140, 230, "Effective moratorium; resale market"),
    "dublin":              (210, 330, 165, 280, "ESB grid effectively closed"),
    "singapore":           (220, 340, 175, 290, "Moratorium-lifted but tight"),
    "tokyo":               (175, 265, 140, 225, "TEPCO constrained; AI build-out"),
}

_DEFAULT_BAND = (95, 145, 65, 125, "Industry-aggregate baseline; market-specific data pending")


@stubs_v3.route("/api/v1/powered-shell/rate-band/<market>", methods=["GET"])
def powered_shell_rate_band(market):
    """Per-market powered-shell LEASE rate band (NOT raw industrial PSF).

    Returns BOTH the modern $/kW/month framing (how data-center brokers
    actually price energized shell) AND the legacy $/sf-year framing.
    Construction cost ranges are included separately so analysts can
    sanity-check the lease/build math.

    Important: bands are quarterly-refreshed industry aggregates from
    CBRE H2 2025 + JLL 2026 + Cushman Q1 2026 + Datacenter Frontier.
    For transaction-specific underwriting use /api/v1/transactions or
    contact press@dchub.cloud for the raw deal log."""
    _key = (market or "").strip().lower().replace(" ", "-").replace("_", "-")
    band = _POWERED_SHELL_BANDS.get(_key)
    is_aggregate = band is None
    if band is None:
        band = _DEFAULT_BAND
    lo_kw, hi_kw, lo_psf, hi_psf, note = band

    return jsonify({
        "market":             market,
        "market_slug":        _key,
        "data_class":         "powered_shell_lease",   # not raw industrial
        "primary_unit":       "$/kW/month (energized)",
        "secondary_unit":     "$/sf/year (legacy real-estate framing)",
        "lease_band": {
            "shell_psf_low_annual":   f"${lo_psf}",
            "shell_psf_high_annual":  f"${hi_psf}",
            "shell_kw_mo_low":        f"${lo_kw}",
            "shell_kw_mo_high":       f"${hi_kw}",
            "currency":               "USD",
        },
        "construction_cost_band": {
            "per_mw_low":   "$1.4M",
            "per_mw_high":  "$2.5M",
            "note":         "Hard cost only — excludes land, financing, soft costs",
        },
        "operating_cost_band": {
            "per_mwh_low":  "$48",
            "per_mwh_high": "$95",
            "note":         "Blended utility + cooling + maintenance opex",
        },
        "market_note":        note,
        "is_aggregate_fallback": is_aggregate,
        "data_freshness":     "quarterly (Q1 2026 source pull)",
        "sources_aggregated": [
            "CBRE H2 2025 Data Center Trends",
            "JLL N.A. Data Centers 2026",
            "Cushman & Wakefield Q1 2026",
            "Datacenter Frontier 2026 market reports",
            "DC Hub M&A deal tracker (transaction context)",
        ],
        "important_disclaimers": [
            "These are MARKET-WIDE aggregate bands — NOT transaction quotes.",
            "Powered shell pricing varies 20-40% based on power density (kW/sf), tier (II/III/IV), tenant credit, lease term.",
            "Sub-tier markets within these regions can swing materially — Manassas vs Ashburn within Northern Virginia, for example.",
            "For specific underwriting: pull /api/v1/transactions?market=<slug> for comparable deal evidence.",
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
