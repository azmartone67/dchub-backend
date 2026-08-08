"""GUARD — the /dcpi index Dataset must not declare itself United States.

The defect, verified live 2026-08-08 on https://dchub.cloud/dcpi:

    "spatialCoverage": {"@type": "Place", "name": "United States"}

a flat literal, on the Dataset node for an index that ranks 300+ markets across
~40 countries. Same class as PR #2389 (which fixed the per-market embed), one
level up and on the flagship page — and the page already had cov_countries in
its own template context while making the claim.

Pure — no DB, no Flask app.
"""
import json
import os
import re

import pytest

import routes.dcpi as dcpi

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _rows(*specs):
    return [{"market_slug": s, "state": st, "iso": iso} for s, st, iso in specs]


def test_a_mixed_index_does_not_claim_one_country():
    out = dcpi.dcpi_index_spatial_coverage(_rows(
        ("ashburn", "VA", "PJM"),
        ("dallas", "TX", "ERCOT"),
        ("tokyo", "JP", "TEPCO"),
        ("frankfurt", "DE", "ENTSOE-DE"),
        ("london", "UK", "NGESO"),
    ))
    countries = {p["addressCountry"] for p in out}
    assert countries == {"US", "JP", "DE", "GB"}
    assert len(out) == 4, "one Place per distinct country, deduped"


def test_it_is_a_list_of_places_not_a_single_named_place():
    out = dcpi.dcpi_index_spatial_coverage(_rows(("tokyo", "JP", "TEPCO")))
    assert isinstance(out, list)
    assert out[0]["@type"] == "Place"
    assert out[0]["addressCountry"] == "JP"
    assert "name" not in out[0], "a bare name is what the literal did"


def test_it_uses_the_same_resolver_as_the_per_market_blocks():
    """Index and markets must not disagree about where a market is."""
    rows = _rows(("johannesburg", "GP", ""), ("perth", "WA", "AEMO"))
    out = {p["addressCountry"] for p in dcpi.dcpi_index_spatial_coverage(rows)}
    assert out == {"ZA", "AU"}
    for slug, state, iso in (("johannesburg", "GP", ""), ("perth", "WA", "AEMO")):
        assert dcpi._market_country(state, iso, slug) in out


def test_output_is_deterministic_and_json_serialisable():
    rows = _rows(("b", "JP", "TEPCO"), ("a", "VA", "PJM"), ("c", "DE", "ENTSOE-DE"))
    a = dcpi.dcpi_index_spatial_coverage(rows)
    b = dcpi.dcpi_index_spatial_coverage(list(reversed(rows)))
    assert a == b, "order must not depend on row order"
    json.dumps(a)


def test_no_resolvable_country_omits_the_property_rather_than_guessing():
    assert dcpi.dcpi_index_spatial_coverage([]) is None
    assert dcpi.dcpi_index_spatial_coverage(
        _rows(("atlantis", "ZZ", "NOBODY"))) is None


def test_a_us_only_index_still_says_us():
    out = dcpi.dcpi_index_spatial_coverage(_rows(
        ("ashburn", "VA", "PJM"), ("dallas", "TX", "ERCOT")))
    assert out == [{"@type": "Place", "addressCountry": "US"}]


def _index_template():
    """The DCPI_INDEX_TEMPLATE body. Scoped deliberately: this module's prose
    QUOTES the old literal to explain the fix, and a whole-file grep would
    match that explanation — guarding nothing while looking strict."""
    src = open(os.path.join(ROOT, "routes/dcpi.py"), encoding="utf-8").read()
    i = src.index('DCPI_INDEX_TEMPLATE = """')
    j = src.index('"""', i + len('DCPI_INDEX_TEMPLATE = """'))
    body = src[i:j]
    assert len(body) > 5000, "template extraction is too small to be the page"
    return body


