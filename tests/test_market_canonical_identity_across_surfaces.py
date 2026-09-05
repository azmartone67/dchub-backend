"""One market, one canonical slug, one score — across every surface.

r-market-canon-split (2026-09-05).

WHAT WAS MEASURED
-----------------
Live through the edge, cache-busted, 2026-09-05. Three of the alias-twin
pairs in util/market_aliases.py had /markets and /dcpi pointing in OPPOSITE
directions, and both sides of each pair declared themselves canonical:

    /dcpi/northern-virginia  301 -> /dcpi/ashburn        DCPI 27.4
    /markets/ashburn         301 -> /markets/northern-virginia   score 11.7
      both <link rel=canonical> -> self
      sitemap-dcpi.xml lists /dcpi/ashburn
      sitemap-markets.xml lists /markets/northern-virginia

    /dcpi/silicon-valley     301 -> /dcpi/santa-clara     DCPI 42.4
    /markets/silicon-valley  200 self-canonical           score 29.9
    /markets/santa-clara     200 self-canonical           score 42.9
      sitemap-markets.xml listed BOTH

    /dcpi/portland           301 -> /dcpi/portland-or
    /markets/portland-or     301 -> /markets/portland

    /pockets/<either side>   200 self-canonical, for all three pairs
      /pockets/ashburn "DCPI composite score -4.3"
      /pockets/northern-virginia "DCPI composite score -109.6"

The other seven pairs (dallas, cheyenne, columbus, the-dalles, dc …) agreed.
So this is not a one-off typo in one dict: it is what happens when four
surfaces each keep their own copy of "which slug names this market".

These are the pages AI assistants cite. An assistant asking about Ashburn got
27.4, 11.7, -4.3 or -109.6 depending on which surface it landed on, and each
page told it the others did not exist.

WHAT THIS GUARD ASSERTS
-----------------------
Two properties, both parametrised over EVERY alias in DCPI_METRO_ALIASES
rather than over the pair that was reported:

  A. CANON AGREEMENT. Every surface resolves an alias to the same slug, and
     that slug is util.market_aliases.canonical_slug's answer. A canonical
     slug redirects nowhere. Nothing advertises (sitemap, curated list, seed
     list) a slug that redirects.

  B. NO SURFACE CAN READ A RETIRED TWIN'S SCORE. The single-market readers
     are EXECUTED against a recording cursor, and the SQL that actually
     reaches the driver must (1) carry PUBLISHED_ONLY and (2) bind the
     canonical slug, never the alias. That is the property that makes two
     different published scores for one market unrepresentable — the frozen
     twin row is unreachable by construction, not by convention.

     Executed, not grepped: `assert "PUBLISHED_ONLY" in source` stays green
     when the predicate is interpolated into a query nobody runs, and stays
     green when the alias is never resolved. Both of those were live defects.

Source-level imports only — never the Flask app (routes/dcpi builds MARKETS
at import time, which needs a DB).
"""
import pytest

from util.market_aliases import DCPI_METRO_ALIASES, canonical_slug
from util.dcpi_score_row import PUBLISHED_ONLY

from routes.market_deep_dive import (
    CURATED_MARKET_SLUGS,
    MARKETS_CANONICAL_REDIRECT,
    listable_market_slug,
)
from routes import market_brief, pockets, market_deep_dive


# ── the twin inventory this guard is parametrised over ──────────────────
#: Every (alias, canonical) pair the alias table knows. Not a hand-listed
#: subset: adding a pair to util/market_aliases.py automatically extends every
#: assertion below, which is the only way this guard keeps up with the map it
#: is guarding.
ALIAS_PAIRS = sorted(DCPI_METRO_ALIASES.items())
CANONICALS = sorted({c for _, c in ALIAS_PAIRS})


def test_the_twin_inventory_is_not_empty():
    """A parametrised guard over an empty list passes vacuously.

    Pinned at the ten pairs live on 2026-09-05 plus a floor, so deleting the
    alias table to make this file green fails here first.
    """
    assert len(ALIAS_PAIRS) >= 10, (
        f"only {len(ALIAS_PAIRS)} alias pairs — every parametrised test below "
        "would be near-vacuous")
    for probe in ("northern-virginia", "silicon-valley", "portland"):
        assert probe in DCPI_METRO_ALIASES, (
            f"{probe} left the alias table; it is one of the three pairs this "
            "guard was written for")


# ═══════════════════════════════════════════════════════════════════════
# A. CANON AGREEMENT
# ═══════════════════════════════════════════════════════════════════════

def _markets_canon(slug):
    """What /markets/<slug> treats as this market's slug."""
    return MARKETS_CANONICAL_REDIRECT.get(slug, slug)


def _brief_canon(slug):
    """What /markets/<slug>/brief treats as this market's slug."""
    return market_brief._canonical(slug)


