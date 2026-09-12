#!/usr/bin/env python3
"""tests/test_sitemap_no_duplicate_selfcanon.py — THE SITEMAP CONTRACT:
one facility, one self-canonical URL.

NO NETWORK, NO DB. The shipped `_build_sitemap_sections` is pulled out of
main.py and EXECUTED against a stub cursor, so these tests fail on a behaviour
regression, not on a phrase moving in a comment.

WHAT WENT WRONG (measured 2026-09-07 against the live /sitemap.xml, 8 shards,
23,094 facility URLs, each URL resolved back to the row that serves it)
---------------------------------------------------------------------------
    3,989 groups of >=2 URLs rendered a byte-identical <h1> AND <title>
    4,007 surplus URLs
    composition: discovered+legacy 3,893 · discovered+discovered 82 · other 14
    every member: HTTP 200, "index, follow", rel=canonical AT ITSELF
    7,665 of 7,996 members carried duplicate_of_id IS NULL

Live pair, both submitted to Google:
    /facilities/007-hebergement-paris-a8b78433   discovered_facilities
    /facilities/007-hebergement-paris-d128fc26   facilities (drain copy)

CAUSE: /api/v1/admin/dedup/drain INSERTs a `facilities` row for a
discovered_facilities row and stamps merged_facility_id — it does not suppress
the discovered row. Both get a frozen slug; the slug hashes provider|name and
the two tables disagree about `provider`, so the hash8s differ. The 2026-07-01
legacy union's overlap guard dedups on the SLUG, which is the one thing a drain
fork does not share.

★ The rendered <h1>/<title> is the only honest identity for "same facility":
  the slug is not (it hashes a field the pair disagrees about) and the id is
  not (discovered_facilities.id is INTEGER, facilities.id is TEXT — two id
  spaces). util/facility_headline holds the ONE copy of that composition;
  routes/facility_profile_page._render_profile and routes/facility_dedup_v4
  both call it, so a detector can never score a mirror of the page.

★ EVERY TEST HERE WAS MUTATION-VERIFIED (see the module's final docstring
  note): with the r-drain-fork skip removed from main.py the duplicate test
  fails with the fork pair listed by slug, and with the identity fold removed
  it fails too. A guard that has never been seen to fail is not a guard.
"""
import ast
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from util.facility_headline import identity_key  # noqa: E402

SRC = os.path.join(ROOT, "main.py")


# ── the stub database ────────────────────────────────────────────────────
#
# `_build_sitemap_sections` issues 14 SELECTs. Only the facility ones matter
# here; everything else answers [] so the markets/dcpi/press/pocket sections
# come back empty and the facility set is the whole artefact under test.

# ── r-twin-pointer (2026-09-07) — the OTHER cross-table class ────────────
# An independently-ingested legacy row (PeeringDB here, as 318 of the 374 live
# pairs are) that the drain NEVER touched: no merged_facility_id, so the map
# above cannot see it. It renders the same <h1>+<title> as a discovered row AND
# carries the same name, and routes/facility_dedup_v4 has written the link into
# facilities.discovered_twin_id.
DISCOVERED_TWIN = ("Equinix FR5", "Equinix", "Frankfurt", None, "DE",
                   900, "2026-07-15", "equinix-fr5-aaaaaaaa")
LEGACY_TWIN = ("Equinix FR5", "Equinix", "Frankfurt", None, "DE",
               "peeringdb-1234", "2026-07-15", "equinix-fr5-bbbbbbbb")
# facilities.canonical_slug -> discovered.canonical_slug, via discovered_twin_id
TWIN_LINK = [("equinix-fr5-bbbbbbbb", "equinix-fr5-aaaaaaaa")]

# (name, provider, city, state, country, id, first_seen, canonical_slug)
DISCOVERED = [
    # the keeper of a drain fork — provider NULL, exactly as the live rows are
    ("007 Hebergement Paris", None, "Paris", None, "FR",
     12300071, "2026-08-30", "007-hebergement-paris-a8b78433"),
    # an ordinary facility with no twin at all — the control. If a change here
    # ever drops this URL, the sitemap is losing real pages, not duplicates.
    ("Equinix DC5", "Equinix", "Ashburn", "VA", "US",
     500, "2026-08-01", "equinix-dc5-11111111"),
    DISCOVERED_TWIN,
]

# the legacy row the drain forked off row 12300071. Same rendered identity
# (provider == name collapses via brand_already_in_name), different hash8.
LEGACY = [
    ("007 Hebergement Paris", "007 Hebergement Paris", "Paris", None, "FR",
     "007-hebergement-paris-paris-fr", "2026-08-30",
     "007-hebergement-paris-d128fc26"),
    LEGACY_TWIN,
]

# discovered.merged_facility_id -> facilities.id, i.e. the drain's own stamp
DRAIN_LINK = [("007-hebergement-paris-d128fc26", "007-hebergement-paris-a8b78433")]

