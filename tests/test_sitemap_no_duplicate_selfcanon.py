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

# (name, provider, city, state, country, id, first_seen, canonical_slug)
DISCOVERED = [
    # the keeper of a drain fork — provider NULL, exactly as the live rows are
    ("007 Hebergement Paris", None, "Paris", None, "FR",
     12300071, "2026-08-30", "007-hebergement-paris-a8b78433"),
    # an ordinary facility with no twin at all — the control. If a change here
    # ever drops this URL, the sitemap is losing real pages, not duplicates.
    ("Equinix DC5", "Equinix", "Ashburn", "VA", "US",
     500, "2026-08-01", "equinix-dc5-11111111"),
]

# the legacy row the drain forked off row 12300071. Same rendered identity
# (provider == name collapses via brand_already_in_name), different hash8.
LEGACY = [
    ("007 Hebergement Paris", "007 Hebergement Paris", "Paris", None, "FR",
     "007-hebergement-paris-paris-fr", "2026-08-30",
     "007-hebergement-paris-d128fc26"),
]

# discovered.merged_facility_id -> facilities.id, i.e. the drain's own stamp
DRAIN_LINK = [("007-hebergement-paris-d128fc26", "007-hebergement-paris-a8b78433")]


class _Cur:
    """Answers only the queries the facility path needs; [] for the rest."""

    def __init__(self, drain_link=DRAIN_LINK, legacy=LEGACY,
                 discovered=DISCOVERED):
        self._rows = []
        self.drain_link = drain_link
        self.legacy = legacy
        self.discovered = discovered
        self.seen = []

    def execute(self, sql, params=None):
        q = " ".join(str(sql).split())
        self.seen.append(q)
        low = q.lower()
        if "information_schema.columns" in low:
            self._rows = [(1,)]                      # canonical_slug exists
        elif "from discovered_facilities" in low and "select name, provider" in low:
            self._rows = list(self.discovered)
        elif low.startswith("select name, provider") and "from facilities" in low:
            self._rows = list(self.legacy)
        elif "join discovered_facilities d on d.merged_facility_id = f.id" in low:
            self._rows = list(self.drain_link)       # _drained_keeper
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


def _run_builder(cur):
    """Execute the SHIPPED _build_sitemap_sections against `cur`."""
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
    os.environ["SITEMAP_THIN_GATE_DISABLE"] = "1"   # fixtures carry no power_mw
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


def test_the_legacy_url_survives_when_its_keeper_is_not_emitted():
    """THE SAFETY PROPERTY of main._drained_keeper: a legacy URL is dropped
    only once the keeper's slug is already in seen_slugs. With no discovered
    row to keep, the legacy URL is the facility's ONLY URL and must stay —
    otherwise the facility silently leaves the sitemap entirely."""
    cur = _Cur(discovered=[DISCOVERED[1]])      # keeper row removed
    slugs = _facility_slugs(_run_builder(cur))
    assert "007-hebergement-paris-d128fc26" in slugs, slugs


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


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
