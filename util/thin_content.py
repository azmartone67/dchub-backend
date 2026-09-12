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

# City values this dataset uses when the real city is unknown. NOT a place —
# see the same list in routes/facility_profile_page.py's comparables guard,
# which must agree.
#
# ★ RE-MEASURED 2026-09-07 (the 08-14 figures below were 314 / 30 / "136 each",
#   and the second one understates the hazard by ~4x):
#     bare 'Regional'          727 live rows, 35 countries, and EVERY one of
#                              them source 'competitor_gap:cloudscene' — one
#                              upstream, not a corpus-wide habit
#     '<X> Regional' labels  1,149 rows across 74 distinct values in EACH
#                              table — Connecticut 142, California 141,
#                              New York 97, Florida 90, Ohio 83, Texas 47,
#                              West Virginia 42, Illinois 35, Arizona 32 …
#   These are REAL market labels and are deliberately absent from the tuple:
#   the test is EQUALITY, never substring, or 1,149 real pages lose their
#   city instead of gaining one. tests/test_thin_content_lanes.py pins a
#   spread of them, not just the two that used to be named here.
PLACEHOLDER_CITIES = ("regional", "unknown", "n/a", "none", "other")


def _has(v) -> bool:
    return v is not None and str(v).strip() not in ("", "0", "0.0", "None")


def is_placeholder_city(value) -> bool:
    """True when `value` is one of the dataset's not-a-place markers.

    ★ THE SCALAR FORM, so a caller holding a bare city string does not have to
      build a dict or paste the tuple. Pasting it is what happened: before
      2026-09-07 three separate places spelled this list — here, the
      comparables guard in routes/facility_profile_page, and that file's `_has`
      (which excluded 'Unknown' and not 'Regional') — and the RENDERER consulted
      none of them, so 727 live pages published "Regional" as a place.
    ★ Equality, never substring: 'California Regional' / 'Connecticut Regional'
      (136 rows each) are REAL market labels.
    """
    return (value or "").strip().lower() in PLACEHOLDER_CITIES


def real_city(fac: dict) -> str:
    """The facility's city, or '' when it is a placeholder rather than a place."""
    c = (fac.get("city") or "").strip()
    return "" if is_placeholder_city(c) else c


