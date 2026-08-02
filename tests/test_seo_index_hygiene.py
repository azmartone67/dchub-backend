"""SEO index-hygiene (2026-08-01) — pins for the four measured index defects.

The 08-01 diagnosis measured, across the 33,516-page indexed corpus:
  1. TITLE OPERATOR-DUPLICATION — SERP titles like "DataBank DataBank Dallas
     (DFW2)" / "Vantage Data Centers Vantage Berlin II": the profile template
     prepended the operator whenever the full provider string wasn't a
     substring of the name, missing shared-leading-brand and
     name-inside-provider cases.
  2. JUNK ANONYMOUS PAGES — "Data Center 343593591 — West Chicago" (numeric
     OSM ids as names) indexed with junk titles; they can never rank.
  3. SITEMAP JUNK — 'unknown-%' provider slugs + numeric-OSM names in the
     sitemap (~1,760 of 17,071 URLs).
  4. NON-CANONICAL SITEMAP ENTRIES — rows whose page rel=canonicals at a
     duplicate_of_id twin were still sitemap-emitted (guaranteed GSC
     "Alternate page with proper canonical").

Fixes are titles / robots-meta / sitemap-emission ONLY — slugs are FROZEN
(reference: slug freeze, 2026-07-03) and no test here may assert a slug
change. Behavior-tested where the code renders without a DB
(_render_profile), source-pinned where it doesn't (_build_sitemap_sections'
DB loop), in the same style as tests/test_api_slug_compose_delegation.py.
"""
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import routes.facility_profile_page as fpp  # noqa: E402

MAIN_SRC = (ROOT / "main.py").read_text()


def _fac(name, provider, slug="testco-test-facility-ab12cd34", **kw):
    d = {"name": name, "provider": provider, "canonical_slug": slug,
         "city": "Dallas", "state": "TX", "country": "USA"}
    d.update(kw)
    return d


# ── 1. title operator-duplication ────────────────────────────────────────

def test_title_measured_serp_duplications_gone():
    # the three SERP shapes measured 08-01 — each rendered the brand twice
    html = fpp._render_profile(_fac("DataBank Dallas (DFW2)", "DataBank, Ltd."), "x")
    assert "DataBank DataBank" not in html
    assert "DataBank Dallas (DFW2)" in html          # name itself intact

    html = fpp._render_profile(_fac("Vantage Berlin II", "Vantage Data Centers"), "x")
    assert "Vantage Data Centers Vantage" not in html
    assert "Vantage Berlin II" in html

    html = fpp._render_profile(
        _fac("Oso Grande Technologies", "Oso Grande Technologies, Inc."), "x")
    assert "Inc. Oso Grande" not in html


def test_title_still_prepends_genuinely_distinct_operator():
    # the operator is the strongest AI-crawler citation signal — a distinct
    # brand must STILL be prepended (r-geo-facility-title contract)
    html = fpp._render_profile(_fac("DC5 Ashburn", "Equinix"), "x")
    assert "<title>Equinix DC5 Ashburn" in html
    # generic first words are not a brand match
    assert fpp._brand_already_in_name("Data Foundry", "Data Center 343593591") is False


# ── 2. junk anonymous pages → noindex ────────────────────────────────────

def test_numeric_osm_junk_pages_noindex():
    html = fpp._render_profile(
        _fac("Data Center 343593591 — West Chicago", "Unknown"), "x")
    assert 'content="noindex"' in html
    # unknown-osm frozen slug (names were cleaned post-freeze; only the slug
    # still carries the marker)
    html = fpp._render_profile(
        _fac("Cleaned Name", "SomeCo", slug="unknown-osm-dc-343593591-ab12cd34"), "x")
    assert 'content="noindex"' in html
    # bare numeric name
    html = fpp._render_profile(_fac("343593591", "Unknown"), "x")
    assert 'content="noindex"' in html


def test_real_pages_still_index_follow():
    html = fpp._render_profile(_fac("DC5 Ashburn", "Equinix"), "x")
    assert 'content="index, follow"' in html
    # short building numbers are REAL facilities, not OSM junk
    html = fpp._render_profile(_fac("Data Center 3", "Equinix"), "x")
    assert 'content="index, follow"' in html


# ── 3+4. sitemap emission guards (source-pinned: the loop needs a DB) ────

def test_sitemap_junk_guard_widened_to_unknown_and_numeric():
    # loop guard covers ALL 'unknown-%' slugs (not just 'unknown-osm-')
    assert "full_slug.startswith('unknown-')" in MAIN_SRC
    assert "_junk_slug_re.search(full_slug)" in MAIN_SRC
    # both SQL emitters (discovered + legacy union) widened at source
    assert MAIN_SRC.count("NOT LIKE 'unknown-%'") == 2
    assert "NOT LIKE 'unknown-osm-%'" not in MAIN_SRC


def test_sitemap_junk_regex_behavior():
    m = re.search(r"_junk_slug_re = _re_junk\.compile\(\s*r'([^']+)'\)", MAIN_SRC)
    assert m, "numeric-OSM junk regex not found in main.py sitemap loop"
    rx = re.compile(m.group(1))
    # junk: numeric-OSM name classes
    assert rx.search("foo-data-center-343593591-west-chicago-ab12cd34")
    assert rx.search("data-center-3435935-ab12cd34")
    assert rx.search("provider-3435935911-ab12cd34")          # bare 8+ digit name
    # NOT junk: real facilities (incl. building numbers < 8 digits)
    assert not rx.search("databank-ltd-databank-minneapolis-msp1-8c8fb870")
    assert not rx.search("x-chicago-building-100200-ab12cd34")
    assert not rx.search("telehouse-75001-paris-ab12cd34")


def test_sitemap_emits_only_self_canonical():
    # the r-selfcanon guard: rows whose page canonicals at a duplicate_of_id
    # twin are skipped (the twin's own row carries the URL), fail-open
    assert "full_slug in _noncanon_slugs" in MAIN_SRC
    assert "t.id = d.duplicate_of_id" in MAIN_SRC
    # pre-bind so the swallow-and-continue except can't leave it unbound:
    # within the sitemap builder, the set is bound before the DB try opens
    region = MAIN_SRC[MAIN_SRC.index("def _build_sitemap_sections"):]
    assert region.index("_noncanon_slugs = set()") < region.index("conn = get_read_db()")


# ── 5. /state-of-power canonical markets figure ──────────────────────────

def test_state_of_power_markets_scored_floors_at_canon():
    import routes.state_of_power as sop
    # the leaderboard-length undercount (144) must never surface again —
    # floor is the canonical market count (canonical_stats, fallback 300)
    assert sop._gather()["summary"]["markets_scored"] >= 300
