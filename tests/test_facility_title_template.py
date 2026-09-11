#!/usr/bin/env python3
"""The title template shortens what SERPs show WITHOUT re-keying dedup.

NO NETWORK, NO DB.

WHY THE TEMPLATE. Measured live 2026-09-09 over 10 facility pages on
dchub.cloud, following redirects: 6 of 10 <title>s ran past Google's ~60-char
SERP cut, the longest at 102. What falls off the end is the TAIL —

    Equinix SV1/SV5/SV10 - Silicon Valley, San Jose — San Jose, CA, US Data
    Center | CAISO grid | DC Hub                                    (102)

i.e. the grid token and the brand, the two things the title had been EXTENDED
to carry (r-geo-headers put the ISO there as a retrieval key). Dropping the
literal "Data Center", the ", ST, CC" tail and the word "grid" returns ~27
chars. It does not fix every title — the facility NAME is 47 chars on its own
there — and that limit is stated in the PR rather than hidden.

★★★ THE LOAD-BEARING PART OF THIS CHANGE IS WHAT IT DOES NOT TOUCH.

identity_key() is the grouping key routes/facility_dedup_v4.py uses to decide
"these two URLs render the same page", and scripts/check_sitemap_selfcanon.py
counts the resulting groups. It was built as (h1, TITLE). Re-pointing the title
therefore re-partitions ~20,000 pages — silently, as a side effect of an SEO
copy edit — and dropping ", ST, CC" is exactly the kind of edit that MERGES
groups: two facilities of the same name in the same-named city in different
states stop being distinguishable.

So identity_key now reads `dedup_title`, which is the pre-change string byte
for byte. These tests hold that: an independent re-implementation of the OLD
formula is the oracle, and the collision matrix is asserted directly.
"""
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from util.facility_headline import (  # noqa: E402
    facility_headline, identity_key, brand_already_in_name)
from util.facility_site_code import site_code_headline  # noqa: E402
from util.thin_content import is_placeholder_city  # noqa: E402

ROWS = [
    ("Charlotte National Data Center", "Spectrum", "Charlotte", "NC", "US"),
    ("SV1/SV5/SV10 - Silicon Valley, San Jose", "Equinix", "San Jose", "CA", "US"),
    ("Switch Tahoe Reno", "Switch Ltd", "McCarran", "NV", "US"),
    ("IAD86", "Amazon Web Services", "Chantilly", "VA", "US"),
    ("QTS Manassas DC2", "Quality Technology Services", "Manassas", "VA", "US"),
    ("Hemel Hempstead 1 HH1", "NTT", "Hemel Hempstead", None, "GB"),
    (None, None, "Dublin", None, "IE"),
    ("Shanwei Data Center", "China Telecom", "Regional", None, "CN"),
    ("Equinix FR5", "Equinix", "Frankfurt am Main", None, "DE"),
    # the collision pair the ", ST, CC" tail exists to separate
    ("Metro Data Center", "Acme", "Springfield", "IL", "US"),
    ("Metro Data Center", "Acme", "Springfield", "MO", "US"),
    ("Metro Data Center", "Acme", "Springfield", None, "CA"),
    ("", "", "", "", ""),
]


def _legacy_title(name, provider, city, state, country):
    """Independent re-implementation of the PRE-2026-09-09 title.

    Deliberately NOT imported from the module under test — an oracle that calls
    the code it is checking proves nothing.
    """
    name = name or "Data Center"
    provider = provider or "Operator"
    city, state, country = city or "", state or "", country or ""
    if is_placeholder_city(city):
        city = ""
    loc_short = ", ".join([p for p in (city, state, country) if p])
    op = "" if (not provider or provider == "Operator"
                or brand_already_in_name(provider, name)) else f"{provider} "
    disp = f"{op}{name}".strip()
    sc = site_code_headline(name, "" if provider == "Operator" else provider,
                            city, state, country)
    if sc:
        return f"{sc} | DC Hub"
    return (f"{disp} — {loc_short} Data Center | DC Hub" if loc_short
            else f"{disp} Data Center | DC Hub")


# ── the invariant: dedup grouping does not move ──────────────────────────

@pytest.mark.parametrize("row", ROWS)
def test_identity_key_still_reads_the_pre_change_title(row):
    expected = " ".join(_legacy_title(*row).split()).lower()
    assert identity_key(*row)[1] == expected, (
        "identity_key moved with the display title — facility_dedup_v4 would "
        "re-group ~20k pages as a side effect of an SEO edit")


