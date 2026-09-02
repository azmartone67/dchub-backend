"""Operator site-code titles on facility pages (2026-09-02, QA sweep
expansion #1 in findings/3_seo.md).

Measured (28d GSC query grain, 2026-08-02..29): "interxion mad1" pos 7.4,
"iad14 data center" 10.2, "fra28 data center" 10.4, "htl05" 10.7,
"digitalrealty ewr12 piscataway" 10.9, "ewr10" 7.7, "dus2" 12.5 — each
13–37 impressions, 0 clicks. Live title for the FR5 page on 2026-09-02:
"Equinix FR5 - Frankfurt, KleyerStrasse — Frankfurt, DE Data Center |
ENTSOE-DE grid | DC Hub".

Guard: 5 positives + 5 negatives on the detector, the headline composer, and
the RENDERED page (title, h1, og:title) with `main` stubbed the way
test_crossover_onramp does. Every assertion is mutation-verified (PR body).
"""
import re
import sys
import types

import pytest

if "main" not in sys.modules:
    sys.modules["main"] = types.SimpleNamespace(
        get_read_db=lambda: None, get_db=lambda: None)

from util.facility_site_code import (   # noqa: E402
    detect_site_code, site_code_headline, DENY_PREFIXES)


POSITIVES = [
    # name, provider, city, expected code, expected headline
    ("Equinix FR5 - Frankfurt, KleyerStrasse", "Equinix", "Frankfurt",
     "FR5", "Equinix FR5 — Frankfurt Data Center"),
    ("Interxion MAD1", "Interxion", "Madrid",
     "MAD1", "Interxion MAD1 — Madrid Data Center"),
    ("Digital Realty IAD14", "Digital Realty", "Ashburn",
     "IAD14", "Digital Realty IAD14 — Ashburn Data Center"),
    ("Piscataway EWR12", "Digital Realty", "Piscataway",
     "EWR12", "Digital Realty EWR12 — Piscataway Data Center"),
    ("DataBank Dallas (DFW2)", "DataBank", "Dallas",
     "DFW2", "DataBank DFW2 — Dallas Data Center"),
    ("Interxion DUS2", "Digital Realty", "Düsseldorf",
     "DUS2", "Digital Realty Interxion DUS2 — Düsseldorf Data Center"),
    ("HTL05", "Equinix", "Hartlepool",
     "HTL05", "Equinix HTL05 — Hartlepool Data Center"),
]

NEGATIVES = [
    # name, provider, city — no code must be detected
    ("Building 3", "Meta", "Prineville"),
    ("Phase 2 Data Center", "Vantage", "Ashburn"),
    ("US1 Data Center", "ColoCo", "Miami"),               # US Route 1
    ("Data Center DC1", "Foo", "Bar"),                    # "Data Center 1"
    ("Equinix FR5 and FR8 campus", "Equinix", "Frankfurt"),  # two codes = campus
    ("Google Data Center Council Bluffs", "Google", "Council Bluffs"),
    ("AWS us-east-1", "Amazon Web Services", "Ashburn"),  # lower-case, hyphen
    ("Google Cloud US-EAST5", "Google", "Columbus"),      # compass region
    ("Node Pole SE1", "Node Pole", "Luleå"),              # price zone
    ("SH130 Corridor Site", "X", "Austin"),               # state highway
    ("Data Center 343593591", "", "West Chicago"),        # OSM junk id
    ("iad14 data center", "Digital Realty", "Ashburn"),   # lower-case never
]


@pytest.mark.parametrize("name,provider,city,code,headline", POSITIVES)
def test_detects_code_and_composes_headline(name, provider, city, code, headline):
    assert detect_site_code(name) == code
    assert site_code_headline(name, provider, city) == headline


@pytest.mark.parametrize("name,provider,city", NEGATIVES)
def test_rejects_non_codes(name, provider, city):
    assert detect_site_code(name) is None, name
    assert site_code_headline(name, provider, city) is None, name


def test_headline_needs_a_city_and_an_operator():
    """No city → no "<City> Data Center"; an unknown operator ("Operator"
    placeholder) with no brand in the name → nothing to lead with."""
    assert site_code_headline("Equinix FR5", "Equinix", "") is None
    assert site_code_headline("Equinix FR5", "Equinix", None) is None
    assert site_code_headline("HTL05", "Operator", "Hartlepool") is None
    assert site_code_headline("HTL05", "", "Hartlepool") is None
    # a brand in the name is enough when the provider column is empty
    assert site_code_headline("Equinix FR5", "", "Frankfurt") == \
        "Equinix FR5 — Frankfurt Data Center"


def test_deny_list_covers_the_documented_ambiguities():
    for pfx in ("US", "SH", "DC", "NO", "SE", "EU", "AI", "MW", "EAST"):
        assert pfx in DENY_PREFIXES, pfx


# ── the rendered page ────────────────────────────────────────────────

BASE = {
    "id": 4242, "state": "", "country": "DE", "region": None,
    "latitude": 50.11, "longitude": 8.68, "power_mw": 12,
    "status": "active", "address": "Kleyerstrasse 88",
}


def _render(name, provider, city):
    import routes.facility_profile_page as fpp
    fac = dict(BASE, name=name, provider=provider, city=city)
    return fpp._render_profile(fac, "equinix-equinix-fr5-3366f937")


def _title(html):
    return re.search(r"<title>(.*?)</title>", html, re.S).group(1)


def _h1(html):
    return re.search(r"<h1>(.*?)</h1>", html, re.S).group(1)


def _og(html):
    return re.search(r'property="og:title" content="(.*?)"', html).group(1)


def test_rendered_page_leads_with_operator_and_code():
    html = _render("Equinix FR5 - Frankfurt, KleyerStrasse", "Equinix", "Frankfurt")
    assert _title(html).startswith("Equinix FR5 — Frankfurt Data Center | "), _title(html)
    assert _title(html).endswith("| DC Hub")
    assert _h1(html) == "Equinix FR5 — Frankfurt Data Center"
    assert _og(html) == "Equinix FR5 — Frankfurt Data Center"
    # the slug/canonical is NOT derived from the headline
    assert 'rel="canonical" href="https://dchub.cloud/facilities/equinix-equinix-fr5-3366f937"' in html
    # JSON-LD keeps the real facility name
    assert '"name": "Equinix FR5 - Frankfurt, KleyerStrasse"' in html


def test_rendered_page_without_a_code_is_byte_identical_to_the_legacy_title():
    html = _render("Google Data Center Council Bluffs", "Google", "Council Bluffs")
    assert _title(html).startswith(
        "Google Data Center Council Bluffs — Council Bluffs, DE Data Center | ")
    assert _h1(html) == "Google Data Center Council Bluffs"
    assert _og(html) == "Google Data Center Council Bluffs — Data Center"


def test_rendered_page_with_two_codes_keeps_the_legacy_title():
    html = _render("Equinix FR5 and FR8 campus", "Equinix", "Frankfurt")
    assert _h1(html) == "Equinix FR5 and FR8 campus"
    assert "FR5 — Frankfurt Data Center" not in _title(html)