def evidence(fac: dict) -> dict:
    """Which indexable facts this facility actually carries.

    ★★★ r-mw-one-owner (2026-09-12). "carries" means WHAT THE PAGE RENDERS, not
    what the column holds. `power` therefore asks
    util.facility_headline.plausible_mw, the single owner of the plausibility
    cap, and NOT `_has` — because a capacity above that cap is suppressed from
    every surface the reader or a crawler can see: the SERP title, the body
    stat tile, the narrative, this module's own LANE-2 context_block, the
    comparables peer annotation, and facility_measures (which feeds both the
    inline Dataset JSON-LD and the /facilities/<slug>.json twin).

    Measured live 2026-09-12 against the published sitemap (18,880 facility
    URLs) and the rows behind it (47,695 across both tables): 17 published
    pages had NO city, NO street address and NO coordinates, so an implausible
    power_mw was the whole of their evidence. All 17 render exactly three stat
    tiles — Power, Status, Country — and a LANE-2 block whose only row is
    "Reported capacity". Suppress the capacity and Status + Country is all that
    is left, i.e. precisely the page LANE 3 exists to noindex; `_has` kept
    every one of them `index, follow` and in BOTH sitemap families.

    ★ ASK THE FUNCTION, do not re-spell `<= MW_PLAUSIBLE_MAX`. A second copy of
      that comparison is the original defect — the cap existed and exactly one
      surface consulted it. See plausible_mw's docstring for the census.
    ★ The import is function-level, like context_block's below: this module is
      imported BY util.facility_headline (is_placeholder_city, at its own
      function level), so a module-level import here would close that cycle.
    ★ SUPPRESSION, NOT DELETION, unchanged: the row keeps its power_mw, the
      page keeps serving 200 at its frozen slug, and the facility qualifies
      again the moment it gains any one of the four — including a corrected
      capacity that lands under the cap.
    """
    from util.facility_headline import plausible_mw as _plausible_mw

    lat = fac.get("latitude", fac.get("lat"))
    lng = fac.get("longitude", fac.get("lon", fac.get("lng")))
    return {
        "power": _plausible_mw(fac.get("power_mw")) is not None,
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


def contentless_slug_set(cursor) -> set:
    """Frozen slugs whose page will serve robots=noindex on the LANE 3 verdict.

    ★★★ r-noindex-coherence (2026-09-07). Measured against the live sitemap
    (18,991 facility URLs, each resolved to the row that actually serves it —
    discovered first then legacy, ORDER BY power_mw DESC, the page's own
    resolution in _fetch_facility_by_slug):

        770 published URLs serve robots=noindex
        770 of 770 are is_contentless
          0 are OSM-junk, NER or headline junk    <- those guards work
        763 arrive through the UNGATED AI family
          7 arrive in the GATED shard via the r-proven-exempt readmission

    A sitemap entry says "index this" and the page says "do not". Lane 3
    noindexes these on evidence, so the sitemap entry is the wrong half. The 7
    are the sharper case: readmitting a GSC-proven page PAST the capacity gate
    cannot achieve anything while the page still says noindex.

    ★ THIS QUERY LIVES HERE, NOT IN main._build_sitemap_sections, and that is
      not cosmetic. tests/test_sitemap_thin_gate::test_the_gate_is_emission_only
      refuses any `power_mw` reference inside a builder query, because capacity
      as the THIN GATE must reach SQL only through `_thin_excl` or the kill
      switch and the collapse floor stop governing it. That guard is right, and
      it fired on the first draft of this change. Capacity here is one of FOUR
      evidence fields for a DIFFERENT policy, so the policy moves next to its
      predicate — the shape util/facility_ner_noindex.refresh_suppressed_slugs
      already established for the other noindexed class.
    ★ Deliberately NOT governed by SITEMAP_THIN_GATE_DISABLE. That switch means
      "publish thin pages", and the ungated AI family sets it — which is where
      763 of the 770 come from. A coherence invariant is not a tuning knob.
    ★ A slug carried by SEVERAL rows is kept whenever ANY of them has content:
      the richest row serves the page, and that page is not noindexed.
    ★ Returns an EMPTY set on failure or on an implausible result. The caller's
      contract is "empty means emit everything", i.e. exactly today's sitemap.
    """
    # measured rate is ~4% of rows; a quarter of the corpus is ~6x that and
    # means the evidence columns went missing, not that the corpus went empty
    _REFUSE_ABOVE = 4
    try:
        cursor.execute(
            "SELECT canonical_slug, city, address, latitude, longitude, "
            "       power_mw "
            "  FROM discovered_facilities "
            " WHERE canonical_slug IS NOT NULL AND canonical_slug <> '' "
            "   AND COALESCE(is_duplicate, 0) = 0 "
            " UNION ALL "
            "SELECT canonical_slug, city, address, latitude, longitude, "
            "       power_mw "
            "  FROM facilities "
            " WHERE canonical_slug IS NOT NULL AND canonical_slug <> ''")
        rows = cursor.fetchall() or []
    except Exception:
        return set()
    out, has_content = set(), set()
    for r in rows:
        fac = {"city": r[1], "address": r[2], "latitude": r[3],
               "longitude": r[4], "power_mw": r[5]}
        (out if is_contentless(fac) else has_content).add(r[0])
    out -= has_content
    if rows and len(out) > len(rows) // _REFUSE_ABOVE:
        return set()
    return out


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
    # ★ r-mw-one-owner (2026-09-12): NOT `_has`. This row is LANE 2 — facts
    #   rendered to make a thin page rankable — and it published "Reported
    #   capacity  63000.0 MW" for a utility's whole generating fleet. The
    #   plausibility cap has one owner (util.facility_headline.MW_PLAUSIBLE_MAX,
    #   asked through plausible_mw); imported here rather than re-spelled,
    #   function-level to match this module's other imports and to keep
    #   thin_content importable with nothing else loaded.
    from util.facility_headline import plausible_mw as _plausible_mw
    if _plausible_mw(fac.get("power_mw")) is not None:
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