def test_same_name_and_city_in_different_states_stay_distinct():
    """The exact collision dropping ', ST, CC' from the title would create."""
    il = identity_key("Metro Data Center", "Acme", "Springfield", "IL", "US")
    mo = identity_key("Metro Data Center", "Acme", "Springfield", "MO", "US")
    ca = identity_key("Metro Data Center", "Acme", "Springfield", None, "CA")
    assert il != mo and il != ca and mo != ca
    # r-title-facts (2026-09-10): this second assertion FLIPPED. It used to say
    # the three share ONE display title — which is why the key had to stop
    # reading the title. The display title now spells the state/country out
    # again, so they are three; the key assertion above never read the title
    # and is unchanged.
    t = {facility_headline(*r)["title"] for r in ROWS[-4:-1]}
    assert t == {"Acme Metro Data Center · Springfield, Illinois | DC Hub",
                 "Acme Metro Data Center · Springfield, Missouri | DC Hub",
                 "Acme Metro Data Center · Springfield, Canada | DC Hub"}, t


def test_grouping_partition_is_identical_under_old_and_new():
    """Not just per-row equality — the whole partition."""
    def part(keyfn):
        g = {}
        for r in ROWS:
            g.setdefault(keyfn(r), []).append(r)
        return sorted((sorted(v, key=repr) for v in g.values()), key=repr)
    new = part(lambda r: identity_key(*r))
    old = part(lambda r: (" ".join(facility_headline(*r)["h1"].split()).lower(),
                          " ".join(_legacy_title(*r).split()).lower()))
    assert new == old


# ── the template itself ──────────────────────────────────────────────────

def test_title_follows_operator_site_city_template():
    """r-title-facts (2026-09-10): Charlotte is already in this name, so the
    city is no longer repeated after it (was "… · Charlotte"), and the spelled
    state does not fit 60 characters beside a 39-character name. A name that
    does NOT carry its city still gets the city, now with the spelled state."""
    t = facility_headline(*ROWS[0])["title"]
    assert t == "Spectrum Charlotte National Data Center | DC Hub"
    t = facility_headline(*ROWS[3])["title"]
    assert t == "Amazon Web Services IAD86 · Chantilly, Virginia | DC Hub"


def test_title_is_composed_from_the_template_not_the_old_shape():
    """Structural, not substring.

    An earlier version of this test banned the literal " Data Center | DC Hub"
    and failed on `China Telecom Shanwei Data Center | DC Hub` — a facility
    genuinely NAMED "Shanwei Data Center", with no city, so the template
    legitimately renders `{lead} | DC Hub`. Banning a phrase that the DATA may
    contain tests the corpus, not the composition. Assert the shape instead.

    r-title-facts (2026-09-10) changed the location-slot rule, so the slot is
    no longer "city, else state/country". It is the city when the name does
    not already carry it, the spelled region when there is no real city, and
    nothing otherwise — with the spelled region appended as a qualifier only
    while the title fits 60. So this asserts what the slot may be DRAWN FROM
    (this row's own city and spelled region, nothing else) and the rules that
    hold whichever option fits, without re-deriving the choice; the exact
    renders are pinned as literals in test_edge_rows_render_the_approved_titles.
    """
    from location_names import get_country_name, get_state_name
    for row in ROWS:
        t = facility_headline(*row)["title"]
        city = "" if is_placeholder_city(row[2] or "") else (row[2] or "")
        state, country = row[3] or "", row[4] or ""
        spelled = {get_state_name(state, "US") if state else "",
                   get_country_name(country) if country else ""} - {""}
        assert t.endswith(" | DC Hub"), f"{t!r} lost the brand"
        segs = t[: -len(" | DC Hub")].split(" · ")
        lead = segs[0]
        assert len(segs) <= 2, f"{t!r}: facility_headline carries no facts"
        assert " — " not in lead, f"old em-dash shape survived in {t!r}"
        # the ", ST, CC" tail is gone: it only ever appeared after the em-dash
        assert not lead.endswith(", US") and not lead.endswith(", GB")
        if len(segs) == 2:
            allowed = set(spelled)
            if city:
                allowed |= {city} | {f"{city}, {r}" for r in spelled}
            assert segs[1] in allowed, (
                f"{t!r}: location slot {segs[1]!r} is neither this row's city "
                f"nor its spelled region {sorted(allowed)}")
        if city:
            assert t.lower().count(city.lower()) == 1, (
                f"{t!r} prints the city {city!r} other than exactly once")
        elif spelled:
            assert len(segs) == 2, f"{t!r}: no real city, and the region is gone"
        assert "Regional" not in t, t


def test_the_country_tail_is_gone_where_it_used_to_be():
    """The 'ST, CC' tail was the bulk of the saving — prove it left."""
    h = facility_headline(*ROWS[0])
    assert ", NC, US" in h["dedup_title"]      # it was there
    assert ", NC, US" not in h["title"]        # and is not now


def test_every_sampled_title_gets_shorter_or_stays_equal():
    for row in ROWS:
        h = facility_headline(*row)
        assert len(h["title"]) <= len(h["dedup_title"]), (
            f"{h['title']!r} ({len(h['title'])}) is not shorter than "
            f"{h['dedup_title']!r} ({len(h['dedup_title'])})")