def _pockets_canon(slug):
    """What /pockets/<slug> treats as this market's slug — measured by
    actually driving the route, so a redirect that is written but never
    reached cannot pass.

    `_get_db` is stubbed to None for the duration: a slug that does NOT
    redirect falls through to _fetch_pocket_detail, whose real _get_db does
    `from main import get_pg_connection`, and tests must not import main
    (the green-main convention — see tests/conftest.py). The stub only
    shortens the no-redirect path to a 404; it cannot affect the redirect,
    which is decided before any DB call.
    """
    from flask import Flask
    app = Flask(__name__)
    app.register_blueprint(pockets.pockets_bp)
    real_get_db = pockets._get_db
    pockets._get_db = lambda: None
    try:
        with app.test_client() as client:
            resp = client.get(f"/pockets/{slug}")
    finally:
        pockets._get_db = real_get_db
    if resp.status_code in (301, 308):
        return (resp.headers["Location"] or "").rsplit("/", 1)[-1]
    return slug


def _dcpi_canon(slug):
    """What /dcpi/<slug> and /api/v1/dcpi/scores/<slug> treat as this
    market's slug. routes/dcpi.py cannot be imported without a DB; this is
    the function it resolves through, and PR #3841's handle_poe_query too."""
    return canonical_slug(slug) or slug


SURFACES = {
    "dcpi":          _dcpi_canon,
    "markets":       _markets_canon,
    "markets-brief": _brief_canon,
    "pockets":       _pockets_canon,
}


@pytest.mark.parametrize("alias,canonical", ALIAS_PAIRS)
def test_every_surface_resolves_an_alias_to_the_same_slug(alias, canonical):
    """The reported defect, generalised: no two surfaces may disagree."""
    answers = {name: fn(alias) for name, fn in SURFACES.items()}
    disagreeing = {n: a for n, a in answers.items() if a != canonical}
    assert not disagreeing, (
        f"surfaces disagree about /{alias}: expected {canonical!r} everywhere, "
        f"got {answers!r}. util.market_aliases is the canon (PR #3841: "
        f"'Northern Virginia is tracked as Ashburn'); a surface that resolves "
        f"the other way publishes a second identity for one market.")


@pytest.mark.parametrize("canonical", CANONICALS)
def test_a_canonical_slug_redirects_nowhere(canonical):
    """The inverse failure: A->B on one surface and B->A on another is two
    disagreements, and the test above only sees the first of them."""
    for name, fn in SURFACES.items():
        assert fn(canonical) == canonical, (
            f"{name} redirects the CANONICAL slug {canonical!r} to "
            f"{fn(canonical)!r} — that is the /markets/ashburn -> "
            f"/markets/northern-virginia direction this guard exists to stop.")


@pytest.mark.parametrize("alias,canonical", ALIAS_PAIRS)
def test_no_surface_advertises_a_slug_that_redirects(alias, canonical):
    """A sitemap or hub that lists a 301 is publishing a duplicate identity
    even when the redirect direction is right."""
    assert alias not in CURATED_MARKET_SLUGS, (
        f"{alias!r} is in CURATED_MARKET_SLUGS, which sitemap-markets.xml "
        f"emits unconditionally, but /markets/{alias} 301s to {canonical}")
    assert alias not in market_brief.SEED_MARKETS, (
        f"{alias!r} is a market-brief seed but /markets/{alias}/brief 301s")
    assert listable_market_slug(alias, set()) is None, (
        f"the sitemap's DB arm would emit /markets/{alias}, which 301s")
    assert canonical not in MARKETS_CANONICAL_REDIRECT, (
        f"{canonical!r} both is a redirect target and redirects itself")


def test_canonical_slugs_are_what_the_curated_surfaces_publish():
    """Every curated/seeded slug is already canonical — not merely 'not an
    alias', which a typo would also satisfy."""
    for slug in tuple(CURATED_MARKET_SLUGS) + tuple(market_brief.SEED_MARKETS):
        assert canonical_slug(slug) == "", (
            f"{slug!r} is published as a market page but canonicalises to "
            f"{canonical_slug(slug)!r}")


def test_the_brief_storage_map_never_becomes_a_second_canon():
    """MARKETS_DEEP_DIVE_PAGE_CANON is a market_deep_dives ROW KEY map. It is
    keyed by canonical slug and its values are storage keys, so it must not be
    read as 'which slug is the page' — that is what it used to be."""
    for page, storage in market_deep_dive.MARKETS_DEEP_DIVE_PAGE_CANON.items():
        assert canonical_slug(page) == "", (
            f"storage map keyed on the alias {page!r}; keys are page slugs "
            f"and a page slug is always canonical")
        assert _markets_canon(page) == page, (
            f"{page!r} keys the brief storage map but /markets/{page} 301s")


# ═══════════════════════════════════════════════════════════════════════
# B. NO SURFACE CAN READ A RETIRED TWIN'S SCORE
# ═══════════════════════════════════════════════════════════════════════

