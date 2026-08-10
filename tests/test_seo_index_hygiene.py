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


# ── 6. published news-NER spans → noindex + sitemap-excluded ─────────────
# r-ner-noindex (2026-08-09). PR #2490 closed the WRITE and left 61 pages
# live. They carry no signal in the name ("Copilot", "FERC", "GitHub") and
# none in the slug, so the discriminator is PROVENANCE — which makes the
# INGEST-ONLY evidence predicate usable, but ONLY behind a source filter.
# Unscoped it matches 139 `facilities` rows, 45 of them real OpenStreetMap
# facilities. Every test below exists to keep that fence standing.

# the exact live slugs measured 2026-08-09
_NER_LIVE_SLUGS = ("copilot-07a85c97", "ferc-ferc-9e0a2b63",
                   "github-github-00427fb3", "intel-intel-4b04b2b8",
                   "why-ot-security-can-a6a56478",
                   "home-rebusinessonline-home-rebusinessonline-06d30f34")
# real facilities the UNSCOPED evidence predicate also matches — the collateral
_REAL_ZERO_EVIDENCE = (("AiNET", "ainet-94dcccb8"),
                       ("CoreSite Reston Campus VA2",
                        "coresite-coresite-reston-campus-va2-f92cf9a2"),
                       ("Equinix Secaucus NY6",
                        "equinix-equinix-secaucus-ny6-176df7ed"))


class _FakeCursor:
    """Answers the module's own SQL — no DB, no main.py import."""

    def __init__(self, by_prong=None, fail=()):
        self._by_prong = by_prong or {}
        self._fail = set(fail)
        self._rows = []
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append(sql)
        # ★ Route by EXACT SQL → the module's own label. Every heuristic tried
        # here has silently mis-routed a prong and passed assertions on data
        # it never loaded: matching a bare "discovered_facilities" collided
        # with the drain prong's own source='discovered_facilities_drain',
        # and matching "FROM facilities" collides with the news_pipeline prong
        # added 2026-08-09 — two prongs now read the same table, so the FROM
        # clause cannot identify one. An exact lookup cannot drift, and a new
        # prong shows up as a KeyError here rather than as a green test.
        import util.facility_ner_noindex as _ner
        by_sql = {s: label for label, s in _ner.SUPPRESSION_QUERIES}
        assert sql in by_sql, "unrecognised prong SQL — update _FakeCursor"
        prong = by_sql[sql]
        if prong in self._fail:
            self._rows = []
            raise RuntimeError("column does not exist: " + prong)
        self._rows = [(s,) for s in self._by_prong.get(prong, ())]

    def fetchall(self):
        return self._rows


def _load_ner(by_prong, fail=()):
    import util.facility_ner_noindex as ner
    ner.reset_cache()
    cur = _FakeCursor(by_prong, fail)
    ner.refresh_suppressed_slugs(cur, force=True)
    return ner, cur


def _all_prongs():
    """Every label, read off the module — so "fail EVERY prong" stays true
    when a prong is added. Hard-coding the pair here is how the two
    total-failure tests below would quietly become one-prong-survives tests."""
    import util.facility_ner_noindex as ner
    return tuple(label for label, _ in ner.SUPPRESSION_QUERIES)


def test_ner_every_prong_is_provenance_scoped():
    """★ THE fence. A prong without `source =` is the 139-row unscoped query."""
    import util.facility_ner_noindex as ner
    assert ner.SUPPRESSION_QUERIES, "no prongs — the guard would be inert"
    for label, sql in ner.SUPPRESSION_QUERIES:
        assert "source = '" in sql, (
            f"prong {label} is not provenance-scoped — unscoped, the evidence "
            f"predicate de-indexes 45 real OpenStreetMap facilities")
        assert "canonical_slug" in sql, f"prong {label} selects no slug"


def test_ner_evidence_prong_checks_every_measured_field():
    """The predicate is only safe as measured — a dropped field widens it."""
    import util.facility_ner_noindex as ner
    for field in ("city", "address", "market", "latitude", "longitude",
                  "lat", "lon", "power_mw", "sqft"):
        assert field in ner.NO_EVIDENCE_SQL, f"evidence field dropped: {field}"
    # a lone '%' raises IndexError client-side in psycopg2, before the server
    assert "%" not in ner.NO_EVIDENCE_SQL


def test_ner_suppressed_slugs_noindex_and_real_ones_untouched():
    ner, _ = _load_ner({"news_ner": _NER_LIVE_SLUGS[:5],
                        "drain_no_evidence": _NER_LIVE_SLUGS[5:]})
    for slug in _NER_LIVE_SLUGS:
        assert ner.is_suppressed_slug(slug), f"not suppressed: {slug}"
        assert fpp._is_junk_facility("Copilot", slug) is True
        html = fpp._render_profile(_fac("Copilot", "Copilot", slug=slug), slug)
        assert 'content="noindex"' in html
    # ★ the collateral half — real coordinate-less facilities keep indexing
    for name, slug in _REAL_ZERO_EVIDENCE:
        assert not ner.is_suppressed_slug(slug)
        assert fpp._is_junk_facility(name, slug) is False
        html = fpp._render_profile(_fac(name, name, slug=slug), slug)
        assert 'content="index, follow"' in html