# ── r-junk-keeper (2026-09-07) ───────────────────────────────────────────
# A drain fork whose KEEPER wears a junk 'unknown-%' slug. Measured live: 63
# such pairs, 0 of the keepers published, 60 of the 63 alternates published —
# so the canonical pointed a PUBLISHED page at an UNADVERTISED slug. The
# keeper query must refuse it, which leaves the legacy page self-canonical.
JUNK_KEEPER = ("Shb Lljn Ltby", None, "Taipei", None, "TW",
               777, "2026-08-20", "unknown-shb-lljn-ltby-9cb60fd9")
JUNK_ALT = ("Shb Lljn Ltby", "Shb Lljn Ltby", "Taipei", None, "TW",
            "legacy-shb", "2026-08-20", "shb-lljn-ltby-55880373")

# ── r-junk-hash8 (2026-09-07) — THE FALSE POSITIVE IN THE JUNK FILTER ────
# The frozen slug is <provider-slug>-<name-slug>-<hash8>. A facility literally
# NAMED "… Data Center" yields '…-data-center-<hash8>', and (10/16)^8 ≈ 2.3% of
# hash8 values are all decimal digits — indistinguishable, to a pattern that
# only asks for 6+ digits, from an OSM node id. Measured 2026-09-07 over the
# 21,068 live discovered slugs: the old pattern matched 695, the anchored one
# matches 674, and the 21 it freed are REAL facilities (every one has a name and
# a city; every one of the 674 that remain is named literally
# "Data Center <digits>" and carries no power_mw).
#
# ★ The three rows below are LIVE slugs, pinned as literals: the real facility
#   the filter was eating, the OSM junk it must keep eating, and the junk shape
#   where the node id is followed by a CITY before the hash8 — that last one is
#   why the anchor allows name text between the digit run and the hash8.
REAL_DIGIT_HASH = ("Meta Rosemount Data Center", "Meta", "Rosemount", "MN",
                   "US", 8157, "2026-03-18",
                   "meta-meta-rosemount-data-center-45882878")
OSM_NUMERIC_JUNK = ("Data Center 32538035", None, "Chicago", None, "US",
                    999001, "2026-05-04", "data-center-32538035-fdf34756")
OSM_NUMERIC_JUNK_CITY = ("Data Center 343593591 West Chicago", None,
                         "West Chicago", None, "US", 999002, "2026-05-04",
                         "data-center-343593591-west-chicago-ab12cd34")


class _Cur:
    """Answers only the queries the facility path needs; [] for the rest."""

    def __init__(self, drain_link=DRAIN_LINK, legacy=LEGACY,
                 discovered=DISCOVERED, twin_link=TWIN_LINK, evidence=None,
                 capacity=None):
        self._rows = []
        self.drain_link = drain_link
        self.twin_link = twin_link
        self.legacy = legacy
        self.discovered = discovered
        # r-thin-sitemap: {canonical_slug: power_mw}. Default None = "every row
        # has capacity", i.e. the gate is INERT and every test written before
        # this parameter existed keeps testing exactly what it tested. Pass a
        # map to make the capacity gate observable (see
        # tests/test_sitemap_publishes_only_served_selfcanonical_urls.py).
        self.capacity = capacity
        # r-noindex-coherence: (canonical_slug, city, address, lat, lng, mw)
        # rows for the contentless-set query. Default: every fixture slug
        # carries a city, i.e. nothing is contentless and the guard is inert —
        # so every OTHER test in this file keeps testing what it tested.
        self.evidence = (evidence if evidence is not None else
                         [(r[7], "Paris", None, None, None, None)
                          for r in list(discovered) + list(legacy)])
        self.seen = []

    def _gated(self, rows, low):
        """Apply _thin_excl the way Postgres would. The clause is literally
        `AND COALESCE(power_mw, 0) > 0`, so keying on that text is reading the
        SQL under test, not re-implementing a policy."""
        if self.capacity is None or "coalesce(power_mw, 0) > 0" not in low:
            return list(rows)
        return [r for r in rows if (self.capacity.get(r[7]) or 0) > 0]

    def _unjunked(self, pairs, low):
        """Apply the keeper query's junk predicate. The SQL emits it from
        routes.facility_dedup_v4.junk_slug_sql; this calls that module's Python
        twin, which test_the_junk_slug_predicate_agrees_with_is_junk_slug pins
        as classifying identically — so the stub cannot drift from the SQL.

        ★ Without this the drain arm answered junk keepers too, and
        test_a_junk_slug_keeper_is_refused_and_the_legacy_url_stays passed only
        because the old `keeper in seen_slugs` escape hatch happened to hold the
        URL in. It asserted the refusal and observed something else.
        """
        if "!~ '^unknown-'" not in low:
            return list(pairs)
        from routes.facility_dedup_v4 import is_junk_slug
        return [p for p in pairs if not is_junk_slug(str(p[1]))]

    def execute(self, sql, params=None):
        q = " ".join(str(sql).split())
        self.seen.append(q)
        low = q.lower()
        if "information_schema.columns" in low:
            self._rows = [(1,)]                      # canonical_slug exists
        elif "from discovered_facilities" in low and "select name, provider" in low:
            self._rows = self._gated(self.discovered, low)
        elif low.startswith("select name, provider") and "from facilities" in low:
            self._rows = self._gated(self.legacy, low)
        elif "join discovered_facilities d on d.merged_facility_id = f.id" in low:
            # _drained_twin_slugs, drain arm
            self._rows = self._unjunked(self.drain_link, low)
        elif "select canonical_slug, city, address" in low:
            self._rows = list(self.evidence)          # r-noindex-coherence
        elif "join discovered_facilities d on d.id = f.discovered_twin_id" in low:
            self._rows = list(self.twin_link)        # _drained_twin_slugs, twin arm
        else:
            self._rows = []
        return self

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Conn:
    def __init__(self, cur):
        self._cur = cur

    def cursor(self):
        return self._cur

    def rollback(self):
        pass

    def close(self):
        pass


