#!/usr/bin/env python3
"""tests/test_sitemap_publishes_only_served_selfcanonical_urls.py

THE TWO WAYS THE SITEMAP ADVERTISED A URL GOOGLE CAN NEVER INDEX, measured on
the live artefact 2026-09-12. NO NETWORK, NO DB — the shipped
`_build_sitemap_sections` is pulled out of main.py and executed against the stub
cursor in tests/test_sitemap_no_duplicate_selfcanon.py, and the redirect
resolver runs against tests/_served_slug_world.

1. NOT SELF-CANONICAL — 66 URLs, and the guard could not see them
----------------------------------------------------------------
    sitemap-facilities-1.xml   66 URLs that are 0-for-66 self-canonical
    all 66                     present in the GATED shard, absent from the
                               UNGATED AI family
    59 of the 66 targets       in the AI family; NONE in the gated shard
    2026-09-07                 the gated-only set was 0 — this is new

That is the gated shard Google and Bing actually read, so all 66 are
guaranteed "Alternate page with proper canonical" rows.

CAUSE: main's drain-fork/twin-pointer drop was conditional on the KEEPER's slug
already being in seen_slugs. In the ungated pass the keeper is emitted first and
the twin is dropped; in the gated pass the keeper is capacity-gated out, the
condition does not fire, and the twin is published — while its page
canonicalises at the keeper regardless, because _drained_twin_url and
_twin_pointer_url consult the DB and nothing else.

★★★ AND THE SUPERSET GUARD IN _rebuild_sitemap_snapshot COMPARED LENGTHS:
`if len(ai_fac) < len(fac)` — 18,741 >= 6,897 passes while 66 members are
missing. A COUNT CANNOT SEE A SET DIFFERENCE. The first test below is that
guard's behaviour, run through the shipped builder both ways.

2. LISTS REDIRECTS — ~19 site-wide
----------------------------------
Three confirmed persistent at both edge and origin, e.g.
/facilities/flexential-dallas-8594e4e2 -> /facilities/flexential-flexential-
dallas-8594e4e2. util/sitemap_redirects.py carries the cause and the reason a
hash8 heuristic cannot decide it.

★ EVERY TEST HERE FAILS ON THE PRE-FIX BUILDER. The first one fails naming the
  legacy twin; see the commit's mutation record.
"""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from tests._served_slug_world import World                       # noqa: E402
from tests.test_sitemap_no_duplicate_selfcanon import (          # noqa: E402
    _Cur, _facility_slugs, _run_builder)

# ── the 66, in fixture form ──────────────────────────────────────────────
#
# One drain fork where the capacity lives on the LEGACY side, which is what
# makes the gated and ungated passes disagree: the keeper has no power_mw so the
# capacity gate withholds it, and the twin has one so the gate keeps it.
KEEPER = ("Iron Mountain AZP-2", None, "Phoenix", "AZ", "US",
          700001, "2026-08-01", "iron-mountain-azp-2-aaaa1111")
TWIN = ("Iron Mountain AZP-2", "Iron Mountain", "Phoenix", "AZ", "US",
        "legacy-azp-2", "2026-08-01", "iron-mountain-azp-2-bbbb2222")
#: an ordinary facility with no twin and a capacity of its own — the control
#: that makes "nothing was emitted" distinguishable from "the twin was dropped"
CONTROL = ("Equinix DC5", "Equinix", "Ashburn", "VA", "US",
           500, "2026-08-01", "equinix-dc5-11111111")

DRAIN = [("iron-mountain-azp-2-bbbb2222", "iron-mountain-azp-2-aaaa1111")]
#: power_mw by frozen slug. The KEEPER is deliberately absent, i.e. NULL.
CAPACITY = {"iron-mountain-azp-2-bbbb2222": 12.0,
            "equinix-dc5-11111111": 40.0}


def _cur():
    return _Cur(discovered=[KEEPER, CONTROL], legacy=[TWIN],
                drain_link=list(DRAIN), twin_link=[], capacity=CAPACITY)


@pytest.fixture(autouse=True)
def _no_real_main(monkeypatch):
    """The builder's hub-count block reaches db_utils, which imports `main`.
    Importing the real one boots the Flask app and hangs this file when it runs
    alone. A module with no get_pg_connection makes that lookup fail fast into
    the block's own `except` — the same answer it gives in CI today, reached
    without a 34k-line import."""
    import types
    if "main" not in sys.modules:
        monkeypatch.setitem(sys.modules, "main", types.ModuleType("main"))
    monkeypatch.delenv("SITEMAP_REDIRECT_RESOLVE_DISABLE", raising=False)


# ── 1. the superset property, on the artefact ────────────────────────────