def test_ner_both_prongs_are_load_bearing():
    """Neither table's prong is a superset of the other's (measured 08-09)."""
    ner, _ = _load_ner({"news_ner": ("state-pauses-projects-over-x-03d74fcf",),
                        "drain_no_evidence": ("home-rebusinessonline-y-06d30f34",)})
    assert ner.suppressed_slugs() == frozenset(
        {"state-pauses-projects-over-x-03d74fcf",
         "home-rebusinessonline-y-06d30f34"})


# ── 6b. the OLDER news path: source='news_pipeline' ─────────────────────
# r-news-pipeline-noindex (2026-08-09). #2490/#2492 covered the NER promoter;
# news_facility_extractor (source='news_pipeline') writes the article TITLE as
# the facility name and was never covered. 3 pages were still index,follow.
#
# ★★ This source is a MIXED population — measured live 2026-08-09, 59 rows:
#    29 headlines and 30 REAL, well-evidenced facilities. That is the whole
#    difference from the news_ner prong, where provenance ALONE is the signal.
#    Here the evidence conjunct IS the discriminator, and a prong of
#    source='news_pipeline' alone de-indexes Stargate Abilene, NTT Frankfurt,
#    IREN Sweetwater and 27 more. The tests below hold both halves.

# the 3 live index,follow pages this prong exists for (measured 2026-08-09)
_NEWS_PIPELINE_HEADLINE_SLUGS = (
    "how-wisconsin-companies-are-benefitting-from-data-center-boom-"
    "urban-milwaukee-0d159289",
    "tech-giants-announce-7b-data-center-michigans-first-hyperscale-"
    "campus-bridge-michigan-aeb3a70d",
    "2026-global-data-center-outlook-jll-ad87ddfe",
)
# ★ REAL facilities on the SAME source — the collateral a provenance-only
#   prong would de-index. Live rows, live slugs, all with city + MW.
_NEWS_PIPELINE_REAL = (
    ("Stargate Abilene Phase 1",
     "stargate-openaisoftbankoracle-stargate-abilene-phase-1-e58a6566"),
    ("NTT Global Data Centers Frankfurt",
     "ntt-global-data-centers-ntt-global-data-centers-frankfurt-2b29a2c1"),
    ("IREN Sweetwater 1", "iren-iren-sweetwater-1-0e57affd"),
    ("Meta Beaver Dam WI", "meta-meta-beaver-dam-wi-eb690c24"),
    ("Vantage Johor Malaysia", "vantage-vantage-johor-malaysia-f2e6e669"),
)


def test_news_pipeline_prong_exists_and_is_evidence_fenced():
    """★★ THE fence for this prong. `source='news_pipeline'` alone is NOT a
    junk marker — 30 of its 59 live rows are real facilities."""
    import util.facility_ner_noindex as ner
    prong = dict((l, s) for l, s in ner.SUPPRESSION_QUERIES).get(
        "news_pipeline_no_evidence")
    assert prong, "the news_pipeline prong is gone — 3 headline pages re-index"
    assert "source = 'news_pipeline'" in prong
    assert ner.NO_EVIDENCE_SQL in prong, (
        "news_pipeline prong dropped the evidence test — provenance alone "
        "de-indexes 30 REAL facilities (Stargate Abilene, NTT Frankfurt, "
        "IREN Sweetwater 1, Meta Beaver Dam WI, Vantage Johor Malaysia)")


def test_every_facilities_table_prong_carries_the_evidence_test():
    """Generalised: on `facilities`, provenance is never sufficient on its own.
    `discovered_facilities` is the only table where a source IS the signal."""
    import util.facility_ner_noindex as ner
    for label, sql in ner.SUPPRESSION_QUERIES:
        if "FROM facilities" in sql:
            assert ner.NO_EVIDENCE_SQL in sql, (
                f"prong {label} reads `facilities` on provenance alone — that "
                f"table mixes real facilities into every news source")


def test_news_pipeline_headlines_noindex_and_its_real_rows_untouched():
    ner, _ = _load_ner(
        {"news_pipeline_no_evidence": _NEWS_PIPELINE_HEADLINE_SLUGS})
    for slug in _NEWS_PIPELINE_HEADLINE_SLUGS:
        assert ner.is_suppressed_slug(slug), f"still indexed: {slug}"
        html = fpp._render_profile(
            _fac("2026 Global Data Center Outlook - JLL", "JLL", slug=slug),
            slug)
        assert 'content="noindex"' in html
    # ★ the collateral half — the real rows on the same source keep indexing
    for name, slug in _NEWS_PIPELINE_REAL:
        assert not ner.is_suppressed_slug(slug), f"de-indexed a REAL row: {name}"
        assert fpp._is_junk_facility(name, slug) is False, name
        html = fpp._render_profile(_fac(name, name, slug=slug), slug)
        assert 'content="index, follow"' in html