class _Log:
    def __getattr__(self, _):
        return lambda *a, **k: None


def _run_builder(cur, thin_gate=False):
    """Execute the SHIPPED _build_sitemap_sections against `cur`.

    thin_gate=False (the default, and what every test here wants) disables the
    capacity gate, because the fixtures carry no power_mw. Pass True together
    with _Cur(capacity=...) to run the GATED build and compare the two sets."""
    src = open(SRC, encoding="utf-8").read()
    i = src.index("def _build_sitemap_sections(")
    j = src.index("\ndef ", i + 100)
    fn = src[i:j]
    ast.parse(fn)                       # the source we run must be the source

    ns = {
        "os": os,
        "logger": _Log(),
        "get_read_db": lambda: _Conn(cur),
        "_build_sitemap_facilities_ungated": lambda: [],
        "_SITEMAP_THIN_GATE_FLOOR": 0,
        "_SITEMAP_PROVEN_MIN_IMPRESSIONS": 10,
        "_SITEMAP_PROVEN_CAP": 9000,
        "_POCKET_SITEMAP_CEILING": 0,
        "refresh_suppressed_slugs": lambda *a, **k: None,
        "__name__": "main_stub",
    }
    exec(compile(fn, SRC, "exec"), ns)
    prev = os.environ.get("SITEMAP_THIN_GATE_DISABLE")
    if thin_gate:
        os.environ.pop("SITEMAP_THIN_GATE_DISABLE", None)
    else:
        os.environ["SITEMAP_THIN_GATE_DISABLE"] = "1"  # fixtures carry no power_mw
    try:
        return ns["_build_sitemap_sections"]()
    finally:
        if prev is None:
            os.environ.pop("SITEMAP_THIN_GATE_DISABLE", None)
        else:
            os.environ["SITEMAP_THIN_GATE_DISABLE"] = prev


_LOC = re.compile(r"<loc>https://dchub\.cloud/facilities/([^<]+)</loc>")


def _facility_slugs(sections):
    out = []
    for entry in sections.get("facilities", []):
        m = _LOC.search(entry)
        if m:
            out.append(m.group(1))
    return out


def _identity_of(slug, cur):
    """The (h1, title) the page at `slug` will render — from the same row the
    /facilities/<slug> lookup would resolve (discovered first, then legacy)."""
    for r in cur.discovered:
        if r[7] == slug:
            return identity_key(r[0], r[1], r[2], r[3], r[4])
    for r in cur.legacy:
        if r[7] == slug:
            return identity_key(r[0], r[1], r[2], r[3], r[4])
    raise AssertionError(f"emitted slug {slug} matches no fixture row")


def _duplicate_groups(slugs, cur):
    """slug list -> {identity: [slugs]} for identities with >=2 URLs."""
    seen = {}
    for s in slugs:
        seen.setdefault(_identity_of(s, cur), []).append(s)
    return {k: v for k, v in seen.items() if len(v) > 1}


# ── the guard ────────────────────────────────────────────────────────────

def test_published_sitemap_has_no_two_selfcanonical_urls_for_one_facility():
    """THE CONTRACT. Every URL in the facility sections is self-canonical (none
    of these fixtures carries a duplicate_of_id), so two URLs rendering one
    identity means two self-canonical URLs for one facility."""
    cur = _Cur()
    slugs = _facility_slugs(_run_builder(cur))
    assert slugs, "builder emitted no facility URLs — the stub stopped matching"
    dupes = _duplicate_groups(slugs, cur)
    assert not dupes, (
        "the sitemap publishes two self-canonical URLs for one facility: "
        + "; ".join(f"{k[0]!r} -> {v}" for k, v in dupes.items()))


def test_the_builder_actually_ran_the_drained_twin_query():
    """A FLOOR on the guard itself. Every test above passes trivially if the
    legacy row never reaches the emit loop — an empty artefact has no
    duplicates. This pins that the shipped builder issued the r-drain-fork
    lookup AND unioned the legacy table, so "no duplicates" is a verdict and
    not an absence."""
    cur = _Cur()
    _run_builder(cur)
    joined = " || ".join(cur.seen).lower()
    assert "join discovered_facilities d on d.merged_facility_id = f.id" in joined, \
        "builder never issued the drained-twin lookup"
    assert any(q.lower().startswith("select name, provider")
               and "from facilities" in q.lower() for q in cur.seen), \
        "builder never unioned the legacy facilities table"