def test_site_code_lead_is_kept_and_city_is_not_lost():
    h = facility_headline("IAD86", "Amazon Web Services", "Chantilly", "VA", "US")
    assert h["title"].startswith("Amazon Web Services IAD86 ·")
    assert "Chantilly" in h["title"]
    assert h["h1"] == "Amazon Web Services IAD86 — Chantilly Data Center"


def test_placeholder_city_is_absent_but_the_country_still_publishes():
    """The floor tests/test_seo_index_hygiene.py holds, asserted here too.

    A first cut of this template keyed the slot on `city` alone, so a
    placeholder-city row rendered with NO location. The country is real.

    r-title-facts (2026-09-10): the country is SPELLED now, so the literal moved
    from " · CN" to " · China"; the invariant did not. It is anchored on the
    location segment, never on "China" anywhere — both rows carry "China" in
    their operator. And the one live row is asserted in both of its shapes,
    because they fail differently: with the operator in the provider column
    the site part is "Shanwei Data Center"; with it baked into the NAME
    (provider NULL, the live page) the site part itself says "China", so only
    the no-city floor keeps the country there.
    """
    for row in (("Shanwei Data Center", "China Telecom", "Regional", None, "CN"),
                ("China Telecom Shanwei Data Center", None, "Regional", None,
                 "CN")):
        h = facility_headline(*row)
        assert "Regional" not in h["title"]
        assert h["title"].endswith(" · China | DC Hub"), (row, h["title"])


# ── r-title-facts (2026-09-10): the display title moved again; the key did not ──

# Rows the NEW display title merges and the key must keep apart. Spelling a
# region out folds its two spellings ("IL" / "Illinois", "IE" / "Ireland")
# into one display string. ROWS has no such pair — its display titles are all
# distinct — so on ROWS alone a key that read `title` would partition exactly
# like one that reads `dedup_title`, and the partition test could not tell.
SPELLING_PAIRS = [
    ("Metro Data Center", "Acme", "Springfield", "IL", "US"),
    ("Metro Data Center", "Acme", "Springfield", "Illinois", "USA"),
    (None, None, "Dublin", None, "IE"),
    (None, None, "Dublin", None, "Ireland"),
]


def test_partition_holds_on_rows_the_display_title_merges():
    """identity_key partitions these exactly as the OLD title did, although the
    new display title merges them pairwise. The premise is pinned first, so
    this cannot go vacuous if the display ever stops merging them."""
    def part(keyfn):
        g = {}
        for r in SPELLING_PAIRS:
            g.setdefault(keyfn(r), []).append(r)
        return sorted((sorted(v, key=repr) for v in g.values()), key=repr)
    display = part(lambda r: facility_headline(*r)["title"])
    legacy = part(lambda r: (" ".join(facility_headline(*r)["h1"].split()).lower(),
                             " ".join(_legacy_title(*r).split()).lower()))
    assert len(display) == 2 and len(legacy) == 4, (display, legacy)
    assert part(lambda r: identity_key(*r)) == legacy


# The approved renders for ROWS — the owner-signed table in the drafts' §5,
# as literals, in ROWS order. No power, status or ISO: facility_headline()
# knows none of them.
APPROVED_EDGE_TITLES = [
    "Spectrum Charlotte National Data Center | DC Hub",
    "Equinix SV1/SV5/SV10 - Silicon Valley, San Jose | DC Hub",
    "Switch Tahoe Reno · McCarran, Nevada | DC Hub",
    "Amazon Web Services IAD86 · Chantilly, Virginia | DC Hub",
    "Quality Technology Services QTS Manassas DC2 | DC Hub",
    "NTT Hemel Hempstead 1 HH1 · United Kingdom | DC Hub",
    "Data Center · Dublin, Ireland | DC Hub",
    "China Telecom Shanwei Data Center · China | DC Hub",
    "Equinix FR5 · Frankfurt am Main, Germany | DC Hub",
    "Acme Metro Data Center · Springfield, Illinois | DC Hub",
    "Acme Metro Data Center · Springfield, Missouri | DC Hub",
    "Acme Metro Data Center · Springfield, Canada | DC Hub",
    "Data Center | DC Hub",
]


def test_edge_rows_render_the_approved_titles():
    assert len(APPROVED_EDGE_TITLES) == len(ROWS), "one approved title per row"
    got = [facility_headline(*r)["title"] for r in ROWS]
    wrong = [(r, g, w) for r, g, w in zip(ROWS, got, APPROVED_EDGE_TITLES)
             if g != w]
    assert not wrong, "\n".join(f"{r}: got {g!r}, approved {w!r}"
                                for r, g, w in wrong)
