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
    # and the DISPLAY title is genuinely the same for them — which is why the
    # key had to stop reading it.
    t = {facility_headline(*r)["title"] for r in ROWS[-4:-1]}
    assert len(t) == 1, f"expected one shared display title, got {t}"


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
    t = facility_headline(*ROWS[0])["title"]
    assert t == "Spectrum Charlotte National Data Center · Charlotte | DC Hub"


def test_title_is_composed_from_the_template_not_the_old_shape():
    """Structural, not substring.

    An earlier version of this test banned the literal " Data Center | DC Hub"
    and failed on `China Telecom Shanwei Data Center | DC Hub` — a facility
    genuinely NAMED "Shanwei Data Center", with no city, so the template
    legitimately renders `{lead} | DC Hub`. Banning a phrase that the DATA may
    contain tests the corpus, not the composition. Assert the shape instead.
    """
    for row in ROWS:
        h = facility_headline(*row)
        t, city = h["title"], (row[2] or "")
        if is_placeholder_city(city):
            city = ""
        # the slot is the most specific place we HAVE — city, else state/country
        loc = city or ", ".join([p for p in (row[3] or "", row[4] or "") if p])
        assert t.endswith(" | DC Hub"), f"{t!r} lost the brand"
        body = t[: -len(" | DC Hub")]
        if loc:
            assert body.endswith(f" · {loc}"), (
                f"{t!r} does not end in the template's ' · {{Location}}'")
            lead = body[: -len(f" · {loc}")]
        else:
            lead = body
        assert " — " not in lead, f"old em-dash shape survived in {t!r}"
        # the ", ST, CC" tail is gone: it only ever appeared after the em-dash
        assert not lead.endswith(", US") and not lead.endswith(", GB")


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
    """
    h = facility_headline("Shanwei Data Center", "China Telecom", "Regional",
                          None, "CN")
    assert "Regional" not in h["title"]
    assert h["title"].endswith(" · CN | DC Hub"), h["title"]