def test_a_legacy_page_canonicalises_to_its_discovered_twin():
    """The other half of the consolidation: the dropped URL still serves 200
    and must point at the keeper, so Google MERGES the two rather than merely
    losing one from the sitemap. Suppression deletes a page; a canonical
    merges it.

    _drained_twin_url is stubbed because it reads the DB — what is under test
    is that _render_profile CONSULTS it for a legacy-served row at all."""
    import routes.facility_profile_page as fpp
    keeper = "https://dchub.cloud/facilities/007-hebergement-paris-a8b78433"
    real = fpp._drained_twin_url
    fpp._drained_twin_url = lambda _id: keeper
    try:
        html = fpp._render_profile(
            {"name": "007 Hebergement Paris", "provider": "007 Hebergement Paris",
             "city": "Paris", "country": "FR", "id": "007-hebergement-paris-paris-fr",
             "canonical_slug": "007-hebergement-paris-d128fc26",
             "_src_table": "facilities"},
            "007-hebergement-paris-d128fc26")
    finally:
        fpp._drained_twin_url = real
    assert f'<link rel="canonical" href="{keeper}"' in html, \
        "a drained legacy page still declares ITSELF canonical"
    # and it stays indexable — a canonical merges, it does not de-index
    assert 'content="index, follow"' in html


def test_a_discovered_page_is_left_self_canonical():
    """The keeper, and every ordinary row, must NOT be sent anywhere. If the
    branch above fired for discovered rows too it would canonicalise pages onto
    each other in a loop."""
    import routes.facility_profile_page as fpp
    real = fpp._drained_twin_url
    fpp._drained_twin_url = lambda _id: "https://dchub.cloud/facilities/WRONG"
    try:
        html = fpp._render_profile(
            {"name": "007 Hebergement Paris", "provider": None, "city": "Paris",
             "country": "FR", "id": 12300071,
             "canonical_slug": "007-hebergement-paris-a8b78433",
             "_src_table": "discovered_facilities"},
            "007-hebergement-paris-a8b78433")
    finally:
        fpp._drained_twin_url = real
    assert '<link rel="canonical" href="https://dchub.cloud/facilities/' \
           '007-hebergement-paris-a8b78433"' in html
    assert "WRONG" not in html


def test_the_drain_fork_is_the_url_that_was_dropped_and_the_keeper_survived():
    """Not just "one URL" — the RIGHT one. The discovered keeper must survive
    and the legacy drain copy must be the one dropped, because the keeper is
    the row the pipeline keeps updating and the row the page lookup prefers.
    A change that kept the fork and dropped the keeper would satisfy the
    duplicate test above and still be wrong."""
    cur = _Cur()
    slugs = _facility_slugs(_run_builder(cur))
    assert "007-hebergement-paris-a8b78433" in slugs, slugs
    assert "007-hebergement-paris-d128fc26" not in slugs, slugs


def test_a_facility_with_no_twin_is_never_dropped():
    """The 2026-07-28 failure mode, pinned: a duplicate filter that removes a
    live page's only URL. 57 of 58 slugs were left with no keeper then, and a
    separate drop-set cost 21 live pages their sitemap entry."""
    cur = _Cur()
    slugs = _facility_slugs(_run_builder(cur))
    assert "equinix-dc5-11111111" in slugs, slugs


def test_the_legacy_url_is_dropped_even_when_its_keeper_is_not_emitted():
    """★★★ REVERSED 2026-09-12 (r-selfcanon-unconditional), on measurement.

    This asserted the opposite: that a legacy URL survives while its keeper is
    absent from the artefact, on the reasoning that the facility would otherwise
    lose its only URL. That read as a safety property and was not one, because
    THE PAGE DOES NOT CONSULT IT. _drained_twin_url canonicalises this row at
    the keeper from the DB facts alone — no capacity gate, no junk/contentless
    filter, no seen_slugs — so a kept twin is a page that canonicalises
    elsewhere, i.e. a guaranteed GSC "Alternate page with proper canonical".

    MEASURED 2026-09-12 on the live sitemap: sitemap-facilities-1.xml, the
    shard Google and Bing actually read, advertised 66 URLs that were
    0-for-66 self-canonical. All 66 were exactly this: the keeper capacity-
    gated out of the gated build, the twin kept by the old condition. 59 of
    the 66 canonical targets sit in the ungated AI family and NONE in the
    gated shard.

    The facility does lose its GATED URL here, and that is the honest outcome:
    the URL we were advertising could never be indexed, and the keeper is
    published in the ungated family. This is _noncanon_slugs' contract, which
    has dropped unconditionally since 2026-08-01.
    """
    cur = _Cur(discovered=[DISCOVERED[1]])      # keeper row removed
    slugs = _facility_slugs(_run_builder(cur))
    assert "007-hebergement-paris-d128fc26" not in slugs, slugs
    # ★ and the control still stands: a facility with NO twin link keeps its
    #   URL. Without this the assertion above is also satisfied by a builder
    #   that emits nothing at all.
    assert "equinix-dc5-11111111" in slugs, slugs