def test_news_pipeline_prong_is_load_bearing_beside_the_other_two():
    """Zero overlap measured (62 prior slugs → 91). Neither existing prong
    reaches these three, so deleting this one silently re-indexes them."""
    ner, _ = _load_ner({"news_ner": _NER_LIVE_SLUGS[:2],
                        "drain_no_evidence": _NER_LIVE_SLUGS[2:],
                        "news_pipeline_no_evidence":
                            _NEWS_PIPELINE_HEADLINE_SLUGS})
    assert ner.suppressed_slugs() == frozenset(
        set(_NER_LIVE_SLUGS) | set(_NEWS_PIPELINE_HEADLINE_SLUGS))


def test_ner_one_failing_prong_does_not_cost_the_other():
    ner, _ = _load_ner({"news_ner": ("copilot-07a85c97",),
                        "drain_no_evidence": ("ferc-ferc-9e0a2b63",)},
                       fail=("drain_no_evidence",))
    assert ner.is_suppressed_slug("copilot-07a85c97")
    assert not ner.is_suppressed_slug("ferc-ferc-9e0a2b63")


def test_ner_total_failure_keeps_the_previous_set():
    """A DB blip must not silently flip 61 pages back to index,follow."""
    ner, _ = _load_ner({"news_ner": ("copilot-07a85c97",)})
    ner.refresh_suppressed_slugs(
        _FakeCursor({}, fail=_all_prongs()), force=True)
    assert ner.is_suppressed_slug("copilot-07a85c97")


def test_ner_total_failure_backs_off_instead_of_retrying_per_request():
    """Post-failure the cache is stale AND empty, so without a backoff every
    facility-page view would re-run two failing statements + two rollbacks —
    amplifying load exactly when the DB is already unwell."""
    ner, _ = _load_ner({}, fail=_all_prongs())
    assert ner.suppressed_slugs() == frozenset()
    probe = _FakeCursor({"news_ner": ("copilot-07a85c97",)})
    ner.refresh_suppressed_slugs(probe)          # inside the backoff window
    assert probe.executed == [], "refresh hammered the DB after a total failure"
    ner.refresh_suppressed_slugs(probe, force=True)   # force still overrides
    assert ner.is_suppressed_slug("copilot-07a85c97")


def test_ner_empty_cache_leaves_every_caller_at_prior_behaviour():
    """No cursor is ever supplied under tests/ — nothing may import main.py."""
    import util.facility_ner_noindex as ner
    ner.reset_cache()
    assert ner.suppressed_slugs() == frozenset()
    assert fpp._is_junk_facility("Copilot", "copilot-07a85c97") is False
    # the shipped name-shape predicate still fires on its own class
    assert fpp._is_junk_facility("Meta Unknown", "meta-meta-unknown-cbd8fdf3")


def test_ner_ingest_only_evidence_predicate_never_reaches_a_serve_path():
    """evidence_reject_reason is INGEST-ONLY — its docstring says so, and 45
    real facilities are the price of forgetting it. Neither the sitemap nor
    the profile renderer may import it."""
    fpp_src = (ROOT / "routes" / "facility_profile_page.py").read_text()
    for src, where in ((MAIN_SRC, "main.py"), (fpp_src, "facility_profile_page")):
        assert "evidence_reject_reason" not in src, (
            f"{where} reaches for the INGEST-ONLY evidence predicate")


def test_sitemap_skips_published_ner_slugs():
    assert "full_slug in _ner_junk_slugs" in MAIN_SRC
    assert "refresh_suppressed_slugs(c)" in MAIN_SRC
    region = MAIN_SRC[MAIN_SRC.index("def _build_sitemap_sections"):]
    # pre-bound before the DB try, same contract as _noncanon_slugs
    assert region.index("_ner_junk_slugs = set()") < region.index("conn = get_read_db()")
    # its own isolated try/except — NOT extra columns on the fac_rows SELECT
    assert region.index("_ner_junk_slugs = set(refresh_suppressed_slugs(c))") > 0


def test_serve_path_warms_the_set_before_the_row_lookup():
    """No cold window: _render_profile reads the set later in the SAME request,
    so a first hit on a junk page must not render index,follow."""
    fpp_src = (ROOT / "routes" / "facility_profile_page.py").read_text()
    region = fpp_src[fpp_src.index("def _fetch_facility_by_slug"):]
    assert region.index("refresh_suppressed_slugs(c)") < region.index("canonical_slug = %s")


# ── 5. /state-of-power canonical markets figure ──────────────────────────

def test_state_of_power_markets_scored_floors_at_canon():
    import routes.state_of_power as sop
    # the leaderboard-length undercount (144) must never surface again —
    # floor is the canonical market count (canonical_stats, fallback 300)
    assert sop._gather()["summary"]["markets_scored"] >= 300
