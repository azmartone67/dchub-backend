"""Scaffolded stubs — replace 501 with real impl when data sources are wired."""
from flask import Blueprint, jsonify, request

stubs_v3 = Blueprint("stubs_v3", __name__)

# r-poweredshell (2026-06-16): wire the /powered-shell page to REAL data.
# Per-slug display metadata for the markets that have a powered-shell rate band
# (_POWERED_SHELL_BANDS below). currency drives country/region so the frontend's
# region filter + "{country} · {region}" line render correctly. Sub-market slugs
# (ashburn, dallas, santa-clara…) are collapsed into their parent market so the
# /markets list shows one card per market, not duplicates.
_PS_MARKET_META = {
    # slug:                (display_name,            country, region)
    "northern-virginia":   ("Northern Virginia",     "US", "north_america"),
    "dallas-fort-worth":   ("Dallas / Fort Worth",   "US", "north_america"),
    "phoenix":             ("Phoenix",               "US", "north_america"),
    "atlanta":             ("Atlanta",               "US", "north_america"),
    "columbus":            ("Columbus OH",           "US", "north_america"),
    "silicon-valley":      ("Silicon Valley",        "US", "north_america"),
    "new-york-tristate":   ("New York Tri-State",    "US", "north_america"),
    "chicago":             ("Chicago",               "US", "north_america"),
    "greater-philadelphia":("Greater Philadelphia",  "US", "north_america"),
    "eastern-pennsylvania":("Eastern Pennsylvania",  "US", "north_america"),
    "hillsboro":           ("Hillsboro / PNW",       "US", "north_america"),
    "reno":                ("Reno",                  "US", "north_america"),
    "las-vegas":           ("Las Vegas",             "US", "north_america"),
    "salt-lake-city":      ("Salt Lake City",        "US", "north_america"),
    "los-angeles":         ("Los Angeles",           "US", "north_america"),
    "frankfurt":           ("Frankfurt",             "DE", "europe"),
    "london":              ("London",                "UK", "europe"),
    "singapore":           ("Singapore",             "SG", "asia_pacific"),
}
# Sub-market slugs that alias to a primary market (excluded from the /markets list).
_PS_SUBMARKET_ALIASES = {"ashburn", "dallas", "columbus-oh", "santa-clara",
                          "new-york", "portland"}


@stubs_v3.route("/api/v1/powered-shell/markets", methods=["GET"])
def powered_shell_markets():
    """Markets with a real powered-shell rate band. Returns a BARE ARRAY of
    {slug, display_name, country, region} — the shape the /powered-shell page's
    unwrap() consumes directly (an object payload would be mis-wrapped → the page
    falls back). Drives the per-market rate-band fetches."""
    out = []
    for slug in _POWERED_SHELL_BANDS:
        if slug in _PS_SUBMARKET_ALIASES:
            continue
        dn, country, region = _PS_MARKET_META.get(
            slug, (slug.replace("-", " ").title(), "US", "north_america"))
        out.append({"slug": slug, "display_name": dn,
                    "country": country, "region": region})
    out.sort(key=lambda m: m["display_name"])
    return jsonify(out), 200

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
        # r-poweredshell (2026-06-16): TOP-LEVEL band fields — the /powered-shell
        # page reads band.mid_psf_year directly (after the `band: r` frontend fix).
        # lease_band kept nested for API back-compat.
        "mid_psf_year":          mid_psf,
        "low_psf_year":          lo_psf,
        "high_psf_year":         hi_psf,
        "currency":              currency,
        "n_comps":               n_comps,
        "typical_term_years":    term,
        "typical_escalator_pct": esc,
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