def test_identity_is_the_rendered_headline_not_the_slug():
    """The pair disagrees about `provider` — that is WHY the slugs differ — so
    any identity keyed on stored columns misses them. Keyed on what the page
    renders, they are one facility."""
    df = identity_key("007 Hebergement Paris", None, "Paris", None, "FR")
    lg = identity_key("007 Hebergement Paris", "007 Hebergement Paris",
                      "Paris", None, "FR")
    assert df == lg
    # and it still separates two genuinely different buildings whose names
    # differ by one site code — the Amazon IAD85/IAD75 class v3 exists to spare
    assert (identity_key("Amazon IAD85", "Amazon", "Manassas", "VA", "US")
            != identity_key("Amazon IAD75", "Amazon", "Manassas", "VA", "US"))


# ── r-twin-pointer: the legacy row the drain never touched ───────────────

def test_the_builder_actually_ran_the_twin_pointer_query():
    """A FLOOR, for the same reason the drain-fork floor exists: the duplicate
    test above passes trivially if the twin arm never runs and the legacy row
    never reaches the emit loop. This pins that the shipped builder issued the
    discovered_twin_id lookup, so "no duplicates" is a verdict, not an
    absence."""
    cur = _Cur()
    _run_builder(cur)
    joined = " || ".join(cur.seen).lower()
    assert "join discovered_facilities d on d.id = f.discovered_twin_id" in joined, \
        "builder never issued the twin-pointer lookup"


def test_the_twinned_legacy_url_is_dropped_and_the_keeper_survived():
    """Not just "one URL" — the RIGHT one. The discovered keeper is the row the
    pipeline keeps updating and the row /facilities/<slug> prefers, so it must
    be the survivor and the independently-ingested legacy copy the casualty."""
    cur = _Cur()
    slugs = _facility_slugs(_run_builder(cur))
    assert "equinix-fr5-aaaaaaaa" in slugs, slugs
    assert "equinix-fr5-bbbbbbbb" not in slugs, slugs


def test_the_twinned_legacy_url_is_dropped_even_when_its_keeper_is_not_emitted():
    """★★★ REVERSED 2026-09-12 with its drain-arm sibling above, for the same
    reason: _twin_pointer_url canonicalises this row at the keeper whatever the
    sitemap decided to emit, so keeping the URL publishes an alternate.

    The 2026-07-28 "a drop-set cost 21 live pages their sitemap entry" lesson is
    carried by the query's NOT EXISTS clause — a slug a LIVE discovered row also
    wears is never in the set at all — not by a membership test against the
    artefact. test_a_facility_with_no_twin_is_never_dropped is its control.
    """
    cur = _Cur(discovered=[r for r in DISCOVERED if r != DISCOVERED_TWIN])
    slugs = _facility_slugs(_run_builder(cur))
    assert "equinix-fr5-bbbbbbbb" not in slugs, slugs
    assert "equinix-dc5-11111111" in slugs, slugs      # the no-twin control


def test_either_link_alone_is_enough_to_drop_the_legacy_url():
    """★★★ REWRITTEN 2026-09-12. This used to pin PRECEDENCE — that where a
    slug carries both links the DRAIN wins, because facility_profile_page tries
    _drained_twin_url first and a sitemap that picked the other keeper would
    leave a KEPT URL whose canonical points somewhere the sitemap dropped.

    That hazard is now structurally impossible in the sitemap, and pinning it
    here would be pinning nothing: main._drained_twin_slugs is a SET, so the
    builder never names a keeper and has no second answer to disagree with. The
    render path still chooses, and test_the_drain_link_is_preferred_in_the_
    render_path_too below is where that choice is pinned.

    What IS load-bearing now: EITHER arm alone drops the URL, so a slug that
    reaches the set through only one of the two queries is not published.
    Written with a drain keeper that IS emitted and a twin keeper that does not
    exist at all, so neither case can be satisfied by the other's keeper."""
    drain_only = _Cur(
        drain_link=[("equinix-fr5-bbbbbbbb", "equinix-fr5-aaaaaaaa")],
        twin_link=[])
    slugs = _facility_slugs(_run_builder(drain_only))
    assert "equinix-fr5-aaaaaaaa" in slugs, slugs      # the drain's keeper
    assert "equinix-fr5-bbbbbbbb" not in slugs, slugs

    twin_only = _Cur(
        drain_link=[],
        twin_link=[("equinix-fr5-bbbbbbbb", "ghost-keeper-99999999")])
    slugs = _facility_slugs(_run_builder(twin_only))
    assert "equinix-fr5-bbbbbbbb" not in slugs, slugs  # keeper absent, still out
    assert "equinix-dc5-11111111" in slugs, slugs      # the no-twin control