class RecordingCursor:
    """Records every statement and its parameters, answers nothing.

    Returning no rows is deliberate: the assertions are about the QUERY the
    driver receives, which is where the publish predicate and the resolved
    slug either are or are not. A cursor that fed rows back would let a reader
    look correct because of what the fixture handed it.
    """

    def __init__(self):
        self.calls = []

    def execute(self, sql, params=None):
        self.calls.append((str(sql), tuple(params or ())))

    def fetchone(self):
        return None

    def fetchall(self):
        return []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def close(self):
        pass


#: The one alias whose retired row is known to still exist and to carry a
#: different frozen score (measured 2026-09-05: 11.7 against the canonical
#: row's 27.4). Every alias is exercised too, below.
PROBE_ALIAS = "northern-virginia"
PROBE_CANON = "ashburn"


def _scores_reads(cur):
    """The statements a reader issued against market_power_scores."""
    return [(sql, params) for sql, params in cur.calls
            if "market_power_scores" in sql]


def _assert_cannot_reach_the_twin(cur, alias, canonical, surface):
    reads = _scores_reads(cur)
    assert reads, f"{surface} issued no market_power_scores read to check"
    sql, params = reads[0]
    assert PUBLISHED_ONLY in sql, (
        f"{surface} reads market_power_scores without {PUBLISHED_ONLY!r}. The "
        f"retired twins were unpublished, not deleted, so this query can "
        f"return {alias!r}'s frozen row and publish it as a live score.\n"
        f"SQL: {sql}")
    bound = [str(p).lower() for p in params if isinstance(p, str)]
    assert alias not in bound, (
        f"{surface} bound the ALIAS {alias!r} — the alias must be resolved to "
        f"{canonical!r} before the query, or an exact-slug match finds the "
        f"twin row and an ORDER BY that prefers exact matches prefers it.\n"
        f"params: {params}")
    assert canonical in bound, (
        f"{surface} resolved {alias!r} to something other than {canonical!r}; "
        f"bound params were {params}")


@pytest.mark.parametrize("alias,canonical", ALIAS_PAIRS)
def test_markets_page_cannot_read_a_retired_twins_score(alias, canonical):
    cur = RecordingCursor()
    try:
        market_deep_dive._gather_market_facts(cur, alias)
    except Exception:
        pass  # the recording is what is under test, not the return value
    _assert_cannot_reach_the_twin(cur, alias, canonical, "/markets/<slug>")


@pytest.mark.parametrize("alias,canonical", ALIAS_PAIRS)
def test_market_brief_cannot_read_a_retired_twins_score(alias, canonical):
    cur = RecordingCursor()
    try:
        market_brief._section_hero(cur, market_brief._canonical(alias))
    except Exception:
        pass
    _assert_cannot_reach_the_twin(cur, alias, canonical,
                                  "/markets/<slug>/brief")


@pytest.mark.parametrize("alias,canonical", ALIAS_PAIRS)
def test_pockets_cannot_read_a_retired_twins_score(alias, canonical,
                                                  monkeypatch):
    cur = RecordingCursor()

    class _Conn:
        def cursor(self_inner):
            return cur

        def rollback(self_inner):
            pass

    monkeypatch.setattr(pockets, "_get_db", lambda: _Conn())
    monkeypatch.setattr(pockets, "_return_db", lambda c: None)
    try:
        pockets._fetch_pocket_detail(alias)
    except Exception:
        pass
    _assert_cannot_reach_the_twin(cur, alias, canonical, "/pockets/<slug>")


def test_the_ranked_pocket_list_is_published_only(monkeypatch):
    """The list feeds /pockets, the markets hub and the pockets shard of
    sitemap-markets.xml. Without the predicate every retired twin was ranked
    and sitemapped as its own market."""
    cur = RecordingCursor()

    class _Conn:
        def cursor(self_inner):
            return cur

        def rollback(self_inner):
            pass

    monkeypatch.setattr(pockets, "_get_db", lambda: _Conn())
    monkeypatch.setattr(pockets, "_return_db", lambda c: None)
    monkeypatch.setitem(pockets._CACHE, "data", None)
    monkeypatch.setitem(pockets._CACHE, "expires_at", 0.0)
    pockets._fetch_pockets(limit_hint=5)
    reads = _scores_reads(cur)
    assert reads, "no market_power_scores read issued"
    assert PUBLISHED_ONLY in reads[0][0], (
        f"the pocket ranking reads market_power_scores without "
        f"{PUBLISHED_ONLY!r}:\n{reads[0][0]}")


def test_the_probe_pair_is_still_the_one_that_was_measured():
    """Anchors the docstring's live measurement to the map, so the numbers
    above cannot quietly stop describing this pair."""
    assert canonical_slug(PROBE_ALIAS) == PROBE_CANON