def test_the_literal_is_gone_from_the_index_template():
    tpl = _index_template()
    assert '"name": "United States"' not in tpl
    assert '"spatialCoverage": {"@type": "Place", "name": "United States"}' not in tpl


def test_the_extraction_would_catch_the_literal_if_it_came_back():
    """A scoped locator is only worth something if it can still fail."""
    tpl = _index_template()
    assert "spatialCoverage" in tpl, "locator is stale — the block moved"
    assert '"name": "United States"' in (
        tpl + '"spatialCoverage": {"@type": "Place", "name": "United States"},')


def test_the_template_renders_the_derived_list():
    """Render the actual template fragment — a variable that is passed but
    never used would satisfy a source grep."""
    import jinja2
    src = open(os.path.join(ROOT, "routes/dcpi.py"), encoding="utf-8").read()
    m = re.search(r'(\{% if spatial_coverage %\}"spatialCoverage".*?\{% endif %\})',
                  src, re.S)
    assert m, "index spatialCoverage block not found — locator stale"
    tpl = jinja2.Template(m.group(1))
    rendered = tpl.render(spatial_coverage=[
        {"@type": "Place", "addressCountry": "US"},
        {"@type": "Place", "addressCountry": "JP"}])
    assert '"addressCountry": "JP"' in rendered
    assert "United States" not in rendered
    # …and it emits nothing at all when there is nothing to say.
    assert tpl.render(spatial_coverage=None).strip() == ""


# ── r-index-coverage-precap: the tier slice must not shrink the claim ───────
# #2393 replaced the literal but computed the coverage at the RENDER call, by
# which point `rows` has been rebound to the anonymous 25-card teaser. That
# teaser is all-US, so the live page still said "United States" — derived
# rather than literal, and identically wrong on the crawlable surface. The
# tests above all passed while that shipped, because none of them exercised
# the ORDER of the two operations. These do.

def _index_fn_src():
    src = open(os.path.join(ROOT, "routes/dcpi.py"), encoding="utf-8").read()
    i = src.index("    _index_cov = _dcpi_index_coverage(rows)")
    j = src.index("    resp = Response(html, mimetype=\"text/html\")", i)
    body = src[i:j]
    assert len(body) > 1000, "index-handler slice too small to be the real body"
    return body


def test_coverage_is_computed_before_the_tier_slice_rebinds_rows():
    body = "\n".join(l for l in _index_fn_src().splitlines()
                     if not l.strip().startswith("#"))
    assert body.strip(), "comment-stripping ate the handler body"
    call = body.index("dcpi_index_spatial_coverage(rows)")
    # The anon teaser rebinds `rows` to 5 BUILD + 20 others.
    rebind = body.index("rows = _builds + _others")
    assert call < rebind, (
        "spatialCoverage is computed AFTER the tier slice — an anonymous "
        "viewer (and every crawler) gets coverage derived from the 25-card "
        "teaser, not from the dataset")


def test_the_render_does_not_recompute_from_the_sliced_rows():
    body = _index_fn_src()
    assert "spatial_coverage=_spatial_cov" in body
    assert body.count("dcpi_index_spatial_coverage(rows)") == 1, (
        "computed more than once — the second call sees the sliced rows")


def test_a_us_only_slice_of_a_mixed_index_would_understate_coverage():
    """Why the ordering matters, demonstrated on the function itself: the anon
    teaser takes 5 BUILD + 20 others, which can be entirely US even when the
    index spans four countries."""
    full = _rows(("ashburn", "VA", "PJM"), ("dallas", "TX", "ERCOT"),
                 ("tokyo", "JP", "TEPCO"), ("frankfurt", "DE", "ENTSOE-DE"),
                 ("london", "UK", "NGESO"))
    teaser = full[:2]                      # a plausible all-US slice
    assert len(dcpi.dcpi_index_spatial_coverage(full)) == 4
    assert dcpi.dcpi_index_spatial_coverage(teaser) == [
        {"@type": "Place", "addressCountry": "US"}]