def test_a_twinned_legacy_page_canonicalises_to_its_keeper():
    """The other half: the dropped URL still serves 200 and must point at the
    keeper, so Google MERGES the pair instead of merely losing one from the
    sitemap. _twin_pointer_url is stubbed because it reads the DB — what is
    under test is that _render_profile consults it for a legacy-served row."""
    import routes.facility_profile_page as fpp
    keeper = "https://dchub.cloud/facilities/equinix-fr5-aaaaaaaa"
    real_d, real_t = fpp._drained_twin_url, fpp._twin_pointer_url
    fpp._drained_twin_url = lambda _id: None          # no drain link exists
    fpp._twin_pointer_url = lambda _id: keeper
    try:
        html = fpp._render_profile(
            {"name": "Equinix FR5", "provider": "Equinix", "city": "Frankfurt",
             "country": "DE", "id": "peeringdb-1234",
             "canonical_slug": "equinix-fr5-bbbbbbbb",
             "_src_table": "facilities"},
            "equinix-fr5-bbbbbbbb")
    finally:
        fpp._drained_twin_url, fpp._twin_pointer_url = real_d, real_t
    assert f'<link rel="canonical" href="{keeper}"' in html, \
        "an unlinked legacy twin still declares ITSELF canonical"
    assert 'content="index, follow"' in html   # a canonical merges, not de-indexes


def test_the_drain_link_is_preferred_in_the_render_path_too():
    """Same precedence as the sitemap, asserted on the page: when both
    resolvers would answer, the drain's own stamp wins. If these two ever
    disagree, one facility gets a canonical pointing at a URL the sitemap
    dropped."""
    import routes.facility_profile_page as fpp
    real_d, real_t = fpp._drained_twin_url, fpp._twin_pointer_url
    fpp._drained_twin_url = lambda _id: "https://dchub.cloud/facilities/DRAIN"
    fpp._twin_pointer_url = lambda _id: "https://dchub.cloud/facilities/TWIN"
    try:
        html = fpp._render_profile(
            {"name": "Equinix FR5", "provider": "Equinix", "city": "Frankfurt",
             "country": "DE", "id": "peeringdb-1234",
             "canonical_slug": "equinix-fr5-bbbbbbbb",
             "_src_table": "facilities"},
            "equinix-fr5-bbbbbbbb")
    finally:
        fpp._drained_twin_url, fpp._twin_pointer_url = real_d, real_t
    assert 'href="https://dchub.cloud/facilities/DRAIN"' in html
    assert "/facilities/TWIN" not in html


def test_a_discovered_page_is_never_sent_to_a_twin_pointer():
    """The keeper, and every ordinary discovered row, must not be sent
    anywhere. If the branch fired for discovered rows too, pages would
    canonicalise onto each other in a loop."""
    import routes.facility_profile_page as fpp
    real = fpp._twin_pointer_url
    fpp._twin_pointer_url = lambda _id: "https://dchub.cloud/facilities/WRONG"
    try:
        html = fpp._render_profile(
            {"name": "Equinix FR5", "provider": "Equinix", "city": "Frankfurt",
             "country": "DE", "id": 900,
             "canonical_slug": "equinix-fr5-aaaaaaaa",
             "_src_table": "discovered_facilities"},
            "equinix-fr5-aaaaaaaa")
    finally:
        fpp._twin_pointer_url = real
    assert '<link rel="canonical" href="https://dchub.cloud/facilities/' \
           'equinix-fr5-aaaaaaaa"' in html
    assert "WRONG" not in html


# ── the live checker's budget ────────────────────────────────────────────

def test_the_live_checkers_budget_is_pinned_just_above_the_measured_residual():
    """★ A CEILING WITH SLACK IN IT IS THE SAME BUG AS A SCAN WITH NO FLOOR.
    scripts/check_sitemap_selfcanon.py defaulted --max-groups to 400 while the
    live number was 303 — 97 groups of headroom, so the guard would have kept
    exiting 0 all the way back up to the population it exists to catch.

    Measured 2026-09-07 on the LIVE artefact: 3,930 before #4101, 322 after it,
    103 after r-twin-pointer. The budget is 120 — 17 groups of headroom, and far
    below the 322 a regression would return to.

    ★ #4110 set this to 100 from a SIMULATED 80 and the guard failed on its
      first real run. A budget is only honest if it is read off the artefact:
      the simulation anchored on a stale BEFORE and modelled 231 drops where 220
      happened. Re-measure with scripts/check_sitemap_selfcanon.py after any
      change that moves the residual — never re-derive it from a prediction.

    Pinned as LITERALS so raising the ceiling is a code review, not a side
    effect. Both numbers are pinned: MIN_URLS is the floor that stops a failed
    fetch reading as a clean sitemap, and the two must move deliberately."""
    import argparse, ast
    src = open(os.path.join(ROOT, "scripts", "check_sitemap_selfcanon.py"),
               encoding="utf-8").read()
    tree = ast.parse(src)

    floor = next(n.value.value for n in ast.walk(tree)
                 if isinstance(n, ast.Assign)
                 and getattr(n.targets[0], "id", None) == "MIN_URLS")
    assert floor == 2000, floor

    budget = None
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call)
                and getattr(node.func, "attr", None) == "add_argument"
                and node.args and getattr(node.args[0], "value", None) == "--max-groups"):
            budget = next(k.value.value for k in node.keywords if k.arg == "default")
    assert budget == 120, budget
    assert budget < 322, (
        "the budget must sit below the pre-fix live number or a full "
        "regression still exits 0")
    assert budget >= 103, (
        "the budget must sit ABOVE the measured live residual or the guard "
        "cries wolf on every run — which is how a real alarm gets ignored")