def test_the_gated_set_is_a_subset_of_the_ungated_one():
    """★★★ THE LOAD-BEARING GUARD. The ungated build is the gated query minus
    one AND clause, so every gated URL must appear in the ungated set. On the
    pre-fix builder it does not: the gated pass publishes the legacy twin
    (keeper capacity-gated out, so the old `keeper in seen_slugs` condition
    never fires) and the ungated pass drops it (keeper emitted first). That is
    exactly the 66 URLs measured live on 2026-09-12, and
    _rebuild_sitemap_snapshot's `len(ai_fac) < len(fac)` cannot see any of them.
    """
    gated = set(_facility_slugs(_run_builder(_cur(), thin_gate=True)))
    ungated = set(_facility_slugs(_run_builder(_cur(), thin_gate=False)))

    # anti-vacuity: an empty artefact is a subset of everything
    assert "equinix-dc5-11111111" in gated, gated
    assert "equinix-dc5-11111111" in ungated, ungated
    # and the gate must actually be biting, or the two passes are one pass
    assert "iron-mountain-azp-2-aaaa1111" in ungated, ungated
    assert "iron-mountain-azp-2-aaaa1111" not in gated, (
        "the capacity gate did not withhold the keeper — the fixture no longer "
        "reproduces the shape that produced the 66")

    missing = gated - ungated
    assert not missing, (
        "the gated sitemap advertises {} URL(s) the ungated build does not "
        "have, so they are not self-canonical and are guaranteed 'Alternate "
        "page with proper canonical': {}".format(len(missing), sorted(missing)))


def test_a_page_that_canonicalises_elsewhere_is_never_advertised():
    """The same defect stated as the contract it breaks, and asserted on the
    member rather than on a set difference — so a builder that dropped BOTH
    sides would fail the test above and pass this one, and vice versa."""
    gated = set(_facility_slugs(_run_builder(_cur(), thin_gate=True)))
    assert "iron-mountain-azp-2-bbbb2222" not in gated, (
        "the legacy twin is published while its page canonicalises at the "
        "keeper; the capacity gate withholding the keeper does not make the "
        "twin self-canonical, because the page never consults the gate")


def test_the_builder_really_ran_the_drain_lookup_in_the_gated_pass():
    """A FLOOR on both tests above: they also pass if the drain arm never ran
    in the gated pass and the twin was dropped for some unrelated reason."""
    cur = _cur()
    _run_builder(cur, thin_gate=True)
    joined = " || ".join(cur.seen).lower()
    assert "join discovered_facilities d on d.merged_facility_id = f.id" in joined
    assert "coalesce(power_mw, 0) > 0" in joined, (
        "the gated pass issued no capacity-gated query — thin_gate=True is not "
        "reaching the builder and both passes above are the same build")


# ── 2. redirects ─────────────────────────────────────────────────────────

def _flexential_world():
    """The live shape: ONE frozen row wearing the DOUBLED provider spelling.
    The sitemap emits the DEDUPED spelling for a not-yet-frozen row of the same
    identity; both share hash8 because the hash keys on provider|name, not on
    the slug. _fetch_facility_by_slug misses on the frozen-slug arms, lands on
    this row through the hash8 arm, and _twin_redirect_target case A 301s to
    its frozen slug.

    ★ Only the frozen row is in the world. The page picks among same-hash8 rows
      with an ORDER BY the world does not implement (and refuses to guess at);
      this pins the case where the frozen row wins, which is the case that
      produced the three measured redirects.
    """
    from routes.facility_slug import stable_hash8
    h = stable_hash8("Flexential", "Flexential Dallas")
    frozen = "flexential-flexential-dallas-" + h
    emitted = "flexential-dallas-" + h
    world = World(discovered=[{
        "id": 900001, "name": "Flexential Dallas", "provider": "Flexential",
        "city": "Dallas", "state": "TX", "country": "US",
        "canonical_slug": frozen, "is_duplicate": 0, "duplicate_of_id": None,
        "address": None, "latitude": None, "longitude": None,
        "power_mw": 30.0,
    }])
    return world, emitted, frozen


class _LentConn:
    """A connection the caller owns. served_slugs takes a cursor off it; with
    the world installed no statement ever reaches that cursor."""

    def __init__(self):
        self.closed = 0

    def cursor(self):
        return None

    def rollback(self):
        pass

    def close(self):
        self.closed += 1


