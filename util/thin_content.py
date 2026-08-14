"""
util/thin_content.py — 2026-08-14. The three lanes of the thin-content program,
as pure functions so they are testable without a DB and reusable by the shell.

WHY THIS EXISTS. 3,563 facility pages sit in Google's "Crawled – currently not
indexed" — Google's way of saying it fetched the page and judged it not worth
an index slot. Measured on discovered_facilities 2026-08-14 (17,948 live rows):

    has coordinates   12,942   72%
    has a real city   16,702   93%
    has power_mw       6,648   37%
    has an address     1,368    8%
    has NOTHING          408   2.3%   <- no power, no coords, no address,
                                          no real city

★ THE DATABASE IS NOT EMPTY — THE PAGE IS. Only 408 facilities have nothing to
say. The other 17,540 have at least one real, citable fact that the profile
page was not rendering: it showed Status / City / Country and a two-sentence
narrative while DC Hub owns 320,000 mapped power/grid/fiber assets and the
facility carries coordinates 72% of the time.

THE THREE LANES (named to match the decision they implement):

  LANE 3  suppress   — the 408 contentless pages stop asking to be indexed.
                       They keep serving 200 at their frozen slug; noindex is
                       not deletion. A page with no power, no coordinates, no
                       address and no real city cannot rank for anything, and
                       every crawl of one is budget taken from a page that can.

  LANE 2  context    — render the market/ISO/DCPI facts already published
                       elsewhere on the site. No new disclosure, no tier
                       question: if /dcpi/<city> shows it to anonymous users,
                       the facility page may too.

  LANE 1  infra      — a shallow, citable slice of the per-site infrastructure
                       read (nearest-substation distance band, transmission
                       proximity). This is ADJACENT TO THE PAID PRODUCT, so it
                       is OFF unless THIN_INFRA_SLICE=1. Flipping it is a
                       pricing decision, not an SEO one, and it is deliberately
                       not the default.

★★ NOTHING HERE GENERATES PROSE. Every string is derived from a field this
facility actually has. "Crawled – currently not indexed" is Google already
detecting low-value pages; padding them with generated sentences is the failure
mode this repo's own gates (MEDIA_CLAIM_VERIFY, PRESS_INTEGRITY_ENFORCE) exist
to prevent. A page with nothing to say gets LANE 3, not filler.
"""
import os

# City values this dataset uses when the real city is unknown. 314 rows carry
# 'Regional' across 30 countries. NOT a place — see the same list in
# routes/facility_profile_page.py's comparables guard, which must agree.
# 'California Regional' / 'Connecticut Regional' (136 rows each) are REAL
# market labels and are deliberately absent: the test is equality, never
# substring, or 272 real pages lose their content instead of gaining it.
PLACEHOLDER_CITIES = ("regional", "unknown", "n/a", "none", "other")


def _has(v) -> bool:
    return v is not None and str(v).strip() not in ("", "0", "0.0", "None")


def real_city(fac: dict) -> str:
    """The facility's city, or '' when it is a placeholder rather than a place."""
    c = (fac.get("city") or "").strip()
    return "" if c.lower() in PLACEHOLDER_CITIES else c


def evidence(fac: dict) -> dict:
    """Which indexable facts this facility actually carries."""
    lat = fac.get("latitude", fac.get("lat"))
    lng = fac.get("longitude", fac.get("lon", fac.get("lng")))
    return {
        "power": _has(fac.get("power_mw")),
        "coords": _has(lat) and _has(lng),
        "address": _has(fac.get("address")),
        "city": bool(real_city(fac)),
    }


def is_contentless(fac: dict) -> bool:
    """LANE 3. True when the page has no fact that could ever rank.

    ★ ALL FOUR must be absent. This is deliberately much narrower than "has no
    coordinates" — _is_junk_facility's docstring records that an evidence test
    on coordinates alone would de-index 45 REAL coordinate-less OSM facilities.
    A facility with a street address but no lat/lon still has something to say;
    one with none of the four does not. Measured: 408 rows of 17,948 (2.3%).
    """
    return not any(evidence(fac).values())


def context_block(fac: dict, dcpi) -> str:
    """LANE 2 (+ LANE 1 when armed). Facts, rendered — never prose.

    Returns '' when there is nothing true to add, so a contentless page does
    not gain a header with an empty body.
    """
    from html import escape as _e

    rows = []
    city = real_city(fac)
    country = (fac.get("country") or "").strip()

    if dcpi:
        market = (dcpi.get("market_name") or "").strip()
        iso = (dcpi.get("iso") or "").strip()
        verdict = (dcpi.get("verdict") or "").strip().upper()
        ttp = dcpi.get("time_to_power_months")
        if market:
            rows.append(("Market", _e(market)))
        if iso:
            rows.append(("Grid operator", _e(iso)))
        if verdict:
            rows.append(("DC Hub Power Index", _e(verdict)))
        if ttp is not None:
            rows.append(("Est. time to power", f"{_e(str(ttp))} months"))
    if city and country:
        rows.append(("Location", f"{_e(city)}, {_e(country)}"))
    if _has(fac.get("power_mw")):
        rows.append(("Reported capacity", f"{_e(str(fac['power_mw']))} MW"))
    if _has(fac.get("operational_year")):
        rows.append(("Operational since", _e(str(fac["operational_year"]))))

    rows += _infra_rows(fac)

    if not rows:
        return ""
    cells = "".join(
        f'<div class="kv"><span class="k">{k}</span>'
        f'<span class="v">{v}</span></div>'
        for k, v in rows
    )
    return (
        '<div class="section"><div class="section-head">'
        '<h2>Market &amp; grid context</h2></div>'
        '<p class="section-sub">Published DC Hub data for this location.</p>'
        f'<div class="kvgrid">{cells}</div></div>'
    )


def infra_slice_armed() -> bool:
    """LANE 1 is a PRICING decision. Default OFF, flipped deliberately."""
    return os.environ.get("THIN_INFRA_SLICE", "0") == "1"


def _infra_rows(fac: dict):
    """LANE 1. A distance BAND, never the underlying asset list.

    Bands, not coordinates or counts: the band is enough to make the page
    uniquely indexable and to show the data exists, while the actual
    substation/transmission read stays the paid product. Returns [] unless
    armed AND the facility carries a precomputed band — this function performs
    NO query, so it cannot add latency or a pool hit to a page render.
    """
    if not infra_slice_armed():
        return []
    from html import escape as _e
    band = (fac.get("substation_band") or "").strip()
    return [("Nearest substation", _e(band))] if band else []