# ── r-junk-keeper: never canonicalise onto a slug the sitemap will not emit ──

def test_the_junk_slug_predicate_agrees_with_is_junk_slug():
    """ONE definition, two consumers. The SQL form and the Python form must
    classify identically, or the sitemap and the rel=canonical disagree about
    which keeper is eligible — which is how a canonical lands on a URL the
    sitemap dropped.

    Executed against real Postgres regex semantics via psycopg2's own parser is
    not possible without a DB, so this pins the two on the SAME corpus using
    Python's `re` with the POSIX pattern translated only in the ways POSIX and
    Python actually differ ([0-9] vs \\d, capturing vs non-capturing groups)."""
    import re
    from routes.facility_dedup_v4 import is_junk_slug, junk_slug_sql
    sql = junk_slug_sql("c")
    # pull the two POSIX patterns straight out of the emitted SQL
    pats = re.findall(r"!~ '([^']+)'", sql)
    assert len(pats) == 2, sql
    rx = [re.compile(p) for p in pats]

    def sql_says_junk(slug):
        return any(r.search(slug) for r in rx)

    corpus = [
        "unknown-shb-lljn-ltby-9cb60fd9", "unknown-osm-dc-123-ab12cd34",
        "data-center-343593591-ab12cd34", "equinix-dc5-ab12cd34",
        "shb-lljn-ltby-55880373", "cyrusone-inc-cyrusone-florence-d10242a8",
        "my-unknown-facility-11111111",      # 'unknown' NOT at the start
        "data-center-12-ab12cd34",           # too few digits
    ]
    for slug in corpus:
        assert sql_says_junk(slug) == is_junk_slug(slug), slug
    # and the corpus actually exercises BOTH verdicts, or the loop proves nothing
    assert any(is_junk_slug(s) for s in corpus)
    assert any(not is_junk_slug(s) for s in corpus)


def test_a_junk_slug_keeper_is_refused_and_the_legacy_url_stays():
    """THE DEFECT, on the artefact. The keeper wears 'unknown-%', which the
    sitemap never emits. Consolidating there would point a published page at an
    unadvertised slug. Refusing leaves the legacy URL published."""
    cur = _Cur(discovered=list(DISCOVERED) + [JUNK_KEEPER],
               legacy=list(LEGACY) + [JUNK_ALT],
               drain_link=list(DRAIN_LINK) + [("shb-lljn-ltby-55880373",
                                               "unknown-shb-lljn-ltby-9cb60fd9")])
    slugs = _facility_slugs(_run_builder(cur))
    assert "shb-lljn-ltby-55880373" in slugs, slugs
    # the junk keeper is excluded from the sitemap at source (r-junk-prune)
    assert "unknown-shb-lljn-ltby-9cb60fd9" not in slugs, slugs


def test_the_builder_query_carries_the_shared_junk_predicate():
    """A FLOOR on the test above: it also passes if the drain arm silently
    stopped running. Pins that the keeper query the builder ISSUED contains the
    predicate, so 'the URL survived' is a verdict and not an absence."""
    cur = _Cur()
    _run_builder(cur)
    keeper_q = [q for q in cur.seen
                if "join discovered_facilities d on d.merged_facility_id = f.id"
                in q.lower()]
    assert keeper_q, "builder never issued the drained-twin lookup"
    assert "!~ '^unknown-'" in keeper_q[0], keeper_q[0]


def test_both_readers_call_the_shared_helper_not_a_copy():
    """The predicate must have ONE definition. A pasted copy is how the sitemap
    and the canonical drift apart — asserted on the CALL, via AST, so a literal
    re-spelling of the same SQL does not satisfy it."""
    import ast
    for rel in ("main.py", "routes/facility_profile_page.py"):
        src = open(os.path.join(ROOT, rel), encoding="utf-8").read()
        called = any(
            isinstance(n, ast.Call)
            and getattr(n.func, "id", getattr(n.func, "attr", None))
            in ("junk_slug_sql", "_junk_slug_sql")
            for n in ast.walk(ast.parse(src)))
        assert called, f"{rel} does not CALL junk_slug_sql"
        assert "!~ '^unknown-'" not in src, (
            f"{rel} spells the predicate itself instead of calling the helper")


# ── r-junk-hash8: an all-digit identity hash is not an OSM node id ───────

def test_a_real_facility_named_data_center_is_emitted():
    """THE DEFECT, on the artefact. 'Meta Rosemount Data Center' (100 MW,
    Rosemount MN) is a real page that serves 200, and the junk filter was
    dropping it from the sitemap because its frozen hash8 — 45882878 — happens
    to be all digits. Run through the SHIPPED builder, not a mirror of it."""
    cur = _Cur(discovered=list(DISCOVERED) + [REAL_DIGIT_HASH])
    slugs = _facility_slugs(_run_builder(cur))
    assert "meta-meta-rosemount-data-center-45882878" in slugs, slugs