def test_the_resolver_names_a_slug_the_page_301s_away_from(monkeypatch):
    """THE DECISION, through the REAL resolver. util.sitemap_redirects calls
    facility_profile_page.served_slugs, and every redirect rule here is that
    module's own — nothing in this file decides a redirect."""
    import routes.facility_profile_page as fpp
    from util.sitemap_redirects import redirecting_slug_set
    world, emitted, frozen = _flexential_world()
    world.install_batch(monkeypatch, fpp)

    out = redirecting_slug_set(_LentConn(),
                               [emitted, frozen, "equinix-dc5-11111111"])
    assert emitted in out, (
        "{} 301s to {} and the resolver did not notice".format(emitted, frozen))
    assert frozen not in out, "the redirect TARGET must not be dropped"
    assert "equinix-dc5-11111111" not in out, (
        "a slug that matches no row at all resolves to itself and must be kept")


def test_a_lent_connection_is_not_closed(monkeypatch):
    """The builder owns the connection it lends and closes it in its own
    finally. served_slugs closing it too would hand the pool a double-close and
    would break the lender's next use of it."""
    import routes.facility_profile_page as fpp
    from util.sitemap_redirects import redirecting_slug_set
    world, emitted, _frozen = _flexential_world()
    world.install_batch(monkeypatch, fpp)

    lent = _LentConn()
    redirecting_slug_set(lent, [emitted])
    assert lent.closed == 0, "served_slugs closed a connection it was lent"
    # and the unlent path still closes its own — the world counts that
    fpp.served_slugs([emitted])
    assert world.closed == 1, (
        "served_slugs stopped closing the connection it opened itself")


def test_an_implausible_redirect_set_is_refused(monkeypatch):
    """Fail-open, like every other set in the builder: a resolver that has gone
    wrong must not be able to shrink the sitemap. Empty means emit everything."""
    import routes.facility_profile_page as fpp
    import util.sitemap_redirects as sr
    monkeypatch.setattr(fpp, "served_slugs",
                        lambda slugs, **_k: {s: "somewhere-else" for s in slugs})
    slugs = ["s-%08d" % i for i in range(2000)]
    assert sr.redirecting_slug_set(_LentConn(), slugs) == set(), (
        "a resolver claiming every URL redirects was believed")
    # ...and a plausible one is still honoured, so the cap is not just "off"
    monkeypatch.setattr(
        fpp, "served_slugs",
        lambda s, **_k: {x: ("elsewhere" if x == "s-00000001" else x) for x in s})
    assert sr.redirecting_slug_set(_LentConn(), slugs) == {"s-00000001"}


def test_the_kill_switch_turns_the_resolver_off(monkeypatch):
    import routes.facility_profile_page as fpp
    import util.sitemap_redirects as sr
    monkeypatch.setattr(fpp, "served_slugs",
                        lambda s, **_k: {x: "elsewhere" for x in s})
    monkeypatch.setenv("SITEMAP_REDIRECT_RESOLVE_DISABLE", "1")
    assert sr.redirecting_slug_set(_LentConn(), ["a-11111111"]) == set()


# ── 3. the resolver is actually wired into the builder ───────────────────

def test_the_builder_consults_the_redirect_resolver(monkeypatch):
    """A FLOOR on the test below: it passes trivially if the builder never
    calls the resolver at all. Pins that the SHIPPED builder hands it the slugs
    it is about to publish."""
    import util.sitemap_redirects as sr
    seen = {}

    def _spy(conn, slugs):
        seen["slugs"] = list(slugs)
        return set()

    monkeypatch.setattr(sr, "redirecting_slug_set", _spy)
    slugs = _facility_slugs(_run_builder(_cur()))
    assert "slugs" in seen, "the builder never consulted the redirect resolver"
    assert seen["slugs"] == slugs, (
        "the resolver was handed a different list from the one published")


def test_a_redirecting_slug_is_not_emitted(monkeypatch):
    """END TO END. The resolver's verdict must reach the artefact — and only
    that URL: a filter that drops the wrong rows is the 2026-07-28 failure."""
    import util.sitemap_redirects as sr
    monkeypatch.setattr(
        sr, "redirecting_slug_set",
        lambda _conn, _slugs: {"iron-mountain-azp-2-aaaa1111"})
    slugs = _facility_slugs(_run_builder(_cur()))
    assert "iron-mountain-azp-2-aaaa1111" not in slugs, slugs
    assert "equinix-dc5-11111111" in slugs, slugs


def test_a_resolver_failure_leaves_the_sitemap_whole(monkeypatch):
    """Fail-open at the call site too. A raising resolver must cost the
    artefact nothing — a bigger sitemap is a smaller failure than an empty
    one, the reasoning every sibling set in the builder records."""
    import util.sitemap_redirects as sr

    def _boom(_conn, _slugs):
        raise RuntimeError("replica unavailable")

    monkeypatch.setattr(sr, "redirecting_slug_set", _boom)
    slugs = _facility_slugs(_run_builder(_cur()))
    assert "equinix-dc5-11111111" in slugs, slugs
    assert "iron-mountain-azp-2-aaaa1111" in slugs, slugs
