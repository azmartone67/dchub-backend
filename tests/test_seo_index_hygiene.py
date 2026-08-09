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


def _junk_rx():
    """Extract the live _junk_slug_re from main.py and compile it.

    ★ 2026-08-09: the old single-literal extractor (r'([^']+)') captured only
    the FIRST r'…' chunk. The pattern is now split across adjacent literals,
    so a one-literal grab would have silently tested the OLD regex and passed
    while the new alternative went unexercised. Concatenate every literal in
    the compile() call, and assert we actually found more than one.
    """
    m = re.search(r"_junk_slug_re = _re_junk\.compile\(\s*((?:r'[^']*'\s*)+)\)",
                  MAIN_SRC)
    assert m, "numeric-OSM junk regex not found in main.py sitemap loop"
    parts = re.findall(r"r'([^']*)'", m.group(1))
    assert len(parts) >= 2, (
        "expected the junk regex to be split across adjacent literals; got "
        f"{len(parts)} — the extractor may be silently reading a stale pattern")
    return re.compile("".join(parts))


def test_sitemap_junk_regex_behavior():
    rx = _junk_rx()
    # junk: numeric-OSM name classes
    assert rx.search("foo-data-center-343593591-west-chicago-ab12cd34")
    assert rx.search("data-center-3435935-ab12cd34")
    assert rx.search("provider-3435935911-ab12cd34")          # bare 8+ digit name
    # NOT junk: real facilities (incl. building numbers < 8 digits)
    assert not rx.search("databank-ltd-databank-minneapolis-msp1-8c8fb870")
    assert not rx.search("x-chicago-building-100200-ab12cd34")
    assert not rx.search("telehouse-75001-paris-ab12cd34")


def test_sitemap_junk_guard_catches_trailing_unknown_name_token():
    """r-junk-suffix (2026-08-09): '<provider>-<name>-unknown-<hash8>'.

    The 08-01/08-02 guard was PREFIX-anchored ('unknown-%'), which only sees
    the provider-ingested-as-Unknown family. 43 live URLs measured 2026-08-09
    carried 'unknown' as the NAME's last token and passed straight through.
    """
    rx = _junk_rx()
    # the exact live slugs measured 2026-08-09 (all first_seen 2026-05-04)
    for s in ("meta-meta-unknown-cbd8fdf3",
              "lithuania-lithuania-unknown-10e4524e",
              "equinix-equinix-unknown-f9fa0574",
              "energy-secretary-energy-secretary-unknown-35dfc99c",
              "equinor-expands-brazil-renewables-with-"
              "equinor-expands-brazil-renewables-with-unknown-a43654b6"):
        assert rx.search(s), f"junk slug not caught: {s}"


def test_sitemap_junk_guard_spares_real_facilities():
    """The anti-regression half — this is where a wide filter eats real pages.

    'data-center-<6+ digits>' looks like OSM junk but matches 25 REAL live
    slugs whose frozen 8-hex identity hash merely BEGINS with 6+ digits
    (equinix-atlanta-data-center-144841dc). The regex anchors the digit run to
    the NAME, never to the hash — keep it that way.
    """
    rx = _junk_rx()
    for s in ("equinix-atlanta-data-center-144841dc",
              "microsoft-corp-lvl-data-center-8024768c",
              "ibm-annapolis-data-center-4498886f",
              "qts-realty-qts-richmond-data-center-3407830a",
              # real facility codes with long digit runs
              "aws-amazon-web-services-dub105051-1a2b3c4d",
              "flexential-flexential-raleigh-ral010203-1a2b3c4d",
              # 'unknown' elsewhere in the name is NOT the junk shape
              "acme-unknown-harbor-campus-ab12cd34"):
        assert not rx.search(s), f"real facility would be dropped: {s}"


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