def test_the_numeric_osm_junk_it_was_meant_to_catch_is_still_dropped():
    """The other half, and the reason this cannot simply be deleted: 674 live
    slugs are genuine OSM junk whose NAME is literally 'Data Center <digits>'.
    Both shapes stay out — the node id immediately before the hash, and the
    node id followed by a city."""
    cur = _Cur(discovered=list(DISCOVERED) + [OSM_NUMERIC_JUNK,
                                              OSM_NUMERIC_JUNK_CITY])
    slugs = _facility_slugs(_run_builder(cur))
    assert "data-center-32538035-fdf34756" not in slugs, slugs
    assert "data-center-343593591-west-chicago-ab12cd34" not in slugs, slugs
    # …and the builder really ran the facility path, so the two absences above
    # are a verdict and not an empty section.
    assert len(slugs) >= len(DISCOVERED), slugs


# ── r-noindex-coherence: never advertise a URL the page noindexes ────────
#
# Measured 2026-09-07 over the live sitemap: 770 of 18,991 facility URLs serve
# robots=noindex, and ALL 770 are util.thin_content.is_contentless (0 OSM-junk,
# 0 NER, 0 headline — those guards work). 763 arrive via the ungated AI family,
# 7 via the r-proven-exempt readmission past the capacity gate.

CONTENTLESS_ROW = ("Aa Telekom Istanbul", None, None, None, "TR",
                   990100, "2026-08-01", "aa-telekom-istanbul-46fa5ef6")
RICH_ROW = ("Nautilus Maine", "Nautilus", "Portland", "ME", "US",
            990101, "2026-08-01", "nautilus-nautilus-maine-c0336e33")


def _ev(rows, contentless_slugs):
    """Evidence tuples for `rows`; the named slugs carry NOTHING."""
    return [(r[7], (None if r[7] in contentless_slugs else "Portland"),
             None, None, None, None) for r in rows]


def test_a_noindexed_contentless_url_is_not_advertised():
    """THE DEFECT, on the artefact. The page for this slug serves
    robots=noindex; the sitemap was still telling Google to index it."""
    disc = list(DISCOVERED) + [CONTENTLESS_ROW, RICH_ROW]
    cur = _Cur(discovered=disc,
               evidence=_ev(disc + list(LEGACY),
                            {"aa-telekom-istanbul-46fa5ef6"}))
    slugs = _facility_slugs(_run_builder(cur))
    assert "aa-telekom-istanbul-46fa5ef6" not in slugs, slugs
    # FLOOR: the row beside it, with a city, is still advertised — so the
    # absence above is a verdict and not an empty section.
    assert "nautilus-nautilus-maine-c0336e33" in slugs, slugs


def test_the_builder_actually_ran_the_contentless_query():
    """A second floor: the test above also passes if the query silently
    stopped being issued and the set stayed empty for a different reason."""
    cur = _Cur()
    _run_builder(cur)
    q = [x for x in cur.seen if "select canonical_slug, city, address" in x.lower()]
    assert q, "builder never issued the contentless-evidence query"
    # it must read BOTH tables — the page resolves discovered first, then legacy
    assert "from discovered_facilities" in q[0].lower(), q[0]
    assert "from facilities" in q[0].lower(), q[0]


def test_a_slug_carried_by_a_rich_row_too_is_kept():
    """_fetch_facility_by_slug orders by power_mw DESC, so when two rows share
    a slug the RICHEST one serves the page — and that page is not noindexed.
    Dropping the URL would remove a real page."""
    disc = list(DISCOVERED) + [CONTENTLESS_ROW, RICH_ROW]
    ev = _ev(disc + list(LEGACY), {"aa-telekom-istanbul-46fa5ef6"})
    # a SECOND row on the same slug, this one carrying a city
    ev.append(("aa-telekom-istanbul-46fa5ef6", "Istanbul", None, None, None, None))
    cur = _Cur(discovered=disc, evidence=ev)
    slugs = _facility_slugs(_run_builder(cur))
    assert "aa-telekom-istanbul-46fa5ef6" in slugs, slugs


def test_an_implausibly_large_contentless_set_is_refused():
    """The blast-radius cap. If the evidence columns go missing, every row
    reads as contentless and this guard would empty the sitemap. Measured rate
    is ~4%; a quarter of the corpus is ~6x that and cannot be real."""
    disc = list(DISCOVERED) + [CONTENTLESS_ROW, RICH_ROW]
    every = {r[7] for r in disc + list(LEGACY)}
    cur = _Cur(discovered=disc, evidence=_ev(disc + list(LEGACY), every))
    slugs = _facility_slugs(_run_builder(cur))
    assert "aa-telekom-istanbul-46fa5ef6" in slugs, (
        "the cap did not fire — a corpus-wide contentless verdict emptied the "
        "sitemap instead of being refused")
    assert "nautilus-nautilus-maine-c0336e33" in slugs, slugs


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