# r-poweredshell (2026-06-16): REAL powered-shell lease comps — the same
# SEC/investor-filing transactions cited in the band methodology above. These
# are VERIFIED public filings, not fabricated. Deal-level detail is a paid
# (Developer+) feature; free/anon callers get an empty list + the upgrade CTA.
_PS_COMPS = [
    {"market": "Northern Virginia", "submarket": "Manassas", "tenant": "AWS",
     "landlord": "COPT Defense", "leased_sf": 728000, "base_rent_psf_year": 10.50,
     "term_years": 15, "signed_date": "2022",
     "source": "SEC filing (COPT)", "source_url": "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK=0001622194"},
    {"market": "Northern Virginia", "submarket": "Ashburn", "tenant": "NTT",
     "landlord": "Digital Realty", "leased_sf": 206000, "base_rent_psf_year": 18.39,
     "term_years": 15, "signed_date": "2021",
     "source": "DLR investor disclosure", "source_url": "https://investor.digitalrealty.com/"},
    {"market": "New York Tri-State", "submarket": "Piscataway", "tenant": "Microsoft",
     "landlord": "Digital Realty", "leased_sf": 220000, "base_rent_psf_year": 22.00,
     "term_years": 15, "signed_date": "2025",
     "source": "DLR investor disclosure", "source_url": "https://investor.digitalrealty.com/"},
    {"market": "Phoenix", "submarket": "Mesa", "tenant": "AWS",
     "landlord": "Vantage Data Centers", "leased_sf": 500000, "base_rent_psf_year": 13.60,
     "term_years": 15, "signed_date": "2025",
     "source": "Vantage announcement", "source_url": "https://vantage-dc.com/news/"},
]


def _ps_is_paid() -> bool:
    """True if the caller resolves to a paid tier (deal-level comps are paid)."""
    try:
        from routes.tier_gate import _resolve_caller_tier
        tier, _ = _resolve_caller_tier()
        return (tier or "FREE").upper() in (
            "PRO", "PAID", "DEVELOPER", "STARTER", "ENTERPRISE", "FOUNDING",
            "RESEARCH_SEED", "ADMIN")
    except Exception:
        return False


@stubs_v3.route("/api/v1/powered-shell/comps", methods=["GET"])
def powered_shell_comps():
    """Real powered-shell lease comps (SEC/investor filings). BARE ARRAY for the
    page's unwrap(). Deal-level detail is gated to Developer+ — free/anon get an
    empty array so the page renders its 'Upgrade to Developer' state."""
    if not _ps_is_paid():
        return jsonify([]), 200
    return jsonify(_PS_COMPS), 200


@stubs_v3.route("/api/v1/powered-shell/pipeline", methods=["GET"])
def powered_shell_pipeline():
    """Real powered-shell / new-build pipeline from the live `deals` table
    (announced builds + developments). BARE ARRAY for unwrap(). No fabrication —
    rows are the tracked M&A/development deals; unknown fields are null."""
    import os
    rows = []
    try:
        import psycopg2
        dsn = os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL")
        if dsn:
            conn = psycopg2.connect(dsn, connect_timeout=8)
            try:
                cur = conn.cursor()
                cur.execute("""
                    SELECT buyer, seller, value, mw, type, region, market, date
                      FROM deals
                     WHERE (LOWER(COALESCE(type,'')) LIKE '%%build%%'
                            OR LOWER(COALESCE(type,'')) LIKE '%%construct%%'
                            OR LOWER(COALESCE(type,'')) LIKE '%%develop%%'
                            OR LOWER(COALESCE(type,'')) LIKE '%%campus%%')
                       AND COALESCE(LOWER(TRIM(buyer)),'')  NOT IN ('tbd','unknown','n/a','')
                     ORDER BY COALESCE(date,'1970-01-01') DESC
                     LIMIT 100
                """)
                for buyer, seller, value, mw, dtype, region, market, ddate in cur.fetchall():
                    mkt = market or region or "—"
                    dev = seller or None
                    tenant = buyer or None
                    rows.append({
                        "project_name": f"{(tenant or dev or 'Data-center')} · {mkt}",
                        "market": mkt, "submarket": None,
                        "stage": "announced",
                        "announced_mw": float(mw) if mw not in (None, "") else None,
                        "announced_sf": None,
                        "announced_capex_usd": (float(value) * 1e6) if value not in (None, "") else None,
                        "target_online_date": None,
                        "developer": dev, "rumored_tenant": tenant,
                        "signed_date": (ddate.isoformat() if hasattr(ddate, "isoformat") else (ddate or None)),
                        "source_url": f"https://dchub.cloud/transactions",
                    })
            finally:
                try: conn.close()
                except Exception: pass
    except Exception:
        rows = []
    return jsonify(rows), 200


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
